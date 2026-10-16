"""Validação do fluxo.yaml e do formato das mensagens enviadas à Meta."""

import pytest

from app.acoes import ACOES
from app.fluxo import Fluxo, FluxoInvalido, carregar, validar
from app.whatsapp import Opcao, ler_webhook, montar_mensagem
from conftest import RAIZ


def test_fluxo_yaml_e_valido():
    fluxo = carregar(RAIZ / "fluxo.yaml", set(ACOES))
    assert fluxo.inicio in fluxo.passos


def test_todo_passo_e_alcancavel():
    fluxo = carregar(RAIZ / "fluxo.yaml", set(ACOES))
    alcancados, pilha = set(), [fluxo.inicio, fluxo.menu]
    while pilha:
        nome = pilha.pop()
        if nome in alcancados:
            continue
        alcancados.add(nome)
        p = fluxo.passos[nome]
        pilha += [x for x in [p.get("proximo"), p.get("entao"), p.get("senao")] if x]
        pilha += list((p.get("saidas") or {}).values())
        pilha += [o["proximo"] for o in p.get("opcoes") or [] if o.get("proximo")]
    assert set(fluxo.passos) - alcancados == set()


def test_validacao_aponta_erros():
    fluxo = Fluxo(inicio="a", menu="a", mensagens={}, padroes_crise=[], passos={
        "a": {"tipo": "opcoes", "texto": "x", "opcoes": [{"rotulo": "Um rótulo grande demais pra botão", "proximo": "b"}]},
        "c": {"tipo": "acao", "acao": "nao_existe", "proximo": "a"},
    })
    erros = "\n".join(validar(fluxo, set(ACOES)))
    assert "destino 'b' não existe" in erros
    assert "máx. 20" in erros
    assert "ação 'nao_existe'" in erros
    assert "lista de expressões de risco vazia" in erros
    assert "mensagens: falta 'crise'" in erros


def test_fluxo_invalido_impede_inicio(tmp_path):
    arquivo = tmp_path / "fluxo.yaml"
    arquivo.write_text("inicio: x\nmenu: x\npassos: {}\n")
    with pytest.raises(FluxoInvalido):
        carregar(arquivo, set(ACOES))


def test_ate_3_opcoes_curtas_viram_botoes():
    m = montar_mensagem("55", "Pergunta?", [Opcao("a:0", "Sim"), Opcao("a:1", "Não")])
    assert m["interactive"]["type"] == "button"
    assert m["interactive"]["action"]["buttons"][1] == {"type": "reply", "reply": {"id": "a:1", "title": "Não"}}


def test_mais_de_3_opcoes_viram_lista_com_limites():
    opcoes = [Opcao(f"a:{i}", f"Opção com título bem comprido número {i}", "descrição") for i in range(12)]
    m = montar_mensagem("55", "Escolha", opcoes)
    linhas = m["interactive"]["action"]["sections"][0]["rows"]
    assert m["interactive"]["type"] == "list" and len(linhas) == 10
    assert all(len(l["title"]) <= 24 for l in linhas)


def test_texto_simples():
    assert montar_mensagem("55", "Oi")["text"] == {"body": "Oi", "preview_url": False}


def test_le_resposta_de_lista_e_template():
    payload = {"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "55", "profile": {"name": "Ana"}}], "messages": [
        {"from": "55", "id": "1", "type": "interactive",
         "interactive": {"type": "list_reply", "list_reply": {"id": "menu:2", "title": "Valores"}}},
        {"from": "55", "id": "2", "type": "button", "button": {"text": "Confirmar", "payload": "confirmar"}},
        {"from": "55", "id": "3", "type": "image", "image": {}},
    ]}}]}]}
    a, b, c = ler_webhook(payload)
    assert (a.tipo, a.opcao_id, a.nome_contato) == ("opcao", "menu:2", "Ana")
    assert (b.tipo, b.texto) == ("opcao", "Confirmar")
    assert c.tipo == "midia"
