import os
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _env(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, padrao).strip()


@dataclass
class Config:
    # WhatsApp Cloud API (Meta). Sem token, o bot roda em modo desenvolvimento e só registra as saídas.
    whatsapp_token: str = field(default_factory=lambda: _env("WHATSAPP_TOKEN"))
    whatsapp_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    whatsapp_app_secret: str = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))
    whatsapp_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN", "troque-este-token"))
    whatsapp_api_version: str = field(default_factory=lambda: _env("WHATSAPP_API_VERSION", "v25.0"))

    # Embedded Signup (página "Conectar WhatsApp"). O app secret é o mesmo WHATSAPP_APP_SECRET.
    meta_app_id: str = field(default_factory=lambda: _env("META_APP_ID"))
    meta_config_id: str = field(default_factory=lambda: _env("META_CONFIG_ID"))
    admin_usuario: str = field(default_factory=lambda: _env("ADMIN_USUARIO", "admin"))
    admin_senha: str = field(default_factory=lambda: _env("ADMIN_SENHA"))

    # CRM
    crm_url: str = field(default_factory=lambda: _env("CRM_URL", "http://localhost:8000/api"))
    crm_token: str = field(default_factory=lambda: _env("CRM_TOKEN", "TROQUE-PELO-TOKEN-DA-API"))
    crm_timeout: float = field(default_factory=lambda: float(_env("CRM_TIMEOUT", "10")))

    # API que a tela de atendimento do CRM usa para falar com o bot
    bot_api_token: str = field(default_factory=lambda: _env("BOT_API_TOKEN", "TROQUE-PELO-TOKEN-DO-BOT"))

    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(RAIZ / "bot.db")))
    fluxo_path: str = field(default_factory=lambda: _env("FLUXO_PATH", str(RAIZ / "fluxo.yaml")))
    sessao_expira_horas: float = field(default_factory=lambda: float(_env("SESSAO_EXPIRA_HORAS", "4")))
    # Coexistência: depois que alguém responde pelo celular, o bot fica quieto nessa conversa
    # até a conversa ficar parada por este tempo (ou ser encerrada pela tela de atendimento).
    pausa_celular_horas: float = field(default_factory=lambda: float(_env("PAUSA_CELULAR_HORAS", "12")))
    fila_intervalo_segundos: float = field(default_factory=lambda: float(_env("FILA_INTERVALO_SEGUNDOS", "30")))
