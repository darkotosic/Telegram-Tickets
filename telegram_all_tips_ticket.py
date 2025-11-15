# telegram_all_tips_ticket.py
# Self-contained builder for two tickets + Telegram poster.
from __future__ import annotations

import os, json, time, random, re
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timezone
import httpx

# ===== ENV =====
API_BASE   = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
API_KEY    = os.getenv("API_FOOTBALL_KEY", "").strip() or os.getenv("X_APISPORTS_KEY", "").strip()
TIMEZONE   = os.getenv("TIMEZONE", "Europe/Belgrade")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL","gpt-4.1-mini").strip()

TELE_BOT   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

LEGS_MIN   = int(os.getenv("LEGS_MIN", "2"))
LEGS_MAX   = int(os.getenv("LEGS_MAX", "6"))
MAX_MATCHES= int(os.getenv("MAX_MATCHES", "180"))
QPS_DELAY  = float(os.getenv("QPS_DELAY", "0.35"))
DEBUG_ON   = os.getenv("DEBUG", "1") not in ("0","false","False","no","No")
QUIET      = os.getenv("QUIET","0") in ("1","true","True")

SKIP_STATUS= {"FT","AET","PEN","PST","CANC","ABD","AWD","WO","SUSP","INT"}

def dbg(msg: str):
    if DEBUG_ON and not QUIET:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[DBG] {now} | {msg}", flush=True)

def _sleep():
    time.sleep(QPS_DELAY + random.uniform(0.0, 0.08))

# ===== HTTP CORE =====
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
        r.raise_for_status()
        data = r.json()
        _http_cache[key] = data
        return data

