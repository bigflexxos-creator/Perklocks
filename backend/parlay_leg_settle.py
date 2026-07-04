"""External Leg Settlement Adapter for Parlay History.

Problem solved (user complaint 2026-06-23 "Bets In parlay tab not grading"):
when a saved parlay leg's source pick has been deleted from the `picks`
collection (because daily refreshes wipe today's slate; settled legacy
picks for past games sometimes get dropped too), the existing
`resolve_saved_parlays` resolver gives up because both `pick_id` lookup
and `snapshot identity match` fail.

This module steps in for those orphaned legs and settles them DIRECTLY
from the underlying game result via:

  • MLB Stats API (free, no Odds credits) → MLB moneyline, spread, total,
    pitcher strikeouts.
  • Soccer: lightweight match-result lookup via the existing soccer
    pipeline's score history (best-effort; falls back to no-op if the
    pipeline hasn't backfilled the result yet).

The adapter is intentionally conservative — it returns "pending" rather
than guessing whenever a game result can't be confirmed unambiguously.
A wrong settle is much worse than leaving a leg pending one more cycle.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta as _timedelta
from typing import Optional

logger = logging.getLogger("lockscore.parlay_leg_settle")


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────

async def try_settle_leg_externally(leg: dict) -> Optional[str]:
    """Best-effort: return "won" / "lost" / "void" / "push" / None.

    None means "couldn't settle, leave pending". The leg argument is the
    snapshot stored on the parlay (sport, event, market, selection,
    event_time, book_odds, etc.).
    """
    sport = (leg.get("sport") or "").upper()
    if sport == "MLB":
        return await _settle_mlb_leg(leg)
    if sport == "SOCCER":
        return await _settle_soccer_leg(leg)
    if sport == "TENNIS":
        return await _settle_tennis_leg_via_espn(leg)
    if sport in ("UFC", "MMA"):
        return await _settle_ufc_leg_via_espn(leg)
    # NBA / NFL / WNBA / CFB — no orphan-safe external settler yet.
    # These sports' picks are settled directly via the picks-table
    # ESPN settlers (`espn_settlement.py`). If the source pick was
    # wiped, we return None (leave pending). Users can force a
    # re-settle via /api/parlay/{id}/resettle which walks a broader
    # score-lookup, or the resolver will pick it up next cycle if
    # the pick row is later restored by the daily refresh.
    return None


# ──────────────────────────────────────────────────────────────────────────
# Tennis (ESPN scoreboard)
# ──────────────────────────────────────────────────────────────────────────

async def _settle_tennis_leg_via_espn(leg: dict) -> Optional[str]:
    """Grade a tennis parlay leg from the ESPN ATP/WTA scoreboard.

    Supported markets:
        • Moneyline
        • Spread (Games handicap)
        • Total Games Over/Under (incl. Alt lines like "Over 16.0 Games (Alt)")

    Uses the existing `espn_settlement` helpers so behaviour matches the
    picks-table Tennis settler exactly.
    """
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    if not event or not market:
        return None
    event_time = leg.get("event_time") or leg.get("commence_time")
    dt = _parse_iso(event_time)
    if not dt:
        return None
    try:
        from espn_settlement import (
            _fetch_espn_tennis_matches, _match_tennis_comp_for_pick,
            _tennis_pick_outcome,
        )
    except Exception:
        return None
    # Tennis matches sometimes span 2 UTC dates (evening ET → next-day UTC).
    # Probe the scheduled day plus ±1 to be safe.
    dates_to_try = [
        dt.strftime("%Y-%m-%d"),
        (dt + _timedelta(days=1)).strftime("%Y-%m-%d"),
        (dt - _timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    for d in dates_to_try:
        comps = await _fetch_espn_tennis_matches(d)
        if not comps:
            continue
        pick_shape = {
            "market": market,
            "selection": leg.get("selection") or "",
            "event": event,
        }
        c = _match_tennis_comp_for_pick(pick_shape, comps)
        if c:
            outcome = _tennis_pick_outcome(pick_shape, c)
            if outcome in ("won", "lost", "push"):
                return outcome
    return None


# ──────────────────────────────────────────────────────────────────────────
# UFC / MMA (ESPN scoreboard)
# ──────────────────────────────────────────────────────────────────────────

async def _settle_ufc_leg_via_espn(leg: dict) -> Optional[str]:
    """Grade a UFC parlay leg from the ESPN MMA scoreboard.

    Supported markets:
        • Moneyline
        • Method of victory (KO/TKO, Submission, Decision)
    """
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    if not event or not market:
        return None
    event_time = leg.get("event_time") or leg.get("commence_time")
    dt = _parse_iso(event_time)
    if not dt:
        return None
    try:
        from espn_settlement import (
            _fetch_espn_ufc_fights, _match_ufc_fight_for_pick,
            _ufc_pick_outcome,
        )
    except Exception:
        return None
    # UFC cards routinely start Sat evening ET and finish Sun UTC — probe ±1.
    dates_to_try = [
        dt.strftime("%Y-%m-%d"),
        (dt + _timedelta(days=1)).strftime("%Y-%m-%d"),
        (dt - _timedelta(days=1)).strftime("%Y-%m-%d"),
    ]
    pick_shape = {
        "market": market,
        "selection": leg.get("selection") or "",
        "event": event,
    }
    for d in dates_to_try:
        fights = await _fetch_espn_ufc_fights(d)
        if not fights:
            continue
        f = _match_ufc_fight_for_pick(pick_shape, fights)
        if f:
            outcome = _ufc_pick_outcome(pick_shape, f)
            if outcome in ("won", "lost", "push"):
                return outcome
    return None


# ──────────────────────────────────────────────────────────────────────────
# MLB
# ──────────────────────────────────────────────────────────────────────────

async def _settle_mlb_leg(leg: dict) -> Optional[str]:
    """MLB markets we can settle from the free MLB Stats API:
        • Moneyline    ("Yankees Moneyline")
        • Spread       ("Yankees +1.5 Spread", "Yankees -1.5 Spread")
        • Total Runs   ("Total Runs Over 8.5", "Total Runs Under 7.5")
        • Pitcher Ks   ("Gerrit Cole (NYY) Over 6.5 Strikeouts")
    """
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    selection = (leg.get("selection") or "").strip()
    if not event or not market:
        return None

    parts = re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return None
    away_team_name, home_team_name = parts[0].strip(), parts[1].strip()

    # Fetch trailing 14d of MLB scores from the free API. Cheap (1 HTTP call,
    # 0 Odds credits) and the resolver only runs a few times per hour.
    try:
        from mlb_live import fetch_mlb_scores
    except Exception:
        return None
    games = await fetch_mlb_scores(days_back=14)
    if not games:
        return None

    # When two teams play a series, multiple completed games will match
    # by team name (e.g. Brewers @ Braves can have 3 in a 14-day window).
    # Pick the game whose start time is closest to the leg's stored
    # `event_time`. Falls back to the first completed match if no event
    # time is available.
    leg_event_time = _parse_iso(leg.get("event_time") or leg.get("commence_time"))
    candidates = [
        g for g in games
        if g.get("completed")
        and g.get("home_team") == home_team_name
        and g.get("away_team") == away_team_name
    ]
    if not candidates:
        return None
    if leg_event_time and len(candidates) > 1:
        def _delta(g: dict) -> float:
            ct = _parse_iso(g.get("commence_time"))
            if not ct:
                return 1e18
            return abs((ct - leg_event_time).total_seconds())
        candidates.sort(key=_delta)
    game = candidates[0]

    # Pull scores
    home_score = None
    away_score = None
    for s in game.get("scores") or []:
        if s.get("name") == home_team_name:
            home_score = s.get("score")
        elif s.get("name") == away_team_name:
            away_score = s.get("score")
    if home_score is None or away_score is None:
        return None
    home_score = int(home_score)
    away_score = int(away_score)
    total_runs = home_score + away_score

    market_lower = market.lower()

    # ─── Moneyline ───────────────────────────────────────────────────
    if "moneyline" in market_lower:
        if not selection:
            return None
        if selection == home_team_name:
            return "won" if home_score > away_score else "lost"
        if selection == away_team_name:
            return "won" if away_score > home_score else "lost"
        return None

    # ─── Spread (±X.Y) ───────────────────────────────────────────────
    if "spread" in market_lower or re.search(r"[+\-]\d+\.\d+", market_lower):
        m_line = re.search(r"([+\-]\d+(?:\.\d+)?)", market)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        if not selection:
            return None
        # Selection team's score minus opponent's, plus the spread line.
        if selection == home_team_name:
            covered = (home_score - away_score) + line
        elif selection == away_team_name:
            covered = (away_score - home_score) + line
        else:
            return None
        if covered > 0:
            return "won"
        if covered < 0:
            return "lost"
        return "push"

    # ─── Total Runs ──────────────────────────────────────────────────
    if "total" in market_lower and ("runs" in market_lower or "run line" in market_lower or "over" in market_lower or "under" in market_lower):
        m_line = re.search(r"(\d+(?:\.\d+)?)", market)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        is_over = "over" in market_lower
        is_under = "under" in market_lower
        if not (is_over or is_under):
            return None
        if is_over:
            if total_runs > line:
                return "won"
            if total_runs < line:
                return "lost"
            return "push"
        # under
        if total_runs < line:
            return "won"
        if total_runs > line:
            return "lost"
        return "push"

    # ─── Pitcher Strikeouts ──────────────────────────────────────────
    if "strikeout" in market_lower or " ks " in f" {market_lower} ":
        m_line = re.search(r"(\d+(?:\.\d+)?)\s*strikeout", market_lower)
        if not m_line:
            m_line = re.search(r"over\s+(\d+(?:\.\d+)?)", market_lower) or re.search(r"under\s+(\d+(?:\.\d+)?)", market_lower)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        is_over = "over" in market_lower
        is_under = "under" in market_lower
        # Need the pitcher's K count — fetch from MLB live boxscore.
        ks = await _fetch_pitcher_strikeouts_from_boxscore(game.get("id"), selection)
        if ks is None:
            return None
        if is_over:
            if ks > line:
                return "won"
            if ks < line:
                return "lost"
            return "push"
        if is_under:
            if ks < line:
                return "won"
            if ks > line:
                return "lost"
            return "push"
        return None

    return None  # unsupported market → pending


async def _fetch_pitcher_strikeouts_from_boxscore(game_pk: Optional[str], pitcher_name: str) -> Optional[int]:
    """Pull the pitcher's K total from MLB Stats API boxscore for a game.
    Returns None on any failure (network, JSON shape, name mismatch)."""
    if not game_pk or not pitcher_name:
        return None
    # The internal id stored by `fetch_mlb_scores` is prefixed with
    # "mlb_" (e.g. "mlb_824908") so it never collides with other sport
    # ids. The MLB Stats API needs the bare numeric gamePk, so strip it.
    gp = str(game_pk)
    if gp.startswith("mlb_"):
        gp = gp[4:]
    try:
        import httpx
        url = f"https://statsapi.mlb.com/api/v1/game/{gp}/boxscore"
        async with httpx.AsyncClient(timeout=12.0) as cx:
            r = await cx.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    target = pitcher_name.strip().lower()
    for side in ("home", "away"):
        team_block = (data.get("teams") or {}).get(side) or {}
        for _pid, info in (team_block.get("players") or {}).items():
            person = info.get("person") or {}
            full = (person.get("fullName") or "").lower()
            # Use a tolerant match: pitcher_name may be "Gerrit Cole" or
            # last-name only ("Cole"). Boxscore fullName is always full.
            if full == target or target in full or full.split()[-1] == target.split()[-1]:
                stats = ((info.get("stats") or {}).get("pitching") or {})
                ks = stats.get("strikeOuts")
                if isinstance(ks, int):
                    return ks
                try:
                    return int(ks)
                except (TypeError, ValueError):
                    return None
    return None


# ──────────────────────────────────────────────────────────────────────────
# Soccer (lightweight — uses existing soccer pipeline cache when available)
# ──────────────────────────────────────────────────────────────────────────

async def _settle_soccer_leg(leg: dict) -> Optional[str]:
    """Best-effort soccer leg settlement.

    Primary path (added 2026-06-24): ESPN soccer scoreboard + summary.
    ESPN exposes finished scores AND goal events across a broad set of
    leagues (WC, Big-5, Brazil, Mexico, Argentina, …) without auth,
    which lets us settle:
        • Moneyline / Win or Draw / Double Chance
        • Total Goals Over/Under
        • Both Teams to Score
        • Anytime Goal Scorer

    Fallback path: legacy cached soccer_matches mongo collection
    (populated by older builds — kept so unit tests still pass and so a
    network-degraded ESPN doesn't kill settlement).
    """
    if not (leg.get("event") and leg.get("market")):
        return None
    # ESPN primary
    try:
        from soccer_espn_settle import settle_soccer_leg as _espn_settle
        result = await _espn_settle(leg)
        if result in ("won", "lost", "void", "push"):
            return result
    except Exception as e:
        logger.warning("ESPN soccer settle failed for %s / %s: %s",
                       leg.get("event"), leg.get("market"), e)

    # FotMob fallback — universal coverage (Finnish Veikkausliiga,
    # Lithuanian A Lyga, every league in the world). ESPN-first is
    # preferred because ESPN's public API is more stable / less likely
    # to add bot protection.
    try:
        from soccer_fotmob_settle import settle_soccer_leg as _fotmob_settle
        result = await _fotmob_settle(leg)
        if result in ("won", "lost", "void", "push"):
            return result
    except Exception as e:
        logger.warning("FotMob soccer settle failed for %s / %s: %s",
                       leg.get("event"), leg.get("market"), e)

    # Legacy fallback: cached soccer_matches mongo collection. Kept for
    # backward-compat — the existing unit tests stub `from server import
    # db` so this path remains exercised.
    return await _settle_soccer_leg_legacy(leg)


async def _settle_soccer_leg_legacy(leg: dict) -> Optional[str]:
    """Original soccer settler from the cached `soccer_matches`
    collection. Kept as a fallback when ESPN is unavailable / when
    older parlay snapshots reference markets ESPN doesn't cover."""
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    selection = (leg.get("selection") or "").strip()
    if not event or not market:
        return None
    market_lower = market.lower()

    # Skip player-prop markets — ESPN already attempted these and we
    # don't have a scorer feed in the legacy mongo cache.
    if "goal scorer" in market_lower or "score or assist" in market_lower or "score & assist" in market_lower:
        return None

    parts = re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return None
    away_team_name, home_team_name = parts[0].strip(), parts[1].strip()

    # Look up the match score from the cached soccer_matches collection
    # (populated by the soccer pipeline's 24h backfill loop).
    try:
        from server import db  # singleton mongo client
    except Exception:
        return None
    match = await db.soccer_matches.find_one({
        "$and": [
            {"home_team": {"$regex": _escape(home_team_name), "$options": "i"}},
            {"away_team": {"$regex": _escape(away_team_name), "$options": "i"}},
        ],
        "status": {"$in": ["FINISHED", "Finished", "finished", "FT"]},
    })
    if not match:
        return None
    score = match.get("full_time_score") or match.get("score") or {}
    try:
        home_goals = int(score.get("home") if score.get("home") is not None else score.get("fullTime", {}).get("home"))
        away_goals = int(score.get("away") if score.get("away") is not None else score.get("fullTime", {}).get("away"))
    except (TypeError, ValueError):
        return None
    total_goals = home_goals + away_goals

    # ─── Moneyline ───────────────────────────────────────────────────
    if "moneyline" in market_lower:
        if not selection:
            return None
        sel_lower = selection.lower()
        if sel_lower in home_team_name.lower():
            return "won" if home_goals > away_goals else "lost"
        if sel_lower in away_team_name.lower():
            return "won" if away_goals > home_goals else "lost"
        if "draw" in sel_lower:
            return "won" if home_goals == away_goals else "lost"
        return None

    # ─── Win or Draw / Double Chance ────────────────────────────────
    if "win or draw" in market_lower or "double chance" in market_lower:
        if not selection:
            return None
        sel_lower = selection.lower()
        if sel_lower in home_team_name.lower():
            return "won" if home_goals >= away_goals else "lost"
        if sel_lower in away_team_name.lower():
            return "won" if away_goals >= home_goals else "lost"
        return None

    # ─── Total Goals ────────────────────────────────────────────────
    if "total goals" in market_lower or ("goals" in market_lower and ("over" in market_lower or "under" in market_lower)):
        m_line = re.search(r"(\d+(?:\.\d+)?)", market)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        is_over = "over" in market_lower
        is_under = "under" in market_lower
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


def _escape(s: str) -> str:
    """Escape a string for safe use in a Mongo regex."""
    return re.sub(r"([.*+?^${}()|[\]\\])", r"\\\1", s)


def _parse_iso(s: Optional[object]) -> Optional["datetime"]:
    """Tolerantly parse an ISO-8601 timestamp from a string or datetime.
    Returns None on any parse failure."""
    from datetime import datetime
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    ss = str(s).strip()
    if not ss:
        return None
    try:
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        return datetime.fromisoformat(ss)
    except Exception:
        return None
