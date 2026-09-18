"""GET /api/picks/{canonical_pick_id}/historical-intelligence
GET /api/historical-intelligence/coverage

Session 4+5 · Universal Historical Intelligence on-demand endpoint.
Lives OUTSIDE the mobile Locks lite hot path — Pick Breakdown loads
it lazily after the card opens.  Locks stays lightweight; deep
history stays reachable.
"""
from __future__ import annotations

import re
import time
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from deps import current_user
from auth import UserPublic
from services.historical_intelligence import (
    HistoricalQuery,
    HistoricalQueryFailed,
    HI_STATUS_QUERY_FAILED,
    query_historical,
    resolve_market_family,
    COVERAGE_MATRIX_SPEC,
)


def _get_db():
    from server import db as _db
    return _db


router = APIRouter(prefix="/api/picks", tags=["historical-intelligence"])
diag_router = APIRouter(
    prefix="/api/historical-intelligence", tags=["historical-intelligence"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_MARKET_FAMILIES = {
    "moneyline", "spread", "run_line", "total",
    "1x2", "handicap", "btts", "double_chance", "corners", "cards",
}

# ATD / receiving / passing / K / hits / etc are player-level.
_PLAYER_MARKET_FAMILIES = {
    "pass_yds", "pass_tds", "interceptions", "completions", "attempts",
    "rush_yds", "rush_attempts", "rush_tds", "rec_yds", "receptions",
    "rec_tds", "targets", "atd",
    "hits", "total_bases", "home_runs", "rbi", "runs", "hits_runs_rbi",
    "strikeouts", "outs", "batter_strikeouts",
    "goals", "assists", "goal_or_assist", "shots", "sot",
}


def _entity_type_for(sport: str, market_family: Optional[str],
                     pick: dict) -> str:
    """Player if the pick clearly identifies a player (name or canonical
    id) AND the market family isn't a team-only family.  Otherwise team."""
    has_player = bool(
        pick.get("canonical_player_id") or pick.get("player_id")
        or pick.get("player_name")
    )
    # Tennis is always player-level (moneyline / spread / total = per-player).
    if sport == "Tennis":
        return "player"
    if market_family in _TEAM_MARKET_FAMILIES:
        return "team"
    if market_family in _PLAYER_MARKET_FAMILIES:
        return "player"
    return "player" if has_player else "team"


def _parse_event_teams(event: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse "Away @ Home" style event strings.  Returns (home, away)."""
    if not event or "@" not in event:
        return None, None
    parts = [p.strip() for p in event.split("@")]
    if len(parts) != 2:
        return None, None
    away, home = parts
    return home or None, away or None


def _parse_team_from_market_prefix(market: str, sport: str) -> Optional[str]:
    """Strip trailing market keywords to expose the team name prefix.
    e.g. "Netherlands Moneyline" → "Netherlands"
         "Denver Broncos -3.5 Spread" → "Denver Broncos"
         "TCU Horned Frogs -8.5 Spread" → "TCU Horned Frogs"
    """
    if not market:
        return None
    m = market.strip()
    # Remove numeric / market suffixes greedily
    suffixes = [
        r"\s*[-+]?\d+(?:\.\d+)?\s*Spread\s*$",
        r"\s*[-+]?\d+(?:\.\d+)?\s*Run\s*Line\s*$",
        r"\s*Moneyline\s*$",
        r"\s*ML\s*$",
        r"\s*Total\s*(?:Points|Goals|Runs|Games)?\s*(?:Over|Under)?\s*[-+]?\d*(?:\.\d+)?\s*$",
        r"\s*1X2\s*$",
    ]
    out = m
    for pat in suffixes:
        out = re.sub(pat, "", out, flags=re.IGNORECASE)
    if out and out != m:
        return out.strip()
    return None


def _guess_team_from_market(market: str, home: Optional[str],
                            away: Optional[str]) -> Optional[str]:
    """Pick market strings prefix the team, e.g. "USC Trojans -31.5 Spread".
    We match the longer team name first for correctness."""
    if not market:
        return None
    for cand in sorted(filter(None, [home, away]), key=lambda s: -len(s or "")):
        if cand and market.startswith(cand):
            return cand
    return None


def _parse_player_from_market(market: str) -> Optional[str]:
    """Extract player name from market strings like:
       "Fernando Tatis Jr. (SD) Over 2.5 Hits + Runs + RBIs"
       "C.J. Stroud Over 215.5 Player Pass Yds"
       "Vinicius Junior Anytime Goal Scorer"
       "Sam LaPorta 3+ Receptions vs Buffalo Bills"
       "James Cook 40+ Rushing Yards  · ALT LOCK"

    Strategy: strip trailing " · ALT LOCK" tag, cut at " (TEAM)",
    " Over "/" Under "/" Anytime "/" Total ", and the "N+" alt-line
    signature ("3+"/"20+"/"175+"), then take everything to the left.
    """
    if not market:
        return None
    m = market.strip()
    # Strip trailing " · ALT LOCK" / " · ALT" suffix
    m = re.sub(r"\s*·\s*ALT.*$", "", m, flags=re.IGNORECASE).strip()
    # Cut at team abbreviation "(XYZ)" if present
    m = re.split(r"\s*\([A-Z]{2,4}\)\s*", m, maxsplit=1)[0]
    # Cut at "N+" alt-line signature (e.g., "3+", "20+", "150+")
    m = re.split(r"\s+\d+\+\b", m, maxsplit=1)[0]
    # Cut at " Over " / " Under " / " Anytime " / " Total " / " Moneyline "
    m = re.split(r"\s+(?:Over|Under|Anytime|Total|Moneyline)\b",
                 m, maxsplit=1, flags=re.IGNORECASE)[0]
    m = m.strip().rstrip(",")
    if not m or len(m) > 60:
        return None
    if m.isupper() and " " not in m:
        return None
    return m


def _resolve_entity(pick: dict, sport: str, family: Optional[str]
                    ) -> tuple[str, str, Optional[str]]:
    """Return (entity_type, entity_id, entity_name)."""
    et = _entity_type_for(sport, family, pick)
    if et == "player":
        pid = (pick.get("canonical_player_id")
               or pick.get("player_id") or "")
        pname = pick.get("player_name")
        if pname:
            # Clean pname: strip trailing " (TEAM)", "N+ X ..." alt-line
            # tail, or " Over/Under X..." suffix that some publishers put
            # in the player_name field itself.
            pname = re.sub(r"\s*\([A-Z]{2,4}\)\s*$", "", pname).strip()
            pname = re.split(r"\s+\d+\+\b", pname, maxsplit=1)[0].strip()
            pname = re.split(r"\s+(?:Over|Under|Anytime|Total|Moneyline)\b",
                             pname, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        # Fallback — parse player name from market prefix when the pick
        # was published without identity fields (very common for MLB
        # alt/compound markets).
        if not pname:
            pname = _parse_player_from_market(pick.get("market") or "")
        return "player", str(pid or pname or ""), pname
    # team
    ctid = pick.get("canonical_team_id") or ""
    team_name = None
    if ctid and not str(ctid).startswith("fallback:"):
        team_name = ctid
    if not team_name:
        team_name = _guess_team_from_market(
            pick.get("market") or "",
            pick.get("home_team"), pick.get("away_team"))
    if not team_name:
        team_name = _parse_team_from_market_prefix(
            pick.get("market") or "", sport)
    if not team_name:
        team_name = pick.get("team")
    # Game-level totals ("Total Goals Over 2.5", "Total Over 8.5") don't
    # name a team — fall back to home_team so we surface real history.
    if not team_name and family == "total":
        home = pick.get("home_team")
        if not home:
            home, _ = _parse_event_teams(pick.get("event"))
        if home:
            team_name = home
    return "team", str(team_name or ""), team_name


def _resolve_opponent(pick: dict, entity_type: str, entity_name: Optional[str]
                       ) -> tuple[Optional[str], Optional[str]]:
    """opponent_id / opponent_name.  Derive from home_team/away_team
    or from `event` string when canonical_opponent_id is empty (very
    common for player props)."""
    opp_id = pick.get("canonical_opponent_id")
    if opp_id and str(opp_id).startswith("fallback:"):
        opp_id = None
    opp_name = pick.get("opponent")
    home = pick.get("home_team"); away = pick.get("away_team")
    if not home and not away:
        home, away = _parse_event_teams(pick.get("event"))
    if not opp_name and entity_name and home and away:
        # For teams: opponent is the other of home/away.
        if entity_type == "team":
            if entity_name == home:  opp_name = away
            elif entity_name == away: opp_name = home
            else:
                # partial match (e.g. entity "Netherlands" == home)
                enm = entity_name.lower()
                if home and enm in home.lower(): opp_name = away
                elif away and enm in away.lower(): opp_name = home
        else:
            # For players — need player's team to know which side is theirs.
            team_context = (pick.get("player_team_name")
                            or pick.get("player_team"))
            if team_context:
                tc = team_context.lower()
                if home and tc in home.lower():  opp_name = away
                elif away and tc in away.lower(): opp_name = home
    return opp_id, opp_name


def _resolve_side(market: str) -> str:
    m = (market or "").lower()
    if "under" in m: return "under"
    if "over" in m:  return "over"
    if "moneyline" in m: return "ml"
    if " ml" in m:    return "ml"
    if "spread" in m or "handicap" in m or "run line" in m: return "cover"
    if "1x2" in m or "double chance" in m or "match result" in m: return "cover_ml_1x2"
    if "btts" in m: return "cover_ml_1x2"
    if "goal scorer" in m or "anytime" in m or "to score" in m: return "over"
    return "over"


def _resolve_threshold(pick: dict, family: Optional[str] = None
                       ) -> Optional[float]:
    for key in ("line", "point", "spread", "total", "published_line"):
        v = pick.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except Exception:
            continue
    # Extract from market string, e.g. "Joe Burrow Over 225.5 Passing Yards"
    market = pick.get("market") or ""
    m = re.search(r"[-+]?\d+(?:\.\d+)?", market)
    if m:
        try:
            return float(m.group(0))
        except Exception:
            pass
    # Family-based sensible defaults for binary/anytime markets
    if family in ("atd", "goals", "goal_or_assist", "btts", "double_chance",
                  "moneyline", "1x2"):
        return 0.5
    return None


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

def _served_by(request: Request, db) -> dict[str, Any]:
    """Backend fingerprint every surface can display verbatim.  When two
    clients (Preview/Web vs Expo Go) disagree, comparing this block
    proves in one glance whether they talked to the same backend + DB."""
    import os
    return {
        "host": request.headers.get("host"),
        "db": getattr(db, "name", None),
        "pid": os.getpid(),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@router.get("/{pick_id}/historical-intelligence")
async def historical_intelligence(
    request: Request,
    user: Annotated[UserPublic, Depends(current_user)],
    pick_id: str,
    sample_scope: str = Query("L10", pattern="^(L5|L10|L20|SEASON|ALL)$"),
    venue_scope:  str = Query("ALL", pattern="^(ALL|HOME|AWAY)$"),
    context_scope: Optional[str] = None,
):
    t0 = time.perf_counter()
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id})
    if not pick:
        raise HTTPException(404, "canonical pick not found")

    sport         = pick.get("sport") or ""
    market        = pick.get("market") or ""
    family        = resolve_market_family(sport, market)
    entity_type, entity_id, entity_name = _resolve_entity(pick, sport, family)
    opponent_id, opponent_name = _resolve_opponent(pick, entity_type, entity_name)

    # Tennis: opponent comes from event/home/away as the OTHER player.
    if sport == "Tennis" and not opponent_name:
        home = pick.get("home_team"); away = pick.get("away_team")
        if not (home and away):
            home, away = _parse_event_teams(pick.get("event"))
        if entity_name and home and away:
            if entity_name.lower() in (home or "").lower(): opponent_name = away
            elif entity_name.lower() in (away or "").lower(): opponent_name = home
            else: opponent_name = away if home == entity_name else home
        # Never let stringly-typed "tp:name" pass through as opponent_id
        if opponent_id and str(opponent_id).startswith("tp:"):
            opponent_id = None

    q = HistoricalQuery(
        sport=sport,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        opponent_id=opponent_id,
        opponent_name=opponent_name,
        market_family=family or "",
        current_threshold=_resolve_threshold(pick, family),
        sample_scope=sample_scope,
        venue_scope=venue_scope,
        context_scope=context_scope,
        side=_resolve_side(market),
    )
    try:
        resp = await query_historical(db, q)
    except HistoricalQueryFailed as exc:
        # 5xx so the client retry ladder engages; body carries the honest
        # status so the UI renders QUERY FAILED (retryable), never
        # "NO RECENT HISTORY".
        raise HTTPException(
            status_code=503,
            detail={
                "status": HI_STATUS_QUERY_FAILED,
                "message": "Historical Intelligence query failed — history source could not be consulted.",
                "sport": exc.sport,
                "pick_id": pick_id,
                "served_by": _served_by(request, db),
            },
        )
    d = resp.to_dict()
    d["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    d["served_by"] = _served_by(request, db)
    d["pick"] = {
        "id":            pick.get("id"),
        "sport":         sport,
        "market":        market,
        "line":          pick.get("line") or pick.get("point"),
        "book_odds":     pick.get("book_odds"),
        "home_team":     pick.get("home_team"),
        "away_team":     pick.get("away_team"),
        "player_name":   pick.get("player_name"),
        "player_team":   pick.get("player_team_name") or pick.get("player_team"),
        "opponent":      opponent_name,
        "commence_time": pick.get("commence_time") or pick.get("event_time"),
    }
    return d


# ---------------------------------------------------------------------------
# Coverage matrix diagnostic endpoint
# ---------------------------------------------------------------------------

async def _coverage_row(db, sport: str, family: str, label: str) -> dict[str, Any]:
    """Report REAL coverage numbers for one sport × market family."""
    row = {"sport": sport, "market_family": family, "label": label,
           "observations": 0, "identity_pct": 0.0, "opponent_pct": 0.0,
           "raw_actual_pct": 0.0, "home_away_pct": 0.0,
           "exact_line_capable": False, "distribution_capable": False,
           "vs_opp_capable": False, "status": "NOT CERTIFIED",
           "source": None}

    if sport == "NFL":
        if family in {"pass_yds","rush_yds","rec_yds","receptions","pass_tds","atd"}:
            row["source"] = "player_game_actuals"
            n = await db.player_game_actuals.count_documents({"sport": "nfl"})
            id_n = await db.player_game_actuals.count_documents(
                {"sport": "nfl", "canonical_player_id": {"$ne": None}})
            opp_n = await db.player_game_actuals.count_documents(
                {"sport": "nfl", "canonical_opponent_id": {"$ne": None}})
            ha_n = await db.player_game_actuals.count_documents(
                {"sport": "nfl", "home_away": {"$in": ["home","away"]}})
            row["observations"] = n
            row["identity_pct"] = round(id_n/max(1,n),3)
            row["opponent_pct"] = round(opp_n/max(1,n),3)
            row["home_away_pct"] = round(ha_n/max(1,n),3)
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0
        else:  # team family
            row["source"] = "team_game_actuals"
            n = await db.team_game_actuals.count_documents({"sport": "nfl"})
            row["observations"] = n
            row["identity_pct"] = 1.0 if n > 0 else 0.0
            row["opponent_pct"] = 1.0 if n > 0 else 0.0
            row["home_away_pct"] = 1.0 if n > 0 else 0.0
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0

    elif sport == "MLB":
        if family in {"moneyline","run_line","total"}:
            row["source"] = "team_game_actuals"
            n = await db.team_game_actuals.count_documents({"sport": "mlb"})
            row["observations"] = n
            row["identity_pct"] = 1.0 if n > 0 else 0.0
            row["opponent_pct"] = 1.0 if n > 0 else 0.0
            row["home_away_pct"] = 1.0 if n > 0 else 0.0
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0
        else:
            row["source"] = "player_game_actuals"
            n = await db.player_game_actuals.count_documents({"sport": "mlb"})
            id_n = await db.player_game_actuals.count_documents(
                {"sport": "mlb", "canonical_player_id": {"$ne": None}})
            opp_n = await db.player_game_actuals.count_documents(
                {"sport": "mlb", "canonical_opponent_id": {"$ne": None}})
            ha_n = await db.player_game_actuals.count_documents(
                {"sport": "mlb", "home_away": {"$in": ["home","away"]}})
            row["observations"] = n
            row["identity_pct"] = round(id_n/max(1,n),3)
            row["opponent_pct"] = round(opp_n/max(1,n),3)
            row["home_away_pct"] = round(ha_n/max(1,n),3)
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0

    elif sport == "Soccer":
        if family in {"1x2","handicap","total","btts","double_chance"}:
            row["source"] = "soccer_matches"
            n = await db.soccer_matches.count_documents({"status": "finished"})
            row["observations"] = n
            row["identity_pct"] = 1.0 if n > 0 else 0.0
            row["opponent_pct"] = 1.0 if n > 0 else 0.0
            row["home_away_pct"] = 1.0 if n > 0 else 0.0
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0
        elif family in {"goals","assists","goal_or_assist","shots","sot"}:
            row["source"] = "soccer_player_game_logs"
            n = await db.soccer_player_game_logs.count_documents({})
            id_n = await db.soccer_player_game_logs.count_documents({"player_id": {"$ne": None}})
            row["observations"] = n
            row["identity_pct"] = round(id_n/max(1,n),3)
            row["opponent_pct"] = 1.0 if n > 0 else 0.0
            row["home_away_pct"] = 1.0 if n > 0 else 0.0
            row["raw_actual_pct"] = 1.0 if n > 0 else 0.0
        else:  # corners/cards
            row["source"] = None
            row["observations"] = 0

    elif sport == "Tennis":
        row["source"] = "tennis_matches_history"
        n = await db.tennis_matches_history.count_documents({})
        row["observations"] = n
        row["identity_pct"] = 1.0 if n > 0 else 0.0
        row["opponent_pct"] = 1.0 if n > 0 else 0.0
        row["home_away_pct"] = 0.0  # tennis has no home/away
        row["raw_actual_pct"] = 1.0 if n > 0 else 0.0

    elif sport == "CFB":
        row["source"] = "games (sport=cfb)"
        n = await db.games.count_documents({"sport": "cfb", "status": "Final"})
        row["observations"] = n
        row["identity_pct"] = 1.0 if n > 0 else 0.0
        row["opponent_pct"] = 1.0 if n > 0 else 0.0
        row["home_away_pct"] = 1.0 if n > 0 else 0.0
        row["raw_actual_pct"] = 1.0 if n > 0 else 0.0

    # Capability flags — need at least raw actuals + identity to be useful
    cap = (row["observations"] > 0 and row["raw_actual_pct"] > 0.5
           and row["identity_pct"] > 0.5)
    row["exact_line_capable"]  = cap
    row["distribution_capable"] = cap
    row["vs_opp_capable"] = cap and row["opponent_pct"] > 0.5
    row["status"] = "CERTIFIED" if cap else "NOT CERTIFIED"
    return row


@diag_router.get("/coverage")
async def coverage_matrix(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Universal Historical Intelligence coverage matrix.

    Reports REAL per-sport × market coverage — observations, identity %,
    exact-current-line capability, distribution capability, VS OPP
    capability.  Any row without sufficient raw actuals is honestly
    marked NOT CERTIFIED.  This endpoint is DIAGNOSTIC ONLY —
    it never modifies picks, scoring, or the Locks board.
    """
    db = _get_db()
    rows: list[dict[str, Any]] = []
    for sport, families in COVERAGE_MATRIX_SPEC.items():
        for family, label in families:
            rows.append(await _coverage_row(db, sport, family, label))
    summary = {
        "total_rows": len(rows),
        "certified": sum(1 for r in rows if r["status"] == "CERTIFIED"),
        "not_certified": sum(1 for r in rows if r["status"] == "NOT CERTIFIED"),
    }
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "summary": summary,
            "rows": rows}
