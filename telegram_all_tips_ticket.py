#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, json, time, random, re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx

API_KEY = os.getenv("API_FOOTBALL_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_ORG = os.getenv("OPENAI_ORG") or None
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BASE_URL = os.getenv("API_FOOTBALL_URL", "https://v3.football.api-sports.io")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Belgrade")
TZ = ZoneInfo(TIMEZONE)

if not API_KEY: 
    print("⛔ Missing API_FOOTBALL_KEY", file=sys.stderr); sys.exit(1)
if not OPENAI_API_KEY:
    print("⛔ Missing OPENAI_API_KEY", file=sys.stderr); sys.exit(1)
if not TELEGRAM_BOT_TOKEN:
    print("⛔ Missing TELEGRAM_BOT_TOKEN", file=sys.stderr); sys.exit(1)

TARGETS = [2.0, 3.0, 4.0]
LEGS_MIN = int(os.getenv("LEGS_MIN", "3"))
LEGS_MAX = int(os.getenv("LEGS_MAX", "7"))
MAX_MATCHES = 300

BASE_TH: Dict[Tuple[str,str], float] = {
    ("Double Chance","1X"): 1.20,
    ("Double Chance","X2"): 1.25,
    ("BTTS","Yes"): 1.40,
    ("BTTS","No"):  1.30,
    ("Over/Under","Over 1.5"):  1.15,
    ("Over/Under","Under 3.5"): 1.20,
    ("Over/Under","Over 2.5"):  1.28,
    ("Match Winner","Home"): 1.30,
    ("Match Winner","Away"): 1.30,
    ("1st Half Goals","Over 0.5"):  1.25,
    ("Home Team Goals","Over 0.5"): 1.25,
    ("Away Team Goals","Over 0.5"): 1.25,
}

ALLOW_LIST: set[int] = {
    2,3,913,5,536,808,960,10,667,29,30,31,32,37,33,34,848,311,310,342,218,144,315,71,
    169,210,346,233,39,40,41,42,703,244,245,61,62,78,79,197,271,164,323,135,136,389,
    88,89,408,103,104,106,94,283,235,286,287,322,140,141,113,207,208,202,203,909,
}
SKIP_STATUS = {"FT","AET","PEN","ABD","AWD","CANC","POSTP","PST","SUSP","INT","WO","LIVE"}
HEADERS = {"x-apisports-key": API_KEY}

def _client() -> httpx.Client:
    return httpx.Client(timeout=30)

def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}{'' if path.startswith('/') else '/'}{path}"
    with _client() as c:
        r = c.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()

def _try_float(x: Any) -> Optional[float]:
    try:
        v = float(x); 
        return v if v > 0 else None
    except Exception:
        return None

def _fmt_dt_local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso

FORBIDDEN_SUBSTRS = [
    "asian","alternative","corners","cards","booking","penalties","penalty",
    "offside","throw in","interval","race to","period","quarter",
    "draw no bet","dnb","to qualify","method of victory","overtime","extra time",
    "win to nil","clean sheet","anytime","player","scorer"
]
DOC_MARKETS = {
    "match_winner": {"Match Winner","1X2","Full Time Result","Result"},
    "double_chance": {"Double Chance","Double chance"},
    "btts": {"Both Teams To Score","Both teams to score","BTTS","Both Teams Score"},
    "ou": {"Goals Over/Under","Over/Under","Total Goals","Total","Goals Over Under","Total Goals Over/Under"},
    "ou_1st": {"1st Half - Over/Under","1st Half Goals Over/Under","First Half - Over/Under","Over/Under - 1st Half","First Half Goals","Goals Over/Under - 1st Half"},
    "ttg_home": {"Home Team Total Goals","Home Team Goals","Home Team - Total Goals","Home Total Goals"},
    "ttg_away": {"Away Team Total Goals","Away Team Goals","Away Team - Total Goals","Away Total Goals"},
    "ttg_generic": {"Team Total Goals","Total Team Goals","Team Goals"}
}

def _is_market_named(name: str, targets: set[str]) -> bool:
    n = (name or "").strip().lower()
    return any(n == t.lower() for t in targets)

def _is_fulltime_main(name: str) -> bool:
    nl = (name or "").lower()
    return not any(b in nl for b in FORBIDDEN_SUBSTRS)

