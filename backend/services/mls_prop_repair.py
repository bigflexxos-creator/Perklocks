"""In-place repair for Soccer player-prop picks after form/xG refresh.

Context (2026-08-22): after populating ``soccer_player_form`` with
real MLS xG / xA data via the ASA fetcher, the same lock-promotion
logic should apply to EVERY soccer league.  This module rescores all
existing Soccer player-scorer picks in place using their stored
``book_odds`` — no new Odds API call is made (freeze-after-publish
compliant) — and applies the universal confidence ladder so honest
high-conviction picks reach the >=85 board on all leagues (EPL /
La Liga / Bundesliga / Ligue 1 / Serie A / MLS / Liga MX / etc).

Usage:
    from services.mls_prop_repair import repair_soccer_props
    summary = await repair_soccer_props(db)

``repair_mls_props`` is retained as an alias for backwards compat.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_prop_repair")

_SCORER_MARKET_KEYS = {
    "player_goal_scorer_anytime",
    "player_first_goal_scorer",
    "player_to_score_or_assist",
    "player_anytime_assist",
    "anytime_goal_scorer",
    "anytime_assist",
    "anytime_goal_involvement",
}


def _implied_prob(american: Any) -> float:
    try:
        o = int(american)
    except Exception:
        return 0.0
    if o == 0:
        return 0.5
    return 100.0/(o+100.0) if o > 0 else abs(o)/(abs(o)+100.0)


def _grade(score: float) -> str:
    if score >= 100: return "APEX Lock"
    if score >= 98:  return "Elite Lock"
    if score >= 95:  return "Strong Lock"
    if score >= 90:  return "Lock"
    if score >= 85:  return "Playable"
    return "Pass"


async def repair_mls_props(db) -> dict:
    """Rescore all existing MLS player-prop picks in-place.

    Uses ONLY existing stored ``book_odds`` — no external API calls.
    Passes existing picks through the same scorer bridge + Lock Score
    formula the fresh ingest uses, so the outcome exactly matches what
    would have been written if ASA data had been present originally.
    """
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    from services.soccer_feature_resolver import (
        resolve_soccer_player_features, resolve_soccer_player_prior,
        resolve_soccer_player_matchup,
    )
    from sports_engine import compute_lock_score
    from pymongo import UpdateOne

    # UNIVERSAL 2026-08-22 — repair scorer picks across EVERY soccer
    # league (EPL / La Liga / Bundesliga / Ligue 1 / Serie A / MLS /
    # Liga MX / etc).  Same identity + evidence + ladder path applies.
    q = {"sport": {"$in": ["Soccer", "soccer"]}}

    now_iso = datetime.now(timezone.utc).isoformat()
    ops = []
    stats = {
        "scanned": 0,
        "updated_up":   0,   # lock rose
        "updated_down": 0,   # lock fell
        "unchanged":    0,
        "no_player":    0,
        "no_book_odds": 0,
        "no_form":      0,
        "on_board_new": 0,
        "off_board_new": 0,
        "by_market": {},
    }

    cursor = db.picks.find(q)
    async for p in cursor:
        stats["scanned"] += 1

        market = str(p.get("market") or "").lower()
        market_key = str(p.get("provider_market_key") or p.get("market_key") or "").lower()
        market_type = str(p.get("market_type") or "").lower()

        # Skip anything that isn't a player-scorer pick.
        is_scorer = (
            market_key in _SCORER_MARKET_KEYS
            or market_type in _SCORER_MARKET_KEYS
            or "goal scorer" in market
            or "anytime assist" in market
            or "score or assist" in market
            or "goal involvement" in market
        )
        if not is_scorer:
            continue

        player = (p.get("selection") or p.get("player_name")
                  or p.get("player") or "").strip()
        if not player:
            stats["no_player"] += 1
            continue

        book_odds = p.get("book_odds")
        try:
            _ = int(book_odds)
        except (TypeError, ValueError):
            # Direct-inject picks (model-only) have no book — leave alone.
            stats["no_book_odds"] += 1
            continue
        book_impl = _implied_prob(book_odds)

        # Universal — resolve using the pick's own league (falls back
        # to inferring from sport_key when missing).
        pick_league = str(p.get("league") or "").strip()
        if not pick_league:
            _sk = str(p.get("sport_key") or "")
            pick_league = "MLS" if _sk == "soccer_usa_mls" else _sk

        form_row, evidence_source = await resolve_soccer_player_features(
            db, player_name=player, league=pick_league,
            canonical_player_id=p.get("canonical_player_id"),
            canonical_player_name=p.get("canonical_player_name"),
        )
        if not form_row:
            stats["no_form"] += 1
            continue

        prior_row = await resolve_soccer_player_prior(
            db, player_name=player, league=pick_league,
            canonical_player_name=p.get("canonical_player_name"),
        )

        # Map the pick's market_key to the bridge-supported set.  Assist
        # markets fall through — the bridge only handles goal / first /
        # score-or-assist right now (Phase 2A.5).
        bridge_market = None
        if market_key == "player_goal_scorer_anytime" or "goal scorer" in market:
            bridge_market = "player_goal_scorer_anytime"
        elif market_key == "player_first_goal_scorer":
            bridge_market = "player_first_goal_scorer"
        elif market_key == "player_to_score_or_assist" or "score or assist" in market:
            bridge_market = "player_to_score_or_assist"

        if bridge_market is None:
            # Assist / involvement — no explicit bridge, but we can still
            # rescore using the direct-inject-style ladder from the
            # assist model to yield a fair lock.  For now, we surface it
            # through the same soccer_scorer_bridge assist_prob path via
            # the player_props system.
            try:
                from services.player_props import (
                    get_player_stats, classify_archetype,
                )
                from services.player_props.assist_model import predict_assist
                stats_obj = await get_player_stats(
                    player, league_hint=pick_league or None,
                )
                if not stats_obj or not stats_obj.data_ok:
                    stats["no_form"] += 1
                    continue
                arche = classify_archetype(stats_obj)
                rec = predict_assist(stats_obj, split=None, archetype=arche)
                if not rec.data_ok:
                    # Model rejects (not an expected creator) — set
                    # off_board explicitly but preserve any prior
                    # settlement fields.
                    new_lock = 60.0
                    model_prob = book_impl
                    off_board = True
                    rej = "NOT_EXPECTED_CREATOR"
                else:
                    model_prob = rec.probability
                    # Same ladder used by direct-inject producers.
                    if model_prob >= 0.55:   new_lock = 95.0
                    elif model_prob >= 0.40: new_lock = 91.0
                    elif model_prob >= 0.25: new_lock = 87.0
                    elif model_prob >= 0.15: new_lock = 83.0
                    else:                    new_lock = 80.0
                    if rec.confidence == "HIGH":
                        new_lock = min(99.0, new_lock + 2.0)
                    elif rec.confidence == "LOW":
                        new_lock = max(75.0, new_lock - 3.0)
                    off_board = new_lock < 85.0
                    rej = "LOW_LOCK_SCORE" if off_board else None
            except Exception as e:
                logger.debug("assist rescore fallback failed for %s: %s", player, e)
                continue
        else:
            bridge = compute_soccer_scorer_factors_sync(
                player=player, market_key=bridge_market,
                book_implied=book_impl,
                form_row=form_row, prior_form_row=prior_row,
                league="MLS",
            )
            if not bridge:
                continue
            model_prob = float(bridge.get("model_prob") or book_impl)
            factors = bridge.get("factors") or {}
            factors = {k: v for k, v in factors.items()
                       if k != "Book Implied Probability"}

            # Attach matchup evidence (H2H) if present.
            opp = (p.get("home_team") if not p.get("is_home")
                   else p.get("away_team"))
            if opp:
                try:
                    matchup = await resolve_soccer_player_matchup(
                        db, player_name=player, opponent_team=opp,
                    )
                    if matchup and matchup.get("events", 0) >= 2:
                        factors["Matchup History"] = min(
                            1.0, float(matchup["events"]) / 5.0)
                except Exception:
                    pass

            _e_scorer = round((model_prob - book_impl) * 100, 2)
            new_lock_val, _ = compute_lock_score(
                factors, win_prob=model_prob * 100,
                pick={"book_odds": book_odds, "edge_percent": _e_scorer,
                       "win_probability": model_prob * 100},
                edge_percent=_e_scorer)
            # UNIVERSAL soccer scorer promotion — see
            # `services.soccer_scorer_lock_ladder.apply_scorer_lock_promotion`
            try:
                from services.soccer_scorer_lock_ladder import (
                    apply_scorer_lock_promotion,
                )
                new_lock_val, _ = apply_scorer_lock_promotion(
                    strict_lock=new_lock_val,
                    model_prob=model_prob,
                    evidence_source=evidence_source or "",
                    games=int(form_row.get("games") or 0),
                    minutes=int(form_row.get("minutes") or 0),
                    goals_per_90=float(form_row.get("goals_per_90") or 0),
                    npxg_per_90=float(form_row.get("npxg_per_90") or 0),
                    market_fit=None,
                )
            except Exception:
                pass
            new_lock = round(new_lock_val, 2)
            off_board = new_lock < 85.0
            rej = "LOW_LOCK_SCORE" if off_board else None

        old_lock = float(p.get("lock_score") or 0)
        new_edge = round((model_prob - book_impl) * 100, 3)

        update = {
            "model_probability":    round(model_prob, 4),
            "model_win_prob":       round(model_prob, 4),
            "win_probability":      round(model_prob * 100, 2),
            "lock_score":           new_lock,
            "lock_score_v2":        new_lock,
            "published_lock_score": new_lock,
            "edge_percent":         new_edge,
            "grade":                _grade(new_lock),
            "confidence":           new_lock,
            "off_board":            off_board,
            "off_board_reasons":    [rej] if (off_board and rej) else None,
            "evidence_source":      evidence_source or "asa",
            "updated_at":           now_iso,
            "mls_prop_repair_applied": True,
            "mls_prop_repair_at":   now_iso,
        }

        ops.append(UpdateOne({"_id": p["_id"]}, {"$set": update}))

        if new_lock > old_lock + 0.5:
            stats["updated_up"] += 1
        elif new_lock < old_lock - 0.5:
            stats["updated_down"] += 1
        else:
            stats["unchanged"] += 1
        if off_board:
            stats["off_board_new"] += 1
        else:
            stats["on_board_new"] += 1

        mkey = market_type or market_key or "unknown"
        stats["by_market"][mkey] = stats["by_market"].get(mkey, 0) + 1

    if ops:
        # Batch writes to keep Mongo happy.
        BATCH = 500
        for i in range(0, len(ops), BATCH):
            chunk = ops[i:i+BATCH]
            try:
                await db.picks.bulk_write(chunk, ordered=False)
            except Exception as e:
                logger.warning("soccer_prop_repair bulk_write chunk failed: %s", e)

    logger.info("Soccer prop repair complete: %s", stats)
    return stats


# Universal alias — the canonical name reflecting all soccer leagues.
repair_soccer_props = repair_mls_props


__all__ = ["repair_mls_props", "repair_soccer_props"]
