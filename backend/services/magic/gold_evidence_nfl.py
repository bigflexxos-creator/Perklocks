"""MAGIC 3G — NFL Gold evidence adapters.

Read-only DB-first Gold evidence over the already-persisted NFL data
(`nfl_player_weekly` = 129,657 rows and `player_game_actuals` = 129,657
NFL rows).  No fabrication.  No polling.  No simulator built here (3H).

Evidence families exposed:

  * ``build_nfl_recent_form``     — last-N averages for the market's
                                     specific stat family (yards ≠ TDs
                                     ≠ receptions).
  * ``build_nfl_usage``           — role/opportunity from
                                     `nfl_player_usage`.
  * ``build_nfl_injury_status``   — ESPN NFL injury feed with strict
                                     starter vocabulary.
  * ``build_nfl_opponent_history` — opponent allowances derived from
                                     the same weekly log per position.
  * ``build_nfl_threshold_history``— exact-threshold hit-rate over
                                     last-N pre-cutoff games.

Simulator remains UNAVAILABLE for NFL — 3H will introduce it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from services.magic.gold_evidence import (
    Availability, GoldEvidence, _pregame_cutoff_from_pick,
)
from services.magic.gold_evidence_ext import LineupStatus


class NflGoldEvidenceType:
    NFL_RECENT_FORM       = "NFL_RECENT_FORM"
    NFL_USAGE             = "NFL_USAGE"
    NFL_INJURY_STATUS     = "NFL_INJURY_STATUS"
    NFL_OPPONENT_HISTORY  = "NFL_OPPONENT_HISTORY"
    NFL_THRESHOLD_HISTORY = "NFL_THRESHOLD_HISTORY"


class NflStarterStatus:
    CONFIRMED_STARTER = "CONFIRMED_STARTER"
    EXPECTED_STARTER  = "EXPECTED_STARTER"
    BACKUP            = "BACKUP"
    QUESTIONABLE      = "QUESTIONABLE"
    DOUBTFUL          = "DOUBTFUL"
    OUT               = "OUT"
    IR                = "IR"
    SUSPENDED         = "SUSPENDED"
    UNKNOWN           = "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Market → stat mapping (exact per user directive)
# ═══════════════════════════════════════════════════════════════════
def _nfl_market_stat(market: str) -> Optional[str]:
    m = (market or "").lower()
    # QB
    if "pass" in m and "yard"    in m: return "passing_yards"
    if "pass" in m and "td"       in m: return "passing_tds"
    if "pass" in m and "attempt"  in m: return "attempts"
    if "pass" in m and "completion" in m: return "completions"
    if "interception" in m or (" int" in m and "pass" in m):
        return "passing_ints"
    # Rushing
    if "rush" in m and "yard" in m:   return "rushing_yards"
    if "carry" in m or "carries" in m: return "carries"
    if "rush" in m and "td"   in m:   return "rushing_tds"
    # Receiving
    if "reception" in m or "recept" in m: return "receptions"
    if "receiv" in m and "yard" in m:  return "receiving_yards"
    if "receiv" in m and "td"   in m:  return "receiving_tds"
    if "target"    in m:               return "targets"
    # ATD — Anytime TD  → union of rushing_tds + receiving_tds
    if ("anytime" in m and ("td" in m or "touchdown" in m)) \
       or "atd" in m or "any td" in m \
       or "anytime touchdown" in m or "anytime scorer" in m:
        return "atd"
    return None


def _atd_from_row(actuals: dict) -> int:
    return int(bool(actuals.get("rushing_tds", 0)
                    or actuals.get("receiving_tds", 0)))


def _stat_from_row(row: dict, stat: str) -> Optional[float]:
    """Pull the stat off a `player_game_actuals.actuals` OR a
    `nfl_player_weekly` row.  Handles both naming conventions
    (`passing_yards` and `pass_yds`).  ATD is derived."""
    actuals = row.get("actuals") or row
    if stat == "atd":
        rt = (actuals.get("rushing_tds")
              if actuals.get("rushing_tds") is not None
              else actuals.get("rush_tds", 0))
        wt = (actuals.get("receiving_tds")
              if actuals.get("receiving_tds") is not None
              else actuals.get("rec_tds", 0))
        return float(bool((rt or 0) or (wt or 0)))
    aliases = {
        "passing_yards":   ("passing_yards",  "pass_yds"),
        "passing_tds":     ("passing_tds",    "pass_tds"),
        "passing_ints":    ("passing_ints",   "interceptions"),
        "rushing_yards":   ("rushing_yards",  "rush_yds"),
        "rushing_tds":     ("rushing_tds",    "rush_tds"),
        "carries":         ("carries",        "rush_attempts"),
        "receiving_yards": ("receiving_yards","rec_yds"),
        "receiving_tds":   ("receiving_tds",  "rec_tds"),
    }
    for key in aliases.get(stat, (stat,)):
        v = actuals.get(key)
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Recent-form adapter
# ═══════════════════════════════════════════════════════════════════
async def build_nfl_recent_form(db, pick: dict) -> GoldEvidence:
    """Average of the pick's SPECIFIC stat over last-N pre-cutoff
    weekly rows."""
    ev = GoldEvidence(
        evidence_type=NflGoldEvidenceType.NFL_RECENT_FORM,
        sport="NFL",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    stat = _nfl_market_stat(pick.get("market") or "")
    cpid = pick.get("canonical_player_id")
    if not stat:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = f"market not mapped to NFL stat: {pick.get('market')!r}"
        return ev
    if not cpid:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no canonical_player_id — cannot join player_game_actuals"
        return ev
    cutoff_iso, cutoff_day = _pregame_cutoff_from_pick(pick)
    rows: list[dict] = []
    try:
        cursor = db.player_game_actuals.find(
            {"sport": "nfl", "canonical_player_id": str(cpid),
             "event_time": {"$lt": cutoff_iso}},
            {"event_time": 1, "actuals": 1, "opponent": 1,
             "season": 1, "week": 1, "_id": 0},
        ).sort([("event_time", -1)]).limit(20)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []
    if not rows:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no pre-cutoff NFL history for player"
        return ev
    values = []
    for r in rows:
        v = _stat_from_row(r, stat)
        if v is not None:
            values.append(v)
    if not values:
        ev.availability = Availability.PARTIAL
        ev.notes = f"logs found but stat={stat} missing on every row"
        return ev
    last5 = values[:5]
    last10 = values[:10]
    avg5 = sum(last5) / len(last5)
    ev.availability = (Availability.AVAILABLE
                        if len(last5) >= 3 else Availability.PARTIAL)
    ev.matchup_feature = f"avg_{stat}_last_5"
    ev.value = round(avg5, 3)
    ev.sample_size = len(last5)
    ev.source = "player_game_actuals"
    ev.timestamp = cutoff_iso
    ev.provenance = {
        "canonical_player_id": str(cpid),
        "stat":                stat,
        "avg_last_5":          round(avg5, 3),
        "avg_last_10":         round(sum(last10)/len(last10), 3),
        "n_games_last_5":      len(last5),
        "n_games_last_10":     len(last10),
        "cutoff":              cutoff_iso,
        "source":              "player_game_actuals",
        "temporal_rule":       "event_time < cutoff (no leakage)",
    }
    line = pick.get("line")
    if line is not None:
        try:
            L = float(line)
            margin = avg5 - L
            threshold = max(0.03 * abs(L), 0.5)
            ev.direction = ("positive" if margin > threshold else
                             ("negative" if margin < -threshold
                              else "neutral"))
        except (TypeError, ValueError):
            ev.direction = "neutral"
    return ev


# ═══════════════════════════════════════════════════════════════════
# Usage adapter
# ═══════════════════════════════════════════════════════════════════
async def build_nfl_usage(db, pick: dict) -> GoldEvidence:
    ev = GoldEvidence(
        evidence_type=NflGoldEvidenceType.NFL_USAGE,
        sport="NFL",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    cpid = pick.get("canonical_player_id")
    if not cpid:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no canonical_player_id"
        return ev
    row = None
    try:
        row = await db.nfl_player_usage.find_one(
            {"player_id": str(cpid)}, sort=[("season", -1)])
        if not row:
            row = await db.nfl_player_usage.find_one(
                {"player_id": cpid}, sort=[("season", -1)])
    except Exception:
        row = None
    if not row:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no nfl_player_usage row for player_id"
        return ev
    ev.availability = Availability.AVAILABLE
    ev.matchup_feature = "snap_pct_avg"
    ev.value = float(row.get("snap_pct_avg") or 0.0)
    ev.source = "nfl_player_usage"
    ev.timestamp = str(row.get("updated_at") or "")
    ev.provenance = {
        "snap_pct_avg":        row.get("snap_pct_avg"),
        "offense_snaps_sum":   row.get("offense_snaps_sum"),
        "special_teams_pct_avg": row.get("special_teams_pct_avg"),
        "position":            row.get("position"),
        "team":                row.get("team"),
        "season":              row.get("season"),
        "games":               row.get("games"),
        "source":              "nfl_player_usage",
    }
    snap = ev.value
    ev.direction = ("positive" if snap >= 0.75
                    else ("neutral" if snap >= 0.4 else "negative"))
    return ev


# ═══════════════════════════════════════════════════════════════════
# Injury adapter (ESPN NFL) with strict starter vocabulary
# ═══════════════════════════════════════════════════════════════════
_ESPN_INJURY_TO_NFL = {
    "out":            NflStarterStatus.OUT,
    "injured reserve": NflStarterStatus.IR,
    "ir":             NflStarterStatus.IR,
    "doubtful":       NflStarterStatus.DOUBTFUL,
    "questionable":   NflStarterStatus.QUESTIONABLE,
    "day-to-day":     NflStarterStatus.QUESTIONABLE,
    "day to day":     NflStarterStatus.QUESTIONABLE,
    "probable":       NflStarterStatus.EXPECTED_STARTER,
    "active":         NflStarterStatus.CONFIRMED_STARTER,
    "suspended":      NflStarterStatus.SUSPENDED,
}


def _minutes_since(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return None


async def build_nfl_injury_status(db, pick: dict) -> GoldEvidence:
    ev = GoldEvidence(
        evidence_type=NflGoldEvidenceType.NFL_INJURY_STATUS,
        sport="NFL",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.availability = Availability.UNAVAILABLE
        return ev
    pname_norm = pname.lower()
    if "(" in pname_norm:
        pname_norm = pname_norm.split("(", 1)[0].strip()
    matched = None; team_doc = None
    try:
        cursor = db.espn_injury_notes.find({"sport": "NFL"})
        async for tb in cursor:
            for inj in tb.get("injuries") or []:
                ath = str(inj.get("athlete") or "").lower()
                if ath == pname_norm or (
                    "(" in ath and ath.split("(", 1)[0].strip() == pname_norm
                ):
                    matched = inj; team_doc = tb
                    break
            if matched: break
    except Exception:
        pass
    if not matched:
        # Not on any injury list — treat as EXPECTED_STARTER but only
        # if the feed itself is recent.
        try:
            newest = await db.espn_injury_notes.find_one(
                {"sport": "NFL"}, sort=[("updated_at", -1)])
        except Exception:
            newest = None
        if not newest:
            ev.availability = Availability.UNAVAILABLE
            ev.notes = "no NFL injury feed"
            return ev
        mins = _minutes_since(newest.get("updated_at"))
        if mins is None or mins > 48 * 60:
            ev.availability = Availability.STALE
            ev.notes = f"NFL injury feed stale ({mins:.0f}m)" if mins else \
                        "feed timestamp unknown"
            return ev
        ev.availability = (Availability.AVAILABLE
                            if mins <= 6 * 60 else Availability.STALE)
        ev.value = 1.0
        ev.provenance = {
            "player":       pname,
            "status":       NflStarterStatus.EXPECTED_STARTER,
            "reason":       "not on any team's injury list",
            "feed_age_min": round(mins, 1) if mins is not None else None,
            "source":       "espn_injury_notes",
        }
        ev.direction = "positive"
        return ev
    mins = _minutes_since((team_doc or {}).get("updated_at"))
    status = _ESPN_INJURY_TO_NFL.get(
        str(matched.get("status") or "").lower(),
        NflStarterStatus.QUESTIONABLE)
    if mins is None or mins > 48 * 60:
        ev.availability = Availability.STALE
    elif mins > 6 * 60:
        ev.availability = Availability.STALE
    else:
        ev.availability = Availability.AVAILABLE
    ev.value = {
        NflStarterStatus.OUT: -1.0,
        NflStarterStatus.IR: -1.0,
        NflStarterStatus.SUSPENDED: -1.0,
        NflStarterStatus.DOUBTFUL: -0.5,
        NflStarterStatus.QUESTIONABLE: 0.0,
        NflStarterStatus.EXPECTED_STARTER: 0.5,
        NflStarterStatus.CONFIRMED_STARTER: 1.0,
    }.get(status, 0.0)
    ev.source = "espn_injury_notes"
    ev.timestamp = str((team_doc or {}).get("updated_at") or "")
    ev.provenance = {
        "player":       matched.get("athlete"),
        "team":         (team_doc or {}).get("team_name"),
        "status":       status,
        "espn_status":  matched.get("status"),
        "description":  matched.get("description"),
        "feed_age_min": round(mins, 1) if mins is not None else None,
        "source":       "espn_injury_notes",
    }
    ev.direction = ("negative" if status in (
        NflStarterStatus.OUT, NflStarterStatus.IR,
        NflStarterStatus.SUSPENDED, NflStarterStatus.DOUBTFUL)
        else ("positive" if status in (
            NflStarterStatus.CONFIRMED_STARTER,
            NflStarterStatus.EXPECTED_STARTER) else "neutral"))
    return ev


# ═══════════════════════════════════════════════════════════════════
# Threshold-history adapter (exact line-safe)
# ═══════════════════════════════════════════════════════════════════
async def build_nfl_threshold_history(db, pick: dict) -> GoldEvidence:
    """Hit-rate over Over/Under exact line — last-N pre-cutoff games."""
    ev = GoldEvidence(
        evidence_type=NflGoldEvidenceType.NFL_THRESHOLD_HISTORY,
        sport="NFL",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    stat = _nfl_market_stat(pick.get("market") or "")
    cpid = pick.get("canonical_player_id")
    line = pick.get("line")
    side = (pick.get("side") or "").lower()
    if not (stat and cpid and line is not None and side in ("over", "under")):
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "requires stat, canonical_player_id, exact line, side"
        return ev
    cutoff_iso, _ = _pregame_cutoff_from_pick(pick)
    rows = []
    try:
        cursor = db.player_game_actuals.find(
            {"sport": "nfl", "canonical_player_id": str(cpid),
             "event_time": {"$lt": cutoff_iso}},
            {"event_time": 1, "actuals": 1, "_id": 0},
        ).sort([("event_time", -1)]).limit(20)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []
    if not rows:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no pre-cutoff logs"
        return ev
    line_f = float(line)
    hits = 0; total = 0
    for r in rows:
        v = _stat_from_row(r, stat)
        if v is None: continue
        total += 1
        if side == "over" and v > line_f: hits += 1
        if side == "under" and v < line_f: hits += 1
    if total == 0:
        ev.availability = Availability.PARTIAL
        return ev
    hit_rate = hits / total
    ev.availability = (Availability.AVAILABLE
                        if total >= 5 else Availability.PARTIAL)
    ev.value = hit_rate
    ev.sample_size = total
    ev.matchup_feature = f"{side}_{stat}_at_{line_f}"
    ev.source = "player_game_actuals"
    ev.timestamp = cutoff_iso
    ev.provenance = {
        "canonical_player_id": str(cpid),
        "stat":                stat,
        "line":                line_f,
        "side":                side,
        "hits":                hits,
        "total":               total,
        "hit_rate":            round(hit_rate, 3),
        "sample_size":         total,
        "cutoff":              cutoff_iso,
        "source":              "player_game_actuals",
        "temporal_rule":       "event_time < cutoff (no leakage)",
    }
    ev.direction = ("positive" if hit_rate >= 0.60
                    else ("negative" if hit_rate <= 0.40
                          else "neutral"))
    return ev


# ═══════════════════════════════════════════════════════════════════
# Opponent-history adapter
# ═══════════════════════════════════════════════════════════════════
async def build_nfl_opponent_history(db, pick: dict) -> GoldEvidence:
    """Opponent allowance vs the pick's position family.  Reads
    `player_game_actuals` — averages the stat allowed by the pick's
    opponent to players at the pick's position over the current season.
    """
    ev = GoldEvidence(
        evidence_type=NflGoldEvidenceType.NFL_OPPONENT_HISTORY,
        sport="NFL",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    stat = _nfl_market_stat(pick.get("market") or "")
    opp = pick.get("opponent") or pick.get("opponent_team")
    position = pick.get("position")
    if not (stat and opp):
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "requires stat + opponent"
        return ev
    cutoff_iso, _ = _pregame_cutoff_from_pick(pick)
    q = {"sport": "nfl", "opponent": str(opp).upper(),
         "event_time": {"$lt": cutoff_iso}}
    if position:
        q["position"] = position
    rows = []
    try:
        cursor = db.player_game_actuals.find(q, {
            "event_time": 1, "actuals": 1, "position": 1, "_id": 0,
        }).sort([("event_time", -1)]).limit(200)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []
    if not rows:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no opponent history rows"
        return ev
    values = [v for v in (_stat_from_row(r, stat) for r in rows)
              if v is not None]
    if not values:
        ev.availability = Availability.PARTIAL
        return ev
    avg = sum(values) / len(values)
    ev.availability = (Availability.AVAILABLE
                        if len(values) >= 10 else Availability.PARTIAL)
    ev.value = round(avg, 3)
    ev.matchup_feature = f"opponent_avg_{stat}_allowed"
    ev.sample_size = len(values)
    ev.source = "player_game_actuals"
    ev.timestamp = cutoff_iso
    ev.provenance = {
        "opponent":       opp, "position": position,
        "stat":           stat,
        "opp_avg":        round(avg, 3),
        "n_samples":      len(values),
        "source":         "player_game_actuals",
        "temporal_rule": "event_time < cutoff (no leakage)",
    }
    line = pick.get("line")
    if line is not None:
        try:
            L = float(line)
            ev.direction = ("positive" if avg > L else "negative")
        except (TypeError, ValueError):
            ev.direction = "neutral"
    return ev


__all__ = [
    "NflGoldEvidenceType", "NflStarterStatus",
    "build_nfl_recent_form", "build_nfl_usage",
    "build_nfl_injury_status", "build_nfl_threshold_history",
    "build_nfl_opponent_history",
]
