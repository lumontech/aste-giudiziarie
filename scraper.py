"""
Scraper per pvp.giustizia.it — Portale delle Vendite Pubbliche
Usa Playwright headless per navigare, filtrare e raccogliere dati sulle aste giudiziarie.
"""

import asyncio
import json
import os
import shutil
import re
import logging
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Page

# ---------------------------------------------------------------------------
# Configurazione logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
BASE_URL = "https://pvp.giustizia.it/pvp/it/search.page"
RESULTS_FILE = os.environ.get("RESULTS_FILE", "results.json")
BACKUP_FILE = os.environ.get("BACKUP_FILE", "results_backup.json")
MAX_RETRIES = 3
NAV_TIMEOUT = 60_000   # 60 s per navigazione
PAGE_TIMEOUT = 45_000  # 45 s per operazioni su pagina
WAIT_BETWEEN_PAGES = 2  # secondi di pausa tra pagine consecutive

REGIONI_ITALIANE = [
    "Abruzzo",
    "Basilicata",
    "Calabria",
    "Campania",
    "Emilia-Romagna",
    "Friuli-Venezia Giulia",
    "Lazio",
    "Liguria",
    "Lombardia",
    "Marche",
    "Molise",
    "Piemonte",
    "Puglia",
    "Sardegna",
    "Sicilia",
    "Toscana",
    "Trentino-Alto Adige",
    "Umbria",
    "Valle d'Aosta",
    "Veneto",
]

# Mappa regione → codice usato dai parametri URL del portale
REGIONE_CODICE: dict[str, str] = {
    "Abruzzo": "abruzzo",
    "Basilicata": "basilicata",
    "Calabria": "calabria",
    "Campania": "campania",
    "Emilia-Romagna": "emilia-romagna",
    "Friuli-Venezia Giulia": "friuli-venezia-giulia",
    "Lazio": "lazio",
    "Liguria": "liguria",
    "Lombardia": "lombardia",
    "Marche": "marche",
    "Molise": "molise",
    "Piemonte": "piemonte",
    "Puglia": "puglia",
    "Sardegna": "sardegna",
    "Sicilia": "sicilia",
    "Toscana": "toscana",
    "Trentino-Alto Adige": "trentino-alto-adige",
    "Umbria": "umbria",
    "Valle d'Aosta": "valle-d-aosta",
    "Veneto": "veneto",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def _parse_price(raw: str) -> Optional[float]:
    """Converte stringhe tipo '€ 45.000,00' → 45000.0"""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.]", "", raw).replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_url(regione: str, prezzo_max: Optional[int], page: int = 1) -> str:
    """Costruisce l'URL di ricerca con parametri GET."""
    cod = REGIONE_CODICE.get(regione, regione.lower())
    params = [
        f"r=it/{cod}",
        "tipoAnnuncio=025",          # immobili
        "numElementiPagina=50",
    ]
    if prezzo_max:
        params.append(f"prezzoMax={prezzo_max}")
    if page > 1:
        params.append(f"pagina={page}")
    return f"{BASE_URL}?" + "&".join(params)


async def _safe_text(locator) -> str:
    try:
        return _clean(await locator.inner_text())
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Estrazione di un singolo annuncio dalla pagina di dettaglio
# ---------------------------------------------------------------------------

async def _extract_detail(page: Page, url: str) -> dict:
    """Carica la pagina di dettaglio e raccoglie i campi estesi."""
    detail: dict = {}
    for attempt in range(MAX_RETRIES):
        try:
            await page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)
            # Estrai tutti i dt/dd presenti nella scheda di dettaglio
            pairs = await page.evaluate("""() => {
                const result = {};
                const rows = document.querySelectorAll('dl dt, dl dd, .campo-label, .campo-valore, th, td');
                let lastKey = null;
                rows.forEach(el => {
                    const tag = el.tagName.toLowerCase();
                    const text = el.innerText.trim();
                    if (tag === 'dt' || tag === 'th' || el.classList.contains('campo-label')) {
                        lastKey = text;
                    } else if ((tag === 'dd' || tag === 'td' || el.classList.contains('campo-valore')) && lastKey) {
                        result[lastKey] = text;
                        lastKey = null;
                    }
                });
                return result;
            }""")
            detail.update(pairs)
            break
        except Exception as exc:
            if attempt < MAX_RETRIES - 1:
                logger.warning("  Retry dettaglio %s (tentativo %d): %s", url, attempt + 1, exc)
                await asyncio.sleep(3 * (attempt + 1))
            else:
                logger.error("  Fallito caricamento dettaglio: %s — %s", url, exc)
    return detail


