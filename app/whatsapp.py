"""Integração com a WhatsApp Cloud API: leitura do webhook, assinatura e envio de mensagens."""

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("bot.whatsapp")

LIMITE_BOTAO = 20       # caracteres no título de um botão
LIMITE_LINHA = 24       # caracteres no título de uma linha de lista
LIMITE_DESCRICAO = 72   # caracteres na descrição de uma linha de lista
LIMITE_CORPO = 1024     # caracteres no corpo de mensagem interativa
MAX_BOTOES = 3
MAX_LINHAS = 10


@dataclass
class Entrada:
    """Mensagem recebida do paciente, já normalizada."""
    wa_message_id: str
    telefone: str
    nome_contato: str
    tipo: str                 # texto | opcao | midia
    texto: str                # texto digitado, ou título da opção escolhida
    opcao_id: str = ""        # id do botão/linha escolhido


@dataclass
class Eco:
    """Mensagem que a equipe enviou pelo app WhatsApp Business no celular (Coexistência)."""
    wa_message_id: str
    telefone: str   # paciente que recebeu
    texto: str


@dataclass
class Opcao:
    id: str
    titulo: str
    descricao: str = ""


def assinatura_valida(corpo: bytes, cabecalho: str | None, app_secret: str) -> bool:
    """Valida o cabeçalho X-Hub-Signature-256 enviado pela Meta."""
    if not app_secret:
        return True  # sem segredo configurado (desenvolvimento)
    if not cabecalho or not cabecalho.startswith("sha256="):
        return False
    esperado = hmac.new(app_secret.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cabecalho.removeprefix("sha256="))


def ler_webhook(payload: dict) -> list[Entrada]:
    """Extrai as mensagens de um payload de webhook. Status de entrega e outros eventos são ignorados."""
    entradas = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            valor = change.get("value", {})
            nomes = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in valor.get("contacts", [])}
            for m in valor.get("messages", []):
                telefone = m.get("from", "")
                base = dict(wa_message_id=m.get("id", ""), telefone=telefone, nome_contato=nomes.get(telefone, ""))
                tipo = m.get("type")
                if tipo == "text":
                    entradas.append(Entrada(**base, tipo="texto", texto=m["text"].get("body", "")))
                elif tipo == "interactive":
                    i = m["interactive"]
                    resposta = i.get("button_reply") or i.get("list_reply") or {}
                    entradas.append(Entrada(**base, tipo="opcao", texto=resposta.get("title", ""),
                                            opcao_id=resposta.get("id", "")))
                elif tipo == "button":  # resposta rápida de template (ex.: lembrete de consulta)
                    b = m["button"]
                    entradas.append(Entrada(**base, tipo="opcao", texto=b.get("text", ""), opcao_id=b.get("payload", "")))
                else:  # áudio, imagem, documento, figurinha, localização...
                    entradas.append(Entrada(**base, tipo="midia", texto=f"[{tipo}]"))
    return entradas


def ler_ecos(payload: dict) -> list[Eco]:
    """Extrai do webhook `smb_message_echoes` as mensagens enviadas pelo celular da clínica."""
    ecos = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("message_echoes", []):
                tipo = m.get("type")
                texto = m["text"].get("body", "") if tipo == "text" else f"[{tipo}]"
                ecos.append(Eco(wa_message_id=m.get("id", ""), telefone=m.get("to", ""), texto=texto))
    return ecos


def ler_eventos_conta(payload: dict) -> list[str]:
    """Avisos `account_update` da Meta (ex.: o número foi desconectado pelo app do celular)."""
    eventos = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "account_update":
                valor = change.get("value", {})
                eventos.append(f"{valor.get('event', 'evento')}: {json.dumps(valor, ensure_ascii=False)[:500]}")
    return eventos


def _corte(texto: str, limite: int) -> str:
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def montar_mensagem(telefone: str, texto: str, opcoes: list[Opcao] | None = None) -> dict:
    """Monta o JSON da Cloud API: texto simples, até 3 botões ou lista de até 10 linhas."""
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": telefone}
    if not opcoes:
        return {**base, "type": "text", "text": {"body": texto, "preview_url": False}}

    corpo = {"text": _corte(texto, LIMITE_CORPO)}
    if len(opcoes) <= MAX_BOTOES and all(len(o.titulo) <= LIMITE_BOTAO and not o.descricao for o in opcoes):
        botoes = [{"type": "reply", "reply": {"id": o.id, "title": o.titulo}} for o in opcoes]
        return {**base, "type": "interactive",
                "interactive": {"type": "button", "body": corpo, "action": {"buttons": botoes}}}

    if len(opcoes) > MAX_LINHAS:
        log.warning("Lista com %d opções; o WhatsApp mostra no máximo %d.", len(opcoes), MAX_LINHAS)
    linhas = []
    for o in opcoes[:MAX_LINHAS]:
        linha = {"id": o.id, "title": _corte(o.titulo, LIMITE_LINHA)}
        if o.descricao:
            linha["description"] = _corte(o.descricao, LIMITE_DESCRICAO)
        linhas.append(linha)
    return {**base, "type": "interactive",
            "interactive": {"type": "list", "body": corpo,
                            "action": {"button": "Ver opções", "sections": [{"title": "Opções", "rows": linhas}]}}}


class ClienteWhatsApp:
    def __init__(self, token: str, phone_number_id: str, api_version: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._http = httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {token}"}, transport=transport)

    async def enviar(self, telefone: str, texto: str, opcoes: list[Opcao] | None = None) -> None:
        resposta = await self._http.post(self._url, json=montar_mensagem(telefone, texto, opcoes))
        if resposta.status_code >= 400:
            log.error("Falha ao enviar para %s: %s %s", telefone, resposta.status_code, resposta.text)
            resposta.raise_for_status()


class ClienteWhatsAppDev:
    """Modo desenvolvimento: guarda as mensagens em vez de enviar (usado nos testes e no simulador)."""

    def __init__(self):
        self.enviadas: list[dict] = []

    async def enviar(self, telefone: str, texto: str, opcoes: list[Opcao] | None = None) -> None:
        self.enviadas.append({"telefone": telefone, "texto": texto, "opcoes": opcoes or [],
                              "payload": montar_mensagem(telefone, texto, opcoes)})
        log.info("[dev] para %s: %s %s", telefone, texto, [o.titulo for o in opcoes or []])
