#!/usr/bin/env bash
# Instala ou atualiza o bot numa VM Debian (pensado para a e2-micro gratuita do Google Cloud).
#
# Uso (na VM, como root):
#   sudo bash instalar.sh bot.suaclinica.com.br
#
# Pode rodar de novo a qualquer momento: atualiza o código e mantém configuração e banco.
set -euo pipefail

DOMINIO="${1:-}"
REPO="${REPO:-https://github.com/andreprohmann/chatbot-clinica.git}"
RAMO="${RAMO:-main}"
APP=/opt/bot-whatsapp
DADOS=/var/lib/bot-whatsapp
CONF=/etc/bot-whatsapp
ENVFILE=$CONF/bot.env

[[ $EUID -eq 0 ]] || { echo "Rode como root: sudo bash $0 SEU-DOMINIO"; exit 1; }
[[ -n "$DOMINIO" ]] || { echo "Informe o domínio: sudo bash $0 bot.suaclinica.com.br"; exit 1; }
tem_systemd() { [[ -d /run/systemd/system ]]; }
passo() { echo -e "\n==> $*"; }

passo "Memória de troca (a e2-micro tem só 1 GB de RAM)"
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile
  swapon /swapfile 2>/dev/null || echo "   (swap será ativado no próximo boot)"
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
else
  echo "   já existe"
fi

passo "Pacotes do sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv sqlite3 caddy unattended-upgrades ca-certificates >/dev/null
echo "   ok (atualizações de segurança automáticas ativadas)"

passo "Usuário e pastas"
id bot &>/dev/null || useradd --system --home-dir "$DADOS" --shell /usr/sbin/nologin bot
install -d -o bot -g bot -m 750 "$DADOS"
install -d -o root -g root -m 755 "$CONF"
install -d -o root -g root -m 700 /var/backups/bot-whatsapp

passo "Código ($REPO, ramo $RAMO)"
if [[ -d $APP/.git ]]; then
  git -C "$APP" fetch -q origin "$RAMO" && git -C "$APP" reset -q --hard "origin/$RAMO"
else
  git clone -q --branch "$RAMO" "$REPO" "$APP"
fi
echo "   versão $(git -C "$APP" log -1 --format='%h %s')"

passo "Ambiente Python"
[[ -d $APP/.venv ]] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
echo "   ok"

passo "Configuração ($ENVFILE)"
if [[ ! -f $ENVFILE ]]; then
  aleatorio() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
  VERIFY=$(aleatorio); BOTTOKEN=$(aleatorio); SENHA=$(aleatorio)
  sed -e "s|^DB_PATH=.*|DB_PATH=$DADOS/bot.db|" \
      -e "s|^WHATSAPP_VERIFY_TOKEN=.*|WHATSAPP_VERIFY_TOKEN=$VERIFY|" \
      -e "s|^BOT_API_TOKEN=.*|BOT_API_TOKEN=$BOTTOKEN|" \
      -e "s|^ADMIN_SENHA=.*|ADMIN_SENHA=$SENHA|" \
      "$APP/.env.exemplo" > "$ENVFILE"
  chmod 600 "$ENVFILE"
  cat <<EOF
   Criado com valores gerados agora. ANOTE em local seguro (aparecem só esta vez):
     WHATSAPP_VERIFY_TOKEN = $VERIFY
     BOT_API_TOKEN         = $BOTTOKEN
     ADMIN_SENHA           = $SENHA   (usuário: admin)
   Complete META_APP_ID, META_CONFIG_ID, WHATSAPP_APP_SECRET, CRM_URL e CRM_TOKEN com:
     sudo nano $ENVFILE   e depois   sudo systemctl restart bot-whatsapp
EOF
else
  echo "   já existe; mantido"
fi

passo "Serviço do bot (systemd)"
install -m 644 "$APP/deploy/bot-whatsapp.service" /etc/systemd/system/bot-whatsapp.service

passo "HTTPS com Caddy (certificado automático para $DOMINIO)"
sed "s|SEU-DOMINIO|$DOMINIO|" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile

passo "Backup diário do banco"
install -m 755 "$APP/deploy/backup.sh" /etc/cron.daily/bot-whatsapp-backup

if tem_systemd; then
  systemctl daemon-reload
  systemctl enable -q --now bot-whatsapp
  systemctl restart bot-whatsapp
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  sleep 3
  if curl -fsS http://127.0.0.1:8001/health >/dev/null; then
    echo -e "\nPronto! O bot está rodando. Teste: https://$DOMINIO/health"
  else
    echo -e "\nO bot não respondeu. Veja o log: sudo journalctl -u bot-whatsapp -n 50"
    exit 1
  fi
else
  echo -e "\n(systemd não encontrado: serviços instalados, mas não iniciados)"
fi
