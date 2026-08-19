"""SGO → existing sport scorer bridge (Perklocks 6-day trial).

Fixes the proven routing gap: SGO adapter writes canonical real-line
rows into ``db.picks`` with the correct market/routing fields, but
``real_line_scorer_ingest.py`` only scores rows it acquires directly.
This bridge re-uses the EXACT SAME scoring functions
(``_ingest_game_market_row`` / ``_ingest_player_scorer_row``) that the
Odds-API-family path calls, so SGO rows now receive real MLB / Soccer /
Tennis model scores — not self-heal fallback scores.

Contract:
  • No new scoring formulas, no alternate Lock-Score logic.
  • Both providers converge on the same scorer after normalization.
  • Preserves the SGO row's canonical fields; scoring fields are
    $set onto the existing doc by SGO ``id``.
  • Self-heal validator is NOT the source of ``lock_score`` here — the
    sport model is.

Route:
    SGO row (source=sportsgameodds_v2)
        ↓  _sgo_row_to_ingest_shape
    row (live_alt_lines shape expected by _ingest_*_row)
        ↓  _ingest_game_market_row / _ingest_player_scorer_row
    scored doc with lock_score / model_probability / edge_percent
        ↓  update_one({"id": sgo["id"]}, {"$set": scoring_fields})
    SGO doc, now carrying real sport-model score
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.sgo_scorer")

# ── SGO leagueID → Odds-API-family sport_key ────────────────────────
# ``_league_from_sport_key`` in the target module consumes this to
# resolve the display league name. Any missing entry defaults to the
# league ID lower-cased (which will produce ``Unknown`` league — still
# fine, model scoring does not require this to be perfect).
_SGO_TO_ODDS_API_SPORT_KEY = {
    "MLB":         "baseball_mlb",
    "EPL":         "soccer_epl",
    "LALIGA":      "soccer_spain_la_liga",
    "MLS":         "soccer_usa_mls",
    "SERIEA":      "soccer_italy_serie_a",
    "BUNDESLIGA":  "soccer_germany_bundesliga",
    "LIGUE1":      "soccer_france_ligue_one",
    "ATP":         "tennis_atp",
    "WTA":         "tennis_wta",
}


# Fields on the scored doc we copy over to the existing SGO row.
_SCORING_FIELDS = (
    "lock_score", "lock_score_v2", "published_lock_score",
    "model_probability", "model_win_prob", "win_probability",
    "model_source", "edge_percent", "edge_method",
    "grade", "confidence", "status",
    "off_board", "off_board_reasons",
    "evidence_score",
    "implied_probability",
    "canonical_wager_id",
)


def _sgo_row_to_ingest_shape(sgo: dict) -> Optional[dict]:
    """Convert an SGO pick doc into the ``live_alt_lines`` row shape
    consumed by ``_ingest_game_market_row`` / ``_ingest_player_scorer_row``.
    Returns None when required fields are missing."""
    price = sgo.get("book_odds")
    event_id = sgo.get("event_id") or sgo.get("provider_event_id")
    mk = sgo.get("market_key")
    sel = sgo.get("provider_selection") or sgo.get("selection")
    if not (price is not None and event_id and mk and sel):
        return None
    sport_key = _SGO_TO_ODDS_API_SPORT_KEY.get(
        (sgo.get("sport_key") or "").upper()
    ) or (sgo.get("sport_key") or "").lower()
    return {
        "price":         price,
        "event_id":      event_id,
        "market_key":    mk,
        "selection":     sel,
        "line":          sgo.get("line"),
        "home_team":     sgo.get("home_team"),
        "away_team":     sgo.get("away_team"),
        "sportsbook":    sgo.get("bookmaker") or "consensus",
        "odds_api_sport": sport_key,
        "commence_time": sgo.get("event_time"),
        # Player-prop-specific fields (only consulted by the player
        # ingester — ignored by the game-market path).
        "player_name":   sgo.get("player_name"),
        "player_team":   sgo.get("player_team"),
    }


async def score_pending_sgo_rows(db, *, limit: int = 500) -> dict:
    """Score every current SGO row that has no ``lock_score`` yet.

    Uses the EXACT scoring functions the Odds-API-family path invokes
    — no duplicated scoring formulas. Returns per-sport counts.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    today = now_iso[:10]

    # Import at call-time so we pick up any hot-reloaded helper.
    from services.real_line_scorer_ingest import (
        _ingest_game_market_row, _ingest_player_scorer_row,
    )

    q = {
        "source": "sportsgameodds_v2",
        "lock_score": {"$in": [None]},   # scored=None means unscored
        "$or": [
            {"event_time": {"$gte": now_iso}},
            {"event_time": {"$exists": False}},
        ],
    }
    counts = {"scanned": 0, "scored": 0,
              "by_sport": {"MLB": 0, "Soccer": 0, "Tennis": 0},
              "skipped": 0, "errors": 0}
    try:
        cursor = db.picks.find(q).limit(limit)
        async for sgo in cursor:
            counts["scanned"] += 1
            row = _sgo_row_to_ingest_shape(sgo)
            if not row:
                counts["skipped"] += 1
                continue
            sport = sgo.get("sport")
            market_family = sgo.get("market_family")
            try:
                if market_family == "game_market":
                    doc, _rej = await _ingest_game_market_row(
                        db, row, today, now_iso,
                    )
                elif market_family == "player_prop":
                    doc, _rej = await _ingest_player_scorer_row(
                        db, row, today, now_iso,
                    )
                else:
                    counts["skipped"] += 1
                    continue
            except Exception as _e:
                counts["errors"] += 1
                logger.debug("sgo score %s failed: %s", sgo.get("id"), _e)
                continue
            if not doc:
                counts["skipped"] += 1
                continue
            # Merge scoring fields onto the EXISTING SGO doc by its
            # SGO ``id`` — preserves SGO provenance and never
            # duplicates the row under the ingest-writer id.
            update = {k: doc.get(k) for k in _SCORING_FIELDS
                        if k in doc and doc.get(k) is not None}
            # Retain the SGO writer marker + provider tag.
            update["updated_at"] = now_iso
            update["scored_by"]  = "sport_model"   # NOT self-heal

            # ── PERKLOCKS UNIVERSAL WIRING (2026-06) ─────────────────
            # Post-scoring stale-reason recomputation.  When the real
            # sport model has produced a valid ``model_probability``
            # (and therefore a valid ``lock_score``), the row must NOT
            # keep contradictory scoring-rejection reasons on its
            # ``off_board_reasons`` list.  We ONLY strip the two
            # scoring-dependent reasons here; every other legitimate
            # protection (synthetic, no_real_book_line, identity
            # failures, chalk_trap, longshot_trap, validation_block,
            # no_bet, provider_unavailable, TEAM_CONTEXT_UNAVAILABLE)
            # is preserved untouched.
            #
            # If nothing remains after stripping, ``off_board`` is
            # cleared so the canonical publisher can evaluate the row.
            # The 85+ floor and every other visibility gate downstream
            # remain the sole authority.
            model_prob_out = doc.get("model_probability") \
                or doc.get("model_win_prob")
            if isinstance(model_prob_out, (int, float)) and model_prob_out > 0:
                new_reasons = [
                    r for r in (doc.get("off_board_reasons") or [])
                    if r not in ("NO_MODEL_PROBABILITY", "lock<85")
                ]
                # Only rewrite off_board_reasons when we actually
                # stripped a stale scoring reason.
                orig = doc.get("off_board_reasons") or []
                if len(new_reasons) != len(orig):
                    update["off_board_reasons"] = new_reasons
                    # Clear off_board only when NO reasons remain AND
                    # canonical lock qualifies (>=85).  Otherwise leave
                    # the flag intact so downstream board rules run.
                    lock_out = doc.get("lock_score") or 0
                    if not new_reasons and lock_out >= 85:
                        update["off_board"] = False
            try:
                await db.picks.update_one(
                    {"id": sgo.get("id")},
                    {"$set": update},
                )
                counts["scored"] += 1
                if sport in counts["by_sport"]:
                    counts["by_sport"][sport] += 1
            except Exception as _e:
                counts["errors"] += 1
                logger.debug("sgo score upsert %s failed: %s",
                              sgo.get("id"), _e)
    except Exception as _e:
        logger.warning("sgo score pass failed: %s", _e)
    return counts
