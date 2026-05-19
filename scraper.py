"""
Scraper aste giudiziarie via API ufficiale webapi.astegiudiziarie.it

Flow:
  POST /api/Search/Map     → lista idLotto + lat/lng + prezzo base
  POST /api/Search/Data    → dettagli completi (a batch di N ID)

API pubblica, no autenticazione, no rate limit visibili. Stesso backend
usato dal frontend www.astegiudiziarie.it (~14.900 aste attive in Italia).
"""

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RESULTS_FILE = os.environ.get("RESULTS_FILE", "results.json")
BACKUP_FILE = os.environ.get("BACKUP_FILE", "results_backup.json")

API_BASE = os.environ.get("ASTE_API_BASE", "https://webapi.astegiudiziarie.it")
SITE_BASE = os.environ.get("ASTE_SITE_BASE", "https://www.astegiudiziarie.it")
API_TIMEOUT = float(os.environ.get("ASTE_API_TIMEOUT", "60"))
DATA_BATCH_SIZE = int(os.environ.get("ASTE_BATCH_SIZE", "20"))  # limite hard server: 20 per chiamata Search/Data

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Origin": SITE_BASE,
    "Referer": SITE_BASE + "/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# Tutte le regioni italiane (nome esatto richiesto dall'API)
REGIONI_ITALIANE = [
    "Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia-Romagna",
    "Friuli-Venezia Giulia", "Lazio", "Liguria", "Lombardia", "Marche",
    "Molise", "Piemonte", "Puglia", "Sardegna", "Sicilia",
    "Toscana", "Trentino-Alto Adige", "Umbria", "Valle d'Aosta", "Veneto",
]

# Back-compat
REGIONE_CODICE: dict[str, str] = {r: r.lower().replace(" ", "-") for r in REGIONI_ITALIANE}

