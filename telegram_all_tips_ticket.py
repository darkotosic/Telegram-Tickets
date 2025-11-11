# telegram_all_tips_ticket.py (dynamic allow + odds-by-date fallback + debug)
from __future__ import annotations
import os, json, time, math, random
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime

import httpx

# ================= ENV / CONFIG =================
API_BASE   = "https://v3.football.api-sports.io"
API_KEY    = os.getenv("API_FOOTBALL_KEY", "").strip() or os.getenv("X_APISPORTS_KEY", "").strip()
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TELE_BOT   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

TIMEZONE   = os.getenv("TIMEZONE", "Europe/Belgrade")
LEGS_MIN   = int(os.getenv("LEGS_MIN", "2"))
LEGS_MAX   = int(os.getenv("LEGS_MAX", "6"))
MAX_MATCHES= int(os.getenv("MAX_MATCHES", "180"))
QPS_DELAY  = float(os.getenv("QPS_DELAY", "0.35"))
DEBUG_ON   = os.getenv("DEBUG", "1") not in ("0","false","False","no","No")

SKIP_STATUS= {"FT","AET","PEN","PST","CANC","ABD","AWD","WO","SUSP","INT"}  # ne preskačemo NS i LIVE

# ================= DEBUG =================
def dbg(msg: str):
    if DEBUG_ON:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"[DBG] {now} | {msg}", flush=True)

def _sleep():
    time.sleep(QPS_DELAY + random.uniform(0.0, 0.08))

# ================= HTTP CORE =================
_http_cache: Dict[str, Any] = {}

def _client() -> httpx.Client:
    if not API_KEY:
        raise RuntimeError("Missing API_FOOTBALL_KEY or X_APISPORTS_KEY")
    return httpx.Client(base_url=API_BASE, headers={"x-apisports-key": API_KEY}, timeout=35)

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
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z","+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str

# ================= PREFERENCES =================
# 1) “Po imenu i državi” -> dinamički resolve u league.id
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

# 2) Statički backup skup (ako API promeni nazive ili search ne vrati sve)
ALLOW_LIST_STATIC: set[int] = {
    2,3,913,5,536,808,960,10,667,29,30,31,32,37,33,34,848,311,310,342,218,144,315,71,
    169,210,346,233,39,40,41,42,703,244,245,61,62,78,79,197,271,164,323,135,136,389,
    88,89,408,103,104,106,94,283,235,286,287,322,140,141,113,207,208,202,203,909,268,269,270,340
}

def _leagues_search(name: str, country: Optional[str]) -> List[Dict[str, Any]]:
    # /leagues?search=Name -> filtriramo po country i current sezoni
    params = {"search": name}
    data = _get("/leagues", params)
    out = []
    for it in data.get("response", []) or []:
        lg = it.get("league") or {}
        cc = (it.get("country") or {}).get("name")
        seasons = it.get("seasons") or []
        current = any(s.get("current") for s in seasons)
        # Ako country == "World" dozvoli sve zemlje za UEFA takmičenja
        if current and (country is None or cc == country or country == "World"):
            out.append({"id": lg.get("id"), "name": lg.get("name"), "country": cc})
    return out

def resolve_allow_ids() -> set[int]:
    ids: set[int] = set()
    for country, name in sorted(PREFERRED_LEAGUES):
        results = _leagues_search(name, None if country == "World" else country)
        if not results:
            dbg(f"RESOLVE miss: {country} — {name}")
        for r in results:
            if r["id"]:
                ids.add(int(r["id"]))
    ids |= {int(x) for x in ALLOW_LIST_STATIC}
    dbg(f"ALLOW_IDS resolved total={len(ids)} sample={sorted(list(ids))[:40]}")
    return ids

ALLOW_IDS: set[int] = resolve_allow_ids()

# ================= MARKETS SETUP =================
# Ticket #1: “bandovi” (min,max) za stabilno slaganje ≥ 2.00
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

# Ticket #2: cap-ovi (≤ cap) za target ≥ 1.85
ALLOW_ALL_CAPS: Dict[Tuple[str,str], float] = {
    ("1st Half Goals","Over 0.5"): 1.35,
    ("Home Team Goals","Over 0.5"): 1.20,
    ("Away Team Goals","Over 0.5"): 1.25,
}
RELAXED_CAPS: Dict[Tuple[str,str], float] = {
    ("1st Half Goals","Over 0.5"): 1.40,
    ("Home Team Goals","Over 0.5"): 1.25,
    ("Away Team Goals","Over 0.5"): 1.30,
}

