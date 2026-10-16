"""Página Conectar WhatsApp (Embedded Signup) contra uma Graph API falsa."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import servidor as crm_simulado
from app.config import Config
from app.main import criar_app
from conftest import RAIZ, assinar

ADMIN = ("admin", "senha-forte")


class MetaFalsa:
    """Responde como a Graph API e registra as chamadas, na ordem."""

    def __init__(self):
        self.chamadas: list[tuple[str, str, dict]] = []
        self.falhar_em: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        caminho = request.url.path.removeprefix("/v25.0")
        corpo = json.loads(request.content) if request.content else {}
        self.chamadas.append((request.method, caminho, {**dict(request.url.params), **corpo,
                                                        "_auth": request.headers.get("authorization", "")}))
        if self.falhar_em and self.falhar_em in caminho:
            return httpx.Response(400, json={"error": {"message": "Invalid OAuth code", "code": 100}})
        if caminho == "/oauth/access_token":
            return httpx.Response(200, json={"access_token": "TOKEN-DA-CLINICA", "token_type": "bearer"})
        if caminho == "/WABA-1/phone_numbers":
            return httpx.Response(200, json={"data": [
                {"id": "PHONE-1", "display_phone_number": "+55 11 90000-0000", "verified_name": "Clínica Exemplo"}]})
        if caminho.endswith("/smb_app_data"):
            return httpx.Response(200, json={"request_id": f"req-{corpo['sync_type']}"})
        if caminho.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "wamid.saida"}]})
        return httpx.Response(200, json={"success": True})

    def rotas(self) -> list[str]:
        return [f"{m} {c}" for m, c, _ in self.chamadas]


@pytest.fixture
def meta():
    return MetaFalsa()


@pytest.fixture
def app_conexao(tmp_path, crm_url, crm, meta):
    config = Config(
        whatsapp_token="", whatsapp_app_secret="segredo-de-teste", whatsapp_api_version="v25.0",
        meta_app_id="APP-1", meta_config_id="CONFIG-1", admin_usuario=ADMIN[0], admin_senha=ADMIN[1],
        crm_url=crm_url, crm_token=crm_simulado.TOKEN, db_path=str(tmp_path / "t.db"),
        fluxo_path=str(RAIZ / "fluxo.yaml"), fila_intervalo_segundos=3600,
    )
    transport = httpx.MockTransport(meta)
    app = criar_app(config, meta_transport=transport, whatsapp_transport=transport)
    with TestClient(app) as client:
        client.config = config
        yield client


def test_pagina_exige_login(app_conexao):
    assert app_conexao.get("/conectar-whatsapp").status_code == 401
    assert app_conexao.get("/conectar-whatsapp", auth=("admin", "errada")).status_code == 401
    pagina = app_conexao.get("/conectar-whatsapp", auth=ADMIN)
    assert pagina.status_code == 200
    assert '"appId": "APP-1"' in pagina.text and '"configId": "CONFIG-1"' in pagina.text
    assert "whatsapp_business_app_onboarding" in pagina.text


def test_sem_senha_configurada_a_pagina_fica_desligada(app_conexao):
    app_conexao.config.admin_senha = ""
    assert app_conexao.get("/conectar-whatsapp", auth=ADMIN).status_code == 503


def test_conectar_whatsapp_do_celular(app_conexao, meta):
    assert app_conexao.get("/health").json()["whatsapp_conectado"] is False

    r = app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "CODE-30S", "waba_id": "WABA-1", "phone_number_id": "", "coexistencia": True})
    assert r.status_code == 200, r.text
    dados = r.json()
    assert dados["numero"] == "+55 11 90000-0000" and dados["coexistencia"] is True and dados["pin"] == ""

    assert meta.rotas() == [
        "GET /oauth/access_token",          # 1º: o code expira em 30s
        "GET /WABA-1/phone_numbers",
        "POST /WABA-1/subscribed_apps",
        "POST /PHONE-1/smb_app_data",       # contatos
        "POST /PHONE-1/smb_app_data",       # histórico
    ]  # e nenhum /register: o número já está registrado no app do celular
    troca = meta.chamadas[0][2]
    assert troca["client_id"] == "APP-1" and troca["client_secret"] == "segredo-de-teste" and troca["code"] == "CODE-30S"
    assert [c[2]["sync_type"] for c in meta.chamadas[3:]] == ["smb_app_state_sync", "history"]
    assert all(c[2]["_auth"] == "Bearer TOKEN-DA-CLINICA" for c in meta.chamadas[1:])

    status = app_conexao.get("/conectar-whatsapp/status", auth=ADMIN).json()
    assert status["conectado"] and status["phone_number_id"] == "PHONE-1"
    assert "token" not in status  # o token nunca sai do servidor
    assert app_conexao.get("/health").json()["whatsapp_conectado"] is True


def test_depois_de_conectar_o_bot_responde_pelo_numero_da_clinica(app_conexao, meta):
    app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "C", "waba_id": "WABA-1", "phone_number_id": "PHONE-1", "coexistencia": True})
    meta.chamadas.clear()

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "5521988887777", "id": "wamid.1", "type": "text", "text": {"body": "oi"}}]}}]}]}
    corpo = json.dumps(payload).encode()
    app_conexao.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)})

    metodo, caminho, enviado = meta.chamadas[-1]
    assert (metodo, caminho) == ("POST", "/PHONE-1/messages")
    assert enviado["_auth"] == "Bearer TOKEN-DA-CLINICA"
    assert enviado["to"] == "5521988887777" and "LGPD" in enviado["interactive"]["body"]["text"]


def test_conexao_continua_depois_de_reiniciar(app_conexao, meta, crm_url):
    app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "C", "waba_id": "WABA-1", "phone_number_id": "PHONE-1", "coexistencia": True})
    novo = criar_app(app_conexao.config, meta_transport=httpx.MockTransport(meta),
                     whatsapp_transport=httpx.MockTransport(meta))
    with TestClient(novo) as client:
        assert client.get("/health").json()["whatsapp_conectado"] is True


def test_numero_novo_e_registrado_com_pin(app_conexao, meta):
    r = app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "C", "waba_id": "WABA-1", "phone_number_id": "PHONE-1", "coexistencia": False})
    assert r.status_code == 200
    pin = r.json()["pin"]
    assert len(pin) == 6 and pin.isdigit()
    assert "POST /PHONE-1/register" in meta.rotas() and "POST /PHONE-1/smb_app_data" not in meta.rotas()
    registro = next(c for c in meta.chamadas if c[1] == "/PHONE-1/register")[2]
    assert registro["pin"] == pin and registro["messaging_product"] == "whatsapp"


def test_erro_da_meta_aparece_para_o_admin(app_conexao, meta):
    meta.falhar_em = "/oauth/access_token"
    r = app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "VENCIDO", "waba_id": "WABA-1", "coexistencia": True})
    assert r.status_code == 502 and "Invalid OAuth code" in r.json()["detail"]
    assert app_conexao.get("/conectar-whatsapp/status", auth=ADMIN).json()["conectado"] is False


def test_aviso_de_desconexao_da_meta_fica_registrado(app_conexao):
    app_conexao.post("/conectar-whatsapp/finalizar", auth=ADMIN, json={
        "code": "C", "waba_id": "WABA-1", "phone_number_id": "PHONE-1", "coexistencia": True})
    payload = {"entry": [{"changes": [{"field": "account_update", "value": {
        "event": "PARTNER_REMOVED", "waba_info": {"waba_id": "WABA-1"}}}]}]}
    corpo = json.dumps(payload).encode()
    app_conexao.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)})
    status = app_conexao.get("/conectar-whatsapp/status", auth=ADMIN).json()
    assert status["ultimo_evento_meta"].startswith("PARTNER_REMOVED")
