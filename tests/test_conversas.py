"""Conversas completas, do jeito que o paciente usa no WhatsApp."""

from conftest import textos


def test_paciente_novo_agenda_primeira_consulta(paciente_novo, crm):
    r = paciente_novo.diz("Oi, boa tarde")
    assert "LGPD" in r[0]["texto"]
    assert [o.titulo for o in r[0]["opcoes"]] == ["Sim, continuar", "Falar com a equipe"]

    r = paciente_novo.toca("Sim, continuar")
    assert crm["pacientes"]["5521988887777"]["consentimento"] is True
    assert r[-1]["texto"] == "Como posso ajudar você hoje?"
    assert r[-1]["payload"]["interactive"]["type"] == "list"  # 6 opções viram lista

    paciente_novo.toca("Agendar consulta")
    r = paciente_novo.toca("Primeira consulta")
    assert "nome completo" in r[-1]["texto"]

    r = paciente_novo.diz("pedro alves")
    assert "Obrigado, Pedro." in r[-1]["texto"]
    paciente_novo.toca("Para mim")
    paciente_novo.toca("Presencial")
    paciente_novo.toca("Sem preferência")
    r = paciente_novo.toca("Tarde")
    assert "horários disponíveis" in r[-1]["texto"]
    horario = r[-1]["opcoes"][0]

    r = paciente_novo.toca(horario.titulo)
    confirmacao = r[-1]["texto"]
    assert "pedro alves" in confirmacao and horario.titulo in confirmacao and horario.descricao in confirmacao

    r = paciente_novo.toca("Confirmar")
    assert "Sua consulta está agendada" in textos(r)
    assert "Nosso endereço" in textos(r)  # presencial
    assert r[-1]["texto"] == "Posso ajudar com mais alguma coisa?"
    (agendamento,) = crm["agendamentos"].values()
    assert agendamento["dados"]["paciente_nome"] == "pedro alves"
    assert agendamento["dados"]["tipo"] == "Primeira consulta"

    r = paciente_novo.toca("Não, obrigado(a)")
    assert "Foi um prazer ajudar" in r[-1]["texto"]


def test_paciente_cadastrada_e_chamada_pelo_nome_e_cancela(joana, crm):
    # cria uma consulta para ela
    joana.diz("oi")
    joana.toca("Agendar consulta")
    joana.toca("Retorno")  # retorno usa o último profissional dela
    joana.toca("Manhã")
    joana.toca_primeira()
    joana.toca("Confirmar")
    joana.toca("Não, obrigado(a)")
    assert len(crm["agendamentos"]) == 1

    r = joana.diz("olá de novo")
    assert r[0]["texto"].startswith("Olá, Joana!")
    r = joana.toca("Minhas consultas")
    assert len(r[-1]["opcoes"]) == 1
    joana.toca_primeira()
    r = joana.toca("Cancelar")
    assert "Tem certeza" in r[-1]["texto"]
    r = joana.toca("Sim, cancelar")
    assert "Consulta cancelada" in textos(r)
    assert next(iter(crm["agendamentos"].values()))["status"] == "cancelado"


