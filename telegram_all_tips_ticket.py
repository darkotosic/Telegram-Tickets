# telegram_all_tips_ticket.py
from __future__ import annotations
import os, json, time, math, random
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timezone

import httpx

# ====== ENV / CONFIG ======
API_BASE = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()  # koristi postojeći secret naziv
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TELEGRAM_BOT = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")
LEGS_MIN = int(os.getenv("LEGS_MIN", "2"))
LEGS_MAX = int(os.getenv("LEGS_MAX", "6"))
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "180"))
QPS_DELAY = float(os.getenv("QPS_DELAY", "0.30"))

# statusi koje preskačemo
SKIP_STATUS = {"FT","AET","PEN","PST","CANC","ABD","AWD","WO","SUSP","INT"}

# prioritetne lige za Ticket #1
PRIO1_LEAGUES = {
    "England": {"Premier League","Championship","League One","League Two","National League"},
    "France": {"Ligue 1","Ligue 2"},
    "Spain": {"La Liga","La Liga 2"},
    "Germany": {"Bundesliga","2. Bundesliga","3. Liga"},
    "Italy": {"Serie A","Serie B"},
    "Netherlands": {"Eredivisie","Eerste Divisie"},
    "Serbia": {"Super Liga"},
    "Turkey": {"Super Lig","1. Lig"},
    "World": {
        "UEFA Champions League","UEFA Europa League",
        "UEFA Europa Conference League","UEFA Nations League"
    },
}

# market “bandovi” za Ticket #1: (min,max) raspon po legu
MARKET_BANDS = {
    ("Double Chance","1X"): (1.20, 1.45),
    ("Double Chance","X2"): (1.20, 1.45),
    ("Over/Under","Over 1.5"): (1.20, 1.55),
    ("Over/Under","Under 3.5"): (1.20, 1.60),
    ("Over/Under","Over 2.5"): (1.60, 2.20),
    ("Both Teams To Score","Yes"): (1.55, 2.05),
    ("Match Winner","Home"): (1.35, 1.80),
    ("Match Winner","Away"): (1.40, 1.90),
}

# Ticket #2: dozvoljena tržišta i cap kvote (<= cap)
ALLOW_ALL_CAPS = {
    ("1st Half Goals","Over 0.5"): 1.35,
    ("Home Team Goals","Over 0.5"): 1.20,
    ("Away Team Goals","Over 0.5"): 1.25,
}

# ====== HTTP helpers ======
_http_cache: Dict[str, Any] = {}

def _sleep():
    time.sleep(QPS_DELAY + random.uniform(0.0, 0.08))

def _client() -> httpx.Client:
    if not API_KEY:
        raise RuntimeError("Missing API_FOOTBALL_KEY")
    return httpx.Client(base_url=API_BASE, headers={"x-apisports-key": API_KEY}, timeout=35)

def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
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
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
    except Exception:
        return iso_str
    return dt.strftime("%Y-%m-%d %H:%M")

# ====== API-Football fetch ======
def fixtures_by_date(date_str: str) -> List[Dict[str, Any]]:
    resp = _get("/fixtures", {"date": date_str}).get("response") or []
    out = []
    for f in resp:
        st = (f.get("fixture",{}).get("status") or {}).get("short","")
        if st in SKIP_STATUS: 
            continue
        out.append(f)
        if len(out) >= MAX_MATCHES: 
            break
    return out

def odds_by_fixture(fid: int) -> Dict[str, Dict[str, float]]:
    # map: market -> outcome -> best_odd
    out: Dict[str, Dict[str, float]] = {}
    for item in _get("/odds", {"fixture": fid}).get("response", []):
        for bm in item.get("bookmakers", []):
            for m in bm.get("markets", []):
                mkt = m.get("name")
                if not mkt: continue
                for oc in m.get("outcomes", []):
                    oname = oc.get("name")
                    odd = oc.get("price") or oc.get("odd")
                    try:
                        v = float(odd)
                    except Exception:
                        continue
                    prev = out.setdefault(mkt, {}).get(oname)
                    if prev is None or v > prev:
                        out[mkt][oname] = v
    return out

def _league_in_prio1(country: str, league: str) -> bool:
    s = PRIO1_LEAGUES.get(country)
    return bool(s and league in s)

