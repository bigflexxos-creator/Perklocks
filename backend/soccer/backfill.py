"""Daily backfill loop — grades yesterday's soccer predictions against
actual results pulled from football-data.org.

For each prediction stored in `soccer_predictions` whose match has
finished, we look up the real outcome (HOME_TEAM / AWAY_TEAM / DRAW)
and write back:

  • `actual_winner`   ("HOME" | "AWAY" | "DRAW")
  • `correct`         (bool — did pick_side match actual_winner?)
  • `graded_at`       (ISO timestamp)

After grading, we aggregate per-bucket accuracy stats into the
`soccer_accuracy` collection so the UI / model can read them
back without rescanning all predictions.

Runs once on startup + every 24h thereafter. Free-tier safe — uses
the cached `finished_matches_for_date()` so a full backfill window
costs at most ~13 API calls (one per active competition per day).
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .client import client
from .normalize import normalize_match

logger = logging.getLogger("lockscore.soccer.backfill")

# How many days back to scan on each backfill pass. 3 days covers
# any games that finished late (e.g. postponed → played next day) and
# also lets us re-grade if the model was updated mid-window.
_LOOKBACK_DAYS = 3


def _pick_outcome_for_prediction(pred: dict, actual_winner: str | None) -> bool | None:
    """Was the predicted side correct?
    Returns None if the result is missing/postponed."""
    if actual_winner not in {"HOME_TEAM", "AWAY_TEAM", "DRAW"}:
        return None
    pick_side = pred.get("pick_side")
    if pick_side == "HOME":
        return actual_winner == "HOME_TEAM"
    if pick_side == "AWAY":
        return actual_winner == "AWAY_TEAM"
    if pick_side == "DRAW":
        return actual_winner == "DRAW"
    return None


async def run_backfill(db) -> dict:
    """Grade all un-graded predictions for the lookback window.

    Returns a summary dict the scheduler logs. Safe to run multiple
    times — already-graded predictions are skipped.
    """
    started = datetime.now(timezone.utc)
    summary: dict[str, Any] = {
        "started_at":      started.isoformat(),
        "days_scanned":    0,
        "results_fetched": 0,
        "graded":          0,
        "skipped_pending": 0,
        "by_outcome":      {},
        "errors":          [],
    }

    today = date.today()
    # Build a fixture_id → actual result map across the window.
    result_index: dict[int, dict] = {}
    for n in range(1, _LOOKBACK_DAYS + 1):
        d = today - timedelta(days=n)
        try:
            matches = await client.finished_matches_for_date(d)
        except Exception as e:
            summary["errors"].append(f"finished_matches({d}): {e}")
            continue
        summary["days_scanned"] += 1
        summary["results_fetched"] += len(matches)
        for raw in matches:
            fx = normalize_match(raw)
            fid = fx.get("fixture_id")
            if fid is None:
                continue
            result_index[fid] = {
                "actual_winner":   (raw.get("score") or {}).get("winner"),
                "home_goals":      fx.get("home_goals"),
                "away_goals":      fx.get("away_goals"),
                "status":          fx.get("status"),
                "match_date":      fx.get("date"),
            }

    if not result_index:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("Soccer backfill: no finished matches found in lookback window")
        return summary

    # Grade every un-graded prediction whose fixture is in our index.
    # `correct` is the canonical flag — its presence means graded.
    counter: Counter = Counter()
    cursor = db.soccer_predictions.find(
        {"fixture_id": {"$in": list(result_index.keys())},
         "correct":    {"$exists": False}},
        {"_id": 0, "id": 1, "fixture_id": 1, "pick_side": 1, "model_version": 1},
    )
    async for pred in cursor:
        info = result_index.get(pred["fixture_id"]) or {}
        correct = _pick_outcome_for_prediction(pred, info.get("actual_winner"))
        if correct is None:
            summary["skipped_pending"] += 1
            continue
        await db.soccer_predictions.update_one(
            {"id": pred["id"]},
            {"$set": {
                "correct":       correct,
                "actual_winner": _actual_to_side(info.get("actual_winner")),
                "home_goals":    info.get("home_goals"),
                "away_goals":    info.get("away_goals"),
                "graded_at":     datetime.now(timezone.utc).isoformat(),
            }},
        )
        summary["graded"] += 1
        counter["correct" if correct else "incorrect"] += 1
        counter[f"side:{pred.get('pick_side')}"] += 1
        counter[f"model:{pred.get('model_version', 'unknown')}"] += 1

    summary["by_outcome"] = dict(counter)

    # Persist aggregate accuracy stats into a tiny rollup collection.
    await _refresh_accuracy_aggregate(db)

    finished = datetime.now(timezone.utc)
    summary["finished_at"] = finished.isoformat()
    summary["elapsed_ms"] = int((finished - started).total_seconds() * 1000)
    logger.info("Soccer backfill: %s", summary)
    return summary


def _actual_to_side(winner: str | None) -> str | None:
    """Map football-data.org winner → our internal side enum."""
    if winner == "HOME_TEAM": return "HOME"
    if winner == "AWAY_TEAM": return "AWAY"
    if winner == "DRAW":      return "DRAW"
    return None


async def _refresh_accuracy_aggregate(db) -> None:
    """Compute per-model + overall accuracy and stash it in
    `soccer_accuracy` for fast UI reads. Single-row design — small
    rollup, full recompute every backfill."""
    cursor = db.soccer_predictions.find(
        {"correct": {"$exists": True}},
        {"_id": 0, "correct": 1, "model_version": 1, "pick_side": 1,
         "confidence": 1, "league": 1},
    )
    total = 0
    correct = 0
    by_model: dict[str, dict] = {}
    by_side: dict[str, dict] = {}
    by_league: dict[str, dict] = {}
    by_confbucket: dict[str, dict] = {}
    async for r in cursor:
        total += 1
        if r.get("correct"): correct += 1
        for bucket_key, key_value in (
            ("model_version", r.get("model_version") or "unknown"),
            ("pick_side",     r.get("pick_side") or "unknown"),
            ("league",        r.get("league") or "unknown"),
        ):
            target = {"model_version": by_model, "pick_side": by_side,
                      "league": by_league}[bucket_key]
            row = target.setdefault(key_value, {"n": 0, "correct": 0})
            row["n"] += 1
            if r.get("correct"): row["correct"] += 1
        conf = float(r.get("confidence") or 0)
        bucket = _conf_bucket(conf)
        row = by_confbucket.setdefault(bucket, {"n": 0, "correct": 0})
        row["n"] += 1
        if r.get("correct"): row["correct"] += 1

    def _shape(buckets: dict) -> list[dict]:
        out = []
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
            wr = v["correct"] / v["n"] if v["n"] else 0.0
            out.append({"key": k, "n": v["n"], "correct": v["correct"],
                        "accuracy": round(wr, 3)})
        return out

    doc = {
        "_id": "rollup",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "by_model_version": _shape(by_model),
        "by_pick_side":     _shape(by_side),
        "by_league":        _shape(by_league)[:25],
        "by_confidence_bucket": _shape(by_confbucket),
    }
    await db.soccer_accuracy.update_one({"_id": "rollup"}, {"$set": doc}, upsert=True)


def _conf_bucket(c: float) -> str:
    """Slice confidence into readable bands for accuracy tracking."""
    if c >= 90: return "90-100"
    if c >= 80: return "80-89"
    if c >= 70: return "70-79"
    if c >= 60: return "60-69"
    return "<60"


async def soccer_backfill_loop(db) -> None:
    """Run backfill on startup + every 24h."""
    # Wait a bit longer than the pipeline so the first prediction
    # cycle has time to populate predictions before grading.
    await asyncio.sleep(120)
    while True:
        try:
            await run_backfill(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Soccer backfill loop error: %s", e)
        await asyncio.sleep(24 * 60 * 60)
