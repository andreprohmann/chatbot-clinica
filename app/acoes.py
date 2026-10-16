"""Ações do fluxo: tudo que precisa consultar ou gravar no CRM.

Cada ação recebe o contexto da conversa e devolve uma "saída" (ex.: "ok", "erro", "vazio").
O fluxo.yaml decide para qual passo cada saída leva.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .crm import ClienteCRM, CRMIndisponivel


@dataclass
class Contexto:
    telefone: str
    conversa_id: str
    variaveis: dict
    crm: ClienteCRM
    mensagens: dict[str, str]
    enviar_crm: Callable[[str, dict], Awaitable[None]]  # envia ao CRM ou guarda na fila se falhar

    def v(self, nome: str) -> str:
        valor = self.variaveis.get(nome)
        return "" if valor is None else str(valor)


Acao = Callable[[Contexto], Awaitable[str]]
ACOES: dict[str, Acao] = {}


def acao(funcao: Acao) -> Acao:
    ACOES[funcao.__name__] = funcao
    return funcao


def _lista(ids: list, rotulos: list, descricoes: list | None = None) -> list[dict]:
    descricoes = descricoes or [""] * len(ids)
    return [{"id": str(i), "rotulo": str(r), "descricao": str(d or "")} for i, r, d in zip(ids, rotulos, descricoes)]


@acao
async def identificar_paciente(ctx: Contexto) -> str:
    try:
        p = await ctx.crm.paciente(ctx.telefone)
    except CRMIndisponivel:
        return "novo"  # sem CRM, tratamos como primeiro contato: o consentimento entra na fila
    primeiro_nome = p.get("primeiro_nome") or (p.get("nome") or "").split(" ")[0]
    ctx.variaveis.update(
        paciente_id=p.get("id") or "",
        paciente_nome=p.get("nome") or "",
        primeiro_nome=primeiro_nome,
        ultimo_profissional_id=p.get("ultimo_profissional_id") or "",
    )
    if not p.get("consentimento"):
        return "novo"
    if p.get("encontrado") and primeiro_nome:
        ctx.variaveis["saudacao"] = f"Olá, {primeiro_nome}! Que bom falar com você de novo. Como posso ajudar?"
        return "cadastrado"
    ctx.variaveis["saudacao"] = "Olá! Que bom falar com você de novo. Como posso ajudar?"
    return "recorrente"


@acao
async def registrar_consentimento(ctx: Contexto) -> str:
    await ctx.enviar_crm("/consentimentos", {"telefone": ctx.telefone, "aceito": True})
    return "ok"


@acao
async def extrair_primeiro_nome(ctx: Contexto) -> str:
    nome = ctx.v("nome_completo").strip()
    ctx.variaveis["primeiro_nome"] = nome.split(" ")[0].capitalize() if nome else ""
    return "ok"


@acao
async def verificar_cadastro(ctx: Contexto) -> str:
    return "ok" if ctx.v("paciente_id") else "sem_cadastro"


@acao
async def buscar_profissionais(ctx: Contexto) -> str:
    try:
        r = await ctx.crm.profissionais(ctx.v("modalidade"))
    except CRMIndisponivel:
        return "erro"
    ctx.variaveis["lista_profissionais"] = _lista(r.get("ids", []), r.get("nomes", []))
    return "ok" if ctx.variaveis["lista_profissionais"] else "vazio"


@acao
async def buscar_horarios(ctx: Contexto) -> str:
    try:
        r = await ctx.crm.horarios(ctx.v("periodo"), ctx.v("modalidade"), ctx.v("profissional"))
    except CRMIndisponivel:
        return "erro"
    ctx.variaveis["lista_horarios"] = _lista(r.get("ids", []), r.get("labels", []), r.get("profissionais"))
    return "ok" if ctx.variaveis["lista_horarios"] else "vazio"


@acao
async def gravar_agendamento(ctx: Contexto) -> str:
    try:
        if ctx.v("remarcando") == "sim":
            await ctx.crm.post(f"/agendamentos/{ctx.v('consulta')}/remarcar", {"horario_id": ctx.v("horario")})
        else:
            r = await ctx.crm.post("/agendamentos", {
                "telefone": ctx.telefone,
                "paciente_id": ctx.v("paciente_id"),
                "paciente_nome": ctx.v("paciente_nome"),
                "responsavel_nome": ctx.v("nome_completo"),
                "horario_id": ctx.v("horario"),
                "modalidade": ctx.v("modalidade"),
                "tipo": ctx.v("tipo_consulta"),
            })
            ctx.variaveis["agendamento_id"] = r.get("id", "")
    except CRMIndisponivel as e:
        return "ocupado" if e.status == 409 else "erro"
    return "ok"


@acao
async def registrar_lista_espera(ctx: Contexto) -> str:
    try:
        await ctx.crm.post("/lista-espera", {
            "telefone": ctx.telefone,
            "paciente_id": ctx.v("paciente_id"),
            "paciente_nome": ctx.v("paciente_nome"),
            "periodo": ctx.v("periodo"),
            "modalidade": ctx.v("modalidade"),
            "profissional_id": ctx.v("profissional"),
        })
    except CRMIndisponivel:
        return "erro"
    return "ok"


@acao
async def buscar_consultas(ctx: Contexto) -> str:
    try:
        r = await ctx.crm.agendamentos(ctx.telefone)
    except CRMIndisponivel:
        return "erro"
    ctx.variaveis["lista_consultas"] = _lista(r.get("ids", []), r.get("labels", []), r.get("descricoes"))
    return "ok" if ctx.variaveis["lista_consultas"] else "vazio"


@acao
async def cancelar_consulta(ctx: Contexto) -> str:
    try:
        await ctx.crm.post(f"/agendamentos/{ctx.v('consulta')}/cancelar",
                           {"motivo": "cancelado pelo paciente via WhatsApp"})
    except CRMIndisponivel:
        return "erro"
    return "ok"
