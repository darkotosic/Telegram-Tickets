# telegram_all_tips_ticket.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os, sys, json, time, math, random, re
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timezone
import httpx

# ========================= ENV / CONFIG =========================
API_BASE   = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
API_KEY    = os.getenv("API_FOOTBALL_KEY", "").strip() or os.getenv("X_APISPORTS_KEY", "").strip()
TIMEZONE   = os.getenv("TIMEZONE", "Europe/Belgrade")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL","gpt-4.1-mini").strip()

TELE_BOT   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELE_CHANS = [c.strip() for c in os.getenv("TELEGRAM_CHANNELS","").split(",") if c.strip()]

LEGS_MIN   = int(os.getenv("LEGS_MIN", "2"))
LEGS_MAX   = int(os.getenv("LEGS_MAX", "6"))
MAX_MATCHES= int(os.getenv("MAX_MATCHES", "180"))
QPS_DELAY  = float(os.getenv("QPS_DELAY", "0.35"))
DEBUG_ON   = os.getenv("DEBUG", "1") not in ("0","false","False","no","No")
QUIET      = os.getenv("QUIET","0") in ("1","true","True")

# Ne preskačemo NS i LIVE, želimo i rano i kasnije ponuđene kvote
SKIP_STATUS= {"FT","AET","PEN","PST","CANC","ABD","AWD","WO","SUSP","INT"}

def dbg(msg: str):
    if DEBUG_ON and not QUIET:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[DBG] {now} | {msg}", flush=True)

def _sleep():
    time.sleep(QPS_DELAY + random.uniform(0.0, 0.08))

# ========================= HTTP CORE =========================
_http_cache: Dict[str, Any] = {}

def _client() -> httpx.Client:
    if not API_KEY:
        raise RuntimeError("Missing API_FOOTBALL_KEY or X_APISPORTS_KEY")
    return httpx.Client(base_url=API_BASE, headers={"x-apisports-key": API_KEY}, timeout=40)

def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    key = f"GET::{path}::{json.dumps(params, sort_keys=True)}"
    if key in _http_cache:
        return _http_cache[key]
    _sleep()
    with _client() as c:
        r = c.get(path, params=params)
        try:
            r.raise_for_status()
        except Exception:
            dbg(f"HTTP {path} params={params} status={r.status_code} text={r.text[:300]}")
            raise
        data = r.json()
        _http_cache[key] = data
        return data

def _fmt_dt_local(iso_str: str) -> str:
    # APIFOOTBALL vraća ISO sa Z; prikaz lokalizovan format
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
        # samo string bez realne tz konverzije, da ne uvodimo zoneinfo zavisnost u GH runneru
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str

# ========================= LIGE: prefer + fallback =========================
PREFERRED_LEAGUES: set[Tuple[str,str]] = {
    ("England", "Premier League"),
    ("England", "Championship"),
    ("England", "League One"),
    ("England", "League Two"),
    ("England", "National League"),
    ("France", "Ligue 1"),
    ("France", "Ligue 2"),
    ("Spain", "La Liga"),
    ("Spain", "La Liga 2"),
    ("Germany", "Bundesliga"),
    ("Germany", "2. Bundesliga"),
    ("Germany", "3. Liga"),
    ("Italy", "Serie A"),
    ("Italy", "Serie B"),
    ("Netherlands", "Eredivisie"),
    ("Netherlands", "Eerste Divisie"),
    ("Serbia", "Super Liga"),
    ("Turkey", "Super Lig"),
    ("Turkey", "1. Lig"),
    ("World", "UEFA Champions League"),
    ("World", "UEFA Europa League"),
    ("World", "UEFA Europa Conference League"),
    ("World", "UEFA Nations League"),
}

# Staticki backup (kad search ne vrati sve ili API promeni naziv)
ALLOW_LIST_STATIC: set[int] = {
    2,3,913,5,536,808,960,10,667,29,30,31,32,37,33,34,848,311,310,342,218,144,315,71,
    169,210,346,233,39,40,41,42,703,244,245,61,62,78,79,197,271,164,323,135,136,389,
    88,89,408,103,104,106,94,283,235,286,287,322,140,141,113,207,208,202,203,909,268,269,270,340
}

def _leagues_search(name: str, want_country: Optional[str]) -> List[Dict[str, Any]]:
    params = {"search": name}
    data = _get("/leagues", params)
    out = []
    for it in data.get("response", []) or []:
        lg = it.get("league") or {}
        cc = (it.get("country") or {}).get("name")
        seasons = it.get("seasons") or []
        current = any(bool(s.get("current")) for s in seasons)
        if not current:
            continue
        if want_country in (None, "World") or cc == want_country:
            out.append({"id": lg.get("id"), "name": lg.get("name"), "country": cc})
    return out

