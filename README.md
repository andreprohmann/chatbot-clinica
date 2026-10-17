# Bot WhatsApp: clínica de psicologia

Assistente virtual da clínica no WhatsApp (API oficial da Meta). Faz agendamento, remarcação,
cancelamento, lista de espera e informações. Aplica o **protocolo de crise** em qualquer ponto
da conversa e passa o atendimento para a equipe, que responde pela **tela de atendimento do CRM**.

```
WhatsApp (Meta) ──webhook──► bot-whatsapp (este projeto) ──API──► CRM (pacientes, agenda, alertas)
                                    ▲                                │
                                    └──── API de atendimento ◄───────┘  (tela da recepção no CRM)
```

## Rodar localmente

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# terminal 1: CRM simulado (com tela de atendimento em http://localhost:8000/atendimento)
python3 -u mock-crm/servidor.py

# terminal 2: o bot
.venv/bin/uvicorn app.main:criar_app --factory --port 8001

# terminal 3: conversar com o bot pelo terminal, sem WhatsApp
.venv/bin/python -m app.simulador                 # como a paciente "Joana" (já cadastrada)
.venv/bin/python -m app.simulador 5521988887777   # como paciente novo
```

Sem `WHATSAPP_TOKEN`, o bot roda em **modo desenvolvimento**: nada é enviado à Meta.

Testes: `.venv/bin/pytest`. Os testes simulam conversas completas pelo webhook (com assinatura
da Meta) contra o CRM simulado: paciente novo, agendamento, remarcação, cancelamento, crise no meio
do fluxo, CRM fora do ar, atendimento humano, coexistência com o celular, janela de 24h e
mensagens duplicadas.

## Estrutura

```
fluxo.yaml           textos e passos da conversa (a equipe edita aqui)
app/main.py          FastAPI: webhook da Meta + API de atendimento
app/motor.py         motor da conversa, crise, transferência para a equipe
app/acoes.py         chamadas ao CRM (buscar horários, agendar...)
app/crise.py         detecção de expressões de risco
app/whatsapp.py      leitura do webhook, assinatura, botões e listas
app/crm.py           cliente HTTP do CRM
app/db.py            SQLite: conversas, histórico, deduplicação, fila de reenvio
app/conectar.py      página "Conectar WhatsApp" (Embedded Signup) + chamadas à Meta (app/meta.py)
app/simulador.py     conversa pelo terminal
mock-crm/            CRM simulado + tela de atendimento de referência
```

## Editar o fluxo

Textos, opções e expressões de risco ficam em `fluxo.yaml`. O cabeçalho do arquivo explica cada
tipo de passo. O bot **se recusa a iniciar** se o fluxo tiver destino inexistente, botão com mais
de 20 caracteres ou ação desconhecida, e mostra a lista de problemas. Depois de mexer na estrutura,
rode `pytest`.

Até 3 opções curtas viram **botões**. Mais que isso, ou opções com descrição, viram **lista** (até 10).

## Contrato com o CRM

### O bot chama o CRM (`CRM_URL`, com `Authorization: Bearer CRM_TOKEN`)

| Método | Rota | Envio → resposta |
| --- | --- | --- |
| GET | `/pacientes?telefone=` | → `{encontrado, consentimento, id, nome, primeiro_nome, ultimo_profissional_id}` |
| POST | `/consentimentos` | `{telefone, aceito}` |
| GET | `/profissionais?modalidade=` | → `{ids[], nomes[]}` |
| GET | `/agenda/horarios?periodo=&modalidade=&profissional_id=` | → `{total, ids[], labels[], profissionais[]}` (até 10) |
| POST | `/agendamentos` | `{telefone, paciente_id, paciente_nome, responsavel_nome, horario_id, modalidade, tipo}` → `{id}`; **409** se o horário foi ocupado |
| GET | `/agendamentos?telefone=` | → `{total, ids[], labels[], descricoes[]}` (consultas futuras) |
| POST | `/agendamentos/{id}/remarcar` | `{horario_id}`; 409 se ocupado |
| POST | `/agendamentos/{id}/cancelar` | `{motivo}` |
| POST | `/lista-espera` | `{telefone, paciente_id, paciente_nome, periodo, modalidade, profissional_id}` |
| GET | `/expediente` | → `{aberto}` |
| POST | `/atendimento-humano` | `{conversa_id, telefone, nome, motivo}`: entrou na fila da recepção |
| POST | `/alertas-crise` | `{conversa_id, telefone, nome, mensagem}`: **o CRM deve notificar a equipe na hora** (push, SMS, alarme na tela) |
| POST | `/eventos-whatsapp` | `{tipo: "nova_mensagem", conversa_id, telefone, texto}`: atualiza a tela em tempo real (opcional) |

Labels aparecem como título de linha no WhatsApp. Mantenha até 24 caracteres
(ex.: `Seg 19/10 às 14h`) e mande o profissional em `profissionais[]` ou `descricoes[]`.

Se o CRM falhar, `/consentimentos`, `/atendimento-humano` e `/alertas-crise` vão para uma
**fila de reenvio** (tentativa a cada 30s). Um alerta de crise nunca se perde. Mesmo com o CRM fora
do ar, o paciente recebe na hora a mensagem com CVV e SAMU.

### A tela de atendimento do CRM chama o bot (`Authorization: Bearer BOT_API_TOKEN`)

| Método | Rota | Uso |
| --- | --- | --- |
| GET | `/api/atendimento/conversas?estado=aguardando_humano` | Fila (estados: `bot`, `aguardando_humano`, `humano`). `motivo_atendimento = "crise"` deve aparecer em destaque |
| GET | `/api/atendimento/conversas/{id}` | Conversa e histórico completo (paciente, bot e atendentes) |
| POST | `/api/atendimento/conversas/{id}/assumir` | `{atendente}`. A partir daqui, o bot fica em silêncio |
| POST | `/api/atendimento/conversas/{id}/mensagens` | `{atendente, texto}` envia pelo WhatsApp. **409** se não assumiu ou se passou a janela de 24h |
| POST | `/api/atendimento/conversas/{id}/encerrar` | `{atendente, enviar_despedida}`. A próxima mensagem do paciente volta para o bot |

**O token do bot fica no servidor do CRM, nunca no navegador.** A tela de referência
(`mock-crm/atendimento.html`) mostra o padrão: o navegador chama o CRM, e o CRM repassa ao bot.

## Conectar ao WhatsApp (Meta)

O passo a passo completo, com o cadastro como Tech Provider, está no
[checklist da equipe](https://claude.ai/code/artifact/e6078ee9-9c2d-4176-9289-c0cf3cd46d7c). Resumo:

1. Preencha o `.env` a partir de `.env.exemplo` (`META_APP_ID`, `META_CONFIG_ID`, `WHATSAPP_APP_SECRET`,
   `WHATSAPP_VERIFY_TOKEN`, `ADMIN_SENHA`) e rode com HTTPS:
   `uvicorn app.main:criar_app --factory --env-file .env --port 8001`.
2. Em *WhatsApp → Configuration → Webhook*, informe `https://SEU-DOMINIO/webhook` e o
   `WHATSAPP_VERIFY_TOKEN`. Assine os campos `messages`, `smb_message_echoes`, `history`,
   `smb_app_state_sync` e `account_update`.
