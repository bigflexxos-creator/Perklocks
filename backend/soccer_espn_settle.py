"""ESPN-backed soccer leg settler.

The football-data.org free tier doesn't expose goal-scorer events on
/v4/matches/{id}, so we can't settle "Anytime Goal Scorer" props from
the same source the pipeline uses for predictions. ESPN's public soccer
APIs DO expose goal events (in `keyEvents`) and final scores across a
broad set of leagues — they're our pragmatic settlement source.

Supported markets:
  • Moneyline (incl. "Draw" selection)
  • Win or Draw / Double Chance
  • Total Goals Over/Under
  • Both Teams to Score (BTTS) Yes/No
  • Anytime Goal Scorer

Returns "won" / "lost" / "push" / None.  None means we couldn't
positively identify the match or scorer — caller should leave the leg
pending.

Designed to be permissive about team naming because ESPN, football-data,
and the Odds API all spell some teams differently (e.g. "Operário PR"
vs "Operario PR", "Goiás" vs "Goias").
"""
from __future__ import annotations

import logging
import re
import unicodedata as _ud
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.soccer_espn_settle")

# ESPN scoreboard / summary base.
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 12.0

# League slugs ESPN exposes for soccer. Ordered so the most-common
# competitions resolve first. We iterate until we hit the team pair.
# (We intentionally avoid hitting every slug in the world — these cover
#  ~95% of the props the platform surfaces.)
_LEAGUES = [
    # FIFA + UEFA international
    "fifa.world", "fifa.friendly", "fifa.confederations",
    "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "uefa.euro", "uefa.nations",
    "concacaf.gold", "conmebol.copa_america", "conmebol.libertadores",
    "afc.asian", "caf.nations",
    # Big-5 + secondary EU leagues
    "eng.1", "eng.2",
    "esp.1", "esp.2",
    "ger.1", "ger.2",
    "ita.1", "ita.2",
    "fra.1", "fra.2",
    "ned.1", "por.1", "tur.1", "bel.1", "sco.1", "gre.1", "rus.1",
    # Americas
    "usa.1", "usa.2",
    "mex.1", "mex.2",
    "bra.1", "bra.2",
    "arg.1", "chi.1", "col.1", "uru.1", "par.1", "ecu.1", "per.1",
    # Asia/AUS
    "aus.1", "jpn.1", "kor.1", "chn.1",
    # Misc
    "cup.world.club",
]

# Negative cache for league codes that returned 4xx to avoid retrying them
# repeatedly in the same process. Cleared when the process restarts.
_DEAD_LEAGUES: set[str] = set()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    """Lower + accent-strip + alnum-only-space-pre­served."""
    if not s:
        return ""
    s = "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")
    return s.strip().lower()


def _names_match(a: str, b: str) -> bool:
    """Tolerant name match.

    Treats "Operário PR" == "Operario PR", "Manchester United" == "Man United",
    "Goiás" == "Goias", "São Bernardo" == "Sao Bernardo".

    Strategy: accent-strip both, then accept exact match, OR substring
    match in either direction once we trim common suffixes (FC, CF, EC,
    AC, SC, AFC, CFC, etc.) and any leading article ("Os ", "El ", "AL-").
    """
    if not a or not b:
        return False
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Strip common club-suffix noise to compare cores.
    suffix_pat = re.compile(r"\b(fc|cf|ec|ac|sc|afc|cfc|sk|fk|ud|cd|fk|de|do|da)\b\.?", re.IGNORECASE)
    a2 = suffix_pat.sub("", na).strip()
    b2 = suffix_pat.sub("", nb).strip()
    a2 = re.sub(r"\s+", " ", a2)
    b2 = re.sub(r"\s+", " ", b2)
    if a2 and b2 and (a2 == b2):
        return True
    # Substring containment (one is a longer form of the other).
    if len(a2) >= 4 and len(b2) >= 4:
        if a2 in b2 or b2 in a2:
            return True
    # Token-overlap as last resort — share >=70% tokens (e.g. "ilves tampere" vs "ilves").
    ta = set(a2.split())
    tb = set(b2.split())
    if ta and tb:
        common = ta & tb
        if len(common) >= 1 and len(common) / max(len(ta), len(tb)) >= 0.5:
            return True
    return False


