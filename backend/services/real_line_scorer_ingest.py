"""Real-line MLS/Soccer scorer ingester — Phase 2A.5E (2026-08).

Wires the already-fetched `live_alt_lines` collection (real sportsbook
odds from The Odds API `event_alt_lines` fetcher) into the authoritative
Soccer candidate pipeline.  Preserves real bookmaker odds — NEVER
substitutes model-derived or synthetic fair-value.

Contract
--------
* Read-only over `live_alt_lines` (owner: alt_lines_feed).
* Write to `picks` with:
    - `source = "real_line_alt_scorer_v1"`
    - `book_odds`  = real sportsbook price
    - `odds_source = "real_book_line"`
    - `no_real_book_line = False`
    - `edge_percent` populated (model - devig-implied)
* Delegates model probability to `services.soccer_scorer_bridge`.
* Missing evidence → `off_board=True` with `MISSING_FEATURE_DATA`.
"""
from __future__ import annotations
import logging, math, uuid
from datetime import datetime, timezone
from typing import Any
logger = logging.getLogger("lockscore.real_line_scorer_ingest")

_SCORER_MARKETS = (
    "player_goal_scorer_anytime",
    "player_first_goal_scorer",
    "player_last_goal_scorer",
    "player_to_score_or_assist",
)
_MARKET_LABEL = {
    "player_goal_scorer_anytime": "Anytime Goal Scorer",
    "player_first_goal_scorer":  "First Goal Scorer",
    "player_last_goal_scorer":   "Last Goal Scorer",
    "player_to_score_or_assist": "To Score or Assist",
}


def _implied_prob(american: int) -> float:
    if american is None: return 0.0
    o = int(american)
    if o == 0: return 0.5
    return 100.0/(o+100.0) if o > 0 else abs(o)/(abs(o)+100.0)


def _grade(score: float) -> str:
    if score >= 100: return "APEX Lock"
    if score >= 98:  return "Elite Lock"
    if score >= 95:  return "Strong Lock"
    if score >= 90:  return "Lock"
    if score >= 85:  return "Playable"
    return "Pass"