3. Abra `https://SEU-DOMINIO/conectar-whatsapp`, entre com `ADMIN_USUARIO`/`ADMIN_SENHA` e clique
   em **Conectar o WhatsApp do celular**.

A página roda o **Embedded Signup** da Meta (v4) e, no servidor (`app/conectar.py`):
1. troca o código de 30 segundos pelo token da clínica;
2. inscreve o app nos webhooks da conta WhatsApp Business;
3. na coexistência, pede a sincronização de contatos e histórico, que tem prazo de 24h. Para um
   número novo, registra o número com um PIN de 6 dígitos, que é mostrado na tela.

O número e o token ficam salvos em `bot.db` (tabela `conexao_whatsapp`), e o bot passa a usá-los na
hora, sem reiniciar. Avisos da Meta sobre a conta (`account_update`) aparecem na própria página.

**Para testar sem a página** (antes da aprovação da Meta), use o número de teste do app de
desenvolvedor: preencha `WHATSAPP_TOKEN` e `WHATSAPP_PHONE_NUMBER_ID` no `.env`. Esses valores têm
prioridade sobre a conexão salva.

O SDK da Meta exige HTTPS. Para testar a página localmente, use um túnel (ex.: `cloudflared`) e
cadastre o domínio do túnel em "Domínios permitidos para o SDK do JavaScript".

