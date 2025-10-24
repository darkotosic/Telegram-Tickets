#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, sys, math, time, random, argparse
from typing import Any, Dict, List, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

# ====== ENV (tačno imena iz tvojih sekreta) ======
API_KEY = os.getenv("API_FOOTBALL_KEY") or os.getenv("API_KEY")
BASE_URL = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID_ALL_TIPS_TICKET")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID_ALL_TIPS_TICKET")  # dozvoljeno je i ime tabele
AIRTABLE_FIELD_ID = os.getenv("AIRTABLE_FIELD_ID_ALL_TIPS_TICKET")  # fldXXXX – polje u koje se upisuje tiket

if not API_KEY:
    print("Missing API_FOOTBALL_KEY", file=sys.stderr); sys.exit(1)
if not (AIRTABLE_API_KEY and AIRTABLE_BASE_ID and AIRTABLE_TABLE_ID and AIRTABLE_FIELD_ID):
    print("Missing Airtable env: AIRTABLE_API_KEY, AIRTABLE_BASE_ID_ALL_TIPS_TICKET, "
          "AIRTABLE_TABLE_ID_ALL_TIPS_TICKET, AIRTABLE_FIELD_ID_ALL_TIPS_TICKET", file=sys.stderr)
    sys.exit(1)

# ====== Podešavanja ======
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Belgrade"))
TARGET_TOTAL_ODDS = float(os.getenv("TARGET_TOTAL_ODDS", "3.0"))
MIN_SINGLE_ODD = float(os.getenv("MIN_SINGLE_ODD", "1.01"))
HOME_ODD_MAX = float(os.getenv("HOME_ODD_MAX", "1.45"))  # strogo < 1.45
MAX_TICKETS = int(os.getenv("MAX_TICKETS", "8"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "6"))
RETRY_MAX = int(os.getenv("RETRY_MAX", "4"))
RATE_LIMIT_RPS = float(os.getenv("RATE_LIMIT_RPS", "3"))  # API-Football je osetljiv na 429

ALLOW_LEAGUES = set()  # po potrebi popuni; prazno = sve
SKIP = {"FT","AET","PEN","CANC","ABD","WO","PST","SUSP","1H","2H","HT","LIVE","ET","BT"}

HEADERS = {"x-apisports-key": API_KEY}
AIRTBL_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
AIRTBL_HEADERS = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

# ====== Helpers ======
def _client() -> httpx.Client:
    return httpx.Client(timeout=30)

def _sleep_for_rate():
    if RATE_LIMIT_RPS > 0:
        time.sleep(1.0 / RATE_LIMIT_RPS)

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{'' if path.startswith('/') else '/'}{path}"
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            _sleep_for_rate()
            with _client() as c:
                r = c.get(url, headers=HEADERS, params=params)
                if r.status_code == 429:
                    # exponential backoff
                    wait = min(2 ** i, 30)
                    time.sleep(wait); continue
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(min(2 ** i, 15))
    raise last_exc or RuntimeError("GET failed")

def fetch_fixtures(date_str: str) -> List[Dict[str, Any]]:
    data = _get("/fixtures", {"date": date_str})
    return data.get("response") or []

def fetch_odds(fid: int) -> List[Dict[str, Any]]:
    data = _get("/odds", {"fixture": fid})
    return data.get("response") or []

def _min_odd_in_bet(odds_resp: List[Dict[str, Any]], bet_name: str) -> Dict[str, float]:
    """Vrati minimalne kvote u traženom marketu."""
    out: Dict[str, float] = {}
    for item in odds_resp:
        for bm in item.get("bookmakers", []) or []:
            for bet in bm.get("bets", []) or []:
                if bet.get("name") != bet_name:
                    continue
                for v in bet.get("values", []) or []:
                    label = (v.get("value") or "").strip()
                    try:
                        odd = float(v.get("odd") or 0)
                    except Exception:
                        odd = 0.0
                    if odd <= 0:
                        continue
                    if label not in out or odd < out[label]:
                        out[label] = odd
    return out

# ====== Selekcija parova ======
def build_picks(date_str: str) -> List[Dict[str, Any]]:
    fixtures = fetch_fixtures(date_str)
    picks: List[Dict[str, Any]] = []

    for f in fixtures:
        fxt = f.get("fixture", {}) or {}
        lg = f.get("league", {}) or {}
        tm = f.get("teams", {}) or {}

        if ALLOW_LEAGUES and lg.get("id") not in ALLOW_LEAGUES:
            continue
        st_short = ((fxt.get("status") or {}).get("short")) or ""
        if st_short in SKIP:
            continue

        fid = fxt.get("id")
        if not fid:
            continue

        odds_resp = fetch_odds(fid)
        if not odds_resp:
            continue

        # 1) Double Chance => 1X, samo ako je 1X < X2
        dc = _min_odd_in_bet(odds_resp, "Double Chance")
        odd_1x = dc.get("1X") or dc.get("Home/Draw")
        odd_x2 = dc.get("X2") or dc.get("Draw/Away")
        if odd_1x and odd_x2 and odd_1x < odd_x2 and odd_1x >= MIN_SINGLE_ODD:
            picks.append({
                "fixture": fxt, "league": lg, "teams": tm,
                "market": "Double Chance", "value": "1X", "odd": float(odd_1x)
            })

        # 2) 1X2 => biraj HOME ako je manja od AWAY i strogo < 1.45
        m1x2 = _min_odd_in_bet(odds_resp, "Match Winner") or _min_odd_in_bet(odds_resp, "1X2")
        home = m1x2.get("Home") or m1x2.get("1")
        away = m1x2.get("Away") or m1x2.get("2")
        if home and away and home < away and home < HOME_ODD_MAX and home >= MIN_SINGLE_ODD:
            picks.append({
                "fixture": fxt, "league": lg, "teams": tm,
                "market": "1X2", "value": "Home", "odd": float(home)
            })

    return picks

