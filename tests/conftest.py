import hashlib
import hmac
import itertools
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "mock-crm"))

import servidor as crm_simulado  # noqa: E402

from app.config import Config  # noqa: E402
from app.main import criar_app  # noqa: E402
from app.whatsapp import ClienteWhatsAppDev  # noqa: E402

APP_SECRET = "segredo-de-teste"
BOT_TOKEN = "token-do-bot"
_ids = itertools.count(1)


@pytest.fixture(scope="session")
def crm_url():
    servidor = crm_simulado.iniciar(0)
    yield f"http://127.0.0.1:{servidor.server_address[1]}/api"
    servidor.shutdown()


@pytest.fixture
def crm(crm_url):
    crm_simulado.resetar()
    crm_simulado.forcar_expediente["aberto"] = True
    return crm_simulado.estado


@pytest.fixture
def bot(tmp_path, crm_url, crm):
    config = Config(
        whatsapp_token="", whatsapp_app_secret=APP_SECRET, whatsapp_verify_token="verifica",
        crm_url=crm_url, crm_token=crm_simulado.TOKEN, crm_timeout=5,
        bot_api_token=BOT_TOKEN, db_path=str(tmp_path / "teste.db"),
        fluxo_path=str(RAIZ / "fluxo.yaml"), fila_intervalo_segundos=3600,
    )
    whatsapp = ClienteWhatsAppDev()
    app = criar_app(config, whatsapp=whatsapp)
    with TestClient(app) as client:
        client.whatsapp = whatsapp
        client.motor = app.state.motor
        yield client


def assinar(corpo: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), corpo, hashlib.sha256).hexdigest()


class Paciente:
    """Simula um paciente conversando pelo WhatsApp: monta o webhook da Meta e lê as respostas do bot."""

    def __init__(self, client: TestClient, telefone: str, nome: str = "Contato Teste"):
        self.client, self.telefone, self.nome = client, telefone, nome

    @property
    def ultima(self) -> dict:
        minhas = [m for m in self.client.whatsapp.enviadas if m["telefone"] == self.telefone]
        return minhas[-1]

    def _webhook(self, mensagem: dict) -> list[dict]:
        antes = len(self.client.whatsapp.enviadas)
        payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": self.telefone, "profile": {"name": self.nome}}],
            "messages": [{"from": self.telefone, "id": f"wamid.{next(_ids)}", "timestamp": "0", **mensagem}],
        }}]}]}
        corpo = json.dumps(payload).encode()
        r = self.client.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo),
                                                                 "content-type": "application/json"})
        assert r.status_code == 200, r.text
        return [m for m in self.client.whatsapp.enviadas[antes:] if m["telefone"] == self.telefone]

    def diz(self, texto: str) -> list[dict]:
        return self._webhook({"type": "text", "text": {"body": texto}})

    def toca(self, titulo: str) -> list[dict]:
        """Toca no botão/linha com esse título na última mensagem do bot."""
        opcoes = self.ultima["opcoes"]
        escolhida = next((o for o in opcoes if o.titulo == titulo), None)
        assert escolhida, f"'{titulo}' não está entre {[o.titulo for o in opcoes]}"
        tipo = "button_reply" if self.ultima["payload"]["interactive"]["type"] == "button" else "list_reply"
        return self._webhook({"type": "interactive", "interactive": {
            "type": tipo, tipo: {"id": escolhida.id, "title": escolhida.titulo}}})

    def toca_primeira(self) -> list[dict]:
        return self.toca(self.ultima["opcoes"][0].titulo)

    def envia_audio(self) -> list[dict]:
        return self._webhook({"type": "audio", "audio": {"id": "midia-1"}})


@pytest.fixture
def paciente_novo(bot):
    return Paciente(bot, "5521988887777", "Pedro")


@pytest.fixture
def joana(bot):
    """Paciente já cadastrada no CRM simulado."""
    return Paciente(bot, "5511999990000", "Joana")


def textos(mensagens: list[dict]) -> str:
    return "\n---\n".join(m["texto"] for m in mensagens)
