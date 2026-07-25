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


def _team_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    for x in (a, b):
        pass
    a = a.lower()
    b = b.lower()
    for suf in (" fc", " f.c.", " sc", " cf", " united", " city",
                " football club"):
        a = a.replace(suf, "")
        b = b.replace(suf, "")
    a = a.strip(); b = b.strip()
    return a == b or a in b or b in a


def _american(r: float) -> int:
    """Rate → juiced American odds, clamped to valid range."""
    r = max(0.05, min(0.95, r))   # clamp to safe range (avoids div-by-zero)
    if r >= 0.5:
        fair = int(round(-100.0 * r / (1.0 - r)))
        juiced = int(fair * 0.92)
        if -100 < juiced <= 0:
            juiced = -105
        return max(min(juiced, -100), -800)
    fair = int(round(100.0 * (1.0 - r) / r))
    juiced = int(fair * 1.08)
    if 0 <= juiced < 100:
        juiced = 105
    return min(max(juiced, 100), 1500)


async def _fetch_mls_events(cx: httpx.AsyncClient) -> list[dict]:
    key = os.getenv("THE_ODDS_API_KEY", "")
    if not key:
        return []
    try:
        r = await cx.get(
            "https://api.the-odds-api.com/v4/sports/soccer_usa_mls/events",
            params={"apiKey": key}, timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
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
            book_odds = _american(p)

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
            is_v3 = (route.market == "anytime_goal_scorer")
            edge_val = None if is_v3 else 4.0

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
                "win_probability": p,
                "book_odds": book_odds,
                "book_implied_prob": round(p / 1.08, 4),
                "lock_score": lock,
                "lock_score_v2": lock,
                "lock_score_v2_raw": lock,
                "lock_score_peak": lock,
                "edge_percent": edge_val,
                "odds_source": "model_derived",
                "odds_status": "no_book_line",
                "confidence_penalty": -5 if is_v3 else 0,
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
    from pymongo import ReplaceOne
    ops = []
    for p in all_picks:
        p["created_at"] = now
        p["pick_date"] = today_str
        p["updated_at"] = now
        ops.append(ReplaceOne({"id": p["id"]}, p, upsert=True))
    if ops:
        try:
            await db.picks.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.warning("MLS direct upsert error (best-effort): %s", e)
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
