"""Cliente da API do CRM. Os endpoints estão documentados no README (seção "Contrato com o CRM")."""

import logging

import httpx

log = logging.getLogger("bot.crm")


class CRMIndisponivel(Exception):
    """CRM fora do ar, lento ou respondendo com erro."""

    def __init__(self, mensagem: str, status: int | None = None):
        super().__init__(mensagem)
        self.status = status


class ClienteCRM:
    def __init__(self, url: str, token: str, timeout: float = 10, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    async def _req(self, metodo: str, rota: str, **kwargs) -> dict:
        try:
            r = await self._http.request(metodo, rota, **kwargs)
        except httpx.HTTPError as e:
            raise CRMIndisponivel(f"{metodo} {rota}: {e!r}") from e
        if r.status_code >= 400:
            raise CRMIndisponivel(f"{metodo} {rota}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        try:
            return r.json() if r.content else {}
        except ValueError as e:
            raise CRMIndisponivel(f"{metodo} {rota}: resposta não é JSON") from e

    async def get(self, rota: str, **params) -> dict:
        return await self._req("GET", rota, params={k: v for k, v in params.items() if v is not None})

    async def post(self, rota: str, payload: dict) -> dict:
        return await self._req("POST", rota, json=payload)

    # Atalhos com nomes do domínio, para o código das ações ficar legível.

    async def paciente(self, telefone: str) -> dict:
        return await self.get("/pacientes", telefone=telefone)

    async def expediente_aberto(self) -> bool:
        try:
            return bool((await self.get("/expediente")).get("aberto"))
        except CRMIndisponivel:
            log.warning("Não foi possível consultar o expediente; assumindo clínica fechada.")
            return False

    async def profissionais(self, modalidade: str) -> dict:
        return await self.get("/profissionais", modalidade=modalidade)

    async def horarios(self, periodo: str, modalidade: str, profissional_id: str) -> dict:
        return await self.get("/agenda/horarios", periodo=periodo, modalidade=modalidade,
                              profissional_id=profissional_id)

    async def agendamentos(self, telefone: str) -> dict:
        return await self.get("/agendamentos", telefone=telefone)
