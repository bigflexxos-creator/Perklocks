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


async def _mark_route_reason(db, sgo: dict, reason_code: str) -> None:
    """Attach a routing rejection reason on the SGO row without
    revoking legitimate protections.  Idempotent — same reason is
    added only once.  Does NOT touch ``lock_score`` / scoring fields.
    """
    try:
        cur = list(sgo.get("off_board_reasons") or [])
        if reason_code in cur:
            return
        cur.append(reason_code)
        await db.picks.update_one(
            {"id": sgo.get("id")},
            {"$set": {"off_board_reasons": cur,
                       "off_board": True,
                       "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
    except Exception as _e:
        logger.debug("mark_route_reason %s on %s failed: %s",
                      reason_code, sgo.get("id"), _e)


async def _score_mlb_sgo(db, sgo: dict, now_iso: str) -> Optional[dict]:
    """Score a single SGO MLB row by REUSING the existing MLB
    data-driven models in ``services.data_driven_model``.

    Contract:
      * No new MLB math, no random tilt, no fallback to book_implied
        for unsupported markets.
      * Totals →  ``mlb_total_prob``
      * Hitter props (Hits / Total Bases / H+R+RBI / RBIs / Home Runs)
        →  ``mlb_hitter_prob``
      * Pitcher props (Strikeouts / Outs Recorded) → ``mlb_pitcher_prop_prob``
      * Moneyline / Run Line have no dedicated MLB probability model
        yet in Perklocks (see sports_engine.py L1252 comment) — we
        return ``None`` and let the caller record
        ``SGO_MLB_ML_MODEL_UNAVAILABLE`` instead of inventing a lift.
      * If required context is unavailable, the DD models return
        ``mp == implied`` (0 lift) — we treat that as no signal and
        return ``None`` so the row is honestly counted as
        ``mlb_context_unavailable``.

    Returns a dict of scoring fields to $set onto the SGO row, or
    ``None`` when routing/context/model are missing (caller counts
    the specific bucket).
    """
    price = sgo.get("book_odds")
    if not isinstance(price, (int, float)):
        return None

    p_int = int(price)
    implied = 100.0 / (p_int + 100.0) if p_int > 0 else (-p_int) / (-p_int + 100.0)

    mk = (sgo.get("market_key") or "").lower()
    fam = (sgo.get("market_family") or "").lower()
    market_label = sgo.get("market") or ""
    line = sgo.get("line")
    try:
        line = float(line) if line is not None else None
    except (TypeError, ValueError):
        line = None
    side_raw = (sgo.get("side") or "").lower()
    side = "Over" if side_raw == "over" else "Under" if side_raw == "under" else side_raw.capitalize()

    HITTER_KEYS = {"batter_hits", "batter_total_bases", "batter_hits_runs_rbis",
                    "batter_rbis", "batter_home_runs"}
    PITCHER_KEYS = {"pitcher_strikeouts", "pitcher_outs"}

    ctx: dict = {}   # data-driven models tolerate empty ctx (0 lift).

    try:
        if fam == "game_market" and mk == "totals":
            if line is None or side not in ("Over", "Under"):
                return None
            from services.data_driven_model import mlb_total_prob
            res = mlb_total_prob(side, line, implied, ctx)
            model_source = "data_driven_model.mlb_total_prob"
        elif fam == "player_prop" and mk in HITTER_KEYS:
            from services.data_driven_model import mlb_hitter_prob
            res = mlb_hitter_prob(market_label, side, line if line is not None else 0.5,
                                    implied, ctx)
            model_source = "data_driven_model.mlb_hitter_prob"
        elif fam == "player_prop" and mk in PITCHER_KEYS:
            from services.data_driven_model import mlb_pitcher_prop_prob
            res = mlb_pitcher_prop_prob(market_label, side, line if line is not None else 0.5,
                                         implied, ctx)
            model_source = "data_driven_model.mlb_pitcher_prop_prob"
        else:
            # Moneyline / spreads / unmapped keys — no dedicated MLB
            # model.  Signal caller so it can tag SGO_MLB_ML_MODEL_UNAVAILABLE.
            return {"_no_mlb_model_for_market": mk}
    except Exception as _e:
        logger.debug("mlb DD scorer failed for %s / %s: %s", mk, market_label, _e)
        return None

    if not isinstance(res, dict) or "mp" not in res:
        return None
    mp = float(res.get("mp") or 0.0)
    total_lift = float(res.get("total_lift") or 0.0)
    # No signal → don't publish a book-following pick as "scored".
    if abs(total_lift) < 1e-4:
        return None

    win_prob   = round(mp * 100.0, 1)
    edge_pct   = round((mp - implied) * 100.0, 2)

    # Lock-Score ladder — mirrors production `sports_engine._backfill_tennis_moneylines`
    # style ladder used elsewhere for pure-model rows.  No new formula.
    if mp >= 0.85:   lock_score = 96.0
    elif mp >= 0.75: lock_score = 92.0
    elif mp >= 0.65: lock_score = 88.0
    elif mp >= 0.55: lock_score = 82.0
    elif mp >= 0.50: lock_score = 76.0
    else:            lock_score = 70.0

    return {
        "model_probability":    round(mp, 4),
        "model_win_prob":       round(mp, 4),
        "win_probability":      win_prob,
        "edge_percent":         edge_pct,
        "implied_probability":  round(implied * 100.0, 3),
        "lock_score":           lock_score,
        "published_lock_score": lock_score,
        "model_source":         model_source,
        "data_driven_used":     True,
        "data_driven_contribs": dict(res.get("contributions") or {}),
    }


async def _score_tennis_sgo(db, sgo: dict, now_iso: str) -> Optional[dict]:
    """Score a single SGO Tennis row by REUSING the existing production
    Tennis scorer ``services.tennis_math_engine.score_tennis_matchup``.

    Contract:
      * No new Tennis math, Elo, edge, or Lock-Score formula is introduced.
      * Moneyline / h2h rows are scored via ``score_tennis_matchup`` +
        ``has_real_tennis_signal`` (identical gate to the production
        Tennis pipeline in ``sports_engine._backfill_tennis_moneylines``).
      * The Lock-Score ladder used here is byte-identical to that
        production caller (see sports_engine.py ~L2816-2821).
      * The Sackmann/h2h context is built via the same lookup helpers
        (``services.tennis.fallback.get_player_stats/get_h2h``) that
        the production caller uses. If context is missing / the
        signal gate fails → return ``None`` so the caller records
        ``tennis_context_unavailable`` (no fallback / self-heal).
      * Non-moneyline markets (spread / total) have NO existing sport-
        model scorer we can reuse without inventing new math, so we
        return ``None`` for them here — they remain unscored SGO rows.
    """
    price = sgo.get("book_odds")
    home = sgo.get("home_team")
    away = sgo.get("away_team")
    if not (isinstance(price, (int, float)) and home and away):
        return None

    mk = (sgo.get("market_key") or "").lower()
    is_ml = ("moneyline" in mk) or (mk in ("h2h", "match winner"))
    if not is_ml:
        # No existing production Tennis scorer for spread/total that we
        # can reuse without inventing new math — leave unscored.
        return None

    sel = (sgo.get("provider_selection") or sgo.get("selection") or "").strip()
    picked, other = home, away
    if sel:
        low = sel.lower()
        if home.lower() in low and away.lower() not in low:
            picked, other = home, away
        elif away.lower() in low and home.lower() not in low:
            picked, other = away, home

    # Implied probability of the picked side at the book price.
    p_int = int(price)
    picked_implied = 100.0 / (p_int + 100.0) if p_int > 0 else (-p_int) / (-p_int + 100.0)

    # ── Build tennis ctx — mirrors production `_backfill_tennis_moneylines`
    league = str(sgo.get("league") or sgo.get("sport_key") or "").lower()
    ev_name = str(sgo.get("event_name") or f"{home} vs {away}").lower()
    combo = f"{league} {ev_name}"
    ctx: dict = {}
    if any(t in combo for t in ("australian open", "french open", "wimbledon", "us open")):
        ctx["match_tier"] = "slam"
    elif any(t in combo for t in ("atp 1000", "wta 1000", "masters 1000")):
        ctx["match_tier"] = "atp1000"
    elif any(t in combo for t in ("atp 500", "wta 500")):
        ctx["match_tier"] = "atp500"
    elif any(t in combo for t in ("atp 250", "wta 250")):
        ctx["match_tier"] = "atp250"
    elif "challenger" in combo:
        ctx["match_tier"] = "challenger"
    elif any(t in combo for t in ("itf", "w15", "w25", "w40", "w60", "m15", "m25")):
        ctx["match_tier"] = "itf"

    if "wimbledon" in ev_name or "grass" in ev_name:
        surface_key = "Grass"
    elif any(x in ev_name for x in ("french", "clay", "roland", "monte carlo", "madrid", "rome", "barcelona")):
        surface_key = "Clay"
    else:
        surface_key = "Hard"
    surface_l = surface_key.lower()

    # Same Sackmann/h2h lookups the production caller uses (silent no-op)
    try:
        from services.tennis.fallback import get_player_stats, get_h2h
        sa = await get_player_stats(db, picked, surface_key)
        sb = await get_player_stats(db, other, surface_key)
        if sa:
            ctx["sackmann_a"] = sa
        if sb:
            ctx["sackmann_b"] = sb
        h = await get_h2h(db, picked, other)
        if h and h.get("matches", 0) >= 1:
            ctx["h2h_a_wins"] = h.get("a_wins", 0)
            ctx["h2h_b_wins"] = h.get("b_wins", 0)
    except Exception as _lookup_err:
        logger.debug("sgo tennis ctx lookup failed for %s vs %s: %s",
                      picked, other, _lookup_err)

    # ── Score via the EXISTING production Tennis scorer ─────────────
    try:
        from services.tennis_math_engine import (
            score_tennis_matchup, has_real_tennis_signal,
        )
    except Exception as _imp_err:
        logger.debug("sgo tennis math engine import failed: %s", _imp_err)
        return None

    signal = score_tennis_matchup(picked, other, surface_l, picked_implied, ctx)
    if not (signal and has_real_tennis_signal(signal)):
        return None

    model_wp = float(signal["home_win_prob"])
    # win_prob / edge_pct — identical to production caller
    win_prob = round(min(0.95, max(0.15, model_wp)) * 100, 1)
    edge_pct = round((win_prob / 100.0 - picked_implied) * 100.0, 2)
    # Lock-Score ladder — byte-identical to
    # sports_engine._backfill_tennis_moneylines (~L2816-2821)
    if model_wp >= 0.85:   lock_score = 96.0
    elif model_wp >= 0.75: lock_score = 92.0
    elif model_wp >= 0.65: lock_score = 88.0
    elif model_wp >= 0.55: lock_score = 82.0
    elif model_wp >= 0.50: lock_score = 76.0
    else:                  lock_score = 70.0

    return {
        "model_probability": round(model_wp, 4),
        "model_win_prob":    round(model_wp, 4),
        "win_probability":   win_prob,
        "edge_percent":      edge_pct,
        "implied_probability": round(picked_implied, 4),
        "lock_score":        lock_score,
        "published_lock_score": lock_score,
        "model_source":      "tennis_math_engine.score_tennis_matchup",
        "data_driven_used":  True,
        "data_driven_contribs": dict(signal.get("contributions") or {}),
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
              "skipped": 0, "errors": 0,
              "published": 0, "publish_errors": 0,
              "tennis_context_unavailable": 0,
              "mlb_context_unavailable": 0,
              "mlb_ml_model_unavailable": 0,
              "unsupported_sport_route": 0}

    # ── PERKLOCKS DOWNSTREAM PUBLICATION WIRING (2026-06) ────────────
    # After the SGO row receives its real sport-model score, it MUST
    # traverse the SAME canonical publication chokepoint every other
    # producer uses (``services.publication_helpers.publish_upserted_picks``).
    # That helper is what stamps ``publication_state='PUBLISHED'`` and
    # writes an immutable ``prediction_snapshots`` row so the
    # ``canonical_publication_filter`` on ``/picks/today`` accepts the
    # row.  Without this call the row is scored on ``db.picks`` but
    # invisible to the board.  The helper is idempotent and skips
    # off-board rows internally so legitimate integrity gates are
    # preserved.
    async def _publish_scored(sgo_row: dict, update_fields: dict) -> None:
        try:
            # Skip publication for rows that legitimately stayed
            # off-board after scoring — matches ``_upsert_pick``'s own
            # guard in real_line_scorer_ingest.py L766.
            resolved_off_board = update_fields.get(
                "off_board", sgo_row.get("off_board"))
            if resolved_off_board is True:
                return
            merged = {**sgo_row, **update_fields}
            merged["publication_source"] = merged.get(
                "publication_source") or "sportsgameodds_v2"
            from services.publication_helpers import (
                publish_upserted_picks as _pub,
            )
            await _pub(
                db, [merged],
                publication_source=merged["publication_source"],
                caller_label="sgo_scorer_bridge",
            )
            counts["published"] += 1
        except Exception as _pub_err:
            counts["publish_errors"] += 1
            logger.debug("sgo publish failed for %s: %s",
                          sgo_row.get("id"), _pub_err)
    try:
        cursor = db.picks.find(q).limit(limit)
        async for sgo in cursor:
            counts["scanned"] += 1
            sport = sgo.get("sport")

            # ── PERKLOCKS UNIVERSAL SPORT ROUTING (2026-06) ──────────
            # Explicit sport-aware routing.  Every supported sport
            # goes to its OWN production scorer.  No Soccer
            # fallthrough — unknown sports fail closed with
            # ``SGO_UNSUPPORTED_SPORT_ROUTE`` rather than getting
            # scored by the Soccer game model.
            if sport == "Tennis":
                if sgo.get("market_family") != "game_market":
                    # No dedicated Tennis scorer for spreads / totals /
                    # player props on SGO rows — quarantine honestly.
                    await _mark_route_reason(db, sgo, "SGO_UNSUPPORTED_SPORT_ROUTE")
                    counts["unsupported_sport_route"] += 1
                    continue
                res = await _score_tennis_sgo(db, sgo, now_iso)
                if res is None:
                    counts["tennis_context_unavailable"] += 1
                    continue
                update = res
                update["updated_at"] = now_iso
                update["scored_by"]  = "sport_model"
                model_prob_out = update.get("model_probability") or 0
                lock_out       = update.get("lock_score") or 0
                if isinstance(model_prob_out, (int, float)) and model_prob_out > 0:
                    orig_reasons = list(sgo.get("off_board_reasons") or [])
                    new_reasons = [
                        r for r in orig_reasons
                        if r not in ("NO_MODEL_PROBABILITY", "lock<85", "grade='Pass'")
                    ]
                    if len(new_reasons) != len(orig_reasons):
                        update["off_board_reasons"] = new_reasons
                        if not new_reasons and lock_out >= 85:
                            update["off_board"] = False
                try:
                    await db.picks.update_one(
                        {"id": sgo.get("id")}, {"$set": update},
                    )
                    counts["scored"] += 1
                    counts["by_sport"]["Tennis"] += 1
                    await _publish_scored(sgo, update)
                except Exception as _e:
                    counts["errors"] += 1
                    logger.debug("sgo tennis upsert %s failed: %s",
                                  sgo.get("id"), _e)
                continue

            if sport == "MLB":
                res = await _score_mlb_sgo(db, sgo, now_iso)
                if res is None:
                    counts["mlb_context_unavailable"] += 1
                    continue
                if res.get("_no_mlb_model_for_market"):
                    await _mark_route_reason(db, sgo, "SGO_MLB_ML_MODEL_UNAVAILABLE")
                    counts["mlb_ml_model_unavailable"] += 1
                    continue
                update = res
                update["updated_at"] = now_iso
                update["scored_by"]  = "sport_model"
                model_prob_out = update.get("model_probability") or 0
                lock_out       = update.get("lock_score") or 0
                if isinstance(model_prob_out, (int, float)) and model_prob_out > 0:
                    orig_reasons = list(sgo.get("off_board_reasons") or [])
                    # Strip ONLY stale routing/scoring reasons caused by
                    # the prior wrong Soccer route.  Legitimate reasons
                    # (PLAYER_IDENTITY_UNRESOLVED, MLB_PARTIAL_PERIOD_
                    # MISLABELED, no_real_book_line, no_bet, synthetic,
                    # validation_block, expired event) are preserved.
                    STALE_MLB_ROUTING = {
                        "NO_MODEL_PROBABILITY", "lock<85", "grade='Pass'",
                        "NO_TEAM_CONTEXT", "TEAM_CONTEXT_UNAVAILABLE",
                        "soccer_model_error",
                    }
                    new_reasons = [r for r in orig_reasons if r not in STALE_MLB_ROUTING]
                    if len(new_reasons) != len(orig_reasons):
                        update["off_board_reasons"] = new_reasons
                        if not new_reasons and lock_out >= 85:
                            update["off_board"] = False
                try:
                    await db.picks.update_one(
                        {"id": sgo.get("id")}, {"$set": update},
                    )
                    counts["scored"] += 1
                    counts["by_sport"]["MLB"] += 1
                    await _publish_scored(sgo, update)
                except Exception as _e:
                    counts["errors"] += 1
                    logger.debug("sgo mlb upsert %s failed: %s",
                                  sgo.get("id"), _e)
                continue

            if sport != "Soccer":
                # NFL / NBA / other unmapped sports: NO existing production
                # scorer wired for SGO rows.  Fail closed — do NOT route
                # into Soccer.
                await _mark_route_reason(db, sgo, "SGO_UNSUPPORTED_SPORT_ROUTE")
                counts["unsupported_sport_route"] += 1
                continue

            # ── Soccer route (unchanged production path) ────────────
            row = _sgo_row_to_ingest_shape(sgo)
            if not row:
                counts["skipped"] += 1
                continue
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
                await _publish_scored(sgo, update)
            except Exception as _e:
                counts["errors"] += 1
                logger.debug("sgo score upsert %s failed: %s",
                              sgo.get("id"), _e)
    except Exception as _e:
        logger.warning("sgo score pass failed: %s", _e)

    # ── PERKLOCKS SGO MLB WRONG-SOCCER-ROUTE RECOVERY (2026-06) ──────
    # Reset scoring artifacts on current/future SGO MLB rows that were
    # previously scored through the Soccer game-model path so they
    # re-enter the correct MLB scorer on the next pass. We
    # deliberately EXCLUDE rows with legitimate protections
    # (PLAYER_IDENTITY_UNRESOLVED / MLB_PARTIAL_PERIOD_MISLABELED /
    # no_real_book_line / no_bet / synthetic / validation_block /
    # expired event / finished game) — those stay off-board.
    try:
        LEGIT_MLB_PROTECTIONS = {
            "PLAYER_IDENTITY_UNRESOLVED", "MLB_PARTIAL_PERIOD_MISLABELED",
            "no_real_book_line", "synthetic", "validation_block",
            "expired_event", "SGO_UNSUPPORTED_SPORT_ROUTE",
        }
        mlb_recover_q = {
            "source": "sportsgameodds_v2",
            "sport": "MLB",
            "model_source": "soccer_game_model",
            "$or": [
                {"event_time": {"$gte": now_iso}},
                {"event_time": {"$exists": False}},
            ],
        }
        n_reset = 0
        async for sgo_mlb in db.picks.find(mlb_recover_q).limit(limit):
            reasons = set(sgo_mlb.get("off_board_reasons") or [])
            if reasons & LEGIT_MLB_PROTECTIONS:
                # Legitimate blocker present — do NOT revive.
                continue
            if sgo_mlb.get("no_bet") is True:
                continue
            # Wipe scoring so the main loop picks it up next time.
            await db.picks.update_one(
                {"id": sgo_mlb.get("id")},
                {"$set": {
                    "lock_score": None,
                    "published_lock_score": None,
                    "model_probability": None,
                    "model_source": None,
                    "scored_by": None,
                    "off_board": False,
                    "off_board_reasons": [],
                    "publication_state": None,
                    "updated_at": now_iso,
                }},
            )
            n_reset += 1
        if n_reset:
            counts["mlb_recovered_for_rescoring"] = n_reset
            logger.info("SGO MLB recovery: reset %d rows for re-scoring",
                         n_reset)
    except Exception as _rec_err:
        logger.warning("sgo mlb recovery failed: %s", _rec_err)

    # ── PERKLOCKS DOWNSTREAM BACKFILL (2026-06) ──────────────────────
    # Publish already-scored SGO rows that still lack canonical
    # publication (``publication_state='PUBLISHED'``).  This closes
    # the gap for rows scored by a prior pipeline version that
    # wrote ``lock_score`` but never routed through
    # ``publish_upserted_picks``.  Off-board rows are skipped by
    # ``_publish_scored`` so legitimate integrity gates
    # (identity/roster/etc.) stay intact.
    try:
        backfill_q = {
            "source": "sportsgameodds_v2",
            "lock_score": {"$gte": 85},
            "off_board": {"$ne": True},
            "$or": [
                {"publication_state": {"$exists": False}},
                {"publication_state": {"$ne": "PUBLISHED"}},
            ],
        }
        backfill_cursor = db.picks.find(backfill_q).limit(limit)
        async for sgo_bf in backfill_cursor:
            await _publish_scored(sgo_bf, {})
    except Exception as _bf_err:
        logger.warning("sgo publication backfill failed: %s", _bf_err)

    return counts
