#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, sys, time, argparse, math
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

# ===== API-Football =====
API_KEY = os.getenv("API_FOOTBALL_KEY")
if not API_KEY:
    print("⛔ Missing API_FOOTBALL_KEY", file=sys.stderr)
    sys.exit(1)

BASE_URL = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
HEADERS = {"x-apisports-key": API_KEY}

# ===== Telegram (direktno u kodu po zahtevu) =====
TELEGRAM_BOT_TOKEN = "7350949079:AAFyq-BZQeSGoJl0wzWA6a0796yqN0f3v4E"
TELEGRAM_CHANNELS = ["@betsmart_win_more", "@naksir_server_channel", "@naksiranalysis"]

# ===== Podešavanja =====
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Belgrade"))
# ciljevi za kombinovanje
ODDS_SINGLE_MAX = float(os.getenv("ODDS_SINGLE_MAX", "1.55"))   # gornja granica kvote po paru
MIN_SINGLE_ODD  = float(os.getenv("MIN_SINGLE_ODD",  "1.18"))   # donja granica kvote po paru
TICKET_TARGET   = float(os.getenv("TICKET_TARGET",   "2.50"))   # minimalna ukupna kvota tiketa
LEGS_MIN        = int(os.getenv("LEGS_MIN",          "2"))
LEGS_MAX        = int(os.getenv("LEGS_MAX",          "3"))
TICKETS_COUNT   = int(os.getenv("TICKETS_COUNT",     "4"))      # tačno 4 tiketa

RATE_LIMIT_RPS  = float(os.getenv("RATE_LIMIT_RPS",  "3"))      # zaštita od 429
RETRY_MAX       = int(os.getenv("RETRY_MAX",         "4"))

# ignorisati završene/stopirane
SKIP = {"FT","AET","PEN","CANC","ABD","WO","PST","SUSP","1H","2H","HT","LIVE","ET","BT"}

# ALLOW_LIST opciono kroz env; prazno = sve
_allow_env = (os.getenv("ALLOW_LIST") or "").strip()
ALLOW_LIST: Optional[set[int]] = {int(x) for x in _allow_env.split(",") if x.strip().isdigit()} if _allow_env else None

# ===== HTTP helperi =====
def _sleep_for_rate(): 
    if RATE_LIMIT_RPS > 0:
        time.sleep(1.0 / RATE_LIMIT_RPS)

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL.rstrip('/')}{path if path.startswith('/') else '/' + path}"
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            _sleep_for_rate()
            with httpx.Client(timeout=30) as c:
                r = c.get(url, headers=HEADERS, params=params or {})
                if r.status_code == 429:
                    time.sleep(min(2 ** i, 30))
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError as e:
            last_exc = e
            time.sleep(min(2 ** i, 15))
    raise last_exc or RuntimeError("GET failed")

# ===== API-Football fetch =====
def fetch_fixtures(date_str: str) -> List[Dict[str, Any]]:
    return _get("/fixtures", {"date": date_str}).get("response") or []

def fetch_odds(fid: int) -> List[Dict[str, Any]]:
    return _get("/odds", {"fixture": fid}).get("response") or []

# ===== Parsiranje tržišta i filtriranje =====
# EXPLICITNO izbacujemo BTTS
BLOCKED_MARKETS = {"both teams to score", "btts"}

# dozvoljena tržišta i mapiranje vrednosti na čitljiv format
VALUE_NORMALIZER = {
    "1": ("Match Winner", "Home Win"),
    "2": ("Match Winner", "Away Win"),
    "home": ("Match Winner", "Home Win"),
    "away": ("Match Winner", "Away Win"),
    "home/away": ("Double Chance", "12"),
    "draw": ("1X2", "Draw"),
    "1x": ("Double Chance", "1X"),
    "x2": ("Double Chance", "X2"),
    "over 0.5": ("Over/Under", "Over 0.5"),
    "over 1.5": ("Over/Under", "Over 1.5"),
    "over 2.5": ("Over/Under", "Over 2.5"),
    "under 3.5": ("Over/Under", "Under 3.5"),
    "under 2.5": ("Over/Under", "Under 2.5"),
}

def _pick_from_betname(betname: str, values: List[Dict[str, Any]]) -> List[Tuple[str,str,float]]:
    nm = betname.lower().strip()
    if any(k in nm for k in BLOCKED_MARKETS):
        return []  # BTTS out

    picks: List[Tuple[str,str,float]] = []
    for v in values or []:
        val_raw = str(v.get("value") or "").strip()
        odd_raw = v.get("odd")
        try:
            odd = float(odd_raw)
        except Exception:
            continue
        if not (MIN_SINGLE_ODD <= odd <= ODDS_SINGLE_MAX):
            continue

        key = val_raw.lower()
        if key in VALUE_NORMALIZER:
            market, value = VALUE_NORMALIZER[key]
        else:
            # za 1X2 / Double Chance / Over/Under gde value već ima naziv
            if any(s in nm for s in ("1x2", "match winner", "match odds")):
                market, value = ("Match Winner/1X2", val_raw)
            elif "double chance" in nm:
                market, value = ("Double Chance", val_raw)
            elif "over/under" in nm or "goals over/under" in nm or "total goals" in nm:
                market, value = ("Over/Under", val_raw)
            else:
                # ostala tržišta dopuštamo ali pod uslovom da nisu BTTS
                market, value = (betname, val_raw)

        picks.append((market, value, odd))
    return picks