def resolve_allow_ids() -> set[int]:
    ids: set[int] = set()
    for country, name in sorted(PREFERRED_LEAGUES):
        res = _leagues_search(name, country)
        if not res:
            dbg(f"RESOLVE miss: {country} — {name}")
        for r in res:
            if r["id"]:
                ids.add(int(r["id"]))
    ids |= {int(x) for x in ALLOW_LIST_STATIC}
    dbg(f"ALLOW_IDS resolved total={len(ids)} sample={sorted(list(ids))[:40]}")
    return ids

ALLOW_IDS: set[int] = resolve_allow_ids()

# ========================= MARKETS i pragovi =========================
# T1: biramo iz "bandova" (interval [lo, hi]) za stabilno slaganje ka ~2.00
MARKET_BANDS: Dict[Tuple[str,str], Tuple[float,float]] = {
    ("Double Chance","1X"): (1.20, 1.45),
    ("Double Chance","X2"): (1.20, 1.45),
    ("Over/Under","Over 1.5"): (1.20, 1.55),
    ("Over/Under","Under 3.5"): (1.20, 1.60),
    ("Over/Under","Over 2.5"): (1.60, 2.20),
    ("Both Teams To Score","Yes"): (1.55, 2.05),
    ("Match Winner","Home"): (1.35, 1.80),
    ("Match Winner","Away"): (1.40, 1.90),
}
RELAXED_BANDS: Dict[Tuple[str,str], Tuple[float,float]] = {
    k: (max(1.10, lo - 0.05), hi + 0.25) for k,(lo,hi) in MARKET_BANDS.items()
}

# T2: cap-ovi (<= cap) prema zahtevu; target ukupno >= 1.85
ALLOW_ALL_CAPS_HARD: Dict[Tuple[str,str], float] = {
    ("1st Half Goals","Over 0.5"): 1.35,
    ("Home Team Goals","Over 0.5"): 1.20,
    ("Away Team Goals","Over 0.5"): 1.25,
}
ALLOW_ALL_CAPS_RELAX: Dict[Tuple[str,str], float] = {
    ("1st Half Goals","Over 0.5"): 1.40,
    ("Home Team Goals","Over 0.5"): 1.25,
    ("Away Team Goals","Over 0.5"): 1.30,
}
MIN_T2_TOTAL = 1.85

# ========================= Odds parser =========================
# API može vratiti 2 različita oblika:
# A) response[].bookmakers[].bets[].values[]  (stariji)
# B) response[].bookmakers[].markets[].outcomes[]  (noviji)
# Uzimamo maksimum po market->outcome.
FORBIDDEN_SUBSTRS = [
    "asian","alternative","corners","cards","booking","penalties","penalty",
    "offside","throw in","interval","race to","period","quarter",
    "draw no bet","dnb","to qualify","method of victory","overtime","extra time",
    "win to nil","clean sheet","anytime","player","scorer",
]

OU_EQUIV = {
    "Over1.5": "Over 1.5",
    "Under3.5": "Under 3.5",
    "Over2.5": "Over 2.5",
}

DOC_MARKETS = {
    "match_winner": {"Match Winner","1X2","Full Time Result","Result"},
    "double_chance": {"Double Chance","Double chance"},
    "btts": {"Both Teams To Score","Both teams to score","BTTS","Both Teams Score"},
    "ou": {"Goals Over/Under","Over/Under","Total Goals","Total","Goals Over Under","Total Goals Over/Under"},
    "ou_1st": {"1st Half - Over/Under","1st Half Goals Over/Under","First Half - Over/Under","Over/Under - 1st Half","First Half Goals","Goals Over/Under - 1st Half"},
    "ttg_home": {"Home Team Total Goals","Home Team Goals","Home Team - Total Goals","Home Total Goals"},
    "ttg_away": {"Away Team Total Goals","Away Team Goals","Away Team - Total Goals","Away Team Goals"},
    "ttg_generic": {"Team Total Goals","Total Team Goals","Team Goals"},
}

def _is_fulltime_main(name: str) -> bool:
    nl=(name or "").lower()
    return not any(b in nl for b in FORBIDDEN_SUBSTRS)

