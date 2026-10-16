"""Aplicação web: webhook da Meta + API usada pela tela de atendimento do CRM.

Rodar:  uvicorn app.main:criar_app --factory --port 8001
"""

import asyncio
import contextlib
import json
import logging
import secrets

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import fluxo as fluxo_mod
from .acoes import ACOES
from .conectar import criar_cliente_whatsapp, rotas_conexao
from .config import Config
from .crm import ClienteCRM
from .db import Banco
from .meta import ClienteMeta
from .motor import ErroAtendimento, Motor
from .whatsapp import (ClienteWhatsApp, ClienteWhatsAppDev, assinatura_valida, ler_ecos, ler_eventos_conta,
                       ler_webhook)

log = logging.getLogger("bot")


class AssumirIn(BaseModel):
    atendente: str = Field(min_length=1, max_length=100)


class MensagemIn(BaseModel):
    atendente: str = Field(min_length=1, max_length=100)
    texto: str = Field(min_length=1, max_length=4096)


class EncerrarIn(BaseModel):
    atendente: str = Field(min_length=1, max_length=100)
    enviar_despedida: bool = True


def criar_app(config: Config | None = None, whatsapp=None, crm: ClienteCRM | None = None,
              meta_transport: httpx.AsyncBaseTransport | None = None,
              whatsapp_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Os parâmetros opcionais existem para os testes injetarem clientes falsos."""
    config = config or Config()
    fluxo = fluxo_mod.carregar(config.fluxo_path, set(ACOES))
    banco = Banco(config.db_path)
    if whatsapp is None:
        conexao = banco.conexao()
        if config.whatsapp_token:  # credenciais fixas no .env têm prioridade
            whatsapp = ClienteWhatsApp(config.whatsapp_token, config.whatsapp_phone_number_id,
                                       config.whatsapp_api_version, whatsapp_transport)
        elif conexao:  # número conectado pela página Conectar WhatsApp
            whatsapp = criar_cliente_whatsapp(config, conexao, whatsapp_transport)
        else:
            log.warning("Nenhum número conectado: modo desenvolvimento, nada é enviado de verdade.")
            whatsapp = ClienteWhatsAppDev()
    crm = crm or ClienteCRM(config.crm_url, config.crm_token, config.crm_timeout)
    motor = Motor(config, banco, whatsapp, crm, fluxo)
    meta = ClienteMeta(config.meta_app_id, config.whatsapp_app_secret, config.whatsapp_api_version, meta_transport)

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_app: FastAPI):
        async def reenviar_periodicamente():
            while True:
                await asyncio.sleep(config.fila_intervalo_segundos)
                try:
                    await motor.reenviar_fila()
                except Exception:
                    log.exception("Erro ao reenviar fila do CRM")

        tarefa = asyncio.create_task(reenviar_periodicamente())
        yield
        tarefa.cancel()

    app = FastAPI(title="Bot WhatsApp – Clínica", lifespan=ciclo_de_vida)
    app.state.motor = motor

    # ------------------------------------------------------------ webhook da Meta

    @app.get("/webhook", response_class=PlainTextResponse)
    async def verificar_webhook(
        modo: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        desafio: str = Query("", alias="hub.challenge"),
    ):
        if modo == "subscribe" and secrets.compare_digest(token, config.whatsapp_verify_token):
            return desafio
        raise HTTPException(403, "token de verificação inválido")

    @app.post("/webhook")
    async def receber_webhook(request: Request, tarefas: BackgroundTasks):
        corpo = await request.body()
        if not assinatura_valida(corpo, request.headers.get("x-hub-signature-256"), config.whatsapp_app_secret):
            raise HTTPException(403, "assinatura inválida")
        try:
            payload = json.loads(corpo)
        except ValueError:
            raise HTTPException(400, "JSON inválido")
        # Responde 200 na hora; a Meta reenvia se demorarmos. O processamento segue em segundo plano.
        for entrada in ler_webhook(payload):
            tarefas.add_task(motor.processar, entrada)
        for eco in ler_ecos(payload):  # respostas enviadas pelo celular da clínica (Coexistência)
            tarefas.add_task(motor.processar_eco, eco)
        for evento in ler_eventos_conta(payload):  # ex.: número desconectado pelo app do celular
            log.warning("Aviso da Meta sobre a conta: %s", evento)
            motor.banco.registrar_evento_meta(evento)
        return {"ok": True}

    @app.get("/health")
    async def saude():
        return {"ok": True, "whatsapp_conectado": not isinstance(motor.whatsapp, ClienteWhatsAppDev),
                "fila_crm": len(motor.banco.fila_pendente())}

    app.include_router(rotas_conexao(config, motor, meta, whatsapp_transport))

    # ------------------------------------------------------------ API da tela de atendimento

    def autenticar(authorization: str = Header("")):
        if not secrets.compare_digest(authorization, f"Bearer {config.bot_api_token}"):
            raise HTTPException(401, "token inválido")

    api = "/api/atendimento"

    @app.get(f"{api}/conversas", dependencies=[Depends(autenticar)])
    async def listar_conversas(estado: str | None = None, limite: int = Query(50, le=200)):
        return {"conversas": motor.banco.listar_conversas(estado, limite)}

    @app.get(f"{api}/conversas/{{conversa_id}}", dependencies=[Depends(autenticar)])
    async def ver_conversa(conversa_id: str):
        c = motor.banco.conversa(conversa_id)
        if not c:
            raise HTTPException(404, "conversa não encontrada")
        c.pop("variaveis")  # estado interno do fluxo; a tela não precisa
        return {"conversa": c, "mensagens": motor.banco.mensagens(conversa_id)}

    @app.post(f"{api}/conversas/{{conversa_id}}/assumir", dependencies=[Depends(autenticar)])
    async def assumir(conversa_id: str, dados: AssumirIn):
        try:
            c = await motor.assumir(conversa_id, dados.atendente)
        except ErroAtendimento as e:
            raise HTTPException(e.status, str(e))
        c.pop("variaveis")
        return {"conversa": c}

    @app.post(f"{api}/conversas/{{conversa_id}}/mensagens", dependencies=[Depends(autenticar)])
    async def responder(conversa_id: str, dados: MensagemIn):
        try:
            await motor.enviar_como_atendente(conversa_id, dados.atendente, dados.texto)
        except ErroAtendimento as e:
            raise HTTPException(e.status, str(e))
        return {"ok": True}

    @app.post(f"{api}/conversas/{{conversa_id}}/encerrar", dependencies=[Depends(autenticar)])
    async def encerrar(conversa_id: str, dados: EncerrarIn):
        try:
            await motor.encerrar(conversa_id, dados.atendente, dados.enviar_despedida)
        except ErroAtendimento as e:
            raise HTTPException(e.status, str(e))
        return {"ok": True}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