### Manter o WhatsApp funcionando no celular (Coexistência)

A Meta permite usar o **mesmo número** no app **WhatsApp Business** do celular e na API ao mesmo
tempo. A equipe continua respondendo pelo celular e o bot atende pela API. As conversas aparecem
nos dois lados.

**Requisitos:**
- O número precisa estar no app **WhatsApp Business**, versão 2.24.17 ou superior. Se hoje for
  um WhatsApp comum, migre para o Business antes; as conversas são mantidas.
- A conexão é feita pelo **Embedded Signup**, que só está disponível para *Tech Providers* ou
  *Solution Partners* da Meta. Há dois caminhos: registrar o app de vocês como Tech Provider
  (exige verificação da empresa na Meta) ou usar um parceiro que ofereça coexistência.
- **Abra o app no celular pelo menos a cada 14 dias.** Se ficar mais tempo sem abrir, a Meta
  desconecta a sincronização.

**O que muda no app do celular:** mensagens temporárias, visualização única, localização em
tempo real e listas de transmissão ficam desativadas. Grupos não sincronizam. Aparelhos
conectados (WhatsApp Web, desktop) são desconectados na ativação.

**Custos:** mensagens enviadas pelo app continuam gratuitas. As da API seguem a tabela da Meta.

**Como o bot se comporta:**
- Quando alguém da equipe responde pelo celular, a Meta avisa o bot (`smb_message_echoes`). O bot
  registra a mensagem no histórico e **sai da conversa**: não responde mais aquele paciente até a
  conversa ficar parada por `PAUSA_CELULAR_HORAS` (padrão 12h) ou ser encerrada pela tela do CRM.
- O protocolo de crise continua valendo: mesmo com a conversa no celular, um alerta vai para o CRM.
- A tela do CRM mostra essas conversas como "Em atendimento com Celular da clínica" e pode assumi-las.
- **Atenção:** se o paciente escreve primeiro, o bot responde na hora. Ele só fica quieto depois
  que alguém da equipe responde pelo celular.

Sem coexistência, o número fica exclusivo da API e não funciona mais no app do celular.

## Hospedagem

Passo a passo para a VM gratuita do Google Cloud (e2-micro), com script de instalação, HTTPS
automático e backup diário: **[deploy/README.md](deploy/README.md)**.

- **Precisa ficar sempre ligado.** Evite planos gratuitos que "dormem": a Meta reenvia o webhook
  e um alerta de crise atrasaria.
- **Uma instância só.** O controle de ordem das mensagens fica em memória e o banco é SQLite. Para
  rodar várias instâncias, troque por PostgreSQL e um lock distribuído.
- É leve (cerca de 100 MB de RAM). Cabe no mesmo servidor do CRM.

## LGPD e segurança

- `bot.db` guarda o histórico das conversas, que é dado de saúde sensível. Defina a retenção,
  faça backup criptografado e restrinja o acesso ao servidor.
- O webhook rejeita chamadas sem a assinatura da Meta quando `WHATSAPP_APP_SECRET` está definido.
  **Em produção, sempre defina.**
- A tela de atendimento exibe mensagens do paciente só como texto (sem HTML), para evitar
  injeção de código.
- O token do WhatsApp da clínica fica em `bot.db`: proteja o arquivo como uma senha. A página
  Conectar WhatsApp exige `ADMIN_SENHA` e fica desligada enquanto ela não estiver definida.

## Ainda não implementado

- **Lembretes e confirmações** (templates aprovados pela Meta): enviados pelo CRM ou por um job
  que use a mesma API do WhatsApp.
- **Entender texto livre com IA** (ex.: "quero remarcar pra quinta de tarde"): hoje o bot entende
  toques nos botões, o número da opção ou o texto exato da opção. Duas respostas que ele não entende
  em seguida transferem para a equipe.
- **Áudio:** o bot pede texto. Transcrição pode entrar depois.
