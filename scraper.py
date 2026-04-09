#!/usr/bin/env python3
"""
Flight Price Tracker — Amadeus API
WAW/WMI → LIS, wrzesień 2026, 2 osoby
Linie: LOT, TAP Air Portugal, Ryanair, Wizz Air
Wylot przed 12:00, powrót po 13:00
"""

import time
import logging
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────
#  KONFIGURACJA
# ─────────────────────────────────────────────────────────────────

import os
AMADEUS_CLIENT_ID     = os.environ.get("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET", "")

GOOGLE_CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME        = "Loty Lizbona 2026"

DEPART_START = datetime.date(2026, 9, 7)
DEPART_END   = datetime.date(2026, 9, 17)

# Kody IATA lotnisk wylotu (Warszawa Chopina + Modlin)
ORIGINS      = ["WAW", "WMI"]
DESTINATION  = "LIS"
ADULTS       = 2
CABIN        = "ECONOMY"

# Interesujące nas linie (kody IATA)
ALLOWED_AIRLINES = {"LO", "TP", "FR", "W6"}
# LO = LOT, TP = TAP Air Portugal, FR = Ryanair, W6 = Wizz Air

# Filtry godzinowe
MAX_DEPART_HOUR  = 12   # wylot z WAW/WMI przed 12:00
MIN_RETURN_HOUR  = 13   # powrót z LIS po 13:00

# ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
#  AMADEUS AUTH
# ─────────────────────────────────────────────────────────────────