def _norm_ou_value(v: str) -> str:
    s=(v or "").strip().title()
    for k,rep in OU_EQUIV.items():
        s=s.replace(k, rep)
    s=re.sub(r"\s+"," ", s)
    return s

def _is_over05(val: str) -> bool:
    return re.search(r"(?i)\bover\s*0\.5\b", val or "") is not None

def _mentions_home(val: str) -> bool:
    return re.search(r"(?i)\bhome\b", val or "") is not None

def _mentions_away(val: str) -> bool:
    return re.search(r"(?i)\baway\b", val or "") is not None

def _try_float(x: Any) -> Optional[float]:
    try:
        v=float(x); return v if v>0 else None
    except Exception:
        return None

def _collect_odds_table(items: list) -> Dict[int, Dict[str, Dict[str, float]]]:
    """
    fixture_id -> market_name -> outcome_name -> max_odd
    Pokriva bets/values i markets/outcomes.
    """
    out: Dict[int, Dict[str, Dict[str,float]]] = {}
    for it in items or []:
        fx = (it.get("fixture") or {}).get("id") or (it.get("fixture") or {}).get("fixture")
        if not fx: 
            continue
        fid = int(fx)
        slot = out.setdefault(fid, {})

        for bm in it.get("bookmakers",[]) or []:
            # Noviji
            for m in bm.get("markets",[]) or []:
                mkt = (m.get("name") or "").strip()
                if not mkt: 
                    continue
                dst = slot.setdefault(mkt, {})
                for oc in m.get("outcomes",[]) or []:
                    name = (oc.get("name") or "").strip()
                    odd  = oc.get("price") if oc.get("price") is not None else oc.get("odd")
                    v = _try_float(odd)
                    if v is None: 
                        continue
                    if name and (name not in dst or v > dst[name]):
                        dst[name] = v

            # Stariji
            for bet in bm.get("bets",[]) or []:
                raw = (bet.get("name") or "").strip()
                if not raw:
                    continue

                # 1st half OU 0.5
                if raw in DOC_MARKETS["ou_1st"]:
                    dst = slot.setdefault("1st Half Goals", {})
                    for val in bet.get("values",[]) or []:
                        if _is_over05(val.get("value") or ""):
                            v=_try_float(val.get("odd"))
                            if v is not None:
                                prev=dst.get("Over 0.5")
                                if prev is None or v>prev:
                                    dst["Over 0.5"]=v
                    continue

                if not _is_fulltime_main(raw):
                    continue

                # Match winner
                if raw in DOC_MARKETS["match_winner"]:
                    dst = slot.setdefault("Match Winner", {})
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").strip()
                        if nm in ("Home","1"):
                            v=_try_float(val.get("odd")); 
                            if v is not None: dst["Home"]=max(dst.get("Home",0.0), v)
                        elif nm in ("Away","2"):
                            v=_try_float(val.get("odd"));
                            if v is not None: dst["Away"]=max(dst.get("Away",0.0), v)
                    continue

                # Double chance
                if raw in DOC_MARKETS["double_chance"]:
                    dst = slot.setdefault("Double Chance", {})
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").replace(" ","").upper()
                        if nm in {"1X","X2","12"}:
                            v=_try_float(val.get("odd"))
                            if v is not None: dst[nm]=max(dst.get(nm,0.0), v)
                    continue

                # BTTS
                if raw in DOC_MARKETS["btts"]:
                    dst = slot.setdefault("Both Teams To Score", {})
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").strip().title()
                        if nm in {"Yes","No"}:
                            v=_try_float(val.get("odd"))
                            if v is not None: dst[nm]=max(dst.get(nm,0.0), v)
                    continue

                # O/U FT
                if raw in DOC_MARKETS["ou"]:
                    dst = slot.setdefault("Over/Under", {})
                    for val in bet.get("values",[]) or []:
                        nm=_norm_ou_value(val.get("value") or "")
                        if nm in {"Over 1.5","Under 3.5","Over 2.5"}:
                            v=_try_float(val.get("odd"))
                            if v is not None: dst[nm]=max(dst.get(nm,0.0), v)
                    continue

                # Team total goals (home/away)
                if raw in DOC_MARKETS["ttg_home"]:
                    dst = slot.setdefault("Home Team Goals", {})
                    for val in bet.get("values",[]) or []:
                        if _is_over05(val.get("value") or ""):
                            v=_try_float(val.get("odd"))
                            if v is not None: dst["Over 0.5"]=max(dst.get("Over 0.5",0.0), v)
                    continue

                if raw in DOC_MARKETS["ttg_away"]:
                    dst = slot.setdefault("Away Team Goals", {})
                    for val in bet.get("values",[]) or []:
                        if _is_over05(val.get("value") or ""):
                            v=_try_float(val.get("odd"))
                            if v is not None: dst["Over 0.5"]=max(dst.get("Over 0.5",0.0), v)
                    continue

                if raw in DOC_MARKETS["ttg_generic"]:
                    # pokušaj da detektuje Home/Away iz value stringa
                    for val in bet.get("values",[]) or []:
                        vv=(val.get("value") or "").strip()
                        if _is_over05(vv):
                            if _mentions_home(vv):
                                dst = slot.setdefault("Home Team Goals", {})
                                v=_try_float(val.get("odd"))
                                if v is not None: dst["Over 0.5"]=max(dst.get("Over 0.5",0.0), v)
                            elif _mentions_away(vv):
                                dst = slot.setdefault("Away Team Goals", {})
                                v=_try_float(val.get("odd"))
                                if v is not None: dst["Over 0.5"]=max(dst.get("Over 0.5",0.0), v)
                    continue

    return out