# ================= ODDS BY DATE FALLBACK =================
# Globalni keš za /odds?date=YYYY-MM-DD
_ODDS_BY_DATE_CACHE: dict[str, dict[int, dict[str, dict[str, float]]]] = {}

def _collect_odds_from_items(items: list) -> dict[int, dict[str, dict[str, float]]]:
    """
    Mapira: fixture_id -> market_name -> outcome_name -> max_odd
    """
    out: dict[int, dict[str, dict[str, float]]] = {}
    for item in items or []:
        fixture = (item.get("fixture") or {}).get("id")
        if not fixture:
            continue
        f = int(fixture)
        dst = out.setdefault(f, {})
        for bm in item.get("bookmakers", []) or []:
            for m in bm.get("markets", []) or []:
                mkt = m.get("name")
                if not mkt:
                    continue
                slot = dst.setdefault(mkt, {})
                for oc in m.get("outcomes", []) or []:
                    name = oc.get("name")
                    odd  = oc.get("price") or oc.get("odd")
                    try:
                        v = float(odd)
                    except Exception:
                        continue
                    prev = slot.get(name)
                    if prev is None or v > prev:
                        slot[name] = v
    return out

def _odds_by_date(date_str: str) -> dict[int, dict[str, dict[str, float]]]:
    """
    Učitaj sve kvote za jedan dan. Keširano po datumu.
    """
    if date_str in _ODDS_BY_DATE_CACHE:
        return _ODDS_BY_DATE_CACHE[date_str]
    data = _get("/odds", {"date": date_str})
    items = data.get("response", []) or []
    dbg(f"Odds-by-date: items={len(items)}")
    table = _collect_odds_from_items(items)
    _ODDS_BY_DATE_CACHE[date_str] = table
    return table

# ================= FETCH LAYERS =================
def fixtures_by_date(date_str: str) -> List[Dict[str, Any]]:
    data = _get("/fixtures", {"date": date_str})
    resp = data.get("response") or []
    out = []
    cnt_skipped = 0
    for f in resp:
        fx = f.get("fixture",{}) or {}
        st = (fx.get("status") or {}).get("short","")
        if st in SKIP_STATUS:
            cnt_skipped += 1
            continue
        out.append(f)
        if len(out) >= MAX_MATCHES:
            break
    dbg(f"Fixtures: total={len(resp)} usable={len(out)} skipped={cnt_skipped}")
    return out

def odds_by_fixture(fid: int, date_hint: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    """
    1) Pokuša /odds?fixture=FID
    2) Ako prazno i date_hint zadat: koristi /odds?date=DATE pa izdvoji fid
    """
    data = _get("/odds", {"fixture": fid})
    items = data.get("response", []) or []
    if items:
        table = _collect_odds_from_items(items)
        m = table.get(fid) or {}
        dbg(f"Odds fid={fid}: via fixture, markets={len(m)}")
        return m

    dbg(f"Odds fid={fid}: empty via fixture; trying date fallback")
    if date_hint:
        all_by_date = _odds_by_date(date_hint)
        m = all_by_date.get(fid) or {}
        dbg(f"Odds fid={fid}: via date={date_hint}, markets={len(m)}")
        return m

    dbg(f"Odds fid={fid}: no markets and no date_hint")
    return {}

# ================= PICK LOGIC =================
def _pick_in_band(odds_map: Dict[str, Dict[str, float]], spec: Tuple[str,str], lo: float, hi: float):
    mkt,name = spec
    v = (odds_map.get(mkt) or {}).get(name)
    if v is None:
        return None
    v = float(v)
    if lo <= v <= hi:
        return (mkt, name, v)
    return None

def _best_from_bands(odds_map: Dict[str, Dict[str, float]], bands: Dict[Tuple[str,str], Tuple[float,float]]):
    best = None; best_odd = 0.0
    for (mkt,name),(lo,hi) in bands.items():
        p = _pick_in_band(odds_map,(mkt,name),lo,hi)
        if p and p[2] > best_odd:
            best, best_odd = p, p[2]
    return best

def _best_from_caps(odds_map: Dict[str, Dict[str, float]], caps: Dict[Tuple[str,str], float]):
    best = None; best_odd = 0.0
    for (mkt,name),cap in caps.items():
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
    }

