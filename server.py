"""
Server Flask — API per le aste pvp.giustizia.it

Endpoint:
  GET /api/ping
  GET /api/aste?regione=Campania&prezzoMax=10000&tipologia=...&comune=...
  GET /api/aste/<id>
  GET /api/scrape?regione=Campania&prezzoMax=10000   (lancia scraper e aggiorna results.json)
  GET /api/status   (informazioni sull'ultimo aggiornamento)
"""

import json
import logging
import os
import secrets
import threading
import asyncio
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

# Carica .env se presente (locale o in /etc/aste-giudiziarie/.env in prod)
try:
    from dotenv import load_dotenv
    # Cerca .env locale prima, poi quello di sistema
    for _p in (".env", "/etc/aste-giudiziarie/.env"):
        if os.path.exists(_p):
            load_dotenv(_p, override=False)
            break
except ImportError:
    pass

from flask import (
    Flask, jsonify, request, abort, send_from_directory, session, redirect
)
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

from scraper import main as run_scraper_main, RESULTS_FILE, REGIONI_ITALIANE

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=14)
# Cookie sicuro se dietro HTTPS (rilevato via header X-Forwarded-Proto da Nginx)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
)

# CORS solo in dev (quando dashboard è aperta da file://). In prod stessa origine.
CORS(app, origins="*", supports_credentials=True)


# ---------------------------------------------------------------------------
# Auth — supporta multi-utente
# ---------------------------------------------------------------------------
# Modalità multi-utente (PROD):
#   ADMIN_USERS=stefano,antonio
#   ADMIN_HASH_stefano=$2b$12$...
#   ADMIN_HASH_antonio=$2b$12$...
#
# Modalità legacy single-user (compat):
#   ADMIN_USER=stefano
#   ADMIN_PASSWORD_HASH=$2b$12$...
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() == "true"

def _load_users() -> dict:
    """Costruisce mappa {username: bcrypt_hash} da env."""
    users: dict[str, str] = {}
    raw = os.environ.get("ADMIN_USERS", "").strip()
    if raw:
        for u in [x.strip() for x in raw.split(",") if x.strip()]:
            h = os.environ.get(f"ADMIN_HASH_{u}", "").strip()
            if h:
                users[u] = h
    # Fallback legacy single-user
    legacy_user = os.environ.get("ADMIN_USER", "").strip()
    legacy_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
    if legacy_user and legacy_hash and legacy_user not in users:
        users[legacy_user] = legacy_hash
    return users

ADMIN_USERS_MAP = _load_users()
ADMIN_PASSWORD_PLAIN = os.environ.get("ADMIN_PASSWORD", "")  # solo dev


def _verify_password(user: str, plain: str) -> bool:
    h = ADMIN_USERS_MAP.get(user)
    if h and _HAS_BCRYPT:
        try:
            return bcrypt.checkpw(plain.encode("utf-8"), h.encode("utf-8"))
        except Exception:
            return False
    # Dev fallback: password in chiaro (solo se utente non in mappa)
    if not ADMIN_USERS_MAP and ADMIN_PASSWORD_PLAIN:
        return secrets.compare_digest(plain, ADMIN_PASSWORD_PLAIN)
    return False


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not AUTH_ENABLED:
            return fn(*args, **kwargs)
        u = session.get("user")
        if u and u in ADMIN_USERS_MAP:
            return fn(*args, **kwargs)
        return jsonify({"error": "unauthorized"}), 401
    return wrapper

# Lock per evitare scraping concorrenti
_scrape_lock = threading.Lock()
_scrape_running = False
_scrape_status: dict = {
    "running": False,
    "last_run": None,
    "last_run_duration": None,
    "last_error": None,
}


# ---------------------------------------------------------------------------
# Helpers: lettura dati
# ---------------------------------------------------------------------------