def _fmt_dt_local(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str

# ===== Allow leagues =====
PREFERRED_LEAGUES: set[Tuple[str,str]] = {
    ("England","Premier League"),
    ("England","Championship"),
    ("France","Ligue 1"),
    ("Spain","La Liga"),
    ("Germany","Bundesliga"),
    ("Italy","Serie A"),
    ("Netherlands","Eredivisie"),
    ("Serbia","Super Liga"),
    ("Turkey","Super Lig"),
}

ALLOW_LIST_STATIC: set[int] = {
    2,3,913,5,536,808,960,10,667,29,30,31,32,37,33,34,848,311,310,342,218,144,315,71,
    169,210,346,233,39,40,41,42,703,244,245,61,62,78,79,197,271,164,323,135,136,389,
    88,89,408,103,104,106,94,283,235,286,287,322,140,141,113,207,208,202,203,909,268,269,270,340
}

def _leagues_search(name: str, country: Optional[str]) -> List[Dict[str, Any]]:
    data = _get("/leagues", {"search": name})
    out=[]
    for it in data.get("response",[]) or []:
        lg = it.get("league") or {}
        cc = (it.get("country") or {}).get("name")
        seasons = it.get("seasons") or []
        current = any(bool(s.get("current")) for s in seasons)
        if not current: 
            continue
        if country in (None,"World") or cc == country:
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

# ===== Markets =====
MARKET_BANDS = {
    ("Double Chance","1X"): (1.20, 1.35),
    ("Double Chance","X2"): (1.20, 1.35),
    ("Over/Under","Over 1.5"): (1.20, 1.35),
    ("Over/Under","Under 3.5"): (1.20, 1.40),
    ("Over/Under","Over 2.5"): (1.30, 1.55),
    ("Both Teams To Score","Yes"): (1.35, 1.55),
    ("Match Winner","Home"): (1.35, 1.55),
    ("Match Winner","Away"): (1.40, 1.60),
}
RELAXED_BANDS = {k: (max(1.10, lo-0.05), hi+0.25) for k,(lo,hi) in MARKET_BANDS.items()}

ALLOW_ALL_CAPS_HARD = {
    ("1st Half Goals","Over 0.5"): 1.35,
    ("Home Team Goals","Over 0.5"): 1.20,
    ("Away Team Goals","Over 0.5"): 1.25,
}
ALLOW_ALL_CAPS_RELAX = {
    ("1st Half Goals","Over 0.5"): 1.40,
    ("Home Team Goals","Over 0.5"): 1.25,
    ("Away Team Goals","Over 0.5"): 1.30,
}
MIN_T2_TOTAL = 1.85

FORBIDDEN_SUBSTRS = [
    "asian","alternative","corners","cards","booking","penalties","penalty",
    "offside","throw in","interval","race to","period","quarter","to qualify",
    "overtime","extra time","win to nil","clean sheet","anytime","player","scorer",
]

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

def _try_float(x: Any) -> Optional[float]:
    try:
        v=float(x); return v if v>0 else None
    except Exception:
        return None

def _collect_odds_table(items: list) -> Dict[int, Dict[str, Dict[str, float]]]:
    out: Dict[int, Dict[str, Dict[str,float]]] = {}
    for it in items or []:
        fx = (it.get("fixture") or {}).get("id") or (it.get("fixture") or {}).get("fixture")
        if not fx:
            continue
        fid = int(fx)
        slot = out.setdefault(fid, {})

        for bm in it.get("bookmakers",[]) or []:
            # new format
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

            # old format
            for bet in bm.get("bets",[]) or []:
                raw = (bet.get("name") or "").strip()
                if not raw:
                    continue

                # Filter noise
                if not _is_fulltime_main(raw):
                    continue

                # Map key markets
                def add(dst_name, value_name, odd):
                    dst = slot.setdefault(dst_name, {})
                    v = _try_float(odd)
                    if v is not None:
                        cur = dst.get(value_name)
                        if cur is None or v > cur:
                            dst[value_name] = v

                if raw in DOC_MARKETS["match_winner"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").strip()
                        if nm in ("Home","1"):
                            add("Match Winner","Home", val.get("odd"))
                        elif nm in ("Away","2"):
                            add("Match Winner","Away", val.get("odd"))
                    continue

                if raw in DOC_MARKETS["double_chance"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").replace(" ","").upper()
                        if nm in {"1X","X2","12"}:
                            add("Double Chance", nm, val.get("odd"))
                    continue

                if raw in DOC_MARKETS["btts"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").strip().title()
                        if nm in {"Yes","No"}:
                            add("Both Teams To Score", nm, val.get("odd"))
                    continue

                if raw in DOC_MARKETS["ou"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "").strip().title().replace("Over1.5","Over 1.5").replace("Under3.5","Under 3.5").replace("Over2.5","Over 2.5")
                        if nm in {"Over 1.5","Under 3.5","Over 2.5"}:
                            add("Over/Under", nm, val.get("odd"))
                    continue

                if raw in DOC_MARKETS["ou_1st"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "")
                        if re.search(r"(?i)over\\s*0\\.5", nm or ""):
                            add("1st Half Goals", "Over 0.5", val.get("odd"))
                    continue

                if raw in DOC_MARKETS["ttg_home"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "")
                        if re.search(r"(?i)over\\s*0\\.5", nm or ""):
                            add("Home Team Goals","Over 0.5", val.get("odd"))
                    continue

                if raw in DOC_MARKETS["ttg_away"]:
                    for val in bet.get("values",[]) or []:
                        nm=(val.get("value") or "")
                        if re.search(r"(?i)over\\s*0\\.5", nm or ""):
                            add("Away Team Goals","Over 0.5", val.get("odd"))
                    continue

    return out

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
    data = _get("/odds", {"fixture": fid})
    items = data.get("response", []) or []
    if items:
        table = _collect_odds_table(items)
        return table.get(fid) or {}
    if date_hint:
        return _odds_by_date(date_hint).get(fid) or {}
    return {}

def fixtures_by_date(date_str: str) -> List[Dict[str, Any]]:
    data = _get("/fixtures", {"date": date_str})
    resp = data.get("response") or []
    out = []
    skipped = 0
    for f in resp:
        fx = f.get("fixture",{}) or {}
        st = (fx.get("status") or {}).get("short","")
        if st in SKIP_STATUS:
            skipped += 1
            continue
        out.append(f)
        if len(out) >= MAX_MATCHES:
            break
    dbg(f"Fixtures: total={len(resp)} usable={len(out)} skipped={skipped}")
    return out

def _ticket_line(f: Dict[str,Any], pick: Tuple[str,str,float]) -> Dict[str,Any]:
    fx = f.get("fixture",{}) or {}
    lg = f.get("league",{}) or {}
    tm = f.get("teams",{}) or {}
    when = _fmt_dt_local(fx.get("date",""))
    home = (tm.get("home") or {}).get("name","")
    away = (tm.get("away") or {}).get("name","")
    mkt,name,odd = pick
    return {
        "league": f"🏟 {lg.get('country','')} — {lg.get('name','')}",
        "teams":  f"⚽ {home} vs {away}",
        "time":   f"⏰ {when}",
        "pick":   f"• {mkt} → {name}: {odd:.2f}",
        "odd": float(odd),
        "league_id": lg.get("id"),
    }

def _best_from_bands(odds_map: Dict[str, Dict[str, float]], bands: Dict[Tuple[str,str], Tuple[float,float]]):
    best=None; best_odd=0.0
    for (mkt,name),(lo,hi) in bands.items():
        v=(odds_map.get(mkt) or {}).get(name)
        if v is None: 
            continue
        v=float(v)
        if lo <= v <= hi and v > best_odd:
            best, best_odd = (mkt,name,v), v
    return best

def _best_from_caps(odds_map: Dict[str, Dict[str, float]], caps: Dict[Tuple[str,str], float]):
    best=None; best_odd=0.0
    for (mkt,name),cap in caps.items():
        v=(odds_map.get(mkt) or {}).get(name)
        if v is None: 
            continue
        v=float(v)
        if v <= cap and v > best_odd:
            best, best_odd = (mkt,name,v), v
    return best

def assemble_ticket1(date_str: str) -> Dict[str, Any]:
    fixtures = fixtures_by_date(date_str)
    allow_fixtures = [f for f in fixtures if (f.get("league") or {}).get("id") in ALLOW_IDS]
    dbg(f"T1 scan_order: prio={len(allow_fixtures)} rest={len(fixtures)-len(allow_fixtures)} total={len(fixtures)}")

    pool: List[Dict[str,Any]] = []
    for f in allow_fixtures:
        fid=int((f.get("fixture") or {}).get("id"))
        odds = odds_by_fixture(fid, date_str)
        p = _best_from_bands(odds, MARKET_BANDS)
        if p: pool.append(_ticket_line(f,p))

    if len(pool) < LEGS_MIN:
        for f in allow_fixtures:
            fid=int((f.get("fixture") or {}).get("id"))
            odds = odds_by_fixture(fid, date_str)
            p = _best_from_bands(odds, RELAXED_BANDS)
            if p: pool.append(_ticket_line(f,p))

    if len(pool) < LEGS_MIN:
        for f in fixtures:
            if f in allow_fixtures: continue
            fid=int((f.get("fixture") or {}).get("id"))
            odds = odds_by_fixture(fid, date_str)
            p = _best_from_bands(odds, MARKET_BANDS)
            if p: pool.append(_ticket_line(f,p))

    if len(pool) < LEGS_MIN:
        for f in fixtures:
            fid=int((f.get("fixture") or {}).get("id"))
            odds = odds_by_fixture(fid, date_str)
            p = _best_from_bands(odds, RELAXED_BANDS)
            if p: pool.append(_ticket_line(f,p))

    pool = sorted(pool, key=lambda L: L["odd"], reverse=True)

    ticket: List[Dict[str,Any]] = []
    total = 1.0
    for leg in pool:
        if len(ticket) >= LEGS_MAX: break
        ticket.append(leg); total *= leg["odd"]
        if len(ticket) >= LEGS_MIN and total >= 2.0: break

    if len(ticket) < LEGS_MIN:
        dbg("T1 not built: insufficient legs")
        return {"legs": [], "text": ""}

    lines = ["🎟 Ticket #1 — Stabilni bandovi", f"📅 {date_str}", ""]
    for l in ticket: lines += [l["league"], l["teams"], l["time"], l["pick"], ""]
    lines.append(f"📈 Ukupno: {total:.2f}")
    return {"legs": ticket, "text": "\n".join(lines)}

def assemble_ticket2_allow_all(date_str: str) -> Dict[str, Any]:
    fixtures = fixtures_by_date(date_str)

    pool: List[Dict[str,Any]] = []
    for f in fixtures:
        fid=int((f.get("fixture") or {}).get("id"))
        odds = odds_by_fixture(fid, date_str)
        p = _best_from_caps(odds, ALLOW_ALL_CAPS_HARD)
        if p: pool.append(_ticket_line(f,p))

    if not pool:
        for f in fixtures:
            fid=int((f.get("fixture") or {}).get("id"))
            odds = odds_by_fixture(fid, date_str)
            p = _best_from_caps(odds, ALLOW_ALL_CAPS_RELAX)
            if p: pool.append(_ticket_line(f,p))

    if not pool:
        dbg("T2 no legs found at all")
        return {"legs": [], "text": ""}

    pool = sorted(pool, key=lambda L: L["odd"])

    ticket: List[Dict[str,Any]] = []
    total = 1.0
    for leg in pool:
        if len(ticket) >= LEGS_MAX: break
        ticket.append(leg); total *= leg["odd"]
        if len(ticket) >= LEGS_MIN and total >= 1.85: break

    if total < 1.85 or len(ticket) < LEGS_MIN:
        for leg in sorted(pool, key=lambda L: L["odd"], reverse=True):
            if leg in ticket: continue
            if len(ticket) >= LEGS_MAX: break
            ticket.append(leg); total *= leg["odd"]
            if total >= 1.85 and len(ticket) >= LEGS_MIN: break

    if total < 1.85 or len(ticket) < LEGS_MIN:
        dbg(f"T2 not built: pool={len(pool)} legs={len(ticket)} total={total:.2f}")
        return {"legs": [], "text": ""}

    lines = ["🎟 Ticket #2 — Allow-all caps", f"📅 {date_str}", ""]
    for l in ticket: lines += [l["league"], l["teams"], l["time"], l["pick"], ""]
    lines.append(f"📈 Ukupno: {total:.2f}")
    return {"legs": ticket, "text": "\n".join(lines)}

# ===== OPENAI reasoning (optional; degrade gracefully) =====
def _reasoning_for(text: str) -> str:
    if not OPENAI_KEY:
        return "Model reasoning unavailable. OPENAI_API_KEY not set."
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        prompt = (
            "Give a concise analyst-style rationale for this soccer betslip. "
            "Explain the selection logic without claiming certainty. Keep it under 120 words.\\n\\n"
            + text
        )
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}],
            temperature=0.3,
            max_tokens=180,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM reasoning unavailable: {e}"

# ===== PUBLIC =====
def build_tickets_and_reasoning(date_str: Optional[str] = None, debug: bool = False) -> Tuple[List[str], List[str]]:
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dbg(f"=== build_tickets_and_reasoning date={date_str} ===")

    t1 = assemble_ticket1(date_str)
    t2 = assemble_ticket2_allow_all(date_str)

    tickets_texts: List[str] = [t["text"] for t in (t1,t2) if t.get("text")]
    reasonings: List[str] = [_reasoning_for(t) for t in tickets_texts]
    dbg(f"RESULT tickets={len(tickets_texts)}")
    return tickets_texts, reasonings

# ===== Telegram posting with expected signature =====
def _chunk_telegram(text: str, limit: int = 4096) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks=[]; buf=text
    while len(buf) > limit:
        cut = buf.rfind("\\n", 0, limit)
        if cut == -1: cut = limit
        chunks.append(buf[:cut].rstrip())
        buf = buf[cut:].lstrip("\\n")
    if buf: chunks.append(buf)
    return chunks

def post_to_telegram(message: str, channel: Optional[str] = None, *, token: Optional[str] = None) -> Tuple[bool, Dict]:
    token = token or TELE_BOT
    if not token:
        return False, {"error":"missing TELEGRAM_BOT_TOKEN"}
    chat_id = channel or os.getenv("TELEGRAM_DEFAULT_CHANNEL")
    if not chat_id:
        return False, {"error":"missing channel"}

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last={}
    with httpx.Client(timeout=30) as c:
        for i, part in enumerate(_chunk_telegram(message), 1):
            payload = {"chat_id": chat_id, "text": (f"[{i}/%d]\\n" % len(_chunk_telegram(message))) + part if i>1 else part,
                       "parse_mode":"HTML","disable_web_page_preview": True}
            r = c.post(url, json=payload)
            try:
                r.raise_for_status()
                last = r.json()
            except Exception as e:
                return False, {"error": str(e), "status": r.status_code, "body": r.text[:300]}
            time.sleep(0.5)
    return True, last