# Mapping idTipologia → label leggibile (dal portale astegiudiziarie.it)
# genere=1 = immobiliari, genere=2 = mobili
TIPOLOGIA_LABELS = {
    1:  ("Immobile residenziale",                       "immobile"),
    2:  ("Immobile commerciale",                        "commerciale"),
    3:  ("Immobile industriale",                        "industriale"),
    4:  ("Impianto sportivo",                           "sportivo"),
    5:  ("Box / Garage / Posto auto / Terreno / Altro", "altro_immobile"),
    6:  ("Autoveicoli e cicli",                         "veicolo"),
    7:  ("Nautica",                                     "nautica"),
    8:  ("Macchinari, utensili, materie prime",         "macchinari"),
    9:  ("Informatica e elettronica",                   "informatica"),
    10: ("Arredamento, elettrodomestici",               "arredamento"),
    11: ("Arte, oreficeria, antiquariato",              "arte"),
    12: ("Abbigliamento e calzature",                   "abbigliamento"),
    13: ("Cancelleria, libri, riviste",                 "cancelleria"),
    14: ("Alimentari",                                  "alimentari"),
    15: ("Materiale sanitario",                         "sanitario"),
    16: ("Animali",                                     "animali"),
    17: ("Quote societarie e crediti",                  "quote"),
    18: ("Diritti, marchi, brevetti",                   "diritti"),
    19: ("Altra categoria mobiliare",                   "altro_mobile"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    if v in (None, "", "—"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _format_data(iso: Optional[str]) -> str:
    """ISO 2026-06-08T10:00:00 -> 08/06/2026."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00").split(".")[0])
        return d.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return str(iso)


def _days_until(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00").split(".")[0])
        now = datetime.now() if d.tzinfo is None else datetime.now(timezone.utc)
        return (d - now).days
    except (ValueError, TypeError):
        return None


def _full_url(rel: Optional[str]) -> str:
    if not rel:
        return ""
    if rel.startswith("http"):
        return rel
    return SITE_BASE + (rel if rel.startswith("/") else "/" + rel)


def _detect_occupazione(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\boccupat[oai]|locat[oai]\b|inquilin[oai]|abitat[oai]", t):
        return "occupato"
    if re.search(r"\b(libero|sgombero|disabitat[oai]|vuoto|sgomberat[oai])\b", t):
        return "libero"
    return ""


def _detect_quota_indivisa(text: str) -> bool:
    t = (text or "").lower()
    return bool(re.search(r"quota\s+indivis|comproprie|nuda\s+propriet|usufrutt", t))


# Regex pattern per estrarre metri quadri dalla descrizione
_MQ_PATTERNS = [
    r"\b(?:di|circa|complessiv[ai]|superficie|estensione|mq)\s*(?:di\s+)?(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:mq|m²|m2|m\.q\.|metri\s*quadr)",
    r"\b(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:mq|m²|m2|m\.q\.|metri\s*quadr)",
    r"\b(\d{1,5})\s*mq\b",
    r"\bsuperficie\s*(?:lord[ai]|util[ei]|comm)?\s*[:\s=di]+\s*(\d{1,5}(?:[.,]\d{1,2})?)",
]

def _extract_mq(text: str) -> Optional[float]:
    """Estrai i metri quadri dalla descrizione."""
    if not text:
        return None
    t = text.lower()
    for pattern in _MQ_PATTERNS:
        for m in re.finditer(pattern, t, re.I):
            try:
                val = m.group(1).replace(",", ".")
                # ignora valori troppo bassi o troppo alti (probabilmente non sono mq)
                v = float(val)
                if 5 <= v <= 50000:
                    return v
            except (ValueError, TypeError):
                continue
    return None


# ---------------------------------------------------------------------------
# Costruzione body Search/Map (schema esatto frontend)
# ---------------------------------------------------------------------------

def _build_search_params(
    regione: Optional[str] = None,
    provincia: Optional[str] = None,
    comune: Optional[str] = None,
    prezzo_min: Optional[float] = None,
    prezzo_max: Optional[float] = None,
    id_tipologie: Optional[list] = None,
    id_tribunale: Optional[int] = None,
    tipo_ricerca: int = 0,
) -> dict:
    return {
        "tipoRicerca": tipo_ricerca,
        "indirizzo": None,
        "latitudine": None, "longitudine": None,
        "latitudineNW": None, "longitudineNW": None,
        "latitudineSE": None, "longitudineSE": None,
        "noGeo": False,
        "idEsperimentoVendita": None,
        "idTipologie": id_tipologie or [],
        "tipologia": None,
        "idCategorie": [],
        "categoria": None,
        "descrizione": None,
        "comune": comune,
        "provincia": provincia,
        "regione": regione,
        "cap": None, "ricercaCap": None,
        "prezzoDa": prezzo_min,
        "prezzoA": prezzo_max,
        "priceMax": 0,
        "priceSteps": None,
        "tipologie": None,
        "idTribunale": id_tribunale,
        "tribunale": None,
        "numeroProcedura": None, "annoProcedura": None,
        "ruolo": None,
        "idTipologiaProcedura": None,
        "giudice": None, "professionista": None,
        "idTipologiaVendita": None, "idModalitaVendita": None,
        "idPubblicazione": None,
        "dataVenditaDa": None, "dataVenditaA": None,
        "codiceAsta": None,
        "hasFoto": None, "hasPlanimetrie": None,
        "hasVirtualTour": None, "hasVideo": None,
        "bandita": None, "telematica": None, "inScadenza": None,
        "lottoUnico": None, "venditeAGI": None,
        "storica": False, "vetrina": False,
        "sezione": None,
        "searchOnMap": False,
        "tribunali": None,
        "tipologieProcedura": None, "tipologieVendita": None,
        "modalitaVendita": None,
        "numeroPubblicazioni": None,
        "listaIdLotto": None,
        "idProcedura": None,
        "orderBy": 6,  # default: data di pubblicazione più recente
    }


# ---------------------------------------------------------------------------
# Investment score (algoritmo semplice locale, sostituisce Apify score)
# ---------------------------------------------------------------------------

def _calc_score(asta: dict) -> int:
    score = 50
    p = asta.get("prezzo_base")
    if p is not None:
        if p < 10000: score += 25
        elif p < 25000: score += 18
        elif p < 50000: score += 12
        elif p < 100000: score += 6
        elif p > 300000: score -= 10

    occ = asta.get("stato_occupazione")
    if occ == "libero": score += 20
    elif occ == "occupato": score -= 30

    if asta.get("_quota_indivisa"):
        score -= 40

    gg = asta.get("days_to_auction")
    if gg is not None:
        if gg < 0: score -= 25
        elif gg < 7: score += 12
        elif gg < 14: score += 8
        elif gg > 90: score -= 5

    if asta.get("hasFoto"): score += 3
    if asta.get("hasPlanimetrie"): score += 3

    # Bonus €/m² basso (come Apify "rank_reason: €/mq basso")
    pmq = asta.get("prezzo_mq")
    if pmq is not None:
        if pmq < 500: score += 15
        elif pmq < 1000: score += 10
        elif pmq < 1500: score += 5
        elif pmq > 4000: score -= 5

    return max(0, min(100, score))


def _calc_rank_reason(asta: dict, score: int) -> str:
    parts = []
    p = asta.get("prezzo_base") or 0
    pmq = asta.get("prezzo_mq")
    if pmq and pmq < 1000: parts.append("€/mq basso")
    if 0 < p < 25000: parts.append("prezzo basso")
    elif p < 50000: parts.append("prezzo accessibile")
    gg = asta.get("days_to_auction")
    if gg is not None and 0 <= gg < 30: parts.append("asta vicina")
    if asta.get("stato_occupazione") == "libero": parts.append("immobile libero")
    if not parts and score >= 70: parts.append("buona opportunità")
    return ", ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Normalizzazione record API → schema dashboard
# ---------------------------------------------------------------------------

def _normalize(raw: dict, idx: int, regione_default: Optional[str] = None) -> dict:
    prezzo_base = _safe_float(raw.get("prezzoBase"))
    descr = raw.get("descrizione") or ""
    categoria = (raw.get("categoria") or "").strip()
    titolo = categoria.title() if categoria else descr[:60]
    id_tipologia = raw.get("idTipologia")

    # Mapping idTipologia → label leggibile + categoria normalizzata per filtro dashboard
    tipologia_label, categoria_norm = TIPOLOGIA_LABELS.get(
        id_tipologia, ("Sconosciuta", "altro")
    )
    # È mobile (auto/nautica/ecc.)? — sopra a id=5 sono tutti mobili
    is_mobile = id_tipologia is not None and id_tipologia >= 6

    # Parsing mq + prezzo_mq dalla descrizione (solo per immobiliari)
    mq = None
    prezzo_mq = None
    if not is_mobile:
        mq = _extract_mq(descr)
        prezzo_mq = round(prezzo_base / mq, 2) if (mq and prezzo_base and mq > 0) else None

    semaforo = raw.get("semaforo") or {}
    stato = (semaforo.get("descrizione") or "").strip()

    occupazione = _detect_occupazione(descr)
    quota_indivisa = _detect_quota_indivisa(descr)
    days = _days_until(raw.get("dataUdienza") or raw.get("dataVendita") or raw.get("dataInizioGara"))

    norm = {
        "id": raw.get("idLotto") or idx,
        "id_asta": raw.get("idAsta"),
        "titolo": titolo,
        "descrizione": descr,
        "tipologia": raw.get("tipologia") or tipologia_label,
        "tipologia_label": tipologia_label,
        "tipologia_id": id_tipologia,
        "tipologia_filter": categoria_norm,
        "is_mobile": is_mobile,
        "categoria_api": categoria,
        "prezzo_base": prezzo_base,
        "prezzo_base_raw": f"€ {prezzo_base:,.0f}" if prezzo_base else None,
        "offerta_minima": None,  # API non lo espone direttamente
        "tribunale": raw.get("tribunale") or "",
        "comune": raw.get("comune") or "",
        "provincia": raw.get("provincia") or "",
        "regione": regione_default or "",
        "indirizzo": raw.get("indirizzo") or "",
        "data_vendita": _format_data(raw.get("dataUdienza") or raw.get("dataVendita") or raw.get("dataInizioGara")),
        "data_pubblicazione": _format_data(raw.get("dataInizioPubblicazione")),
        "data_fine_pubblicazione": _format_data(raw.get("dataFinePubblicazione")),
        "data_fine_cauzione": _format_data(raw.get("dataFineCauzione")),
        "stato_occupazione": occupazione,
        "stato": stato,
        "_quota_indivisa": quota_indivisa,
        "vendita_telematica": raw.get("venditaTelematica"),
        "link": _full_url(raw.get("urlSchedaDettagliata")),
        "image": _full_url(raw.get("urlPhoto")),
        "has_foto": raw.get("hasFoto"),
        "has_planimetrie": raw.get("hasPlanimetrie"),
        "has_virtual_tour": raw.get("hasVirtualTour"),
        "has_video": raw.get("hasMovie"),
        "ruolo": raw.get("ruolo") or "",
        "numero_procedura": raw.get("numeroProcedura"),
        "anno_procedura": raw.get("annoProcedura"),
        "latitudine": raw.get("latitudine"),
        "longitudine": raw.get("longitudine"),
        # mq parsati dalla descrizione (parità con Apify)
        "mq_stimati": mq,
        "prezzo_mq": prezzo_mq,
    }
    norm["days_to_auction"] = days
    norm["investment_score"] = _calc_score(norm)
    norm["rank_reason"] = _calc_rank_reason(norm, norm["investment_score"])
    norm["is_opportunity"] = norm["investment_score"] >= 70 and not quota_indivisa and occupazione != "occupato"
    return norm


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

async def _call_search_map(client: httpx.AsyncClient, body: dict) -> list[dict]:
    url = f"{API_BASE}/api/Search/Map"
    r = await client.post(url, json=body, headers=DEFAULT_HEADERS, timeout=API_TIMEOUT)
    if r.status_code >= 400:
        logger.error("Search/Map HTTP %d: %s", r.status_code, r.text[:300])
        r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


async def _call_search_data(client: httpx.AsyncClient, id_lotti: list[int]) -> list[dict]:
    if not id_lotti:
        return []
    url = f"{API_BASE}/api/Search/Data"
    r = await client.post(url, json=id_lotti, headers=DEFAULT_HEADERS, timeout=API_TIMEOUT)
    if r.status_code >= 400:
        logger.error("Search/Data HTTP %d: %s", r.status_code, r.text[:300])
        r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


async def _scrape_regione(
    client: httpx.AsyncClient,
    regione: str,
    *,
    prezzo_min: Optional[float] = None,
    prezzo_max: Optional[float] = None,
    id_tipologie: Optional[list] = None,
) -> list[dict]:
    """Per regione: 2 Search/Map (immobili + mobili) → Search/Data batched.

    Faccio 2 chiamate (tipoRicerca=1 immobili + tipoRicerca=2 mobili)
    perché tipoRicerca=0 a volte non include tutti i mobili.
    """
    # Step 1: ottieni IDs con 2 chiamate Search/Map (immobili + mobili)
    # NOTA: per tipoRicerca=2 (mobili) il server ha un bug — se idTipologie=[]
    # ritorna solo alcune categorie default. Bisogna passare ESPLICITAMENTE
    # tutti gli ID (6..25 copre tutti i tipi mobili attuali e futuri).
    ids: list[int] = []
    seen: set[int] = set()
    IDS_IMMOBILI = [1, 2, 3, 4, 5]
    IDS_MOBILI = list(range(6, 26))  # 6..25 per coprire eventuali nuove categorie

    for tr, default_ids in ((1, IDS_IMMOBILI), (2, IDS_MOBILI)):
        # Se l'utente ha filtrato per tipologia specifica, intersect con i default del tipo corrente
        if id_tipologie:
            tids = [t for t in id_tipologie if t in default_ids]
            if not tids:
                continue  # nessun match con questo tipoRicerca
        else:
            tids = default_ids

        body = _build_search_params(
            regione=regione,
            prezzo_min=prezzo_min,
            prezzo_max=prezzo_max,
            id_tipologie=tids,
            tipo_ricerca=tr,
        )
        map_results = await _call_search_map(client, body)
        for m in map_results:
            iid = m.get("idLotto")
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)

    if not ids:
        return []

    batches = [ids[i:i + DATA_BATCH_SIZE] for i in range(0, len(ids), DATA_BATCH_SIZE)]
    logger.info("  %s: %d ID totali (immobili+mobili) → %d batch da %d (parallel)",
                regione, len(ids), len(batches), DATA_BATCH_SIZE)

    # Step 2: dettagli batched in parallel
    PARALLEL = 5
    all_data: list[dict] = []
    for i in range(0, len(batches), PARALLEL):
        group = batches[i:i + PARALLEL]
        results = await asyncio.gather(
            *[_call_search_data(client, b) for b in group],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("    batch fallito: %s", r)
                continue
            all_data.extend(r)

    norm = [_normalize(d, idx=j, regione_default=regione) for j, d in enumerate(all_data)]
    return norm


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    regioni: Optional[list[str]] = None,
    prezzo_max: Optional[int] = None,
    prezzo_min: Optional[int] = None,
    tipologia: Optional[str] = None,  # back-compat
) -> dict:
    start = datetime.now()

    # Mappa tipologia stringa → id (idTipologie)
    id_tipologie = None
    if tipologia:
        TIPOLOGIA_MAP = {
            "residenziale": [1], "immobile": [1],
            "commerciale": [2], "industriale": [3],
            "sportivo": [4], "altro": [5],
        }
        id_tipologie = TIPOLOGIA_MAP.get(tipologia.lower())

    target = regioni if regioni else list(REGIONI_ITALIANE)

    logger.info("=" * 60)
    logger.info("Avvio scraping (webapi.astegiudiziarie.it)")
    logger.info("Regioni: %d (%s)", len(target), target[0] if len(target) == 1 else "tutte")
    if prezzo_max: logger.info("Prezzo max: €%s", f"{prezzo_max:,}")
    if prezzo_min: logger.info("Prezzo min: €%s", f"{prezzo_min:,}")
    if id_tipologie: logger.info("idTipologie: %s", id_tipologie)
    logger.info("=" * 60)

    aste_all: list[dict] = []
    aste_per_regione: dict[str, int] = {}
    errori: dict[str, str] = {}

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS) as client:
        for i, reg in enumerate(target, 1):
            logger.info("[%d/%d] %s", i, len(target), reg)
            try:
                norm = await _scrape_regione(
                    client, reg,
                    prezzo_min=prezzo_min,
                    prezzo_max=prezzo_max,
                    id_tipologie=id_tipologie,
                )
                aste_all.extend(norm)
                aste_per_regione[reg] = len(norm)
                logger.info("  ✓ %s: %d aste", reg, len(norm))
            except Exception as e:
                errori[reg] = str(e)
                aste_per_regione[reg] = 0
                logger.error("  ✗ %s: %s", reg, e)

    duration = (datetime.now() - start).total_seconds()

    out = {
        "timestamp_aggiornamento": datetime.now().isoformat(),
        "totale_aste": len(aste_all),
        "aste_per_regione": aste_per_regione,
        "filtro_prezzo_max": prezzo_max,
        "filtro_prezzo_min": prezzo_min,
        "filtro_tipologia": tipologia,
        "durata_secondi": round(duration, 1),
        "errori": errori,
        "fonte": "webapi.astegiudiziarie.it",
        "aste": aste_all,
    }

    # SAFETY: non sovrascrivere results.json se 0 aste + errori
    if len(aste_all) == 0 and errori:
        logger.error("=" * 60)
        logger.error("SCRAPE FALLITO — 0 aste + errori. results.json NON sovrascritto.")
        try:
            diag_path = os.path.join(os.path.dirname(RESULTS_FILE) or ".", "last_failure.json")
            with open(diag_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Diagnostico non salvato: %s", e)
        raise RuntimeError(f"Scrape fallito: {list(errori.keys())[:3]}")

    # Backup rotativo (solo se results corrente è valido)
    if os.path.exists(RESULTS_FILE):
        try:
            existing = json.load(open(RESULTS_FILE))
            if existing.get("totale_aste", 0) > 0:
                for n in (2, 1):
                    src = f"{BACKUP_FILE}.{n}"
                    dst = f"{BACKUP_FILE}.{n+1}"
                    if os.path.exists(src):
                        shutil.copy2(src, dst)
                if os.path.exists(BACKUP_FILE):
                    shutil.copy2(BACKUP_FILE, f"{BACKUP_FILE}.1")
                shutil.copy2(RESULTS_FILE, BACKUP_FILE)
                logger.info("Backup rotativo eseguito")
        except Exception as e:
            logger.warning("Backup fallito: %s", e)

    os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("Completato in %.1f s — %d aste totali", duration, len(aste_all))
    for r, n in sorted(aste_per_regione.items(), key=lambda x: -x[1]):
        if n > 0:
            logger.info("  %-25s %d", r + ":", n)
    if errori:
        logger.warning("Errori: %s", list(errori.keys()))
    logger.info("Salvato in %s", RESULTS_FILE)

    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--regione", action="append")
    p.add_argument("--prezzo-max", type=int)
    p.add_argument("--prezzo-min", type=int)
    p.add_argument("--tipologia")
    args = p.parse_args()
    asyncio.run(main(
        regioni=args.regione,
        prezzo_max=args.prezzo_max,
        prezzo_min=args.prezzo_min,
        tipologia=args.tipologia,
    ))
