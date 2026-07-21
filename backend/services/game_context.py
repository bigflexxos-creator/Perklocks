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