def _load_results() -> dict:
    """Carica results.json; restituisce struttura vuota se non esiste."""
    if not os.path.exists(RESULTS_FILE):
        return {
            "timestamp_aggiornamento": None,
            "totale_aste": 0,
            "aste_per_regione": {},
            "aste": [],
        }
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter_aste(
    aste: list[dict],
    regione: Optional[str] = None,
    prezzo_max: Optional[float] = None,
    prezzo_min: Optional[float] = None,
    tipologia: Optional[str] = None,
    comune: Optional[str] = None,
    provincia: Optional[str] = None,
    tribunale: Optional[str] = None,
) -> list[dict]:
    """Applica i filtri alla lista di aste."""
    result = aste

    if regione:
        reg_lc = regione.lower()
        result = [a for a in result if reg_lc in (a.get("regione") or "").lower()]

    if prezzo_max is not None:
        result = [
            a for a in result
            if a.get("prezzo_base") is None or a["prezzo_base"] <= prezzo_max
        ]

    if prezzo_min is not None:
        result = [
            a for a in result
            if a.get("prezzo_base") is not None and a["prezzo_base"] >= prezzo_min
        ]

    if tipologia:
        tip_lc = tipologia.lower()
        result = [a for a in result if tip_lc in (a.get("tipologia") or "").lower()]

    if comune:
        com_lc = comune.lower()
        result = [a for a in result if com_lc in (a.get("comune") or "").lower()]

    if provincia:
        prov_lc = provincia.lower()
        result = [a for a in result if prov_lc in (a.get("provincia") or "").lower()]

    if tribunale:
        trib_lc = tribunale.lower()
        result = [a for a in result if trib_lc in (a.get("tribunale") or "").lower()]

    return result


def _paginate(items: list, page: int, per_page: int) -> tuple[list, dict]:
    """Restituisce la slice paginata e i metadati di paginazione."""
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


# ---------------------------------------------------------------------------
# Funzione di scraping (eseguita in background)
# ---------------------------------------------------------------------------

def _do_scrape(regioni: Optional[list[str]], prezzo_max: Optional[int]) -> None:
    global _scrape_running, _scrape_status
    _scrape_running = True
    _scrape_status["running"] = True
    _scrape_status["last_error"] = None
    start = datetime.now()
    logger.info("Scraping avviato (background): regioni=%s prezzoMax=%s", regioni, prezzo_max)
    try:
        asyncio.run(run_scraper_main(regioni=regioni, prezzo_max=prezzo_max))
        _scrape_status["last_run"] = datetime.now().isoformat()
        _scrape_status["last_run_duration"] = round((datetime.now() - start).total_seconds(), 1)
        logger.info("Scraping completato in %.1f s", _scrape_status["last_run_duration"])
    except Exception as exc:
        _scrape_status["last_error"] = str(exc)
        logger.error("Errore durante lo scraping: %s", exc)
    finally:
        _scrape_running = False
        _scrape_status["running"] = False
        _scrape_lock.release()


# ---------------------------------------------------------------------------
# Endpoint: static + auth
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    """Serve la dashboard. Mostrata anche senza auth; il JS gestisce il login."""
    return send_from_directory(BASE_DIR, "dashboard.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/api/login", methods=["POST"])
def login():
    if not AUTH_ENABLED:
        # Quando auth è disabilitata, usa il primo utente della mappa o "admin"
        u = next(iter(ADMIN_USERS_MAP), "admin")
        session["user"] = u
        session.permanent = True
        return jsonify({"ok": True, "user": u, "auth_disabled": True})
    body = request.get_json(silent=True) or {}
    user = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    if user not in ADMIN_USERS_MAP or not _verify_password(user, pwd):
        return jsonify({"error": "Credenziali non valide"}), 401
    session.clear()
    session["user"] = user
    session.permanent = True
    logger.info("Login ok per utente %s da %s", user, request.remote_addr)
    return jsonify({"ok": True, "user": user})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def me():
    u = session.get("user")
    return jsonify({
        "authenticated": (u in ADMIN_USERS_MAP) or not AUTH_ENABLED,
        "user": u,
        "auth_enabled": AUTH_ENABLED,
    })


# ---------------------------------------------------------------------------
# Endpoint: health check (pubblico, per check di Nginx)
# ---------------------------------------------------------------------------

@app.route("/api/ping", methods=["GET"])
def ping():
    """Health check."""
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# ---------------------------------------------------------------------------
# Endpoint: stato sistema
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
@require_auth
def status():
    """Informazioni sull'ultimo aggiornamento e sullo stato dello scraper."""
    data = _load_results()
    return jsonify({
        "scraper": _scrape_status,
        "dati": {
            "timestamp_aggiornamento": data.get("timestamp_aggiornamento"),
            "totale_aste": data.get("totale_aste", 0),
            "aste_per_regione": data.get("aste_per_regione", {}),
            "filtro_prezzo_max": data.get("filtro_prezzo_max"),
        },
        "regioni_disponibili": REGIONI_ITALIANE,
    })


