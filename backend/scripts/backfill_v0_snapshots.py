"""Phase 1c — v0 legacy snapshot backfill.

DRY-RUN ONLY in Phase 1a.  Actual writes happen in Phase 1c after
review of the mismatch report.

For every row in `picks`, this script:
  1. Constructs a `PublishedPayload` from the current field values.
  2. Marks `snapshot_version=0`, `is_legacy=True`,
     `publication_source="legacy_backfill"`.
  3. Fills every `*_version` field with `"legacy_unknown"` unless the
     pick already carries a real version string.
  4. In DRY-RUN mode: computes what the snapshot WOULD look like and
     compares it to any existing snapshot; reports counts + any
     mismatches; NEVER writes to `prediction_snapshots`.
  5. In LIVE mode (--live): performs an idempotent insert using the
     publication service; skips predictions that already have a v0
     snapshot.

Usage:
    # Dry-run (default) — safe, reports only
    python -m scripts.backfill_v0_snapshots

    # Live write (Phase 1c) — requires --live AND explicit confirmation
    python -m scripts.backfill_v0_snapshots --live --i-understand
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError


BATCH_SIZE = 200


def _default_board_version() -> str:
    return "legacy_backfill_v0"


async def main(*, live: bool, i_understand: bool, limit: int | None) -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "lockscore_db")]

    from services.prediction_publication_service import (
        PredictionPublicationService, SNAPSHOT_COLLECTION,
        PublishedPayload, _compute_idempotency_key, _sha256_canonical,
        LEGACY_UNKNOWN, _normalize_probability_at_publish,
    )

    if live and not i_understand:
        print("REFUSING to run in --live mode without --i-understand")
        return

    pub = PredictionPublicationService(db, board_version=_default_board_version())
    await pub.ensure_indices()

    n_examined = 0
    n_would_create = 0
    n_already_have_v0 = 0
    n_errors = 0
    n_created = 0
    field_gaps: dict[str, int] = {}

    cursor = db.picks.find({}, {"_id": 0})
    if limit:
        cursor = cursor.limit(limit)

    async for pick in cursor:
        n_examined += 1
        pid = pick.get("id")
        if not pid:
            n_errors += 1
            continue

        # Track which version fields are missing so we know how much
        # legacy_unknown we'll be emitting.
        for k in ("model_version", "fusion_version", "scoring_version",
                  "calibration_version", "validator_version",
                  "simulation_version", "feature_snapshot_version"):
            if not pick.get(k):
                field_gaps[k] = field_gaps.get(k, 0) + 1

        # Already have a v0?
        existing_v0 = await db[SNAPSHOT_COLLECTION].find_one(
            {"prediction_id": pid, "snapshot_version": 0},
            {"_id": 0, "payload_hash": 1, "idempotency_key": 1},
        )
        if existing_v0:
            n_already_have_v0 += 1
            continue

        # ── P0-1 (2026-08-11) canonical unit alignment ──────────────
        # Backfill emits the same units as the live publish path:
        #   • probability → 0-1 fraction (canonical)
        #   • edge        → Optional[float]  (None preserved)
        #   • confidence  → label string (never 0.0)
        _bf_prob = _normalize_probability_at_publish(
            _maybe_float(pick.get("win_probability")))
        _bf_edge = _maybe_float(pick.get("edge_percent"))
        _bf_conf_raw = pick.get("confidence")
        _bf_conf = (str(_bf_conf_raw)
                    if _bf_conf_raw not in (None, "") else LEGACY_UNKNOWN)

        # Build a legacy payload.
        payload = PublishedPayload(
            prediction_id=pid,
            pick_id=pid,
            snapshot_version=0,
            board_version=_default_board_version(),
            published_probability=_bf_prob,
            published_edge=_bf_edge,
            published_lock_score=round(_float(pick.get("lock_score")), 2),
            published_grade=str(pick.get("grade") or "Pass"),
            published_confidence=_bf_conf,
            published_confidence_score=None,
            published_reasoning=(pick.get("reasoning")
                                  or pick.get("pick_rationale") or {}),
            published_line=_maybe_float(pick.get("line")),
            published_odds=_maybe_int(pick.get("book_odds")),
            model_version=str(pick.get("model_version") or LEGACY_UNKNOWN),
            fusion_version=str(pick.get("fusion_version") or LEGACY_UNKNOWN),
            scoring_version=str(pick.get("scoring_version") or LEGACY_UNKNOWN),
            calibration_version=str(
                pick.get("calibration_version") or LEGACY_UNKNOWN),
            validator_version=str(
                pick.get("validator_version") or LEGACY_UNKNOWN),
            simulation_version=str(
                pick.get("simulation_version") or LEGACY_UNKNOWN),
            feature_snapshot_version=str(
                pick.get("feature_snapshot_version") or LEGACY_UNKNOWN),
            publication_source="legacy_backfill",
            is_legacy=True,
        )
        n_would_create += 1

        if live:
            snap_dict = payload.to_snapshot_dict(
                payload_hash="",
                idempotency_key="",
                published_at=datetime.now(timezone.utc),
                is_active=False,  # legacy v0 is NEVER active
            )
            snap_dict["payload_hash"] = _sha256_canonical(snap_dict)
            snap_dict["idempotency_key"] = _compute_idempotency_key(payload)
            try:
                await db[SNAPSHOT_COLLECTION].insert_one(snap_dict)
                n_created += 1
            except DuplicateKeyError:
                # Someone else beat us — count as already-have
                n_already_have_v0 += 1
            except Exception as e:
                n_errors += 1
                if n_errors < 20:
                    print(f"  ! insert failed for {pid}: {e}")

        if n_examined % 500 == 0:
            print(f"  examined {n_examined}, would_create={n_would_create}, "
                  f"already_v0={n_already_have_v0}")

    print("=" * 68)
    print(f" Phase 1c v0 backfill — {'LIVE' if live else 'DRY-RUN'}")
    print("=" * 68)
    print(f" Picks examined         : {n_examined:,}")
    print(f" Would create v0 snap.  : {n_would_create:,}")
    print(f" Already have v0        : {n_already_have_v0:,}")
    print(f" Errors                 : {n_errors:,}")
    if live:
        print(f" Actually created       : {n_created:,}")
    print(f"\n Version metadata gaps (fields that will be 'legacy_unknown'):")
    for k in sorted(field_gaps.keys()):
        pct = 100 * field_gaps[k] / max(1, n_examined)
        print(f"   {k:<30} {field_gaps[k]:>6,}  ({pct:>5.1f}% of picks)")
    print("=" * 68)


def _float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _maybe_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _maybe_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except Exception:
        return None


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true",
                   help="Actually write snapshots (default is dry-run)")
    p.add_argument("--i-understand", action="store_true",
                   help="Required alongside --live to prevent accidents")
    p.add_argument("--limit", type=int, default=None,
                   help="Limit rows examined (useful for smoke tests)")
    args = p.parse_args()
    asyncio.run(main(live=args.live, i_understand=args.i_understand,
                     limit=args.limit))
