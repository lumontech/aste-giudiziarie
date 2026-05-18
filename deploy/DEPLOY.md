# Deploy su VPS Contabo

Guida step-by-step. Sostituisci `IP_VPS` con l'IP pubblico della tua Contabo.
Es. IP `203.0.113.5` → dominio nip.io `203-0-113-5.nip.io`.

## 1. Carica i file sulla VPS

Dal **tuo PC locale** (Windows cmd):

```cmd
cd C:\Users\Stefano\.claude
scp -r aste.giudiziarie root@IP_VPS:/opt/
```

Se non hai `scp` su Windows, installa OpenSSH client (Impostazioni → App
facoltative → OpenSSH client), oppure usa WinSCP / FileZilla.

> Tip: escludere `.venv\` e `results_backup.json` per non trasferire MB inutili.
> Da cmd puoi usare `robocopy` per copiare prima in una cartella temp filtrando.

## 2. Connettiti alla VPS via SSH

```cmd
ssh root@IP_VPS
```

## 3. Lancia lo script di installazione

```bash
cd /opt/aste.giudiziarie/deploy
chmod +x install.sh
sudo bash install.sh 203-0-113-5.nip.io info@lumontec.it
```

Tempi: 3-7 minuti (download Chromium + dipendenze).

## 4. Imposta la password

Lo script crea `.env` con secret key, ma serve la password admin:

```bash
sudo /opt/aste.giudiziarie/.venv/bin/python /opt/aste.giudiziarie/server.py --hash-password 'TuaPasswordSegreta'
# copia l'output ($2b$12$...)

sudo nano /opt/aste.giudiziarie/.env
# incolla l'hash in: ADMIN_PASSWORD_HASH=$2b$12$...
# modifica ADMIN_USER se vuoi (default: stefano)

sudo systemctl restart aste-server
```

## 5. Accedi

Apri nel browser: **https://203-0-113-5.nip.io**

Schermata di login → inserisci `stefano` + la password scelta.

## 6. Operazioni quotidiane

```bash
# Stato server
sudo systemctl status aste-server

# Log live
sudo tail -f /var/log/aste-server.log

# Log Nginx
sudo tail -f /var/log/nginx/aste.access.log

# Riavvio
sudo systemctl restart aste-server

# Aggiornamento codice (dopo aver fatto scp dei file modificati)
cd /opt/aste.giudiziarie
sudo -u aste .venv/bin/pip install -r requirements.txt
sudo systemctl restart aste-server
```

## 7. Scraping automatico

Il server.py ha già uno scheduler interno (APScheduler) che lancia uno
scraping completo ogni notte alle **02:00** Europe/Rome. Puoi anche
lanciarlo manualmente dalla dashboard con il bottone "Aggiorna Dati"
o "Scansiona Ora".

## 8. Backup `results.json`

```bash
# Copia rapida dei dati attuali sul PC locale
scp root@IP_VPS:/opt/aste.giudiziarie/results.json ./backup_$(date +%Y%m%d).json
```

## Troubleshooting

| Sintomo | Fix |
|---|---|
| Login non funziona | `journalctl -u aste-server -n 50` per vedere errori |
| Pagina bianca | Verifica `systemctl status aste-server` — il processo è up? |
| 502 Bad Gateway | Flask è down: `systemctl restart aste-server` |
| Certificato HTTPS scaduto | `sudo certbot renew` (automatico via cron di certbot) |
| Scraper non funziona | `/opt/aste.giudiziarie/.venv/bin/playwright install chromium` |
| Cookie sessione persi al riavvio | Normale — la `secret_key` è generata una volta in `.env`, controlla che non venga rigenerata |

## Sicurezza

- `.env` ha permessi `600` (solo owner legge)
- Cookie sessione `HttpOnly`, `SameSite=Lax`, `Secure` (sotto HTTPS)
- Password hashata con bcrypt rounds=12
- Nginx fa terminazione TLS, Flask ascolta solo su 127.0.0.1
- Firewall UFW chiude tutto tranne 22/80/443
- `/api/ping` è pubblico (per healthcheck) — gli altri richiedono auth

## Disinstallazione

```bash
sudo systemctl stop aste-server
sudo systemctl disable aste-server
sudo rm /etc/systemd/system/aste-server.service
sudo rm /etc/nginx/sites-enabled/aste /etc/nginx/sites-available/aste
sudo systemctl reload nginx
sudo userdel -r aste
sudo rm -rf /opt/aste.giudiziarie
```