# ---------------------------------------------------------------------------
# Endpoint: lista aste con filtri
# ---------------------------------------------------------------------------

@app.route("/api/aste", methods=["GET"])
@require_auth
def get_aste():
    """
    Restituisce le aste filtrate da results.json.

    Query params:
      regione      — es. Campania
      prezzoMax    — es. 10000
      prezzoMin    — es. 5000
      tipologia    — es. appartamento
      comune       — es. Napoli
      provincia    — es. NA
      tribunale    — es. Napoli
      page         — numero pagina (default 1)
      perPage      — elementi per pagina (default 50, max 200)
    """
    regione = request.args.get("regione") or request.args.get("r")
    prezzo_max_raw = request.args.get("prezzoMax") or request.args.get("pm")
    prezzo_min_raw = request.args.get("prezzoMin") or request.args.get("pmin")
    tipologia = request.args.get("tipologia")
    comune = request.args.get("comune")
    provincia = request.args.get("provincia")
    tribunale = request.args.get("tribunale")

    try:
        prezzo_max = float(prezzo_max_raw) if prezzo_max_raw else None
        prezzo_min = float(prezzo_min_raw) if prezzo_min_raw else None
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(50000, max(1, int(request.args.get("perPage", 200))))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": f"Parametro non valido: {exc}"}), 400

    data = _load_results()
    aste = data.get("aste", [])

    filtered = _filter_aste(
        aste,
        regione=regione,
        prezzo_max=prezzo_max,
        prezzo_min=prezzo_min,
        tipologia=tipologia,
        comune=comune,
        provincia=provincia,
        tribunale=tribunale,
    )

    paginated, pag_meta = _paginate(filtered, page, per_page)

    return jsonify({
        "timestamp_aggiornamento": data.get("timestamp_aggiornamento"),
        "filtri": {
            "regione": regione,
            "prezzoMax": prezzo_max,
            "prezzoMin": prezzo_min,
            "tipologia": tipologia,
            "comune": comune,
            "provincia": provincia,
            "tribunale": tribunale,
        },
        "paginazione": pag_meta,
        "aste": paginated,
    })


# ---------------------------------------------------------------------------
# Endpoint: dettaglio singolo lotto
# ---------------------------------------------------------------------------

@app.route("/api/aste/<int:asta_id>", methods=["GET"])
@require_auth
def get_asta_detail(asta_id: int):
    """Restituisce il dettaglio di un singolo lotto tramite ID."""
    data = _load_results()
    aste = data.get("aste", [])
    for a in aste:
        if a.get("id") == asta_id:
            return jsonify(a)
    abort(404, description=f"Lotto con id={asta_id} non trovato.")


# ---------------------------------------------------------------------------
# Endpoint: analisi mercato incrociata (Claude + web search)
# ---------------------------------------------------------------------------

# Cache in-memory delle analisi (idLotto -> result)
_market_cache: dict = {}
_market_cache_lock = threading.Lock()


