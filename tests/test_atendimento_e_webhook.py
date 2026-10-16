"""API da tela de atendimento do CRM e segurança do webhook."""

from app.db import agora, iso

AUTH = {"Authorization": "Bearer token-do-bot"}


def _transferir(paciente):
    paciente.diz("oi")
    paciente.toca("Falar com a equipe")
    return paciente.client.motor.banco.conversa_ativa(paciente.telefone)


def test_fluxo_completo_de_atendimento_humano(joana, crm):
    bot = joana.client
    c = _transferir(joana)
    assert crm["atendimentos"][-1] == {"conversa_id": c["id"], "telefone": joana.telefone,
                                       "nome": "Joana Martins", "motivo": "solicitado_pelo_paciente"}

    fila = bot.get("/api/atendimento/conversas?estado=aguardando_humano", headers=AUTH).json()["conversas"]
    assert [x["id"] for x in fila] == [c["id"]]

    # responder sem assumir não pode
    r = bot.post(f"/api/atendimento/conversas/{c['id']}/mensagens", json={"atendente": "Rita", "texto": "Oi"},
                 headers=AUTH)
    assert r.status_code == 409

    assert bot.post(f"/api/atendimento/conversas/{c['id']}/assumir", json={"atendente": "Rita"},
                    headers=AUTH).status_code == 200
    # outra atendente não pode tomar a conversa
    r = bot.post(f"/api/atendimento/conversas/{c['id']}/assumir", json={"atendente": "Carlos"}, headers=AUTH)
    assert r.status_code == 409 and "Rita" in r.json()["detail"]

    r = bot.post(f"/api/atendimento/conversas/{c['id']}/mensagens",
                 json={"atendente": "Rita", "texto": "Olá, Joana! Aqui é a Rita, da recepção."}, headers=AUTH)
    assert r.status_code == 200
    assert joana.ultima["texto"] == "Olá, Joana! Aqui é a Rita, da recepção."

    # paciente responde: bot fica quieto e avisa a tela
    assert joana.diz("Oi Rita, preciso de um recibo") == []
    assert crm["eventos"][-1]["texto"] == "Oi Rita, preciso de um recibo"

    historico = bot.get(f"/api/atendimento/conversas/{c['id']}", headers=AUTH).json()["mensagens"]
    autores = [m["autor"] for m in historico]
    assert "atendente" in autores and autores[-1] == "paciente"

    assert bot.post(f"/api/atendimento/conversas/{c['id']}/encerrar", json={"atendente": "Rita"},
                    headers=AUTH).status_code == 200
    assert "Atendimento encerrado" in joana.ultima["texto"]

    # próxima mensagem volta para o bot, em uma conversa nova
    r = joana.diz("oi")
    assert r[0]["texto"].startswith("Olá, Joana!")
    assert joana.client.motor.banco.conversa_ativa(joana.telefone)["id"] != c["id"]


def test_janela_de_24h_bloqueia_mensagem_livre(joana):
    bot = joana.client
    c = _transferir(joana)
    bot.post(f"/api/atendimento/conversas/{c['id']}/assumir", json={"atendente": "Rita"}, headers=AUTH)
    banco = bot.motor.banco
    conversa = banco.conversa(c["id"])
    conversa["ultima_entrada_em"] = iso(agora().replace(year=agora().year - 1))
    banco.salvar_conversa(conversa)
    r = bot.post(f"/api/atendimento/conversas/{c['id']}/mensagens", json={"atendente": "Rita", "texto": "Oi"},
                 headers=AUTH)
    assert r.status_code == 409 and "24h" in r.json()["detail"]


def test_api_exige_token(bot):
    assert bot.get("/api/atendimento/conversas").status_code == 401
    assert bot.get("/api/atendimento/conversas", headers={"Authorization": "Bearer errado"}).status_code == 401


def test_conversa_inexistente(bot):
    r = bot.post("/api/atendimento/conversas/nao-existe/assumir", json={"atendente": "Rita"}, headers=AUTH)
    assert r.status_code == 404


def test_webhook_rejeita_assinatura_invalida(bot):
    r = bot.post("/webhook", content=b'{"entry": []}', headers={"x-hub-signature-256": "sha256=falsa"})
    assert r.status_code == 403
    assert bot.post("/webhook", content=b'{"entry": []}').status_code == 403


def test_verificacao_do_webhook_pela_meta(bot):
    ok = bot.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verifica", "hub.challenge": "123"})
    assert ok.status_code == 200 and ok.text == "123"
    errado = bot.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "123"})
    assert errado.status_code == 403


def test_status_de_entrega_sao_ignorados(bot):
    import json
    from conftest import assinar
    corpo = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}).encode()
    assert bot.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)}).status_code == 200
    assert bot.whatsapp.enviadas == []
