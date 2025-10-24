#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, time, random, argparse
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx

# ============ ENVIRONMENT ============
API_KEY = os.getenv("API_FOOTBALL_KEY")
BASE_URL = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
if not API_KEY:
    print("⛔ Missing API_FOOTBALL_KEY", file=sys.stderr)
    sys.exit(1)

# Telegram bot
TELEGRAM_BOT_TOKEN = "7350949079:AAFyq-BZQeSGoJl0wzWA6a0796yqN0f3v4E"
TELEGRAM_CHANNELS = ["@betsmart_win_more", "@naksir_server_channel", "@naksiranalysis"]

# Global settings
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Belgrade"))
MAX_MATCHES = 250
RELAX_STEPS = 3
RELAX_ADD = 0.03
LEGS_MIN = 2
LEGS_MAX = 5

# Target odds for 4 tickets
TARGETS = [2.0, 3.0, 4.0, 5.0]

HEADERS = {"x-apisports-key": API_KEY}
SKIP_STATUS = {"FT","AET","PEN","CANC","ABD","WO","PST","SUSP","INT","LIVE","ET"}
ALLOW_LIST: set[int] = set()

# ============ BASE THRESHOLDS ============
BASE_TH: Dict[Tuple[str,str], float] = {
    ("Double Chance","1X"): 1.25,
    ("Double Chance","X2"): 1.30,
    ("Over/Under","Over 1.5"): 1.20,
    ("Over/Under","Under 3.5"): 1.15,
    ("Over/Under","Over 2.5"): 1.44,
    ("Match Winner","Home"): 1.33,
    ("Match Winner","Away"): 1.34,
    ("Home Team Goals","Over 0.5"): 1.30,
    ("Away Team Goals","Over 0.5"): 1.40,
    ("1st Half Goals","Over 0.5"): 1.35,
}

# ============ HELPERS ============
def debug(msg: str):
    print(f"DEBUG: {msg}", flush=True)

def _client() -> httpx.Client:
    debug("Creating httpx client")
    return httpx.Client(timeout=30)

def _try_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None

def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso

# ============ API FETCH ============
def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}{'' if path.startswith('/') else '/'}{path}"
    debug(f"GET {url} {params}")
    for attempt in range(6):
        try:
            with _client() as c:
                r = c.get(url, headers=HEADERS, params=params)
                if r.status_code == 429:
                    debug(f"Rate limit 429, waiting 5s…")
                    time.sleep(5)
                    continue
                r.raise_for_status()
                return r.json()
        except Exception as e:
            debug(f"HTTP error attempt {attempt+1}: {e}")
            time.sleep(3)
    raise RuntimeError("GET failed after retries")

def fetch_fixtures(date_str: str) -> List[Dict[str, Any]]:
    debug(f"Fetching fixtures for {date_str}")
    res = _get("/fixtures", {"date": date_str}).get("response") or []
    fixtures = []
    for f in res:
        fx = f.get("fixture", {}) or {}
        lg = f.get("league", {}) or {}
        st = (fx.get("status") or {}).get("short", "")
        if st not in SKIP_STATUS and (not ALLOW_LIST or lg.get("id") in ALLOW_LIST):
            fixtures.append(f)
        if len(fixtures) >= MAX_MATCHES:
            break
    debug(f"Fixtures collected: {len(fixtures)}")
    return fixtures

def fetch_odds(fid: int) -> List[Dict[str, Any]]:
    debug(f"Fetching odds for fixture {fid}")
    return _get("/odds", {"fixture": fid}).get("response") or []

