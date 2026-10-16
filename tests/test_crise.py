"""Protocolo de crise: precisa funcionar em qualquer ponto da conversa, inclusive com o CRM fora do ar."""

import pytest
from conftest import textos

from app.crise import DetectorCrise
from app.fluxo import carregar
from app.acoes import ACOES
from conftest import RAIZ


@pytest.fixture(scope="module")
def detector():
    return DetectorCrise(carregar(RAIZ / "fluxo.yaml", set(ACOES)).padroes_crise)


@pytest.mark.parametrize("texto", [
    "às vezes penso em SUICÍDIO", "nao aguento mais", "Não quero mais viver",
    "tenho vontade de morrer", "penso em tirar minha própria vida", "eu me corto quando fico mal... quero me cortar",
])
def test_detecta_expressoes_de_risco(detector, texto):
    assert detector.detectar(texto)


@pytest.mark.parametrize("texto", ["quero remarcar", "Agendar consulta", "Tarde", "minha mãe morreu ano passado"])
def test_nao_dispara_em_mensagens_comuns(detector, texto):
    assert not detector.detectar(texto)


def test_crise_no_meio_do_agendamento(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    joana.toca("Retorno")
    r = joana.diz("na verdade eu não aguento mais")
    assert "CVV – 188" in r[0]["texto"]
    assert crm["alertas"][0]["mensagem"] == "na verdade eu não aguento mais"
    assert crm["alertas"][0]["conversa_id"]
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    assert c["estado"] == "aguardando_humano" and c["motivo_atendimento"] == "crise"

    # o bot não volta a perguntar coisas do fluxo; a equipe assume
    r = joana.diz("oi?")
    assert r == []
    assert crm["eventos"][-1]["texto"] == "oi?"


def test_crise_nao_some_da_fila_com_o_tempo(joana, crm):
    joana.diz("quero morrer")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    joana.client.motor.banco.envelhecer_conversa(c["id"], horas=30)
    assert joana.diz("bom dia") == []  # bot não recomeça o fluxo por cima da crise
    atual = joana.client.motor.banco.conversa_ativa(joana.telefone)
    assert atual["id"] == c["id"] and atual["motivo_atendimento"] == "crise"


def test_pedido_comum_de_atendimento_expira_em_24h(joana):
    joana.diz("oi")
    joana.toca("Falar com a equipe")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    joana.client.motor.banco.envelhecer_conversa(c["id"], horas=25)
    r = joana.diz("oi")
    assert r[0]["texto"].startswith("Olá, Joana!")


def test_crise_fora_do_expediente_reforca_cvv(joana, crm):
    import servidor
    servidor.forcar_expediente["aberto"] = False
    r = joana.diz("quero morrer")
    assert "Eles estão disponíveis agora" in textos(r)


def test_crise_com_crm_fora_do_ar_vai_para_fila_e_e_reenviada(joana, crm):
    crm["fora_do_ar"] = True
    r = joana.diz("não quero mais viver")
    assert "CVV – 188" in r[0]["texto"]  # o paciente é atendido mesmo sem CRM
    assert crm["alertas"] == []
    motor = joana.client.motor
    assert [i["rota"] for i in motor.banco.fila_pendente()] == ["/alertas-crise"]

    crm["fora_do_ar"] = False
    assert joana.client.portal.call(motor.reenviar_fila) == 1
    assert crm["alertas"][0]["mensagem"] == "não quero mais viver"
    assert motor.banco.fila_pendente() == []


def test_crise_com_atendente_na_conversa_so_alerta(joana, crm):
    joana.diz("Falar com a equipe")  # sem passo ativo, vai pro início
    joana.toca("Falar com a equipe")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    joana.client.post(f"/api/atendimento/conversas/{c['id']}/assumir", json={"atendente": "Rita"},
                      headers={"Authorization": "Bearer token-do-bot"})
    r = joana.diz("estou pensando em me matar")
    assert r == []  # quem responde é a atendente, mas o alerta sai
    assert crm["alertas"][-1]["mensagem"] == "estou pensando em me matar"
