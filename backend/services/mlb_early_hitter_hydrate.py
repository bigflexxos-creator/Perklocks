"""μ-closure FIX 1 (2026-06) — MLB Early Hitter Context Decoupling.

Sync variant — used from ``_props_picks_from_event`` in sports_engine.py
which is a synchronous function that runs in the middle of an async
prop-generation pipeline.  Uses pymongo (not motor) so it can execute
without an ``await``.

Purpose
-------
When a REAL sportsbook hitter prop exists for
:code:`batter_hits / batter_total_bases / batter_hits_runs_rbis`
BEFORE the player appears in the confirmed / projected lineup context,
the previous flow starved the model:

    real sportsbook line
      → player absent from ctx["hitters"]
      → sparse/empty hitter context
      → <3 real factors
      → MISSING_FEATURE_DATA
      → candidate dies before MLB model

This helper closes that gap surgically by hydrating a MINIMAL hitter
row from EXISTING data sources (Statcast leaderboard, hitter_intel
cache) so the feature engine can extract >=3 factors when a REAL line
exists.

Contract
--------
* **No fabrication** — only real cached data is attached.  If nothing
  is available, ctx is untouched and the existing MISSING_FEATURE_DATA
  path still fires for the correct data reason.
* **Lineup status preserved** — ``lineup_confirmed=False`` /
  ``lineup_source="hydrated_from_player_db"``.  Existing UNKNOWN → Lock
  cap 88 continues to apply.
* **Later refresh authoritative** — projected/confirmed lineup lands
  after hydration and the existing merge overwrites the hydrated row.
* **Scratched/benched removal untouched** — hydrated rows carry
  ``lineup_confirmed=False`` so the existing invalidation predicate
  is a no-op on them.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("lockscore.services.mlb_early_hitter_hydrate")

# Lazy-initialised sync pymongo client.  We reuse a single client for
# the process lifetime — creating it on demand keeps import-time cheap
# and avoids booting a second connection pool during module load.
_SYNC_DB = None


def _sync_db():
    # PERKLOCKS ROOT FIX (2026-09-03) — universal hitter hydration
    # unblocker.  The previous ``return _SYNC_DB or None`` used
    # pymongo Database truthiness which raises
    # ``NotImplementedError: Database objects do not implement truth
    # value testing`` on every call.  The exception was caught (and
    # silently ``logger.debug``-swallowed) by the caller in
    # ``sports_engine._props_picks_from_event``, so hydration was
    # UNIVERSALLY dead for every MLB hitter — every ``ctx["hitters"]``
    # stayed empty, every Statcast factor returned None, every
    # hitter candidate died at ``MISSING_FEATURE_DATA``.  Explicit
    # ``is None`` and ``is False`` checks bypass truthiness entirely.
    global _SYNC_DB
    if _SYNC_DB is None:
        try:
            from pymongo import MongoClient
            client = MongoClient(os.environ["MONGO_URL"])
            _SYNC_DB = client[os.environ.get("DB_NAME", "lockscore_db")]
        except Exception as e:
            logger.debug("sync pymongo init failed: %s", e)
            _SYNC_DB = False   # sentinel — don't retry
    if _SYNC_DB is False:
        return None
    return _SYNC_DB


def hydrate_missing_hitter(ctx: dict, player_name: str) -> bool:
    """Attach a minimal REAL hitter row to ``ctx["hitters"]`` when the
    player is missing.  Sync — safe to call from a synchronous function
    running inside an async caller's event loop.

    Returns ``True`` when at least one real signal was attached, else
    ``False``.  Never raises.
    """
    if not player_name or not isinstance(player_name, str):
        return False

    hitters = ctx.setdefault("hitters", {})
    key = player_name.strip().lower()
    if key in hitters and hitters[key]:
        return False   # already present

    db = _sync_db()
    if db is None:
        return False

    row: dict = {
        "lineup_confirmed":   False,
        "is_starter":         None,
        "lineup_slot":        None,
        "lineup_source":      "hydrated_from_player_db",
        "lineup_updated_at":  None,
        "is_home":            None,
        # Real Statcast fields (xBA / xwOBA / barrel% / hard-hit%)
        # power the factor_batter_statcast_* readers — up to 4 factors
        # can populate from a single row.
        "statcast":           None,
    }

    signals = 0

    # Statcast — Baseball Savant leaderboard row (season-scoped).
    try:
        year = datetime.now(timezone.utc).year
        sc = db.mlb_statcast_players.find_one(
            {"name": key, "year": year, "type": "batter"},
            {"_id": 0},
        )
        if sc:
            row["statcast"] = sc
            signals += 1
    except Exception as e:
        logger.debug("statcast lookup failed for %s: %s", player_name, e)

    # Recent form / splits — mlb_hitter_intel cache.
    try:
        hi = db.mlb_hitter_intel.find_one(
            {"name": key},
            {"_id": 0, "l10_hit_rate": 1,
             "home_ops": 1, "away_ops": 1,
             "vs_lhp_ops": 1, "vs_rhp_ops": 1,
             "opp_pitcher_hand": 1},
        )
        if hi:
            for k in ("l10_hit_rate", "home_ops", "away_ops",
                      "vs_lhp_ops", "vs_rhp_ops", "opp_pitcher_hand"):
                if hi.get(k) is not None:
                    row[k] = hi[k]
                    signals += 1
    except Exception as e:
        logger.debug("hitter_intel lookup failed for %s: %s", player_name, e)

    if signals == 0:
        # Nothing real to attach — MISSING_FEATURE_DATA rejection will
        # still fire below for the genuine data reason.
        return False

    hitters[key] = row
    logger.info(
        "Hydrated hitter %s from player DB (%d signals attached, "
        "lineup_status=unknown preserved)", player_name, signals,
    )
    return True


__all__ = ["hydrate_missing_hitter"]
