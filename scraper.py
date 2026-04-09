#!/usr/bin/env python3
"""
Skyscanner Flight Price Tracker
WAW → LIS, wrzesień 2026, 2 osoby, loty bezpośrednie
Używa Playwright + stealth żeby ominąć wykrywanie bota
"""

import re
import time
import random
import logging
import datetime
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────────────────────────
#  KONFIGURACJA
# ─────────────────────────────────────────────────────────────────

GOOGLE_CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME        = "Loty Lizbona 2026"

DEPART_START = datetime.date(2026, 9, 7)
DEPART_END   = datetime.date(2026, 9, 17)

ADULTS          = 2
CABIN           = "economy"
STOPS           = "!oneStop,!twoPlusStops"
DEPARTURE_TIMES = "0-720,780-1439"

PAGE_LOAD_TIMEOUT = 60_000   # ms (Playwright używa milisekund)

# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def build_url(depart: datetime.date, ret: datetime.date) -> str:
    d = depart.strftime("%y%m%d")
    r = ret.strftime("%y%m%d")
    return (
        f"https://www.skyscanner.pl/transport/loty/wars/lis/{d}/{r}/"
        f"?adultsv2={ADULTS}&cabinclass={CABIN}&childrenv2=&ref=home&rtn=1"
        f"&outboundaltsenabled=false&inboundaltsenabled=false"
        f"&departure-times={DEPARTURE_TIMES}&stops={STOPS}"
    )


def human_delay(min_s: float = 2.0, max_s: float = 5.0):
    """Losowe opóźnienie imitujące człowieka."""
    time.sleep(random.uniform(min_s, max_s))


def fetch_page(page, url: str, label: str) -> str | None:
    """
    Otwiera URL w Playwright, czeka na wyniki, zwraca HTML.
    Wykonuje losowe przewijania i ruchy myszy żeby wyglądać jak człowiek.
    """
    try:
        log.info(f"  Otwieram: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

        # Krótka pauza po załadowaniu DOM
        human_delay(2, 4)

        title = page.title()
        log.info(f"  Tytuł strony: '{title}'")

        # Sprawdzamy czy to captcha / blokada
        content_lower = page.content().lower()
        if any(x in content_lower for x in ["captcha", "access denied", "robot", "cloudflare", "challenge"]):
            log.warning(f"  UWAGA: wygląda jak blokada bota!")
            page.screenshot(path=f"screenshot_{label}_blocked.png", full_page=True)

        # Symulujemy przewijanie strony — człowiek zazwyczaj scrolluje
        page.mouse.move(random.randint(300, 900), random.randint(200, 600))
        human_delay(0.5, 1.5)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
        human_delay(1, 2)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
        human_delay(0.5, 1.5)
        page.evaluate("window.scrollTo(0, 0)")

        # Czekamy na listę wyników lotów
        log.info(f"  Czekam na wyniki lotów...")
        page.wait_for_selector("ul#flights-results-list", timeout=PAGE_LOAD_TIMEOUT)
        log.info(f"  Lista wyników załadowana!")

        # Jeszcze chwila żeby załadowały się wszystkie ceny
        human_delay(4, 6)
        return page.content()

    except PlaywrightTimeout:
        title = page.title()
        log.warning(f"  Timeout po {PAGE_LOAD_TIMEOUT//1000}s. Tytuł: '{title}'")
        page.screenshot(path=f"screenshot_{label}_timeout.png", full_page=True)
        snippet = page.content()[:500].replace(chr(10), " ")
        log.info(f"  Początek HTML: {snippet}")
        return page.content()

    except Exception as e:
        log.error(f"  Błąd Playwright: {e}")
        try:
            page.screenshot(path=f"screenshot_{label}_error.png", full_page=True)
        except Exception:
            pass
        return None


def parse_flights(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []

    option_headers = soup.select("h3[class*='bpk-text--body-default']")

    for h3 in option_headers:
        label = h3.get_text(strip=True)

        price_match = re.search(
            r"(\d[\d\xa0\s]+\d)\s*zł\s*za pasażera.*?(\d[\d\xa0\s]+\d)\s*zł",
            label
        )
        if not price_match:
            continue

        price_per_person = int(re.sub(r"[\s\xa0]+", "", price_match.group(1)))
        price_total      = int(re.sub(r"[\s\xa0]+", "", price_match.group(2)))

        ticket_container = h3.find_parent("div", class_=re.compile("FlightsTicket_container"))
        if not ticket_container:
            continue

        legs = ticket_container.select("div[class*='LegDetails_container']")
        if len(legs) < 2:
            continue

        def get_times(leg):
            dep_el = leg.select_one("[class*='routePartialDepart'] span[class*='subheading']")
            arr_el = leg.select_one("[class*='routePartialArrive'] span[class*='subheading']")
            dep = dep_el.get_text(strip=True) if dep_el else "?"
            arr = arr_el.get_text(strip=True) if arr_el else "?"
            return dep, arr

        out_dep, out_arr = get_times(legs[0])
        in_dep,  in_arr  = get_times(legs[1])

        airline_el = ticket_container.select_one("img[alt]")
        airline = airline_el["alt"] if airline_el and airline_el.get("alt") else "?"

        results.append({
            "price_per_person": price_per_person,
            "price_total":      price_total,
            "outbound_dep":     out_dep,
            "outbound_arr":     out_arr,
            "inbound_dep":      in_dep,
            "inbound_arr":      in_arr,
            "airline":          airline,
        })

    return results


def cheapest(flights: list[dict]) -> dict | None:
    if not flights:
        return None
    return min(flights, key=lambda f: f["price_per_person"])


def get_all_date_pairs() -> list[tuple[datetime.date, datetime.date]]:
    pairs = []
    d = DEPART_START
    while d <= DEPART_END:
        pairs.append((d, d + datetime.timedelta(days=7)))
        d += datetime.timedelta(days=1)
    return pairs


# ─────────────────────────────────────────────────────────────────
#  GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADER = [
    "Timestamp",
    "Wylot (data)",
    "Powrót (data)",
    "Cena / os. (PLN)",
    "Cena razem (PLN)",
    "Wylot WAW",
    "Przylot LIS",
    "Wylot LIS",
    "Przylot WAW",
    "Linia",
    "URL",
]


def get_or_create_worksheet(gc: gspread.Client, title: str) -> gspread.Worksheet:
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(SPREADSHEET_NAME)
        log.info(f"Utworzono nowy arkusz: {SPREADSHEET_NAME}")

    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=5000, cols=len(SHEET_HEADER))
        ws.append_row(SHEET_HEADER, value_input_option="RAW")
        ws.format("1", {"textFormat": {"bold": True}})
        log.info(f"Utworzono zakładkę: {title}")

    return ws


