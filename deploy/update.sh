#!/usr/bin/env bash
# =====================================================================
# Aste Giudiziarie — aggiornamento da git
#
# git pull → reinstalla dep cambiate → pm2 restart
#
# Uso (sul VPS):
#   sudo bash /root/aste-giudiziarie/deploy/update.sh
# =====================================================================
set -euo pipefail

APP_DIR=/root/aste-giudiziarie
ENV_FILE=/etc/aste-giudiziarie/.env

if [[ $EUID -ne 0 ]]; then
  echo "ERRORE: lanciare come root o con sudo."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERRORE: $ENV_FILE non esiste. Setup non completato."
  exit 1
fi

# Carica GITHUB_PAT da .env (per repo privato)
set -a
source "$ENV_FILE"
set +a

if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo "ERRORE: GITHUB_PAT mancante in $ENV_FILE."
  exit 1
fi

echo "==> git pull (con PAT)"
cd "$APP_DIR"
git -c http.extraHeader="Authorization: Bearer $GITHUB_PAT" pull --ff-only

echo "==> aggiorno dipendenze"
"$APP_DIR/.venv/bin/pip" install -r requirements.txt -q

echo "==> riavvio pm2"
pm2 restart aste-giudiziarie
pm2 save

echo ""
echo "================================================================"
echo "  Aggiornato. Log: pm2 logs aste-giudiziarie --lines 30"
echo "================================================================"
