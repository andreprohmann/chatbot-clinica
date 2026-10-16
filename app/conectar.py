"""Página "Conectar WhatsApp": liga o número da clínica ao bot pelo Embedded Signup da Meta.

Protegida por usuário e senha (ADMIN_USUARIO / ADMIN_SENHA). Só o administrador deve acessar.
"""

import json
import logging
import secrets
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .config import Config
from .meta import ClienteMeta, ErroMeta
from .motor import Motor
from .whatsapp import ClienteWhatsApp

log = logging.getLogger("bot.conectar")
PAGINA = Path(__file__).with_name("conectar.html")


class FinalizarIn(BaseModel):
    code: str = Field(min_length=1)
    waba_id: str = Field(min_length=1)
    phone_number_id: str = ""
    coexistencia: bool


def criar_cliente_whatsapp(config: Config, conexao: dict, transport: httpx.AsyncBaseTransport | None = None):
    return ClienteWhatsApp(conexao["token"], conexao["phone_number_id"], config.whatsapp_api_version, transport)


def resumo(conexao: dict | None) -> dict:
    if not conexao:
        return {"conectado": False}
    return {
        "conectado": True,
        "numero": conexao["numero"],
        "nome_verificado": conexao["nome_verificado"],
        "waba_id": conexao["waba_id"],
        "phone_number_id": conexao["phone_number_id"],
        "coexistencia": bool(conexao["coexistencia"]),
        "conectado_em": conexao["conectado_em"],
        "ultimo_evento_meta": conexao["ultimo_evento_meta"],
    }


def rotas_conexao(config: Config, motor: Motor, meta: ClienteMeta,
                  whatsapp_transport: httpx.AsyncBaseTransport | None = None) -> APIRouter:
    router = APIRouter(prefix="/conectar-whatsapp")
    basic = HTTPBasic(realm="Conectar WhatsApp")

    def admin(cred: HTTPBasicCredentials = Depends(basic)):
        if not config.admin_senha:
            raise HTTPException(503, "Defina ADMIN_SENHA no .env para usar esta página.")
        usuario_ok = secrets.compare_digest(cred.username.encode(), config.admin_usuario.encode())
        senha_ok = secrets.compare_digest(cred.password.encode(), config.admin_senha.encode())
        if not (usuario_ok and senha_ok):
            raise HTTPException(401, "Usuário ou senha inválidos", headers={"WWW-Authenticate": "Basic"})

    @router.get("", response_class=HTMLResponse, dependencies=[Depends(admin)])
    async def pagina():
        if not (config.meta_app_id and config.meta_config_id):
            raise HTTPException(503, "Defina META_APP_ID e META_CONFIG_ID no .env (veja o checklist, etapa 4).")
        dados = json.dumps({"appId": config.meta_app_id, "configId": config.meta_config_id,
                            "versao": config.whatsapp_api_version})
        return PAGINA.read_text(encoding="utf-8").replace("/*CONFIG*/{}", dados)

    @router.get("/status", dependencies=[Depends(admin)])
    async def status():
        return resumo(motor.banco.conexao())

    @router.post("/finalizar", dependencies=[Depends(admin)])
    async def finalizar(dados: FinalizarIn):
        # O code do Embedded Signup expira em 30 segundos: a troca precisa ser a primeira coisa.
        try:
            token = await meta.trocar_codigo(dados.code)
            numeros = await meta.numeros(dados.waba_id, token)
            numero = next((n for n in numeros if n.get("id") == dados.phone_number_id), None) \
                or (numeros[0] if numeros else None)
            if not numero:
                raise ErroMeta("Nenhum número encontrado nessa conta WhatsApp Business.")
            await meta.assinar_webhooks(dados.waba_id, token)

            pin, sync_contatos, sync_historico = "", "", ""
            if dados.coexistencia:
                # Número já registrado no app do celular: não registrar de novo, só sincronizar (prazo de 24h).
                sync_contatos = await meta.sincronizar(numero["id"], token, "smb_app_state_sync")
                sync_historico = await meta.sincronizar(numero["id"], token, "history")
            else:
                pin = f"{secrets.randbelow(10**6):06d}"
                await meta.registrar_numero(numero["id"], token, pin)
        except ErroMeta as e:
            log.error("Falha ao conectar o WhatsApp: %s", e)
            raise HTTPException(502, str(e))

        motor.banco.salvar_conexao(
            waba_id=dados.waba_id, phone_number_id=numero["id"], numero=numero.get("display_phone_number", ""),
            nome_verificado=numero.get("verified_name", ""), token=token, coexistencia=int(dados.coexistencia),
            pin=pin, sync_contatos_id=sync_contatos, sync_historico_id=sync_historico,
        )
        conexao = motor.banco.conexao()
        motor.whatsapp = criar_cliente_whatsapp(config, conexao, whatsapp_transport)
        log.info("WhatsApp conectado: %s (coexistência: %s)", conexao["numero"], dados.coexistencia)
        return {**resumo(conexao), "pin": pin}

    return router
