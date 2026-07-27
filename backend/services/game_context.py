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
import os
from typing import Any

logger = logging.getLogger("lockscore.game_context")


# 2026-07-22 — lazy Mongo handle for Statcast lookups. Avoids threading
# a `db` argument through the entire ctx-builder call chain (which
# would require touching every caller in sports_engine.py).
_MOTOR_CLIENT = None
_MOTOR_DB = None


def _get_db():
    """Return the Mongo db handle, initialising a lazy motor client
    on first call. Same DB name resolution as backend.deps."""
    global _MOTOR_CLIENT, _MOTOR_DB
    if _MOTOR_DB is not None:
        return _MOTOR_DB
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        _MOTOR_CLIENT = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        _MOTOR_DB = _MOTOR_CLIENT[os.getenv("DB_NAME") or "perkslocks_production"]
    except Exception as e:
        logger.debug("Lazy Mongo init failed: %s", e)
        _MOTOR_DB = None
    return _MOTOR_DB


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

    # 5) Pitcher throwing hand + season K stats (2026-07-21 Tier-1 fix)
    # Needed so K-prop picks can look up opposing team K% vs same hand.
    # Previously "Opp K% vs same hand" was a random uniform(0.65, 0.95)
    # placeholder — no real data behind pitcher K prop lock scores.
    try:
        from mlb_bvp import _get_json, MLB_STATS_BASE
        for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
            sp = ctx.get(side_key)
            if not (isinstance(sp, dict) and sp.get("id")):
                continue
            pdata = await _get_json(
                f"{MLB_STATS_BASE}/people/{sp['id']}?hydrate=stats(group=[pitching],type=[season])"
            )
            person = ((pdata or {}).get("people") or [{}])[0]
            hand = ((person.get("pitchHand") or {}).get("code") or "").upper()
            if hand in ("L", "R"):
                sp["throws"] = hand
            # Season K% + IP for stamina.
            for stgrp in person.get("stats") or []:
                for spl in stgrp.get("splits") or []:
                    st = spl.get("stat") or {}
                    ks = st.get("strikeOuts") or st.get("strikeouts")
                    bf = st.get("battersFaced") or st.get("plateAppearances")
                    ip = st.get("inningsPitched")
                    gs = st.get("gamesStarted") or st.get("gamesPitched") or 0
                    if ks is not None and bf:
                        try:
                            sp["k_pct"] = round(float(ks) / float(bf), 4)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                    if ip and gs:
                        try:
                            sp["ip_per_start"] = round(float(ip) / float(gs), 2)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                    break
    except Exception as e:
        logger.debug("pitcher hand/stats ctx fetch failed: %s", e)

    # 6) Opposing team K% vs starter's throwing hand — the KEY signal
    # for pitcher K props. Uses MLB Stats API team-splits endpoint via
    # our new mlb_team_k_intel cache (lazy-refreshed once per day).
    # Populates BOTH home starter (facing away lineup) and away starter
    # (facing home lineup) so build_pick can pick the right one per prop.
    try:
        from services.mlb_team_k_intel import get_team_k_pct_vs_hand
        # Resolve team IDs if game context doesn't already have them.
        home_id = game.get("home_team_id") or game.get("homeTeamId")
        away_id = game.get("away_team_id") or game.get("awayTeamId")
        if not (home_id and away_id):
            from mlb_bvp import _get_json, MLB_STATS_BASE
            commence = game.get("commence_time") or game.get("event_time") or ""
            date_str = commence[:10] if commence else None
            if date_str:
                sched = await _get_json(
                    f"{MLB_STATS_BASE}/schedule?sportId=1&date={date_str}"
                )
                for d in (sched or {}).get("dates", []):
                    for gm in d.get("games", []):
                        t = gm.get("teams") or {}
                        h = ((t.get("home") or {}).get("team") or {})
                        a = ((t.get("away") or {}).get("team") or {})
                        if h.get("name") == home_team:
                            home_id = home_id or h.get("id")
                            away_id = away_id or a.get("id")
                            break
        sph = ctx.get("starting_pitcher_home") or {}
        spa = ctx.get("starting_pitcher_away") or {}
        # db=None → lazy motor client inside mlb_team_k_intel.
        if sph.get("throws") and away_id:
            k = await get_team_k_pct_vs_hand(None, int(away_id), sph["throws"])
            if k:
                sph["opp_k_pct"] = k["k_pct"]
                sph["opp_k_rank"] = k.get("rank")
                sph["opp_k_team"] = k.get("team_name")
        if spa.get("throws") and home_id:
            k = await get_team_k_pct_vs_hand(None, int(home_id), spa["throws"])
            if k:
                spa["opp_k_pct"] = k["k_pct"]
                spa["opp_k_rank"] = k.get("rank")
                spa["opp_k_team"] = k.get("team_name")
    except Exception as e:
        logger.debug("team-K-vs-hand ctx fetch failed: %s", e)

    # 6a) Pitcher-vs-Team (PvT) career + recent K's ─────────────────
    # 2026-07-27 Wheeler bug fix — attach how each starter has done
    # against THIS specific opposing team historically. Feeds into
    # services.mlb_k_probability.compute_expected_k as a multiplier
    # that can shift λ (expected K) up or down 25%. This is what
    # would have caught the Wheeler Under 6.5 K blunder — his
    # career vs MIA is 198 K in 28 GS (7.07 K/start).
    try:
        from services.mlb_pvt import fetch_pvt, lookup_team_id
        sph = ctx.get("starting_pitcher_home") or {}
        spa = ctx.get("starting_pitcher_away") or {}
        # For the HOME starter, opponent = away team
        away_team_name = game.get("away_team") or game.get("awayTeam") or ""
        if sph.get("id") and away_team_name:
            aw_tid = away_id if isinstance(away_id, int) else await lookup_team_id(away_team_name)
            if aw_tid:
                pvt = await fetch_pvt(int(sph["id"]), int(aw_tid))
                if pvt:
                    sph["pvt"] = pvt
                    logger.debug(
                        "PvT attached: %s vs %s → %d GS / %d K / recent %s",
                        sph.get("name"), away_team_name,
                        pvt.get("gs_vs_team", 0), pvt.get("k_vs_team", 0),
                        pvt.get("recent_k_vs_team"),
                    )
        # For the AWAY starter, opponent = home team
        home_team_name = home_team or game.get("home_team") or ""
        if spa.get("id") and home_team_name:
            hm_tid = home_id if isinstance(home_id, int) else await lookup_team_id(home_team_name)
            if hm_tid:
                pvt = await fetch_pvt(int(spa["id"]), int(hm_tid))
                if pvt:
                    spa["pvt"] = pvt
                    logger.debug(
                        "PvT attached: %s vs %s → %d GS / %d K / recent %s",
                        spa.get("name"), home_team_name,
                        pvt.get("gs_vs_team", 0), pvt.get("k_vs_team", 0),
                        pvt.get("recent_k_vs_team"),
                    )
    except Exception as e:
        logger.debug("PvT ctx fetch failed: %s", e)

    # 6b) Statcast xwOBA-against attachment for both starting pitchers.
    # 2026-07-22 — feeds factor_pitcher_statcast_k_upside so elite whiff
    # pitchers surface Overs before their raw K/9 catches up.
    try:
        from services.mlb_statcast import get_pitcher_statcast
        for side_key in ("starting_pitcher_home", "starting_pitcher_away"):
            sp = ctx.get(side_key) or {}
            sp_name = sp.get("name")
            if not sp_name:
                continue
            sc = await get_pitcher_statcast(_get_db(), sp_name)
            if sc:
                sp["statcast"] = {
                    "xba_against":   sc.get("xba_against") or sc.get("xba"),
                    "xslg_against":  sc.get("xslg_against") or sc.get("xslg"),
                    "xwoba_against": sc.get("xwoba_against") or sc.get("xwoba"),
                    "xera":          sc.get("xera"),
                    "era":           sc.get("era"),
                }
    except Exception as e:
        logger.debug("pitcher statcast ctx fetch failed: %s", e)

    # 6c) Plate-umpire K-zone attachment (2026-07-22).
    # Feeds factor_umpire_k in mlb_feature_engine — wide-zone umps
    # (Angel Hernandez +2.8pp K) boost pitcher K Overs and fade hitter
    # Overs; tight-zone umps (Pat Hoberg -2.9pp K) do the opposite.
    # Non-blocking — home-plate ump only posts alongside lineups ~2h
    # pre-game so most regens will see None until near tip-off.
    try:
        from services.mlb_umpire import _fetch_plate_umpire, get_umpire_zone
        import httpx
        gid = game.get("id") or ""
        commence = game.get("commence_time") or game.get("event_time") or ""
        # gamePk resolution: reuse the mlb_usage helper.
        from services.mlb_usage import _find_gamepk_for_event
        home_name = ctx.get("home_team") or game.get("home_team", "")
        away_name = ctx.get("away_team") or game.get("away_team", "")
        event_str = f"{away_name} @ {home_name}"
        async with httpx.AsyncClient(timeout=8.0) as cx:
            pk = await _find_gamepk_for_event(cx, event_str, commence)
            if pk:
                ump_name = await _fetch_plate_umpire(cx, int(pk))
                if ump_name:
                    ctx["plate_umpire"] = {"name": ump_name}
                    z = get_umpire_zone(ump_name)
                    if z:
                        ctx["plate_umpire"].update(z)
    except Exception as e:
        logger.debug("plate umpire ctx fetch failed: %s", e)

    # 7) Team recent runs-per-game + bullpen ERA (2026-07-21 Phase 1)
    # Populates ctx["team_runs"][team_name.lower()] and
    # ctx["bullpens"][team_name.lower()]. These fuel factor_team_offense_recent
    # and factor_team_bullpen in mlb_feature_engine so MLB moneyline and
    # total picks can pass the 4-real-factor gate.
    #
    # Source: statsapi.mlb.com team stats — season aggregate is fine as
    # a starting point (upgrade to L15 in Phase 2). Team hitting: runs /
    # gamesPlayed. Team pitching: bullpen ERA extracted from
    # pitcherRole="RP" splits (or full-team ERA as fallback proxy).
    try:
        from mlb_bvp import _get_json, MLB_STATS_BASE
        season = int((game.get("commence_time") or "")[:4] or 2026)
        # Resolve team IDs (may already be present from step 6).
        if not (home_id and away_id):
            commence = game.get("commence_time") or ""
            date_str = commence[:10] if commence else None
            if date_str:
                sched = await _get_json(
                    f"{MLB_STATS_BASE}/schedule?sportId=1&date={date_str}"
                )
                for d in (sched or {}).get("dates", []):
                    for gm in d.get("games", []):
                        t = gm.get("teams") or {}
                        h = ((t.get("home") or {}).get("team") or {})
                        a = ((t.get("away") or {}).get("team") or {})
                        if h.get("name") == home_team:
                            home_id = home_id or h.get("id")
                            away_id = away_id or a.get("id")
                            break
        ctx.setdefault("team_runs", {})
        ctx.setdefault("bullpens", {})
        for tid, tname in ((home_id, home_team), (away_id, away_team)):
            if not (tid and tname):
                continue
            # Hitting: runs per game.
            hit = await _get_json(
                f"{MLB_STATS_BASE}/teams/{tid}/stats"
                f"?stats=season&group=hitting&sportIds=1&season={season}"
            )
            for stgrp in (hit or {}).get("stats") or []:
                for spl in stgrp.get("splits") or []:
                    st = spl.get("stat") or {}
                    r = st.get("runs")
                    g = st.get("gamesPlayed") or st.get("games") or 0
                    if r is not None and g:
                        try:
                            ctx["team_runs"][tname.strip().lower()] = round(float(r) / float(g), 2)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                    break

            # Pitching: bullpen ERA (relievers). Fetch pitching stats then
            # try to isolate relief; fall back to full-team ERA as proxy.
            pit = await _get_json(
                f"{MLB_STATS_BASE}/teams/{tid}/stats"
                f"?stats=season&group=pitching&sportIds=1&season={season}"
            )
            for stgrp in (pit or {}).get("stats") or []:
                for spl in stgrp.get("splits") or []:
                    st = spl.get("stat") or {}
                    era = st.get("era")
                    if era is not None:
                        try:
                            # Full-team ERA — proxy for bullpen. Upgrade
                            # to relief-only split in Phase 2.
                            ctx["bullpens"][tname.strip().lower()] = {
                                "era": float(era), "source": "team_season_era_proxy",
                            }
                        except (TypeError, ValueError):
                            pass
                    break
    except Exception as e:
        logger.debug("team offense/bullpen ctx fetch failed: %s", e)

    # 8) Team-vs-SP aggregate BvP OPS — deferred (Phase 1 follow-up).
    # Requires a get_team_vs_pitcher_ops helper in mlb_bvp.py that
    # aggregates the starting lineup's career OPS vs the opposing SP.
    # Left as None for now — ML picks depending on this factor will
    # instead get the 4th factor from park + weather when available.

    # 9) Per-hitter enrichment (2026-07-21) — the missing piece that
    # was gating ALL MLB hitter props to 0/5 factors. User: "Why are
    # Lane Thomas and Ty France not on the board — they hot".
    # For every hitter on both starting lineups, populate:
    #   ctx["hitters"][name.lower()] = {
    #     "l10_hit_rate": float,      # hits/AB over last 10 games
    #     "home_ops": float,          # season home OPS
    #     "away_ops": float,          # season away OPS
    #     "vs_l_ops"/"vs_r_ops": float,  # platoon splits
    #     "opp_pitcher_hand": "L"|"R",
    #     "is_home": bool,
    #     "bvp": {"pa": int, "ops": float, "hits": int, "ab": int},
    #   }
    # This unblocks hitter props (Hits, HRs, TBs, R, RBIs).
    try:
        import httpx
        from services.mlb_hitter_intel import fetch_batter_splits
        from mlb_bvp import _get_json, MLB_STATS_BASE
        season = int((game.get("commence_time") or str(datetime.now(timezone.utc).year))[:4] or 2026)
        ctx.setdefault("hitters", {})

        # Resolve gamePk to fetch lineups.
        game_pk = None
        commence = game.get("commence_time") or ""
        date_str = commence[:10] if commence else None
        if date_str:
            sched = await _get_json(
                f"{MLB_STATS_BASE}/schedule?sportId=1&date={date_str}"
                f"&hydrate=probablePitcher"
            )
            for d in (sched or {}).get("dates", []):
                for gm in d.get("games", []):
                    t = gm.get("teams") or {}
                    h = ((t.get("home") or {}).get("team") or {})
                    if h.get("name") == home_team:
                        game_pk = gm.get("gamePk")
                        break

        if game_pk:
            # Fetch boxscore for lineups.
            box = await _get_json(f"{MLB_STATS_BASE.replace('/v1', '/v1.1')}/game/{game_pk}/feed/live")
            teams_data = ((box or {}).get("liveData") or {}).get("boxscore", {}).get("teams") or {}

            sph_hand = ((ctx.get("starting_pitcher_home") or {}).get("throws") or "").upper()
            spa_hand = ((ctx.get("starting_pitcher_away") or {}).get("throws") or "").upper()
            sph_id = (ctx.get("starting_pitcher_home") or {}).get("id")
            spa_id = (ctx.get("starting_pitcher_away") or {}).get("id")

            async with httpx.AsyncClient(timeout=8.0) as client:
                for side_key, is_home_side in (("home", True), ("away", False)):
                    tside = teams_data.get(side_key) or {}
                    batting_order = tside.get("battingOrder") or []
                    players = tside.get("players") or {}
                    # Away team faces home SP; home team faces away SP.
                    opp_hand = (sph_hand if not is_home_side else spa_hand) or None
                    opp_pid = sph_id if not is_home_side else spa_id
                    for pid in (batting_order[:9] if batting_order else []):
                        pdata = players.get(f"ID{pid}") or {}
                        pname = ((pdata.get("person") or {}).get("fullName") or "").strip()
                        if not pname or not pid:
                            continue
                        try:
                            bs = await fetch_batter_splits(client, int(pid), season)
                        except Exception:
                            continue
                        hitter_row: dict[str, Any] = {
                            "is_home": is_home_side,
                            "opp_pitcher_hand": opp_hand if opp_hand in ("L", "R") else None,
                            "opp_pitcher_name": (
                                (ctx.get("starting_pitcher_away") if is_home_side
                                 else ctx.get("starting_pitcher_home")) or {}
                            ).get("name"),
                        }
                        if bs.last10_avg is not None:
                            hitter_row["l10_hit_rate"] = bs.last10_avg
                        if bs.ops_vs_l is not None:
                            hitter_row["vs_l_ops"] = bs.ops_vs_l
                        if bs.ops_vs_r is not None:
                            hitter_row["vs_r_ops"] = bs.ops_vs_r
                        if bs.season_ops is not None:
                            # Use season OPS as a proxy for home/away
                            # until we fetch true home/away splits.
                            hitter_row["home_ops" if is_home_side else "away_ops"] = bs.season_ops
                        # BvP vs opposing SP (mlb_bvp cache).
                        if opp_pid:
                            try:
                                from mlb_bvp import fetch_bvp
                                bvp_row = await fetch_bvp(int(pid), int(opp_pid))
                                if bvp_row and bvp_row.get("pa"):
                                    hitter_row["bvp"] = {
                                        "pa": bvp_row.get("pa"),
                                        "ops": bvp_row.get("ops"),
                                        "hits": bvp_row.get("hits"),
                                        "ab": bvp_row.get("ab"),
                                    }
                            except Exception:
                                pass
                        # 2026-07-22 Statcast xStats attachment.
                        # Uses the mlb_statcast_players cache — free
                        # signal for the feature engine (xBA/xwOBA/
                        # barrel%/hard-hit%). Silent on cache miss so
                        # non-qualifying hitters don't block picks.
                        try:
                            from services.mlb_statcast import get_batter_statcast
                            sc = await get_batter_statcast(_get_db(), pname)
                            if sc:
                                hitter_row["statcast"] = {
                                    "xba":         sc.get("xba"),
                                    "xslg":        sc.get("xslg"),
                                    "xwoba":       sc.get("xwoba"),
                                    "xba_diff":    sc.get("xba_diff"),
                                    "barrel_pct":  sc.get("barrel_pct"),
                                    "hard_hit":    sc.get("hard_hit"),
                                    "avg_ev":      sc.get("avg_ev"),
                                    "launch_angle":sc.get("launch_angle"),
                                    "sweet_spot":  sc.get("sweet_spot"),
                                }
                        except Exception:
                            pass
                        ctx["hitters"][pname.strip().lower()] = hitter_row
    except Exception as e:
        logger.debug("hitter enrichment failed: %s", e)

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

    # ── Book consensus & match tier (WORKS FOR EVERY MATCH — no
    # player-level data needed). Book consensus = how tight is the
    # pick's implied prob across all bookmakers on this game? Sharp
    # market = tight spread. Match tier from the tourney/league.
    try:
        h2h_bks = []
        for bk in (game.get("bookmakers") or []):
            for m in bk.get("markets") or []:
                if m.get("key") != "h2h":
                    continue
                for o in m.get("outcomes") or []:
                    name = (o.get("name") or "").strip()
                    price = o.get("price")
                    if isinstance(price, (int, float)) and name == home:
                        # American odds → implied prob
                        p = 100.0 / (price + 100.0) if price >= 100 else -price / (-price + 100.0)
                        h2h_bks.append(p)
        if len(h2h_bks) >= 3:
            spread_pp = (max(h2h_bks) - min(h2h_bks)) * 100.0
            ctx["book_consensus_spread_pp"] = round(spread_pp, 2)
    except Exception as e:
        logger.debug("tennis book-consensus ctx failed: %s", e)

    # Match tier detection
    league = (game.get("sport_title") or game.get("league") or "").lower()
    event  = (game.get("event") or game.get("tournament") or "").lower()
    combo  = f"{league} {event}"
    if any(t in combo for t in ("australian open","french open","wimbledon","us open","grand slam")):
        ctx["match_tier"] = "slam"
    elif any(t in combo for t in ("atp1000","masters 1000","atp masters","indian wells","miami open","monte carlo","madrid open","italian open","canadian open","cincinnati open","shanghai masters","paris masters")):
        ctx["match_tier"] = "atp1000"
    elif "wta 1000" in combo or "wta1000" in combo:
        ctx["match_tier"] = "wta1000"
    elif "atp 500" in combo or "wta 500" in combo:
        ctx["match_tier"] = "atp500"
    elif "atp 250" in combo or "wta 250" in combo:
        ctx["match_tier"] = "atp250"
    elif "challenger" in combo:
        ctx["match_tier"] = "challenger"
    elif "itf" in combo or "w15" in combo or "w25" in combo or "w40" in combo or "w60" in combo or "m15" in combo or "m25" in combo:
        ctx["match_tier"] = "itf"

    # 1) Sackmann career stats via services.tennis.fallback (real cache)
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        import os
        client = AsyncIOMotorClient(os.environ.get("MONGO_URL","mongodb://localhost:27017"))
        db = client["lockscore_db"]
        from services.tennis.fallback import get_player_stats, get_h2h
        surface = (game.get("surface") or "").strip() or "All"
        surface_key = surface.title() if surface.lower() in ("hard","clay","grass","all") else "All"
        stats_a = await get_player_stats(db, home, surface_key)
        stats_b = await get_player_stats(db, away, surface_key)
        if stats_a: ctx["sackmann_a"] = stats_a
        if stats_b: ctx["sackmann_b"] = stats_b
        # ── Fallback: for challenger-level players not in the top-2252
        # Sackmann roster, compute a lightweight rolling record straight
        # from ``tennis_matches``. Uses win%, ace%, DF% on the last 20
        # matches — gives DD enough signal to lift chalk/dog for these
        # otherwise unranked players.
        for suffix, player in (("a", home), ("b", away)):
            if ctx.get(f"sackmann_{suffix}"):
                continue
            # Use canonical "Lastname F." matches too via a regex.
            name_re = f"^{player}$"
            if player.strip().endswith("."):
                p2 = player.strip()[:-1].strip()
                parts = p2.rsplit(" ", 1)
                if len(parts) == 2 and len(parts[1]) <= 2:
                    name_re = f"^{parts[1]}\\w*\\s+{parts[0]}$"
            cursor = db.tennis_matches.find({
                "$or": [
                    {"winner_name": {"$regex": name_re, "$options": "i"}},
                    {"loser_name":  {"$regex": name_re, "$options": "i"}},
                ]
            }).sort("match_date", -1).limit(20)
            wins = 0
            losses = 0
            async for m in cursor:
                if m.get("winner_name","").lower().find(player.split()[0].lower()) >= 0:
                    wins += 1
                else:
                    losses += 1
            n = wins + losses
            if n >= 5:
                ctx[f"sackmann_{suffix}"] = {
                    "name":     player,
                    "surface":  surface_key,
                    "source":   "match_history_fallback",
                    "n_matches": n, "n_wins": wins, "n_losses": losses,
                    "win_pct":  round(100.0 * wins / n, 2),
                }
        # H2H
        h = await get_h2h(db, home, away)
        if h and h.get("matches", 0) >= 1:
            ctx["h2h_a_wins"] = h.get("a_wins", 0)
            ctx["h2h_b_wins"] = h.get("b_wins", 0)
    except Exception as e:
        logger.debug("tennis Sackmann ctx failed: %s", e)

    # 2) Surface Elo (Sackmann-derived; stored per-player-per-surface)
    try:
        elo_a = None; elo_b = None
        if ctx.get("sackmann_a"):
            elo_a = ctx["sackmann_a"].get("elo") or ctx["sackmann_a"].get("elo_rating")
        if ctx.get("sackmann_b"):
            elo_b = ctx["sackmann_b"].get("elo") or ctx["sackmann_b"].get("elo_rating")
        if isinstance(elo_a, (int, float)): ctx["surface_elo_a"] = elo_a
        if isinstance(elo_b, (int, float)): ctx["surface_elo_b"] = elo_b
    except Exception as e:
        logger.debug("tennis Elo ctx failed: %s", e)

    # 3) Fatigue — count matches in last 7 days from tennis_matches
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        client = AsyncIOMotorClient(os.environ.get("MONGO_URL","mongodb://localhost:27017"))
        db = client["lockscore_db"]
        for suffix, player in (("a", home), ("b", away)):
            cnt = await db.tennis_matches.count_documents({
                "$or": [{"winner_name": player}, {"loser_name": player}],
                "match_date": {"$gte": cutoff[:10]},
            })
            ctx[f"fatigue_{suffix}_matches_7d"] = int(cnt)
    except Exception as e:
        logger.debug("tennis fatigue ctx failed: %s", e)

    return ctx
