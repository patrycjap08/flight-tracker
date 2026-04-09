#!/usr/bin/env python3
"""
Skyscanner Flight Price Tracker
WAW → LIS, wrzesień 2026, 2 osoby, loty bezpośrednie
Używa Selenium (headless Chrome) żeby załadować JS przed parsowaniem HTML
"""

import re
import time
import random
import logging
import datetime
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

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

PAGE_LOAD_TIMEOUT = 60

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


def make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=pl-PL")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver


def fetch_page(driver: webdriver.Chrome, url: str) -> str | None:
    try:
        driver.get(url)
        # Czekamy na listę wyników — pojawia się gdy Skyscanner załaduje bilety
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ul#flights-results-list")
            )
        )
        # Dodatkowe 5s żeby załadowały się wszystkie ceny
        time.sleep(5)
        return driver.page_source
    except TimeoutException:
        log.warning(f"  Timeout — zwracam co jest na stronie")
        return driver.page_source
    except Exception as e:
        log.error(f"  Błąd Selenium: {e}")
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

    driver = make_driver()
    try:
        for depart, ret in date_pairs:
            url   = build_url(depart, ret)
            label = f"{depart.strftime('%d.%m')} – {ret.strftime('%d.%m')}"
            log.info(f"Pobieranie: {label}")

            html    = fetch_page(driver, url)
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
                log.warning(f"  Nie znaleziono lotów dla {label}")

            append_result(ws, timestamp, depart, ret, best, url)
            time.sleep(random.uniform(5, 10))

    finally:
        driver.quit()
        log.info("=== Koniec scrapowania ===")


if __name__ == "__main__":
    main()
