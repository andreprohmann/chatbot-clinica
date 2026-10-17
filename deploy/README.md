# Hospedar no Google Cloud (e2-micro gratuita)

O plano gratuito do Google inclui 1 VM `e2-micro` (2 vCPU compartilhadas, 1 GB de RAM), 30 GB de
**disco padrão** e 1 GB de saída de dados por mês, folgado para mensagens de WhatsApp.
Vale só nas regiões `us-east1`, `us-central1` e `us-west1`. Fonte:
[Google Cloud Free Tier](https://docs.cloud.google.com/free/docs/free-cloud-features).

Tempo estimado: 30 a 40 minutos.

## 1. Conta e proteção contra cobrança

1. Acesse console.cloud.google.com, crie um projeto (ex.: `bot-clinica`) e ative o faturamento.
   O Google exige cartão até para o plano gratuito.
2. Em **Faturamento → Orçamentos e alertas**, crie um orçamento de **R$ 5** com alertas por e-mail.
   Se algo sair do gratuito, vocês ficam sabendo no mesmo dia.
3. Se a conta começar no teste de US$ 300 (90 dias), faça o **upgrade para conta paga** antes de
   o teste acabar. Sem isso, a VM pode ser desligada. O plano gratuito continua sem custo
   dentro dos limites.

## 2. Criar a VM

**Compute Engine → Instâncias de VM → Criar instância**:

| Campo | Valor | Por quê |
| --- | --- | --- |
| Nome | `bot-whatsapp` | |
| Região | `us-east1` (Carolina do Sul) | Das três gratuitas, é a mais próxima do Brasil |
| Tipo de máquina | `e2-micro` | A única gratuita |
| Disco de inicialização | Debian 12, **Disco permanente padrão**, 30 GB | O padrão do console é "equilibrado", que **é cobrado**. Troque |
| Firewall | Marque **Permitir HTTP** e **Permitir HTTPS** | Para o certificado e para a Meta |
| Escopos de acesso | "Definir acesso para cada API", e **Storage: leitura e gravação** | Para o backup ir para o Cloud Storage |

Antes de criar, confira a estimativa de custo no canto da tela. Ela deve indicar que a VM entra
no nível gratuito.

## 3. IP fixo e domínio

1. **Rede VPC → Endereços IP**: na linha do IP externo da VM, clique em **Promover para estático**.
   Sem isso, o IP pode mudar quando a VM reinicia. Pelos relatos, IP estático em uso não é cobrado,
   mas a documentação oficial não diz. Confira na primeira fatura.
2. No DNS do domínio da clínica, crie um registro **A**: `bot.suaclinica.com.br` → IP da VM.
   Espere alguns minutos e teste com `ping bot.suaclinica.com.br`.

## 4. Instalar o bot

Na lista de VMs, clique em **SSH** (abre um terminal no navegador) e rode:

```bash
curl -fsSL https://raw.githubusercontent.com/andreprohmann/chatbot-clinica/main/deploy/instalar.sh -o instalar.sh
sudo bash instalar.sh bot.suaclinica.com.br
```

O script:
- cria 1 GB de memória de troca;
- instala Python, Caddy (HTTPS automático) e as atualizações de segurança automáticas;
- baixa o código, cria a configuração e sobe o bot como serviço que reinicia sozinho;
- agenda o backup diário.

**Anote as senhas que ele mostra no fim**: `WHATSAPP_VERIFY_TOKEN`, `BOT_API_TOKEN` e `ADMIN_SENHA`.
Elas aparecem uma única vez.

Complete a configuração e reinicie:

```bash
sudo nano /etc/bot-whatsapp/bot.env      # META_APP_ID, META_CONFIG_ID, WHATSAPP_APP_SECRET, CRM_URL, CRM_TOKEN
sudo systemctl restart bot-whatsapp
```

Teste no navegador: `https://bot.suaclinica.com.br/health` deve responder `"ok": true`.

## 5. Backup fora da VM (recomendado)

O backup diário fica em `/var/backups/bot-whatsapp` (14 dias). Se a VM for perdida, ele vai junto.
Para guardar também fora dela:

1. **Cloud Storage → Criar bucket**: região `us-east1`, classe Standard, acesso público
   **bloqueado**. O gratuito cobre 5 GB nas mesmas três regiões.
2. Acrescente em `/etc/bot-whatsapp/bot.env`: `BACKUP_BUCKET=gs://nome-do-bucket`
3. Teste: `sudo /etc/cron.daily/bot-whatsapp-backup && gcloud storage ls gs://nome-do-bucket`

Os backups contêm conversas de pacientes. Mantenham o bucket privado e com acesso só para a equipe técnica.

## 6. Conectar à Meta

Siga o [checklist](https://claude.ai/code/artifact/e6078ee9-9c2d-4176-9289-c0cf3cd46d7c) a partir
da etapa 6: webhook em `https://bot.suaclinica.com.br/webhook` e, depois, a página
`https://bot.suaclinica.com.br/conectar-whatsapp`.

## Dia a dia

| Tarefa | Comando |
| --- | --- |
| Atualizar para a versão mais nova do GitHub | `sudo bash instalar.sh bot.suaclinica.com.br` |
| Ver o log do bot | `sudo journalctl -u bot-whatsapp -f` |
| Reiniciar | `sudo systemctl restart bot-whatsapp` |
| Situação do serviço | `systemctl status bot-whatsapp` |
| Restaurar um backup | `sudo systemctl stop bot-whatsapp`, depois `gunzip -c /var/backups/bot-whatsapp/bot-AAAAMMDD.db.gz \| sudo -u bot tee /var/lib/bot-whatsapp/bot.db >/dev/null` e `sudo systemctl start bot-whatsapp` |

## Limites desta hospedagem

- **1 GB de RAM:** sobra para o bot (cerca de 100 MB). O CRM com banco de dados pode não caber
  junto. Se precisar, o CRM vai para outro lugar e conversa com o bot pela internet.
- **Dados nos EUA:** as regiões gratuitas ficam lá. A LGPD permite transferência internacional
  com as salvaguardas previstas (como cláusulas contratuais), mas **confirmem com o jurídico** e
  registrem a transferência na política de privacidade e no registro de tratamento de dados.