def _build_market_prompt(asta: dict) -> tuple[str, str]:
    """Costruisce prompt mirato in base al tipo di lotto.
    Ritorna (prompt, tipo_analisi: 'auto'|'immobile'|'altro')."""
    tip_id = asta.get("tipologia_id")
    titolo = asta.get("titolo", "")
    descrizione = asta.get("descrizione", "")
    prezzo = asta.get("prezzo_base", 0) or 0
    comune = asta.get("comune", "")
    provincia = asta.get("provincia", "")
    regione = asta.get("regione", "")
    indirizzo = asta.get("indirizzo", "")
    mq = asta.get("mq_stimati")
    data_vendita = asta.get("data_vendita", "")
    days = asta.get("days_to_auction")

    base_context = f"""DATI LOTTO ALL'ASTA:
- Titolo: {titolo}
- Descrizione completa: {descrizione[:1500]}
- Prezzo base asta: € {prezzo:,.0f}
- Località: {comune} ({provincia}), {regione}
- Indirizzo: {indirizzo}
- Categoria: {asta.get('tipologia_label', 'sconosciuta')}
- Data vendita: {data_vendita}{f' (in {days} giorni)' if days is not None else ''}
"""

    if tip_id == 6:  # auto
        prompt = base_context + f"""
Sei un esperto del mercato auto usate in Italia. Analizza questo lotto giudiziario.

OBIETTIVO:
1. Identifica dalla descrizione: MARCA, MODELLO, ANNO IMMATRICOLAZIONE, ALIMENTAZIONE, CHILOMETRI (se menzionati), VERSIONE/ALLESTIMENTO
2. Usa il web_search per cercare lo stesso modello/anno su AUTOSCOUT24, SUBITO.IT, AUTOMOBILE.IT
   - Esempio query: "FIAT 500L 2014 benzina km" oppure "site:autoscout24.it FIAT Punto 2010"
3. Trova almeno 3-5 annunci comparabili con caratteristiche simili
4. Calcola prezzo medio/min/max di mercato
5. Confronta con prezzo asta giudiziaria € {prezzo:,.0f}
6. Considera che auto all'asta possono avere: ipoteche, fermo amministrativo, stato meccanico ignoto, possibile mancanza chiavi/documenti

RISPONDI ESCLUSIVAMENTE CON UN OGGETTO JSON (no testo prima/dopo, no markdown) in questa struttura:
{{
  "tipo_analisi": "auto",
  "caratteristiche": {{"marca": "...", "modello": "...", "anno": "...", "alimentazione": "...", "km": "...", "versione": "..."}},
  "prezzo_mercato": {{"min": numero, "media": numero, "max": numero, "n_confronti": numero, "fonti": ["autoscout24","subito.it"]}},
  "annunci_simili": [{{"titolo":"...","prezzo":numero,"anno":"...","km":"...","url":"..."}}, ...max 3],
  "convenienza_percentuale": numero (negativo se asta sotto mercato),
  "verdetto": "OTTIMO" | "BUONO" | "MEDIO" | "MEDIOCRE" | "PESSIMO",
  "punteggio": numero 0-100,
  "motivazione": "stringa max 200 caratteri",
  "rischi": ["...", "..."],
  "raccomandazione": "stringa max 150 caratteri"
}}"""
        return prompt, "auto"

    elif tip_id in (1, 2, 3, 4, 5):  # immobili
        prompt = base_context + f"""
Sei un esperto di mercato immobiliare italiano. Analizza questo lotto giudiziario.

OBIETTIVO:
1. Identifica dalla descrizione: TIPOLOGIA precisa (appartamento/villa/commerciale), MQ ({mq if mq else 'da estrarre'}), N. VANI, PIANO, CONDIZIONI (ristrutturato/da ristrutturare), STATO OCCUPAZIONE
2. Usa il web_search per cercare prezzi al m² nella zona "{comune} ({provincia})":
   - "prezzo medio mq {comune}"
   - "site:immobiliare.it {comune} appartamento vendita"
   - "site:idealista.it {comune} vendita"
3. Trova prezzo medio €/m² per quella zona/tipologia (anche da quotazioni OMI Agenzia Entrate)
4. Calcola valore di mercato stimato = mq × prezzo medio €/m²
5. Confronta con prezzo asta € {prezzo:,.0f}
6. Stima eventuali costi di ristrutturazione se la descrizione menziona "da ristrutturare", "ristrutturare", danni
7. Considera: occupazione, quota indivisa, lite pendente sono fattori di rischio

RISPONDI ESCLUSIVAMENTE CON JSON (no testo, no markdown):
{{
  "tipo_analisi": "immobile",
  "caratteristiche": {{"tipologia":"...","mq":numero,"vani":"...","piano":"...","condizioni":"...","occupazione":"libero|occupato|sconosciuto"}},
  "prezzo_mercato": {{"euro_mq_min":numero,"euro_mq_medio":numero,"euro_mq_max":numero,"valore_stimato":numero,"fonti":["immobiliare.it","idealista","OMI"]}},
  "convenienza_percentuale": numero (negativo se asta sotto valore mercato),
  "verdetto": "OTTIMO" | "BUONO" | "MEDIO" | "MEDIOCRE" | "PESSIMO",
  "punteggio": numero 0-100,
  "costi_ristrutturazione_stimati": numero,
  "motivazione": "stringa max 250 caratteri",
  "rischi": ["...", "..."],
  "raccomandazione": "stringa max 200 caratteri"
}}"""
        return prompt, "immobile"

    else:  # arte, macchinari, ecc.
        prompt = base_context + f"""
Sei un esperto di mercato dell'usato/collezionismo italiano. Analizza questo lotto giudiziario.

OBIETTIVO:
1. Identifica esattamente cosa è il lotto dalla descrizione
2. Usa il web_search per cercare prezzi di oggetti/beni simili su EBAY, SUBITO.IT, VINTED, CATAWIKI
3. Trova un range di prezzo di mercato
4. Confronta con prezzo asta € {prezzo:,.0f}
5. Considera che beni mobili all'asta sono "as-is" senza garanzia

RISPONDI ESCLUSIVAMENTE CON JSON (no testo, no markdown):
{{
  "tipo_analisi": "altro",
  "caratteristiche": {{"descrizione_breve":"...","categoria":"..."}},
  "prezzo_mercato": {{"min":numero,"media":numero,"max":numero,"fonti":["ebay","subito.it"]}},
  "convenienza_percentuale": numero,
  "verdetto": "OTTIMO" | "BUONO" | "MEDIO" | "MEDIOCRE" | "PESSIMO",
  "punteggio": numero 0-100,
  "motivazione": "stringa max 250 caratteri",
  "rischi": ["..."],
  "raccomandazione": "stringa max 200 caratteri"
}}"""
        return prompt, "altro"