def _parse_event_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _candidate_dates(event_time_iso: Optional[str]) -> list[str]:
    """Return YYYYMMDD strings to scan on ESPN for a given event time.

    We try the event-time date plus ±1 day to cover late-finishing
    matches that roll past midnight UTC and any timezone drift between
    the parlay snapshot and ESPN's UTC-day grouping.
    """
    dt = _parse_event_iso(event_time_iso) or datetime.now(timezone.utc)
    dates = []
    for delta in (0, -1, 1):
        d = (dt + timedelta(days=delta)).strftime("%Y%m%d")
        if d not in dates:
            dates.append(d)
    return dates


async def _http_get(url: str, params: dict | None = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
            r = await cx.get(url, params=params or {},
                             headers={"User-Agent": _UA, "Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if 400 <= r.status_code < 500:
            return None
    except Exception as e:
        logger.debug("ESPN GET failed %s: %s", url, e)
    return None


async def _find_event(home_team: str, away_team: str,
                      event_time_iso: Optional[str]) -> Optional[tuple[str, dict]]:
    """Return (league_slug, event_dict) for the first ESPN event that
    matches both teams within ±1 day of `event_time_iso`. None if not
    found across every supported league."""
    dates = _candidate_dates(event_time_iso)
    for league in _LEAGUES:
        if league in _DEAD_LEAGUES:
            continue
        for ds in dates:
            url = f"{_ESPN_BASE}/{league}/scoreboard"
            data = await _http_get(url, {"dates": ds})
            if data is None:
                # Mark known-dead league slugs so we don't keep hammering them.
                _DEAD_LEAGUES.add(league)
                break  # 4xx → skip remaining dates for this league
            events = data.get("events") or []
            for ev in events:
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors") or []
                if len(competitors) < 2:
                    continue
                # ESPN puts home first usually; verify with homeAway flag.
                home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
                hname = ((home_c.get("team") or {}).get("displayName") or "")
                aname = ((away_c.get("team") or {}).get("displayName") or "")
                if _names_match(hname, home_team) and _names_match(aname, away_team):
                    return league, ev
    return None


async def _fetch_summary(league: str, event_id: str) -> Optional[dict]:
    url = f"{_ESPN_BASE}/{league}/summary"
    return await _http_get(url, {"event": str(event_id)})


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────
async def settle_soccer_leg(leg: dict) -> Optional[str]:
    """Best-effort soccer leg settle from ESPN. See module docstring."""
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

    found = await _find_event(home_team, away_team, event_time)
    if not found:
        return None
    league, ev = found

    # Only settle when match is full-time / finished.
    comp = (ev.get("competitions") or [{}])[0]
    status = ((comp.get("status") or {}).get("type") or {}).get("name") or ""
    if status not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET",
                      "STATUS_FINAL_PEN", "STATUS_FORFEIT"):
        return None

    # Pull final scores.
    competitors = comp.get("competitors") or []
    home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
    try:
        home_goals = int(home_c.get("score"))
        away_goals = int(away_c.get("score"))
    except (TypeError, ValueError):
        return None
    total_goals = home_goals + away_goals
    market_lower = market.lower()

    # ─── Anytime Goal Scorer ─────────────────────────────────────────
    if "goal scorer" in market_lower or "to score or assist" in market_lower or "score & assist" in market_lower:
        summary = await _fetch_summary(league, ev.get("id"))
        if not summary:
            return None
        return _settle_scorer_market(summary, selection, market_lower)

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
        # Selection may be a team OR a phrase like "Home or Draw".
        sel = selection or market
        if _names_match(sel, home_team) or "home" in _norm(sel).split() or _norm(sel).startswith("1x"):
            return "won" if home_goals >= away_goals else "lost"
        if _names_match(sel, away_team) or "away" in _norm(sel).split() or _norm(sel).startswith("x2"):
            return "won" if away_goals >= home_goals else "lost"
        # Phrase "Home or Away" (12) is unusual but support it.
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
    if "total goals" in market_lower or "total" in market_lower or (
        "goals" in market_lower and ("over" in market_lower or "under" in market_lower)
    ):
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
            if total_goals > line:
                return "won"
            if total_goals < line:
                return "lost"
            return "push"
        if is_under:
            if total_goals < line:
                return "won"
            if total_goals > line:
                return "lost"
            return "push"
        return None

    return None


# ──────────────────────────────────────────────────────────────────────
# Goal-scorer settlement helpers
# ──────────────────────────────────────────────────────────────────────
def _settle_scorer_market(summary: dict, selection: str, market_lower: str) -> Optional[str]:
    """Resolve goal-scorer style markets from an ESPN /summary payload.

    The `keyEvents` array contains every meaningful event; goals are
    flagged with `scoringPlay: true` and we can read the scorer name
    out of `participants[].athlete.displayName` or fall back to the
    natural-language `text` field.
    """
    if not selection:
        return None
    key_events = summary.get("keyEvents") or []
    scorers = _extract_scorers(key_events)
    if not scorers:
        # No goals at all — every "Anytime Goal Scorer" pick loses.
        if "anytime" in market_lower or "goal scorer" in market_lower:
            return "lost"
        return None
    if _scorer_match(selection, scorers):
        return "won"
    return "lost"


def _extract_scorers(key_events: list[dict]) -> list[str]:
    """Pull the canonical scorer name from every keyEvent that's a goal."""
    out: list[str] = []
    for e in key_events:
        if not e.get("scoringPlay"):
            continue
        tp = (e.get("type") or {}).get("type") or (e.get("type") or {}).get("text") or ""
        # Only count regulation/ET goals — exclude shootouts unless the
        # market explicitly counts them (rare; leave out for safety).
        if e.get("shootout"):
            continue
        # Skip own goals (the credited scorer is the defender, not a
        # bookmaker scorer for the prop) — text usually contains "Own Goal".
        text = (e.get("text") or "")
        if "own goal" in text.lower():
            continue
        # Prefer the athlete in `participants[*].athlete` (most reliable).
        scorer = None
        for p in (e.get("participants") or []):
            if (p.get("type") or "scorer").lower() in {"scorer", "athlete", "scorer-1"} or not p.get("type"):
                a = p.get("athlete") or {}
                scorer = a.get("displayName") or a.get("shortName") or scorer
                if scorer:
                    break
        if not scorer:
            # Fall back to parsing the `shortText` like
            # "Cristiano Ronaldo Goal - Volley" → "Cristiano Ronaldo".
            short = e.get("shortText") or ""
            scorer = re.sub(r"\s+(Goal.*|Penalty.*|Header.*)$", "", short, flags=re.IGNORECASE).strip()
        if not scorer:
            # Last-ditch: pull from full text "Goal! ... [Name] (Team) ..."
            m = re.search(r"goal!?\s*[^.]*?\.\s*([A-Z][\w'\-\.\u00C0-\u024F]+(?:\s+[A-Z][\w'\-\.\u00C0-\u024F]+)+)",
                          text, re.IGNORECASE)
            if m:
                scorer = m.group(1)
        if scorer:
            out.append(scorer)
    return out


def _scorer_match(selection: str, scorers: list[str]) -> bool:
    """True if `selection` matches any scorer name (accent + case insensitive,
    last-name fallback)."""
    sel = _norm(selection)
    if not sel:
        return False
    sel_last = sel.split()[-1]
    for s in scorers:
        ns = _norm(s)
        if ns == sel:
            return True
        # Bidirectional substring (handles "C. Ronaldo" vs "Cristiano Ronaldo")
        if (sel in ns) or (ns in sel):
            return True
        if ns.split()[-1] == sel_last and len(sel_last) >= 4:
            return True
    return False
