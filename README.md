# Aste Giudiziarie — Dashboard

Dashboard web per il monitoraggio di aste giudiziarie italiane reali da
[pvp.giustizia.it](https://pvp.giustizia.it).

## Architettura

- **Backend Flask** (`server.py`) — API REST + serve la dashboard, gestisce login,
  scheduler scraping notturno.
- **Scraper Playwright** (`scraper.py`) — naviga il portale PVP, parsa lotti per
  regione/budget, salva in `results.json`.
- **Dashboard SPA** (`dashboard.html`) — singolo file HTML standalone, dark
  theme, filtri lato client, modal dettaglio, analisi AI via Anthropic.

## Sviluppo locale

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/playwright install chromium
.venv/Scripts/python server.py
```

Dashboard su `http://127.0.0.1:5000`.

Per disabilitare login in dev: aggiungi `AUTH_ENABLED=false` in un file `.env`.

## Deploy su VPS

Vedi [`deploy/DEPLOY.md`](deploy/DEPLOY.md) per la guida completa
(Ubuntu 22.04 + Nginx + Let's Encrypt + systemd).

Quick start sulla VPS:
```bash
git clone <REPO_URL> /opt/aste.giudiziarie
cd /opt/aste.giudiziarie/deploy
sudo bash install.sh <DOMINIO> <EMAIL>
```

## Stack

| Componente | Tech |
|---|---|
| API | Flask + Flask-CORS + APScheduler |
| Scraping | Playwright (async) + Chromium |
| Auth | Flask session + bcrypt |
| Frontend | HTML5 + vanilla JS + Chart.js (CDN) |
| Reverse proxy | Nginx |
| Process manager | systemd |
| TLS | Let's Encrypt via certbot |
| AI assist | Anthropic API (claude-sonnet-4) |