# ---------------------------------------------------------------------------
# Estrazione aste dalla lista risultati
# ---------------------------------------------------------------------------

async def _parse_listing_page(page: Page) -> list[dict]:
    """Estrae tutti i lotti visibili nella pagina di lista corrente."""
    aste: list[dict] = []
    try:
        await page.wait_for_selector(
            ".search-result-item, .annuncio-item, article.lotto, .risultato-ricerca, .aste-list-item",
            timeout=PAGE_TIMEOUT,
        )
    except PlaywrightTimeoutError:
        # Prova selettore generico
        pass

    items = await page.query_selector_all(
        ".search-result-item, .annuncio-item, article.lotto, .risultato-ricerca, .aste-list-item, .media-body"
    )

    if not items:
        # Fallback: leggi tutta la struttura e cerca blocchi aste
        items = await page.query_selector_all("ul.list-group > li, .row.annuncio, div[data-annuncio]")

    for item in items:
        try:
            ast: dict = {}

            # Titolo
            for sel in ["h2", "h3", ".titolo", ".title", ".lotto-titolo", "a.titolo-link"]:
                el = await item.query_selector(sel)
                if el:
                    ast["titolo"] = _clean(await el.inner_text())
                    break

            # Link diretto
            link_el = await item.query_selector("a[href]")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    ast["link"] = href if href.startswith("http") else "https://pvp.giustizia.it" + href

            # Prezzo base
            for sel in [".prezzo-base", ".prezzo", ".price", "[data-prezzo]", ".importo"]:
                el = await item.query_selector(sel)
                if el:
                    txt = _clean(await el.inner_text())
                    ast["prezzo_base_raw"] = txt
                    ast["prezzo_base"] = _parse_price(txt)
                    break

            # Tribunale / Comune / Provincia / Regione
            for label_sel in [".tribunal", ".comune-label", ".luogo", ".ubicazione", "span.tribunale"]:
                el = await item.query_selector(label_sel)
                if el:
                    ast["tribunale"] = _clean(await el.inner_text())
                    break

            # Estrai coppie chiave-valore generiche dall'elemento
            pairs_raw: dict = await item.evaluate("""el => {
                const out = {};
                const dts = el.querySelectorAll('dt');
                dts.forEach(dt => {
                    const dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD') {
                        out[dt.innerText.trim()] = dd.innerText.trim();
                    }
                });
                // Anche label+span
                const spans = el.querySelectorAll('.label, .campo-nome, .field-label');
                spans.forEach(sp => {
                    const val = sp.nextElementSibling;
                    if (val) out[sp.innerText.trim()] = val.innerText.trim();
                });
                return out;
            }""")

            # Normalizza campi comuni
            for k, v in pairs_raw.items():
                kl = k.lower()
                if "tribunale" in kl:
                    ast.setdefault("tribunale", v)
                elif "comune" in kl:
                    ast.setdefault("comune", v)
                elif "provincia" in kl:
                    ast.setdefault("provincia", v)
                elif "regione" in kl:
                    ast.setdefault("regione", v)
                elif "tipologia" in kl or "tipo" in kl:
                    ast.setdefault("tipologia", v)
                elif "data" in kl and ("vendita" in kl or "asta" in kl or "udienza" in kl):
                    ast.setdefault("data_vendita", v)
                elif "occupazione" in kl or "stato occup" in kl:
                    ast.setdefault("stato_occupazione", v)
                elif "offerta" in kl and "minima" in kl:
                    ast.setdefault("offerta_minima_raw", v)
                    ast.setdefault("offerta_minima", _parse_price(v))
                elif "prezzo" in kl and "base" in kl and "prezzo_base" not in ast:
                    ast["prezzo_base_raw"] = v
                    ast["prezzo_base"] = _parse_price(v)

            # Data vendita da testo libero
            if not ast.get("data_vendita"):
                raw_text = _clean(await item.inner_text())
                m = re.search(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b", raw_text)
                if m:
                    ast["data_vendita"] = m.group(1)

            if ast.get("titolo") or ast.get("link"):
                ast.setdefault("titolo", "N/D")
                ast.setdefault("prezzo_base", None)
                ast.setdefault("offerta_minima", None)
                ast.setdefault("tribunale", "")
                ast.setdefault("comune", "")
                ast.setdefault("provincia", "")
                ast.setdefault("regione", "")
                ast.setdefault("tipologia", "")
                ast.setdefault("data_vendita", "")
                ast.setdefault("stato_occupazione", "")
                ast.setdefault("link", "")
                aste.append(ast)

        except Exception as exc:
            logger.debug("  Errore parsing elemento: %s", exc)
            continue

    return aste


async def _get_total_pages(page: Page) -> int:
    """Determina il numero totale di pagine dalla paginazione."""
    try:
        # Cerca l'ultima pagina nella navigazione
        last_page_el = await page.query_selector(
            "a[aria-label='Ultima pagina'], .pagination li:last-child a, "
            ".paginator .last, nav.pagination span.last, [data-pagina-totale]"
        )
        if last_page_el:
            txt = _clean(await last_page_el.inner_text())
            n = re.search(r"\d+", txt)
            if n:
                return int(n.group())

        # Cerca elementi di paginazione e prendi il numero massimo
        pag_els = await page.query_selector_all(".pagination a, .paginator a, nav.pagination a")
        nums: list[int] = []
        for el in pag_els:
            txt = _clean(await el.inner_text())
            m = re.search(r"^\d+$", txt)
            if m:
                nums.append(int(m.group()))
        if nums:
            return max(nums)

        # Cerca testo tipo "Pagina 1 di 5"
        page_text = await page.content()
        m = re.search(r"[Pp]agina\s+\d+\s+di\s+(\d+)", page_text)
        if m:
            return int(m.group(1))

        # Cerca attributo data-* che indica totale pagine
        el = await page.query_selector("[data-tot-pagine], [data-total-pages]")
        if el:
            for attr in ["data-tot-pagine", "data-total-pages"]:
                val = await el.get_attribute(attr)
                if val and val.isdigit():
                    return int(val)

    except Exception:
        pass
    return 1


async def _no_results(page: Page) -> bool:
    """Verifica se la pagina non contiene risultati."""
    try:
        content = await page.content()
        nessun_result = [
            "nessun risultato",
            "nessuna asta",
            "non sono stati trovati",
            "0 risultati",
            "no results",
        ]
        lc = content.lower()
        return any(phrase in lc for phrase in nessun_result)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scraping di una singola regione
# ---------------------------------------------------------------------------

async def scrape_regione(
    page: Page,
    regione: str,
    prezzo_max: Optional[int] = None,
) -> list[dict]:
    """Raccoglie tutte le aste di una regione, gestendo la paginazione."""
    all_aste: list[dict] = []
    current_page = 1

    while True:
        url = _build_url(regione, prezzo_max, current_page)
        success = False

        for attempt in range(MAX_RETRIES):
            try:
                await page.goto(url, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)  # lascia caricare JS

                # Gestisci eventuale cookie banner
                for cookie_sel in [
                    "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
                    "button.accept-cookies",
                    "#acceptCookies",
                    "a.accept-all",
                    "button[data-action='accept']",
                ]:
                    try:
                        btn = await page.query_selector(cookie_sel)
                        if btn:
                            await btn.click()
                            await page.wait_for_timeout(500)
                    except Exception:
                        pass

                success = True
                break

            except PlaywrightTimeoutError:
                logger.warning(
                    "  %s pag.%d — timeout (tentativo %d/%d)",
                    regione, current_page, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(5 * (attempt + 1))
            except Exception as exc:
                logger.warning(
                    "  %s pag.%d — errore: %s (tentativo %d/%d)",
                    regione, current_page, exc, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(3 * (attempt + 1))

        if not success:
            logger.error("  %s pag.%d — saltata dopo %d tentativi falliti", regione, current_page, MAX_RETRIES)
            break

        if await _no_results(page):
            logger.info("  %s — nessun risultato trovato", regione)
            break

        # Prima pagina: determina totale pagine
        if current_page == 1:
            total_pages = await _get_total_pages(page)
            logger.info("  %s — %d pagine da scaricare", regione, total_pages)

        # Estrai aste dalla pagina corrente
        page_aste = await _parse_listing_page(page)

        # Aggiungi regione se non già presente
        for a in page_aste:
            if not a.get("regione"):
                a["regione"] = regione

        all_aste.extend(page_aste)

        logger.debug("  %s pag.%d — %d aste estratte", regione, current_page, len(page_aste))

        if current_page >= total_pages or not page_aste:
            break

        current_page += 1
        await asyncio.sleep(WAIT_BETWEEN_PAGES)

    return all_aste


# ---------------------------------------------------------------------------
# Scraping completo di tutte le regioni
# ---------------------------------------------------------------------------

async def run_scraper(
    regioni: Optional[list[str]] = None,
    prezzo_max: Optional[int] = None,
) -> dict:
    """
    Esegue lo scraping per tutte le regioni (o quelle indicate) e restituisce
    un dizionario con i risultati, timestamp e statistiche.
    """
    if regioni is None:
        regioni = REGIONI_ITALIANE

    all_results: list[dict] = []
    per_regione: dict[str, int] = {}
    start_ts = datetime.now()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
        )
        # Blocca risorse pesanti per velocizzare
        await context.route(
            "**/*.{png,jpg,jpeg,gif,svg,mp4,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )
        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        for idx, regione in enumerate(regioni, 1):
            logger.info(
                "[%d/%d] Scraping %s (prezzoMax=%s)...",
                idx, len(regioni), regione,
                f"€{prezzo_max:,}" if prezzo_max else "nessuno",
            )
            try:
                aste = await scrape_regione(page, regione, prezzo_max)
                per_regione[regione] = len(aste)
                all_results.extend(aste)
                logger.info("  ✓ %s: %d aste trovate", regione, len(aste))
            except Exception as exc:
                logger.error("  ✗ %s: errore imprevisto — %s", regione, exc)
                per_regione[regione] = 0

        await browser.close()

    # Aggiungi ID univoco a ogni asta
    for i, ast in enumerate(all_results):
        ast["id"] = i + 1

    end_ts = datetime.now()
    result_payload = {
        "timestamp_aggiornamento": end_ts.isoformat(),
        "durata_secondi": round((end_ts - start_ts).total_seconds(), 1),
        "totale_aste": len(all_results),
        "aste_per_regione": per_regione,
        "filtro_prezzo_max": prezzo_max,
        "aste": all_results,
    }
    return result_payload


# ---------------------------------------------------------------------------
# Salvataggio su disco
# ---------------------------------------------------------------------------

def save_results(payload: dict) -> None:
    """Salva il backup del file esistente, poi scrive il nuovo."""
    if os.path.exists(RESULTS_FILE):
        shutil.copy2(RESULTS_FILE, BACKUP_FILE)
        logger.info("Backup salvato in %s", BACKUP_FILE)

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info(
        "Risultati salvati: %d aste totali in %s",
        payload["totale_aste"],
        RESULTS_FILE,
    )


# ---------------------------------------------------------------------------
# Entry point standalone
# ---------------------------------------------------------------------------

async def main(regioni: Optional[list[str]] = None, prezzo_max: Optional[int] = None) -> dict:
    logger.info("=" * 60)
    logger.info("Avvio scraping pvp.giustizia.it")
    logger.info("Regioni: %s", regioni or "tutte (20)")
    logger.info("Prezzo max: %s", f"€{prezzo_max:,}" if prezzo_max else "nessuno")
    logger.info("=" * 60)

    payload = await run_scraper(regioni=regioni, prezzo_max=prezzo_max)
    save_results(payload)

    logger.info("=" * 60)
    logger.info("Scraping completato in %.1f s", payload["durata_secondi"])
    logger.info("Totale aste: %d", payload["totale_aste"])
    for reg, cnt in payload["aste_per_regione"].items():
        logger.info("  %-30s %d", reg + ":", cnt)
    logger.info("=" * 60)

    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper pvp.giustizia.it")
    parser.add_argument(
        "--regione",
        nargs="*",
        help="Regione/i da scrapare (default: tutte le 20)",
        default=None,
    )
    parser.add_argument(
        "--prezzoMax",
        type=int,
        default=None,
        help="Prezzo massimo in euro",
    )
    args = parser.parse_args()

    asyncio.run(main(regioni=args.regione, prezzo_max=args.prezzoMax))
