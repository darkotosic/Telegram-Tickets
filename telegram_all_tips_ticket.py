# telegram_two_tickets.py
import os
import json
import time
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple, Optional

import httpx

API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.getenv("X_APISPORTS_KEY", "").strip()
TELEGRAM_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAN = os.getenv("TELEGRAM_CHANNEL", "").strip()

# ---- Runtime config ----
TZ = os.getenv("TZ", "Europe/Belgrade")
LEGS_MIN = int(os.getenv("LEGS_MIN", "2"))
LEGS_MAX = int(os.getenv("LEGS_MAX", "6"))
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "180"))  # gornja granica skeniranja mečeva po danu
QPS_DELAY = float(os.getenv("QPS_DELAY", "0.30"))   # dampening poziva prema API-ju

PUBLIC_DIR = os.path.join(os.getcwd(), "public")
os.makedirs(PUBLIC_DIR, exist_ok=True)

# statusi koje preskačemo
SKIP_STATUS = {"FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO", "SUSP", "INT"}

# ---- League allow list (prioriteti) ----
# prioritet 1 set: prvo prolazi kroz njih za Ticket #1
PRIO1_LEAGUES = {
    # country : set of league names as seen in API-Football
    "England": {"Premier League", "Championship", "League One", "League Two", "National League"},
    "France": {"Ligue 1", "Ligue 2"},
    "Spain": {"La Liga", "La Liga 2"},
    "Germany": {"Bundesliga", "2. Bundesliga", "3. Liga"},
    "Italy": {"Serie A", "Serie B"},
    "Netherlands": {"Eredivisie", "Eerste Divisie"},
    "Serbia": {"Super Liga"},
    "Turkey": {"Super Lig", "1. Lig"},
    "World": {
        "UEFA Champions League",
        "UEFA Europa League",
        "UEFA Europa Conference League",
        "UEFA Nations League"
    },
}

# ---- Ticket #2 tržišta i capovi kvota (<= cap) ----
ALLOW_ALL_CAPS = {
    ("1st Half Goals", "Over 0.5"): 1.35,
    ("Home Team Goals", "Over 0.5"): 1.20,
    ("Away Team Goals", "Over 0.5"): 1.25,
}

# ---- Ticket #1 kandidati tržišta i poželjni rasponi kvota ----
# Cilj: stabilan miks koji brže sklapa >=2.00 bez prenapregnutih kvota po nozi.
# Raspon (min_odd, max_odd). Biramo najbolju kvotu u okviru raspona.
MARKET_BANDS = {
    ("Double Chance", "1X"): (1.20, 1.45),
    ("Double Chance", "X2"): (1.20, 1.45),
    ("Over/Under", "Over 1.5"): (1.20, 1.55),
    ("Over/Under", "Under 3.5"): (1.20, 1.60),
    ("Over/Under", "Over 2.5"): (1.60, 2.20),
    ("Both Teams To Score", "Yes"): (1.55, 2.05),
    ("Match Winner", "Home"): (1.35, 1.80),
    ("Match Winner", "Away"): (1.40, 1.90),
}

# ---- simple memory cache ----
_http_cache: Dict[str, Any] = {}

def _sleep():
    time.sleep(QPS_DELAY + random.uniform(0.0, 0.08))

def _client() -> httpx.Client:
    headers = {"x-apisports-key": API_KEY}
    return httpx.Client(base_url=API_BASE, headers=headers, timeout=30)

def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("Missing X_APISPORTS_KEY")
    key = f"GET::{path}::{json.dumps(params, sort_keys=True)}"
    if key in _http_cache:
        return _http_cache[key]
    _sleep()
    with _client() as c:
        r = c.get(path, params=params)
        r.raise_for_status()
        data = r.json()
        _http_cache[key] = data
        return data

def _fmt_dt_local(iso_str: str) -> str:
    # API-Football vraća UTC ISO
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return iso_str
    # Bez zavisnosti za time zone. Prikaži utc kao HH:MM.
    return dt.strftime("%Y-%m-%d %H:%M")

def fixtures_by_date(date_str: str) -> List[Dict[str, Any]]:
    data = _get("/fixtures", {"date": date_str})
    resp = data.get("response") or []
    out = []
    for f in resp:
        fx = f.get("fixture", {}) or {}
        st = (fx.get("status") or {}).get("short", "")
        if st in SKIP_STATUS:
            continue
        out.append(f)
        if len(out) >= MAX_MATCHES:
            break
    return out

