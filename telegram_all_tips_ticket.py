#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, sys, math, time, random, argparse
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

# ================= ENV =================
API_KEY = os.getenv("API_FOOTBALL_KEY") or os.getenv("API_KEY")
BASE_URL = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
if not API_KEY:
    print("⛔ Missing API_FOOTBALL_KEY", file=sys.stderr)
    sys.exit(1)

# ===== Telegram slanje =====
TELEGRAM_BOT_TOKEN = "7350949079:AAFyq-BZQeSGoJl0wzWA6a0796yqN0f3v4E"
TELEGRAM_CHANNELS = ["@betsmart_win_more", "@naksir_server_channel", "@naksiranalysis"]

# ================= Podešavanja =================
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Belgrade"))
TARGETS = [2.0, 3.0, 4.0, 5.0]
MIN_SINGLE_ODD = float(os.getenv("MIN_SINGLE_ODD", "1.01"))
HOME_ODD_MAX = float(os.getenv("HOME_ODD_MAX", "1.45"))
RETRY_MAX = int(os.getenv("RETRY_MAX", "4"))
RATE_LIMIT_RPS = float(os.getenv("RATE_LIMIT_RPS", "3"))
ALLOW_LEAGUES: Set[int] = set()
SKIP = {"FT","AET","PEN","CANC","ABD","WO","PST","SUSP","1H","2H","HT","LIVE","ET","BT"}
HEADERS = {"x-apisports-key": API_KEY}

# ================= HTTP helper =================
def _client() -> httpx.Client:
    print("DEBUG: initializing httpx client")
    return httpx.Client(timeout=30)

def _sleep_for_rate():
    if RATE_LIMIT_RPS > 0:
        print(f"DEBUG: sleeping for rate limit {1.0/RATE_LIMIT_RPS:.2f}s")
        time.sleep(1.0 / RATE_LIMIT_RPS)

def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{'' if path.startswith('/') else '/'}{path}"
    print(f"DEBUG: GET {url} {params}")
    last_exc = None
    for i in range(RETRY_MAX):
        try:
            _sleep_for_rate()
            with _client() as c:
                r = c.get(url, headers=HEADERS, params=params)
                print(f"DEBUG: HTTP status {r.status_code}")
                if r.status_code == 429:
                    print(f"DEBUG: rate limited, retry {i}")
                    time.sleep(min(2 ** i, 30))
                    continue
                r.raise_for_status()
                return r.json()
        except httpx.HTTPError as e:
            print(f"DEBUG: HTTP error {e}, retry {i}")
            last_exc = e
            time.sleep(min(2 ** i, 15))
    raise last_exc or RuntimeError("GET failed")

# ================= API-Football =================
def fetch_fixtures(date_str: str) -> List[Dict[str, Any]]:
    print(f"DEBUG: fetching fixtures for {date_str}")
    data = _get("/fixtures", {"date": date_str})
    response = data.get("response") or []
    print(f"DEBUG: fixtures count: {len(response)}")
    return response

def fetch_odds(fid: int) -> List[Dict[str, Any]]:
    print(f"DEBUG: fetching odds for fixture {fid}")
    data = _get("/odds", {"fixture": fid})
    response = data.get("response") or []
    print(f"DEBUG: odds sets: {len(response)}")
    return response

def _min_odd_in_bet(odds_resp: List[Dict[str, Any]], bet_name: str) -> Dict[str, float]:
    print(f"DEBUG: parsing odds for bet '{bet_name}'")
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
    print(f"DEBUG: parsed {len(out)} values for '{bet_name}': {out}")
    return out

# ================= Selekcija parova =================
def build_picks(date_str: str) -> List[Dict[str, Any]]:
    print(f"DEBUG: building picks for {date_str}")
    fixtures = fetch_fixtures(date_str)
    picks: List[Dict[str, Any]] = []

    for f in fixtures:
        fxt = f.get("fixture", {}) or {}
        lg = f.get("league", {}) or {}
        tm = f.get("teams", {}) or {}
        fid = fxt.get("id")

        if not fid:
            continue
        st_short = ((fxt.get("status") or {}).get("short")) or ""
        if st_short in SKIP:
            continue
        if ALLOW_LEAGUES and lg.get("id") not in ALLOW_LEAGUES:
            continue

        odds_resp = fetch_odds(fid)
        if not odds_resp:
            continue

        # 1X double chance
        dc = _min_odd_in_bet(odds_resp, "Double Chance")
        odd_1x = dc.get("1X") or dc.get("Home/Draw")
        odd_x2 = dc.get("X2") or dc.get("Draw/Away")
        if odd_1x and odd_x2 and odd_1x < odd_x2 and odd_1x >= MIN_SINGLE_ODD:
            picks.append({
                "fixture": fxt, "league": lg, "teams": tm,
                "market": "Double Chance", "value": "1X", "odd": float(odd_1x)
            })
            print(f"DEBUG: added DC 1X {odd_1x} for fixture {fid}")

        # HOME win
        m1x2 = _min_odd_in_bet(odds_resp, "Match Winner") or _min_odd_in_bet(odds_resp, "1X2")
        home = m1x2.get("Home") or m1x2.get("1")
        away = m1x2.get("Away") or m1x2.get("2")
        if home and away and home < away and home < HOME_ODD_MAX and home >= MIN_SINGLE_ODD:
            picks.append({
                "fixture": fxt, "league": lg, "teams": tm,
                "market": "1X2", "value": "Home", "odd": float(home)
            })
            print(f"DEBUG: added Home win {home} for fixture {fid}")

    random.shuffle(picks)
    print(f"DEBUG: total picks collected: {len(picks)}")
    return picks

