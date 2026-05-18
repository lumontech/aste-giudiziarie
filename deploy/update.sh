#!/usr/bin/env bash
# =====================================================================
# Aste Giudiziarie — aggiornamento codice
#
# Esegue git pull, reinstalla eventuali nuove dipendenze, riavvia il
# servizio. Da lanciare sulla VPS dopo un commit/push dal locale.
#
# Uso:
#   sudo bash /opt/aste.giudiziarie/deploy/update.sh
# =====================================================================
set -euo pipefail

APP_DIR=/opt/aste.giudiziarie
APP_USER=aste
SERVICE_NAME=aste-server

if [[ $EUID -ne 0 ]]; then
  echo "ERRORE: lanciare con sudo."
  exit 1
fi

echo "==> git pull"
cd "$APP_DIR"
sudo -u "$APP_USER" git pull --ff-only

echo "==> aggiorno dipendenze (se cambiate)"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r requirements.txt -q

echo "==> riavvio servizio"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl status "$SERVICE_NAME" --no-pager -l | head -10

echo ""
echo "================================================================"
echo "  Aggiornato. Log live: tail -f /var/log/aste-server.log"
echo "================================================================"
