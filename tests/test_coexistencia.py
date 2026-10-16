"""Coexistência: a equipe continua usando o app WhatsApp Business no celular, no mesmo número do bot."""

import itertools
import json

from conftest import assinar

AUTH = {"Authorization": "Bearer token-do-bot"}
_ids = itertools.count(1)


def resposta_pelo_celular(client, telefone: str, texto: str):
    """Simula o webhook smb_message_echoes que a Meta envia quando alguém responde pelo app."""
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{
        "field": "smb_message_echoes",
        "value": {"messaging_product": "whatsapp", "message_echoes": [{
            "from": "5511900000000", "to": telefone, "id": f"wamid.eco{next(_ids)}",
            "timestamp": "0", "type": "text", "text": {"body": texto}}]},
    }]}]}
    corpo = json.dumps(payload).encode()
    r = client.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)})
    assert r.status_code == 200


def test_resposta_pelo_celular_tira_o_bot_da_conversa(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    resposta_pelo_celular(joana.client, joana.telefone, "Oi Joana! Aqui é a Rita, pode deixar que eu agendo.")

    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    assert c["estado"] == "humano" and c["atendente"] == "Celular da clínica"
    assert joana.diz("Obrigada! Pode ser quinta?") == []  # o bot não se intromete
    historico = joana.client.motor.banco.mensagens(c["id"])
    assert any(m["autor"] == "atendente" and "Rita" in m["conteudo"] for m in historico)


def test_conversa_iniciada_pelo_celular(paciente_novo):
    # a recepção manda a primeira mensagem pelo app, antes de o paciente falar com o bot
    resposta_pelo_celular(paciente_novo.client, paciente_novo.telefone, "Olá Pedro, tudo bem? Sua guia chegou.")
    assert paciente_novo.diz("Que ótimo, obrigado!") == []


def test_crise_e_alertada_mesmo_com_atendimento_pelo_celular(joana, crm):
    joana.diz("oi")
    resposta_pelo_celular(joana.client, joana.telefone, "Oi Joana, tudo bem?")
    assert joana.diz("não, eu não aguento mais") == []
    assert crm["alertas"][-1]["mensagem"] == "não, eu não aguento mais"


def test_bot_volta_depois_da_pausa(joana):
    resposta_pelo_celular(joana.client, joana.telefone, "Oi Joana!")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    joana.client.motor.banco.envelhecer_conversa(c["id"], horas=13)  # pausa padrão: 12h
    r = joana.diz("oi, quero agendar")
    assert r[0]["texto"].startswith("Olá, Joana!")


def test_tela_do_crm_pode_assumir_conversa_do_celular(joana):
    resposta_pelo_celular(joana.client, joana.telefone, "Oi Joana!")
    joana.diz("oi")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    r = joana.client.post(f"/api/atendimento/conversas/{c['id']}/assumir", json={"atendente": "Rita"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["conversa"]["atendente"] == "Rita"


def test_eco_repetido_e_ignorado(joana):
    payload = {"entry": [{"changes": [{"field": "smb_message_echoes", "value": {"message_echoes": [
        {"to": joana.telefone, "id": "wamid.ECO-REPETIDO", "type": "text", "text": {"body": "Oi"}}]}}]}]}
    corpo = json.dumps(payload).encode()
    for _ in range(2):
        joana.client.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)})
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    assert [m["conteudo"] for m in joana.client.motor.banco.mensagens(c["id"])] == ["Oi"]