# ========================= ODDS BY DATE FALLBACK =========================
_ODDS_BY_DATE_CACHE: dict[str, dict[int, dict[str, dict[str, float]]]] = {}

def _odds_by_date(date_str: str) -> dict[int, dict[str, dict[str, float]]]:
    if date_str in _ODDS_BY_DATE_CACHE:
        return _ODDS_BY_DATE_CACHE[date_str]
    data = _get("/odds", {"date": date_str})
    items = data.get("response", []) or []
    dbg(f"Odds-by-date: items={len(items)}")
    table = _collect_odds_table(items)
    _ODDS_BY_DATE_CACHE[date_str] = table
    return table

def odds_by_fixture(fid: int, date_hint: Optional[str]) -> Dict[str, Dict[str, float]]:
    # 1) pokušaj fixture
    data = _get("/odds", {"fixture": fid})
    items = data.get("response", []) or []
    if items:
        table = _collect_odds_table(items)
        m = table.get(fid) or {}
        dbg(f"Odds fid={fid}: fixture-markets={len(m)}")
        return m
    # 2) fallback date
    if date_hint:
        by_date = _odds_by_date(date_hint)
        m = by_date.get(fid) or {}
        dbg(f"Odds fid={fid}: via date={date_hint} markets={len(m)}")
        return m
    dbg(f"Odds fid={fid}: no markets")
    return {}

# ========================= FETCH FIXTURES =========================
def fixtures_by_date(date_str: str) -> List[Dict[str, Any]]:
    data = _get("/fixtures", {"date": date_str})
    resp = data.get("response") or []
    out = []
    skipped = 0
    for f in resp:
        fx = f.get("fixture",{}) or {}
        lg = f.get("league",{}) or {}
        st = (fx.get("status") or {}).get("short","")
        if st in SKIP_STATUS:
            skipped += 1
            continue
        # Ticket 1 preferira ALLOW_IDS, Ticket 2 može koristiti sve
        out.append(f)
        if len(out) >= MAX_MATCHES:
            break
    dbg(f"Fixtures: total={len(resp)} usable={len(out)} skipped={skipped}")
    return out

# ========================= UTIL =========================
def _product(vals: List[float]) -> float:
    p=1.0
    for v in vals: p*=v
    return p

def _ticket_line(f: Dict[str,Any], pick: Tuple[str,str,float]) -> Dict[str,Any]:
    fx = f.get("fixture",{}) or {}
    lg = f.get("league",{}) or {}
    tm = f.get("teams",{}) or {}
    fid = int(fx.get("id"))
    when = _fmt_dt_local(fx.get("date",""))
    home = (tm.get("home") or {}).get("name","")
    away = (tm.get("away") or {}).get("name","")
    mkt,name,odd = pick
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
        "country": (lg.get("country") or ""),
    }

def _pick_in_band(odds_map: Dict[str, Dict[str, float]], spec: Tuple[str,str], lo: float, hi: float):
    mkt,name = spec
    v = (odds_map.get(mkt) or {}).get(name)
    if v is None: return None
    v=float(v)
    if lo <= v <= hi: return (mkt,name,v)
    return None

def _best_from_bands(odds_map: Dict[str, Dict[str, float]], bands: Dict[Tuple[str,str], Tuple[float,float]]):
    best=None; best_odd=0.0
    for (mkt,name),(lo,hi) in bands.items():
        p=_pick_in_band(odds_map,(mkt,name),lo,hi)
        if p and p[2] > best_odd:
            best, best_odd = p, p[2]
    return best

