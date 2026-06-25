"""Nightly Player Intelligence refresh job.

Responsibilities:
  1. On first call ever, upsert the seed catalog into `player_profiles_v2`
  2. Aggregate settled picks (last 90 d) by (sport, player) — uses the same
     player extraction as Auto-Elite so MLB hitters, soccer scorers, NBA/NFL
     player-props all flow through one pipeline
  3. Compute last-N trend, volatility 0-100, usage intensity
  4. Merge learned data into existing seeded profiles (seeds keep their
     archetype/team unless learning has a stronger signal)
  5. Rebuild the in-memory resolver index so future picks see the new aliases

Called from the existing nightly settlement loop. Cheap — typical run:
1-2 seconds on ~500 settled picks.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from .resolver import extract_player_from_market, rebuild_index_from_db_profiles
from .seeds import seed_rows
from .volatility import classify_usage, compute_volatility, summarise_trend

logger = logging.getLogger("lockscore.player_intel.refresh")

LOOKBACK_DAYS = 90
COLLECTION = "player_profiles_v2"


# Sport-specific market hooks — used to find every settled pick that
# references a player. Mirrors the regex from auto_elite.py but kept here so
# Player Intel can evolve independently.
PLAYER_MARKET_FILTERS = [
    {"market": {"$regex": "goal scorer", "$options": "i"}},
    {"market": {"$regex": "to score",   "$options": "i"}},
    {"market": {"$regex": "Hits",       "$options": "i"}},
    {"market": {"$regex": "Total Bases","$options": "i"}},
    {"market": {"$regex": "Strikeouts", "$options": "i"}},
    {"market": {"$regex": "Outs Recorded", "$options": "i"}},
    {"market": {"$regex": "Player Points", "$options": "i"}},
    {"market": {"$regex": "Rebounds",  "$options": "i"}},
    {"market": {"$regex": "Assists",   "$options": "i"}},
    {"market": {"$regex": "Receiving Yards","$options": "i"}},
    {"market": {"$regex": "Rushing Yards", "$options": "i"}},
    {"market": {"$regex": "Passing Yards", "$options": "i"}},
    {"market": {"$regex": "Receptions","$options": "i"}},
]


def _norm_archetype_for_sport(sport: str, position: str | None,
                              market: str | None) -> str | None:
    """Heuristic archetype fallback when we have no seed and limited stats.

    Used ONLY when learning kicks in for a player without a seed entry.
    """
    pos = (position or "").upper()
    m = (market or "").lower()
    if sport == "Soccer":
        if "goal scorer" in m or "to score" in m:
            return "finisher"
        if "assist" in m:
            return "playmaker"
        return None
    if sport == "NBA":
        if pos in {"PG"}:
            return "facilitator"
        if pos in {"SG", "SF"}:
            return "scorer"
        if pos in {"PF", "C"}:
            return "rim protector"
        return "scorer"
    if sport == "NFL":
        if pos == "QB":
            return "pocket QB"
        if pos == "RB":
            return "workhorse RB"
        if pos == "WR":
            return "possession receiver"
        if pos == "TE":
            return "red zone target"
        return None
    if sport == "Tennis":
        return "baseline grinder"
    if sport == "MLB":
        if "Strikeouts" in (market or "") or "Outs Recorded" in (market or ""):
            return "control pitcher"
        return "contact hitter"
    return None


async def _seed_into_db(db) -> int:
    """Upsert the static seed catalog. Safe to call every run \u2014 uses upsert
    so manual edits to existing rows are preserved (only missing fields
    refreshed).
    """
    inserted = 0
    for row in seed_rows():
        key = {
            "canonical_name": row["canonical_name"],
            "sport":          row["sport"],
        }
        # Use $setOnInsert for the immutable seed fields, $set for everything
        # we always want to keep in sync (aliases, position, team if changed).
        existing = await db[COLLECTION].find_one(key)
        update_payload: dict[str, Any] = {
            "$set": {
                "team":              row["team"],
                "position":          row["position"],
                "archetype":         row["archetype"],
                "archetype_source":  "seed",
                "aliases":           sorted(set(row["aliases"])),
                "usage_intensity":   row["usage_intensity"],
                "is_seed":           True,
                "updated_at":        _dt.datetime.now(_dt.timezone.utc).isoformat(),
            },
        }
        if not existing:
            update_payload["$setOnInsert"] = {
                "volatility":  None,
                "sample_size": 0,
            }
            inserted += 1
        await db[COLLECTION].update_one(key, update_payload, upsert=True)
    return inserted


async def refresh_player_profiles(db) -> dict:
    """Rebuild player_profiles_v2 from seeds + settled picks.

    Idempotent. Run after settlement.
    """
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    # 1) Seed catalog upsert
    n_seeded = await _seed_into_db(db)

    # 2) Aggregate settled player-prop picks
    by_player: dict[tuple[str, str], dict] = {}
    cur = db.picks.find(
        {
            "$or": PLAYER_MARKET_FILTERS,
            "status":      {"$in": ["won", "lost"]},
            "event_time":  {"$gte": cutoff},
        },
        {
            "_id": 0, "sport": 1, "market": 1, "status": 1,
            "event": 1, "event_time": 1, "league": 1,
        },
    ).sort("event_time", 1)

    async for p in cur:
        market = p.get("market") or ""
        name = extract_player_from_market(market)
        if not name:
            continue
        sport = p.get("sport") or "Soccer"
        k = (sport, name)
        r = by_player.setdefault(k, {
            "sport":   sport,
            "name":    name,
            "results": [],
            "leagues": set(),
        })
        r["results"].append({
            "ts":     p.get("event_time"),
            "event":  p.get("event") or "",
            "market": market,
            "won":    p.get("status") == "won",
            "league": p.get("league"),
        })
        if p.get("league"):
            r["leagues"].add(p.get("league"))

    # 3) For each player, compute learning-side metrics + upsert
    n_learned_updates = 0
    for (sport, name), r in by_player.items():
        results = r["results"]
        wins   = sum(1 for x in results if x["won"])
        n      = len(results)
        hit    = wins / n if n else 0.0
        last_n = results[-10:]
        trend  = summarise_trend(results)
        volat  = compute_volatility([bool(x["won"]) for x in results])
        usage  = classify_usage(len(last_n))

        # Determine archetype only if no seed match (don't override seed).
        existing = await db[COLLECTION].find_one(
            {"sport": sport, "canonical_name": name}
        )
        archetype_source = (existing or {}).get("archetype_source")
        archetype = (existing or {}).get("archetype")
        if (not existing) or archetype_source not in ("seed", "manual"):
            # Use a market-derived heuristic when there's no seed; future
            # iterations could call LLM here for ambiguous cases.
            guess_market = results[-1]["market"] if results else None
            archetype = _norm_archetype_for_sport(
                sport, (existing or {}).get("position"), guess_market
            ) or archetype
            archetype_source = "learned"

        payload = {
            "canonical_name": name,
            "sport":          sport,
            "aliases":        sorted(set([name] + ((existing or {}).get("aliases") or []))),
            "archetype":      archetype,
            "archetype_source": archetype_source,
            "n_picks":        n,
            "wins":           wins,
            "hit_rate":       round(hit, 3),
            "last5_hit":      trend["last5_hit"],
            "last10_hit":     trend["last10_hit"],
            "current_streak": trend["current_streak"],
            "volatility":     volat,
            "usage_intensity": usage,
            "sample_size":    n,
            "leagues":        sorted(r["leagues"]),
            "updated_at":     _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "source":         (existing or {}).get("source") or "learned",
        }
        await db[COLLECTION].update_one(
            {"sport": sport, "canonical_name": name},
            {"$set": payload},
            upsert=True,
        )
        n_learned_updates += 1

    # 4) Player metadata enrichment — augment NBA/NFL/MLB profiles with
    #    real position + team + injury status.
    #
    #    Phase 1 (2026-06-25): MLB now resolved via the local free-source
    #    player_db (MLB Stats API) — zero quota cost, faster lookups,
    #    deeper data (bats/throws, full season splits). NBA/NFL still
    #    route through the legacy SportsDataIO client until their
    #    ingestors land in Phase 2.
    try:
        from player_db.client import enrich_profile as local_enrich
        from .sportsdataio_client import SPORTSDATAIO_KEY
        recent_cutoff = (_dt.datetime.now(_dt.timezone.utc)
                         - _dt.timedelta(days=7)).isoformat()
        recent_names_by_sport: dict[str, set[str]] = {
            "NBA": set(), "NFL": set(), "MLB": set(),
        }
        from .resolver import extract_player_from_market as _epm
        async for p in db.picks.find(
            {"sport": {"$in": list(recent_names_by_sport.keys())},
             "event_time": {"$gte": recent_cutoff}},
            {"_id": 0, "sport": 1, "market": 1},
        ):
            n = _epm(p.get("market") or "")
            if n:
                recent_names_by_sport[p["sport"]].add(n)
        enriched_n = 0
        # MLB → local DB (always on, no key gate). NBA/NFL → legacy
        # SportsDataIO if its key is present.
        for sport_u, name_set in recent_names_by_sport.items():
            if sport_u != "MLB" and not SPORTSDATAIO_KEY:
                continue
            for nm in name_set:
                prof = await db[COLLECTION].find_one(
                    {"sport": sport_u, "canonical_name": nm}
                )
                if not prof:
                    prof = {
                        "canonical_name": nm, "sport": sport_u,
                        "aliases": [nm], "archetype": None,
                        "archetype_source": "auto", "source": "auto",
                    }
                prof.pop("_id", None)
                # Phase-1 router: MLB uses local DB; everyone else goes
                # to the legacy enrich path (which itself routes through
                # SportsDataIO).
                await local_enrich(prof)
                prof["updated_at"] = _dt.datetime.now(
                    _dt.timezone.utc
                ).isoformat()
                await db[COLLECTION].update_one(
                    {"sport": sport_u, "canonical_name": nm},
                    {"$set": prof},
                    upsert=True,
                )
                enriched_n += 1
        logger.info(
            "Player Intelligence enrichment: %d profiles updated "
            "(MLB→local player_db, NBA/NFL→SportsDataIO%s)",
            enriched_n,
            "" if SPORTSDATAIO_KEY else " [SKIPPED — no key]",
        )
    except Exception as e:
        logger.warning("Player metadata enrichment skipped: %s", e)

    # 5) Rebuild the in-memory resolver index from the full DB snapshot so
    #    learned aliases become immediately visible to enrichment.
    snapshot: list[dict] = []
    async for row in db[COLLECTION].find({}, {"_id": 0}):
        snapshot.append(row)
    rebuild_index_from_db_profiles(snapshot)

    logger.info(
        "Player Intelligence refresh: %d seeded, %d learned updates, %d total in DB",
        n_seeded, n_learned_updates, len(snapshot),
    )
    return {
        "seeded_new":      n_seeded,
        "learned_updates": n_learned_updates,
        "total_profiles":  len(snapshot),
    }