# ====== pick logic ======
def _pick_in_band(odds_map: Dict[str, Dict[str, float]], spec: Tuple[str,str], lo: float, hi: float):
    mkt, name = spec
    v = (odds_map.get(mkt) or {}).get(name)
    if v is None: 
        return None
    v = float(v)
    if lo <= v <= hi:
        return (mkt, name, v)
    return None

def _best_from_bands(odds_map: Dict[str, Dict[str, float]]):
    best = None; best_odd = 0.0
    for (mkt,name),(lo,hi) in MARKET_BANDS.items():
        p = _pick_in_band(odds_map, (mkt,name), lo, hi)
        if p and p[2] > best_odd:
            best, best_odd = p, p[2]
    return best

def _best_from_caps(odds_map: Dict[str, Dict[str, float]]):
    best = None; best_odd = 0.0
    for (mkt,name),cap in ALLOW_ALL_CAPS.items():
        v = (odds_map.get(mkt) or {}).get(name)
        if v is None: 
            continue
        v = float(v)
        if v <= cap and v > best_odd:
            best, best_odd = (mkt,name,v), v
    return best

def _ticket_line(f: Dict[str,Any], pick: Tuple[str,str,float]) -> Dict[str,Any]:
    fx = f.get("fixture",{}) or {}
    lg = f.get("league",{}) or {}
    tm = f.get("teams",{}) or {}
    fid = int(fx.get("id"))
    when = _fmt_dt_local(fx.get("date",""))
    home = (tm.get("home") or {}).get("name","")
    away = (tm.get("away") or {}).get("name","")
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
        "league_id": lg.get("id"),
        "season": lg.get("season"),
        "home_id": (tm.get("home") or {}).get("id"),
        "away_id": (tm.get("away") or {}).get("id"),
    }

def _sort_desc_by_odd(legs: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    return sorted(legs, key=lambda L: L["odd"], reverse=True)

def _build_ticket(idx: int, target: float, pool: List[Dict[str,Any]], used: set):
    legs: List[Dict[str,Any]] = []
    total = 1.0
    for L in pool:
        if L["fid"] in used: 
            continue
        legs.append(L)
        total *= L["odd"]

        while len(legs) > LEGS_MAX:
            legs.sort(key=lambda x: x["odd"])
            legs.pop(0)
            total = math.prod([x["odd"] for x in legs]) if legs else 1.0

        if len(legs) >= LEGS_MIN and total >= target:
            used.update(x["fid"] for x in legs)
            return legs, round(total, 2)
    return None, 1.0

# ====== legs assemblers ======
def assemble_ticket1_legs(date_str: str) -> List[Dict[str,Any]]:
    all_fx = fixtures_by_date(date_str)
    prio = [f for f in all_fx if _league_in_prio1((f.get("league") or {}).get("country",""),
                                                  (f.get("league") or {}).get("name",""))]
    rest = [f for f in all_fx if f not in prio]
    scan = prio + rest
    legs: List[Dict[str,Any]] = []
    for f in scan:
        fid = int((f.get("fixture") or {}).get("id",0)) or 0
        if not fid: 
            continue
        o = odds_by_fixture(fid)
        p = _best_from_bands(o)
        if p:
            legs.append(_ticket_line(f, p))
    return _sort_desc_by_odd(legs)

def assemble_ticket2_legs(date_str: str) -> List[Dict[str,Any]]:
    legs: List[Dict[str,Any]] = []
    for f in fixtures_by_date(date_str):
        fid = int((f.get("fixture") or {}).get("id",0)) or 0
        if not fid: 
            continue
        o = odds_by_fixture(fid)
        p = _best_from_caps(o)
        if p:
            legs.append(_ticket_line(f, p))
    return _sort_desc_by_odd(legs)

# ====== lightweight stats for reasoning ======
def _team_stats(league_id: int, season: int, team_id: int) -> Dict[str,Any]:
    try:
        resp = _get("/teams/statistics", {"league": league_id, "season": season, "team": team_id})
        s = resp.get("response") or {}
        return {
            "form": (s.get("form") or "")[:10],
            "played": (s.get("fixtures") or {}).get("played", {}).get("total"),
            "wins": (s.get("fixtures") or {}).get("wins", {}).get("total"),
            "draws": (s.get("fixtures") or {}).get("draws", {}).get("total"),
            "loses": (s.get("fixtures") or {}).get("loses", {}).get("total"),
            "goals_for_avg": (s.get("goals") or {}).get("for", {}).get("average", {}).get("total"),
            "goals_against_avg": (s.get("goals") or {}).get("against", {}).get("average", {}).get("total"),
            "btts_pct": (s.get("both_teams_to_score") or {}).get("total"),
            "clean_sheet_pct": (s.get("clean_sheet") or {}).get("total"),
        }
    except Exception:
        return {}

def _reasoning_openai(ticket_title: str, legs: List[Dict[str,Any]]) -> str:
    # Ako nema ključa, daj deterministički fallback
    if not OPENAI_KEY:
        bullets = []
        for L in legs:
            bullets.append(f"- {L['teams']} — {L['market']} {L['pick_name']} @ {L['odd']:.2f}")
        return "Key drivers:\n" + "\n".join(bullets)

    # pripremi kratke numerike za oba tima
    facts = []
    for L in legs:
        lg_id = L.get("league_id"); season = L.get("season")
        h = L.get("home_id"); a = L.get("away_id")
        hs = _team_stats(lg_id, season, h) if lg_id and season and h else {}
        as_ = _team_stats(lg_id, season, a) if lg_id and season and a else {}
        facts.append({
            "fixture": L["teams"],
            "market": L["market"],
            "pick": L["pick_name"],
            "odd": L["odd"],
            "home_stats": hs, "away_stats": as_,
        })

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_KEY)
    prompt = (
        "Write concise, non-promotional betting reasoning for a daily ticket. "
        "Use neutral language and avoid certainty. Use bullets. 3–6 bullets total. "
        "Use only the provided facts. No emojis."
    )
    content = {"ticket": ticket_title, "facts": facts}
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content": prompt},
                {"role":"user","content": json.dumps(content, ensure_ascii=False)},
            ],
            max_tokens=260,
            temperature=0.3,
        )
        txt = r.choices[0].message.content.strip()
        return txt
    except Exception:
        # siguran fallback
        bullets = []
        for L in legs:
            bullets.append(f"- {L['teams']} — {L['market']} {L['pick_name']} @ {L['odd']:.2f}")
        return "Key drivers:\n" + "\n".join(bullets)