def _best_from_caps(odds_map: Dict[str, Dict[str, float]], caps: Dict[Tuple[str,str], float]):
    best=None; best_odd=0.0
    for (mkt,name),cap in caps.items():
        v=(odds_map.get(mkt) or {}).get(name)
        if v is None: continue
        v=float(v)
        if v <= cap and v > best_odd:
            best, best_odd = (mkt,name,v), v
    return best

# ========================= ASSEMBLY: T1 i T2 =========================
def assemble_ticket1(date_str: str) -> Dict[str, Any]:
    # Ticket 1: samo iz ALLOW_IDS i MARKET_BANDS; ako nema, probaj RELAXED_BANDS
    fixtures = fixtures_by_date(date_str)
    # Prvo izdvoji samo allow-lige
    allow_fixtures = [f for f in fixtures if (f.get("league") or {}).get("id") in ALLOW_IDS]
    dbg(f"T1 scan_order: prio={len(allow_fixtures)} rest={len(fixtures)-len(allow_fixtures)} total={len(fixtures)}")

    pool: List[Dict[str,Any]] = []
    # 1) Prioritet: allow
    for f in allow_fixtures:
        fx=f.get("fixture",{}) or {}
        fid=int(fx.get("id"))
        odds = odds_by_fixture(fid, date_str)
        pick = _best_from_bands(odds, MARKET_BANDS)
        if pick:
            pool.append(_ticket_line(f, pick))

    # 2) Ako nedovoljno, probaj RELAXED_BANDS na allow
    if len(pool) < LEGS_MIN:
        for f in allow_fixtures:
            fx=f.get("fixture",{}) or {}
            fid=int(fx.get("id"))
            odds = odds_by_fixture(fid, date_str)
            pick = _best_from_bands(odds, RELAXED_BANDS)
            if pick:
                pool.append(_ticket_line(f, pick))

    # 3) Ako i dalje nema, pusti sve lige sa basic bandovima
    if len(pool) < LEGS_MIN:
        for f in fixtures:
            if f in allow_fixtures: 
                continue
            fx=f.get("fixture",{}) or {}
            fid=int(fx.get("id"))
            odds = odds_by_fixture(fid, date_str)
            pick = _best_from_bands(odds, MARKET_BANDS)
            if pick:
                pool.append(_ticket_line(f, pick))

    # 4) Kao poslednja šansa, sve lige + RELAXED_BANDS
    if len(pool) < LEGS_MIN:
        for f in fixtures:
            fx=f.get("fixture",{}) or {}
            fid=int(fx.get("id"))
            odds = odds_by_fixture(fid, date_str)
            pick = _best_from_bands(odds, RELAXED_BANDS)
            if pick:
                pool.append(_ticket_line(f, pick))

    pool = sorted(pool, key=lambda L: L["odd"], reverse=True)

    ticket: List[Dict[str,Any]] = []
    total = 1.0
    for leg in pool:
        # ne preteruj dužinu
        if len(ticket) >= LEGS_MAX:
            break
        trial_total = total * leg["odd"]
        ticket.append(leg)
        total = trial_total
        if len(ticket) >= LEGS_MIN and total >= 2.0:
            break

    if len(ticket) < LEGS_MIN:
        dbg("T1 not built: insufficient legs")
        return {"legs": [], "total": 1.0, "title": "🎟 Ticket #1 — band", "text": ""}

    text_lines = [
        "🎟 Ticket #1 — Stabilni bandovi",
        f"📅 {date_str}",
        "",
    ]
    for l in ticket:
        text_lines += [l["league"], l["teams"], l["time"], l["pick"], ""]
    text_lines.append(f"📈 Ukupno: {total:.2f}")
    body = "\n".join(text_lines)

    dbg(f"T1 built legs={len(ticket)} total={total:.2f}")
    return {"legs": ticket, "total": total, "title": "🎟 Ticket #1", "text": body}

