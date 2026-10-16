"""Persistência em SQLite: conversas, mensagens, deduplicação e fila de envios ao CRM.

SQLite atende bem uma clínica. Para trocar por PostgreSQL, basta reimplementar esta classe.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

ESQUEMA = """
CREATE TABLE IF NOT EXISTS conversas (
    id TEXT PRIMARY KEY,
    telefone TEXT NOT NULL,
    nome_contato TEXT NOT NULL DEFAULT '',
    estado TEXT NOT NULL DEFAULT 'bot',      -- bot | aguardando_humano | humano
    passo TEXT,                              -- passo do fluxo aguardando resposta (NULL = início)
    variaveis TEXT NOT NULL DEFAULT '{}',
    motivo_atendimento TEXT NOT NULL DEFAULT '',
    atendente TEXT NOT NULL DEFAULT '',
    ultima_entrada_em TEXT,                  -- última mensagem do paciente (janela de 24h da Meta)
    atualizado_em TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    ativa INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_conversas_telefone ON conversas (telefone, ativa);
CREATE INDEX IF NOT EXISTS ix_conversas_estado ON conversas (estado, ativa);

CREATE TABLE IF NOT EXISTS mensagens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id TEXT NOT NULL REFERENCES conversas (id),
    autor TEXT NOT NULL,                     -- paciente | bot | atendente
    atendente TEXT NOT NULL DEFAULT '',
    tipo TEXT NOT NULL,                      -- texto | opcao | midia | sistema
    conteudo TEXT NOT NULL,
    wa_message_id TEXT,
    criado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mensagens_conversa ON mensagens (conversa_id, id);

CREATE TABLE IF NOT EXISTS mensagens_processadas (
    wa_message_id TEXT PRIMARY KEY,
    criado_em TEXT NOT NULL
);

-- Número conectado pela página "Conectar WhatsApp" (Embedded Signup). Uma linha só (id = 1).
CREATE TABLE IF NOT EXISTS conexao_whatsapp (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    waba_id TEXT NOT NULL,
    phone_number_id TEXT NOT NULL,
    numero TEXT NOT NULL DEFAULT '',
    nome_verificado TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL,
    coexistencia INTEGER NOT NULL,
    pin TEXT NOT NULL DEFAULT '',
    sync_contatos_id TEXT NOT NULL DEFAULT '',
    sync_historico_id TEXT NOT NULL DEFAULT '',
    ultimo_evento_meta TEXT NOT NULL DEFAULT '',
    conectado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fila_crm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rota TEXT NOT NULL,
    payload TEXT NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    ultimo_erro TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL
);
"""


def agora() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def de_iso(texto: str | None) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


class Banco:
    def __init__(self, caminho: str):
        self._con = sqlite3.connect(caminho, check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        with self._lock:
            self._con.executescript(ESQUEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._con.execute(sql, params)

    def _um(self, sql: str, params: tuple = ()) -> dict | None:
        linha = self._exec(sql, params).fetchone()
        return dict(linha) if linha else None

    def _todos(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(l) for l in self._exec(sql, params).fetchall()]

    # ---------------------------------------------------------- deduplicação

    def marcar_processada(self, wa_message_id: str) -> bool:
        """True se a mensagem é nova; False se a Meta está reenviando uma já processada."""
        try:
            self._exec("INSERT INTO mensagens_processadas VALUES (?, ?)", (wa_message_id, iso(agora())))
            return True
        except sqlite3.IntegrityError:
            return False

    # ---------------------------------------------------------- conversas

    def conversa_ativa(self, telefone: str) -> dict | None:
        c = self._um(
            "SELECT * FROM conversas WHERE telefone = ? AND ativa = 1 ORDER BY criado_em DESC LIMIT 1",
            (telefone,),
        )
        return _decodificar(c)

    def nova_conversa(self, telefone: str, nome_contato: str = "") -> dict:
        self._exec("UPDATE conversas SET ativa = 0 WHERE telefone = ? AND ativa = 1", (telefone,))
        cid = uuid.uuid4().hex
        momento = iso(agora())
        self._exec(
            "INSERT INTO conversas (id, telefone, nome_contato, atualizado_em, criado_em) VALUES (?, ?, ?, ?, ?)",
            (cid, telefone, nome_contato, momento, momento),
        )
        return self.conversa(cid)

    def conversa(self, conversa_id: str) -> dict | None:
        return _decodificar(self._um("SELECT * FROM conversas WHERE id = ?", (conversa_id,)))

    def salvar_conversa(self, c: dict) -> None:
        self._exec(
            """UPDATE conversas SET nome_contato = ?, estado = ?, passo = ?, variaveis = ?,
               motivo_atendimento = ?, atendente = ?, ultima_entrada_em = ?, atualizado_em = ?, ativa = ?
               WHERE id = ?""",
            (
                c["nome_contato"], c["estado"], c["passo"], json.dumps(c["variaveis"], ensure_ascii=False),
                c["motivo_atendimento"], c["atendente"], c["ultima_entrada_em"], iso(agora()), c["ativa"], c["id"],
            ),
        )

    def listar_conversas(self, estado: str | None = None, limite: int = 50) -> list[dict]:
        sql = """SELECT c.id, c.telefone, c.nome_contato, c.estado, c.motivo_atendimento, c.atendente,
                        c.atualizado_em, c.ultima_entrada_em,
                        (SELECT conteudo FROM mensagens m WHERE m.conversa_id = c.id ORDER BY m.id DESC LIMIT 1)
                          AS ultima_mensagem
                 FROM conversas c WHERE c.ativa = 1"""
        params: tuple = ()
        if estado:
            sql += " AND c.estado = ?"
            params = (estado,)
        sql += " ORDER BY c.atualizado_em DESC LIMIT ?"
        return self._todos(sql, params + (limite,))

    # ---------------------------------------------------------- mensagens

    def registrar_mensagem(self, conversa_id: str, autor: str, tipo: str, conteudo: str,
                           wa_message_id: str | None = None, atendente: str = "") -> None:
        self._exec(
            """INSERT INTO mensagens (conversa_id, autor, atendente, tipo, conteudo, wa_message_id, criado_em)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (conversa_id, autor, atendente, tipo, conteudo, wa_message_id, iso(agora())),
        )

    def mensagens(self, conversa_id: str, limite: int = 200) -> list[dict]:
        return self._todos(
            "SELECT * FROM (SELECT * FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conversa_id, limite),
        )

    # ---------------------------------------------------------- fila de envios ao CRM

    def enfileirar(self, rota: str, payload: dict, erro: str) -> None:
        self._exec(
            "INSERT INTO fila_crm (rota, payload, tentativas, ultimo_erro, criado_em) VALUES (?, ?, 1, ?, ?)",
            (rota, json.dumps(payload, ensure_ascii=False), erro, iso(agora())),
        )

    def fila_pendente(self) -> list[dict]:
        itens = self._todos("SELECT * FROM fila_crm ORDER BY id")
        for i in itens:
            i["payload"] = json.loads(i["payload"])
        return itens

    def remover_da_fila(self, item_id: int) -> None:
        self._exec("DELETE FROM fila_crm WHERE id = ?", (item_id,))

    def falha_na_fila(self, item_id: int, erro: str) -> None:
        self._exec("UPDATE fila_crm SET tentativas = tentativas + 1, ultimo_erro = ? WHERE id = ?", (erro, item_id))

    # ---------------------------------------------------------- conexão com o WhatsApp

    def salvar_conexao(self, **dados) -> None:
        dados = {"conectado_em": iso(agora()), **dados}
        colunas = ", ".join(dados)
        self._exec(f"INSERT OR REPLACE INTO conexao_whatsapp (id, {colunas}) VALUES (1, {', '.join('?' * len(dados))})",
                   tuple(dados.values()))

    def conexao(self) -> dict | None:
        return self._um("SELECT * FROM conexao_whatsapp WHERE id = 1")

    def registrar_evento_meta(self, evento: str) -> None:
        self._exec("UPDATE conexao_whatsapp SET ultimo_evento_meta = ? WHERE id = 1", (evento,))

    # ---------------------------------------------------------- testes

    def envelhecer_conversa(self, conversa_id: str, horas: float) -> None:
        """Usado nos testes para simular inatividade."""
        passado = iso(agora() - timedelta(hours=horas))
        self._exec("UPDATE conversas SET atualizado_em = ?, ultima_entrada_em = ? WHERE id = ?",
                   (passado, passado, conversa_id))


def _decodificar(c: dict | None) -> dict | None:
    if c:
        c["variaveis"] = json.loads(c["variaveis"])
    return c
