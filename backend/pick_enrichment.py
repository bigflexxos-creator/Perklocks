"""Universal ESPN-backed pick enrichment.

Runs immediately before `db.picks.insert_many(...)` so EVERY pick across
every sport gets the same treatment:

  1. **Active-roster validation** — calls `services.active_registry.is_active`
     to drop picks whose player has been retired / traded / cut.
     Currently armed for NBA + NFL (where the registry has live data).
     Soccer (CSL) keeps its own legacy `csl_espn_live` filter (already
     applied upstream in `sportdb_player_scorer`).
  2. **`pick_rationale` block** — structured "show your work" data for
     every player-prop pick: ESPN rank where available, raw stats, source
     of truth, the math (λ, prob), evidence + concerns tags. The UI's
     LockPickCard expands this into an audit panel so users understand
     WHY each pick made the board, not just the win-prob number.

The user request driving this module (2026-06-28):

    "ESPN data should be in pipeline for all sports"
    "I want education behind goalscorer not just random picks"

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.pick_enrichment")


# ─── Sport detection ──────────────────────────────────────────────
def _detect_sport(pick: dict) -> Optional[str]:
    """Maps the pick's sport_key / league / sport field to the
    `active_registry` sport key. Returns None for picks that don't map
    cleanly (those get passed through untouched)."""
    sport = (pick.get("sport") or "").lower()
    sport_key = (pick.get("sport_key") or "").lower()
    league = (pick.get("league") or "").lower()
    if "basketball" in sport_key or sport == "basketball" or "nba" in league:
        return "nba"
    if "football" in sport_key and "americanfootball" in sport_key.replace("_", "") + sport.replace(" ", ""):
        return "nfl"
    if sport_key.startswith("americanfootball_nfl") or "nfl" in league:
        return "nfl"
    if "americanfootball_ncaaf" in sport_key or "cfb" in league or "college football" in league:
        return "cfb"
    if sport_key.startswith("baseball_mlb") or "mlb" in league or sport == "baseball":
        return "mlb"
    if "soccer" in sport_key or sport == "soccer" or "football" in sport_key:
        return "soccer"
    return None


def _extract_player_name(pick: dict) -> Optional[str]:
    """Returns the player name attached to this pick, or None for
    team-level picks (moneyline, spread, total)."""
    name = pick.get("player_name") or pick.get("player") or ""
    if name and isinstance(name, str):
        return name.strip()
    # Try to parse from market string ("LeBron James Points Over 25.5")
    market = (pick.get("market") or "")
    for sep in (" - ", " Over ", " Under ", " Anytime ", " To Score", " To Record"):
        if sep in market:
            return market.split(sep, 1)[0].strip()
    return None


# ─── Rationale builder ──────────────────────────────────────────────
def _build_rationale(pick: dict, sport: str, name: str) -> dict[str, Any]:
    """Build a structured rationale for a player-prop pick using whatever
    sources we have for the given sport. Always returns a dict so the
    UI can render *something* — empty evidence/concerns lists are fine."""
    rationale: dict[str, Any] = {
        "summary": "",
        "data_source": pick.get("source") or "model",
        "evidence": [],
        "concerns": [],
        "espn_rank": None,
        "stats_this_season": None,
        "model_win_prob_pct": pick.get("win_probability"),
        "edge_percent": pick.get("edge_percent"),
        "lock_score": pick.get("lock_score"),
    }

    # Pull the active_registry record for this player (NBA / NFL / future sports).
    try:
        from services import active_registry
        rec = active_registry.get_record(sport, name) if sport in ("nba", "nfl") else None
    except Exception:
        rec = None

    if rec:
        # NFL nfl.com's first stat column is *not* games-played for passing
        # leaders (it's passing yards); cap any value > 100 to None for
        # display purposes so the UI doesn't claim "3587 games this season".
        gp = rec.get("games_played")
        if isinstance(gp, (int, float)) and gp > 200:
            gp = None
        rationale["stats_this_season"] = {
            "team": rec.get("team"),
            "minutes": rec.get("minutes"),
            "games_played": gp,
            "sources": list((rec.get("sources") or {}).keys()),
        }
        srcs = list((rec.get("sources") or {}).keys())
        if srcs:
            rationale["evidence"].append(
                f"✅ Active per {len(srcs)} ESPN-backed sources: {', '.join(srcs)}"
            )
        if isinstance(gp, (int, float)) and gp >= 30:
            rationale["evidence"].append(
                f"📊 {gp:.0f} games this season — large sample"
            )
        elif isinstance(gp, (int, float)) and gp <= 5:
            rationale["concerns"].append(
                f"⚠️ Only {gp:.0f} games played — small sample"
            )

    # CSL: piggy-back the live ESPN scorer board (rank + form).
    if sport == "soccer":
        try:
            import csl_espn_live as _csl
            live = _csl.get_live_form(name)
            if live:
                # Compute rank from in-memory leaderboard.
                rows = sorted(
                    (v for v in _csl._scorer_index.values() if (v.get("goals") or 0) > 0),
                    key=lambda r: (r.get("goals") or 0),
                    reverse=True,
                )
                rank = None
                key = _csl._norm(name)
                for i, r in enumerate(rows, 1):
                    if _csl._norm(r.get("name") or "") == key:
                        rank = i
                        break
                rationale["espn_rank"] = rank
                rationale["stats_this_season"] = {
                    "team": live.get("team"),
                    "goals": live.get("goals"),
                    "matches": live.get("matches"),
                    "assists": live.get("assists"),
                    "rate_per_match": live.get("rate_per_match"),
                }
                if rank and rank <= 10:
                    rationale["evidence"].append(
                        f"🥇 ESPN #{rank} scorer in his league — top-tier threat"
                    )
                elif rank and rank <= 25:
                    rationale["evidence"].append(
                        f"📈 ESPN #{rank} scorer — consistent contributor"
                    )
                elif rank:
                    rationale["concerns"].append(
                        f"⚠️ ESPN #{rank} scorer — outside top contributors"
                    )
                if (live.get("goals") or 0) > 0:
                    rationale["evidence"].append(
                        f"⚽ {live['goals']} goals in {live['matches']} matches"
                        f" ({live['rate_per_match']:.2f}/match)"
                    )
        except Exception:
            pass

    # CFB: pull cached CollegeFootballData ratings + portal + returning
    # production. Team(s) are parsed from `pick.event` ("Alabama @ Auburn").
    # The team carrying the pick is preferred via `pick.team` when set,
    # otherwise we fall back to the home team of the matchup string.
    if sport == "cfb":
        try:
            from services import cfb_rationale
            from server import db as _live_db
            team_for_pick = (pick.get("team") or "").strip()
            opponent = ""
            event = pick.get("event") or ""
            if "@" in event:
                away, _, home = event.partition("@")
                away = away.strip()
                home = home.strip()
                if not team_for_pick:
                    team_for_pick = home
                opponent = away if team_for_pick == home else home
            cfb = cfb_rationale.build_cfb_rationale_sync(
                _live_db, team_for_pick, opponent=opponent or None,
                player_name=name,
            )
            if cfb:
                # Replace summary if CFB has a richer one
                if cfb.get("summary"):
                    rationale["summary"] = cfb["summary"]
                # Merge evidence + concerns (CFB block lives alongside
                # universal model/edge bullets)
                rationale["evidence"].extend(cfb.get("evidence") or [])
                rationale["concerns"].extend(cfb.get("concerns") or [])
                # Attach CFB-specific structured blocks for the UI
                for k in ("team_quality", "matchup", "returning_production", "portal"):
                    v = cfb.get(k)
                    if v:
                        rationale[k] = v
                rationale["data_source"] = "collegefootballdata"
                rationale["engine"] = "cfb_rationale"
        except Exception as e:
            logger.debug(f"CFB rationale build skipped for {name}: {e}")

    # Win-prob and edge framing — universally useful.
    wp = pick.get("win_probability")
    edge = pick.get("edge_percent")
    if isinstance(wp, (int, float)):
        if wp >= 65:
            rationale["evidence"].append(f"💯 Model gives {wp:.1f}% win prob — high confidence")
        elif wp <= 35:
            rationale["concerns"].append(f"📉 Model gives only {wp:.1f}% win prob — longshot")
    if isinstance(edge, (int, float)) and edge >= 5.0:
        rationale["evidence"].append(f"📊 Edge vs market: +{edge:.1f}%")
    elif isinstance(edge, (int, float)) and edge <= -2.0:
        rationale["concerns"].append(f"📊 Negative edge vs market: {edge:.1f}%")

    # Existing pick_rationale (e.g. CSL synth picks already built one in
    # sportdb_player_scorer) wins — don't overwrite a richer source.
    existing = pick.get("pick_rationale")
    if isinstance(existing, dict) and existing.get("evidence"):
        # Merge our additions onto the existing block.
        existing.setdefault("evidence", []).extend(rationale["evidence"])
        existing.setdefault("concerns", []).extend(rationale["concerns"])
        return existing

    # Compose a one-line summary fallback.
    rank_part = f"ESPN #{rationale['espn_rank']} " if rationale["espn_rank"] else ""
    rationale["summary"] = (
        f"{name}: {rank_part}model {wp:.0f}% to hit"
        if isinstance(wp, (int, float))
        else f"{name}: pick rationale (auto)"
    )
    return rationale


# ─── Public entry point ──────────────────────────────────────────────
def enrich_picks_with_active_registry(picks: list[dict]) -> dict[str, int]:
    """Mutates each pick in `picks`:

      * Adds `pick_rationale` to every player pick (sport-specific).
      * Marks `validation_block` reasons on picks whose player is
        confirmed INACTIVE — caller may drop these before persistence.

    Returns counts: {enriched, blocked_inactive, skipped_team_pick, mlb_intel}.
    """
    import asyncio, re
    counts = {"enriched": 0, "blocked_inactive": 0, "skipped_team_pick": 0, "mlb_intel": 0}
    try:
        from services import active_registry
    except Exception:
        active_registry = None  # type: ignore

    # MLB hit-prop matchup intel — runs synchronously via asyncio.run on a
    # collected batch so we don't fire one task per pick.
    mlb_hit_picks: list[dict] = []
    for pick in picks:
        sport = _detect_sport(pick)
        name = _extract_player_name(pick)
        if not name or not sport:
            counts["skipped_team_pick"] += 1
            continue
        if active_registry is not None and sport in ("nba", "nfl"):
            verdict = active_registry.is_active(sport, name)
            if verdict is False:
                pick["validation_block"] = "inactive_player"
                pick["validation_block_reason"] = (
                    f"{name} not found in ESPN active {sport.upper()} roster + season leaders"
                )
                counts["blocked_inactive"] += 1
                continue
        try:
            pick["pick_rationale"] = _build_rationale(pick, sport, name)
            counts["enriched"] += 1
        except Exception as e:
            logger.debug(f"rationale build failed for {name} ({sport}): {e}")
        # Queue MLB hit-prop matchups for deep enrichment
        if sport == "mlb" and re.search(r"hit", (pick.get("market") or ""), re.IGNORECASE):
            mlb_hit_picks.append(pick)

    if mlb_hit_picks:
        counts["mlb_intel"] = _run_mlb_intel(mlb_hit_picks)
    return counts


def _run_mlb_intel(picks: list[dict]) -> int:
    """Resolves batter/pitcher IDs and runs the mlb_hitter_intel engine for
    each MLB hit-prop pick. Attaches the structured rationale dict back
    onto each pick. Runs the async work inside a fresh event loop because
    the surrounding `enrich_picks_with_active_registry` is sync."""
    import asyncio
    try:
        from server import db  # noqa: F401  (re-use the running app's mongo handle)
    except Exception:
        return 0
    from services import mlb_hitter_intel, mlb_matchup_resolver

    async def _one(pick: dict) -> bool:
        name = _extract_player_name(pick)
        event = pick.get("event") or ""
        evt_time = pick.get("event_time") or ""
        if not (name and event and evt_time):
            return False
        try:
            resolved = await mlb_matchup_resolver.resolve_matchup(db, name, event, evt_time)
            if not resolved:
                return False
            m = await mlb_hitter_intel.build_matchup(
                db,
                batter_id=resolved["batter_id"],
                pitcher_id=resolved["pitcher_id"],
                batter_name=name,
                pitcher_name=resolved.get("pitcher_name") or "",
                batter_team=resolved.get("batter_team") or "",
                pitcher_team=resolved.get("pitcher_team") or "",
                ballpark=resolved.get("ballpark"),
                batting_order=resolved.get("batting_order"),
                is_home=resolved.get("is_home", True),
            )
            # Replace the lightweight rationale with the full matchup model
            pick["pick_rationale"] = m.to_rationale()
            # Add a market-aware lean (line = 0.5 / 1.5 / 2.5 parsed from market)
            line = 0.5
            mlower = (pick.get("market") or "").lower()
            mlin = __import__("re").search(r"(\d+(?:\.\d+)?)", mlower)
            if mlin:
                try:
                    line = float(mlin.group(1))
                except Exception:
                    pass
            market_p = pick.get("implied_probability")
            if isinstance(market_p, (int, float)):
                if market_p > 1.0:
                    market_p = market_p / 100.0
                lean = mlb_hitter_intel.lean_and_edge(m, market_p, line=line)
                pick["pick_rationale"]["lean"] = lean["lean"]
                pick["pick_rationale"]["edge_pct_points"] = lean["edge_pct_points"]
                pick["pick_rationale"]["model_prob"] = lean["model_prob"]
            return True
        except Exception as e:
            logger.debug(f"mlb_intel build failed for {name}: {e}")
            return False

    async def _all():
        results = await asyncio.gather(*(_one(p) for p in picks), return_exceptions=False)
        return sum(1 for r in results if r)

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_all())
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"mlb_intel batch failed: {e}")
        return 0
