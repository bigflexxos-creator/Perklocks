"""FotMob-backed soccer leg settler (universal fallback).

Used when ESPN can't find a match because the league isn't in ESPN's
slug catalogue (e.g. Finnish Veikkausliiga, Lithuanian A Lyga, Faroe
Islands Premier League, Vietnamese V.League — every fixture in the
world that has any betting interest).

Strategy:
    GET /api/data/matches?date=YYYYMMDD   → all finished matches that
                                              day, grouped by league.
    GET /api/data/matchDetails?matchId=X  → keyEvents incl. goals + scorers.

FotMob's public JSON API isn't officially documented, so we keep this
adapter conservative — match by team-name (accent-stripped, fuzzy),
match by date (±1 day window for tz drift), and skip whenever we can't
get a clean lock on both teams.

Supported markets (same as ESPN settler):
    • Moneyline / Win or Draw / Double Chance
    • Total Goals Over/Under
    • Both Teams to Score (BTTS)
    • Anytime Goal Scorer
"""
from __future__ import annotations

import logging
import re
import unicodedata as _ud
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.soccer_fotmob_settle")

_BASE = "https://www.fotmob.com/api/data"
_TIMEOUT = 12.0
_HEADERS = {
    "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    # FotMob's web client always sends an x-mas header (rotated proof
    # token). The endpoint doesn't STRICTLY require it for /api/data/*
    # but sending a dummy value matches normal browser traffic and
    # avoids triggering rate-limit heuristics.
    "x-mas":           "static",
}


# ──────────────────────────────────────────────────────────────────────
# Helpers (shared style with soccer_espn_settle._names_match etc.)
# ──────────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    if not s:
        return ""
    s = "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")
    return s.strip().lower()


def _names_match(a: str, b: str) -> bool:
    """Tolerant name match — same rules as the ESPN settler."""
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Strip common club-suffix noise.
    suffix_pat = re.compile(
        r"\b(fc|cf|ec|ac|sc|afc|cfc|sk|fk|ud|cd)\b\.?",
        re.IGNORECASE,
    )
    a2 = re.sub(r"\s+", " ", suffix_pat.sub("", na).strip())
    b2 = re.sub(r"\s+", " ", suffix_pat.sub("", nb).strip())
    if a2 and b2 and a2 == b2:
        return True
    if len(a2) >= 3 and len(b2) >= 3 and (a2 in b2 or b2 in a2):
        return True
    ta, tb = set(a2.split()), set(b2.split())
    if ta and tb:
        common = ta & tb
        if len(common) >= 1 and len(common) / max(len(ta), len(tb)) >= 0.5:
            return True
    # First-token compare ("Ilves Tampere" → "Ilves", "KuPS Kuopio" → "KuPS").
    sa = a2.split()
    sb = b2.split()
    if sa and sb and sa[0] == sb[0] and len(sa[0]) >= 3:
        return True
    return False


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _candidate_dates(event_time_iso: Optional[str]) -> list[str]:
    dt = _parse_iso(event_time_iso) or datetime.now(timezone.utc)
    out = []
    for delta in (0, -1, 1):
        d = (dt + timedelta(days=delta)).strftime("%Y%m%d")
        if d not in out:
            out.append(d)
    return out


async def _http_get(path: str, params: dict | None = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS) as cx:
            r = await cx.get(f"{_BASE}/{path}", params=params or {})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("FotMob GET %s failed: %s", path, e)
    return None


async def _matches_for_date(date_str: str) -> list[dict]:
    """Return flat list of finished match summaries for the given UTC date.
    Each entry is augmented with the league name for downstream logging."""
    data = await _http_get("matches", {"date": date_str})
    if not data:
        return []
    out: list[dict] = []
    for lg in (data.get("leagues") or []):
        for m in (lg.get("matches") or []):
            m["__league"] = lg.get("name")
            out.append(m)
    return out


async def _find_match(home_team: str, away_team: str,
                      event_time_iso: Optional[str]) -> Optional[dict]:
    """Find a FotMob match dict for the given fixture, scanning ±1 day."""
    for ds in _candidate_dates(event_time_iso):
        matches = await _matches_for_date(ds)
        if not matches:
            continue
        for m in matches:
            mh = ((m.get("home") or {}).get("longName") or
                  (m.get("home") or {}).get("name") or "")
            ma = ((m.get("away") or {}).get("longName") or
                  (m.get("away") or {}).get("name") or "")
            if _names_match(mh, home_team) and _names_match(ma, away_team):
                return m
    return None


