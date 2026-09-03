"""Pick → Matchup Intelligence wiring (2026-07-28).

Bridges the production `picks` collection to the read-only
`player_matchup_intelligence` and `nfl_matchup_intelligence` engines.

──────────────────────────────────────────────────────────────────────
Contract
──────────────────────────────────────────────────────────────────────
    payload = await build_matchup_payload(db, pick_doc)

    → dict with schema:
        {
          "supported": bool,          # False → sport/market unhandled
          "sport": str,
          "player_name": str,
          "opponent_team": str | None,
          "stat": str,                # canonical stat key
          "threshold": float | None,  # over/under line if any
          "matchup_grade": "A+..F",
          "sample_confidence": "high|medium|low|none",
          "sample_size": int,
          "threshold_hit_rate": float,
          "avg_stat_output": float,
          "consistency_score": float,
          "career_vs_opponent": { games, avg, hit_rate, ... },
          "recent_vs_similar":  { ... },
          "overall_last_10":    { ... },
          "overall_season":     { ... },
          "last_meeting": { ... } | None,  # NFL only
          "stat_lines":  { ... } | None,   # NFL only (multi-stat)
          "data_sources_used": [...],
          "notes": [...],
        }

Zero writes. Zero HTTP calls. Empty payloads are returned when parsing
fails or the sport/market isn't supported — never raises.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.pick_matchup_wiring")

# ─────────────────────────────────────────────────────────────────────
# Market → canonical stat key
# ─────────────────────────────────────────────────────────────────────
# Ordered by specificity: check longer/more specific patterns first.
_MLB_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"hits.*runs.*rbis|hrri|hits\s*\+\s*runs\s*\+\s*rbis", re.I),
     "hits_runs_rbis"),
    (re.compile(r"total\s+bases", re.I),  "total_bases"),
    (re.compile(r"home\s*runs?|\bhr\b", re.I), "home_runs"),
    (re.compile(r"\brbis?\b", re.I),       "rbi"),
    (re.compile(r"\bhits?\b", re.I),       "hits"),
    (re.compile(r"strikeouts?|\bks?\b", re.I), "strikeouts"),
]

_NFL_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pass.*yard", re.I),      "passing_yards"),
    (re.compile(r"pass.*td|pass.*touchdown", re.I), "passing_tds"),
    (re.compile(r"pass.*attempt", re.I),   "attempts"),
    (re.compile(r"pass.*completion", re.I),"completions"),
    (re.compile(r"interception|\bint\b", re.I), "passing_ints"),
    (re.compile(r"rush.*yard|rushing", re.I), "rushing_yards"),
    (re.compile(r"rush.*td", re.I),        "rushing_tds"),
    (re.compile(r"carr(y|ies)", re.I),     "carries"),
    (re.compile(r"recept", re.I),          "receptions"),
    (re.compile(r"recv.*yard|receiving.*yard", re.I), "receiving_yards"),
    (re.compile(r"recv.*td|receiving.*td", re.I),     "receiving_tds"),
    (re.compile(r"target", re.I),          "targets"),
]

_NBA_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"rebound", re.I),                          "rebounds"),
    (re.compile(r"assist", re.I),                           "assists"),
    # 3PT / 3-pointer / three-pointer variants — must precede `point`
    (re.compile(
        r"three|3\s*-?\s*p(?:t|oint)|3s?\s*made",
        re.I), "threes_made"),
    (re.compile(r"steal", re.I),                            "steals"),
    (re.compile(r"block", re.I),                            "blocks"),
    # `point` last so "3-Pointers" doesn't match here first.
    (re.compile(r"point", re.I),                            "points"),
]

_TENNIS_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ace", re.I),                     "aces"),
    (re.compile(r"double\s*fault", re.I),          "double_faults"),
    (re.compile(r"break\s*point", re.I),           "break_points_won"),
    (re.compile(r"total\s*games", re.I),           "total_games"),
]

# ─────────────────────────────────────────────────────────────────────
# Soccer market → stat map (Phase 7 Part 4c, 2026-06)
# ─────────────────────────────────────────────────────────────────────
# Order matters — most specific first. "goal_contributions" (G+A) must
# match before plain "goals" or "assists" so composite markets don't
# collapse into one component. "shots on target" must match before
# plain "shots" for the same reason.
_SOCCER_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    # Composite (G+A) — MUST come first, before individual G / A regexes.
    (re.compile(r"to\s*score\s*or\s*assist|score\s*or\s*assist|"
                 r"goal\s*contribut|goals?\s*\+\s*assists?|"
                 r"goals?\s*&\s*assists?",
                 re.I),                                 "goal_contributions"),
    (re.compile(r"shots?\s*on\s*target|sot\b", re.I),   "shots_on_target"),
    # Assists AFTER goal_contributions so "score or assist" isn't captured
    # by "assist" alone.
    (re.compile(r"anytime\s*assist|to\s*assist|assists?", re.I),
                                                        "assists"),
    (re.compile(r"anytime\s*(?:goal\s*)?scorer|to\s*score|"
                 r"first\s*(?:goal\s*)?scorer|player\s*(?:to\s*)?score|"
                 r"\bgoals?\b",
                 re.I),                                 "goals"),
    (re.compile(r"\bxg\b|expected\s*goals?", re.I),     "xg"),
    (re.compile(r"\bshots?\b", re.I),                   "shots"),
]

# Match-level (non-player) SOCCER markets — safe-skip.
_SOCCER_MATCH_LEVEL_RE = re.compile(
    r"both\s*teams\s*to\s*score|btts\b|"
    r"total\s*goals?|total\s*(?:corners?|cards?|bookings?)|"
    r"first\s*half\s*result|correct\s*score|double\s*chance|"
    r"handicap|asian\s*handicap",
    re.I,
)


def _detect_stat(sport: str, market: str) -> Optional[str]:
    sport_u = (sport or "").upper()
    market_s = market or ""
    if sport_u == "MLB":
        table = _MLB_MARKET_STAT_MAP
    elif sport_u == "NFL":
        table = _NFL_MARKET_STAT_MAP
    elif sport_u == "NBA":
        table = _NBA_MARKET_STAT_MAP
    elif sport_u == "TENNIS":
        table = _TENNIS_MARKET_STAT_MAP
    elif sport_u == "SOCCER":
        # Match-level (non-player) soccer markets safe-skip first.
        if _SOCCER_MATCH_LEVEL_RE.search(market_s):
            return None
        table = _SOCCER_MARKET_STAT_MAP
    else:
        return None
    for pat, key in table:
        if pat.search(market_s):
            return key
    return None


# ═════════════════════════════════════════════════════════════════════
# ALT-LINE MAGIC / MATCHUP UNIVERSAL STAT RESOLVER (2026-06-30)
# ─────────────────────────────────────────────────────────────────────
# ``_detect_stat`` returns a stat family key from a raw market string
# alone — that string is IDENTICAL for MLB batter and pitcher
# strikeouts ("... Strikeouts"), so both collapse to the "strikeouts"
# key.  Downstream models & threshold grids key them separately:
#     (MLB, strikeouts)         → BATTER grid  [0.5–3.5]
#     (MLB, pitcher_strikeouts) → PITCHER grid [3.5–11.5]
# A pitcher K prop that lands on the batter grid returns
# ``supported: False`` from every threshold (no batter K model exists
# for a pitcher), silently emptying the Alt-Line Magic bundle.
#
# ``resolve_market_stat`` is the ONE authoritative resolver every
# consumer (Alt-Line Magic, Matchup Intelligence, Similar-Matchup
# Engine, Prop H2H) uses to route a pick into the correct stat
# family before downstream inference.  It layers ordered signals on
# top of ``_detect_stat`` — no new fabricated data, only correct
# routing:
#
#   1. canonical_market_family (definitive; set by canonical
#      publication contract at generation time).
#   2. provider_market_key (definitive; from the Odds API market
#      key ingested by ``alt_lines_feed`` / bulk odds).
#   3. Market suffix " · ALT LOCK" — emitted by
#      ``sports_engine._prop_market_label`` ONLY when a pick is an
#      alt-line variant.  MLB Odds API SPORT_CONFIG has no batter-K
#      alt market (only ``pitcher_strikeouts_alternate``), so this
#      marker on a Strikeouts market is a definitive PITCHER tag.
#   4. Threshold heuristic: line ≥ 3.5 → pitcher (batter K props
#      are quoted 0.5/1.5/2.5 in practice; the pitcher grid starts
#      at 3.5 and runs to 11.5).
# ═════════════════════════════════════════════════════════════════════
def resolve_market_stat(
    sport: str,
    market: str,
    *,
    pick: Optional[dict] = None,
    threshold: Optional[float] = None,
) -> Optional[str]:
    """Return the canonical stat family key for a pick's market.

    Wraps ``_detect_stat`` and applies universal family disambiguation
    that requires more than the raw market string alone.  Backwards-
    compatible when called with only ``sport`` + ``market`` — it will
    return the same result as ``_detect_stat`` unless a pick / threshold
    pair unlocks a more specific family (e.g. pitcher_strikeouts).
    """
    stat = _detect_stat(sport, market)
    if not stat:
        return None
    sport_u = (sport or "").upper()

    # MLB · BATTER vs PITCHER Strikeouts disambiguation.
    if sport_u == "MLB" and stat == "strikeouts":
        cmf = ""
        pmk = ""
        if isinstance(pick, dict):
            cmf = (pick.get("canonical_market_family") or "").lower()
            pmk = (pick.get("provider_market_key")
                    or pick.get("market_key") or "").lower()
        market_l = (market or "").lower()
        # Line comes from explicit arg OR the market string.
        line_val: Optional[float] = None
        if isinstance(threshold, (int, float)):
            line_val = float(threshold)
        else:
            _tm = _THRESHOLD_RE.search(market or "")
            if _tm:
                try:
                    line_val = float(_tm.group(1))
                except (TypeError, ValueError):
                    line_val = None
        is_pitcher = (
            cmf.startswith("pitcher_strikeouts")
            or "pitcher_strikeouts" in pmk
            or "alt lock" in market_l
            or (line_val is not None and line_val >= 3.5)
        )
        if is_pitcher:
            return "pitcher_strikeouts"

    return stat


# ─────────────────────────────────────────────────────────────────────
# Player + threshold parsing
# ─────────────────────────────────────────────────────────────────────
# Handles common market string shapes:
#   "Noah Cameron (KC) Over 2.5 Strikeouts  · ALT LOCK"
#   "Aaron Judge Over 1.5 Total Bases"
#   "Joe Burrow Over 249.5 Passing Yards"
_THRESHOLD_RE = re.compile(
    r"(?:Over|Under|O|U)\s+(-?\d+(?:\.\d+)?)", re.I,
)
_PLAYER_PARENS_RE = re.compile(r"^([A-Z][A-Za-z\.'\-\s]+?)\s*\(([A-Z0-9]{2,4})\)")
_MONEYLINE_RE = re.compile(r"moneyline|winner", re.I)
_TEAM_RE = re.compile(r"total\s*(?:runs|goals|points|score)", re.I)
# Tennis-specific: match-level game totals / spreads are NOT player props.
_TENNIS_MATCH_LEVEL_RE = re.compile(
    r"total\s*games|^\s*(?:over|under)\s+-?\d+(?:\.\d+)?\s+games?\b|"
    r"[+\-]\d+(?:\.\d+)?\s*(?:spread|games?)?\s*$",
    re.I,
)


def _parse_player_and_team_abbr(pick: dict) -> tuple[str, Optional[str]]:
    """Extract (player_name, team_abbrev) from selection + market.

    Returns ("", None) if the pick is a team/moneyline market — matchup
    intelligence isn't applicable to those.
    """
    market = pick.get("market") or ""
    selection = pick.get("selection") or ""
    sport = (pick.get("sport") or "").upper()
    # Team markets short-circuit — no player.
    if _MONEYLINE_RE.search(market) or _TEAM_RE.search(market):
        return "", None
    # Tennis match-level game totals / spreads have no player anchor.
    if sport == "TENNIS" and _TENNIS_MATCH_LEVEL_RE.search(market):
        return "", None
    # MLB / player-with-team-abbrev shape.
    m = _PLAYER_PARENS_RE.match(market)
    if m:
        return m.group(1).strip(), m.group(2).upper()
    # Fallback: use selection as player name (props usually store the
    # player in `selection`).
    if selection:
        s = selection.strip()
        # Reject direction words that leak in for game/team-total markets.
        if s.lower() in {"over", "under"}:
            return "", None
        return s, None
    return "", None


def _parse_threshold(market: str) -> Optional[float]:
    m = _THRESHOLD_RE.search(market or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


# Soccer implicit-threshold markets: "Anytime Goal Scorer",
# "First Goal Scorer", "To Score or Assist", etc. all mean "≥ 1".
# Standard books quote them as 0.5-line binary props. This helper is
# used by the fusion parser to infer the threshold when the market
# string doesn't contain a numeric line.
_SOCCER_IMPLICIT_HALF_RE = re.compile(
    r"anytime\s*(?:goal\s*)?scorer|first\s*(?:goal\s*)?scorer|"
    r"to\s*score\s*or\s*assist|to\s*score|"
    r"anytime\s*assist",
    re.I,
)


def _infer_soccer_threshold(market: str) -> Optional[float]:
    """Return 0.5 for implicit-≥1 soccer markets, else None."""
    if not market:
        return None
    if _SOCCER_IMPLICIT_HALF_RE.search(market):
        return 0.5
    return None


# ─────────────────────────────────────────────────────────────────────
# Name → MLB player_id resolution (cached)
# ─────────────────────────────────────────────────────────────────────
# Both `player_game_logs` and `props_history` use the MLB Stats API
# player_id integer — never the player name. We resolve the id via the
# same helper the H2H endpoint uses, and cache per-name in-process.
_MLB_PID_CACHE: dict[str, Optional[int]] = {}


async def _resolve_mlb_player_id(name: str) -> Optional[int]:
    if not name:
        return None
    if name in _MLB_PID_CACHE:
        return _MLB_PID_CACHE[name]
    pid: Optional[int] = None
    # 1. Try `mlb_bvp.lookup_player_id` (batter route) — free, cached.
    try:
        from mlb_bvp import lookup_player_id as _bvp_lookup   # type: ignore
        pid = await _bvp_lookup(name)
    except Exception:
        pid = None
    # 2. Fallback to the pitcher-search endpoint.
    if not pid:
        try:
            from mlb_pitcher_h2h import _resolve_pitcher_id  # type: ignore
            pid = await _resolve_pitcher_id(name)
        except Exception:
            pid = None
    _MLB_PID_CACHE[name] = pid
    return pid


def _parse_opponent_mlb(event: str, pitcher_abbrev: Optional[str]) -> Optional[str]:
    """Delegate to `mlb_pitcher_h2h.resolve_opp_team_name` when possible."""
    if not event:
        return None
    if pitcher_abbrev:
        try:
            from mlb_pitcher_h2h import resolve_opp_team_name  # lazy
            return resolve_opp_team_name(event, pitcher_abbrev)
        except Exception:
            pass
    # Fallback: split on @/vs and return whichever half doesn't match
    # the pick's home_team/away_team hint.
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    return parts[1].strip()   # best-effort


def _parse_opponent_generic(event: str, own_team_hint: Optional[str]) -> Optional[str]:
    """For sports where we don't have an abbreviation → team resolver.

    Splits event on @/vs; if `own_team_hint` is present we return the
    OTHER side; otherwise we return the second side (best-effort).
    """
    if not event:
        return None
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if own_team_hint:
        oh = own_team_hint.lower()
        if oh in a.lower() or a.lower() in oh:
            return b
        if oh in b.lower() or b.lower() in oh:
            return a
    return b


def _parse_opponent_tennis(event: str, player_name: Optional[str]) -> Optional[str]:
    """Tennis: use the player's name to disambiguate which side of the
    event string is the opponent.

    Tennis events come as "Player A @ Player B" or "Player A vs Player B"
    (also "Bertea E. / Pace F. vs Pieri T. / Tsygourov" for doubles).
    We do a fuzzy last-name / first-initial match so "Alcaraz" resolves
    inside "Carlos Alcaraz" and "Alcaraz C." both.
    """
    if not event:
        return None
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not player_name:
        return b       # best-effort
    p_low = player_name.lower().strip()
    a_low, b_low = a.lower(), b.lower()
    # Exact / substring first.
    if p_low in a_low or a_low in p_low:
        return b
    if p_low in b_low or b_low in p_low:
        return a
    # Last-name fallback: try match on any token ≥ 3 chars.
    for tok in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ']{3,}", p_low):
        if tok in a_low:
            return b
        if tok in b_low:
            return a
    return b


# ─────────────────────────────────────────────────────────────────────
# Soccer opponent resolver (Phase 7 Part 4c)
# ─────────────────────────────────────────────────────────────────────
# Soccer picks store the event as "Team A @ Team B" and the market as
# "{Player} Anytime Goal Scorer" — no team abbrev. We look up the
# player's most-recent team in soccer_player_game_logs and return the
# opposite side of the fixture.
_SOCCER_TEAM_CACHE: dict[str, Optional[str]] = {}


async def _resolve_soccer_player_team(db, player_name: str) -> Optional[str]:
    if not player_name:
        return None
    if player_name in _SOCCER_TEAM_CACHE:
        return _SOCCER_TEAM_CACHE[player_name]
    import unicodedata
    def _canon(s: str) -> str:
        d = "".join(c for c in unicodedata.normalize("NFKD", s)
                     if not unicodedata.combining(c))
        return re.sub(r"\s+", " ",
                       re.sub(r"[\.\-'\"\u2019]", "", d).strip().lower())
    name_c = _canon(player_name)
    doc = await db.soccer_player_game_logs.find_one(
        {"name_canonical": name_c},
        {"_id": 0, "team_name": 1},
        sort=[("match_date", -1)],
    )
    team = doc["team_name"] if doc else None
    _SOCCER_TEAM_CACHE[player_name] = team
    return team


async def _parse_opponent_soccer(db, event: str,
                                   player_name: Optional[str]) -> Optional[str]:
    """Resolve opponent for a soccer pick by looking up the player's team."""
    if not event:
        return None
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not player_name:
        return b
    team = await _resolve_soccer_player_team(db, player_name)
    if team:
        t_low = team.lower()
        if t_low in a.lower() or a.lower() in t_low:
            return b
        if t_low in b.lower() or b.lower() in t_low:
            return a
    # Fallback: last-name substring match (rare — teams usually anchor).
    p_low = player_name.lower()
    for tok in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ']{3,}", p_low):
        if tok in a.lower():
            return b
        if tok in b.lower():
            return a
    return b


