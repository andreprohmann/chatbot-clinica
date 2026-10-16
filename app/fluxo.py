"""Carrega o fluxo.yaml e valida a estrutura antes de o bot começar a atender."""

from dataclasses import dataclass
from pathlib import Path

import yaml

from .whatsapp import LIMITE_BOTAO, LIMITE_LINHA, MAX_BOTOES, MAX_LINHAS

TIPOS = {"mensagem", "opcoes", "opcoes_dinamicas", "texto_livre", "acao", "condicao", "definir", "transferir"}
TIPOS_QUE_ESPERAM = {"opcoes", "opcoes_dinamicas", "texto_livre"}
MENSAGENS_OBRIGATORIAS = {
    "nao_entendi", "nao_entendi_transferir", "texto_curto", "midia", "equipe_dentro_horario",
    "equipe_fora_horario", "crise", "crise_fora_horario", "atendimento_encerrado",
}


class FluxoInvalido(Exception):
    pass


@dataclass
class Fluxo:
    inicio: str
    menu: str
    passos: dict[str, dict]
    mensagens: dict[str, str]
    padroes_crise: list[str]


def carregar(caminho: str | Path, acoes_disponiveis: set[str]) -> Fluxo:
    dados = yaml.safe_load(Path(caminho).read_text(encoding="utf-8"))
    fluxo = Fluxo(
        inicio=dados.get("inicio", ""),
        menu=dados.get("menu", ""),
        passos=dados.get("passos") or {},
        mensagens=dados.get("mensagens") or {},
        padroes_crise=dados.get("crise") or [],
    )
    erros = validar(fluxo, acoes_disponiveis)
    if erros:
        raise FluxoInvalido("fluxo.yaml inválido:\n- " + "\n- ".join(erros))
    return fluxo


def validar(fluxo: Fluxo, acoes_disponiveis: set[str]) -> list[str]:
    erros = []
    nomes = set(fluxo.passos)

    def destino(origem: str, alvo: str | None, obrigatorio: bool = True):
        if alvo is None:
            if obrigatorio:
                erros.append(f"{origem}: destino não definido")
        elif alvo not in nomes:
            erros.append(f"{origem}: destino '{alvo}' não existe")

    destino("inicio", fluxo.inicio)
    destino("menu", fluxo.menu)
    for falta in sorted(MENSAGENS_OBRIGATORIAS - set(fluxo.mensagens)):
        erros.append(f"mensagens: falta '{falta}'")
    if not fluxo.padroes_crise:
        erros.append("crise: lista de expressões de risco vazia")

    for nome, p in fluxo.passos.items():
        tipo = p.get("tipo")
        if tipo not in TIPOS:
            erros.append(f"{nome}: tipo '{tipo}' desconhecido")
            continue
        if tipo in {"mensagem", "opcoes", "opcoes_dinamicas", "texto_livre"} and not p.get("texto"):
            erros.append(f"{nome}: falta 'texto'")
        if tipo == "mensagem":
            destino(nome, p.get("proximo"), obrigatorio=False)
        elif tipo in {"definir", "texto_livre", "opcoes_dinamicas"}:
            destino(nome, p.get("proximo"))
        elif tipo == "acao":
            if p.get("acao") not in acoes_disponiveis:
                erros.append(f"{nome}: ação '{p.get('acao')}' não existe em app/acoes.py")
            for saida, alvo in (p.get("saidas") or {}).items():
                destino(f"{nome}.saidas.{saida}", alvo)
            if "proximo" in p or not p.get("saidas"):
                destino(nome, p.get("proximo"))
        elif tipo == "condicao":
            if not p.get("variavel"):
                erros.append(f"{nome}: falta 'variavel'")
            destino(f"{nome}.entao", p.get("entao"))
            destino(f"{nome}.senao", p.get("senao"))
        elif tipo == "transferir" and not p.get("motivo"):
            erros.append(f"{nome}: falta 'motivo'")

        if tipo in {"texto_livre", "opcoes_dinamicas"} and not p.get("salvar_em"):
            erros.append(f"{nome}: falta 'salvar_em'")
        if tipo == "opcoes_dinamicas" and not p.get("fonte"):
            erros.append(f"{nome}: falta 'fonte'")
        if tipo == "opcoes":
            opcoes = p.get("opcoes") or []
            if not opcoes:
                erros.append(f"{nome}: nenhuma opção")
            if len(opcoes) > MAX_LINHAS:
                erros.append(f"{nome}: {len(opcoes)} opções; o WhatsApp mostra no máximo {MAX_LINHAS}")
            vira_botao = len(opcoes) <= MAX_BOTOES and not any(o.get("descricao") for o in opcoes)
            limite = LIMITE_BOTAO if vira_botao else LIMITE_LINHA
            for i, o in enumerate(opcoes):
                rotulo = o.get("rotulo", "")
                if not rotulo:
                    erros.append(f"{nome}.opcoes[{i}]: falta 'rotulo'")
                elif len(rotulo) > limite:
                    erros.append(f"{nome}.opcoes[{i}]: '{rotulo}' tem {len(rotulo)} caracteres (máx. {limite})")
                destino(f"{nome}.opcoes[{i}]", o.get("proximo") or p.get("proximo"))
    return erros
