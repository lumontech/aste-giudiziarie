#!/usr/bin/env bash
# =====================================================================
# Aste Giudiziarie — installazione su VPS Ubuntu 22.04 / 24.04
#
# Uso:
#   sudo bash install.sh <dominio> <email>
#
# Esempio (con sottodominio nip.io basato sull'IP della VPS):
#   sudo bash install.sh 203-0-113-5.nip.io info@lumontec.it
#
# Lo script:
#   1. installa pacchetti di sistema (python, nginx, certbot, ufw)
#   2. crea utente di servizio "aste"
#   3. crea venv + installa requirements + Playwright Chromium
#   4. genera FLASK_SECRET_KEY e .env (se non esistono)
#   5. installa unit systemd
#   6. configura Nginx reverse proxy
#   7. apre firewall 22/80/443
#   8. ottiene certificato Let's Encrypt e abilita HTTPS
# =====================================================================
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-info@lumontec.it}"
APP_DIR=/opt/aste.giudiziarie
APP_USER=aste
SERVICE_NAME=aste-server

if [[ -z "$DOMAIN" ]]; then
  echo "ERRORE: dominio richiesto."
  echo "Uso: sudo bash install.sh <dominio> [email]"
  echo "Es:  sudo bash install.sh 203-0-113-5.nip.io info@lumontec.it"
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "ERRORE: lanciare con sudo."
  exit 1
fi

echo "==> [1/8] Aggiornamento pacchetti di sistema"
apt update -y
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx ufw openssl curl

echo "==> [2/8] Utente di servizio: $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd -r -m -s /bin/bash "$APP_USER"

# Crea cartella app se non esiste — i file devono già essere stati copiati qui
if [[ ! -f "$APP_DIR/server.py" ]]; then
  echo "ERRORE: $APP_DIR/server.py non trovato."
  echo "Copia prima i file: scp -r aste.giudiziarie/* root@VPS:$APP_DIR/"
  exit 1
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> [3/8] Python venv e requirements"
sudo -u "$APP_USER" bash <<'EOSU'
cd /opt/aste.giudiziarie
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
EOSU

echo "==> [3b/8] Playwright Chromium + dipendenze di sistema"
"$APP_DIR/.venv/bin/playwright" install-deps chromium
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/playwright" install chromium

echo "==> [4/8] File .env"
if [[ ! -f "$APP_DIR/.env" ]]; then
  SECRET_KEY=$(openssl rand -hex 32)
  cp "$APP_DIR/deploy/env.example" "$APP_DIR/.env"
  sed -i "s|__GENERA_UNA_CHIAVE_LUNGA__|$SECRET_KEY|" "$APP_DIR/.env"
  echo ""
  echo "  >>> .env creato. DEVI ORA:"
  echo "  1. Generare hash password: $APP_DIR/.venv/bin/python $APP_DIR/server.py --hash-password 'TUA_PASSWORD'"
  echo "  2. Incollare l'hash in $APP_DIR/.env alla riga ADMIN_PASSWORD_HASH"
  echo "  3. Modificare ADMIN_USER se vuoi un username diverso"
  echo "  4. Rilanciare: systemctl restart $SERVICE_NAME"
  echo ""
fi
chmod 600 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"

echo "==> [5/8] systemd service"
cp "$APP_DIR/deploy/aste-server.service" "/etc/systemd/system/${SERVICE_NAME}.service"
touch /var/log/aste-server.log
chown "$APP_USER:$APP_USER" /var/log/aste-server.log
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl status "$SERVICE_NAME" --no-pager -l | head -20

echo "==> [6/8] Nginx reverse proxy per $DOMAIN"
sed "s/__DOMAIN__/$DOMAIN/g" "$APP_DIR/deploy/nginx.conf" > /etc/nginx/sites-available/aste
ln -sf /etc/nginx/sites-available/aste /etc/nginx/sites-enabled/aste
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==> [7/8] Firewall (UFW)"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> [8/8] HTTPS con Let's Encrypt"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" --redirect || {
  echo "ATTENZIONE: certbot ha fallito. Verifica che il dominio risolva all'IP della VPS."
  echo "Puoi rilanciare manualmente: certbot --nginx -d $DOMAIN"
}

echo ""
echo "================================================================"
echo "  COMPLETATO"
echo "================================================================"
echo "  Dashboard:  https://$DOMAIN"
echo "  Stato:      systemctl status $SERVICE_NAME"
echo "  Log:        tail -f /var/log/aste-server.log"
echo "  Nginx log:  tail -f /var/log/nginx/aste.access.log"
echo ""
echo "  Se non hai ancora impostato la password, fallo ora:"
echo "    $APP_DIR/.venv/bin/python $APP_DIR/server.py --hash-password 'TUA_PWD'"
echo "    nano $APP_DIR/.env  # incolla l'hash in ADMIN_PASSWORD_HASH"
echo "    systemctl restart $SERVICE_NAME"
echo "================================================================"