# ─────────────────────────────────────────────────────────────────────
# Empty payload builder
# ─────────────────────────────────────────────────────────────────────
def _empty_payload(sport: str, reason: str) -> dict:
    return {
        "supported": False,
        "sport": sport,
        "reason": reason,
        "matchup_grade": None,
        "sample_confidence": "none",
        "sample_size": 0,
        "notes": [reason],
    }


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def build_matchup_payload(db, pick: dict) -> dict:
    """Build the Matchup Intelligence payload for a single pick.

    Never raises — errors are folded into a `notes` list. Always
    returns a dict the frontend can render defensively.
    """
    sport = (pick.get("sport") or "").strip()
    market = pick.get("market") or ""
    event = pick.get("event") or ""

    if not sport:
        return _empty_payload(sport, "pick missing sport")

    # 1. Player + team abbrev.
    player_name, team_abbr = _parse_player_and_team_abbr(pick)
    if not player_name:
        return _empty_payload(sport, "team/moneyline market — no player matchup")

    # 2. Stat — universal resolver (routes MLB pitcher K props to the
    #    pitcher_strikeouts family so Matchup Intelligence hits the
    #    correct signal set, matching Alt-Line Magic).
    stat = resolve_market_stat(sport, market, pick=pick)
    if not stat:
        return _empty_payload(sport, f"unrecognised stat in market: {market!r}")

    # 3. Threshold — explicit line in market, else infer implicit for
    #     soccer "Anytime Goal Scorer" / "To Score or Assist" markets.
    threshold = _parse_threshold(market)
    if threshold is None and sport.upper() == "SOCCER":
        threshold = _infer_soccer_threshold(market)

    # 4. Opponent.
    if sport.upper() == "MLB":
        opponent = _parse_opponent_mlb(event, team_abbr)
    elif sport.upper() == "TENNIS":
        # Tennis: anchor off the player's name (not the team abbrev,
        # which doesn't exist for tennis) so we pick the OPPOSITE side
        # of the "Player A @ Player B" event string.
        opponent = _parse_opponent_tennis(event, player_name)
    elif sport.upper() == "SOCCER":
        # Soccer: look up the player's most-recent team from
        # `soccer_player_game_logs` to pick the OPPOSITE side of the
        # "Team A @ Team B" event string.
        opponent = await _parse_opponent_soccer(db, event, player_name)
    else:
        # Use home_team / away_team hint if present, else naïve split.
        own_hint = None
        if team_abbr:
            own_hint = team_abbr
        opponent = _parse_opponent_generic(event, own_hint)

    # 5. Route to the right engine.
    try:
        if sport.upper() == "NFL":
            from services.nfl_matchup_intelligence import (
                get_nfl_matchup_intelligence,
            )
            nfl_res = await get_nfl_matchup_intelligence(
                db,
                player_name=player_name,
                opponent_team=opponent or "",
            )
            # Pull the stat's threshold hit at the requested line (if any)
            grade, hit_rate = _grade_from_nfl(nfl_res, stat, threshold)
            return {
                "supported": True,
                "sport": sport,
                "player_name": player_name,
                "opponent_team": opponent,
                "stat": stat,
                "threshold": threshold,
                "matchup_grade": grade,
                "sample_confidence": nfl_res.sample_confidence,
                "sample_size": nfl_res.games_played,
                "threshold_hit_rate": hit_rate,
                "position": nfl_res.position,
                "last_meeting": nfl_res.last_meeting,
                "stat_lines": {
                    k: v.to_dict() for k, v in nfl_res.stat_lines.items()
                },
                "data_sources_used": nfl_res.data_sources_used,
                "notes": nfl_res.notes,
            }
        # Generic engine for MLB / Tennis / NBA / Soccer.
        from services.player_matchup_intelligence import (
            get_matchup_intelligence,
        )
        # MLB: resolve name → int player_id so props_history +
        # player_game_logs can be queried (both are id-keyed only).
        pid: Optional[int] = None
        if sport.upper() == "MLB":
            pid = await _resolve_mlb_player_id(player_name)
        res = await get_matchup_intelligence(
            db,
            sport=sport,
            player_name=player_name,
            stat=stat,
            player_id=pid,
            opponent_team=opponent,
            threshold=threshold,
        )
        return {
            "supported": True,
            "sport": sport,
            "player_name": player_name,
            "opponent_team": opponent,
            "stat": res.stat,
            "threshold": threshold,
            "matchup_grade": res.matchup_grade,
            "sample_confidence": res.sample_confidence,
            "sample_size": res.sample_size,
            "threshold_hit_rate": res.threshold_hit_rate,
            "avg_stat_output": res.avg_stat_output,
            "median_stat_output": res.median_stat_output,
            "consistency_score": res.consistency_score,
            "career_vs_opponent": res.career_vs_opponent.to_dict(),
            "recent_vs_similar":  res.recent_vs_similar.to_dict(),
            "overall_last_5":     res.overall_last_5.to_dict(),
            "overall_last_10":    res.overall_last_10.to_dict(),
            "overall_season":     res.overall_season.to_dict(),
            "data_sources_used": res.data_sources_used,
            "notes": res.notes,
        }
    except Exception as e:
        logger.exception("matchup engine failed for pick %s: %s",
                         pick.get("id"), e)
        return _empty_payload(sport, f"engine error: {e}")