async def ingest_real_line_soccer_scorers(db, *, today: str) -> dict[str, int]:
    """One-shot ingestion pass.  Idempotent: existing (event, market,
    selection) rows are updated, not duplicated.  Returns stats."""
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    from services.soccer_historical_stats import aggregate_player_season, load_player_h2h
    from services.soccer_season_resolver import resolve_prior_season
    from sports_engine import compute_lock_score  # reuse canonical composite

    stats = {"scanned": 0, "written": 0, "skipped": 0, "off_board": 0}
    now = datetime.now(timezone.utc)

    cursor = db.live_alt_lines.find({
        "market_key": {"$in": list(_SCORER_MARKETS)},
        # `alt_lines_feed._sport_label` writes lowercase sport labels
        # ("soccer" / "mlb" / "tennis" / ...).  Accept both forms to
        # stay tolerant if the feed ever normalises to Title case.
        "sport": {"$in": ["soccer", "Soccer"]},
    })
    async for row in cursor:
        stats["scanned"] += 1
        try:
            price = int(row.get("price") or 0)
        except Exception:
            price = 0
        if price == 0:
            stats["skipped"] += 1; continue

        event_id = row.get("event_id")
        mk = row.get("market_key")
        player = (row.get("selection") or "").strip()
        home = row.get("home_team"); away = row.get("away_team")
        book = row.get("sportsbook")
        sport_key = row.get("odds_api_sport") or ""
        if not (event_id and mk and player and (home or away)):
            stats["skipped"] += 1; continue

        book_impl = _implied_prob(price)

        # Historical prior season row (may return None; that's fine).
        try:
            league = "MLS" if "mls" in sport_key else sport_key
            prior = await aggregate_player_season(
                db, player_name_canonical=player.lower(),
                season=resolve_prior_season(league))
        except Exception:
            prior = None

        # Current-season form row (may be missing → bridge returns None).
        form = await db.soccer_player_form.find_one(
            {"name_canonical": player.lower()}) if player else None

        bridge = compute_soccer_scorer_factors_sync(
            player=player, market_key=mk, book_implied=book_impl,
            form_row=form, prior_form_row=prior, league=league)
        if not bridge:
            # Missing feature data — write off_board candidate for
            # attribution rather than silent drop.
            model_prob = book_impl  # temp: fair value; won't publish
            factors = {"Book Implied Probability": book_impl}
            reason = "MISSING_FEATURE_DATA"
            off_board = True
            lock, breakdown = compute_lock_score(factors, win_prob=book_impl*100)
        else:
            model_prob = float(bridge.get("model_prob") or book_impl)
            factors = bridge.get("factors") or {}
            reason = None
            lock, breakdown = compute_lock_score(factors, win_prob=model_prob*100)
            off_board = lock < 85.0

        # Canonical edge = model - book_implied (de-vig fallback since we
        # don't have opposing side for de-vig here).
        edge_percent = round((model_prob - book_impl) * 100, 3)

        # Deterministic UUID5 pick id (survives restarts + refreshes).
        # Matches the orchestrator's UUID5 namespace so downstream
        # dedupe / prediction_snapshot joins line up.
        _NS = uuid.UUID("00000000-0000-0000-0000-000000000001")
        _external_id = f"real_line_alt_scorer_v1|{event_id}|{mk}|{player.lower()}"
        pick_id = str(uuid.uuid5(_NS, _external_id))

        # Team inference — for Anytime/First/Last/SoA we cannot always
        # determine team from a single outcome, but for score-or-assist
        # AND player_first_goal_scorer we can try via H2H lookup.
        team_infer = None
        # Provider payload sometimes carries `player_team` — leave as None
        # when unknown (teammate rule safely skips per Phase 2A.5D).

        doc = {
            "id": pick_id,
            "external_id": _external_id,
            "sport": "Soccer",
            "league": league,
            "pick_date": today,
            "event": f"{away} @ {home}" if home and away else (home or away),
            "event_id": event_id,
            "provider_event_id": event_id,
            "market": f"{player} {_MARKET_LABEL.get(mk, mk)}",
            "market_key": mk,
            "selection": player,
            "team": team_infer,
            "book_odds": price,
            "bookmaker": book,
            "odds_source": "real_book_line",
            "odds_status": "book_line_present",
            "no_real_book_line": False,
            "implied_probability": round(book_impl * 100, 3),
            "model_probability": model_prob,
            "model_win_prob": model_prob,
            "edge_percent": edge_percent,
            "edge_method": "RAW_FALLBACK",
            "lock_score": round(lock, 2),
            "lock_score_v2": round(lock, 2),
            "published_lock_score": round(lock, 2),
            "grade": _grade(lock),
            "confidence": lock,
            "status": "pending",
            "no_bet": False,
            "off_board": off_board,
            "off_board_reasons": [reason] if (off_board and reason) else None,
            "source": "real_line_alt_scorer_v1",
            "publication_source": "real_line_alt_scorer_v1",
            "commence_time": row.get("commence_time"),
            "updated_at": now.isoformat(),
        }
        if off_board: stats["off_board"] += 1

        # Idempotent upsert keyed on deterministic pick id.  Falls
        # back on the (event, market, selection) composite lookup for
        # any legacy rows that predate the UUID5 change.
        await db.picks.update_one(
            {"$or": [
                {"id": pick_id},
                {"event_id": event_id, "market_key": mk,
                 "selection": player, "pick_date": today,
                 "source": "real_line_alt_scorer_v1"},
            ]},
            {"$set": doc}, upsert=True,
        )
        stats["written"] += 1
    return stats


__all__ = ["ingest_real_line_soccer_scorers"]
