"""Chamadas à Graph API usadas para conectar o número da clínica pelo Embedded Signup.

Fluxo (documentação da Meta para Tech Providers):
  1. trocar o `code` do Embedded Signup por um token (o code vale só 30 segundos)
  2. inscrever o app nos webhooks da conta WhatsApp Business (WABA)
  3. número novo: registrar com um PIN de 6 dígitos | coexistência: NÃO registrar (já está no app)
  4. coexistência: pedir a sincronização de contatos e histórico (prazo de 24h)
"""

import httpx


class ErroMeta(Exception):
    pass


class ClienteMeta:
    def __init__(self, app_id: str, app_secret: str, versao: str, transport: httpx.AsyncBaseTransport | None = None):
        self._app_id, self._app_secret = app_id, app_secret
        self._http = httpx.AsyncClient(base_url=f"https://graph.facebook.com/{versao}", timeout=20,
                                       transport=transport)

    async def _req(self, metodo: str, rota: str, token: str | None = None, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = await self._http.request(metodo, rota, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise ErroMeta(f"Falha de rede ao chamar a Meta ({rota}): {e!r}") from e
        dados = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in dados:
            erro = dados.get("error", {})
            raise ErroMeta(f"Meta recusou {metodo} {rota}: {erro.get('message') or r.text[:300]}")
        return dados

    async def trocar_codigo(self, code: str) -> str:
        dados = await self._req("GET", "/oauth/access_token", params={
            "client_id": self._app_id, "client_secret": self._app_secret, "code": code})
        if not dados.get("access_token"):
            raise ErroMeta("A Meta não devolveu o token de acesso.")
        return dados["access_token"]

    async def numeros(self, waba_id: str, token: str) -> list[dict]:
        dados = await self._req("GET", f"/{waba_id}/phone_numbers", token,
                                params={"fields": "id,display_phone_number,verified_name"})
        return dados.get("data", [])

    async def assinar_webhooks(self, waba_id: str, token: str) -> None:
        await self._req("POST", f"/{waba_id}/subscribed_apps", token)

    async def registrar_numero(self, phone_number_id: str, token: str, pin: str) -> None:
        await self._req("POST", f"/{phone_number_id}/register", token,
                        json={"messaging_product": "whatsapp", "pin": pin})

    async def sincronizar(self, phone_number_id: str, token: str, tipo: str) -> str:
        """tipo: 'smb_app_state_sync' (contatos) ou 'history' (conversas)."""
        dados = await self._req("POST", f"/{phone_number_id}/smb_app_data", token,
                                json={"messaging_product": "whatsapp", "sync_type": tipo})
        return str(dados.get("request_id", ""))