def collect_candidates(fixture: Dict[str, Any], odds_resp: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in odds_resp or []:
        for bm in item.get("bookmakers", []) or []:
            for bet in bm.get("bets", []) or []:
                betname = bet.get("name") or ""
                for market, value, odd in _pick_from_betname(betname, bet.get("values") or []):
                    out.append({
                        "fixture": fixture.get("fixture") or fixture,
                        "league": fixture.get("league") or {},
                        "teams": fixture.get("teams") or {},
                        "market": market,
                        "value": value,
                        "odd": odd,
                    })
    return out

# ===== Kompozicija tiketa =====
def build_pool(date_str: str) -> List[Dict[str, Any]]:
    fixtures = fetch_fixtures(date_str)
    res: List[Dict[str, Any]] = []

    # Ako je aktivan allow-list, filtriraj
    allow_active = bool(ALLOW_LIST)
    for f in fixtures:
        fx = f.get("fixture", {}) or {}
        lg = f.get("league", {}) or {}
        if not fx.get("id"):
            continue
        st = (fx.get("status") or {}).get("short") or ""
        if st in SKIP:
            continue
        if allow_active and lg.get("id") not in ALLOW_LIST:
            continue

        odds = fetch_odds(fx["id"])
        res.extend(collect_candidates(f, odds))

    # sort po kvoti opadajuće
    res.sort(key=lambda d: float(d["odd"]), reverse=True)
    return res

def _format_leg(d: Dict[str, Any]) -> str:
    lg = d.get("league", {})
    tm = d.get("teams", {})
    fx = d.get("fixture", {})
    dt_iso = fx.get("date")
    try:
        tstr = datetime.fromisoformat(str(dt_iso).replace("Z","+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        tstr = str(dt_iso)
    league_name = f"{lg.get('country','')} — {lg.get('name','')}".strip(" —")
    home = (tm.get("home") or {}).get("name", "Home")
    away = (tm.get("away") or {}).get("name", "Away")
    return (
        f"🏟 {league_name}\n"
        f"🆔 Fixture ID: {fx.get('id')}\n"
        f"⚽ {home} vs {away}\n"
        f"⏰ {tstr}\n"
        f"• {d['market']} → {d['value']}: {float(d['odd']):.2f}"
    )

def make_tickets(pool: List[Dict[str, Any]]) -> List[str]:
    tickets: List[str] = []
    used_fixtures: set[int] = set()

    def can_use(d: Dict[str, Any]) -> bool:
        fx = d.get("fixture", {})
        fid = fx.get("id")
        return fid not in used_fixtures

    i = 0
    while len(tickets) < TICKETS_COUNT and i < len(pool):
        block: List[Dict[str, Any]] = []
        total = 1.0
        # greedy: biraj najbolje kvote koje nisu već korišćene i dosegni target
        for j in range(i, len(pool)):
            d = pool[j]
            if not can_use(d):
                continue
            cand = block + [d]
            if len(cand) > LEGS_MAX:
                continue
            new_total = total * float(d["odd"])
            block.append(d)
            total = new_total
            if len(block) >= LEGS_MIN and total >= TICKET_TARGET:
                break
        # ako nije dostigao target, pokušaj da dodaš još jednu iz repa
        if not (len(block) >= LEGS_MIN and total >= TICKET_TARGET):
            # pokušaj još jednom sken kroz ostatak
            for k in range(len(pool)-1, -1, -1):
                if len(block) >= LEGS_MAX:
                    break
                d2 = pool[k]
                if can_use(d2):
                    block.append(d2)
                    total *= float(d2["odd"])
                    if len(block) >= LEGS_MIN and total >= TICKET_TARGET:
                        break

        if len(block) >= LEGS_MIN:
            for d in block:
                fid = (d.get("fixture") or {}).get("id")
                if fid: used_fixtures.add(fid)
            parts = [_format_leg(x) for x in block]
            comps = [f"{float(x['odd']):.2f}" for x in block]
            ticket_txt = "\n\n".join(parts) + f"\n\nTOTAL ODDS: {' × '.join(comps)} = {total:.2f}"
            tickets.append(ticket_txt)

        i += 1

    # ako i dalje nema 4, dopuni sa najboljim preostalim parovima pojedinačno
    if len(tickets) < TICKETS_COUNT:
        for d in pool:
            if len(tickets) >= TICKETS_COUNT:
                break
            if not can_use(d):
                continue
            block = [d]
            total = float(d["odd"])
            parts = [_format_leg(d)]
            comps = [f"{total:.2f}"]
            ticket_txt = "\n\n".join(parts) + f"\n\nTOTAL ODDS: {' × '.join(comps)} = {total:.2f}"
            tickets.append(ticket_txt)
            fid = (d.get("fixture") or {}).get("id")
            if fid: used_fixtures.add(fid)

    return tickets[:TICKETS_COUNT]

# ===== Telegram slanje =====
def _tg_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for ch in TELEGRAM_CHANNELS:
        try:
            with httpx.Client(timeout=20) as c:
                c.post(url, json={"chat_id": ch, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        except Exception:
            pass

def run(date_str: Optional[str] = None, send_telegram: bool = True) -> List[str]:
    date_str = date_str or datetime.now(TZ).strftime("%Y-%m-%d")
    pool = build_pool(date_str)
    tickets = make_tickets(pool)

    # header za dnevni paket
    header = f"📅 {date_str} — NAKSIR ANALYST AI\nIzbačeni BTTS marketi. Formirano {len(tickets)} tiketa."
    if send_telegram:
        _tg_send(header)
        for idx, t in enumerate(tickets, 1):
            _tg_send(f"🎫 Ticket #{idx}\n{t}")

    # ispis u log
    print(header)
    for i, t in enumerate(tickets, 1):
        print(f"\n--- Ticket #{i} ---\n{t}\n")

    return tickets

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD; default today", default=None)
    ap.add_argument("--no-send", action="store_true", help="ne šalji na Telegram, samo ispis")
    args = ap.parse_args()
    run(args.date, send_telegram=not args.no_send)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
