"""Generalized Soccer Player Prop Injector (Big-5 European leagues).

Extends the same Player Prop Intelligence approach as
`mls_direct_inject.py` to EPL / La Liga / Serie A / Bundesliga /
Ligue 1 by sourcing candidates from `soccer_player_form` (Understat).

Sport keys handled:
   soccer_epl                    (English Premier League)
   soccer_spain_la_liga
   soccer_italy_serie_a
   soccer_germany_bundesliga
   soccer_france_ligue_one
   soccer_uefa_champs_league     (players from Big-5 clubs)

Runs every 15 min as a background task from server startup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

from services.player_props import (
    Archetype,
    build_matchup_context,
    classify_archetype,
    get_matchup_split,
    get_player_stats,
    select_markets_v3,
)


def _derive_pick_date(event_time: Optional[str], today_str: str) -> str:
    """Return the pick_date that should be stamped for a given event.

    Games starting today or overnight (within ~24h) → today's date.
    Games further out → the calendar date of `event_time` in UTC.

    Fixes user report (2026-07-26): UEFA/Big-5 injectors were tagging
    today's `pick_date` on games 4-5 days out, bloating /picks/today
    and forcing the mobile app into a 5-10s timeout.
    """
    if not event_time:
        return today_str
    try:
        # event_time is ISO-8601 like "2026-07-30T17:00Z"
        s = str(event_time).replace("Z", "+00:00")
        et = datetime.fromisoformat(s)
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_hours = (et - now).total_seconds() / 3600.0
        if delta_hours <= 24:
            return today_str
        return et.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return today_str

logger = logging.getLogger("lockscore.soccer_prop_inject")


# ─────────── League config ───────────
# sport_key → Understat league label in `soccer_player_form`
_SPORT_TO_LEAGUE = {
    "soccer_epl":                    "EPL",
    "soccer_spain_la_liga":          "La_liga",
    "soccer_italy_serie_a":          "Serie_A",
    "soccer_germany_bundesliga":     "Bundesliga",
    "soccer_france_ligue_one":       "Ligue_1",
    # UCL players come from all Big-5 — search all leagues.
    "soccer_uefa_champs_league":     None,
}


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _team_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a = a.lower(); b = b.lower()
    for suf in (" fc", " f.c.", " sc", " cf", " united", " city",
                " football club"):
        a = a.replace(suf, ""); b = b.replace(suf, "")
    a = a.strip(); b = b.strip()
    return a == b or a in b or b in a


def _american(r: float) -> int:
    """DEPRECATED — Session A (2026-06) synthetic-odds purge.

    Retained ONLY as a stub that raises so any accidental re-use is
    caught in CI.  A pick without a real sportsbook line MUST publish
    with book_odds=None + no_real_book_line=True + odds_source='MODEL_ONLY'.
    """
    raise NotImplementedError(
        "_american is purged by Session A — do not synthesize sportsbook "
        "American odds from model probability.  Emit book_odds=None + "
        "no_real_book_line=True + odds_source='MODEL_ONLY' instead.",
    )


async def _fetch_events(cx: httpx.AsyncClient, sport_key: str) -> list[dict]:
    key = os.getenv("THE_ODDS_API_KEY", "")
    if not key:
        return []
    try:
        from services.odds_cache import cached_httpx_get
        data = await cached_httpx_get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/events",
            {},
            api_key=key,
            endpoint_type="events_list",
            caller="soccer_prop_inject._fetch_events",
            sport_key=sport_key,
            skip_completed=True,
            timeout=10,
        )
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Events fetch %s failed: %s", sport_key, e)
        return []


async def _fetch_scorers_for_league(league_hint: Optional[str]
                                     ) -> list[dict]:
    """Return top attacking players from `soccer_player_form`.

    If `league_hint` is None (UCL), pull all Big-5 top-90 combined.
    """
    from deps import db
    query = {"league": league_hint} if league_hint else {
        "league": {"$in": ["EPL", "La_liga", "Serie_A",
                            "Bundesliga", "Ligue_1"]}
    }
    # Prioritize elite attackers: at least 5 goals OR 5 assists this
    # season, in the current season doc.
    query["$or"] = [{"goals": {"$gte": 5}}, {"assists": {"$gte": 5}}]
    docs = await db.soccer_player_form.find(query).sort(
        [("goals", -1)]
    ).to_list(length=400)
    return docs


async def _generate_for_event(ev: dict, sport_key: str,
                               league_hint: Optional[str],
                               all_scorers: list[dict]) -> list[dict]:
    home = (ev.get("home_team") or "").strip()
    away = (ev.get("away_team") or "").strip()
    if not home or not away:
        return []

    # Bucket scorers to teams by (best-effort) name match. Understat
    # stores team as e.g. "Manchester City", oddsapi as "Manchester City".
    home_players: list[dict] = []
    away_players: list[dict] = []
    for r in all_scorers:
        team = r.get("team") or ""
        name = r.get("player_name") or ""
        if not name:
            continue
        if _team_match(team, home):
            home_players.append({"name": name, "team": team})
        elif _team_match(team, away):
            away_players.append({"name": name, "team": team})

    # Rank by combined output (goals + assists per game) for pick priority.
    def _rank_key(r: dict) -> float:
        s = next((s for s in all_scorers
                  if (s.get("player_name") or "") == r["name"]
                  and (s.get("team") or "") == r["team"]), None)
        if not s:
            return 0.0
        g = int(s.get("goals") or 0)
        a = int(s.get("assists") or 0)
        games = int(s.get("games") or 0) or 1
        return (g + a) / games

    home_players.sort(key=_rank_key, reverse=True)
    away_players.sort(key=_rank_key, reverse=True)

    picks: list[dict] = []
    commence = ev.get("commence_time") or ""
    event_id = ev.get("id") or f"{sport_key}-{home}-{away}"

    async def _emit_for(entry: dict, opp: str, is_home: bool) -> list[dict]:
        from deps import db
        name = entry["name"]
        stats = await get_player_stats(name, league_hint=league_hint)
        if not stats or not stats.data_ok:
            return []

        archetype = classify_archetype(stats)
        if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
            return []

        split = await get_matchup_split(name, opp)  # None for non-MLS

        # Build matchup context. We don't have per-team defense stats
        # for Big-5 yet — home/away + form remains the main signals.
        matchup_ctx = build_matchup_context(
            stats, opp,
            is_home=is_home,
            event_commence=commence,
            last_match_iso=None,     # would come from schedule feed (future)
            split=split,
        )

        routes = await select_markets_v3(
            db, stats, archetype, split, matchup_ctx,
            opp_team_name=opp,
            sport_key=sport_key,
            is_home=is_home,
            lineup_status="unknown",
        )
        if not routes:
            return []

        out: list[dict] = []
        # ── Data-quality gate (iter-93) — same rule as mls_direct_inject.
        #   Reject picks lacking a real season sample OR any attacking
        #   signal OR flagged data_ok=False. Also drop routes with
        #   market_fit < 40 or LOW+<60 combos (junk picks the user
        #   explicitly asked to purge from the board).
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
            # Session A (2026-06) synthetic-odds purge — no real
            # sportsbook player-prop line source for Big-5 leagues in
            # this pod.  book_odds MUST NOT be computed from p.
            book_odds = None

            if p >= 0.55: lock = 95.0
            elif p >= 0.40: lock = 91.0
            elif p >= 0.25: lock = 87.0
            elif p >= 0.15: lock = 83.0
            else:            lock = 80.0
            if route.confidence == "HIGH": lock = min(99.0, lock + 2.0)
            elif route.confidence == "LOW": lock = max(75.0, lock - 3.0)
            # market_fit adjusts +/- 1
            if route.market_fit >= 90:
                lock = min(99.0, lock + 1.0)
            elif route.market_fit < 40:
                lock = max(75.0, lock - 2.0)

            grade = ("Strong Lock" if lock >= 95 else
                      ("Lock" if lock >= 90 else "Playable"))

            # ── Strict Edge Gate (v3) ──────────────────────────────
            # We DO NOT calculate a "true betting edge" because we have
            # no real sportsbook player-prop lines for Big-5 soccer in
            # this pod.  Session A (2026-06): apply the same rule to
            # EVERY market — no synthetic 4.0% edges anywhere.
            edge_val = None
            odds_source_val = "MODEL_ONLY"

            pick = {
                "id": f"soccer-prop-{route.market}-{event_id}-{name.replace(' ', '_').lower()}",
                "external_id": f"SOCCER-PROP-{route.market}-{event_id}-{name}",
                "sport": "Soccer",
                "league": (league_hint or "UCL").replace("_", " "),
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
                "odds_source": odds_source_val,
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
                "sport_key": sport_key,
                "archetype": archetype.value,
                "archetype_display": archetype.display(),
                "market_fit": route.market_fit,
                "samples": {
                    "goals": stats.goals,
                    "assists": stats.assists,
                    "games": stats.games,
                    "minutes": stats.minutes,
                    "goals_per_90": stats.goals_per_90,
                    "assists_per_90": stats.assists_per_90,
                    "key_passes_per_90": stats.key_passes_per_90,
                    "npxg_per_90": stats.npxg_per_90,
                    "source": stats.source,
                    "league": stats.league,
                },
                "pick_rationale": {
                    "engine": "goal_scorer_v3" if route.market == "anytime_goal_scorer" else "player_prop_intelligence_v2",
                    "engine_version": route.recommendation.debug.get("engine", "player_prop_intelligence_v2"),
                    "summary": (
                        f"{name} ({archetype.display()}): "
                        f"model p={p*100:.0f}% · {stats.goals}G/{stats.assists}A "
                        f"in {stats.games} games. Market fit {route.market_fit}%."
                    ),
                    "evidence": route.recommendation.evidence,
                    "concerns": route.recommendation.concerns,
                    "matchup": {
                        "player": name,
                        "team": stats.team,
                        "opponent": opp,
                        "is_home": is_home,
                    },
                    "recent_form": {
                        "engine": "player_prop_intelligence_v2",
                        "form_score": stats.form_score,
                        "form_label": stats.form_label,
                    },
                    "model_debug": route.recommendation.debug,
                    "market_fit": route.market_fit,
                    # v3-only signals — surface team λ and ensemble
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
            out.append(pick)
        return out

    sem = asyncio.Semaphore(6)
    async def _run(entry: dict, opp: str, is_home: bool):
        async with sem:
            return await _emit_for(entry, opp, is_home)

    tasks = [_run(e, away, True) for e in home_players[:6]]
    tasks += [_run(e, home, False) for e in away_players[:6]]
    for lst in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(lst, list):
            picks.extend(lst)
    return picks


async def run_once() -> dict:
    """One full injection pass across all Big-5 + UCL sport keys."""
    from deps import db
    from pymongo import ReplaceOne

    now = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals: dict[str, int] = {}
    total_picks_written = 0

    async with httpx.AsyncClient(timeout=15) as cx:
        for sport_key, league_hint in _SPORT_TO_LEAGUE.items():
            events = await _fetch_events(cx, sport_key)
            if not events:
                totals[sport_key] = 0
                continue
            scorers = await _fetch_scorers_for_league(league_hint)
            if not scorers:
                totals[sport_key] = 0
                continue

            all_picks: list[dict] = []
            for ev in events:
                ct = ev.get("commence_time") or ""
                try:
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    if dt < datetime.now(timezone.utc):
                        continue
                except Exception:
                    pass
                picks = await _generate_for_event(
                    ev, sport_key, league_hint, scorers,
                )
                all_picks.extend(picks)

            totals[sport_key] = len(all_picks)
            total_picks_written += len(all_picks)

            if not all_picks:
                continue
            ops = []
            for p in all_picks:
                p["created_at"] = now
                # ── pick_date derivation (2026-07-26) ───────────────
                # Set pick_date from the actual event_time when the
                # game is > 24h away. Fixes user report: UEFA/Big-5
                # picks for games 4-5 days out were being tagged with
                # today's pick_date, bloating the /picks/today board.
                p["pick_date"] = _derive_pick_date(p.get("event_time"), today_str)
                p["updated_at"] = now
                # Block 2D C1 (2026-08) — explicit bypass marker.
                # Fixing the underlying bypass (routing through
                # canonical publication + strict>85 gate) is deferred
                # to Block 2E because each direct-inject pick would
                # need soccer feature-engine evidence attached first.
                p["bypasses_canonical_publication"] = True
                p["publication_route"] = "soccer_prop_direct_inject"
                # Block 2D Closure §5 (2026-08) — enforce the canonical
                # publication barrier: real book_odds + Lock>=85 +
                # implied_probability derivable.  Failures survive as
                # shadow rows (off_board=True + no_bet=True) so they
                # never surface on user-visible boards without meeting
                # the same gate as canonically-generated picks.
                try:
                    from services.canonical_publication_barrier import (
                        apply_canonical_barrier,
                    )
                    apply_canonical_barrier(p)
                except Exception:
                    # Barrier must never break the writer — if it errs,
                    # default to off_board=True (conservative).
                    p["off_board"] = True
                    p["no_bet"] = True
                    p["publication_gate"] = "canonical_barrier_error"
                ops.append(ReplaceOne({"id": p["id"]}, p, upsert=True))
            try:
                await db.picks.bulk_write(ops, ordered=False)
                try:
                    from services.pipeline_diagnostic import log_reason as _plog
                    from services.pipeline_diagnostic import ReasonCode as _RC
                    _plog(
                        sport="Soccer", market="prop_direct_inject",
                        reason=_RC.NON_CANONICAL_WRITE.value,
                        meta={"writer": "soccer_prop_inject",
                              "sport_key": sport_key,
                              "count": len(ops)},
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Soccer prop upsert error (%s): %s", sport_key, e)

            # ── Phase 1b — publication service side-injector wiring ──
            # Route this soccer-prop batch through the publication
            # service so an immutable snapshot exists for each pick
            # BEFORE it becomes user-visible on the board.
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
                    all_picks,
                    publication_source="soccer_prop_inject",
                    dual_write=True,
                )
                logger.info(
                    "Soccer Prop Inject %s publication: new=%d existing=%d "
                    "errors=%d mismatches=%d",
                    sport_key,
                    pub_summary.get("new_snapshots", 0),
                    pub_summary.get("existing_snapshots", 0),
                    len(pub_summary.get("errors", []) or []),
                    pub_summary.get("mismatches_logged", 0),
                )
                # ── Production-Truth OBSERVE hook ────────────────
                try:
                    from services.production_truth.publication_observer import (
                        observe_publication,
                    )
                    await observe_publication(
                        db, all_picks,
                        publication_source="soccer_prop_inject",
                        caller_label=f"soccer_prop_inject/{sport_key}",
                    )
                except Exception as _obs_err:    # pragma: no cover
                    logger.debug(
                        "Soccer Prop Inject production_truth observer "
                        "failed (non-fatal): %s", _obs_err,
                    )
            except Exception as e:
                logger.warning("Soccer Prop Inject publication step "
                                "failed (non-fatal): %s", e)

            logger.info(
                "Soccer Prop Inject %s: %d picks across %d events",
                sport_key, len(all_picks), len(events),
            )

    return {"ok": True, "picks_written": total_picks_written,
            "by_sport_key": totals, "pick_date": today_str}


async def loop() -> None:
    """Fire-and-forget refresh loop — runs every 15 min."""
    await asyncio.sleep(45)   # let stats warm up
    while True:
        try:
            summary = await run_once()
            logger.info("Soccer Prop Inject cycle: %s", summary)
        except Exception as e:
            logger.warning("Soccer Prop Inject failed: %s", e)
        await asyncio.sleep(15 * 60)
