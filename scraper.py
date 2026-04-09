#!/usr/bin/env python3
"""
Skyscanner Flight Price Tracker
WAW → LIS, wrzesień 2026, 2 osoby, loty bezpośrednie
Zapisuje do Google Sheets: najtańszą cenę + godziny lotu dla każdej kombinacji dat
"""

import re
import time
import random
import logging
import datetime
import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────
#  KONFIGURACJA
# ─────────────────────────────────────────────────────────────────

GOOGLE_CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME        = "Loty Lizbona 2026"

# Zakres dat wylotu: 07.09 – 17.09 (powrót zawsze +7 dni, max 24.09)
DEPART_START = datetime.date(2026, 9, 7)
DEPART_END   = datetime.date(2026, 9, 17)

ADULTS          = 2
CABIN           = "economy"
STOPS           = "!oneStop,!twoPlusStops"
DEPARTURE_TIMES = "0-720,780-1439"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.skyscanner.pl/",
}

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


def fetch_page(url: str) -> str | None:
    """Pobiera HTML strony z losowym opóźnieniem."""
    try:
        time.sleep(random.uniform(4, 9))
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 200:
            return resp.text
        log.warning(f"HTTP {resp.status_code} dla {url}")
        return None
    except requests.RequestException as e:
        log.error(f"Błąd pobierania {url}: {e}")
        return None


def parse_time_from_leg(leg_div) -> str | None:
    """Wyciąga godzinę wylotu z elementu LegDetails."""
    time_el = leg_div.select_one("[class*='RoutePartial_routePartialDepart'] span[class*='subheading']")
    if time_el:
        return time_el.get_text(strip=True)
    return None


def parse_flights(html: str) -> list[dict]:
    """
    Parsuje HTML Skyscannera.
    Zwraca listę ofert, każda jako słownik:
      {
        price_per_person: int,      # cena za 1 osobę w PLN
        price_total: int,           # cena za obie osoby
        outbound_dep: str,          # godzina wylotu z WAW (np. "10:10")
        outbound_arr: str,          # godzina przylotu do LIS (np. "13:30")
        inbound_dep: str,           # godzina wylotu z LIS
        inbound_arr: str,           # godzina przylotu do WAW
        airline: str,               # linia lotnicza
      }
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Każda oferta ma aria-label na <h3> w stylu:
    # "Opcja 2: 1 795 zł za pasażera. Razem: 3 590 zł."
    option_headers = soup.select("h3[class*='bpk-text--body-default']")

    for h3 in option_headers:
        label = h3.get_text(strip=True)

        # Wyciągamy ceny
        price_match = re.search(
            r"(\d[\d\s]+\d)\s*zł\s*za pasażera.*?(\d[\d\s]+\d)\s*zł",
            label
        )
        if not price_match:
            continue

        price_per_person = int(re.sub(r"\s+", "", price_match.group(1)))
        price_total      = int(re.sub(r"\s+", "", price_match.group(2)))

        # Kontener biletu — cofamy się do najbliższego FlightsTicket_container
        ticket_container = h3.find_parent("div", class_=re.compile("FlightsTicket_container"))
        if not ticket_container:
            continue

        # Nogi: pierwsza = tam, druga = powrót
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
        in_dep, in_arr   = get_times(legs[1])

        # Linia lotnicza — pierwsza znaleziona (tam)
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
    """Zwraca istniejący arkusz lub tworzy nowy z nagłówkiem."""
    try:
        sh = gc.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(SPREADSHEET_NAME)
        sh.share(None, perm_type="anyone", role="reader")
        log.info(f"Utworzono nowy arkusz: {SPREADSHEET_NAME}")

    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=5000, cols=len(SHEET_HEADER))
        ws.append_row(SHEET_HEADER, value_input_option="RAW")
        ws.format("1", {"textFormat": {"bold": True}})
        log.info(f"Utworzono zakładkę: {title}")

    return ws


def append_result(ws: gspread.Worksheet, timestamp: str, depart: datetime.date,
                  ret: datetime.date, flight: dict | None, url: str):
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

    # Połączenie z Google Sheets
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    # Jedna zakładka per sesja (np. "2026-04-09 06:00")
    now = datetime.datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    tab_title = "Dane"  # wszystkie wpisy w jednej zakładce, timestamp w kolumnie

    ws = get_or_create_worksheet(gc, tab_title)

    date_pairs = get_all_date_pairs()
    log.info(f"Sprawdzam {len(date_pairs)} kombinacji dat")

    for depart, ret in date_pairs:
        url = build_url(depart, ret)
        label = f"{depart.strftime('%d.%m')} – {ret.strftime('%d.%m')}"
        log.info(f"Pobieranie: {label}  {url}")

        html = fetch_page(url)
        if html is None:
            log.warning(f"  Brak odpowiedzi dla {label}")
            append_result(ws, timestamp, depart, ret, None, url)
            continue

        flights = parse_flights(html)
        best = cheapest(flights)

        if best:
            log.info(
                f"  Najtańszy: {best['price_per_person']} zł/os "
                f"({best['outbound_dep']}→{best['outbound_arr']} / "
                f"{best['inbound_dep']}→{best['inbound_arr']}) "
                f"[{best['airline']}]"
            )
        else:
            log.warning(f"  Nie znaleziono lotów dla {label}")

        append_result(ws, timestamp, depart, ret, best, url)

    log.info("=== Koniec scrapowania ===")


if __name__ == "__main__":
    main()
