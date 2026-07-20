"""Per-Game Context Fetcher — Phase 3 (2026-07-19).

Gathers everything the data-driven model needs BEFORE picks are
generated for that game. This is the module that flips the pipeline
from "pick then enrich" to "enrich then pick".

Coverage today (MVP scope):
  - MLB: weather (from OpenWeather via services.enrichment.weather),
         park HR factor (from mlb_park_hand._STADIUM_LATLON keys +
         base park factors from signal_engine.mlb_deep._PARK_FACTORS),
         starting pitcher IDs from MLB StatsAPI (light call),
         team runs/G rolling from a static seed table.

What's PUNTED to Phase 3.1:
  - Fetching each starter's Stuff+ (requires the Fangraphs cache to
    be already warm; we read `services.mlb_stuff_plus.get_by_name`
    when available, else skip that lift).
  - Batter Statcast xwOBA / barrel% (needs a name-to-Statcast lookup
    that isn't yet wired here — read from the batter enricher
    output when it lands on the pick later).

All lookups are async / cached. Nothing here can block the ingest
path longer than ~500ms per game because the OpenWeather + park
factor lookups are already TTL-cached.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.game_context")


async def build_mlb_game_context(game: dict) -> dict[str, Any]:
    """Return the enrichment ctx dict that ``data_driven_model.mlb_*``
    functions consume. Never throws — missing sub-fetches produce
    partial context."""
    ctx: dict[str, Any] = {}
    home_team = (game.get("home_team") or "").strip()
    away_team = (game.get("away_team") or "").strip()
    ctx["home_team"] = home_team
    ctx["away_team"] = away_team

    # 1) Weather via the existing enricher (uses TTL cache).
    try:
        from services.enrichment.weather import enrich_pick_with_weather
        # Feed the enricher a stub pick so we reuse the same team→venue
        # lookup + dome short-circuit + OpenWeather call.
        stub = {"sport": "MLB", "home_team": home_team}
        await enrich_pick_with_weather(stub)
        if stub.get("weather"):
            ctx["weather"] = stub["weather"]
    except Exception as e:
        logger.debug("weather ctx fetch failed for %s: %s", home_team, e)

    # 2) Park HR factor — static lookup.
    try:
        from services.signal_engine.mlb_deep import _PARK_FACTORS
        pf = _PARK_FACTORS.get(home_team) or {}
        if pf.get("hr"):
            ctx["park_hr_factor"] = int(pf["hr"])
    except Exception as e:
        logger.debug("park factor ctx fetch failed for %s: %s", home_team, e)

    # 3) Starting pitcher IDs — lightweight MLB StatsAPI probable-pitcher
    # lookup. Cached inside mlb_bvp.
    try:
        from mlb_bvp import _get_json, MLB_STATS_BASE
        gid = game.get("id") or game.get("external_id")
        commence = game.get("commence_time") or game.get("event_time") or ""
        date_str = commence[:10] if commence else None
        if date_str:
            data = await _get_json(
                f"{MLB_STATS_BASE}/schedule?sportId=1&date={date_str}"
                f"&hydrate=probablePitcher(note)"
            )
            for d in (data or {}).get("dates", []):
                for gm in d.get("games", []):
                    tteams = gm.get("teams") or {}
                    home_name = ((tteams.get("home") or {}).get("team") or {}).get("name", "")
                    if home_name == home_team:
                        ph = ((tteams.get("home") or {}).get("probablePitcher") or {})
                        pa = ((tteams.get("away") or {}).get("probablePitcher") or {})
                        if ph.get("fullName"):
                            ctx["starting_pitcher_home"] = {
                                "name": ph.get("fullName"),
                                "id":   ph.get("id"),
                            }
                        if pa.get("fullName"):
                            ctx["starting_pitcher_away"] = {
                                "name": pa.get("fullName"),
                                "id":   pa.get("id"),
                            }
                        break
    except Exception as e:
        logger.debug("probable pitcher ctx fetch failed: %s", e)

    # 4) Attach Stuff+ from the existing cache when the starters resolved.
    try:
        from services.mlb_stuff_plus import get_by_name
        for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
            sp = ctx.get(side_key)
            if isinstance(sp, dict) and sp.get("name"):
                sp_grade = await get_by_name(sp["name"])
                if sp_grade and isinstance(sp_grade, dict):
                    if "stuff_plus" in sp_grade:
                        sp["stuff_plus"] = sp_grade["stuff_plus"]
                    if "pitching_plus" in sp_grade:
                        sp["pitching_plus"] = sp_grade["pitching_plus"]
    except Exception as e:
        logger.debug("stuff+ ctx fetch failed: %s", e)

    return ctx