# ============ ODDS PARSING ============
def best_market_odds(odds_resp: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    debug("Parsing best market odds")
    best: Dict[str, Dict[str, float]] = {}
    def put(mkt: str, val: str, odd_raw: Any):
        odd = _try_float(odd_raw)
        if odd is None:
            return
        best.setdefault(mkt, {})
        if val not in best[mkt] or odd > best[mkt][val]:
            best[mkt][val] = odd

    for item in odds_resp:
        for bm in item.get("bookmakers", []) or []:
            for bet in bm.get("bets", []) or []:
                name = (bet.get("name") or "").strip()
                if not name:
                    continue

                # Match Winner
                if "Match Winner" in name or "1X2" in name:
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").strip()
                        if val in ("Home","1"):
                            put("Match Winner","Home",v.get("odd"))
                        elif val in ("Away","2"):
                            put("Match Winner","Away",v.get("odd"))
                    continue

                # Double Chance
                if "Double Chance" in name:
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").replace(" ","").upper()
                        if val in {"1X","X2","12"}:
                            put("Double Chance",val,v.get("odd"))
                    continue

                # Over/Under
                if "Over/Under" in name or "Total Goals" in name:
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").strip().title()
                        if val in {"Over 1.5","Under 3.5","Over 2.5"}:
                            put("Over/Under",val,v.get("odd"))
                    continue

                # Team Totals
                if "Home Team Goals" in name:
                    for v in bet.get("values",[]) or []:
                        val=(v.get("value") or "").strip()
                        if "Over 0.5" in val:
                            put("Home Team Goals","Over 0.5",v.get("odd"))
                    continue
                if "Away Team Goals" in name:
                    for v in bet.get("values",[]) or []:
                        val=(v.get("value") or "").strip()
                        if "Over 0.5" in val:
                            put("Away Team Goals","Over 0.5",v.get("odd"))
                    continue

                # First Half Goals
                if "1st Half" in name:
                    for v in bet.get("values",[]) or []:
                        val=(v.get("value") or "").strip()
                        if "Over 0.5" in val:
                            put("1st Half Goals","Over 0.5",v.get("odd"))
                    continue
    debug(f"Markets found: {list(best.keys())}")
    return best

# ============ CANDIDATES ============
def collect_candidates(f: Dict[str, Any], th: Dict[Tuple[str,str], float]) -> List[Dict[str, Any]]:
    odds = f.get("odds", {}) or {}
    out: List[Dict[str, Any]] = []
    debug(f"Collecting candidates for fixture {f.get('fixture',{}).get('id')}")

    def add_if_ok(market: str, pick: str):
        v = _try_float((odds.get(market) or {}).get(pick))
        lim = th.get((market,pick))
        if v is not None and lim is not None and v < lim:
            out.append({"market": market, "pick": pick, "odd": v})
            debug(f"  accepted {market} {pick} {v}")

    add_if_ok("Double Chance","1X")
    add_if_ok("Double Chance","X2")
    add_if_ok("Over/Under","Over 1.5")
    add_if_ok("Over/Under","Under 3.5")
    add_if_ok("Over/Under","Over 2.5")
    add_if_ok("Match Winner","Home")
    add_if_ok("Match Winner","Away")
    add_if_ok("Home Team Goals","Over 0.5")
    add_if_ok("Away Team Goals","Over 0.5")
    add_if_ok("1st Half Goals","Over 0.5")

    return out

def best_leg_for_fixture(f: Dict[str,Any], th: Dict[Tuple[str,str],float]) -> Optional[Dict[str,Any]]:
    cands = collect_candidates(f, th)
    if not cands:
        return None
    best = sorted(cands, key=lambda x: x["odd"], reverse=True)[0]
    fx = f.get("fixture", {}) or {}
    lg = f.get("league", {}) or {}
    tm = f.get("teams", {}) or {}
    return {
        "fid": fx.get("id"),
        "league": f"🏟 {lg.get('country','')} — {lg.get('name','')}",
        "teams": f"⚽ {tm.get('home',{}).get('name')} vs {tm.get('away',{}).get('name')}",
        "time": f"⏰ {_fmt_dt(fx.get('date',''))}",
        "market": best["market"],
        "pick_name": best["pick"],
        "odd": best["odd"],
        "pick": f"• {best['market']} → {best['pick']}: {best['odd']:.2f}"
    }

# ============ BUILD INPUT ============
def build_input_full(fixtures: List[Dict[str, Any]]) -> Dict[str, Any]:
    debug("Building full input")
    out = []
    for f in fixtures:
        fid = f.get("fixture", {}).get("id")
        odds_resp = fetch_odds(fid)
        odds_best = best_market_odds(odds_resp)
        out.append({"fixture": f.get("fixture"), "league": f.get("league"),
                    "teams": f.get("teams"), "odds": odds_best})
    debug(f"Full input built for {len(out)} fixtures")
    return {"fixtures": out}

# ============ TICKETS ============
def _product(nums: List[float]) -> float:
    p = 1.0
    for x in nums:
        p *= x
    return p

def _format_ticket(n: int, legs: List[Dict[str, Any]]) -> str:
    parts = [f"🎫 Ticket #{n}"]
    comps = []
    for L in legs:
        parts.extend([L["league"], f"🆔 {L['fid']}", L["teams"], L["time"], L["pick"], ""])
        comps.append(f"{L['odd']:.2f}")
    total = _product([l["odd"] for l in legs])
    parts.append(f"TOTAL ODDS: {' × '.join(comps)} = {total:.2f}")
    return "\n".join(parts)

def build_tickets(payload: Dict[str,Any], th: Dict[Tuple[str,str],float]) -> List[str]:
    debug("Building 4 tickets with adaptive thresholds")
    tickets = []
    th_current = dict(th)
    fixtures = payload.get("fixtures", [])

    for target in TARGETS:
        pool = []
        for f in fixtures:
            leg = best_leg_for_fixture(f, th_current)
            if leg:
                pool.append(leg)
        pool.sort(key=lambda x: x["odd"], reverse=True)
        debug(f"Candidate pool size {len(pool)} for target {target}")

        total = 1.0
        t_legs = []
        for leg in pool:
            if leg["fid"] in [l["fid"] for l in t_legs]:
                continue
            t_legs.append(leg)
            total *= leg["odd"]
            if total >= target:
                break
        ticket_txt = _format_ticket(len(tickets)+1, t_legs)
        tickets.append(ticket_txt)
        debug(f"Built ticket #{len(tickets)} with total odds {total:.2f}")
        th_current = {k: v + RELAX_ADD for k,v in th_current.items()}

    return tickets[:4]

# ============ TELEGRAM ============
def tg_send(text: str) -> None:
    debug(f"Sending message len={len(text)}")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    with httpx.Client(timeout=20) as c:
        for ch in TELEGRAM_CHANNELS:
            try:
                r = c.post(url, json={**payload, "chat_id": ch})
                debug(f"Sent to {ch}, status {r.status_code}")
            except Exception as e:
                debug(f"Failed to send to {ch}: {e}")

# ============ MAIN ============
def run(date_str: str):
    debug(f"Run started for {date_str}")
    fixtures = fetch_fixtures(date_str)
    if not fixtures:
        debug("No fixtures found.")
        return
    payload = build_input_full(fixtures)
    tickets = build_tickets(payload, BASE_TH)
    header = f"📅 {date_str} — NAKSIR ANALYST AI\nBTTS excluded. Formirano {len(tickets)} tiketa."
    tg_send(header)
    for i, t in enumerate(tickets, 1):
        tg_send(f"🎫 Ticket #{i}\n{t}")
    debug("Run complete.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD", default=None)
    args = parser.parse_args()
    date_str = args.date or datetime.now(TZ).strftime("%Y-%m-%d")
    run(date_str)

if __name__ == "__main__":
    main()
