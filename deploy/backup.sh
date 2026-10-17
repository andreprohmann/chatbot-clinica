#!/usr/bin/env bash
# Backup diário do banco do bot (instalado em /etc/cron.daily pelo instalar.sh).
#
# Guarda 14 dias de cópias na própria VM. Para copiar também para fora da VM (recomendado),
# defina BACKUP_BUCKET=gs://nome-do-bucket em /etc/bot-whatsapp/bot.env.
set -euo pipefail

BANCO=/var/lib/bot-whatsapp/bot.db
DESTINO=/var/backups/bot-whatsapp
[[ -f $BANCO ]] || exit 0

BUCKET=$(grep -E '^BACKUP_BUCKET=' /etc/bot-whatsapp/bot.env 2>/dev/null | cut -d= -f2- || true)
ARQUIVO="$DESTINO/bot-$(date +%Y%m%d).db"

# .backup gera uma cópia consistente mesmo com o bot gravando no banco.
sqlite3 "$BANCO" ".backup '$ARQUIVO'"
gzip -f "$ARQUIVO"
chmod 600 "$ARQUIVO.gz"
find "$DESTINO" -name 'bot-*.db.gz' -mtime +14 -delete

if [[ -n "$BUCKET" ]]; then
  gcloud storage cp --quiet "$ARQUIVO.gz" "$BUCKET/" \
    || logger -t bot-whatsapp-backup "Falha ao copiar backup para $BUCKET"
fi
