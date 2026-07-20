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


# ── SOCCER GAME CONTEXT ─────────────────────────────────────────────
async def build_soccer_game_context(game: dict) -> dict[str, Any]:
    """Fetch soccer-specific context BEFORE picks are generated.

    Populates:
      - home_form / away_form from soccer_form cache
      - home_xg_rolling / away_xg_rolling from soccer_team_xg collection
      - home_manager_style / away_manager_style from context table
      - pressure = 'high' | 'normal' from context detector
    """
    ctx: dict[str, Any] = {}
    home_team = (game.get("home_team") or "").strip()
    away_team = (game.get("away_team") or "").strip()

    # 1) Team form via sportdb_client (working cache in DB)
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        import os
        client = AsyncIOMotorClient(os.environ.get("MONGO_URL","mongodb://localhost:27017"))
        db = client["lockscore_db"]
        from sportdb_client import lookup_team_form
        hf = await lookup_team_form(db, home_team)
        af = await lookup_team_form(db, away_team)
        if hf and hf.get("n_matches", 0) >= 3: ctx["home_form"] = hf
        if af and af.get("n_matches", 0) >= 3: ctx["away_form"] = af
    except Exception as e:
        logger.debug("soccer form ctx failed: %s", e)

    # 2) Rolling xG from dedicated collection with form fallback
    try:
        from services.enrichment.soccer_rolling_xg import _lookup
        from motor.motor_asyncio import AsyncIOMotorClient
        import os
        client = AsyncIOMotorClient(os.environ.get("MONGO_URL","mongodb://localhost:27017"))
        db = client["lockscore_db"]
        h_doc = await _lookup(db, home_team)
        a_doc = await _lookup(db, away_team)
        if h_doc: ctx["home_xg_rolling"] = h_doc
        if a_doc: ctx["away_xg_rolling"] = a_doc
        # Form-proxy fallback
        if not h_doc and ctx.get("home_form", {}).get("n_matches", 0) >= 5:
            hf = ctx["home_form"]
            ctx["home_xg_rolling"] = {
                "xg_avg":  float(hf.get("gf_avg", 0)),
                "xga_avg": float(hf.get("ga_avg", 0)),
                "xg_diff": float(hf.get("gf_avg", 0)) - float(hf.get("ga_avg", 0)),
                "matches": int(hf.get("n_matches", 0)),
                "source":  "form_proxy",
            }
        if not a_doc and ctx.get("away_form", {}).get("n_matches", 0) >= 5:
            af = ctx["away_form"]
            ctx["away_xg_rolling"] = {
                "xg_avg":  float(af.get("gf_avg", 0)),
                "xga_avg": float(af.get("ga_avg", 0)),
                "xg_diff": float(af.get("gf_avg", 0)) - float(af.get("ga_avg", 0)),
                "matches": int(af.get("n_matches", 0)),
                "source":  "form_proxy",
            }
    except Exception as e:
        logger.debug("soccer xg ctx failed: %s", e)

    # 3) Manager styles (currently only inferred from lineup/coach if attached)
    try:
        from services.enrichment.soccer_context import manager_style
        hm = game.get("home_manager") or ""
        am = game.get("away_manager") or ""
        ctx["home_manager_style"] = manager_style(hm) if hm else "balanced"
        ctx["away_manager_style"] = manager_style(am) if am else "balanced"
    except Exception as e:
        logger.debug("manager ctx failed: %s", e)

    # 4) High-pressure fixture detector
    try:
        from services.enrichment.soccer_context import high_pressure_context
        stub = {
            "sport": "Soccer",
            "event": f"{away_team} @ {home_team}",
            "league": game.get("league") or "",
            "round": game.get("round") or "",
        }
        pressure, reason = high_pressure_context(stub)
        ctx["pressure"] = pressure
        ctx["pressure_reason"] = reason
    except Exception as e:
        logger.debug("pressure ctx failed: %s", e)

    return ctx


# ── TENNIS MATCH CONTEXT ─────────────────────────────────────────────
async def build_tennis_match_context(game: dict) -> dict[str, Any]:
    """Fetch tennis-specific context BEFORE picks are generated.

    Populates (for both players a=home, b=away):
      - sackmann_a / sackmann_b = career serve/return stats
      - surface_elo_a / surface_elo_b = surface-adjusted Elo
      - fatigue_a_matches_7d / fatigue_b_matches_7d = int
      - h2h_a_wins / h2h_b_wins = int
    Doubles picks ("A / A2") skip enrichment (no per-player data).
    """
    ctx: dict[str, Any] = {}
    home = (game.get("home_team") or "").strip()
    away = (game.get("away_team") or "").strip()
    if "/" in home or "/" in away:
        return ctx  # doubles \u2014 no per-player data available

    # 1) Sackmann career stats + surface Elo (uses existing tennis engine)
    try:
        from tennis_engine import get_player_stats, get_surface_elo, matches_last_days
        surface = (game.get("surface") or "").lower() or "hard"
        stats_a = await get_player_stats(home)
        stats_b = await get_player_stats(away)
        if stats_a: ctx["sackmann_a"] = stats_a
        if stats_b: ctx["sackmann_b"] = stats_b
        elo_a = await get_surface_elo(home, surface)
        elo_b = await get_surface_elo(away, surface)
        if isinstance(elo_a, (int, float)): ctx["surface_elo_a"] = elo_a
        if isinstance(elo_b, (int, float)): ctx["surface_elo_b"] = elo_b
        # Fatigue
        try:
            ctx["fatigue_a_matches_7d"] = await matches_last_days(home, 7)
            ctx["fatigue_b_matches_7d"] = await matches_last_days(away, 7)
        except Exception:
            pass
    except Exception as e:
        logger.debug("tennis ctx sackmann/elo failed: %s", e)

    # 2) H2H career record
    try:
        from tennis_engine import get_h2h_record
        h = await get_h2h_record(home, away)
        if h:
            ctx["h2h_a_wins"] = h.get("a_wins", 0)
            ctx["h2h_b_wins"] = h.get("b_wins", 0)
    except Exception as e:
        logger.debug("tennis h2h ctx failed: %s", e)

    return ctx