def _call_anthropic_with_search(prompt: str, api_key: str) -> dict:
    """Chiama Claude API con tool web_search e estrae il JSON dalla risposta."""
    import httpx
    payload = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
        if r.status_code >= 400:
            logger.error("Anthropic API error %d: %s", r.status_code, r.text[:500])
            raise RuntimeError(f"Anthropic HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()

    # Estrai il testo finale (l'ultimo blocco di tipo "text")
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")  # prendi l'ultimo

    # Estrai il JSON dal testo
    import re
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"JSON non trovato nella risposta Claude: {text[:300]}")
    return json.loads(m.group(0))


@app.route("/api/analisi-mercato/<int:asta_id>", methods=["POST"])
@require_auth
def analisi_mercato(asta_id):
    """Analisi mercato incrociata via Claude + web_search.
    Cerca su autoscout24/subito/immobiliare/idealista e dà verdetto strutturato."""

    # Force refresh con ?refresh=1
    refresh = request.args.get("refresh") in ("1", "true")

    # Cache hit?
    with _market_cache_lock:
        cached = _market_cache.get(asta_id)
    if cached and not refresh:
        cached_at = cached.get("_cached_at")
        # Cache valida per 24h
        if cached_at and (datetime.now() - datetime.fromisoformat(cached_at)).total_seconds() < 86400:
            return jsonify({**cached, "from_cache": True})

    # Recupera lotto
    data = _load_results()
    asta = next((a for a in data.get("aste", []) if a.get("id") == asta_id), None)
    if not asta:
        return jsonify({"error": f"Lotto {asta_id} non trovato"}), 404

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({
            "error": "ANTHROPIC_API_KEY non configurata sul server",
            "fix": "Aggiungi ANTHROPIC_API_KEY=sk-ant-... in /etc/aste-giudiziarie/.env e riavvia pm2"
        }), 500

    try:
        prompt, tipo_analisi = _build_market_prompt(asta)
        logger.info("Analisi mercato per lotto %d (tipo: %s)", asta_id, tipo_analisi)
        result = _call_anthropic_with_search(prompt, api_key)
        result["_cached_at"] = datetime.now().isoformat()
        result["_lotto_id"] = asta_id
        with _market_cache_lock:
            _market_cache[asta_id] = result
        return jsonify({**result, "from_cache": False})
    except Exception as e:
        logger.error("Analisi mercato fallita per lotto %d: %s", asta_id, e)
        return jsonify({"error": f"Analisi fallita: {str(e)[:300]}"}), 500


# ---------------------------------------------------------------------------
# Endpoint: lancia scraping
# ---------------------------------------------------------------------------