# ====== Ticket builder ======
def _fmt_leg(d: Dict[str, Any]) -> str:
    lg = d.get("league", {}); tm = d.get("teams", {}); fxt = d.get("fixture", {})
    dt_iso = fxt.get("date")
    try:
        dt = datetime.fromisoformat(str(dt_iso).replace("Z", "+00:00")).astimezone(TZ)
        tstr = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        tstr = str(dt_iso)
    league_name = f"{lg.get('country','')} — {lg.get('name','')}".strip(" —")
    home = (tm.get("home") or {}).get("name", "Home")
    away = (tm.get("away") or {}).get("name", "Away")
    return (
        f"🏟 {league_name}\n"
        f"🆔 {fxt.get('id')}\n"
        f"⚽ {home} vs {away}\n"
        f"⏰ {tstr}\n"
        f"• {d['market']} → {d['value']}: {float(d['odd']):.2f}"
    )

def make_tickets(picks: List[Dict[str, Any]]) -> List[str]:
    random.shuffle(picks)
    tickets_text: List[str] = []
    cur: List[Dict[str, Any]] = []
    cur_total = 1.0

    for p in picks:
        if len(tickets_text) >= MAX_TICKETS:
            break
        o = float(p.get("odd") or 1.0)
        if o < MIN_SINGLE_ODD:
            continue

        if cur_total * o >= TARGET_TOTAL_ODDS:
            cur.append(p)
            body = "\n\n".join(_fmt_leg(x) for x in cur)
            total = 1.0; parts = []
            for x in cur:
                ox = float(x["odd"]); total *= ox; parts.append(f"{ox:.2f}")
            body += f"\n\nTOTAL ODDS: {' × '.join(parts)} = {total:.2f}"
            tickets_text.append(body)
            cur, cur_total = [], 1.0
        else:
            cur.append(p); cur_total *= o

    if cur and len(tickets_text) < MAX_TICKETS:
        body = "\n\n".join(_fmt_leg(x) for x in cur)
        total = 1.0; parts = []
        for x in cur:
            ox = float(x["odd"]); total *= ox; parts.append(f"{ox:.2f}")
        body += f"\n\nTOTAL ODDS: {' × '.join(parts)} = {total:.2f}"
        tickets_text.append(body)

    return tickets_text

    # zadnji tiket ako postoji i ima smisla
    if cur:
        body = "\n\n".join(_fmt_leg(x) for x in cur)
        total = 1.0; parts = []
        for x in cur:
            ox = float(x["odd"]); total *= ox; parts.append(f"{ox:.2f}")
        body += f"\n\nTOTAL ODDS: {' × '.join(parts)} = {total:.2f}"
        tickets_text.append(body)

    return tickets_text

# ====== Airtable upload: jedan tiket = jedan red ======
def airtable_insert_batch(records: List[str]) -> None:
    # Airtable prima do 10 rekorda po zahtevu
    import json
    with _client() as c:
        for i in range(0, len(records), 10):
            chunk = records[i:i+10]
            payload = {
                "records": [{"fields": {AIRTABLE_FIELD_ID: t}} for t in chunk]
            }
            r = c.post(AIRTBL_URL, headers=AIRTBL_HEADERS, json=payload)
            if r.status_code >= 400:
                # prikaži telo greške radi 422 debug-a
                try:
                    print(f"Airtable error {r.status_code}: {r.text}", file=sys.stderr)
                except Exception:
                    pass
            r.raise_for_status()

# ====== Main ======
def run(date_str: str) -> List[str]:
    picks = build_picks(date_str)
    tickets = make_tickets(picks)
    return tickets

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD; default today in Europe/Belgrade")
    args = parser.parse_args()
    date_str = args.date or datetime.now(TZ).strftime("%Y-%m-%d")

    tickets = run(date_str)
    if not tickets:
        print("No tickets"); return

    # ispis u stdout radi loga
    for i, t in enumerate(tickets, 1):
        print(f"🎫 Ticket #{i}\n{t}\n" + ("-"*64))

    # upis u Airtable: jedan tiket = jedan red
    airtable_insert_batch(tickets)
    print(f"Uploaded {len(tickets)} tickets to Airtable")

if __name__ == "__main__":
    main()