# ================= Format =================
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
    text = (
        f"🏟 {league_name}\n"
        f"🆔 Fixture ID: {fxt.get('id')}\n"
        f"⚽ {home} vs {away}\n"
        f"⏰ {tstr}\n"
        f"• {d['market']} → {d['value']}: {float(d['odd']):.2f}"
    )
    print(f"DEBUG: formatted leg {fxt.get('id')} {d['market']} {d['odd']}")
    return text

# ================= Ticket builder =================
def make_tickets_for_targets(picks: List[Dict[str, Any]], targets: List[float]) -> List[str]:
    print(f"DEBUG: making tickets for targets {targets}")
    tickets_text: List[str] = []
    used_global: Set[int] = set()
    picks_sorted = sorted(picks, key=lambda x: float(x["odd"]), reverse=True)

    def fid_of(p: Dict[str, Any]) -> Optional[int]:
        return (p.get("fixture") or {}).get("id")

    for target in targets:
        print(f"DEBUG: building ticket target {target}")
        cur: List[Dict[str, Any]] = []
        cur_total = 1.0
        used_local: Set[int] = set()

        for p in picks_sorted:
            fid = fid_of(p)
            if not fid or fid in used_global or fid in used_local:
                continue
            o = float(p.get("odd") or 1.0)
            if o < MIN_SINGLE_ODD:
                continue
            cur.append(p)
            used_local.add(fid)
            cur_total *= o
            if cur_total >= target:
                break

        if cur:
            used_global |= used_local
            body = "\n\n".join(_fmt_leg(x) for x in cur)
            total = 1.0; parts = []
            for x in cur:
                ox = float(x["odd"]); total *= ox; parts.append(f"{ox:.2f}")
            body += f"\n\nTOTAL ODDS: {' × '.join(parts)} = {total:.2f}"
            tickets_text.append(body)
            print(f"DEBUG: ticket target {target} built with total odds {total:.2f}")
        else:
            print(f"DEBUG: not enough picks to reach target {target}")

    if len(tickets_text) < 4:
        print("DEBUG: filling missing tickets with singles")
        for p in picks_sorted:
            if len(tickets_text) >= 4:
                break
            fid = fid_of(p)
            if not fid or fid in used_global:
                continue
            body = _fmt_leg(p)
            o = float(p["odd"])
            body += f"\n\nTOTAL ODDS: {o:.2f}"
            tickets_text.append(body)
            used_global.add(fid)
            print(f"DEBUG: added single filler ticket for fixture {fid}")

    print(f"DEBUG: total tickets built {len(tickets_text)}")
    return tickets_text[:4]

# ================= Telegram slanje =================
def tg_send(text: str) -> None:
    print(f"DEBUG: sending Telegram message len={len(text)}")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    with httpx.Client(timeout=20) as c:
        for ch in TELEGRAM_CHANNELS:
            try:
                r = c.post(url, json={**payload, "chat_id": ch})
                print(f"DEBUG: sent to {ch}, status {r.status_code}")
            except Exception as e:
                print(f"DEBUG: failed to send to {ch}: {e}")

# ================= Main run =================
def run(date_str: str, send_telegram: bool = True) -> List[str]:
    print(f"DEBUG: run started for {date_str}")
    picks = build_picks(date_str)
    tickets = make_tickets_for_targets(picks, TARGETS)

    header = f"📅 {date_str} — NAKSIR ANALYST AI\nBTTS markets excluded. Formirano {len(tickets)} tiketa."
    print(f"DEBUG: header ready")
    if send_telegram:
        tg_send(header)
        for i, t in enumerate(tickets, 1):
            tg_send(f"🎫 Ticket #{i}\n{t}")
            print(f"DEBUG: sent Ticket #{i}")

    print(header)
    for i, t in enumerate(tickets, 1):
        print(f"\n--- Ticket #{i} ---\n{t}\n")

    print("DEBUG: run complete")
    return tickets

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD; default today in Europe/Belgrade")
    parser.add_argument("--no-send", action="store_true", help="ne šalji na Telegram")
    args = parser.parse_args()
    date_str = args.date or datetime.now(TZ).strftime("%Y-%m-%d")
    print(f"DEBUG: main entry date={date_str}, send={not args.no_send}")
    run(date_str, send_telegram=not args.no_send)

if __name__ == "__main__":
    main()
