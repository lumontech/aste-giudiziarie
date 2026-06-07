#!/usr/bin/env bash
# =====================================================================
# Aste Giudiziarie — aggiornamento da git
#
# git pull → reinstalla dep cambiate → pm2 restart
#
# Uso (sul VPS):
#   sudo bash /root/aste-giudiziarie/deploy/update.sh
# =====================================================================
# Niente "set -u": .env contiene hash bcrypt con $ letterali ($2b$12$...) che bash
# interpreterebbe come variabili non definite.
set -eo pipefail

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

# Estrai SOLO GITHUB_PAT dal .env (senza sourcing, per evitare expansion di $ negli hash)
GITHUB_PAT=$(grep -E '^GITHUB_PAT=' "$ENV_FILE" | head -1 | cut -d= -f2-)

if [[ -z "$GITHUB_PAT" ]]; then
  echo "ERRORE: GITHUB_PAT mancante in $ENV_FILE."
  exit 1
fi

echo "==> git pull (con PAT)"
cd "$APP_DIR"
# Usa URL embedded auth, poi ripristina remote pulito (evita problemi quoting http.extraHeader)
git remote set-url origin "https://oauth2:${GITHUB_PAT}@github.com/lumontech/aste-giudiziarie.git"
git pull --ff-only
git remote set-url origin https://github.com/lumontech/aste-giudiziarie.git

echo "==> aggiorno dipendenze"
"$APP_DIR/.venv/bin/pip" install -r requirements.txt -q

echo "==> riavvio pm2"
pm2 restart aste-giudiziarie
pm2 save

echo ""
echo "================================================================"
echo "  Aggiornato. Log: pm2 logs aste-giudiziarie --lines 30"
echo "================================================================"
