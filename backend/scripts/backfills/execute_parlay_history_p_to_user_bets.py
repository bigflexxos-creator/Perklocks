"""execute_parlay_history_p_to_user_bets — Phase 3G Step 5 executor.

Controlled migration of eligible user-owned ``p_*`` records from
``parlay_history`` into canonical ``user_bets`` records.

Step 5 hard rules
─────────────────
* Requires BOTH ``--execute`` AND ``--confirm PRODUCTION``.  Any missing
  flag → refuse and exit non-zero.
* Runs an immediate pre-execution gate: fresh preflight of the
  migration index + ledger preflight + fresh classification.  Refuses
  to write if any condition fails.
* Never modifies ``parlay_history``.  Never modifies
  ``prediction_snapshots``.  Never modifies ``settlement_events``.
  Never overwrites an existing canonical wager.
* Uses ``services.user_bet_ledger.map_legacy_user_parlay`` as the SINGLE
  mapping source.  No re-implementation.
* Uses the Phase 3B shared DB lifecycle — no independent Mongo client.
* Idempotent by ``(migration_source, migration_source_id)`` — a rerun
  inserts zero additional rows.

Rollback metadata
─────────────────
Every inserted row carries:
  • ``migration_source = "parlay_history"``
  • ``migration_source_id = <legacy p_ id>``
  • ``migration_version = 1``  (bump if the mapper shape changes)
  • ``is_legacy = true``
Rollback is a single Mongo filter:
  ``db.user_bets.deleteMany({migration_source:"parlay_history",
                             migration_version: 1})``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_backend_root = Path(__file__).resolve().parents[2]
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from dotenv import load_dotenv
load_dotenv(str(_backend_root / ".env"))

from services import database as _shared_db
from services import user_bet_ledger as UBL
from scripts.backfills import migrate_parlay_history_p_to_user_bets as DRYRUN

logger = logging.getLogger("lockscore.backfill.step5_execute")

SOURCE_COLLECTION = DRYRUN.SOURCE_COLLECTION
TARGET_COLLECTION = DRYRUN.TARGET_COLLECTION
FORBIDDEN_MUTATION_COLLECTIONS = DRYRUN.FORBIDDEN_MUTATION_COLLECTIONS

STEP5_MIGRATION_VERSION = UBL.CANONICAL_MIGRATION_VERSION
CONFIRM_TOKEN = "PRODUCTION"


class ExecutionRefused(RuntimeError):
    pass


@dataclass
class InsertRecord:
    legacy_id:           str
    user_bet_id:         str
    user_id:             str            # NOT emitted to public reports
    wager_type:          str
    canonical_status:    str
    original_status:     Optional[str]
    legs_count:          int
    combined_odds:       Optional[int]
    stake_amount:        Optional[float]
    inserted_at:         datetime


@dataclass
class ExecReport:
    started_at:                datetime
    finished_at:               Optional[datetime] = None
    mode:                       str                 = "refused"
    filter_user_id_present:    bool                = False
    resume_from:               Optional[str]       = None
    limit:                     Optional[int]       = None
    batch_size:                int                 = 100
    migration_version:         int                 = STEP5_MIGRATION_VERSION

    # Pre-execution
    pre_index_preflight:       dict[str, Any]      = field(default_factory=dict)
    pre_ledger_preflight:      dict[str, Any]      = field(default_factory=dict)
    pre_dryrun_summary:        dict[str, Any]      = field(default_factory=dict)
    pre_gate_ok:               bool                = False
    pre_gate_blockers:         list[str]           = field(default_factory=list)

    # Execution
    collection_counts_before:  dict[str, int]      = field(default_factory=dict)
    selected_count:            int                 = 0
    inserted_count:            int                 = 0
    skipped_existing_count:    int                 = 0
    inserted_records:          list[dict]          = field(default_factory=list)
    inserted_legacy_ids:       list[str]           = field(default_factory=list)
    inserted_user_bet_ids:     list[str]           = field(default_factory=list)
    collection_counts_after:   dict[str, int]      = field(default_factory=dict)
    forbidden_mutations:       list[str]           = field(default_factory=list)

    # Post-execution
    post_dryrun_summary:       dict[str, Any]      = field(default_factory=dict)
    post_migrated_all_duplicate: Optional[bool]    = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("started_at", "finished_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════════════
# Pre-execution gate
# ═════════════════════════════════════════════════════════════════════
async def _pre_execution_gate(
    *,
    db,
    user_id: Optional[str],
    resume_from: Optional[str],
    limit: Optional[int],
    batch_size: int,
) -> tuple[bool, list[str], DRYRUN.DryRunReport]:
    """Rerun the Step 4 dry-run immediately and gate on its findings."""
    dryrun = await DRYRUN.run_dry_run(
        db=db,
        batch_size=int(batch_size),
        limit=limit,
        resume_from=resume_from,
        user_id=user_id,
        include_manual_review=True,
        verbose=False,
    )
    blockers: list[str] = []

    if not dryrun.index_preflight.get("ok"):
        blockers.append(
            f"migration index preflight failed: "
            f"{dryrun.index_preflight.get('conflict_note') or 'unknown'}"
        )
    if not dryrun.ledger_preflight.get("ok"):
        blockers.append(
            f"ledger preflight failed: "
            f"{dryrun.ledger_preflight.get('conflicts') or 'unknown'}"
        )
    if dryrun.counts_by_classification.get(DRYRUN.C_MANUAL_REVIEW, 0) > 0:
        blockers.append(
            f"manual_review > 0 ({dryrun.counts_by_classification[DRYRUN.C_MANUAL_REVIEW]})"
        )
    if dryrun.counts_by_classification.get(DRYRUN.C_UNSAFE, 0) > 0:
        blockers.append(
            f"unsafe > 0 ({dryrun.counts_by_classification[DRYRUN.C_UNSAFE]})"
        )
    ready = dryrun.counts_by_classification.get(DRYRUN.C_MIGRATION_READY, 0)
    if ready < 1:
        # It's OK to run with zero migration_ready (nothing to do), but
        # we DO block if the entire slate turned unsafe/manual — that's
        # a signal.  Zero ready + zero manual + zero unsafe = idempotent
        # rerun; that path is allowed.
        pass

    return (len(blockers) == 0, blockers, dryrun)


# ═════════════════════════════════════════════════════════════════════
# Execute
# ═════════════════════════════════════════════════════════════════════
async def _collection_counts(db) -> dict[str, int]:
    return await DRYRUN._collection_counts(db)


async def execute_migration(
    *,
    db,
    batch_size: int,
    limit: Optional[int],
    resume_from: Optional[str],
    user_id: Optional[str],
) -> ExecReport:
    report = ExecReport(
        started_at=_now_utc(),
        mode="execute",
        filter_user_id_present=user_id is not None,
        resume_from=resume_from,
        limit=limit,
        batch_size=int(batch_size),
    )

    # ── Pre-execution gate (Step 4 dry-run) ─────────────────────────
    ok, blockers, dryrun = await _pre_execution_gate(
        db=db,
        user_id=user_id,
        resume_from=resume_from,
        limit=limit,
        batch_size=batch_size,
    )
    report.pre_index_preflight  = dryrun.index_preflight
    report.pre_ledger_preflight = dryrun.ledger_preflight
    report.pre_dryrun_summary   = {
        "eligible_p_star":           dryrun.eligible_p_star,
        "excluded_plearn":           dryrun.excluded_plearn,
        "counts_by_classification":  dryrun.counts_by_classification,
        "status_mapping_breakdown":  dryrun.status_mapping_breakdown,
        "payout_gaps":               dryrun.payout_gaps,
        "leg_identity_coverage":     dryrun.leg_identity_coverage,
    }
    report.pre_gate_ok        = ok
    report.pre_gate_blockers  = blockers

    if not ok:
        report.mode = "refused"
        report.finished_at = _now_utc()
        return report

    # ── Baseline counts ─────────────────────────────────────────────
    report.collection_counts_before = await _collection_counts(db)

    # ── Enumerate the migration_ready rows to be inserted ───────────
    # NOTE: We iterate parlay_history in the same order as the dry-run
    # (sorted by ``id`` ascending).
    src = db[SOURCE_COLLECTION]
    tgt = db[TARGET_COLLECTION]

    q: dict[str, Any] = {}
    if user_id:
        q["user_id"] = user_id
    if resume_from:
        q["id"] = {"$gt": resume_from}

    cursor = src.find(q).sort("id", 1)
    if batch_size and batch_size > 0:
        cursor = cursor.batch_size(int(batch_size))

    processed = 0
    async for doc in cursor:
        if limit is not None and processed >= limit:
            break
        processed += 1

        # Re-run row analysis so a race with a concurrent writer can be
        # detected and this row skipped.
        rc = await DRYRUN.analyse_row(tgt, doc)
        if rc.classification == DRYRUN.C_DUPLICATE_EXISTING:
            report.skipped_existing_count += 1
            continue
        if rc.classification != DRYRUN.C_MIGRATION_READY:
            # Any manual_review / unsafe / excluded row is skipped.
            # The pre-gate above should have blocked us if these existed
            # in the slate, but we defend in depth: NEVER migrate a
            # non-ready row.
            continue

        try:
            bet = UBL.map_legacy_user_parlay(doc)
        except UBL.LegacyRowNotEligible:
            # Should be impossible given rc == migration_ready, but
            # never crash the whole run on a single bad row.
            continue

        report.selected_count += 1

        # Idempotent guard — check for an existing canonical row with
        # the same migration source id.  This is the same check the
        # partial-unique index enforces at the DB level.
        existing = await tgt.find_one({
            "migration_source":    "parlay_history",
            "migration_source_id": bet.migration_source_id,
        })
        if existing is not None:
            report.skipped_existing_count += 1
            continue

        # Insert the canonical row.
        bet_doc = bet.to_document()
        try:
            await tgt.insert_one(bet_doc)
        except Exception as e:
            # Unique-index violation on race → treat as skipped.
            logger.warning(
                "insert race-lost on %s: %s", bet.migration_source_id, e,
            )
            report.skipped_existing_count += 1
            continue

        report.inserted_count += 1
        report.inserted_legacy_ids.append(bet.migration_source_id)
        report.inserted_user_bet_ids.append(bet.user_bet_id)
        rec = InsertRecord(
            legacy_id=bet.migration_source_id or "",
            user_bet_id=bet.user_bet_id,
            user_id=bet.user_id,
            wager_type=bet.wager_type,
            canonical_status=bet.status,
            original_status=bet.original_status,
            legs_count=len(bet.legs or []),
            combined_odds=bet.combined_odds,
            stake_amount=bet.stake_amount,
            inserted_at=_now_utc(),
        )
        rec_pub = asdict(rec)
        # Never emit raw user_id into the public report body — mirror
        # Step 3's safe-reporting policy.
        rec_pub.pop("user_id", None)
        report.inserted_records.append(rec_pub)

    # ── After counts + forbidden-mutation guard ─────────────────────
    report.collection_counts_after = await _collection_counts(db)
    forbidden = []
    for c in FORBIDDEN_MUTATION_COLLECTIONS:
        b = report.collection_counts_before.get(c, 0)
        a = report.collection_counts_after.get(c, 0)
        if b != a:
            forbidden.append(c)
    report.forbidden_mutations = forbidden

    # ── Post-execution dry-run — every migrated row must now dup ────
    post = await DRYRUN.run_dry_run(
        db=db,
        batch_size=int(batch_size),
        limit=None,
        resume_from=None,
        user_id=None,
        include_manual_review=True,
        verbose=False,
    )
    report.post_dryrun_summary = {
        "eligible_p_star":          post.eligible_p_star,
        "counts_by_classification": post.counts_by_classification,
    }
    if report.inserted_legacy_ids:
        # Every legacy_id we inserted must now classify as
        # duplicate_existing (primary match).
        inserted_set = set(report.inserted_legacy_ids)
        matched = 0
        for rc in post.classifications:
            if rc.get("legacy_id") in inserted_set:
                if rc.get("classification") == DRYRUN.C_DUPLICATE_EXISTING and \
                   rc.get("duplicate_match") == "primary":
                    matched += 1
        report.post_migrated_all_duplicate = (matched == len(inserted_set))
    else:
        report.post_migrated_all_duplicate = True   # nothing to check

    report.finished_at = _now_utc()
    return report


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3G Step 5 — execute legacy p_* → user_bets migration.",
    )
    p.add_argument("--execute", action="store_true", default=False,
                   help="required to perform any write")
    p.add_argument("--confirm", type=str, default=None,
                   help=f"must equal {CONFIRM_TOKEN!r} to authorise production execution")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--resume-from", type=str, default=None)
    p.add_argument("--user-id", type=str, default=None)
    p.add_argument("--report-path", type=str, default=None)
    p.add_argument("--verbose", action="store_true", default=False)
    return p.parse_args(argv)


async def _amain(argv: Optional[list[str]] = None) -> ExecReport:
    args = _parse_args(argv)
    if not (args.execute and args.confirm == CONFIRM_TOKEN):
        msg = (
            "Phase 3G Step 5: production execution requires BOTH --execute "
            f"AND --confirm {CONFIRM_TOKEN!r}.  Refusing to run — zero writes."
        )
        print(msg, file=sys.stderr)
        raise ExecutionRefused(msg)

    _shared_db.initialize_database()
    db = _shared_db.get_database()
    report = await execute_migration(
        db=db,
        batch_size=int(args.batch_size),
        limit=(int(args.limit) if args.limit is not None else None),
        resume_from=args.resume_from,
        user_id=args.user_id,
    )
    payload = json.dumps(report.to_dict(), indent=2, default=str)
    if args.verbose:
        print(payload)
    else:
        summary = {
            "mode":                       report.mode,
            "pre_gate_ok":                report.pre_gate_ok,
            "pre_gate_blockers":          report.pre_gate_blockers,
            "selected_count":             report.selected_count,
            "inserted_count":             report.inserted_count,
            "skipped_existing_count":     report.skipped_existing_count,
            "inserted_legacy_ids":        report.inserted_legacy_ids,
            "forbidden_mutations":        report.forbidden_mutations,
            "post_migrated_all_duplicate": report.post_migrated_all_duplicate,
        }
        print(json.dumps(summary, indent=2, default=str))
    if args.report_path:
        Path(args.report_path).write_text(payload, encoding="utf-8")
    if report.forbidden_mutations:
        raise RuntimeError(
            f"forbidden collections changed during execute: "
            f"{report.forbidden_mutations}"
        )
    return report


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(_amain())
    except ExecutionRefused:
        sys.exit(2)


if __name__ == "__main__":
    main()