def odds_by_fixture(fid: int) -> Dict[str, Any]:
    data = _get("/odds", {"fixture": fid})
    # strukturom: response -> list, svaka ima 'bookmakers' -> markets -> outcomes
    # vrati kao pomoćna mapa: { market_name: { outcome_name: odd } }
    out: Dict[str, Dict[str, float]] = {}
    for item in data.get("response", []):
        for bm in item.get("bookmakers", []):
            for m in bm.get("markets", []):
                mkt_name = m.get("name")
                if not mkt_name:
                    continue
                for oc in m.get("outcomes", []):
                    oname = oc.get("name")
                    odd = oc.get("price") or oc.get("odd")
                    try:
                        odd = float(odd)
                    except Exception:
                        continue
                    out.setdefault(mkt_name, {})
                    prev = out[mkt_name].get(oname)
                    # uzmi najbolju kvotu među bookmakerima
                    if prev is None or odd > prev:
                        out[mkt_name][oname] = odd
    return out

def _market_pick_within_band(odds_map: Dict[str, Dict[str, float]],
                             band_spec: Tuple[str, str],
                             lo: float, hi: float) -> Optional[Tuple[str, str, float]]:
    mkt, name = band_spec
    val = (odds_map.get(mkt) or {}).get(name)
    if val is None:
        return None
    if lo <= val <= hi:
        return (mkt, name, float(val))
    return None

def _best_from_allowed_bands(odds_map: Dict[str, Dict[str, float]]) -> Optional[Tuple[str, str, float]]:
    best = None
    best_odd = 0.0
    # blaga preferencija ka višoj kvoti u odobrenom rasponu
    for (mkt, name), (lo, hi) in MARKET_BANDS.items():
        pick = _market_pick_within_band(odds_map, (mkt, name), lo, hi)
        if pick and pick[2] > best_odd:
            best = pick
            best_odd = pick[2]
    return best

def _best_from_caps(odds_map: Dict[str, Dict[str, float]]) -> Optional[Tuple[str, str, float]]:
    # samo tri tržišta sa cap-om; izaberi najvišu kvotu koja je <= cap
    best = None
    best_odd = 0.0
    for (mkt, name), cap in ALLOW_ALL_CAPS.items():
        val = (odds_map.get(mkt) or {}).get(name)
        if val is None:
            continue
        v = float(val)
        if v <= cap and v > best_odd:
            best = (mkt, name, v)
            best_odd = v
    return best

def _league_in_prio1(country: str, league: str) -> bool:
    s = PRIO1_LEAGUES.get(country)
    return bool(s and league in s)

def _ticket_line(f: Dict[str, Any], pick: Tuple[str, str, float]) -> Dict[str, Any]:
    fx = f.get("fixture", {}) or {}
    lg = f.get("league", {}) or {}
    tm = f.get("teams", {}) or {}
    fid = int(fx.get("id"))
    when = _fmt_dt_local(fx.get("date", ""))
    home = (tm.get("home") or {}).get("name", "")
    away = (tm.get("away") or {}).get("name", "")
    mkt, name, odd = pick
    return {
        "fid": fid,
        "league": f"🏟 {lg.get('country','')} — {lg.get('name','')}",
        "teams": f"⚽ {home} vs {away}",
        "time": f"⏰ {when}",
        "market": mkt,
        "pick_name": name,
        "odd": float(odd),
        "pick": f"• {mkt} → {name}: {odd:.2f}",
    }

def assemble_legs_ticket1(date_str: str) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []
    # prvo prioritetne lige
    all_fx = fixtures_by_date(date_str)
    prio = [f for f in all_fx if _league_in_prio1((f.get("league") or {}).get("country",""),
                                                  (f.get("league") or {}).get("name",""))]
    rest = [f for f in all_fx if f not in prio]
    scan_order = prio + rest
    for f in scan_order:
        fid = int((f.get("fixture") or {}).get("id", 0))
        if not fid:
            continue
        o = odds_by_fixture(fid)
        pick = _best_from_allowed_bands(o)
        if pick:
            legs.append(_ticket_line(f, pick))
    # sortiraj opadajuće po kvoti da brže sastavi cilj
    legs.sort(key=lambda L: L["odd"], reverse=True)
    return legs

