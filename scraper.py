"""
Scraper aste giudiziarie italiane via Apify Actor

Usa l'actor "ayrtondavoli97/italy-judicial-real-estate-auctions-astegiudiziarie-it"
che estrae dati strutturati da astegiudiziarie.it (~14.900 aste attive nazionali).

API: POST https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token=...
Body filtri: maxItems, priceMin, priceMax, regione, provincia, tribunale, propertyType
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
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

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
APIFY_ACTOR = os.environ.get(
    "APIFY_ACTOR",
    "ayrtondavoli97~italy-judicial-real-estate-auctions-astegiudiziarie-it",
)
APIFY_BASE = "https://api.apify.com/v2"
APIFY_TIMEOUT = float(os.environ.get("APIFY_TIMEOUT", "240"))  # secondi
APIFY_MAX_ITEMS = int(os.environ.get("APIFY_MAX_ITEMS", "300"))  # per regione

# Tutte le regioni italiane
REGIONI_ITALIANE = [
    "Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia-Romagna",
    "Friuli-Venezia Giulia", "Lazio", "Liguria", "Lombardia", "Marche",
    "Molise", "Piemonte", "Puglia", "Sardegna", "Sicilia",
    "Toscana", "Trentino-Alto Adige", "Umbria", "Valle d'Aosta", "Veneto",
]

# Back-compat: alcune parti del codice importano REGIONE_CODICE
REGIONE_CODICE: dict[str, str] = {r: r.lower().replace(" ", "-") for r in REGIONI_ITALIANE}


# ---------------------------------------------------------------------------
# Normalizzazione output Apify → modello dashboard
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    if v in (None, "", "—"):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _normalize(raw: dict, idx: int) -> dict:
    """Adatta un record Apify al formato atteso da dashboard.html."""
    prezzo_base = _safe_float(raw.get("prezzo_base") or raw.get("priceBase"))
    offerta_min = _safe_float(raw.get("offerta_minima") or raw.get("minOffer"))

    return {
        "id": raw.get("id") or raw.get("idLotto") or idx,
        "titolo": (
            raw.get("titolo")
            or raw.get("descrizione_breve")
            or raw.get("title")
            or f"Lotto #{idx}"
        ),
        "tipologia": raw.get("tipologia") or raw.get("propertyType") or "",
        "prezzo_base": prezzo_base,
        "prezzo_base_raw": f"€ {prezzo_base:,.0f}" if prezzo_base else None,
        "offerta_minima": offerta_min,
        "offerta_minima_raw": f"€ {offerta_min:,.0f}" if offerta_min else None,
        "tribunale": raw.get("tribunale") or "",
        "comune": raw.get("comune") or "",
        "provincia": raw.get("provincia") or "",
        "regione": raw.get("regione") or "",
        "data_vendita": raw.get("data_udienza") or raw.get("data_vendita") or "",
        "stato_occupazione": raw.get("stato_occupazione") or "",
        "link": raw.get("url") or raw.get("link") or "",
        # campi aggiuntivi propri di Apify (utili a dashboard.html)
        "mq_stimati": raw.get("mq_stimati"),
        "prezzo_mq": raw.get("prezzo_mq"),
        "days_to_auction": raw.get("days_to_auction"),
        "investment_score": raw.get("investment_score"),
        "is_opportunity": raw.get("is_opportunity"),
        "rank_reason": raw.get("rank_reason"),
        "has_foto": raw.get("has_foto"),
        "has_planimetrie": raw.get("has_planimetrie"),
        # raw per debug
        "_raw": {k: v for k, v in raw.items() if k not in ("html", "raw_html")},
    }


# ---------------------------------------------------------------------------
# Chiamata Apify
# ---------------------------------------------------------------------------

async def _call_apify(
    client: httpx.AsyncClient,
    *,
    regione: Optional[str] = None,
    provincia: Optional[str] = None,
    tribunale: Optional[str] = None,
    property_type: Optional[str] = None,
    prezzo_min: Optional[float] = None,
    prezzo_max: Optional[float] = None,
    max_items: int = APIFY_MAX_ITEMS,
) -> list[dict]:
    """Chiamata sincrona all'actor Apify — ritorna l'array di items."""
    if not APIFY_TOKEN:
        raise RuntimeError(
            "APIFY_TOKEN mancante. Imposta in /etc/aste-giudiziarie/.env "
            "(token da https://console.apify.com/account/integrations)"
        )

    url = f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
    body: dict = {"maxItems": int(max_items)}
    if regione: body["regione"] = regione
    if provincia: body["provincia"] = provincia
    if tribunale: body["tribunale"] = tribunale
    if property_type: body["propertyType"] = property_type
    if prezzo_min is not None: body["priceMin"] = float(prezzo_min)
    if prezzo_max is not None: body["priceMax"] = float(prezzo_max)

    logger.info("  → Apify: %s", {k: v for k, v in body.items()})
    resp = await client.post(url, params={"token": APIFY_TOKEN}, json=body, timeout=APIFY_TIMEOUT)
    if resp.status_code >= 400:
        logger.error("  ← HTTP %d: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        logger.warning("  ← Risposta non-array: %s", type(data).__name__)
        return []
    return data


# ---------------------------------------------------------------------------
# Main: scraping multi-regione + salvataggio
# ---------------------------------------------------------------------------

async def main(
    regioni: Optional[list[str]] = None,
    prezzo_max: Optional[int] = None,
    prezzo_min: Optional[int] = None,
    tipologia: Optional[str] = None,
) -> dict:
    """
    Scarica aste per le regioni richieste e salva in RESULTS_FILE.
    Se regioni è None, fa scrape nazionale (singola chiamata Apify, più efficiente).
    """
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("Avvio scraping (Apify) astegiudiziarie.it")
    logger.info("Regioni: %s", regioni or "tutte (nazionale singolo)")
    if prezzo_max: logger.info("Prezzo max: €%s", f"{prezzo_max:,}")
    if prezzo_min: logger.info("Prezzo min: €%s", f"{prezzo_min:,}")
    if tipologia: logger.info("Tipologia: %s", tipologia)
    logger.info("=" * 60)

    aste_all: list[dict] = []
    aste_per_regione: dict[str, int] = {}
    errori: dict[str, str] = {}

    async with httpx.AsyncClient() as client:
        if regioni:
            # Una chiamata per regione (filtraggio server-side)
            for i, reg in enumerate(regioni, 1):
                logger.info("[%d/%d] %s", i, len(regioni), reg)
                try:
                    items = await _call_apify(
                        client,
                        regione=reg,
                        prezzo_min=prezzo_min,
                        prezzo_max=prezzo_max,
                        property_type=tipologia,
                    )
                    norm = [_normalize(a, len(aste_all) + j) for j, a in enumerate(items)]
                    aste_all.extend(norm)
                    aste_per_regione[reg] = len(norm)
                    logger.info("  ✓ %s: %d aste", reg, len(norm))
                except Exception as e:
                    errori[reg] = str(e)
                    aste_per_regione[reg] = 0
                    logger.error("  ✗ %s: %s", reg, e)
        else:
            # Scrape nazionale singolo (1 sola chiamata Apify)
            logger.info("Scraping nazionale singolo (max %d items)", APIFY_MAX_ITEMS * 2)
            try:
                items = await _call_apify(
                    client,
                    prezzo_min=prezzo_min,
                    prezzo_max=prezzo_max,
                    property_type=tipologia,
                    max_items=APIFY_MAX_ITEMS * 5,  # nazionale → alza il limite
                )
                norm = [_normalize(a, j) for j, a in enumerate(items)]
                aste_all.extend(norm)
                # Raggruppa per regione
                for a in norm:
                    r = a.get("regione") or "Sconosciuta"
                    aste_per_regione[r] = aste_per_regione.get(r, 0) + 1
                logger.info("  ✓ Nazionale: %d aste", len(norm))
            except Exception as e:
                errori["__nazionale__"] = str(e)
                logger.error("  ✗ Nazionale: %s", e)

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
        "fonte": "apify:" + APIFY_ACTOR,
        "aste": aste_all,
    }

    # Backup
    if os.path.exists(RESULTS_FILE):
        try:
            shutil.copy2(RESULTS_FILE, BACKUP_FILE)
        except Exception as e:
            logger.warning("Backup fallito: %s", e)

    # Crea dir se non esiste
    os.makedirs(os.path.dirname(RESULTS_FILE) or ".", exist_ok=True)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("Completato in %.1f s — %d aste totali", duration, len(aste_all))
    for r, n in sorted(aste_per_regione.items(), key=lambda x: -x[1]):
        logger.info("  %-25s %d", r + ":", n)
    if errori:
        logger.warning("Errori per regione: %s", list(errori.keys()))
    logger.info("Salvato in %s", RESULTS_FILE)

    return out


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Scraper aste giudiziarie via Apify")
    p.add_argument("--regione", action="append", help="Regione (può ripetersi)")
    p.add_argument("--prezzo-max", type=int)
    p.add_argument("--prezzo-min", type=int)
    p.add_argument("--tipologia", help="Es: appartamento, villa, terreno")
    args = p.parse_args()
    asyncio.run(main(
        regioni=args.regione,
        prezzo_max=args.prezzo_max,
        prezzo_min=args.prezzo_min,
        tipologia=args.tipologia,
    ))