class AmadeusClient:
    BASE = "https://test.api.amadeus.com"  # zmień na api.amadeus.com po przejściu na produkcję

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token        = None
        self._token_expiry = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        resp = requests.post(
            f"{self.BASE}/v1/security/oauth2/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token        = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        log.info("Amadeus token odświeżony")
        return self._token

    def search_flights(self, origin: str, destination: str,
                       depart_date: str, return_date: str,
                       adults: int, cabin: str) -> list[dict]:
        """
        Wyszukuje loty i zwraca surowe oferty z API.
        depart_date / return_date w formacie YYYY-MM-DD
        """
        token = self._get_token()
        params = {
            "originLocationCode":      origin,
            "destinationLocationCode": destination,
            "departureDate":           depart_date,
            "returnDate":              return_date,
            "adults":                  adults,
            "travelClass":             cabin,
            "nonStop":                 "true",   # tylko loty bezpośrednie
            "currencyCode":            "PLN",
            "max":                     50,        # max ofert na zapytanie
        }
        resp = requests.get(
            f"{self.BASE}/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
        log.warning(f"  Amadeus HTTP {resp.status_code}: {resp.text[:200]}")
        return []


# ─────────────────────────────────────────────────────────────────
#  FILTROWANIE I PARSOWANIE OFERT
# ─────────────────────────────────────────────────────────────────

def parse_hour(dt_str: str) -> int:
    """Wyciąga godzinę z datetime ISO np. '2026-09-07T10:10:00'"""
    return int(dt_str[11:13])


def parse_time(dt_str: str) -> str:
    """Zwraca HH:MM z datetime ISO"""
    return dt_str[11:16]


def extract_best_offer(offers: list[dict], max_depart_hour: int, min_return_hour: int) -> dict | None:
    """
    Z listy ofert Amadeus wybiera najtańszą która spełnia:
    - linia lotnicza z ALLOWED_AIRLINES
    - wylot z WAW/WMI przed max_depart_hour
    - powrót z LIS po min_return_hour
    """
    candidates = []

    for offer in offers:
        try:
            price_total = float(offer["price"]["grandTotal"])
            price_per_person = price_total / ADULTS

            itineraries = offer["itineraries"]
            if len(itineraries) < 2:
                continue

            # Lot tam — pierwszy segment (bezpośredni więc jeden)
            outbound_seg = itineraries[0]["segments"][0]
            # Lot powrotny
            inbound_seg  = itineraries[1]["segments"][0]

            airline = outbound_seg["carrierCode"]
            if airline not in ALLOWED_AIRLINES:
                continue

            out_dep_hour = parse_hour(outbound_seg["departure"]["at"])
            in_dep_hour  = parse_hour(inbound_seg["departure"]["at"])

            if out_dep_hour >= max_depart_hour:
                continue
            if in_dep_hour < min_return_hour:
                continue

            candidates.append({
                "price_per_person": round(price_per_person, 2),
                "price_total":      round(price_total, 2),
                "outbound_dep":     parse_time(outbound_seg["departure"]["at"]),
                "outbound_arr":     parse_time(outbound_seg["arrival"]["at"]),
                "inbound_dep":      parse_time(inbound_seg["departure"]["at"]),
                "inbound_arr":      parse_time(inbound_seg["arrival"]["at"]),
                "airline":          airline,
                "origin":           outbound_seg["departure"]["iataCode"],
            })

        except (KeyError, IndexError, ValueError):
            continue

    if not candidates:
        return None
    return min(candidates, key=lambda x: x["price_total"])


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
    "Lotnisko wylotu",
    "Linia",
    "Cena / os. (PLN)",
    "Cena razem (PLN)",
    "Wylot WAW/WMI",
    "Przylot LIS",
    "Wylot LIS",
    "Przylot WAW/WMI",
]

AIRLINE_NAMES = {
    "LO": "LOT",
    "TP": "TAP Air Portugal",
    "FR": "Ryanair",
    "W6": "Wizz Air",
}


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


def append_result(ws, timestamp, depart, ret, best):
    if best:
        airline_name = AIRLINE_NAMES.get(best["airline"], best["airline"])
        row = [
            timestamp,
            depart.strftime("%d.%m.%Y"),
            ret.strftime("%d.%m.%Y"),
            best["origin"],
            airline_name,
            best["price_per_person"],
            best["price_total"],
            best["outbound_dep"],
            best["outbound_arr"],
            best["inbound_dep"],
            best["inbound_arr"],
        ]
    else:
        row = [timestamp, depart.strftime("%d.%m.%Y"), ret.strftime("%d.%m.%Y"),
               "", "", "BRAK OFERT", "", "", "", "", ""]

    ws.append_row(row, value_input_option="USER_ENTERED")


# ─────────────────────────────────────────────────────────────────
#  GŁÓWNA LOGIKA
# ─────────────────────────────────────────────────────────────────

def main():
    log.info("=== Start scrapowania ===")

    amadeus = AmadeusClient(AMADEUS_CLIENT_ID, AMADEUS_CLIENT_SECRET)

    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    ws    = get_or_create_worksheet(gc, "Dane")

    timestamp  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    date_pairs = get_all_date_pairs()
    log.info(f"Sprawdzam {len(date_pairs)} kombinacji dat x {len(ORIGINS)} lotniska")

    for depart, ret in date_pairs:
        depart_str = depart.strftime("%Y-%m-%d")
        ret_str    = ret.strftime("%Y-%m-%d")
        label      = f"{depart.strftime('%d.%m')} – {ret.strftime('%d.%m')}"

        all_offers = []

        # Sprawdzamy oba lotniska (WAW i WMI)
        for origin in ORIGINS:
            log.info(f"Zapytanie: {origin} → {DESTINATION}  {label}")
            offers = amadeus.search_flights(
                origin, DESTINATION, depart_str, ret_str, ADULTS, CABIN
            )
            log.info(f"  Otrzymano {len(offers)} ofert z {origin}")
            all_offers.extend(offers)
            time.sleep(0.5)  # grzeczna przerwa między requestami

        best = extract_best_offer(all_offers, MAX_DEPART_HOUR, MIN_RETURN_HOUR)

        if best:
            airline_name = AIRLINE_NAMES.get(best["airline"], best["airline"])
            log.info(
                f"  Najtańszy: {best['price_per_person']} PLN/os "
                f"({best['outbound_dep']}→{best['outbound_arr']} / "
                f"{best['inbound_dep']}→{best['inbound_arr']}) "
                f"[{airline_name}] z {best['origin']}"
            )
        else:
            log.warning(f"  Brak ofert spełniających kryteria dla {label}")

        append_result(ws, timestamp, depart, ret, best)

    log.info("=== Koniec ===")


if __name__ == "__main__":
    main()