def assemble_ticket2_allow_all(date_str: str) -> Dict[str, Any]:
    # Ticket 2: koristi samo cap-ove <= iz zahtevane liste; target ukupno ≥ 1.85
    fixtures = fixtures_by_date(date_str)

    # Kandidati iz HARD cap-ova
    pool_hard: List[Dict[str,Any]] = []
    for f in fixtures:
        fx=f.get("fixture",{}) or {}
        fid=int(fx.get("id"))
        odds = odds_by_fixture(fid, date_str)
        pick = _best_from_caps(odds, ALLOW_ALL_CAPS_HARD)
        if pick:
            pool_hard.append(_ticket_line(f, pick))

    # Ako nema, probaj RELAX caps
    pool: List[Dict[str,Any]] = pool_hard[:]
    if not pool:
        for f in fixtures:
            fx=f.get("fixture",{}) or {}
            fid=int(fx.get("id"))
            odds = odds_by_fixture(fid, date_str)
            pick = _best_from_caps(odds, ALLOW_ALL_CAPS_RELAX)
            if pick:
                pool.append(_ticket_line(f, pick))

    if not pool:
        dbg("T2 no legs found at all")
        return {"legs": [], "total": 1.0, "title": "🎟 Ticket #2 — allow-all", "text": ""}

    pool = sorted(pool, key=lambda L: L["odd"])  # za target 1.85 koristi niže kvote prvo

    ticket: List[Dict[str,Any]] = []
    total = 1.0
    for leg in pool:
        if len(ticket) >= LEGS_MAX:
            break
        ticket.append(leg)
        total *= leg["odd"]
        if len(ticket) >= LEGS_MIN and total >= MIN_T2_TOTAL:
            break

    if total < MIN_T2_TOTAL or len(ticket) < LEGS_MIN:
        # pokušaj dodavanja većih kvota iz ostatka kako bi gurnuo preko 1.85
        for leg in sorted(pool, key=lambda L: L["odd"], reverse=True):
            if leg in ticket:
                continue
            if len(ticket) >= LEGS_MAX:
                break
            ticket.append(leg)
            total *= leg["odd"]
            if total >= MIN_T2_TOTAL and len(ticket) >= LEGS_MIN:
                break

    if total < MIN_T2_TOTAL or len(ticket) < LEGS_MIN:
        dbg(f"T2 not built: pool={len(pool)} legs={len(ticket)} total={total:.2f}")
        return {"legs": [], "total": 1.0, "title": "🎟 Ticket #2 — allow-all", "text": ""}

    text_lines = [
        "🎟 Ticket #2 — Allow-all caps",
        f"📅 {date_str}",
        "",
    ]
    for l in ticket:
        text_lines += [l["league"], l["teams"], l["time"], l["pick"], ""]
    text_lines.append(f"📈 Ukupno: {total:.2f}")
    body = "\n".join(text_lines)

    dbg(f"T2 built legs={len(ticket)} total={total:.2f}")
    return {"legs": ticket, "total": total, "title": "🎟 Ticket #2", "text": body}

# ========================= PUBLIC API =========================
def build_tickets_and_reasoning(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Glavna funkcija koju poziva main.py
    Vraća: {"tickets":[{...},{...}], "messages":[str,...]}
    """
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dbg(f"=== build_tickets_and_reasoning date={date_str} ===")

    t1 = assemble_ticket1(date_str)
    t2 = assemble_ticket2_allow_all(date_str)

    tickets = [t for t in (t1, t2) if t.get("legs")]
    msgs = [t["text"] for t in tickets]
    dbg(f"RESULT tickets={len(tickets)}")
    return {"tickets": tickets, "messages": msgs}

# ========================= TELEGRAM =========================
def _telegram_send_text(bot_token: str, chat: str, text: str) -> Dict[str,Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    with httpx.Client(timeout=30) as c:
        r = c.post(url, json=payload)
        try:
            r.raise_for_status()
        except Exception:
            dbg(f"Telegram error chat={chat} status={r.status_code} text={r.text[:300]}")
            raise
        return r.json()

def post_to_telegram(messages: List[str], channels: Optional[List[str]] = None) -> List[Dict[str,Any]]:
    if channels is None or not channels:
        channels = TELE_CHANS
    if not TELE_BOT or not channels:
        dbg("Telegram posting skipped: missing bot token or channels")
        return []
    out=[]
    for m in messages:
        for ch in channels:
            try:
                out.append(_telegram_send_text(TELE_BOT, ch, m))
            except Exception as e:
                dbg(f"Telegram send failed to {ch}: {e}")
    return out

# ========================= CLI run (manual) =========================
if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    res = build_tickets_and_reasoning(date_arg)
    sent_payloads=[]
    if res["messages"]:
        sent_payloads = post_to_telegram(res["messages"], TELE_CHANS)
    summary = {"sent": [bool(x.get("ok")) for x in sent_payloads], "tickets": len(res["tickets"])}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