@app.route("/api/scrape", methods=["GET", "POST"])
@require_auth
def trigger_scrape():
    """
    Avvia lo scraper in background.

    Query params (GET) o JSON body (POST):
      regione    — singola regione (opzionale; default: tutte)
      regioni    — lista separata da virgola (es. Campania,Lazio)
      prezzoMax  — prezzo massimo intero
    """
    global _scrape_running

    if _scrape_running:
        return jsonify({
            "status": "already_running",
            "message": "Scraping già in corso. Riprova più tardi.",
            "scraper_status": _scrape_status,
        }), 409

    # Leggi parametri
    if request.method == "POST" and request.is_json:
        body = request.get_json()
    else:
        body = {}

    regione_param = request.args.get("regione") or body.get("regione")
    regioni_param = request.args.get("regioni") or body.get("regioni")
    prezzo_max_raw = request.args.get("prezzoMax") or body.get("prezzoMax")

    regioni: Optional[list[str]] = None
    if regioni_param:
        regioni = [r.strip() for r in regioni_param.split(",") if r.strip()]
    elif regione_param:
        regioni = [regione_param.strip()]

    try:
        prezzo_max: Optional[int] = int(prezzo_max_raw) if prezzo_max_raw else None
    except (ValueError, TypeError):
        return jsonify({"error": "prezzoMax deve essere un intero"}), 400

    if not _scrape_lock.acquire(blocking=False):
        return jsonify({
            "status": "already_running",
            "message": "Scraping già in corso.",
        }), 409

    t = threading.Thread(
        target=_do_scrape,
        args=(regioni, prezzo_max),
        daemon=True,
    )
    t.start()

    return jsonify({
        "status": "started",
        "message": "Scraping avviato in background.",
        "parametri": {
            "regioni": regioni or "tutte (20)",
            "prezzoMax": prezzo_max,
        },
    }), 202


# ---------------------------------------------------------------------------
# Scheduler notturno
# ---------------------------------------------------------------------------

def _scheduled_scrape() -> None:
    """Job schedulato: scraping completo di tutte le regioni alle 02:00."""
    logger.info("Scraping notturno schedulato avviato (02:00).")
    if _scrape_lock.acquire(blocking=False):
        _do_scrape(regioni=None, prezzo_max=None)
    else:
        logger.warning("Scraping notturno saltato: scraping già in corso.")


def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Europe/Rome")
    scheduler.add_job(
        _scheduled_scrape,
        CronTrigger(hour=2, minute=0),
        id="nightly_scrape",
        name="Scraping notturno pvp.giustizia.it",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler avviato — scraping notturno ogni giorno alle 02:00 (Europe/Rome).")
    return scheduler


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(exc):
    return jsonify({"error": "Not found", "detail": str(exc)}), 404


@app.errorhandler(500)
def internal_error(exc):
    return jsonify({"error": "Internal server error", "detail": str(exc)}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Server Flask — aste pvp.giustizia.it")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="Porta (default: 5000)")
    parser.add_argument("--no-scheduler", action="store_true", help="Disabilita scheduler notturno")
    parser.add_argument("--scrape-on-start", action="store_true", help="Avvia scraping all'avvio")
    parser.add_argument("--hash-password", metavar="PWD",
                        help="Genera hash bcrypt per ADMIN_PASSWORD_HASH e esce")
    args = parser.parse_args()

    if args.hash_password:
        if not _HAS_BCRYPT:
            print("ERRORE: bcrypt non installato. Esegui: pip install bcrypt", file=sys.stderr)
            sys.exit(1)
        h = bcrypt.hashpw(args.hash_password.encode("utf-8"), bcrypt.gensalt(rounds=12))
        print(h.decode("utf-8"))
        sys.exit(0)

    if not args.no_scheduler:
        scheduler = _start_scheduler()

    if args.scrape_on_start:
        logger.info("--scrape-on-start: avvio scraping iniziale...")
        if _scrape_lock.acquire(blocking=False):
            t = threading.Thread(target=_do_scrape, args=(None, None), daemon=True)
            t.start()

    logger.info("Server in ascolto su http://%s:%d", args.host, args.port)
    logger.info("Endpoint disponibili:")
    logger.info("  GET  /api/ping")
    logger.info("  GET  /api/status")
    logger.info("  GET  /api/aste?regione=Campania&prezzoMax=10000")
    logger.info("  GET  /api/aste/<id>")
    logger.info("  GET  /api/scrape?regione=Campania&prezzoMax=10000")

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