def _sort_desc_by_odd(legs: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    return sorted(legs, key=lambda L: L["odd"], reverse=True)

# ================= ASSEMBLERS =================
def assemble_ticket1_legs(date_str: str) -> List[Dict[str,Any]]:
    legs: List[Dict[str,Any]] = []
    all_fx = fixtures_by_date(date_str)

    today_ids = sorted({ int((f.get("league") or {}).get("id", 0)) for f in all_fx if (f.get("league") or {}).get("id") })
    dbg(f"Today league.ids present: {today_ids[:60]} ...")

    # prvo prioritetne lige i lige čiji id je u ALLOW_IDS
    prio: List[Dict[str,Any]] = []
    rest: List[Dict[str,Any]] = []
    for f in all_fx:
        lg = f.get("league") or {}
        cid = int(lg.get("id", 0) or 0)
        country = lg.get("country", "")
        name = lg.get("name", "")
        in_prio = (country, name) in PREFERRED_LEAGUES
        in_allow = cid in ALLOW_IDS
        if in_prio or in_allow:
            prio.append(f)
        else:
            rest.append(f)

    scan = prio + rest
    dbg(f"T1 scan_order: prio={len(prio)} rest={len(rest)} total={len(scan)}")

    for f in scan:
        fid = int((f.get("fixture") or {}).get("id",0)) or 0
        if not fid:
            continue
        o = odds_by_fixture(fid, date_hint=date_str)
        p = _best_from_bands(o, MARKET_BANDS)
        if p:
            legs.append(_ticket_line(f, p))
        else:
            dbg(f"T1 skip fid={fid} reason=band_miss league_id={(f.get('league') or {}).get('id')} markets={list(o.keys())[:5]}")

    if not legs:
        dbg("T1 no legs -> RELAXED_BANDS applied")
        for f in scan:
            fid = int((f.get("fixture") or {}).get("id",0)) or 0
            if not fid:
                continue
            o = odds_by_fixture(fid, date_hint=date_str)
            p = _best_from_bands(o, RELAXED_BANDS)
            if p:
                legs.append(_ticket_line(f, p))

    legs = _sort_desc_by_odd(legs)
    dbg(f"T1 legs found={len(legs)}")
    return legs

def assemble_ticket2_legs(date_str: str) -> List[Dict[str,Any]]:
    legs: List[Dict[str,Any]] = []
    all_fx = fixtures_by_date(date_str)
    dbg(f"T2 scan all leagues: {len(all_fx)} fixtures")

    for f in all_fx:
        fid = int((f.get("fixture") or {}).get("id",0)) or 0
        if not fid:
            continue
        o = odds_by_fixture(fid, date_hint=date_str)
        p = _best_from_caps(o, ALLOW_ALL_CAPS)
        if p:
            legs.append(_ticket_line(f, p))
        else:
            dbg(f"T2 skip fid={fid} reason=no FH0.5/TTG<=caps")

    if not legs:
        dbg("T2 no legs -> RELAXED_CAPS applied")
        for f in all_fx:
            fid = int((f.get("fixture") or {}).get("id",0)) or 0
            if not fid:
                continue
            o = odds_by_fixture(fid, date_hint=date_str)
            p = _best_from_caps(o, RELAXED_CAPS)
            if p:
                legs.append(_ticket_line(f, p))

    legs = _sort_desc_by_odd(legs)
    dbg(f"T2 legs found={len(legs)}")
    return legs

# ================= BUILDER =================
def _format_ticket_text(idx: int, legs: List[Dict[str,Any]], total: float) -> str:
    head = f"🎫 Ticket #{idx}\n"
    lines = []
    for L in legs:
        lines.append("\n".join([L["league"], f"🆔 {L['fid']}", L["teams"], L["time"], L["pick"]]))
    body = "\n\n".join(lines)
    tail = f"\n\n🧮 Total odds: {total:.2f}"
    return head + body + tail

def _build_ticket(idx: int, target: float, pool: List[Dict[str,Any]], used: set):
    legs: List[Dict[str,Any]] = []
    total = 1.0
    for L in pool:
        if L["fid"] in used:
            continue
        legs.append(L); total *= L["odd"]

        while len(legs) > LEGS_MAX:
            legs.sort(key=lambda x: x["odd"])
            removed = legs.pop(0)
            total = math.prod([x["odd"] for x in legs]) if legs else 1.0
            dbg(f"T{idx} trim removed fid={removed['fid']} new_total={total:.2f}")

        if len(legs) >= LEGS_MIN and total >= target:
            used.update(x["fid"] for x in legs)
            dbg(f"T{idx} BUILT len={len(legs)} total={total:.2f}")
            return legs, round(total,2)
    dbg(f"T{idx} not built: pool={len(pool)} legs_accum={len(legs)} total={total:.2f}")
    return None, 1.0

# ================= OPENAI REASONING (optional) =================
def _reasoning_openai(ticket_title: str, legs: List[Dict[str,Any]]) -> str:
    if not OPENAI_KEY:
        return "Key drivers:\n" + "\n".join(
            f"- {L['teams']} — {L['market']} {L['pick_name']} @ {L['odd']:.2f}" for L in legs
        )
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        facts = [
            {"fixture": L["teams"], "market": L["market"], "pick": L["pick_name"], "odd": L["odd"]}
            for L in legs
        ]
        prompt = ("Write concise, neutral reasoning for a daily ticket. 3–6 bullets. "
                  "Avoid promotional tone and certainty. Use only provided picks. No emojis.")
        r = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL","gpt-4o-mini"),
            messages=[
                {"role":"system","content": prompt},
                {"role":"user","content": json.dumps({"ticket": ticket_title, "facts": facts}, ensure_ascii=False)},
            ],
            max_tokens=240, temperature=0.3,
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        dbg(f"OpenAI reasoning error: {e}")
        return "Key drivers:\n" + "\n".join(
            f"- {L['teams']} — {L['market']} {L['pick_name']} @ {L['odd']:.2f}" for L in legs
        )

# ================= PUBLIC API =================
def build_tickets_and_reasoning(date_str: str) -> Tuple[List[str], List[str]]:
    dbg(f"=== build_tickets_and_reasoning date={date_str} ===")
    # sanity check: pokušaj da vidiš ima li dnevnih kvota
    try:
        _ = _odds_by_date(date_str)
    except Exception as e:
        dbg(f"ODDS endpoint error: {e}")

    used: set[int] = set()
    tickets_texts: List[str] = []
    reasonings: List[str] = []

    # Ticket #1 → target 2.00
    legs1 = assemble_ticket1_legs(date_str)
    t1, tot1 = _build_ticket(1, 2.00, legs1, used)
    if t1:
        tickets_texts.append(_format_ticket_text(1, t1, tot1))
        reasonings.append(_reasoning_openai("Ticket #1", t1))
    else:
        dbg("Ticket #1 not generated")

    # Ticket #2 → target 1.85
    legs2 = assemble_ticket2_legs(date_str)
    legs2 = [L for L in legs2 if L["fid"] not in used]
    t2, tot2 = _build_ticket(2, 1.85, legs2, used)
    if t2:
        tickets_texts.append(_format_ticket_text(2, t2, tot2))
        reasonings.append(_reasoning_openai("Ticket #2", t2))
    else:
        dbg("Ticket #2 not generated")

    dbg(f"RESULT tickets={len(tickets_texts)}")
    return tickets_texts, reasonings

def post_to_telegram(message: str, channel: str) -> Tuple[bool, Dict[str,Any]]:
    if not TELE_BOT:
        return False, {"reason": "TELEGRAM_BOT_TOKEN not configured"}
    url = f"https://api.telegram.org/bot{TELE_BOT}/sendMessage"
    _sleep()
    with httpx.Client(timeout=30) as c:
        r = c.post(url, data={"chat_id": channel, "text": message})
        ok = 200 <= r.status_code < 300
        try:
            data = r.json()
        except Exception:
            data = {"status": r.status_code, "text": r.text[:200]}
    dbg(f"Telegram send channel={channel} ok={ok}")
    return ok, data
