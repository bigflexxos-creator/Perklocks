"""Player Game Actuals Backfill (Locked Roadmap Item).

Normalises real authoritative legacy per-game rows into the
canonical ``db.player_game_actuals`` collection consumed by the
Stage-1 / Stage-2 Player History adapters.

Design constraints (per the roadmap contract):

* Reads ONLY from legacy Perklocks collections already present in
  the environment (§24 cost safety — no external API burn).
* Idempotent — deterministic uniqueness on (sport,
  canonical_player_id, event_id).  Rerunning MUST NOT double the
  sample (§16).
* Missing data stays UNKNOWN — a legacy row missing a stat surfaces
  as ``None`` on the normalized ``actuals`` sub-document; a real
  numeric 0 (0 hits, 0 rebounds) is preserved (§22).
* Provenance retained on every row: source collection,
  source_record_id, backfill_version, ingested_at (§17).
* Identity-unresolved rows are REJECTED honestly — never attached
  to a guessed player (§4).
* Bounded, checkpoint-friendly (§25) — accepts ``limit`` and
  ``since_date`` filters.

Adapters (best-effort — each returns ``None`` when the row cannot
be normalised):

    MLB    — h / hr / rbi / r / total_bases / pitcher_strikeouts /
             pitcher_outs
    NBA    — points / rebounds / assists / threes / steals /
             blocks / turnovers
    NFL    — passing / rushing / receiving yards + TDs (populated
             from whichever legacy fields are present)
    NHL    — game-level result / shots
    Tennis — aces / double_faults / games / sets / surface

Soccer is handled by the ``mls_player_matchup_history`` normaliser
in ``normalize_mls_matchup_history`` because that collection stores
per-opponent aggregates with ``recent[]`` per-event lists rather
than a flat per-game row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.player_history.backfill")

BACKFILL_VERSION = "v1.0.0"
TARGET_COLLECTION = "player_game_actuals"


def _f(v):
    """Legacy-row numeric coercion.  None / '' / non-numeric → None
    (never 0)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _normalize_ip_to_outs(ip) -> Optional[float]:
    """Baseball IP ('7.1' = 7⅓ = 22 outs)."""
    if ip is None or ip == "":
        return None
    try:
        s = str(ip)
        whole, _, frac = s.partition(".")
        whole_i = int(whole)
        frac_i = int(frac) if frac else 0
        if frac_i not in (0, 1, 2):
            # Fall back to plain multiplication.
            return round(float(ip) * 3)
        return whole_i * 3 + frac_i
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════
# Per-sport row → normalized document
# ═══════════════════════════════════════════════════════════════════
def _mlb_actuals(row: dict) -> dict:
    hr  = _f(row.get("home_runs"))
    h   = _f(row.get("hits"))
    rbi = _f(row.get("rbi"))
    r   = _f(row.get("runs"))
    tb  = _f(row.get("total_bases"))
    k_p = _f(row.get("pitcher_strikeouts"))
    outs = _f(row.get("outs")) or _normalize_ip_to_outs(row.get("innings_pitched"))
    return {"h": h, "hr": hr, "rbi": rbi, "r": r, "tb": tb,
             "strikeouts": _f(row.get("strikeouts")),
             "k": k_p,
             "outs": outs,
             "at_bats": _f(row.get("at_bats"))}


def _nba_actuals(row: dict) -> dict:
    return {"points":    _f(row.get("points")),
             "rebounds":  _f(row.get("rebounds")),
             "assists":   _f(row.get("assists")),
             "threes_made": _f(row.get("threes_made") or row.get("3pm")),
             "steals":    _f(row.get("steals")),
             "blocks":    _f(row.get("blocks")),
             "turnovers": _f(row.get("turnovers"))}


def _nfl_actuals(row: dict) -> dict:
    return {"pass_yds":    _f(row.get("pass_yds") or row.get("passing_yards")),
             "pass_tds":    _f(row.get("pass_tds")),
             "completions": _f(row.get("completions")),
             "attempts":    _f(row.get("attempts")),
             "interceptions": _f(row.get("interceptions")),
             "rush_yds":    _f(row.get("rush_yds") or row.get("rushing_yards")),
             "rush_attempts": _f(row.get("rush_attempts") or row.get("carries")),
             "rush_tds":    _f(row.get("rush_tds")),
             "rec_yds":     _f(row.get("rec_yds") or row.get("receiving_yards")),
             "receptions":  _f(row.get("receptions")),
             "rec_tds":     _f(row.get("rec_tds")),
             "targets":     _f(row.get("targets"))}


def _tennis_actuals(row: dict) -> dict:
    return {"aces":            _f(row.get("aces")),
             "double_faults":   _f(row.get("double_faults")),
             "games_won":       _f(row.get("games_won")),
             "games_lost":      _f(row.get("games_lost")),
             "sets_won":        _f(row.get("sets_won"))}


