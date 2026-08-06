"""user_bets_add_canonical_fields — Phase 3G Step 3 schema extension.

Idempotent, resumable, batch-based migration that extends existing
``user_bets`` documents with the canonical nullable fields required
by :mod:`services.user_bet_ledger`.

Guardrails
──────────
* **Dry-run by default.**  ``--execute`` is required for any write.
* **Only ``user_bets`` is touched.**  A defence-in-depth guard aborts
  if the resolved database exposes ``parlay_history`` or
  ``prediction_snapshots`` writes on any code path in this module.
* **Never overwrites populated values.**  For every candidate field
  the script reads the current value; only *missing* keys (``$exists``
  = False) OR keys whose stored value is exactly ``None`` are added.
* **No invented data.**  Sportsbook, opening/closing lines, odds, CLV,
  snapshot IDs, prediction IDs, payouts, stakes, and event identity
  are ONLY carried across when the legacy source field is present.
  When a source is missing, the target is set to ``None`` (never a
  guessed value).
* **No settlement recompute.**  Existing ``pnl_units`` values are
  copied into ``profit_loss`` as-is — never recomputed.
* **Shared Phase 3B DB lifecycle.**  This script uses
  ``services.database.get_database()`` — no independent Mongo client.
  (Standalone execution goes through
  ``services.database.initialize_database()``.)

Usage
─────
    # Dry-run against the live DB (default)
    python -m scripts.backfills.user_bets_add_canonical_fields

    # Execute
    python -m scripts.backfills.user_bets_add_canonical_fields --execute

    # Scoped by user id, with report path
    python -m scripts.backfills.user_bets_add_canonical_fields \
        --user-id 151f530d-72e8-45c1-9a04-20f4110536cc \
        --report-path /app/PHASE3G_STEP3_DRYRUN_REPORT.json

Options
───────
    --dry-run              default
    --execute              perform writes
    --batch-size <int>     rows per Mongo batch (default 200)
    --limit <int>          stop after N rows scanned
    --resume-from <str>    resume from a specific user_bets._id or id
    --report-path <path>   write JSON report to this file
    --user-id <str>        filter to a single user
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Import order: services.database must load before this script does
# any DB work.  Standalone execution supports both "python -m ..." and
# "python scripts/backfills/user_bets_add_canonical_fields.py".
_backend_root = Path(__file__).resolve().parents[2]
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from dotenv import load_dotenv
load_dotenv(str(_backend_root / ".env"))

from services import database as _shared_db
from services import user_bet_ledger as UBL

logger = logging.getLogger("lockscore.backfill.user_bets_step3")

# ── Constants ────────────────────────────────────────────────────────
TARGET_COLLECTION = "user_bets"
FORBIDDEN_COLLECTIONS = ("parlay_history", "prediction_snapshots")

# Ordered list of every canonical field this migration considers.
# Each entry is (canonical_name, source_field_or_None, default_value).
# ``source_field`` is the legacy field to copy FROM (when present).
# ``default_value`` is used when neither the canonical nor the source
# is available — but only for fields where a default is documented.
_MISSING = object()

CANONICAL_FIELDS: dict[str, dict[str, Any]] = {
    # Identity
    "user_bet_id":         {"src": "id",           "default": _MISSING},
    "client_bet_id":       {"src": None,           "default": None},
    "idempotency_key":     {"src": None,           "default": None},
    # Type + status
    "wager_type":          {"src": "bet_type",     "default": _MISSING},
    "original_status":     {"src": "status",       "default": None},
    # Money — never invented
    "stake_amount":        {"src": "stake_units",  "default": None},
    "combined_odds":       {"src": None,           "default": None},   # only for parlays; computed further below
    "potential_payout":    {"src": None,           "default": None},
    "actual_payout":       {"src": None,           "default": None},
    "profit_loss":         {"src": "pnl_units",    "default": None},
    "sportsbook":          {"src": None,           "default": None},
    # Provenance
    "source":              {"src": None,           "default": "user_track"},   # only for native (is_legacy=false)
    "migration_version":   {"src": None,           "default": UBL.CANONICAL_MIGRATION_VERSION},
    "migration_source":    {"src": None,           "default": None},
    "migration_source_id": {"src": None,           "default": None},
    "is_legacy":           {"src": None,           "default": False},
    # Discretionary
    "mode":                {"src": None,           "default": None},
    "tags":                {"src": None,           "default": []},
    "risk_tier":           {"src": None,           "default": None},
    "correlation_warning": {"src": None,           "default": None},
    # Reference IDs (single-leg)
    "prediction_id":       {"src": "pick_id",      "default": None},
    "snapshot_id":         {"src": None,           "default": None},
    "market_contract_id":  {"src": None,           "default": None},
    "board_version":       {"src": None,           "default": None},
    "event_id":            {"src": None,           "default": None},
    "sport_key":           {"src": "sport",        "default": None},
    # Line snapshots — nullable future fields, never invented
    "opening_line":        {"src": None,           "default": None},
    "opening_odds":        {"src": None,           "default": None},
    "closing_line":        {"src": None,           "default": None},
    "closing_odds":        {"src": None,           "default": None},
    "clv_value":           {"src": None,           "default": None},
    "clv_status":          {"src": None,           "default": UBL.CLV_UNAVAILABLE},
    # Legs + audit trail — arrays default to []
    "legs":                {"src": None,           "default": []},
    "settlement_events":   {"src": None,           "default": []},
}


@dataclass
class RowPlan:
    doc_id:                    str                # existing id or generated
    user_id:                   Optional[str]
    resume_key:                str                # stable resume anchor
    proposed_updates:          dict[str, Any]     = field(default_factory=dict)
    manual_review_reasons:     list[str]          = field(default_factory=list)
    conflicts:                 list[dict]         = field(default_factory=list)   # populated non-null value that would need overwriting

    def to_report_row(self) -> dict[str, Any]:
        return {
            "doc_id":                self.doc_id,
            "user_id_hash":          _hash_userid(self.user_id),
            "resume_key":            self.resume_key,
            "proposed_update_keys":  sorted(self.proposed_updates.keys()),
            "manual_review_reasons": list(self.manual_review_reasons),
            "conflicts":             list(self.conflicts),
        }


@dataclass
class BackfillReport:
    started_at:              datetime
    finished_at:              Optional[datetime]  = None
    mode:                     str                  = "dry-run"
    filter_user_id:           Optional[str]        = None
    resume_from:              Optional[str]        = None
    limit:                    Optional[int]        = None
    batch_size:               int                  = 200
    total_scanned:            int                  = 0
    total_updated:            int                  = 0
    total_skipped:            int                  = 0
    total_manual_review:      int                  = 0
    conflict_rows:            list[dict]           = field(default_factory=list)
    manual_review_rows:       list[dict]           = field(default_factory=list)
    coverage_before:          dict[str, int]       = field(default_factory=dict)
    coverage_after:           dict[str, int]       = field(default_factory=dict)
    collection_counts_before: dict[str, int]       = field(default_factory=dict)
    collection_counts_after:  dict[str, int]       = field(default_factory=dict)
    last_resume_key:          Optional[str]        = None
    forbidden_touched:        list[str]            = field(default_factory=list)
    schema_version:           int                  = UBL.CANONICAL_MIGRATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at":              self.started_at.isoformat(),
            "finished_at":             self.finished_at.isoformat() if self.finished_at else None,
            "mode":                    self.mode,
            "filter_user_id_present":  self.filter_user_id is not None,
            "resume_from":             self.resume_from,
            "limit":                   self.limit,
            "batch_size":              self.batch_size,
            "total_scanned":           int(self.total_scanned),
            "total_updated":           int(self.total_updated),
            "total_skipped":           int(self.total_skipped),
            "total_manual_review":     int(self.total_manual_review),
            "conflict_rows":           self.conflict_rows[:200],
            "manual_review_rows":      self.manual_review_rows[:200],
            "coverage_before":         dict(self.coverage_before),
            "coverage_after":          dict(self.coverage_after),
            "collection_counts_before": dict(self.collection_counts_before),
            "collection_counts_after": dict(self.collection_counts_after),
            "last_resume_key":         self.last_resume_key,
            "forbidden_touched":       list(self.forbidden_touched),
            "schema_version":          self.schema_version,
        }


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
def _hash_userid(uid: Optional[str]) -> Optional[str]:
    """Never emit raw user_id into a report; return a short hash."""
    if not uid:
        return None
    import hashlib
    return "uid_" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _get_collection_counts(db) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in (TARGET_COLLECTION, *FORBIDDEN_COLLECTIONS):
        try:
            out[name] = await db[name].count_documents({})
        except Exception:
            out[name] = -1
    return out


async def _coverage_snapshot(db) -> dict[str, int]:
    coll = db[TARGET_COLLECTION]
    out: dict[str, int] = {}
    for fname in CANONICAL_FIELDS.keys():
        out[fname] = await coll.count_documents({fname: {"$exists": True}})
    return out


def _decide_wager_type(doc: dict) -> tuple[Optional[str], list[str]]:
    """Return (wager_type_or_None, reasons_if_ambiguous).

    - If canonical `wager_type` is already set → returned as-is.
    - Else, if legacy `bet_type` in {"straight","parlay"} → derive.
    - Else, if `parlay_legs` has ≥2 entries → parlay.
    - Else, if `parlay_legs` is empty AND `pick_id` present → straight.
    - Else → None + reason.
    """
    current = doc.get("wager_type")
    if current in UBL.CANONICAL_WAGER_TYPES:
        return current, []
    bt = doc.get("bet_type")
    if bt in UBL.CANONICAL_WAGER_TYPES:
        return bt, []
    parlay_legs = doc.get("parlay_legs") or []
    if isinstance(parlay_legs, list) and len(parlay_legs) >= 2:
        return UBL.WAGER_TYPE_PARLAY, []
    if isinstance(parlay_legs, list) and len(parlay_legs) == 0 and doc.get("pick_id"):
        return UBL.WAGER_TYPE_STRAIGHT, []
    return None, ["ambiguous wager_type — no bet_type, no parlay_legs, no pick_id"]


def _decide_user_bet_id(doc: dict) -> tuple[Optional[str], list[str]]:
    """Preserve the existing stable id when safe; otherwise report.

    - `user_bet_id` present → keep.
    - Else `id` present → adopt as `user_bet_id`.
    - Else → generate a NEW UUID and REPORT the rule.
    """
    if isinstance(doc.get("user_bet_id"), str) and doc["user_bet_id"]:
        return doc["user_bet_id"], []
    if isinstance(doc.get("id"), str) and doc["id"]:
        return doc["id"], []
    return str(uuid.uuid4()), [
        "user_bet_id generated: doc had no user_bet_id and no id"
    ]


def _is_native_row(doc: dict) -> bool:
    """A row is native (is_legacy=False) unless it carries any
    migration_source markers.  Documents predating Step 3 were only
    ever written by the /api/user/bets/track route → native."""
    if doc.get("migration_source"):
        return False
    if doc.get("is_legacy") is True:
        return False
    return True


def _row_missing_key(doc: dict, key: str) -> bool:
    """True if the row does not carry this canonical key OR the value
    stored is None.  We do NOT treat an explicit ``False`` / empty list
    / 0 as missing — only ``None`` and truly-absent keys."""
    if key not in doc:
        return True
    return doc.get(key) is None


def _combined_odds_from(doc: dict) -> Optional[int]:
    """For parlays with an existing ``odds_at_bet`` computed at
    creation time, that value IS the combined odds.  Straight bets have
    no combined_odds concept → None.  Never invents from legs."""
    if doc.get("bet_type") == UBL.WAGER_TYPE_PARLAY:
        legacy_odds = doc.get("odds_at_bet")
        if isinstance(legacy_odds, (int, float)):
            return int(legacy_odds)
    return None


def build_row_plan(doc: dict) -> RowPlan:
    """Compute the safe update dict for one document.

    Rules:
      • Never overwrite an existing non-None value.
      • Never invent values that have no derivable source.
      • Report any conflict where the existing value would need to be
        replaced (this never happens by construction — we skip when
        present — but we emit a conflict entry if the doc has a value
        that VIOLATES the canonical contract, e.g. wager_type outside
        the vocabulary).
    """
    user_id = doc.get("user_id")
    doc_id  = doc.get("id") or doc.get("user_bet_id") or ""
    plan = RowPlan(
        doc_id=str(doc_id),
        user_id=user_id,
        resume_key=str(doc_id or doc.get("_id")),
    )

    # ── Contract-conflict checks (existing value violates canonical) ─
    wt_current = doc.get("wager_type")
    if wt_current is not None and wt_current not in UBL.CANONICAL_WAGER_TYPES:
        plan.conflicts.append({
            "field": "wager_type", "current": wt_current,
            "reason": f"value outside canonical vocabulary {sorted(UBL.CANONICAL_WAGER_TYPES)}",
        })
    st_current = doc.get("status")
    if st_current is not None and st_current not in UBL.CANONICAL_STATUSES:
        # legacy status like "live" survives here because status is
        # OWNED by the existing route/settler; we NEVER rewrite it
        # during a schema-extension pass.  Just report it so the
        # operator can decide before writer cutover.
        if st_current != UBL.STATUS_PENDING:
            plan.manual_review_reasons.append(
                f"legacy status {st_current!r} — not remapped by this migration"
            )

    # ── Missing user_id is a hard manual-review flag ──────────────────
    if not user_id:
        plan.manual_review_reasons.append("row is missing user_id")

    # ── Compute the missing-fields dict ──────────────────────────────
    updates: dict[str, Any] = {}
    reasons_ambiguous: list[str] = []

    # Special-cased fields
    if _row_missing_key(doc, "user_bet_id"):
        ubid, notes = _decide_user_bet_id(doc)
        if ubid is not None:
            updates["user_bet_id"] = ubid
            plan.manual_review_reasons.extend(notes)

    if _row_missing_key(doc, "wager_type"):
        wt, notes = _decide_wager_type(doc)
        if wt is not None:
            updates["wager_type"] = wt
        else:
            plan.manual_review_reasons.extend(notes)

    if _row_missing_key(doc, "is_legacy"):
        updates["is_legacy"] = not _is_native_row(doc)

    if _row_missing_key(doc, "source"):
        # Only apply the "user_track" default to native rows (no
        # migration markers present).  For non-native rows we skip
        # (their source will be set by the migration script that
        # creates them, not by this schema-extension pass).
        if _is_native_row(doc):
            updates["source"] = "user_track"

    if _row_missing_key(doc, "combined_odds"):
        co = _combined_odds_from(doc)
        # co is None for straight bets → skip (never invent)
        if co is not None:
            updates["combined_odds"] = co

    # Generic fields
    for fname, spec in CANONICAL_FIELDS.items():
        if fname in ("user_bet_id", "wager_type", "is_legacy",
                     "source", "combined_odds"):
            continue    # handled above
        if not _row_missing_key(doc, fname):
            continue    # already populated — never overwrite
        src_key = spec["src"]
        default = spec["default"]
        # Prefer the source-field copy when present.
        if src_key is not None and src_key in doc and doc.get(src_key) is not None:
            updates[fname] = doc[src_key]
            continue
        # Otherwise use the documented default (unless _MISSING sentinel).
        if default is _MISSING:
            plan.manual_review_reasons.append(
                f"{fname}: no source value and no documented default"
            )
            continue
        updates[fname] = default

    plan.proposed_updates = updates
    return plan


# ═════════════════════════════════════════════════════════════════════
# Guardrails
# ═════════════════════════════════════════════════════════════════════
class ForbiddenWriteDetected(RuntimeError):
    pass


def _static_guard_no_forbidden_writes() -> None:
    """Fail-fast static guard: the module source must not reference
    any write operation against a forbidden collection.
    """
    src = Path(__file__).read_text(encoding="utf-8")
    for coll in FORBIDDEN_COLLECTIONS:
        for op in (".insert_one(", ".insert_many(", ".update_one(",
                   ".update_many(", ".delete_one(", ".delete_many(",
                   ".replace_one(", ".find_one_and_update(",
                   ".drop(", ".rename("):
            needle = f"{coll}{op}"
            if needle in src:
                raise ForbiddenWriteDetected(
                    f"static guard: source references forbidden write {needle}"
                )


_static_guard_no_forbidden_writes()


# ═════════════════════════════════════════════════════════════════════
# Main migration entrypoint
# ═════════════════════════════════════════════════════════════════════
async def run_migration(
    *,
    db,
    execute: bool,
    batch_size: int,
    limit: Optional[int],
    resume_from: Optional[str],
    user_id: Optional[str],
) -> BackfillReport:
    report = BackfillReport(
        started_at=_now_utc(),
        mode="execute" if execute else "dry-run",
        filter_user_id=user_id,
        resume_from=resume_from,
        limit=limit,
        batch_size=int(batch_size),
    )
    report.collection_counts_before = await _get_collection_counts(db)
    report.coverage_before = await _coverage_snapshot(db)

    coll = db[TARGET_COLLECTION]
    q: dict[str, Any] = {}
    if user_id:
        q["user_id"] = user_id
    # Resume anchor: process only rows whose id (string) is > resume_from
    if resume_from:
        q["id"] = {"$gt": resume_from}

    # Deterministic iteration order by ``id``.
    cursor = coll.find(q).sort("id", 1)
    if isinstance(batch_size, int) and batch_size > 0:
        cursor = cursor.batch_size(batch_size)

    processed = 0
    async for doc in cursor:
        if limit is not None and processed >= limit:
            break
        processed += 1
        report.total_scanned += 1

        plan = build_row_plan(doc)
        report.last_resume_key = plan.resume_key
        if plan.manual_review_reasons:
            report.total_manual_review += 1
            report.manual_review_rows.append(plan.to_report_row())
        if plan.conflicts:
            report.conflict_rows.append(plan.to_report_row())

        if not plan.proposed_updates:
            report.total_skipped += 1
            continue

        if not execute:
            # Dry-run: count as an update the operator WOULD do.
            report.total_updated += 1
            continue

        # Execute path: build a match filter that requires the target
        # keys to still be missing / null.  This makes the update
        # naturally race-safe against concurrent writers and also
        # makes the migration idempotent — a second run finds no
        # rows to update.
        write_filter: dict[str, Any] = {"id": doc.get("id")} if doc.get("id") else {"_id": doc.get("_id")}
        # Only $set keys that are provably missing right now.
        set_dict: dict[str, Any] = {}
        for k, v in plan.proposed_updates.items():
            # Add a per-field guard so a concurrent writer setting a
            # value would win over us.
            write_filter[f"{k}"] = write_filter.get(f"{k}") or None  # keep same match key
            # We build a compound filter that requires each key to be
            # ($exists:false OR value=None); if that changes between
            # the plan and the write, this row is skipped harmlessly.
            set_dict[k] = v
        # Compose the "still missing" filter conjunctively.
        conjunctive_filter: dict[str, Any] = {"id": doc.get("id")} if doc.get("id") else {"_id": doc.get("_id")}
        conjunctive_filter["$or"] = []  # placeholder; we use $and instead
        exists_terms: list[dict[str, Any]] = []
        for k in set_dict.keys():
            exists_terms.append({
                "$or": [
                    {k: {"$exists": False}},
                    {k: None},
                ],
            })
        if exists_terms:
            conjunctive_filter["$and"] = exists_terms
            del conjunctive_filter["$or"]
        else:
            del conjunctive_filter["$or"]

        result = await coll.update_one(conjunctive_filter, {"$set": set_dict})
        if result.modified_count == 1:
            report.total_updated += 1
        else:
            report.total_skipped += 1

    report.coverage_after = await _coverage_snapshot(db)
    report.collection_counts_after = await _get_collection_counts(db)

    # Verify forbidden collections were untouched.
    for coll_name in FORBIDDEN_COLLECTIONS:
        before = report.collection_counts_before.get(coll_name, 0)
        after  = report.collection_counts_after.get(coll_name, 0)
        if before != after:
            report.forbidden_touched.append(coll_name)

    report.finished_at = _now_utc()
    return report


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3G Step 3 — extend user_bets with canonical fields.",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True,
                     help="dry-run mode (default)")
    grp.add_argument("--execute", action="store_true", default=False,
                     help="perform writes")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--resume-from", type=str, default=None,
                   help="resume-from anchor (last user_bets.id processed)")
    p.add_argument("--report-path", type=str, default=None,
                   help="write JSON report to this path")
    p.add_argument("--user-id", type=str, default=None,
                   help="filter to a single user_id")
    return p.parse_args(argv)


async def _amain(argv: Optional[list[str]] = None) -> BackfillReport:
    args = _parse_args(argv)
    execute = bool(args.execute)
    # Explicit init on the shared owner so we don't lazy-init inside
    # the async loop.
    _shared_db.initialize_database()
    db = _shared_db.get_database()
    report = await run_migration(
        db=db,
        execute=execute,
        batch_size=int(args.batch_size),
        limit=(int(args.limit) if args.limit is not None else None),
        resume_from=args.resume_from,
        user_id=args.user_id,
    )
    payload = json.dumps(report.to_dict(), indent=2, default=str)
    print(payload)
    if args.report_path:
        Path(args.report_path).write_text(payload, encoding="utf-8")
    if report.forbidden_touched:
        raise ForbiddenWriteDetected(
            f"forbidden collections changed during run: {report.forbidden_touched}"
        )
    return report


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
