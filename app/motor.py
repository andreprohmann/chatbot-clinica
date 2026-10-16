"""Motor da conversa: recebe cada mensagem do paciente, aplica o protocolo de crise,
executa o fluxo e cuida da passagem para a equipe (tela de atendimento do CRM)."""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import timedelta

from .acoes import ACOES, Contexto
from .config import Config
from .crise import DetectorCrise, normalizar
from .crm import ClienteCRM, CRMIndisponivel
from .db import Banco, agora, de_iso
from .fluxo import Fluxo
from .whatsapp import Eco, Entrada, Opcao

log = logging.getLogger("bot.motor")

ESTADO_BOT = "bot"
ESTADO_AGUARDANDO = "aguardando_humano"
ESTADO_HUMANO = "humano"

ATENDENTE_CELULAR = "Celular da clínica"   # quem responde pelo app WhatsApp Business (Coexistência)
JANELA_ATENDIMENTO = timedelta(hours=24)  # regra da Meta para mensagens livres
EXPIRA_COM_EQUIPE = timedelta(hours=24)   # conversa aguardando ou com atendente (exceto crise)
MAX_PASSOS_SEGUIDOS = 50                  # proteção contra laço infinito no fluxo


class ErroAtendimento(Exception):
    def __init__(self, mensagem: str, status: int = 409):
        super().__init__(mensagem)
        self.status = status


def preencher(texto: str, variaveis: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(variaveis.get(m.group(1)) or ""), texto)


