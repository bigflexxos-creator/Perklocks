"""CFB precompute — async wrapper that runs BEFORE the sync pick
emission loop, mirroring the NFL `_ctx["nfl_precomputed"]` pattern.

USER MANDATE 2026-07-27 (Phase 3): "Build cfb_feature_engine — mirror
of NFL." The engine ships in services/cfb_feature_engine.py. THIS
module is the async pre-compute glue that puts real-data factors on
the ctx so the sync emission path can look them up per-player.

Call `await precompute_cfb_factors(db, ctx, players_by_market)` from
the async CFB fetch path (fetch_cfb_picks in sports_engine.py) BEFORE
handing the ctx to `_picks_from_game`. Then the emission branch reads:

    _pc = (ctx.get("cfb_precomputed") or {}).get(player.lower(), {}).get(mk) or {}
    factors = _pc.get("factors") or {"Book Implied Probability": mp}

Zero cost when CFB market list is empty (July / August pre-season).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("lockscore.services.cfb_precompute")

# Reuse NFL's market → stat mapping (CFB props are shaped identically)
CFB_MARKET_TO_STAT = {
    "player_pass_yds":               "passing_yards",
    "player_pass_yds_alternate":     "passing_yards",
    "player_pass_tds":               "passing_tds",
    "player_pass_attempts":          "attempts",
    "player_pass_completions":       "completions",
    "player_rush_yds":               "rushing_yards",
    "player_rush_yds_alternate":     "rushing_yards",
    "player_rush_attempts":          "carries",
    "player_rush_tds":               "rushing_tds",
    "player_receptions":             "receptions",
    "player_receptions_alternate":   "receptions",
    "player_reception_yds":          "receiving_yards",
    "player_reception_yds_alternate": "receiving_yards",
    "player_reception_tds":          "receiving_tds",
}


async def _resolve_cfb_season(db, ctx_season: Optional[int]) -> int:
    """Pick the correct CFB season to look up. During July/early August
    the upcoming season's data (SP+, RP) hasn't been ingested yet — the
    DB still has last year's data. Auto-fall back to the most recent
    season that has SP+ ratings ingested. Once the new season's data
    lands (mid-August typically), this returns the current year again.
    """
    if isinstance(ctx_season, int) and ctx_season > 2000:
        # Verify data exists for this year
        exists = await db.cfb_sp_ratings.find_one({"year": ctx_season})
        if exists:
            return ctx_season
    # Fall back to the newest year present in SP+ ratings
    latest = await db.cfb_sp_ratings.find_one(sort=[("year", -1)])
    if latest and isinstance(latest.get("year"), int):
        return int(latest["year"])
    # Final fallback: current calendar year (offseason May-August use prev)
    now = datetime.utcnow()
    return now.year if now.month >= 8 else (now.year - 1)


async def precompute_cfb_factors(
    db,
    ctx: dict,
    props: list[dict],
) -> dict:
    """Pre-compute CFB feature-engine factors for every (player, market)
    tuple in the given prop list.

    props: [{"player": str, "market": str, "line": float, "side": str,
             "book_implied": float, "player_team": str, "opponent": str,
             "position": str}]

    Mutates & returns ctx with:
        ctx["cfb_precomputed"] = {
            player_name_lower: {
                market_key: {
                    "factors": {...},
                    "sources": [...],
                    "rationale_bits": [...],
                    "has_enough_real_data": bool,
                }
            }
        }
    """
    if not props:
        return ctx
    from services.cfb_feature_engine import (
        build_cfb_prop_factors, has_enough_real_data_cfb,
    )
    season = await _resolve_cfb_season(db, ctx.get("cfb_season"))
    ctx["cfb_season_resolved"] = season
    out: dict = {}
    for p in props:
        player = p.get("player") or ""
        market = p.get("market") or ""
        stat = CFB_MARKET_TO_STAT.get(market)
        if not player or not stat:
            continue
        try:
            factors, sources = await build_cfb_prop_factors(
                db,
                player=player,
                player_team=p.get("player_team") or "",
                opponent=p.get("opponent") or "",
                position=p.get("position") or "QB",
                prop_stat=stat,
                line=float(p.get("line") or 0.0),
                side=str(p.get("side") or "over").lower(),
                season=int(season),
                is_home=bool(p.get("is_home", True)),
                book_implied=p.get("book_implied"),
            )
            rationale_bits = factors.pop("_rationale_bits", []) if isinstance(factors, dict) else []
            out.setdefault(player.strip().lower(), {})[market] = {
                "factors":              factors,
                "sources":              sources,
                "rationale_bits":       rationale_bits,
                "has_enough_real_data": has_enough_real_data_cfb(factors),
            }
        except Exception as e:
            logger.debug("CFB precompute failed for %s / %s: %s", player, market, e)
    ctx["cfb_precomputed"] = out
    logger.info("CFB precompute: %d players × market cache slots ready", sum(len(v) for v in out.values()))
    return ctx


__all__ = ["precompute_cfb_factors", "CFB_MARKET_TO_STAT"]
