"""MLS ESPN Direct-Inject Worker.

User report 2026-07-22: MLS scorer picks (Surridge, Bouanga, Mercau)
kept getting killed by the main pick pipeline — chalk trap, longshot
trap, starter-gate, correlated dedupe, board validator, etc. — despite
being generated correctly at the source.

This worker BYPASSES the entire pipeline. It:
  1. Pulls MLS events from The Odds API (once every 15 min).
  2. Cross-references `espn_mls_stats` for real top scorers by team.
  3. Attaches per-opponent matchup history from `mls_player_matchup_history`.
  4. Writes picks DIRECTLY to `db.picks` with `off_board=False`,
     `no_bet=False`, and `source="mls_espn_direct"`.
  5. Uses deterministic IDs so re-runs are idempotent (upsert).

Runs from server startup as a fire-and-forget async task.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.mls_direct")


def _norm(n: str) -> str:
    if not n:
        return ""
    nk = unicodedata.normalize("NFKD", n)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


# ─── MLS team-name aliases (2026-07-26) ──────────────────────────────
# ESPN's `espn_mls_stats.team` uses short forms ("LAFC", "RBNY") while
# The Odds API returns long forms ("Los Angeles FC", "Red Bull New
# York"). Substring match alone fails for LAFC/LAGalaxy/NYCFC etc.
# This alias table lets `_team_match` recognise the equivalent names.
_MLS_TEAM_ALIASES: dict[str, list[str]] = {
    "lafc":            ["los angeles fc", "la fc", "los angeles"],
    "los angeles fc":  ["lafc", "la fc"],
    "la galaxy":       ["los angeles galaxy", "galaxy"],
    "los angeles galaxy": ["la galaxy", "galaxy"],
    "nycfc":           ["new york city fc", "new york city", "ny city"],
    "new york city fc":["nycfc", "ny city fc", "new york city"],
    "rbny":            ["red bull new york", "new york red bulls", "ny red bulls"],
    "red bull new york":["rbny", "new york red bulls", "ny red bulls"],
    "new york red bulls":["rbny", "red bull new york"],
    "cf montreal":     ["montreal impact", "cf mtl"],
    "cf mtl":          ["cf montreal", "montreal"],
    "d.c. united":     ["dc united", "d c united", "washington dc"],
    "dc united":       ["d.c. united", "d c united"],
    "sporting kc":     ["sporting kansas city", "kansas city sc"],
    "sporting kansas city": ["sporting kc", "kansas city sc"],
    "sjq":             ["san jose earthquakes", "quakes"],
    "san jose earthquakes":["sjq", "quakes"],
    "portland":        ["portland timbers", "timbers"],
    "portland timbers":["portland", "timbers"],
    "salt lake":       ["real salt lake", "rsl"],
    "real salt lake":  ["salt lake", "rsl"],
    "st louis":        ["st. louis city sc", "st louis city sc", "stl city"],
    "st. louis city sc":["st louis", "stl city"],
    "st louis city sc":["st louis", "stl city"],
    "philly":          ["philadelphia union", "union"],
    "philadelphia union":["philly", "union"],
    "columbus":        ["columbus crew sc", "crew"],
    "columbus crew sc":["columbus", "crew"],
    "seattle":         ["seattle sounders fc", "sounders"],
    "seattle sounders fc":["seattle", "sounders"],
    "atlanta":         ["atlanta united fc", "atlanta united"],
    "atlanta united fc":["atlanta", "atlanta united"],
    "new england":     ["new england revolution", "revolution", "revs"],
    "new england revolution":["new england", "revs"],
    "colorado":        ["colorado rapids", "rapids"],
    "colorado rapids": ["colorado", "rapids"],
    "chicago":         ["chicago fire", "fire"],
    "chicago fire":    ["chicago", "fire"],
    "houston":         ["houston dynamo fc", "dynamo"],
    "houston dynamo fc":["houston", "dynamo"],
    "minnesota":       ["minnesota united fc", "loons"],
    "minnesota united fc":["minnesota", "loons"],
    "vancouver":       ["vancouver whitecaps", "whitecaps"],
    "vancouver whitecaps":["vancouver", "whitecaps"],
    "toronto":         ["toronto fc"],
    "toronto fc":      ["toronto"],
    "orlando":         ["orlando city sc"],
    "orlando city sc": ["orlando"],
    "cincinnati":      ["fc cincinnati"],
    "fc cincinnati":   ["cincinnati"],
    "dallas":          ["fc dallas"],
    "fc dallas":       ["dallas"],
    "miami":           ["inter miami cf"],
    "inter miami cf":  ["miami"],
    "austin":          ["austin fc"],
    "austin fc":       ["austin"],
    "nashville":       ["nashville sc"],
    "nashville sc":    ["nashville"],
    "san diego":       ["san diego fc"],
    "san diego fc":    ["san diego"],
    "charlotte":       ["charlotte fc"],
    "charlotte fc":    ["charlotte"],
}


def _team_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a = a.lower().strip()
    b = b.lower().strip()
    # Strip common suffixes.
    for suf in (" fc", " f.c.", " sc", " cf", " united",
                " football club"):
        a = a.replace(suf, "")
        b = b.replace(suf, "")
    a = a.strip(); b = b.strip()
    if not a or not b:
        return False
    # Direct / substring match — safe only when strings are meaningful.
    # Require min length 4 on the shorter side to avoid false positives
    # like "la" matching "atlanta" or "cin" matching "cincinnati".
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and short in long_:
        return True
    # Alias table: check whether any alias of `a` matches `b` (or vice
    # versa). This is what fixes LAFC↔"Los Angeles FC". We check the
    # alias against the OTHER string only, never against the target
    # itself (that would be a self-alias false positive — the reason
    # Cincinnati was matching LAFC previously).
    #
    # NOTE: Only accept EXACT alias matches (alt == other). Partial
    # substring matches through aliases cause "LA Galaxy" to match
    # "Los Angeles FC" through the "los angeles galaxy" alias containing
    # "los angeles". Exact-match is safe because the alias table lists
    # every canonical form explicitly.
    for target, other in ((a, b), (b, a)):
        aliases = _MLS_TEAM_ALIASES.get(target, [])
        for alt in aliases:
            if alt and alt == other:
                return True
    return False


def _american(r: float) -> int:
    """DEPRECATED — Session A (2026-06) synthetic-odds purge.

    Used to synthesize sportsbook American odds from a model
    probability (rate).  Retained ONLY as a stub that raises so any
    accidental re-use is caught in CI.  A pick without a real
    sportsbook line MUST publish with book_odds=None +
    no_real_book_line=True + odds_source='MODEL_ONLY'.
    """
    raise NotImplementedError(
        "_american is purged by Session A — do not synthesize sportsbook "
        "American odds from model probability.  Emit book_odds=None + "
        "no_real_book_line=True + odds_source='MODEL_ONLY' instead.",
    )


async def _fetch_mls_events(cx: httpx.AsyncClient) -> list[dict]:
    key = os.getenv("THE_ODDS_API_KEY", "")
    if not key:
        return []
    try:
        from services.odds_cache import cached_httpx_get
        data = await cached_httpx_get(
            "https://api.the-odds-api.com/v4/sports/soccer_usa_mls/events",
            {},
            api_key=key,
            endpoint_type="events_list",
            caller="mls_direct_inject._fetch_mls_events",
            sport_key="soccer_usa_mls",
            skip_completed=True,
            timeout=10,
        )
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("MLS events fetch failed: %s", e)
        return []


async def _generate_for_event(ev: dict, all_scorers: list[dict],
                               matchup_get) -> list[dict]:
    """Generate picks using the Player Prop Intelligence System (Phase 3).

    Uses matchup intelligence + market selector to route each player
    to their best-fit market(s) with a live probability boost/drop from
    the MatchupContext (home/away, form extremes, rest days).
    """
    from services.player_props import (
        get_player_stats,
        classify_archetype,
        select_markets_v3,
        build_matchup_context,
    )
    from services.player_props.models import MatchupSplit, Archetype
    from deps import db as _v3_db

    home = (ev.get("home_team") or "").strip()
    away = (ev.get("away_team") or "").strip()
    if not home or not away:
        return []

    # Collect scorer candidates by side.
    home_sc: list[dict] = []
    away_sc: list[dict] = []
    for r in all_scorers:
        team = r.get("team") or ""
        try:
            goals = int(r.get("goals") or 0)
        except Exception:
            goals = 0
        try:
            assists = int(r.get("assists") or 0)
        except Exception:
            assists = 0
        # Require *some* attacking output — a 0G/0A defender should not
        # surface even if the odds board prices them.
        if goals < 3 and assists < 3:
            continue
        name = r.get("name") or ""
        if not name:
            continue
        entry = {"name": name, "team": team,
                 "goals": goals, "assists": assists,
                 "games": int(r.get("games") or 0)}
        if _team_match(team, home):
            home_sc.append(entry)
        elif _team_match(team, away):
            away_sc.append(entry)
    home_sc.sort(key=lambda x: x["goals"] + x["assists"], reverse=True)
    away_sc.sort(key=lambda x: x["goals"] + x["assists"], reverse=True)

    picks: list[dict] = []
    commence = ev.get("commence_time") or ""
    event_id = ev.get("id") or f"MLS-{home}-{away}"

    async def _emit_for(entry: dict, opp: str, is_home: bool) -> list[dict]:
        name = entry["name"]
        team = entry["team"]

        stats = await get_player_stats(name, league_hint="MLS")
        if not stats or not stats.data_ok:
            return []

        rec = await matchup_get(name, opp)
        split = None
        if rec:
            split = MatchupSplit(
                opponent=opp,
                matches=int(rec.get("matches", 0)),
                goals=int(rec.get("goals", 0)),
                assists=int(rec.get("assists", 0)),
                scored_matches=int(rec.get("scored_matches", 0)),
                assist_matches=int(rec.get("assist_matches", 0)),
                shots=int(rec.get("shots", 0)),
            )

        archetype = classify_archetype(stats)
        if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
            return []

        matchup_ctx = build_matchup_context(
            stats, opp,
            is_home=is_home,
            event_commence=commence,
            last_match_iso=None,
            split=split,
        )

        routes = await select_markets_v3(
            _v3_db, stats, archetype, split, matchup_ctx,
            opp_team_name=opp,
            sport_key="soccer_usa_mls",
            is_home=is_home,
            lineup_status="unknown",
        )
        if not routes:
            return []

        out: list[dict] = []
        # ── Data-quality gate (iter-93) ─────────────────────────────
        # User report: "Still got a bunch of fake stats and picks that
        # shouldn't be on board for mls ... I don't want random goal-
        # scorers or assist bets I want real data bets".
        #
        # Reject picks whose upstream signal is too weak to trust:
        #   • no real season sample (games < 3 AND minutes < 180)
        #   • no attacking output signal (goals_per_90 + assists_per_90
        #     + npxg_per_90 all zero — data was never populated)
        #   • market_fit BELOW 40 (already partial-penalised further
        #     down, but at market_fit<40 the model has essentially told
        #     us this player is a bad match — no reason to surface it)
        #   • confidence LOW + market_fit < 60 combined — two weak
        #     signals stacking means junk pick.
        _game_sample_ok = stats.games >= 3 or stats.minutes >= 180
        _has_attack_signal = (
            (stats.goals_per_90 or 0) > 0.02
            or (stats.assists_per_90 or 0) > 0.02
            or (stats.npxg_per_90 or 0) > 0.02
        )
        if not (_game_sample_ok and _has_attack_signal and stats.data_ok):
            return []
        for route in routes:
            if route.market_fit < 40:
                continue
            if route.confidence == "LOW" and route.market_fit < 60:
                continue
            p = route.probability
            # Session A (2026-06) synthetic-odds purge — the MLS
            # direct-inject path has NO real sportsbook player-prop
            # line source in this pod.  We MUST NOT convert model
            # probability back into an American book_odds price.
            # Emit book_odds=None + no_real_book_line=True instead.
            book_odds = None

            if p >= 0.55: lock = 95.0
            elif p >= 0.40: lock = 91.0
            elif p >= 0.25: lock = 87.0
            elif p >= 0.15: lock = 83.0
            else:            lock = 80.0
            if route.confidence == "HIGH": lock = min(99.0, lock + 2.0)
            elif route.confidence == "LOW": lock = max(75.0, lock - 3.0)
            if route.market_fit >= 90:
                lock = min(99.0, lock + 1.0)
            elif route.market_fit < 40:
                lock = max(75.0, lock - 2.0)

            grade = ("Strong Lock" if lock >= 95 else
                      ("Lock" if lock >= 90 else "Playable"))

            # ── Strict Edge Gate (v3) ──────────────────────────────
            # No real sportsbook player-prop line → no true edge to
            # measure. Model-derived book_odds are fair-value + juice
            # only; reporting a fabricated edge would be circular.
            # Session A (2026-06): edge_percent stays None for EVERY
            # market emitted from this path — the pipeline had no real
            # sportsbook player-prop lines regardless of market.  Do
            # NOT report a fabricated 4.0% edge for the non-v3 markets.
            edge_val = None

            pick = {
                "id": f"mls-direct-{route.market}-{event_id}-{name.replace(' ', '_').lower()}",
                "external_id": f"MLS-DIRECT-{route.market}-{event_id}-{name}",
                "sport": "Soccer",
                "league": "MLS",
                "event": f"{away} @ {home}",
                "event_time": commence,
                "market": f"{name} {route.label}",
                "market_type": route.market,
                "selection": name,
                "pick_side": name,
                "model_win_prob": p,
                "model_probability": p,
                "win_probability": round(p * 100, 2),
                "book_odds": book_odds,
                "no_real_book_line": True,
                "book_implied_prob": None,
                "lock_score": lock,
                "lock_score_v2": lock,
                "lock_score_v2_raw": lock,
                "lock_score_peak": lock,
                "edge_percent": edge_val,
                "odds_source": "MODEL_ONLY",
                "odds_status": "no_book_line",
                "confidence_penalty": -5 if (route.market == "anytime_goal_scorer") else 0,
                "grade": grade,
                "confidence": grade,
                "status": "pending",
                "no_bet": False,
                "off_board": False,
                "elite_player": True,
                "is_elite": True,
                "is_synthetic_scorer": True,
                "is_long_shot": True,
                "synthetic": True,
                "synthetic_source": (
                    "goal_scorer_v3" if route.market == "anytime_goal_scorer"
                    else "player_prop_intelligence_v2"
                ),
                "source": (
                    "goal_scorer_v3" if route.market == "anytime_goal_scorer"
                    else "player_prop_intelligence_v2"
                ),
                "home_team": home,
                "away_team": away,
                "home_team_name": home,
                "away_team_name": away,
                "sport_key": "soccer_usa_mls",
                "archetype": archetype.value,
                "archetype_display": archetype.display(),
                "market_fit": route.market_fit,
                "samples": {
                    "goals": stats.goals,
                    "assists": stats.assists,
                    "games": stats.games,
                    "goals_per_90": stats.goals_per_90,
                    "assists_per_90": stats.assists_per_90,
                    "leaderboard_team": team,
                    "source": stats.source,
                },
                "pick_rationale": {
                    "engine": "goal_scorer_v3" if route.market == "anytime_goal_scorer" else "player_prop_intelligence_v2",
                    "engine_version": route.recommendation.debug.get("engine", "player_prop_intelligence_v2"),
                    "summary": (
                        f"{name} ({archetype.display()}): "
                        f"model p={p*100:.0f}% · {stats.goals}G/{stats.assists}A "
                        f"in {stats.games} games · fit {route.market_fit}%."
                    ),
                    "evidence": route.recommendation.evidence,
                    "concerns": route.recommendation.concerns,
                    "matchup": {
                        "player": name, "team": team, "opponent": opp,
                        "is_home": is_home,
                    },
                    "recent_form": {
                        "engine": "player_prop_intelligence_v2",
                        "form_score": stats.form_score,
                        "form_label": stats.form_label,
                    },
                    "model_debug": route.recommendation.debug,
                    "market_fit": route.market_fit,
                    # v3-only signals for the goal-scorer market
                    "v3_signals": (
                        {
                            "lam_player":    route.recommendation.debug.get("lam_player"),
                            "lam_team":      route.recommendation.debug.get("lam_team"),
                            "lam_opponent":  route.recommendation.debug.get("lam_opponent"),
                            "expected_minutes": route.recommendation.debug.get("expected_minutes"),
                            "goal_share":    route.recommendation.debug.get("goal_share"),
                            "ensemble":      route.recommendation.debug.get("ensemble"),
                            "p_first":       route.recommendation.debug.get("p_first"),
                            "p_2plus":       route.recommendation.debug.get("p_2plus"),
                            "seasons_used":  route.recommendation.debug.get("seasons_used"),
                        } if route.market == "anytime_goal_scorer" else None
                    ),
                },
            }

            if split and split.matches:
                pick["matchup_history"] = {
                    "opponent": split.opponent,
                    "matches": split.matches,
                    "goals": split.goals,
                    "assists": split.assists,
                    "goals_per_match": round(split.gpm(), 2),
                    "assists_per_match": round(split.apm(), 2),
                    "scored_in": split.scored_matches,
                    "assisted_in": split.assist_matches,
                    "gi_rate": round(split.gi_rate(), 3),
                    "recent": (rec.get("recent") or [])[:3] if rec else [],
                }

            out.append(pick)
        return out

    sem = asyncio.Semaphore(6)
    async def _run(entry: dict, opp: str, is_home: bool):
        async with sem:
            return await _emit_for(entry, opp, is_home)

    tasks = [_run(e, away, True) for e in home_sc[:6]]
    tasks += [_run(e, home, False) for e in away_sc[:6]]
    for lst in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(lst, list):
            picks.extend(lst)
    return picks


async def run_once() -> dict:
    """Do a single MLS ESPN direct-inject pass."""
    from deps import db
    from services.mls_player_matchup_history import get_player_vs_opponent
    scorers = await db.espn_mls_stats.find({}).to_list(length=500)
    if not scorers:
        return {"ok": False, "reason": "no_espn_mls_stats"}

    async with httpx.AsyncClient(timeout=15) as cx:
        events = await _fetch_mls_events(cx)
    if not events:
        return {"ok": False, "reason": "no_events"}

    now = datetime.now(timezone.utc).isoformat()
    # Compute today's pick_date in UTC. Games with UTC commence_time
    # spanning midnight bucket to the earlier date (matches game_slate).
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    all_picks = []
    for ev in events:
        # Skip past events.
        ct = ev.get("commence_time") or ""
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if dt < datetime.now(timezone.utc):
                continue
        except Exception:
            pass
        picks = await _generate_for_event(ev, scorers, get_player_vs_opponent)
        all_picks.extend(picks)

    if not all_picks:
        return {"ok": True, "generated": 0}

    # Direct upsert bypassing all pipeline filters.
    # ── Block 2D C1 (2026-08) — publication-bypass observability ──
    # This writer historically wrote user-visible picks directly to
    # db.picks.  Per Block 2D directive, every bypass is now tagged
    # so downstream telemetry / dashboards can distinguish canonical
    # vs bypass writes.  Fixing the bypass itself (routing through
    # canonical publication) is deferred to Block 2E because it
    # requires the direct-inject picks to survive the canonical
    # publication_barrier + strict>85 gate — potentially reducing
    # coverage until each MLS direct-inject pick is re-modelled with
    # feature-engine evidence.
    from pymongo import ReplaceOne
    from services.soccer_prop_inject import _derive_pick_date
    from services.canonical_publication_barrier import apply_canonical_barrier
    ops = []
    for p in all_picks:
        p["created_at"] = now
        # Games > 24h away get stamped with their actual event date so
        # they don't crowd today's board (2026-07-26 user report).
        p["pick_date"] = _derive_pick_date(p.get("event_time"), today_str)
        p["updated_at"] = now
        # Block 2D C1 — explicit bypass marker.
        p["bypasses_canonical_publication"] = True
        p["publication_route"] = "mls_espn_direct"
        # Block 2D Closure §5 (2026-08) — route through the canonical
        # publication barrier.  Picks that fail (missing real odds /
        # Lock < 85 / etc.) are STORED (shadow) but marked
        # off_board=True + no_bet=True so they cannot surface on
        # user-visible boards without meeting the same gate as
        # canonically-generated picks.
        apply_canonical_barrier(p)
        ops.append(ReplaceOne({"id": p["id"]}, p, upsert=True))
    if ops:
        try:
            await db.picks.bulk_write(ops, ordered=False)
            try:
                from services.pipeline_diagnostic import log_reason as _plog
                from services.pipeline_diagnostic import ReasonCode as _RC
                _plog(
                    sport="Soccer", market="scorer_direct_inject",
                    reason=_RC.NON_CANONICAL_WRITE.value,
                    meta={"writer": "mls_direct_inject",
                          "count": len(ops)},
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning("MLS direct upsert error (best-effort): %s", e)

    # ── Phase 1b — route side-injector through publication service ──
    # Every prediction that becomes user-visible must have an
    # immutable snapshot.  MLS direct-inject writes directly to
    # `picks` (bypassing the canonical `_refresh_picks` tail), so we
    # publish here at the tail of this injector's write path.
    try:
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        publisher = PredictionPublicationService(db)
        try:
            await publisher.ensure_indices()
        except Exception:
            pass
        pub_summary = await publisher.publish_batch(
            all_picks, publication_source="mls_direct_inject",
            dual_write=True,
        )
        logger.info("MLS direct-inject publication: new=%d existing=%d "
                    "errors=%d mismatches=%d",
                    pub_summary.get("new_snapshots", 0),
                    pub_summary.get("existing_snapshots", 0),
                    len(pub_summary.get("errors", []) or []),
                    pub_summary.get("mismatches_logged", 0))
        # ── Production-Truth OBSERVE hook (direct-inject origin) ──
        try:
            from services.production_truth.publication_observer import (
                observe_publication,
            )
            await observe_publication(
                db, all_picks,
                publication_source="mls_direct_inject",
                caller_label="mls_direct_inject",
            )
        except Exception as _obs_err:            # pragma: no cover
            logger.debug(
                "MLS direct-inject production_truth observer failed "
                "(non-fatal): %s", _obs_err,
            )
    except Exception as e:
        logger.warning("MLS direct-inject publication step failed "
                        "(non-fatal): %s", e)

    logger.info(
        "MLS direct-inject: wrote %d picks across %d events (pick_date=%s)",
        len(all_picks), len(events), today_str,
    )
    return {"ok": True, "generated": len(all_picks),
            "events": len(events), "pick_date": today_str}


async def loop() -> None:
    """Fire-and-forget refresh loop — runs every 15 min."""
    await asyncio.sleep(30)   # let ESPN stats hydrate first
    while True:
        try:
            summary = await run_once()
            logger.info("MLS direct-inject cycle: %s", summary)
        except Exception as e:
            logger.warning("MLS direct-inject failed: %s", e)
        await asyncio.sleep(15 * 60)   # 15 min