def append_result(ws, timestamp, depart, ret, flight, url):
    if flight:
        row = [
            timestamp,
            depart.strftime("%d.%m.%Y"),
            ret.strftime("%d.%m.%Y"),
            flight["price_per_person"],
            flight["price_total"],
            flight["outbound_dep"],
            flight["outbound_arr"],
            flight["inbound_dep"],
            flight["inbound_arr"],
            flight["airline"],
            url,
        ]
    else:
        row = [timestamp, depart.strftime("%d.%m.%Y"), ret.strftime("%d.%m.%Y"),
               "BRAK DANYCH", "", "", "", "", "", "", url]

    ws.append_row(row, value_input_option="USER_ENTERED")


# ─────────────────────────────────────────────────────────────────
#  GŁÓWNA LOGIKA
# ─────────────────────────────────────────────────────────────────

def main():
    log.info("=== Start scrapowania ===")

    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    ws = get_or_create_worksheet(gc, "Dane")

    timestamp  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    date_pairs = get_all_date_pairs()
    log.info(f"Sprawdzam {len(date_pairs)} kombinacji dat")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )

        # Kontekst z realistycznymi ustawieniami przeglądarki
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            }
        )

        # Ukrywamy że to Playwright/automation
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['pl-PL', 'pl', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        page = context.new_page()

        try:
            # Najpierw odwiedź stronę główną żeby zbudować cookies — jak prawdziwy użytkownik
            log.info("Odwiedzam stronę główną Skyscanner...")
            page.goto("https://www.skyscanner.pl", wait_until="domcontentloaded", timeout=30_000)
            human_delay(3, 6)

            for depart, ret in date_pairs:
                url   = build_url(depart, ret)
                label = f"{depart.strftime('%d.%m')}_{ret.strftime('%d.%m')}"
                log.info(f"Pobieranie: {depart.strftime('%d.%m')} – {ret.strftime('%d.%m')}")

                html    = fetch_page(page, url, label)
                flights = parse_flights(html) if html else []
                best    = cheapest(flights)

                if best:
                    log.info(
                        f"  Najtańszy: {best['price_per_person']} zł/os "
                        f"({best['outbound_dep']}→{best['outbound_arr']} / "
                        f"{best['inbound_dep']}→{best['inbound_arr']}) "
                        f"[{best['airline']}]"
                    )
                else:
                    log.warning(f"  Nie znaleziono lotów")

                append_result(ws, timestamp, depart, ret, best, url)

                # Przerwa między stronami — jak człowiek który czyta wyniki
                human_delay(8, 15)

        finally:
            browser.close()

    log.info("=== Koniec scrapowania ===")


if __name__ == "__main__":
    main()