def _grade_from_nfl(nfl_res, stat: str, threshold: Optional[float]) -> tuple[str, float]:
    """Turn an NFL matchup result into a letter grade + hit-rate.

    Uses the stat_line's threshold that's closest to the pick's
    threshold; falls back to games_played tier when no threshold set.
    """
    if not nfl_res or nfl_res.games_played == 0:
        return "F", 0.0
    sl = nfl_res.stat_lines.get(stat)
    if not sl or not sl.thresholds:
        # No stat breakdown → grade from sample confidence alone.
        conf = nfl_res.sample_confidence
        return ({"high": "B", "medium": "C", "low": "D"}.get(conf, "F"), 0.0)
    if threshold is None:
        # No pick line → use the median threshold's hit-rate.
        thr_keys = sorted(sl.thresholds.keys())
        pick_t = thr_keys[len(thr_keys) // 2]
    else:
        # Snap to nearest stored threshold.
        pick_t = min(sl.thresholds.keys(),
                     key=lambda t: abs(float(t) - float(threshold)))
    hit = sl.thresholds[pick_t]
    hr = float(hit.hit_rate)
    games = int(hit.games)
    sample_mult = min(1.0, games / 8.0)   # 8 games saturates
    raw = hr * (0.6 + 0.4 * sample_mult)
    grade = (
        "A+" if raw >= 0.85 else
        "A"  if raw >= 0.72 else
        "B"  if raw >= 0.60 else
        "C"  if raw >= 0.48 else
        "D"  if raw >= 0.34 else
        "F"
    )
    return grade, round(hr, 4)


__all__ = ["build_matchup_payload", "_parse_opponent_tennis",
             "_parse_opponent_soccer", "_resolve_soccer_player_team"]