def assemble_legs_ticket2(date_str: str) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []
    for f in fixtures_by_date(date_str):
        fid = int((f.get("fixture") or {}).get("id", 0))
        if not fid:
            continue
        o = odds_by_fixture(fid)
        pick = _best_from_caps(o)
        if pick:
            legs.append(_ticket_line(f, pick))
    legs.sort(key=lambda L: L["odd"], reverse=True)
    return legs

def _build_ticket(idx: int, target: float, pool: List[Dict[str, Any]],
                  used: set) -> Optional[Tuple[str, List[int], List[Dict[str, Any]]]]:
    t: List[Dict[str, Any]] = []
    total = 1.0
    for L in pool:
        if L["fid"] in used:
            continue
        t.append(L)
        total *= L["odd"]
        # ukloni višak nogu ako pređemo LEGS_MAX
        while len(t) > LEGS_MAX:
            # heuristika: izbaci najmanju kvotu
            t.sort(key=lambda x: x["odd"])
            t.pop(0)
            total = math.prod([x["odd"] for x in t]) if t else 1.0

        if len(t) >= LEGS_MIN and total >= target:
            txt = _format_ticket(idx, t, total)
            used.update(x["fid"] for x in t)
            return txt, [x["fid"] for x in t], t
    return None

def _format_ticket(idx: int, legs: List[Dict[str, Any]], total: float) -> str:
    head = f"🎫 Ticket #{idx}\n"
    lines = []
    for L in legs:
        lines.append("\n".join([
            L["league"],
            f"🆔 {L['fid']}",
            L["teams"],
            L["time"],
            L["pick"],
        ]))
    body = "\n\n".join(lines)
    tail = f"\n\n🧮 Total odds: {total:.2f}"
    return head + body + tail

def build_two_tickets(date_str: str) -> Dict[str, Any]:
    used: set = set()
    tickets_payload: List[Dict[str, Any]] = []

    # Ticket #1: cilj 2.00 iz allow-list prioritetnih liga, zatim ostatak
    legs1 = assemble_legs_ticket1(date_str)
    r1 = _build_ticket(1, 2.00, legs1, used)
    if r1:
        txt, fids, tlegs = r1
        tickets_payload.append({
            "ticket": 1,
            "target": 2.00,
            "total_odds": round(math.prod([x["odd"] for x in tlegs]), 2),
            "legs": tlegs,
            "text": txt,
        })

    # Ticket #2: cilj 1.85 iz allow-all sa capovima
    legs2 = assemble_legs_ticket2(date_str)
    # izbegni duplikate
    legs2 = [L for L in legs2 if L["fid"] not in used]
    r2 = _build_ticket(2, 1.85, legs2, used)
    if r2:
        txt, fids, tlegs = r2
        tickets_payload.append({
            "ticket": 2,
            "target": 1.85,
            "total_odds": round(math.prod([x["odd"] for x in tlegs]), 2),
            "legs": tlegs,
            "text": txt,
        })

    return {
        "status": "ok",
        "date": date_str,
        "count": len(tickets_payload),
        "tickets": tickets_payload,
    }

def write_public(payload: Dict[str, Any]) -> str:
    fp = os.path.join(PUBLIC_DIR, "tickets.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return fp

def send_telegram(text: str) -> Dict[str, Any]:
    if not TELEGRAM_BOT or not TELEGRAM_CHAN:
        return {"ok": False, "reason": "telegram not configured"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage"
    _sleep()
    with httpx.Client(timeout=30) as c:
        r = c.post(url, data={"chat_id": TELEGRAM_CHAN, "text": text})
        try:
            return {"ok": r.status_code == 200, "resp": r.json()}
        except Exception:
            return {"ok": False, "status": r.status_code, "text": r.text}

def main():
    # datum iz env-a ili danas
    date_str = os.getenv("RUN_DATE")
    if not date_str:
        # koristi današnji UTC datum jer API očekuje ISO date
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    payload = build_two_tickets(date_str)
    out_fp = write_public(payload)

    results = []
    for t in payload.get("tickets", []):
        res = send_telegram(t["text"])
        results.append({"ticket": t["ticket"], **res})

    print(json.dumps({
        "status": payload["status"],
        "date": payload["date"],
        "tickets_written": out_fp,
        "tickets": payload.get("count", 0),
        "telegram": results,
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