def test_remarcar(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    joana.toca("Retorno")
    joana.toca("Manhã")
    joana.toca_primeira()
    joana.toca("Confirmar")
    (ag,) = crm["agendamentos"].values()
    horario_antigo = ag["horario_id"]

    joana.toca("Sim")  # "Posso ajudar com mais alguma coisa?"
    joana.toca("Minhas consultas")
    joana.toca_primeira()
    joana.toca("Remarcar")
    joana.toca("Tarde")
    joana.toca_primeira()
    r = joana.toca("Confirmar")
    assert "agendada" in textos(r)
    assert len(crm["agendamentos"]) == 1  # remarcou, não criou outra
    assert ag["horario_id"] != horario_antigo and horario_antigo not in crm["ocupados"]


def test_digitar_numero_ou_texto_da_opcao_funciona(paciente_novo):
    paciente_novo.diz("oi")
    r = paciente_novo.diz("1")  # primeira opção: "Sim, continuar"
    assert r[-1]["texto"] == "Como posso ajudar você hoje?"
    r = paciente_novo.diz("valores e CONVÊNIOS")
    assert "Valores e formas de pagamento" in r[0]["texto"]


def test_menu_a_qualquer_momento(joana):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    r = joana.diz("menu")
    assert r[-1]["texto"] == "Claro! Como posso ajudar?"


def test_resposta_invalida_repete_e_depois_transfere(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    r = joana.diz("quero na quinta")
    assert "não consegui entender" in r[0]["texto"]
    assert [o.titulo for o in r[0]["opcoes"]] == ["Primeira consulta", "Retorno"]  # repete as opções

    r = joana.diz("quinta de manhã!!")
    assert "chamar alguém da equipe" in textos(r)
    assert crm["atendimentos"][-1]["motivo"] == "nao_entendeu"


def test_audio_recebe_aviso(joana):
    joana.diz("oi")
    r = joana.envia_audio()
    assert "apenas por texto" in r[0]["texto"]
    assert r[0]["opcoes"]  # e as opções continuam disponíveis


def test_sem_horario_oferece_lista_de_espera(joana, crm):
    import servidor
    original = servidor.gerar_horarios
    servidor.gerar_horarios = lambda *a: []
    try:
        joana.diz("oi")
        joana.toca("Agendar consulta")
        joana.toca("Retorno")
        r = joana.toca("Noite")
        assert "não encontrei horários" in r[-1]["texto"]
        r = joana.toca("Lista de espera")
        assert "lista de espera" in textos(r)
        assert crm["lista_espera"][0]["periodo"] == "Noite"
    finally:
        servidor.gerar_horarios = original


def test_horario_ocupado_no_ultimo_segundo(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    joana.toca("Retorno")
    r = joana.toca("Manhã")
    horario_id = joana.client.motor.banco.conversa_ativa(joana.telefone)["variaveis"]["lista_horarios"][0]["id"]
    joana.toca(r[-1]["opcoes"][0].titulo)
    crm["ocupados"].add(horario_id)  # outra pessoa pegou antes da confirmação
    r = joana.toca("Confirmar")
    assert "acabou de ser preenchido" in r[0]["texto"]
    assert r[-1]["texto"] == "Qual período funciona melhor para você?"


def test_crm_fora_do_ar_durante_agendamento_transfere(joana, crm):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    joana.toca("Retorno")
    crm["fora_do_ar"] = True
    r = joana.toca("Manhã")
    assert "Tive um problema para acessar nossa agenda" in textos(r)
    assert "Nossa equipe atende de" in textos(r)  # sem CRM, não sabe o expediente: assume fechado
    assert joana.client.motor.banco.conversa_ativa(joana.telefone)["estado"] == "aguardando_humano"


def test_sessao_expirada_recomeca(joana):
    joana.diz("oi")
    joana.toca("Agendar consulta")
    c = joana.client.motor.banco.conversa_ativa(joana.telefone)
    joana.client.motor.banco.envelhecer_conversa(c["id"], horas=5)
    r = joana.diz("oi de novo")
    assert r[0]["texto"].startswith("Olá, Joana!")
    assert joana.client.motor.banco.conversa_ativa(joana.telefone)["id"] != c["id"]


def test_mensagem_repetida_pela_meta_e_processada_uma_vez(joana):
    import json
    from conftest import assinar
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": joana.telefone, "id": "wamid.REPETIDA", "type": "text", "text": {"body": "oi"}}]}}]}]}
    corpo = json.dumps(payload).encode()
    for _ in range(3):
        joana.client.post("/webhook", content=corpo, headers={"x-hub-signature-256": assinar(corpo)})
    assert len(joana.client.whatsapp.enviadas) == 1