class Motor:
    def __init__(self, config: Config, banco: Banco, whatsapp, crm: ClienteCRM, fluxo: Fluxo):
        self.config = config
        self.banco = banco
        self.whatsapp = whatsapp
        self.crm = crm
        self.fluxo = fluxo
        self.detector = DetectorCrise(fluxo.padroes_crise)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ================================================================ entrada do paciente

    async def processar(self, entrada: Entrada) -> None:
        # Uma mensagem por vez por telefone: evita respostas fora de ordem quando o paciente digita rápido.
        async with self._locks[entrada.telefone]:
            if entrada.wa_message_id and not self.banco.marcar_processada(entrada.wa_message_id):
                log.info("Mensagem %s repetida pela Meta; ignorada.", entrada.wa_message_id)
                return
            try:
                await self._processar(entrada)
            except Exception:
                log.exception("Erro ao processar mensagem de %s", entrada.telefone)

    async def _processar(self, e: Entrada) -> None:
        c = self._conversa_para(e)
        c["ultima_entrada_em"] = agora().isoformat(timespec="seconds")
        if e.nome_contato:
            c["nome_contato"] = e.nome_contato
        self.banco.registrar_mensagem(c["id"], "paciente", e.tipo, e.texto, e.wa_message_id)
        self.banco.salvar_conversa(c)

        em_crise = e.tipo != "midia" and self.detector.detectar(e.texto)

        if c["estado"] == ESTADO_HUMANO:
            # Atendente já está na conversa: o bot fica em silêncio e só avisa o CRM.
            if em_crise:
                await self._enviar_crm("/alertas-crise", self._dados_alerta(c, e.texto))
            await self._notificar(c, "nova_mensagem", e.texto)
            return

        if em_crise:
            await self._protocolo_crise(c, e.texto)
            return

        if c["estado"] == ESTADO_AGUARDANDO:
            await self._notificar(c, "nova_mensagem", e.texto)
            return

        if e.tipo == "texto" and normalizar(e.texto) == "menu":
            c["variaveis"]["saudacao"] = "Claro! Como posso ajudar?"
            await self._executar(c, self.fluxo.menu)
        elif not c["passo"]:
            await self._executar(c, self.fluxo.inicio)
        else:
            await self._responder(c, e)

    async def processar_eco(self, eco: Eco) -> None:
        """A equipe respondeu pelo celular: registra no histórico e o bot sai da conversa."""
        async with self._locks[eco.telefone]:
            if eco.wa_message_id and not self.banco.marcar_processada(eco.wa_message_id):
                return
            c = self._conversa_para(Entrada(wa_message_id="", telefone=eco.telefone, nome_contato="",
                                            tipo="texto", texto=""))
            self.banco.registrar_mensagem(c["id"], "atendente", "texto", eco.texto, eco.wa_message_id,
                                          atendente=ATENDENTE_CELULAR)
            if c["estado"] != ESTADO_HUMANO:  # se alguém já assumiu pela tela, mantém
                c.update(estado=ESTADO_HUMANO, atendente=ATENDENTE_CELULAR, passo=None)
            self.banco.salvar_conversa(c)

    def _conversa_para(self, e: Entrada) -> dict:
        c = self.banco.conversa_ativa(e.telefone)
        if c:
            if c["estado"] == ESTADO_AGUARDANDO and c["motivo_atendimento"] == "crise":
                return c  # crise nunca sai da fila sozinha: só a equipe encerra
            if c["estado"] == ESTADO_BOT:
                limite = timedelta(hours=self.config.sessao_expira_horas)
            elif c["atendente"] == ATENDENTE_CELULAR:
                limite = timedelta(hours=self.config.pausa_celular_horas)
            else:
                limite = EXPIRA_COM_EQUIPE
            if agora() - de_iso(c["atualizado_em"]) <= limite:
                return c
            log.info("Conversa %s expirou; iniciando outra.", c["id"])
        return self.banco.nova_conversa(e.telefone, e.nome_contato)

    # ================================================================ fluxo

    async def _responder(self, c: dict, e: Entrada) -> None:
        nome = c["passo"]
        passo = self.fluxo.passos.get(nome)
        if not passo:  # passo removido do fluxo.yaml depois que a conversa começou
            await self._executar(c, self.fluxo.inicio)
            return

        if passo["tipo"] == "texto_livre":
            if e.tipo == "midia":
                await self._enviar(c, self.fluxo.mensagens["midia"])
                await self._enviar(c, preencher(passo["texto"], c["variaveis"]))
                return
            resposta = e.texto.strip()
            if len(resposta) < 2:
                await self._enviar(c, self.fluxo.mensagens["texto_curto"])
                return
            c["variaveis"][passo["salvar_em"]] = resposta
            c["variaveis"]["tentativas_invalidas"] = 0
            await self._executar(c, passo["proximo"])
            return

        opcoes = self._opcoes_do_passo(nome, passo, c["variaveis"])
        escolhida = self._identificar_opcao(nome, opcoes, e)
        if escolhida is None:
            await self._resposta_invalida(c, nome, opcoes, midia=e.tipo == "midia")
            return

        c["variaveis"]["tentativas_invalidas"] = 0
        item = escolhida["item"]
        if passo["tipo"] == "opcoes_dinamicas":
            campo = passo["salvar_em"]
            c["variaveis"].update({campo: item["id"], f"{campo}_rotulo": item["rotulo"],
                                   f"{campo}_descricao": item.get("descricao", "")})
            destino = passo["proximo"]
        else:
            if passo.get("salvar_em"):
                c["variaveis"][passo["salvar_em"]] = item.get("valor", item["rotulo"])
            destino = item.get("proximo") or passo["proximo"]
        await self._executar(c, destino)

    async def _resposta_invalida(self, c: dict, nome: str, opcoes: list[dict], midia: bool) -> None:
        tentativas = int(c["variaveis"].get("tentativas_invalidas") or 0) + 1
        c["variaveis"]["tentativas_invalidas"] = tentativas
        if tentativas >= 2:
            c["variaveis"]["tentativas_invalidas"] = 0
            await self._transferir(c, "nao_entendeu", self.fluxo.mensagens["nao_entendi_transferir"])
            return
        aviso = self.fluxo.mensagens["midia" if midia else "nao_entendi"]
        await self._enviar(c, aviso, [o["opcao"] for o in opcoes])
        self.banco.salvar_conversa(c)

    async def _executar(self, c: dict, nome: str | None) -> None:
        """Executa passos até chegar a um que espera resposta do paciente (ou ao fim da conversa)."""
        for _ in range(MAX_PASSOS_SEGUIDOS):
            if nome is None:
                c.update(passo=None, variaveis={})  # fim: a próxima mensagem recomeça do início
                self.banco.salvar_conversa(c)
                return
            passo = self.fluxo.passos[nome]
            v = c["variaveis"]
            for chave, valor in (passo.get("definir") or {}).items():
                v[chave] = preencher(str(valor), v)

            tipo = passo["tipo"]
            if tipo == "mensagem":
                await self._enviar(c, preencher(passo["texto"], v))
                nome = passo.get("proximo")
            elif tipo == "definir":
                nome = passo["proximo"]
            elif tipo == "condicao":
                nome = passo["entao"] if str(v.get(passo["variavel"]) or "") == str(passo.get("igual", "")) \
                    else passo["senao"]
            elif tipo == "acao":
                saida = await ACOES[passo["acao"]](self._contexto(c))
                nome = (passo.get("saidas") or {}).get(saida) or passo.get("proximo")
            elif tipo == "transferir":
                await self._transferir(c, passo["motivo"], preencher(passo["texto"], v) if passo.get("texto") else None)
                return
            else:  # opcoes, opcoes_dinamicas, texto_livre: pergunta e espera
                opcoes = self._opcoes_do_passo(nome, passo, v)
                await self._enviar(c, preencher(passo["texto"], v), [o["opcao"] for o in opcoes])
                c["passo"] = nome
                self.banco.salvar_conversa(c)
                return
        log.error("Fluxo excedeu %d passos seguidos na conversa %s; transferindo.", MAX_PASSOS_SEGUIDOS, c["id"])
        await self._transferir(c, "erro_fluxo", None)

    def _opcoes_do_passo(self, nome: str, passo: dict, variaveis: dict) -> list[dict]:
        if passo["tipo"] == "opcoes":
            itens = passo["opcoes"]
        elif passo["tipo"] == "opcoes_dinamicas":
            itens = variaveis.get(passo["fonte"]) or []
        else:
            return []
        return [{"item": it, "opcao": Opcao(id=f"{nome}:{i}", titulo=it["rotulo"], descricao=it.get("descricao", ""))}
                for i, it in enumerate(itens)]

    @staticmethod
    def _identificar_opcao(nome: str, opcoes: list[dict], e: Entrada) -> dict | None:
        if e.tipo == "midia":
            return None
        if e.opcao_id.startswith(f"{nome}:"):  # clique em botão/lista desta pergunta
            indice = e.opcao_id.split(":", 1)[1]
            if indice.isdigit() and int(indice) < len(opcoes):
                return opcoes[int(indice)]
        texto = normalizar(e.texto)
        if texto.isdigit() and 1 <= int(texto) <= len(opcoes):  # "2" = segunda opção
            return opcoes[int(texto) - 1]
        for o in opcoes:
            if normalizar(o["item"]["rotulo"]) == texto:
                return o
        return None

    def _contexto(self, c: dict) -> Contexto:
        return Contexto(telefone=c["telefone"], conversa_id=c["id"], variaveis=c["variaveis"], crm=self.crm,
                        mensagens=self.fluxo.mensagens, enviar_crm=self._enviar_crm)

    # ================================================================ equipe e crise

    async def _transferir(self, c: dict, motivo: str, texto: str | None) -> None:
        if texto:
            await self._enviar(c, texto)
        c.update(estado=ESTADO_AGUARDANDO, motivo_atendimento=motivo, passo=None)
        self.banco.salvar_conversa(c)
        await self._enviar_crm("/atendimento-humano", {
            "conversa_id": c["id"], "telefone": c["telefone"], "nome": self._nome(c), "motivo": motivo,
        })
        aberto = await self.crm.expediente_aberto()
        await self._enviar(c, self.fluxo.mensagens["equipe_dentro_horario" if aberto else "equipe_fora_horario"])

    async def _protocolo_crise(self, c: dict, texto: str) -> None:
        await self._enviar(c, self.fluxo.mensagens["crise"])
        c.update(estado=ESTADO_AGUARDANDO, motivo_atendimento="crise", passo=None)
        self.banco.salvar_conversa(c)
        await self._enviar_crm("/alertas-crise", self._dados_alerta(c, texto))
        if not await self.crm.expediente_aberto():
            await self._enviar(c, self.fluxo.mensagens["crise_fora_horario"])

    def _dados_alerta(self, c: dict, texto: str) -> dict:
        return {"conversa_id": c["id"], "telefone": c["telefone"], "nome": self._nome(c), "mensagem": texto}

    @staticmethod
    def _nome(c: dict) -> str:
        v = c["variaveis"]
        return v.get("paciente_nome") or v.get("nome_completo") or c["nome_contato"]

    # ================================================================ tela de atendimento (CRM)

    async def assumir(self, conversa_id: str, atendente: str) -> dict:
        c = self._conversa_ativa(conversa_id)
        async with self._locks[c["telefone"]]:
            c = self._conversa_ativa(conversa_id)
            if c["estado"] == ESTADO_HUMANO and c["atendente"] not in ("", atendente, ATENDENTE_CELULAR):
                raise ErroAtendimento(f"Conversa já está com {c['atendente']}.")
            c.update(estado=ESTADO_HUMANO, atendente=atendente, passo=None)
            self.banco.salvar_conversa(c)
            self.banco.registrar_mensagem(c["id"], "atendente", "sistema", f"{atendente} assumiu o atendimento",
                                          atendente=atendente)
            return self.banco.conversa(conversa_id)

    async def enviar_como_atendente(self, conversa_id: str, atendente: str, texto: str) -> None:
        c = self._conversa_ativa(conversa_id)
        async with self._locks[c["telefone"]]:
            c = self._conversa_ativa(conversa_id)
            if c["estado"] != ESTADO_HUMANO or c["atendente"] != atendente:
                raise ErroAtendimento("Assuma a conversa antes de responder.")
            ultima = de_iso(c["ultima_entrada_em"])
            if not ultima or agora() - ultima > JANELA_ATENDIMENTO:
                raise ErroAtendimento(
                    "Passaram mais de 24h desde a última mensagem do paciente. Pela regra da Meta, "
                    "só é possível enviar um template aprovado (ex.: pedir que o paciente responda).")
            await self._enviar(c, texto, autor="atendente", atendente=atendente)
            self.banco.salvar_conversa(c)

    async def encerrar(self, conversa_id: str, atendente: str, enviar_despedida: bool = True) -> None:
        c = self._conversa_ativa(conversa_id)
        async with self._locks[c["telefone"]]:
            c = self._conversa_ativa(conversa_id)
            if enviar_despedida:
                ultima = de_iso(c["ultima_entrada_em"])
                if ultima and agora() - ultima <= JANELA_ATENDIMENTO:
                    await self._enviar(c, self.fluxo.mensagens["atendimento_encerrado"], autor="atendente",
                                       atendente=atendente)
            self.banco.registrar_mensagem(c["id"], "atendente", "sistema", f"{atendente} encerrou o atendimento",
                                          atendente=atendente)
            c.update(estado=ESTADO_BOT, passo=None, ativa=0)  # próxima mensagem abre nova conversa com o bot
            self.banco.salvar_conversa(c)

    def _conversa_ativa(self, conversa_id: str) -> dict:
        c = self.banco.conversa(conversa_id)
        if not c or not c["ativa"]:
            raise ErroAtendimento("Conversa não encontrada ou já encerrada.", status=404)
        return c

    # ================================================================ saídas

    async def _enviar(self, c: dict, texto: str, opcoes: list[Opcao] | None = None,
                      autor: str = "bot", atendente: str = "") -> None:
        await self.whatsapp.enviar(c["telefone"], texto, opcoes)
        registro = texto + "".join(f"\n[{o.titulo}]" for o in opcoes or [])
        self.banco.registrar_mensagem(c["id"], autor, "texto", registro, atendente=atendente)

    async def _enviar_crm(self, rota: str, payload: dict) -> None:
        """Envia ao CRM; se falhar, guarda na fila para reenvio (alertas de crise nunca se perdem)."""
        try:
            await self.crm.post(rota, payload)
        except CRMIndisponivel as e:
            log.error("CRM indisponível em %s; guardado na fila. %s", rota, e)
            self.banco.enfileirar(rota, payload, str(e))

    async def _notificar(self, c: dict, tipo: str, texto: str) -> None:
        """Avisa a tela de atendimento sobre novidades. Não é crítico: se falhar, a tela vê no próximo refresh."""
        try:
            await self.crm.post("/eventos-whatsapp", {"tipo": tipo, "conversa_id": c["id"],
                                                      "telefone": c["telefone"], "texto": texto})
        except CRMIndisponivel:
            pass

    async def reenviar_fila(self) -> int:
        enviados = 0
        for item in self.banco.fila_pendente():
            try:
                await self.crm.post(item["rota"], item["payload"])
            except CRMIndisponivel as e:
                self.banco.falha_na_fila(item["id"], str(e))
                continue
            self.banco.remover_da_fila(item["id"])
            enviados += 1
        if enviados:
            log.info("Fila do CRM: %d envio(s) recuperado(s).", enviados)
        return enviados