def _normalize_ou_value(v: str) -> str:
    import re
    s = (v or "").strip()
    s = s.replace("Over1.5","Over 1.5").replace("Under3.5","Under 3.5").replace("Over2.5","Over 2.5")
    s = re.sub(r"\s+", " ", s.title())
    return s

def _norm_over05(val: str) -> bool:
    import re
    return re.search(r"(?i)\bover\s*0\.5\b", val or "") is not None

def _value_mentions_home(val: str) -> bool:
    import re
    return re.search(r"(?i)\bhome\b", val or "") is not None

def _value_mentions_away(val: str) -> bool:
    import re
    return re.search(r"(?i)\baway\b", val or "") is not None

def best_market_odds(odds_resp: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    best: Dict[str, Dict[str, float]] = {}
    def put(mkt: str, val: str, odd_raw: Any):
        odd = _try_float(odd_raw)
        if odd is None: return
        best.setdefault(mkt, {})
        old = best[mkt].get(val)
        if old is None or odd > old:
            best[mkt][val] = odd

    for item in odds_resp:
        for bm in item.get("bookmakers", []) or []:
            for bet in bm.get("bets", []) or []:
                raw = (bet.get("name") or "").strip()
                if not raw: continue

                if _is_market_named(raw, DOC_MARKETS["ou_1st"]):
                    for v in bet.get("values", []) or []:
                        if _norm_over05(v.get("value") or ""):
                            put("1st Half Goals","Over 0.5", v.get("odd"))
                    continue

                if not _is_fulltime_main(raw): 
                    continue

                if _is_market_named(raw, DOC_MARKETS["match_winner"]):
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").strip()
                        if val in ("Home","1"): put("Match Winner","Home", v.get("odd"))
                        elif val in ("Away","2"): put("Match Winner","Away", v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["double_chance"]):
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").replace(" ","").upper()
                        if val in {"1X","X2","12"}:
                            put("Double Chance", val, v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["btts"]):
                    for v in bet.get("values", []) or []:
                        val = (v.get("value") or "").strip().title()
                        if val in {"Yes","No"}: put("BTTS", val, v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["ou"]):
                    for v in bet.get("values", []) or []:
                        norm = _normalize_ou_value(v.get("value") or "")
                        if norm in {"Over 1.5","Under 3.5","Over 2.5"}:
                            put("Over/Under", norm, v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["ttg_home"]):
                    for v in bet.get("values", []) or []:
                        if _norm_over05(v.get("value") or ""):
                            put("Home Team Goals","Over 0.5", v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["ttg_away"]):
                    for v in bet.get("values", []) or []:
                        if _norm_over05(v.get("value") or ""):
                            put("Away Team Goals","Over 0.5", v.get("odd"))
                    continue

                if _is_market_named(raw, DOC_MARKETS["ttg_generic"]):
                    for v in bet.get("values", []) or []:
                        vv = (v.get("value") or "").strip()
                        if _norm_over05(vv):
                            if _value_mentions_home(vv):
                                put("Home Team Goals","Over 0.5", v.get("odd"))
                            elif _value_mentions_away(vv):
                                put("Away Team Goals","Over 0.5", v.get("odd"))
                    continue
    return best

def fetch_fixtures(date_str: str) -> List[Dict[str, Any]]:
    items = _get("/fixtures", {"date": date_str}).get("response") or []
    out = []
    for f in items:
        lg = f.get("league", {}) or {}; fx = f.get("fixture", {}) or {}
        st = (fx.get("status") or {}).get("short","")
        if lg.get("id") in ALLOW_LIST and st not in SKIP_STATUS:
            out.append(f)
        if len(out) >= MAX_MATCHES: break
    return out

def fetch_odds(fid: int) -> List[Dict[str, Any]]:
    return _get("/odds", {"fixture": fid}).get("response") or []

def fetch_odds_best(fid: int) -> Dict[str, Dict[str, float]]:
    return best_market_odds(fetch_odds(fid))

def assemble_legs(date_str: str) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []
    for f in fetch_fixtures(date_str):
        fx=f.get("fixture",{}) or {}; lg=f.get("league",{}) or {}; tm=f.get("teams",{}) or {}
        fid=fx.get("id"); when=_fmt_dt_local(fx.get("date",""))
        home=(tm.get("home") or {}); away=(tm.get("away") or {})
        odds = fetch_odds_best(fid)

        best_pick=None; best_odd=0.0; best_market=None; best_name=None
        for (mkt, variants) in odds.items():
            for name, odd in variants.items():
                lim = BASE_TH.get((mkt, name))
                if lim is None: 
                    continue
                if odd < lim and odd > best_odd:
                    best_odd = odd; best_market=mkt; best_name=name
        if best_market:
            legs.append({
                "fid": int(fid),
                "league": f"🏟 {lg.get('country','')} — {lg.get('name','')}",
                "teams": f"⚽ {home.get('name')} vs {away.get('name')}",
                "time": f"⏰ {when}",
                "market": best_market,
                "pick_name": best_name,
                "odd": float(best_odd),
                "pick": f"• {best_market} → {best_name}: {best_odd:.2f}"
            })
    legs.sort(key=lambda L: L["odd"], reverse=True)
    return legs

def _product(nums: List[float]) -> float:
    p=1.0
    for x in nums: p*=x
    return p

def _format_ticket(n:int, legs: List[Dict[str,Any]]) -> str:
    parts=[f"🎫 Ticket #{n}"]; comps=[]
    for L in legs:
        parts.extend([L["league"], f"🆔 {L['fid']}", L["teams"], L["time"], L["pick"], ""])
        comps.append(f"{L['odd']:.2f}")
    total=_product([l["odd"] for l in legs])
    parts.append(f"TOTAL ODDS: {' × '.join(comps)} = {total:.2f}")
    return "\n".join(parts)

def build_three_tickets(date_str: str) -> List[Tuple[str, List[int]]]:
    legs = assemble_legs(date_str)
    used_fixtures: set[int] = set()
    tickets: List[Tuple[str, List[int]]] = []

    targets = [2.0, 3.0, 4.0]
    for idx, target in enumerate(targets, start=1):
        t: List[Dict[str,Any]] = []
        total=1.0
        for L in legs:
            if L["fid"] in used_fixtures:
                continue
            cand_total = total * L["odd"]
            t.append(L); total = cand_total
            if len(t) >= LEGS_MIN and total >= target:
                while len(t) > LEGS_MAX:
                    t.pop()
                txt=_format_ticket(idx, t)
                tickets.append((txt, [x["fid"] for x in t]))
                used_fixtures.update(x["fid"] for x in t)
                break
        if len(tickets) < idx:
            for L in legs:
                if L["fid"] in used_fixtures or L in t: 
                    continue
                t.append(L); total *= L["odd"]
                if len(t) >= LEGS_MIN and total >= target:
                    txt=_format_ticket(idx, t)
                    tickets.append((txt, [x["fid"] for x in t]))
                    used_fixtures.update(x["fid"] for x in t)
                    break
    return tickets[:3]

PROMPT_REASON = "You are a football analyst. Review each betting ticket. For each ticket, give a short reasoning (max 5 bullet points, 1 line each). Focus on match dynamics, recent form, and why each leg is conservative yet realistic. Return plain text only, no JSON, keep it concise."

def _openai_headers() -> Dict[str,str]:
    h={"Authorization": f"Bearer {OPENAI_API_KEY}"}
    if OPENAI_ORG: h["OpenAI-Organization"]=OPENAI_ORG
    return h

def _chat(messages: List[Dict[str,str]], temperature: float=0.2) -> str:
    url = f"{OPENAI_BASE_URL.rstrip('/')}/chat/completions"
    body = {"model": OPENAI_MODEL, "temperature": temperature, "messages": messages}
    with httpx.Client(timeout=120) as c:
        r = c.post(url, headers=_openai_headers(), json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

def build_tickets_and_reasoning(date_str: str) -> Tuple[List[str], List[str]]:
    tickets = build_three_tickets(date_str)
    ticket_texts = [t[0] for t in tickets]

    reasonings: List[str] = []
    for idx, ticket in enumerate(ticket_texts, start=1):
        prompt = f"{PROMPT_REASON}\n---\n{ticket}\n---\nWrite reasoning for Ticket #{idx} only."
        txt = _chat([
            {"role":"system","content":"Be concise. Output plain text."},
            {"role":"user","content": prompt}
        ])
        reasonings.append(txt)
        time.sleep(0.5)
    return ticket_texts, reasonings

def post_to_telegram(message: str, channel: str) -> Tuple[bool, Dict[str, Any]]:
    token = TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    with httpx.Client(timeout=30) as c:
        r = c.post(url, json=payload)
        ok = 200 <= r.status_code < 300
        try:
            data = r.json()
        except Exception:
            data = {"status_code": r.status_code, "text": r.text[:200]}
    return ok, data
