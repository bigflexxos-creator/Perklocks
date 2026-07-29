"""Parlay Learning Loop (Phase 5, 2026-06-30).

Post-settlement telemetry that records:
  • selected legs
  • confidence before the game (predicted survival + per-leg parlay_score)
  • result of the parlay (won / lost / push)
  • which leg failed (for losses)
  • the inferred reason it failed (matchup grade, thin sample,
    negative correlation exposure, low fusion probability, …)

The loop is a WRITE-ONLY telemetry pipeline — it does NOT retrain
models or mutate picks. Its output feeds the ranker's reliability
signal via `get_leg_reliability(sport, market_family)`.

Collections
───────────
  `parlay_learning_events`
    { id, signature, outcome, leg_count, mode, survival_pct,
      failed_leg: {pick_id, sport, family, reason, ranking_snapshot},
      recorded_at }

  `parlay_leg_reliability`
    { id, sport, family, n_wins, n_losses, n_total,
      hit_rate, avg_predicted_score, avg_actual_delta,
      updated_at }
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.parlay_intelligence.learning")

EVENTS_COLL = "parlay_learning_events"
RELIAB_COLL = "parlay_leg_reliability"


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _stable_id(signature: str, tag: str = "ple") -> str:
    h = hashlib.sha1(f"{tag}_{signature}".encode()).hexdigest()[:14]
    return f"{tag}_{h}"


def _pick_status(pick_row: Optional[dict]) -> str:
    if not pick_row:
        return "unknown"
    return (pick_row.get("status") or "pending").lower()


# ═════════════════════════════════════════════════════════════════════
# Failure attribution
# ═════════════════════════════════════════════════════════════════════
def infer_failure_reason(leg_snapshot: dict, ranking: Optional[dict] = None,
                          correlation_ctx: Optional[dict] = None) -> str:
    """Return a short human string describing why this leg likely failed.

    `leg_snapshot` is the row stored in `parlay_history.legs` (has
    `market_family`, `sport`, `lock_score`, `win_probability`).
    `ranking` is the LegRanking dict captured at parlay-shown time.
    `correlation_ctx` is the report snapshot at build time."""
    reasons: list[str] = []
    lock = leg_snapshot.get("lock_score")
    win_p = leg_snapshot.get("win_probability")

    if ranking:
        comps = ranking.get("components") or {}
        if isinstance(comps.get("matchup"), (int, float)) \
                and comps["matchup"] < 45:
            reasons.append("weak matchup grade")
        if isinstance(comps.get("sample_confidence"), (int, float)) \
                and comps["sample_confidence"] < 50:
            reasons.append("thin historical sample")
        if isinstance(comps.get("model_agreement"), (int, float)) \
                and comps["model_agreement"] < 50:
            reasons.append("models disagreed")
        if isinstance(comps.get("fused_probability"), (int, float)) \
                and comps["fused_probability"] < 55:
            reasons.append("fused probability was borderline")

    if isinstance(lock, (int, float)) and lock < 82:
        reasons.append(f"lock only {lock:.0f}")
    if isinstance(win_p, (int, float)) and win_p < 60:
        reasons.append(f"win prob only {win_p:.0f}%")

    if correlation_ctx:
        # Was this leg part of a negative-correlation pair?
        idx = correlation_ctx.get("_leg_index")
        for pair in correlation_ctx.get("negative_pairs") or []:
            try:
                if idx in tuple(pair):
                    reasons.append("negative-correlation exposure")
                    break
            except Exception:
                pass

    if not reasons:
        return "no obvious pregame weakness — variance loss"
    return "; ".join(reasons[:3])


# ═════════════════════════════════════════════════════════════════════
# Reliability aggregation
# ═════════════════════════════════════════════════════════════════════
async def _bump_reliability(db, sport: str, family: str,
                             outcome: str,
                             predicted_score: Optional[float]) -> None:
    """Increment the reliability counters for this (sport, family). Uses
    an atomic upsert so concurrent settlement passes are safe."""
    if db is None or not family or family == "other":
        return
    inc_wins = 1 if outcome == "won" else 0
    inc_losses = 1 if outcome == "lost" else 0
    inc_pushes = 1 if outcome == "push" else 0
    inc: dict = {
        "n_total": 1, "n_wins": inc_wins, "n_losses": inc_losses,
        "n_pushes": inc_pushes,
    }
    if isinstance(predicted_score, (int, float)):
        inc["_score_sum"] = float(predicted_score)
        inc["_score_n"] = 1
    try:
        await db[RELIAB_COLL].update_one(
            {"sport": sport, "family": family},
            {
                "$setOnInsert": {
                    "id": _stable_id(f"{sport}|{family}", "prl"),
                    "sport": sport, "family": family,
                },
                "$inc": inc,
                "$set": {"updated_at": _now_iso()},
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning("reliability bump failed for %s/%s: %s",
                       sport, family, e)


async def _refresh_derived(db, sport: str, family: str) -> None:
    """Recompute hit_rate and avg_predicted_score from the counters
    (idempotent, safe to call after every update)."""
    if db is None:
        return
    try:
        row = await db[RELIAB_COLL].find_one(
            {"sport": sport, "family": family},
            {"_id": 0},
        )
        if not row:
            return
        n_total = row.get("n_total") or 0
        n_wins = row.get("n_wins") or 0
        score_n = row.get("_score_n") or 0
        score_sum = row.get("_score_sum") or 0.0
        hit_rate = (n_wins / n_total) if n_total > 0 else 0.0
        avg_pred = (score_sum / score_n) if score_n > 0 else None
        set_fields: dict = {"hit_rate": round(hit_rate, 3)}
        if avg_pred is not None:
            set_fields["avg_predicted_score"] = round(avg_pred, 2)
        await db[RELIAB_COLL].update_one(
            {"sport": sport, "family": family},
            {"$set": set_fields},
        )
    except Exception as e:
        logger.warning("reliability derived refresh failed: %s", e)


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════
async def record_completed_parlay(db, parlay_row: dict, *,
                                   pick_statuses: Optional[dict] = None,
                                   ranking_snapshot: Optional[dict] = None,
                                   correlation_snapshot: Optional[dict] = None,
                                   ) -> Optional[dict]:
    """Record one settled parlay to the learning log.

    Args:
      parlay_row: settled `parlay_history` row (has `signature`, `status`,
        `legs`, `survival_pct`, `leg_count`).
      pick_statuses: {pick_id: "won"|"lost"|"push"|"pending"} map.
      ranking_snapshot: {pick_id: {parlay_score, components, …}} captured
        when the parlay was shown to the user. Used to attribute the
        failing leg to specific pregame signals.
      correlation_snapshot: full CorrelationReport dict for the parlay.

    Returns the recorded event dict, or None on no-op / error.
    """
    if not isinstance(parlay_row, dict):
        return None
    status = (parlay_row.get("status") or "").lower()
    if status not in ("won", "lost", "push"):
        return None
    legs = parlay_row.get("legs") or []
    signature = parlay_row.get("signature") or ""
    if not signature:
        return None

    pick_statuses = pick_statuses or {}
    ranking_snapshot = ranking_snapshot or {}
    correlation_snapshot = correlation_snapshot or {}

    # Identify failing leg for losses
    failed_leg: Optional[dict] = None
    if status == "lost":
        for idx, leg in enumerate(legs):
            pid = leg.get("pick_id")
            leg_status = pick_statuses.get(pid) if pid else None
            if leg_status == "lost":
                ranking = ranking_snapshot.get(pid) if pid else None
                ctx = dict(correlation_snapshot)
                ctx["_leg_index"] = idx
                failed_leg = {
                    "pick_id":  pid,
                    "sport":    leg.get("sport"),
                    "family":   leg.get("market_family") or "other",
                    "market":   leg.get("market"),
                    "reason":   infer_failure_reason(leg, ranking, ctx),
                    "ranking_snapshot": ranking,
                    "leg_index": idx,
                }
                break
        # If none flagged lost specifically (all pending / unknown), skip
        # attribution but still record the parlay event.

    event = {
        "id":            _stable_id(signature, "ple"),
        "signature":     signature,
        "outcome":       status,
        "leg_count":     parlay_row.get("leg_count") or len(legs),
        "mode":          parlay_row.get("mode"),
        "survival_pct":  parlay_row.get("survival_pct"),
        "failed_leg":    failed_leg,
        "recorded_at":   _now_iso(),
    }
    if db is not None:
        try:
            await db[EVENTS_COLL].update_one(
                {"signature": signature, "outcome": status},
                {"$setOnInsert": event},
                upsert=True,
            )
        except Exception as e:
            logger.warning("record_completed_parlay insert failed: %s", e)

    # Bump per-(sport, family) reliability counters. The whole parlay
    # outcome propagates to each leg's bucket — this is the same
    # weighting the existing `parlay_synergy` map uses.
    seen: set[tuple[str, str]] = set()
    for leg in legs:
        sport = (leg.get("sport") or "").lower()
        family = leg.get("market_family") or "other"
        if (sport, family) in seen or not sport or family == "other":
            continue
        seen.add((sport, family))
        pred_score = None
        pid = leg.get("pick_id")
        if pid and pid in ranking_snapshot:
            pred_score = (ranking_snapshot[pid] or {}).get("parlay_score")
        await _bump_reliability(db, sport, family, status, pred_score)
        await _refresh_derived(db, sport, family)

    return event


async def get_leg_reliability(db, sport: str, family: str,
                              *, min_samples: int = 5) -> Optional[dict]:
    """Return the reliability row for this (sport, family), or None if
    below the min-sample gate. Used by leg_ranker to blend historical
    parlay-context performance into future rankings."""
    if db is None or not sport or not family or family == "other":
        return None
    try:
        row = await db[RELIAB_COLL].find_one(
            {"sport": (sport or "").lower(), "family": family},
            {"_id": 0},
        )
    except Exception as e:
        logger.warning("get_leg_reliability read failed: %s", e)
        return None
    if not row:
        return None
    n_total = int(row.get("n_total") or 0)
    if n_total < min_samples:
        return None
    return {
        "sport": row.get("sport"),
        "family": row.get("family"),
        "n_total": n_total,
        "n_wins":  int(row.get("n_wins") or 0),
        "n_losses": int(row.get("n_losses") or 0),
        "hit_rate": float(row.get("hit_rate") or 0.0),
        "avg_predicted_score": row.get("avg_predicted_score"),
    }