# ====== public API (za main.py) ======
def build_tickets_and_reasoning(date_str: str) -> Tuple[List[str], List[str]]:
    used: set[int] = set()
    tickets_texts: List[str] = []
    reasonings: List[str] = []

    # Ticket #1 => target 2.00
    legs1 = assemble_ticket1_legs(date_str)
    t1, tot1 = _build_ticket(1, 2.00, legs1, used)
    if t1:
        txt1 = _format_ticket_text(1, t1, tot1)
        tickets_texts.append(txt1)
        reasonings.append(_reasoning_openai("Ticket #1", t1))

    # Ticket #2 => target 1.85
    legs2_all = assemble_ticket2_legs(date_str)
    legs2 = [L for L in legs2_all if L["fid"] not in used]
    t2, tot2 = _build_ticket(2, 1.85, legs2, used)
    if t2:
        txt2 = _format_ticket_text(2, t2, tot2)
        tickets_texts.append(txt2)
        reasonings.append(_reasoning_openai("Ticket #2", t2))

    return tickets_texts, reasonings

def _format_ticket_text(idx: int, legs: List[Dict[str,Any]], total: float) -> str:
    head = f"🎫 Ticket #{idx}\n"
    lines = []
    for L in legs:
        lines.append("\n".join([
            L["league"], f"🆔 {L['fid']}",
            L["teams"], L["time"], L["pick"],
        ]))
    body = "\n\n".join(lines)
    tail = f"\n\n🧮 Total odds: {total:.2f}"
    return head + body + tail

def post_to_telegram(message: str, channel: str) -> Tuple[bool, Dict[str,Any]]:
    if not TELEGRAM_BOT:
        return False, {"reason":"TELEGRAM_BOT_TOKEN not configured"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT}/sendMessage"
    _sleep()
    with httpx.Client(timeout=30) as c:
        r = c.post(url, data={"chat_id": channel, "text": message})
        try:
            return r.status_code == 200, r.json()
        except Exception:
            return False, {"status": r.status_code, "text": r.text}
