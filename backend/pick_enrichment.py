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
            raw = market.split(sep, 1)[0].strip()
            # Strip trailing "(BAL)" / "(NYY)" team-tag suffix that MLB
            # markets carry — keeps the resolver lookup clean.
            import re as _re
            return _re.sub(r"\s*\([A-Z]{2,4}\)\s*$", "", raw).strip()
    return None


# ─── Rationale builder ──────────────────────────────────────────────
def _build_team_rationale(pick: dict, sport: Optional[str]) -> dict:
    """Build a sport-specific rationale for TEAM-LEVEL picks (MLB ML/RL,
    Soccer 1X2, NBA/NFL spreads, Tennis totals, etc.).

    HARD RULE (2026-06-28, user feedback "Why this pick is still generic"):
    NEVER emit generic boilerplate like:
      • "MLB · Spread: model 69% confidence"
      • "⚾ MLB: Pick · Event"
      • "📉 Longshot — model gives only X% win prob"
    These are not evidence — they restate what the card header already
    shows and degrade trust. The frontend's `hasRationale` check (in
    LockPickCard.tsx) hides the "Why this pick?" toggle entirely when
    summary + evidence + concerns are all empty, so returning an empty
    rationale produces a CLEAN card without a misleading toggle.

    For now we return a stub. Sport-specific team rationale (e.g. MLB
    pitcher matchup from `mlb_matchup_resolver_cache`, soccer xG-vs-xGA,
    NFL line trends) is generated async downstream in
    `services/sport_rationale.build_sport_specific` and merged in via
    the deep enrichment loop — that builder DOES emit real, factual
    bullets (e.g. "⚾ Red Sox: facing Gerrit Cole today — opportunity
    to hit a rated arm"). If that pass doesn't find any real signal,
    the toggle stays hidden, which is the correct UX.
    """
    return {
        "summary": "",
        "data_source": pick.get("source") or "model",
        "evidence": [],
        "concerns": [],
        "model_win_prob_pct": pick.get("win_probability"),
        "edge_percent": pick.get("edge_percent"),
        "lock_score": pick.get("lock_score"),
    }


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

    # ── SPORT-SPECIFIC RATIONALE (2026-06-28) ───────────────────────
    # Build sport-aware evidence FIRST so the generic
    # "Model gives X% win prob" bullet only fires as a fallback when
    # no sport-specific data is available. Today's slate breakdown:
    #   • MLB pitcher props (K's, Outs, Walks, ER) → fetch_pitcher_h2h
    #   • MLB team picks (ML/RL/Total)             → matchup_resolver
    #   • Tennis ML / Totals                       → tennis_players ELO
    # Soccer goalscorer / MLB hit / CFB picks already have their own
    # rationale builders above and skip this layer.
    sport_specific_added = False
    try:
        from services import sport_rationale
        ss = sport_rationale.build_sport_specific_sync(pick, sport, name)
        if ss.get("evidence") or ss.get("concerns"):
            rationale["evidence"].extend(ss.get("evidence") or [])
            rationale["concerns"].extend(ss.get("concerns") or [])
            rationale["engine"] = rationale.get("engine") or f"sport_rationale.{sport}"
            sport_specific_added = True
        # Merge sport-specific rolling form (L5/L10/L20) if the builder
        # produced it — used for MLB pitchers, NBA/NFL players, etc.
        # The LockPickCard chip renders `pick_rationale.recent_form`.
        if ss.get("recent_form"):
            existing_rf = rationale.get("recent_form") or {}
            merged_rf = dict(existing_rf)
            merged_rf.update(ss["recent_form"])
            rationale["recent_form"] = merged_rf
    except Exception as e:
        logger.debug(f"sport_rationale failed for {sport}/{name}: {e}")

    # Win-prob and edge framing — only as a last-resort SOFT fallback
    # (down-graded post-user-feedback 2026-06-28). We no longer emit
    # the generic "💯 Model gives X% — high confidence" because it
    # reads identically across all sports → users perceived it as
    # boilerplate. When the sport-specific layer can't speak, we'd
    # rather omit the WHY-WE-LIKE-IT block than fill it with a generic
    # bullet. The expanded panel will still show the model summary
    # line + edge chip — the data is there, just not in cardboard
    # "high confidence" prose.
    wp = pick.get("win_probability")
    edge = pick.get("edge_percent")
    # Concerns are still informative — a true "longshot" / "negative
    # edge" callout is sport-agnostic, helpful, and short.
    if not sport_specific_added:
        if isinstance(wp, (int, float)) and wp <= 30:
            rationale["concerns"].append(f"📉 Longshot — model gives only {wp:.1f}% win prob")
        if isinstance(edge, (int, float)) and edge <= -5.0:
            rationale["concerns"].append(f"📊 Heavy chalk — negative edge vs market ({edge:.1f}%)")

    # Existing pick_rationale (e.g. CSL synth picks already built one in
    # sportdb_player_scorer) wins — don't overwrite a richer source.
    existing = pick.get("pick_rationale")
    if isinstance(existing, dict) and existing.get("evidence"):
        # Prune the legacy generic bullets ("Model gives X% win prob",
        # "Edge vs market: +N%") off the existing block before merging
        # — otherwise re-enrichment passes preserve them indefinitely,
        # defeating the sport-specific layer. We identify them by their
        # leading emoji + canonical phrasing.
        def _is_generic(line: str) -> bool:
            return (
                "Model gives" in line
                or "Edge vs market" in line
                or "Negative edge vs market" in line
            )
        existing["evidence"] = [
            e for e in (existing.get("evidence") or []) if not _is_generic(e)
        ]
        existing["concerns"] = [
            c for c in (existing.get("concerns") or []) if not _is_generic(c)
        ]
        # Merge our additions onto the existing block, de-duped.
        for e in rationale["evidence"]:
            if e not in existing["evidence"]:
                existing["evidence"].append(e)
        for c in rationale["concerns"]:
            if c not in existing["concerns"]:
                existing["concerns"].append(c)
        # Adopt the new engine tag when sport_rationale fired — so the
        # rationale block self-documents which layer produced it.
        if rationale.get("engine") and not existing.get("engine"):
            existing["engine"] = rationale["engine"]
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
    import asyncio  # noqa: F401
    import re
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
            # Team-level pick (no player) — still attach a minimal rationale
            # so the "Why this pick?" toggle has SOMETHING to show. Without
            # this, Tennis totals / MLB MLs / Soccer 1X2 lines render with no
            # toggle at all and users think the feature is broken.
            try:
                pick["pick_rationale"] = _build_team_rationale(pick, sport)
            except Exception as e:
                logger.debug(f"team rationale build failed: {e}")
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

            # ── Vegas + lineup context (2026-07-01 spec) ──
            # Team-implied runs derived from the game total (over/under
            # main line). If we can't find it, fall back to the MLB
            # league average of ~4.4 runs per team so we can still
            # score the pitcher-matchup rationale.
            # (2026-07-02: previous code dropped 100% of hitter picks
            # when game_total was absent — that killed the vs-pitcher
            # panel the user asked for. We still gate at a lower level
            # in the ranker if the fallback is used and the model is
            # low confidence.)
            batting_order = resolved.get("batting_order")
            team_implied_runs = (
                pick.get("team_implied_runs")
                or resolved.get("team_implied_runs")
            )
            if not team_implied_runs:
                game_total = pick.get("game_total") or resolved.get("game_total")
                if game_total:
                    team_implied_runs = float(game_total) / 2.0
                else:
                    # League-avg fallback so pitcher matchup still renders.
                    team_implied_runs = 4.4
                    pick["_hitter_context_fallback"] = "league_avg_team_runs"
            obp_in_front = pick.get("obp_in_front") or resolved.get("obp_in_front")

            try:
                m = await mlb_hitter_intel.build_matchup(
                    db,
                    batter_id=resolved["batter_id"],
                    pitcher_id=resolved["pitcher_id"],
                    batter_name=name,
                    pitcher_name=resolved.get("pitcher_name") or "",
                    batter_team=resolved.get("batter_team") or "",
                    pitcher_team=resolved.get("pitcher_team") or "",
                    ballpark=resolved.get("ballpark"),
                    batting_order=batting_order,
                    is_home=resolved.get("is_home", True),
                    team_implied_runs=team_implied_runs,
                    obp_in_front=obp_in_front,
                    strict=False,   # allow fallback so vs-pitcher renders
                )
            except mlb_hitter_intel.HitterContextMissing as ctx_err:
                # No longer happens with strict=False, but keep the
                # branch as a safety net.
                pick["_hitter_context_missing"] = str(ctx_err)
                logger.info("hitter_intel gated pick out: %s | %s", name, ctx_err)
                return False

            # Replace the lightweight rationale with the full matchup model
            pick["pick_rationale"] = m.to_rationale()

            # Attach the three sub-scores onto the pick itself so
            # downstream rankers/analytics can inspect them.
            pick["hitter_scores"] = {
                "p_hit": m.p_hit_score,
                "p_rbi": m.p_rbi_score,
                "p_run": m.p_run_score,
            }

            # Market-aware lean.
            line = 0.5
            market_label = pick.get("market") or ""
            mlower = market_label.lower()
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
                lean = mlb_hitter_intel.lean_and_edge(
                    m, market_p, line=line, market=market_label,
                )
                pick["pick_rationale"]["lean"] = lean["lean"]
                pick["pick_rationale"]["edge_pct_points"] = lean["edge_pct_points"]
                pick["pick_rationale"]["model_prob"] = lean["model_prob"]
                pick["pick_rationale"]["sub_scores"] = lean.get("sub_scores")
            return True
        except Exception as e:
            logger.debug(f"mlb_intel build failed for {name}: {e}")
            return False

    async def _all():
        results = await asyncio.gather(*(_one(p) for p in picks), return_exceptions=False)
        return sum(1 for r in results if r)

    try:
        # If we're already inside a running event loop (the normal case
        # when pick_enrichment is called from the async refresh handler),
        # spinning up a nested loop fails with "Cannot run the event
        # loop while another loop is running". Run on a worker thread
        # so it gets its own asyncio.run() context.
        import concurrent.futures
        try:
            import asyncio as _asyncio
            _asyncio.get_running_loop()
            inside_loop = True
        except RuntimeError:
            inside_loop = False
        if inside_loop:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(lambda: asyncio.run(_all()))
                return future.result(timeout=120)
        # No running loop — original sync path works.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_all())
        finally:
            loop.close()
    except Exception as e:
        logger.warning(f"mlb_intel batch failed: {e}")
        return 0