async def _match_detail(match_id) -> Optional[dict]:
    return await _http_get("matchDetails", {"matchId": str(match_id)})


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────
async def settle_soccer_leg(leg: dict) -> Optional[str]:
    """Best-effort FotMob settle — same return contract as the ESPN
    settler ("won" / "lost" / "push" / None)."""
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    selection = (leg.get("selection") or "").strip()
    event_time = leg.get("event_time") or leg.get("commence_time")
    if not event or not market:
        return None
    parts = re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return None
    away_team, home_team = parts[0].strip(), parts[1].strip()

    match = await _find_match(home_team, away_team, event_time)
    if not match:
        return None

    status = match.get("status") or {}
    if not status.get("finished"):
        return None
    if status.get("cancelled"):
        return "void"

    try:
        home_goals = int((match.get("home") or {}).get("score"))
        away_goals = int((match.get("away") or {}).get("score"))
    except (TypeError, ValueError):
        return None
    total_goals = home_goals + away_goals
    market_lower = market.lower()

    # ─── Anytime Goal Scorer ─────────────────────────────────────────
    if "goal scorer" in market_lower or "to score or assist" in market_lower or "score & assist" in market_lower:
        detail = await _match_detail(match.get("id"))
        if not detail:
            return None
        return _settle_scorer_market(detail, selection, market_lower)

    # ─── Moneyline ───────────────────────────────────────────────────
    if "moneyline" in market_lower:
        if not selection:
            return None
        sel = _norm(selection)
        if _names_match(selection, home_team):
            return "won" if home_goals > away_goals else "lost"
        if _names_match(selection, away_team):
            return "won" if away_goals > home_goals else "lost"
        if "draw" in sel:
            return "won" if home_goals == away_goals else "lost"
        return None

    # ─── Win or Draw / Double Chance ─────────────────────────────────
    if "win or draw" in market_lower or "double chance" in market_lower:
        sel = selection or market
        if _names_match(sel, home_team) or "home" in _norm(sel).split() or _norm(sel).startswith("1x"):
            return "won" if home_goals >= away_goals else "lost"
        if _names_match(sel, away_team) or "away" in _norm(sel).split() or _norm(sel).startswith("x2"):
            return "won" if away_goals >= home_goals else "lost"
        if _norm(sel) in ("12", "home or away", "away or home"):
            return "won" if home_goals != away_goals else "lost"
        return None

    # ─── Both Teams to Score ─────────────────────────────────────────
    if "both teams to score" in market_lower or "btts" in market_lower:
        is_yes = ("yes" in _norm(selection) or "yes" in market_lower) and "no" not in _norm(selection)
        is_no = _norm(selection) == "no" or " no " in f" {market_lower} "
        btts = (home_goals > 0 and away_goals > 0)
        if is_yes:
            return "won" if btts else "lost"
        if is_no:
            return "won" if not btts else "lost"
        return None

    # ─── Total Goals Over/Under ──────────────────────────────────────
    if ("total goals" in market_lower or
        "total" in market_lower or
        ("goals" in market_lower and ("over" in market_lower or "under" in market_lower))):
        m_line = re.search(r"(\d+(?:\.\d+)?)", market)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        is_over = "over" in market_lower or "over" in _norm(selection)
        is_under = "under" in market_lower or "under" in _norm(selection)
        if is_over:
            if total_goals > line:  return "won"
            if total_goals < line:  return "lost"
            return "push"
        if is_under:
            if total_goals < line:  return "won"
            if total_goals > line:  return "lost"
            return "push"
        return None

    return None


def _settle_scorer_market(detail: dict, selection: str, market_lower: str) -> Optional[str]:
    content = detail.get("content") or {}
    mf = content.get("matchFacts") or {}
    events = mf.get("events") or {}
    ev_list = events.get("events") if isinstance(events, dict) else events
    scorers: list[str] = []
    for e in (ev_list or []):
        type_ = (e.get("type") or "").lower()
        if "goal" not in type_:
            continue
        if "own" in type_ or "owngoal" in type_:
            continue
        # FotMob marks penalty misses with type "penalty_missed" → ignore.
        if "miss" in type_:
            continue
        player = e.get("player") or {}
        name = player.get("name") if isinstance(player, dict) else str(player or "")
        if name:
            scorers.append(name)
    if not scorers:
        if "anytime" in market_lower or "goal scorer" in market_lower:
            return "lost"
        return None
    if _scorer_match(selection, scorers):
        return "won"
    return "lost"


def _scorer_match(selection: str, scorers: list[str]) -> bool:
    sel = _norm(selection)
    if not sel:
        return False
    sel_last = sel.split()[-1]
    for s in scorers:
        ns = _norm(s)
        if ns == sel:
            return True
        if (sel in ns) or (ns in sel):
            return True
        if ns.split()[-1] == sel_last and len(sel_last) >= 4:
            return True
    return False
