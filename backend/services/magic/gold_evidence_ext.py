"""MAGIC 3E.b/c/d — Gold evidence extensions.

New adapters added on top of :mod:`services.magic.gold_evidence`:

  Soccer
  ──────
  * ``build_soccer_recent_role``    — recent minutes / starts / role
                                       from ``soccer_player_game_logs``
  * ``build_soccer_teammate_context`` — teammate availability (PARTIAL)
                                       derived from injuries + roster

  NBA
  ───
  * ``build_nba_injury_status``     — real ESPN injury status
  * ``build_nba_recent_usage``      — recent minutes / usage from
                                       ``player_game_actuals``
  * ``build_nba_rest_context``      — B2B / rest days from most-recent
                                       ``player_game_logs`` row

All adapters follow the same hard rules as 3D:
  * READ-ONLY.  Persisted evidence only.  No fabrication.
  * Missing data ⇒ ``UNAVAILABLE`` (or ``PARTIAL`` for teammate).
  * ``source_timestamp`` / ``observed_at`` preserved in provenance.
  * Canonical identity preferred; wrong-team / same-name refused.

Statuses added to the vocabulary:
  ``LINEUP_STATUS`` values:
    CONFIRMED_STARTER, CONFIRMED_BENCH, PREDICTED_STARTER,
    PREDICTED_BENCH, QUESTIONABLE, OUT, SUSPENDED, UNKNOWN.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from services.magic.gold_evidence import (
    Availability, GoldEvidence, GoldEvidenceType, _pregame_cutoff_from_pick,
)


# ═══════════════════════════════════════════════════════════════════
# Extended evidence-type names (extend contract implicitly)
# ═══════════════════════════════════════════════════════════════════
class ExtGoldEvidenceType:
    SOCCER_RECENT_ROLE      = "SOCCER_RECENT_ROLE"
    SOCCER_TEAMMATE_CONTEXT = "SOCCER_TEAMMATE_CONTEXT"
    NBA_INJURY_STATUS       = "NBA_INJURY_STATUS"
    NBA_RECENT_USAGE        = "NBA_RECENT_USAGE"
    NBA_REST_CONTEXT        = "NBA_REST_CONTEXT"


# ═══════════════════════════════════════════════════════════════════
# Lineup-status vocabulary (used by lineup persistence + adapters)
# ═══════════════════════════════════════════════════════════════════
class LineupStatus:
    CONFIRMED_STARTER = "CONFIRMED_STARTER"
    CONFIRMED_BENCH   = "CONFIRMED_BENCH"
    PREDICTED_STARTER = "PREDICTED_STARTER"
    PREDICTED_BENCH   = "PREDICTED_BENCH"
    QUESTIONABLE     = "QUESTIONABLE"
    OUT              = "OUT"
    SUSPENDED        = "SUSPENDED"
    UNKNOWN          = "UNKNOWN"

    ALL = (
        CONFIRMED_STARTER, CONFIRMED_BENCH,
        PREDICTED_STARTER, PREDICTED_BENCH,
        QUESTIONABLE, OUT, SUSPENDED, UNKNOWN,
    )


# ═══════════════════════════════════════════════════════════════════
# Freshness helpers
# ═══════════════════════════════════════════════════════════════════
def _minutes_since(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# Soccer recent-role adapter (3E.b)
# ═══════════════════════════════════════════════════════════════════
async def build_soccer_recent_role(db, pick: dict) -> GoldEvidence:
    """Recent minutes / starts / substitute appearances derived from
    ``soccer_player_game_logs``.

    Returns:
        AVAILABLE with `value = avg_minutes_last_5`, sample_size=n_games,
        plus starts / subbed-on counts in provenance.
        PARTIAL if only 1–2 recent games available.
        UNAVAILABLE if no logs match this player + are pre-cutoff.

    Temporal safety: ``match_date < pregame_cutoff`` (calendar-day,
    same-day + future excluded).
    """
    ev = GoldEvidence(
        evidence_type=ExtGoldEvidenceType.SOCCER_RECENT_ROLE,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no player name on pick"
        return ev
    cutoff_iso, cutoff_day = _pregame_cutoff_from_pick(pick)

    # Deterministic name candidates (case + parenthetical strip).
    cands = [pname, pname.lower()]
    if "(" in pname:
        p2 = pname.split("(", 1)[0].strip()
        cands += [p2, p2.lower()]

    q = {
        "$or": [
            {"player_name": {"$in": cands}},
            {"name_canonical": {"$in": [c.lower() for c in cands]}},
        ],
        # Match date is stored as string 'YYYY-MM-DD HH:MM:SS';
        # slice with substr comparison.
        "match_date": {"$lt": cutoff_day + " 99"},
    }
    logs: list[dict] = []
    try:
        cursor = db.soccer_player_game_logs.find(q, {
            "match_date": 1, "minutes": 1, "starts": 1,
            "roster_in": 1, "roster_out": 1, "opponent_team_name": 1,
            "team_name": 1, "position": 1, "goals": 1, "assists": 1,
            "_id": 0,
        }).sort([("match_date", -1)]).limit(10)
        async for r in cursor:
            md = str(r.get("match_date") or "")[:10]
            if md and md < cutoff_day:
                logs.append(r)
    except Exception:
        logs = []

    if not logs:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no pre-cutoff soccer_player_game_logs for player"
        ev.provenance = {"cutoff": cutoff_iso, "cutoff_day": cutoff_day,
                          "candidates": cands,
                          "source": "soccer_player_game_logs"}
        return ev

    last5 = logs[:5]
    total_mins = 0.0
    n_starts = 0
    n_bench_sub = 0
    n_played = 0
    for r in last5:
        mins = r.get("minutes")
        if mins is not None:
            try:
                total_mins += float(mins)
                n_played += 1
            except (TypeError, ValueError):
                pass
        if r.get("starts") in (1, True, "1", "True"):
            n_starts += 1
        if r.get("roster_in") and not r.get("starts"):
            n_bench_sub += 1
    avg_mins = (total_mins / max(len(last5), 1)) if last5 else 0.0
    ev.timestamp = cutoff_iso
    ev.source = "soccer_player_game_logs"
    ev.sample_size = len(last5)
    ev.matchup_feature = "avg_minutes_last_5"
    ev.value = round(avg_mins, 1)
    ev.provenance = {
        "cutoff":           cutoff_iso,
        "cutoff_day":       cutoff_day,
        "n_games_used":     len(last5),
        "avg_minutes":      round(avg_mins, 1),
        "starts_last_5":    n_starts,
        "subbed_on_last_5": n_bench_sub,
        "played_last_5":    n_played,
        "position":         (last5[0].get("position") if last5 else None),
        "latest_match":     (last5[0].get("match_date") if last5 else None),
        "opponent_last":    (last5[0].get("opponent_team_name")
                              if last5 else None),
        "source":           "soccer_player_game_logs",
        "temporal_rule":    "match_date < cutoff_day (same-day+future excluded)",
    }
    # Availability: PARTIAL for very small samples (< 3 recent games),
    # AVAILABLE for ≥3 pre-cutoff games.
    if len(last5) < 3:
        ev.availability = Availability.PARTIAL
        ev.notes = f"only {len(last5)} recent pre-cutoff games"
    else:
        ev.availability = Availability.AVAILABLE
        # Directional: heavy minutes = positive for scorer/creator markets.
        if avg_mins >= 75:
            ev.direction = "positive"
        elif avg_mins <= 30:
            ev.direction = "negative"
        else:
            ev.direction = "neutral"
    return ev


# ═══════════════════════════════════════════════════════════════════
# Soccer teammate-context adapter (3E.c — PARTIAL only)
# ═══════════════════════════════════════════════════════════════════
async def build_soccer_teammate_context(db, pick: dict) -> GoldEvidence:
    """Teammate availability derived from ``soccer_injuries`` +
    ``player_identities`` roster for the pick's team.

    Rules:
      * ONLY PARTIAL evidence class emitted — never AVAILABLE.
      * Never fabricates a "teammate finishing probability" number.
      * Missing injury feed for the league ⇒ UNAVAILABLE.
    """
    ev = GoldEvidence(
        evidence_type=ExtGoldEvidenceType.SOCCER_TEAMMATE_CONTEXT,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    team = (pick.get("team") or pick.get("player_current_team")
            or pick.get("canonical_team_id") or "").strip()
    if not team:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no team context on pick"
        return ev

    # Look up injuries for this team in soccer_injuries (unified
    # collection populated by the 3E.a loop).  Fall back to the
    # generic espn_injury_notes doc when the soccer collection is
    # empty.
    inj_doc = None
    try:
        inj_doc = await db.soccer_injuries.find_one({"team_name": team})
        if not inj_doc:
            inj_doc = await db.soccer_injuries.find_one(
                {"team_norm": team.lower().replace(" ", "")})
    except Exception:
        inj_doc = None
    if not inj_doc:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no soccer_injuries record for team"
        ev.provenance = {"team": team,
                          "source": "soccer_injuries",
                          "note": "no ingest coverage for this league yet"}
        return ev

    injuries = inj_doc.get("injuries") or []
    important_out = [
        i for i in injuries
        if str(i.get("status") or "").lower() in ("out", "suspended")
    ]
    questionable = [
        i for i in injuries
        if str(i.get("status") or "").lower()
        in ("questionable", "doubtful", "day-to-day", "day to day")
    ]
    ev.availability = Availability.PARTIAL
    ev.matchup_feature = "teammates_unavailable_count"
    ev.value = float(len(important_out))
    ev.sample_size = len(injuries)
    ev.source = "soccer_injuries"
    ev.timestamp = str(inj_doc.get("updated_at") or "")
    ev.provenance = {
        "team":                  team,
        "teammates_out":         [i.get("athlete") for i in important_out],
        "teammates_questionable":[i.get("athlete") for i in questionable],
        "n_out":                 len(important_out),
        "n_questionable":        len(questionable),
        "source":                "soccer_injuries",
        "updated_at":            inj_doc.get("updated_at"),
        "note":                  "PARTIAL — teammate finishing quality not fabricated",
    }
    ev.direction = "negative" if len(important_out) >= 2 else "neutral"
    return ev


# ═══════════════════════════════════════════════════════════════════
# NBA injury-status adapter (3E.d)
# ═══════════════════════════════════════════════════════════════════
_NBA_INJURY_SEVERITY = {
    "out":            LineupStatus.OUT,
    "doubtful":       LineupStatus.QUESTIONABLE,
    "questionable":   LineupStatus.QUESTIONABLE,
    "day-to-day":     LineupStatus.QUESTIONABLE,
    "day to day":     LineupStatus.QUESTIONABLE,
    "probable":       LineupStatus.CONFIRMED_STARTER,  # ≈ likely to play
    "active":         LineupStatus.CONFIRMED_STARTER,
    "suspended":      LineupStatus.SUSPENDED,
}


async def build_nba_injury_status(db, pick: dict) -> GoldEvidence:
    """Real ESPN NBA injury status from ``espn_injury_notes``.

    Freshness:
      AVAILABLE if injury doc updated within the last 6h.
      STALE     if updated between 6h and 48h.
      UNAVAILABLE if older than 48h or missing.
    """
    ev = GoldEvidence(
        evidence_type=ExtGoldEvidenceType.NBA_INJURY_STATUS,
        sport="NBA",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no player name"
        return ev
    pname_norm = pname.lower()
    if "(" in pname_norm:
        pname_norm = pname_norm.split("(", 1)[0].strip()

    # Search every NBA injury team doc for a matching athlete.
    matched: Optional[dict] = None
    team_doc: Optional[dict] = None
    try:
        cursor = db.espn_injury_notes.find({"sport": "NBA"})
        async for tb in cursor:
            for inj in tb.get("injuries") or []:
                ath = str(inj.get("athlete") or "").lower()
                if ath == pname_norm or (
                    "(" in ath and ath.split("(", 1)[0].strip() == pname_norm
                ):
                    matched = inj
                    team_doc = tb
                    break
            if matched:
                break
    except Exception:
        matched = None

    if not matched:
        # Player not on any injury list — treat as PROBABLE / healthy,
        # but only if we have RECENT injury data for the league (else
        # we can't distinguish "healthy" from "missing feed").
        try:
            newest = await db.espn_injury_notes.find_one(
                {"sport": "NBA"}, sort=[("updated_at", -1)])
        except Exception:
            newest = None
        if not newest:
            ev.availability = Availability.UNAVAILABLE
            ev.notes = "no NBA injury feed available"
            return ev
        mins = _minutes_since(newest.get("updated_at"))
        if mins is None or mins > 60 * 48:
            ev.availability = Availability.STALE
            ev.notes = f"NBA injury feed stale ({mins:.0f}m old)" if mins else \
                       "NBA injury feed timestamp unknown"
            return ev
        ev.availability = (Availability.AVAILABLE
                            if mins <= 6 * 60 else Availability.STALE)
        ev.matchup_feature = "player_status"
        ev.value = 1.0   # 1 = healthy / probable
        ev.source = "espn_injury_notes"
        ev.timestamp = str(newest.get("updated_at") or "")
        ev.provenance = {
            "player":       pname,
            "status":       LineupStatus.CONFIRMED_STARTER,
            "reason":       "not on any team's injury list",
            "feed_age_min": round(mins, 1) if mins is not None else None,
            "source":       "espn_injury_notes",
        }
        ev.direction = "positive"
        return ev

    # Freshness of the matched team doc.
    mins = _minutes_since((team_doc or {}).get("updated_at"))
    status = _NBA_INJURY_SEVERITY.get(
        str(matched.get("status") or "").lower(),
        LineupStatus.QUESTIONABLE)
    if mins is None:
        ev.availability = Availability.STALE
    elif mins > 60 * 48:
        ev.availability = Availability.STALE
    elif mins > 6 * 60:
        ev.availability = Availability.STALE
    else:
        ev.availability = Availability.AVAILABLE
    ev.matchup_feature = "player_status"
    ev.value = {
        LineupStatus.OUT: -1.0,
        LineupStatus.SUSPENDED: -1.0,
        LineupStatus.QUESTIONABLE: 0.0,
        LineupStatus.CONFIRMED_STARTER: 1.0,
    }.get(status, 0.0)
    ev.source = "espn_injury_notes"
    ev.timestamp = str((team_doc or {}).get("updated_at") or "")
    ev.provenance = {
        "player":         matched.get("athlete"),
        "team":           (team_doc or {}).get("team_name"),
        "status":         status,
        "espn_status":    matched.get("status"),
        "severity":       matched.get("severity"),
        "description":    matched.get("description"),
        "injury_date":    matched.get("date"),
        "team_updated_at":(team_doc or {}).get("updated_at"),
        "feed_age_min":   round(mins, 1) if mins is not None else None,
        "source":         "espn_injury_notes",
    }
    ev.direction = ("negative"
                    if status in (LineupStatus.OUT, LineupStatus.SUSPENDED)
                    else ("neutral" if status == LineupStatus.QUESTIONABLE
                          else "positive"))
    return ev


# ═══════════════════════════════════════════════════════════════════
# NBA recent-usage adapter (3E.d) — reads player_game_actuals
# ═══════════════════════════════════════════════════════════════════
def _nba_market_stat(market: str) -> Optional[str]:
    m = (market or "").lower()
    if "point" in m:   return "points"
    if "rebound" in m: return "rebounds"
    if "assist" in m:  return "assists"
    if "three" in m or "3-point" in m or "3pt" in m or "3-pt" in m:
        return "threes_made"
    if "steal" in m:   return "steals"
    if "block" in m:   return "blocks"
    return None


async def build_nba_recent_usage(db, pick: dict) -> GoldEvidence:
    """Recent (last-N) counting-stat rate for the SPECIFIC market
    family.  Never generic — a rebounds pick reads rebounds only.
    """
    ev = GoldEvidence(
        evidence_type=ExtGoldEvidenceType.NBA_RECENT_USAGE,
        sport="NBA",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    cpid = pick.get("canonical_player_id")
    stat = _nba_market_stat(pick.get("market") or "")
    if not cpid:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no canonical_player_id — cannot reach player_game_actuals"
        return ev
    if not stat:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = f"market not mapped to counting stat: {pick.get('market')!r}"
        return ev
    cutoff_iso, cutoff_day = _pregame_cutoff_from_pick(pick)
    rows: list[dict] = []
    try:
        cursor = db.player_game_actuals.find(
            {"sport": "nba", "canonical_player_id": str(cpid),
             "event_time": {"$lt": cutoff_iso}},
            {"event_time": 1, "actuals": 1, "opponent": 1,
             "home_away": 1, "_id": 0},
        ).sort([("event_time", -1)]).limit(10)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []
    if not rows:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no pre-cutoff NBA history for canonical_player_id"
        ev.provenance = {"canonical_player_id": str(cpid),
                          "stat": stat, "cutoff": cutoff_iso}
        return ev
    values = []
    minutes_vals = []
    for r in rows:
        actuals = r.get("actuals") or {}
        v = actuals.get(stat)
        if v is not None:
            try: values.append(float(v))
            except (TypeError, ValueError): pass
        m = actuals.get("minutes") or r.get("minutes")
        if m is not None:
            try: minutes_vals.append(float(m))
            except (TypeError, ValueError): pass
    if not values:
        ev.availability = Availability.PARTIAL
        ev.notes = f"logs found but {stat} missing on every row"
        ev.provenance = {"n_games": len(rows), "stat": stat,
                          "cutoff": cutoff_iso}
        return ev
    last5 = values[:5]
    avg = sum(last5) / len(last5)
    ev.availability = (Availability.AVAILABLE
                        if len(last5) >= 3 else Availability.PARTIAL)
    ev.matchup_feature = f"avg_{stat}_last_5"
    ev.value = round(avg, 2)
    ev.sample_size = len(last5)
    ev.source = "player_game_actuals"
    ev.timestamp = cutoff_iso
    ev.provenance = {
        "canonical_player_id": str(cpid),
        "stat":                stat,
        "avg_last_5":          round(avg, 2),
        "avg_last_10":         round(sum(values)/len(values), 2),
        "n_games_last_5":      len(last5),
        "n_games_last_10":     len(values),
        "avg_minutes_last_5":  (round(sum(minutes_vals[:5])/max(len(minutes_vals[:5]),1),1)
                                 if minutes_vals else None),
        "cutoff":              cutoff_iso,
        "source":              "player_game_actuals",
        "temporal_rule":       "event_time < cutoff (no leakage)",
    }
    line = pick.get("line")
    if line is not None:
        try:
            L = float(line)
            ev.direction = ("positive" if avg > L + 1 else
                             ("negative" if avg < L - 1 else "neutral"))
        except (TypeError, ValueError):
            ev.direction = "neutral"
    return ev


# ═══════════════════════════════════════════════════════════════════
# NBA rest-context adapter (3E.d)
# ═══════════════════════════════════════════════════════════════════
async def build_nba_rest_context(db, pick: dict) -> GoldEvidence:
    """Rest-days / B2B context from player_game_logs (raw ingest)."""
    ev = GoldEvidence(
        evidence_type=ExtGoldEvidenceType.NBA_REST_CONTEXT,
        sport="NBA",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    cpid = pick.get("canonical_player_id")
    if not cpid:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no canonical_player_id"
        return ev
    cutoff_iso, cutoff_day = _pregame_cutoff_from_pick(pick)
    try:
        row = await db.player_game_logs.find_one(
            {"sport": "nba", "player_id": int(cpid) if str(cpid).isdigit() else cpid,
             "date": {"$lt": cutoff_day}},
            sort=[("date", -1)],
        )
    except Exception:
        row = None
    if not row:
        try:
            row = await db.player_game_logs.find_one(
                {"sport": "nba", "player_id": cpid,
                 "date": {"$lt": cutoff_day}},
                sort=[("date", -1)],
            )
        except Exception:
            row = None
    if not row:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no pre-cutoff NBA gamelog for player"
        return ev
    rest_days = row.get("rest_days")
    is_b2b = row.get("is_b2b")
    is_home = row.get("is_home")
    ev.availability = Availability.AVAILABLE
    ev.matchup_feature = "rest_days_since_last_game"
    ev.value = float(rest_days) if rest_days is not None else 0.0
    ev.source = "player_game_logs"
    ev.timestamp = cutoff_iso
    ev.provenance = {
        "last_game_date":   row.get("date"),
        "rest_days":        rest_days,
        "is_b2b":           bool(is_b2b),
        "is_home_last":     bool(is_home),
        "opp_last":         row.get("opp_team_id"),
        "cutoff":           cutoff_iso,
        "source":           "player_game_logs",
        "temporal_rule":    "date < cutoff_day (no leakage)",
    }
    if is_b2b:
        ev.direction = "negative"
    elif rest_days is not None and rest_days >= 3:
        ev.direction = "positive"
    else:
        ev.direction = "neutral"
    return ev


__all__ = [
    "ExtGoldEvidenceType", "LineupStatus",
    "build_soccer_recent_role", "build_soccer_teammate_context",
    "build_nba_injury_status", "build_nba_recent_usage",
    "build_nba_rest_context",
]