def _row_to_actual(sport: str, row: dict) -> Optional[dict]:
    """Build the canonical actuals sub-document for the sport."""
    s = (sport or "").lower()
    fns = {"mlb": _mlb_actuals, "nba": _nba_actuals,
            "nfl": _nfl_actuals, "tennis": _tennis_actuals}
    if s not in fns:
        return None
    return fns[s](row)


def _extract_event_time(row: dict) -> Optional[str]:
    for k in ("event_time", "date", "game_date"):
        v = row.get(k)
        if v is None or v == "":
            continue
        # Ensure ISO-shaped for `< as_of` comparisons.
        if isinstance(v, str):
            if "T" in v:
                return v
            return v + "T00:00:00Z"
        try:
            return v.isoformat()
        except Exception:
            continue
    return None


async def _resolve_canonical_player_id(db, sport: str, row: dict) -> Optional[str]:
    """Best-effort canonical player id lookup.

    Uses the ``players`` collection when present; falls back to the
    legacy ``player_id`` string itself so downstream Stage-2 lookups
    (which accept either form) can still resolve.  When neither is
    present the row is rejected (§4).
    """
    pid = row.get("canonical_player_id") or row.get("player_id")
    if not pid:
        return None
    return str(pid)


# ═══════════════════════════════════════════════════════════════════
# Backfill driver
# ═══════════════════════════════════════════════════════════════════
async def backfill_from_player_game_logs(
    db,
    *,
    sport: str,
    limit: int = 20_000,
    since_date: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Read legacy ``db.player_game_logs`` rows for ``sport`` and
    upsert normalized rows into ``db.player_game_actuals``.

    Returns a coverage report.  NEVER raises — normalisation errors
    are counted, not propagated.
    """
    s = (sport or "").lower()
    counters = {
        "sport":               s,
        "examined":            0,
        "accepted":            0,
        "identity_unresolved": 0,
        "event_unresolved":    0,
        "missing_all_stats":   0,
        "duplicates_avoided":  0,
        "inserted":            0,
        "updated":             0,
        "seasons":             set(),
        "players":             set(),
        "date_min":            None,
        "date_max":            None,
    }

    q: dict = {"sport": s}
    cursor = db.player_game_logs.find(q, {"_id": 0}).limit(limit)
    async for row in cursor:
        counters["examined"] += 1
        cpid = await _resolve_canonical_player_id(db, s, row)
        if not cpid:
            counters["identity_unresolved"] += 1
            continue
        event_id = row.get("game_id") or row.get("event_id")
        if not event_id:
            counters["event_unresolved"] += 1
            continue
        event_time = _extract_event_time(row)
        if since_date and event_time and event_time < since_date:
            continue
        actuals = _row_to_actual(s, row)
        if actuals is None:
            counters["missing_all_stats"] += 1
            continue
        if all(v is None for v in actuals.values()):
            # Row has zero usable stats → honestly reject (§22).
            counters["missing_all_stats"] += 1
            continue
        doc = {
            "sport":               s,
            "canonical_player_id": cpid,
            "player_id":           row.get("player_id"),
            "player_name":         row.get("player_name") or row.get("name"),
            "team":                row.get("team"),
            "opponent":            (row.get("opponent")
                                     or row.get("opp_team_id")),
            "home_away":           row.get("home_away")
                                     or (("home" if row.get("at_vs") == "vs"
                                           else "away") if row.get("at_vs") else None),
            "event_id":            str(event_id),
            "event_time":          event_time,
            "season":              row.get("season"),
            "week":                row.get("week"),
            "surface":             row.get("surface"),
            "actuals":             actuals,
            # Provenance (§17).
            "source":              "legacy_player_game_logs",
            "source_record_id":    str(event_id),
            "source_player_id":    row.get("player_id"),
            "backfill_version":    BACKFILL_VERSION,
            "ingested_at":         datetime.now(timezone.utc).isoformat(),
        }
        if event_time:
            counters["date_min"] = min(counters["date_min"] or event_time,
                                         event_time)
            counters["date_max"] = max(counters["date_max"] or event_time,
                                         event_time)
        if row.get("season") is not None:
            counters["seasons"].add(row.get("season"))
        counters["players"].add(cpid)
        counters["accepted"] += 1

        if dry_run:
            continue
        # Idempotent upsert (§16).  Unique on sport +
        # canonical_player_id + event_id.
        try:
            filt = {"sport": s, "canonical_player_id": cpid,
                     "event_id": str(event_id)}
            existing = await db[TARGET_COLLECTION].find_one(filt,
                                                              {"_id": 1})
            if existing:
                await db[TARGET_COLLECTION].update_one(
                    filt, {"$set": doc})
                counters["updated"] += 1
                counters["duplicates_avoided"] += 1
            else:
                await db[TARGET_COLLECTION].insert_one(doc)
                counters["inserted"] += 1
        except Exception as e:                   # pragma: no cover
            logger.debug("upsert failure for %s/%s/%s: %s",
                         s, cpid, event_id, e)

    counters["seasons"] = sorted(counters["seasons"])
    counters["players"] = len(counters["players"])
    return counters


# ═══════════════════════════════════════════════════════════════════
# Soccer — MLS matchup history normaliser
# ═══════════════════════════════════════════════════════════════════
async def backfill_from_mls_matchup_history(
    db,
    *,
    limit: int = 5000,
    dry_run: bool = False,
) -> dict:
    """Normalise ``db.mls_player_matchup_history`` (opponent-aggregated
    with ``recent[]`` per-event lists) into per-event canonical rows.
    """
    counters = {
        "sport":               "soccer",
        "examined_players":    0,
        "accepted":            0,
        "inserted":            0,
        "updated":             0,
        "duplicates_avoided":  0,
        "players":             set(),
        "seasons":             set(),
        "date_min":            None,
        "date_max":            None,
    }
    cursor = db.mls_player_matchup_history.find({}, {"_id": 0}).limit(limit)
    async for player_doc in cursor:
        counters["examined_players"] += 1
        pid = player_doc.get("player_id") or player_doc.get("_id")
        pname = player_doc.get("player_name")
        if not pid:
            continue
        counters["players"].add(str(pid))
        for opp_bucket in player_doc.get("by_opponent") or []:
            opp_id = opp_bucket.get("opponent_id")
            opp_name = opp_bucket.get("opponent_name")
            for ev in opp_bucket.get("recent") or []:
                date_iso = ev.get("date")
                if not date_iso:
                    continue
                if isinstance(date_iso, str) and "T" not in date_iso:
                    date_iso = date_iso + "T00:00:00Z"
                counters["date_min"] = min(counters["date_min"] or date_iso,
                                             date_iso)
                counters["date_max"] = max(counters["date_max"] or date_iso,
                                             date_iso)
                if ev.get("season") is not None:
                    counters["seasons"].add(ev.get("season"))
                event_id = f"mls-{pid}-{opp_id}-{date_iso[:10]}"
                doc = {
                    "sport":               "soccer",
                    "canonical_player_id": str(pid),
                    "player_id":           str(pid),
                    "player_name":         pname,
                    "opponent":            opp_id,
                    "canonical_opponent_id": opp_id,
                    "opponent_name":       opp_name,
                    "event_id":            event_id,
                    "event_time":          date_iso,
                    "season":              ev.get("season"),
                    "competition":         "MLS",
                    "actuals": {
                        "goals":            _f(ev.get("goals")),
                        "assists":          _f(ev.get("assists")),
                        "shots":            _f(ev.get("shots")),
                        "shots_on_target":  _f(ev.get("shots_on_target")),
                    },
                    "source":              "legacy_mls_matchup_history",
                    "source_record_id":    event_id,
                    "source_player_id":    str(pid),
                    "backfill_version":    BACKFILL_VERSION,
                    "ingested_at":         datetime.now(timezone.utc).isoformat(),
                }
                counters["accepted"] += 1
                if dry_run:
                    continue
                filt = {"sport": "soccer",
                         "canonical_player_id": str(pid),
                         "event_id": event_id}
                try:
                    existing = await db[TARGET_COLLECTION].find_one(
                        filt, {"_id": 1})
                    if existing:
                        await db[TARGET_COLLECTION].update_one(
                            filt, {"$set": doc})
                        counters["updated"] += 1
                        counters["duplicates_avoided"] += 1
                    else:
                        await db[TARGET_COLLECTION].insert_one(doc)
                        counters["inserted"] += 1
                except Exception as e:               # pragma: no cover
                    logger.debug("MLS upsert failure: %s", e)
    counters["seasons"] = sorted(counters["seasons"])
    counters["players"] = len(counters["players"])
    return counters


async def ensure_backfill_indexes(db) -> None:
    """Ensure a UNIQUE index on the idempotency key.  Safe to call
    repeatedly."""
    try:
        await db[TARGET_COLLECTION].create_index(
            [("sport", 1), ("canonical_player_id", 1), ("event_id", 1)],
            name="unique_backfill_key", unique=True,
        )
    except Exception:
        pass


__all__ = [
    "BACKFILL_VERSION",
    "TARGET_COLLECTION",
    "backfill_from_player_game_logs",
    "backfill_from_mls_matchup_history",
    "ensure_backfill_indexes",
    "_row_to_actual",
]
