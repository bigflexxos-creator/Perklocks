"""migrate_parlay_history_p_to_user_bets — Phase 3G Step 4 dry-run.

Analysis-only tool that inspects legacy user-owned parlays in
``parlay_history`` (``p_*`` ids) and reports what would be inserted into
``user_bets`` **without performing a single write.**

Step 4 hard rules
─────────────────
* ``--dry-run`` is the default.
* ``--execute`` is DEFINED but HARD-DISABLED in Step 4.  If passed, the
  script prints a rejection message and exits with a non-zero code.
* Only ``parlay_history`` is read.  ``user_bets`` is read (never
  written) to detect duplicates.  Every other collection is completely
  untouched, including ``prediction_snapshots`` and ``settlement_events``.
* The pure Step 2 mapper (`services.user_bet_ledger.map_legacy_user_parlay`)
  is the SINGLE source of truth for legacy-to-canonical mapping.  This
  script does not re-implement mapping logic.
* Uses the Phase 3B shared DB lifecycle — no independent Mongo client.

Classifications (every eligible row lands in exactly one):
  • migration_ready         — safe to insert as-is on execute
  • duplicate_existing      — already migrated (primary key match, OR
                               high-confidence secondary match)
  • manual_review           — mapping succeeded but the row has an
                               ambiguity that a human must resolve
  • unsafe                  — mapping raised OR the row has a hard
                               contract violation
  • excluded_learning       — ``plearn_*`` row (never touched)
  • excluded_missing_user   — no ``user_id``
  • excluded_invalid_structure — missing ``id`` prefix / < 2 legs / etc.
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
from services import index_registry as IR

logger = logging.getLogger("lockscore.backfill.step4_dry_run")

SOURCE_COLLECTION = "parlay_history"
TARGET_COLLECTION = "user_bets"
FORBIDDEN_MUTATION_COLLECTIONS = ("parlay_history", "prediction_snapshots",
                                  "settlement_events")

# Classification labels
C_MIGRATION_READY               = "migration_ready"
C_DUPLICATE_EXISTING            = "duplicate_existing"
C_MANUAL_REVIEW                 = "manual_review"
C_UNSAFE                        = "unsafe"
C_EXCLUDED_LEARNING             = "excluded_learning"
C_EXCLUDED_MISSING_USER         = "excluded_missing_user"
C_EXCLUDED_INVALID_STRUCTURE    = "excluded_invalid_structure"

CLASSIFICATIONS = (
    C_MIGRATION_READY, C_DUPLICATE_EXISTING, C_MANUAL_REVIEW, C_UNSAFE,
    C_EXCLUDED_LEARNING, C_EXCLUDED_MISSING_USER, C_EXCLUDED_INVALID_STRUCTURE,
)

# Required unique index that Step 5 (--execute) will depend on.
MIGRATION_INDEX_NAME = "migration_source_1_migration_source_id_1_uniq_partial"


# ═════════════════════════════════════════════════════════════════════
# Data shapes
# ═════════════════════════════════════════════════════════════════════
@dataclass
class RowClassification:
    legacy_id:               str
    user_id_present:         bool
    original_status:         Optional[str]
    canonical_status:        Optional[str]
    leg_count:               int
    stake_coverage:          bool
    combined_odds_coverage:  bool
    payout_coverage:         bool
    profit_loss_coverage:    bool
    prediction_id_leg_cov:   str    # e.g. "3/3"
    snapshot_id_leg_cov:     str
    market_contract_leg_cov: str
    exact_line_leg_cov:      str
    original_odds_leg_cov:   str
    proposed_user_bet_id:    Optional[str]
    migration_source_id:     Optional[str]
    duplicate_match:         Optional[str]     # None | "primary" | "secondary"
    classification:          str
    reasons:                 list[str] = field(default_factory=list)
    warnings:                list[str] = field(default_factory=list)


@dataclass
class IndexPreflight:
    ok:                      bool
    name:                    str
    present:                 bool
    keys_match:              bool
    unique:                  bool
    partial_filter_present:  bool
    conflict_note:           Optional[str]
    registry_spec_summary:   dict[str, Any]
    live_index_summary:      Optional[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DryRunReport:
    started_at:               datetime
    finished_at:               Optional[datetime] = None
    filter_user_id_present:    bool                = False
    resume_from:               Optional[str]       = None
    limit:                     Optional[int]       = None
    batch_size:                int                 = 200
    include_manual_review_in_output: bool          = True
    total_parlay_history:      int                 = 0
    excluded_plearn:           int                 = 0
    eligible_p_star:           int                 = 0
    counts_by_classification:  dict[str, int]      = field(default_factory=dict)
    classifications:           list[dict]          = field(default_factory=list)
    status_mapping_breakdown:  dict[str, int]      = field(default_factory=dict)
    payout_gaps:               dict[str, int]      = field(default_factory=dict)
    leg_identity_coverage:     dict[str, str]      = field(default_factory=dict)
    prediction_id_leg_ratio:   str                 = "0/0"
    snapshot_id_leg_ratio:     str                 = "0/0"
    index_preflight:           dict[str, Any]      = field(default_factory=dict)
    ledger_preflight:          dict[str, Any]      = field(default_factory=dict)
    collection_counts_before:  dict[str, int]      = field(default_factory=dict)
    collection_counts_after:   dict[str, int]      = field(default_factory=dict)
    zero_write_verified:       bool                = False
    forbidden_mutations:       list[str]           = field(default_factory=list)
    production_execute_blocked: bool               = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("started_at", "finished_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        return d


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _collection_counts(db) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in (TARGET_COLLECTION, SOURCE_COLLECTION,
                 "prediction_snapshots", "settlement_events"):
        try:
            out[name] = await db[name].count_documents({})
        except Exception:
            out[name] = -1
    return out


def _leg_id_coverage(legs: list[dict], key: str) -> tuple[int, int]:
    if not isinstance(legs, list):
        return (0, 0)
    total = len(legs)
    present = sum(
        1 for L in legs
        if isinstance(L, dict) and L.get(key) not in (None, "", 0)
    )
    return present, total


def _ratio(present: int, total: int) -> str:
    return f"{present}/{total}"


def _classify_learning_or_invalid(doc: dict) -> Optional[str]:
    """Return an exclusion classification when the row is not an
    eligible user parlay, or None when the row is eligible."""
    if UBL.is_learning_row(doc):
        return C_EXCLUDED_LEARNING
    uid = doc.get("user_id")
    if not (isinstance(uid, str) and uid):
        return C_EXCLUDED_MISSING_USER
    id_ = doc.get("id") or ""
    if not (isinstance(id_, str) and id_.startswith("p_")):
        return C_EXCLUDED_INVALID_STRUCTURE
    leg_ids = doc.get("leg_ids") or []
    legs    = doc.get("legs") or []
    if len(leg_ids) < 2 and len(legs) < 2:
        return C_EXCLUDED_INVALID_STRUCTURE
    return None


def _compute_payout_pnl_verdict(canonical, legacy) -> tuple[str, list[str]]:
    """Given a mapped :class:`UserBet`, decide whether the row is
    migration-ready from a payout/PnL perspective.

    Returns ``(classification_hint, warnings)`` where
    ``classification_hint`` is one of:
      • ""                — no payout/pnl concern
      • "manual_review"   — settlement meaning incomplete
    """
    warns: list[str] = []
    status = canonical.status
    stake  = canonical.stake_amount
    payout = canonical.actual_payout
    pnl    = canonical.profit_loss
    combined = canonical.combined_odds

    if status == UBL.STATUS_WON:
        # Legacy source truth: was ``payout`` actually captured?
        legacy_payout = legacy.get("payout") if isinstance(legacy, dict) else None
        legacy_pnl    = legacy.get("profit_loss") if isinstance(legacy, dict) else None
        legacy_actual = legacy.get("actual_payout") if isinstance(legacy, dict) else None
        if (legacy_payout is not None
            or legacy_pnl is not None
            or legacy_actual is not None):
            # Existing settlement meaning is present in the source row.
            return "", warns
        # Legacy row had no payout / profit_loss / actual_payout even
        # though status=won.  The Step 2 mapper filled ``profit_loss``
        # via the American-odds formula — Step 4 policy requires human
        # review of that fallback before it lands in production.
        warns.append(
            "won row: legacy payout / profit_loss / actual_payout all null; "
            "Step 2 mapper would fall back to the American-odds formula on "
            "execute — Step 4 policy is manual_review until the operator "
            "confirms the formula matches current app behaviour"
        )
        return C_MANUAL_REVIEW, warns

    if status == UBL.STATUS_LOST:
        if pnl is not None or isinstance(stake, (int, float)):
            return "", warns
        warns.append("lost row lacks both profit_loss and stake")
        return C_MANUAL_REVIEW, warns

    if status in (UBL.STATUS_PUSHED, UBL.STATUS_VOID, UBL.STATUS_CANCELLED):
        # profit_loss is 0.0 by construction in the mapper.
        return "", warns

    if status == UBL.STATUS_PENDING:
        # Pending row — no payout expected.
        return "", warns

    if status == UBL.STATUS_UNKNOWN:
        warns.append(f"unknown canonical status from legacy {legacy.get('status')!r}")
        return C_MANUAL_REVIEW, warns

    return "", warns


# ═════════════════════════════════════════════════════════════════════
# Index preflight
# ═════════════════════════════════════════════════════════════════════
async def preflight_migration_index(db) -> IndexPreflight:
    """Verify the migration_source_1_migration_source_id_1_uniq_partial
    index exists on ``user_bets`` and matches the Phase 3C registry
    definition.  Never creates or modifies indexes."""
    spec = next(
        (s for s in IR.get_specs_for_collection(TARGET_COLLECTION)
         if s.name == MIGRATION_INDEX_NAME),
        None,
    )
    if spec is None:
        return IndexPreflight(
            ok=False,
            name=MIGRATION_INDEX_NAME,
            present=False,
            keys_match=False,
            unique=False,
            partial_filter_present=False,
            conflict_note="spec not found in registry",
            registry_spec_summary={},
            live_index_summary=None,
        )

    spec_summary = {
        "name":            spec.name,
        "keys":            list(spec.keys),
        "unique":          bool(spec.unique),
        "partial_filter":  spec.partial_filter,
    }

    try:
        live = await db[TARGET_COLLECTION].index_information()
    except Exception as e:
        return IndexPreflight(
            ok=False, name=MIGRATION_INDEX_NAME, present=False,
            keys_match=False, unique=False, partial_filter_present=False,
            conflict_note=f"index_information failed: {e}",
            registry_spec_summary=spec_summary, live_index_summary=None,
        )

    info = live.get(MIGRATION_INDEX_NAME)
    if info is None:
        return IndexPreflight(
            ok=False, name=MIGRATION_INDEX_NAME, present=False,
            keys_match=False, unique=False, partial_filter_present=False,
            conflict_note="index missing on live collection",
            registry_spec_summary=spec_summary, live_index_summary=None,
        )

    keys_pairs = [(k, int(v)) for k, v in info.get("key", [])]
    keys_match = keys_pairs == list(spec.keys)
    unique     = bool(info.get("unique", False))
    partial_present = "partialFilterExpression" in info

    ok = keys_match and unique and partial_present
    conflict_note = None
    if not ok:
        parts = []
        if not keys_match: parts.append(f"keys mismatch: live={keys_pairs} spec={list(spec.keys)}")
        if not unique:     parts.append("live index is not unique")
        if not partial_present: parts.append("live index has no partial filter")
        conflict_note = "; ".join(parts)

    return IndexPreflight(
        ok=ok,
        name=MIGRATION_INDEX_NAME,
        present=True,
        keys_match=keys_match,
        unique=unique,
        partial_filter_present=partial_present,
        conflict_note=conflict_note,
        registry_spec_summary=spec_summary,
        live_index_summary={
            "keys":                   keys_pairs,
            "unique":                 unique,
            "partial_filter_present": partial_present,
        },
    )


# ═════════════════════════════════════════════════════════════════════
# Duplicate detection
# ═════════════════════════════════════════════════════════════════════
async def _find_existing_by_primary(coll, *, legacy_id: str) -> Optional[dict]:
    return await coll.find_one({
        "migration_source":    "parlay_history",
        "migration_source_id": legacy_id,
    })


async def _find_existing_by_high_confidence(coll, *, user_id: str,
                                            leg_ids_sorted: list[str],
                                            placed_at: Optional[datetime],
                                            combined_odds: Optional[int],
                                            stake: Optional[float]) -> Optional[dict]:
    """Secondary detection — high-confidence only.  Requires ALL of:
    same user, same sorted leg_ids, same placed_at (to the minute), and
    same combined_odds AND stake when both present."""
    if not leg_ids_sorted:
        return None
    q: dict[str, Any] = {
        "user_id":     user_id,
        "wager_type":  UBL.WAGER_TYPE_PARLAY,
        # Order-independent equality via sorted-list comparison.
        "parlay_legs": leg_ids_sorted,
    }
    if combined_odds is not None:
        q["combined_odds"] = combined_odds
    if stake is not None:
        q["stake_amount"] = stake
    if placed_at is not None:
        lo = placed_at.replace(second=0, microsecond=0)
        hi_ts = lo.timestamp() + 60
        hi = datetime.fromtimestamp(hi_ts, tz=timezone.utc)
        q["placed_at"] = {"$gte": lo, "$lt": hi}
    return await coll.find_one(q)


# ═════════════════════════════════════════════════════════════════════
# Row analysis
# ═════════════════════════════════════════════════════════════════════
async def analyse_row(coll_target, doc: dict) -> RowClassification:
    legacy_id = str(doc.get("id") or "")
    exclusion = _classify_learning_or_invalid(doc)
    legs = doc.get("legs") or []
    leg_ids = doc.get("leg_ids") or []
    leg_count = len(legs) if legs else len(leg_ids)

    rc = RowClassification(
        legacy_id=legacy_id,
        user_id_present=bool(doc.get("user_id")),
        original_status=doc.get("status"),
        canonical_status=None,
        leg_count=int(leg_count),
        stake_coverage=doc.get("stake") is not None,
        combined_odds_coverage=doc.get("combined_odds") is not None,
        payout_coverage=doc.get("payout") is not None,
        profit_loss_coverage=False,
        prediction_id_leg_cov=_ratio(*_leg_id_coverage(legs, "pick_id")),
        snapshot_id_leg_cov=_ratio(0, len(legs)),          # never set on legacy
        market_contract_leg_cov=_ratio(0, len(legs)),      # never set on legacy
        exact_line_leg_cov=_ratio(*_leg_id_coverage(legs, "line")),
        original_odds_leg_cov=_ratio(*_leg_id_coverage(legs, "book_odds")),
        proposed_user_bet_id=None,
        migration_source_id=legacy_id if legacy_id else None,
        duplicate_match=None,
        classification=exclusion or C_MIGRATION_READY,
    )

    if exclusion is not None:
        rc.reasons.append(f"excluded: {exclusion}")
        return rc

    # Attempt mapping via the pure Step 2 mapper.
    try:
        bet = UBL.map_legacy_user_parlay(doc)
    except UBL.LegacyRowNotEligible as e:
        rc.classification = C_EXCLUDED_INVALID_STRUCTURE
        rc.reasons.append(f"mapper rejected: {e}")
        return rc
    except Exception as e:
        rc.classification = C_UNSAFE
        rc.reasons.append(f"mapper raised: {type(e).__name__}: {e}")
        return rc

    rc.canonical_status = bet.status
    rc.profit_loss_coverage = bet.profit_loss is not None
    rc.proposed_user_bet_id = bet.user_bet_id

    # Enforce guardrail: void ≠ pushed and vice versa.
    if doc.get("status") == "void" and rc.canonical_status != UBL.STATUS_VOID:
        rc.classification = C_UNSAFE
        rc.reasons.append("void collapsed to pushed — hard contract violation")
        return rc
    if doc.get("status") == "push" and rc.canonical_status != UBL.STATUS_PUSHED:
        rc.classification = C_UNSAFE
        rc.reasons.append("push collapsed to void — hard contract violation")
        return rc

    # Unknown canonical status → manual_review immediately.
    if rc.canonical_status == UBL.STATUS_UNKNOWN:
        rc.classification = C_MANUAL_REVIEW
        rc.reasons.append(f"unknown canonical status from legacy {doc.get('status')!r}")
        return rc

    # Duplicate detection.
    primary = await _find_existing_by_primary(coll_target, legacy_id=legacy_id)
    if primary is not None:
        rc.duplicate_match = "primary"
        rc.classification = C_DUPLICATE_EXISTING
        rc.reasons.append("migration_source + migration_source_id already exists")
        return rc

    leg_ids_sorted = sorted(leg_ids) if leg_ids else []
    secondary = await _find_existing_by_high_confidence(
        coll_target,
        user_id=str(doc.get("user_id")),
        leg_ids_sorted=leg_ids_sorted,
        placed_at=bet.placed_at,
        combined_odds=bet.combined_odds,
        stake=bet.stake_amount,
    )
    if secondary is not None:
        # High-confidence duplicate — same user + same sorted legs +
        # same placed_at + same combined_odds/stake if those were both
        # present.  Never based on display text.
        rc.duplicate_match = "secondary"
        rc.classification = C_DUPLICATE_EXISTING
        rc.reasons.append("high-confidence secondary match found")
        return rc

    # Payout / PnL verdict.
    hint, warns = _compute_payout_pnl_verdict(bet, doc)
    rc.warnings.extend(warns)
    if hint == C_MANUAL_REVIEW:
        rc.classification = C_MANUAL_REVIEW
        return rc

    rc.classification = C_MIGRATION_READY
    return rc


# ═════════════════════════════════════════════════════════════════════
# Main dry-run
# ═════════════════════════════════════════════════════════════════════
async def run_dry_run(
    *,
    db,
    batch_size: int,
    limit: Optional[int],
    resume_from: Optional[str],
    user_id: Optional[str],
    include_manual_review: bool,
    verbose: bool,
) -> DryRunReport:
    report = DryRunReport(
        started_at=_now_utc(),
        filter_user_id_present=user_id is not None,
        resume_from=resume_from,
        limit=limit,
        batch_size=int(batch_size),
        include_manual_review_in_output=include_manual_review,
    )
    src = db[SOURCE_COLLECTION]
    tgt = db[TARGET_COLLECTION]

    # ── Preflight ────────────────────────────────────────────────────
    preflight = await preflight_migration_index(db)
    report.index_preflight = preflight.to_dict()

    ledger_pref = await UBL.preflight_unique_indexes(db=db)
    report.ledger_preflight = ledger_pref.to_dict()

    # ── Baseline counts ──────────────────────────────────────────────
    report.collection_counts_before = await _collection_counts(db)
    report.total_parlay_history = int(report.collection_counts_before.get(SOURCE_COLLECTION, 0))

    # ── Scan ────────────────────────────────────────────────────────
    q: dict[str, Any] = {}
    if user_id:
        q["user_id"] = user_id
    if resume_from:
        q["id"] = {"$gt": resume_from}

    counts: dict[str, int] = {c: 0 for c in CLASSIFICATIONS}
    status_breakdown: dict[str, int] = {}
    payout_gaps = {"won_missing_payout": 0, "lost_missing_stake": 0}
    pid_present_total = 0
    pid_total_total = 0
    snap_present_total = 0
    snap_total_total = 0
    mc_present_total = 0
    mc_total_total = 0
    ln_present_total = 0
    ln_total_total = 0
    od_present_total = 0
    od_total_total = 0

    cursor = src.find(q).sort("id", 1)
    if batch_size and batch_size > 0:
        cursor = cursor.batch_size(int(batch_size))

    processed = 0
    async for doc in cursor:
        if limit is not None and processed >= limit:
            break
        processed += 1
        rc = await analyse_row(tgt, doc)
        counts[rc.classification] = counts.get(rc.classification, 0) + 1
        if rc.classification == C_EXCLUDED_LEARNING:
            report.excluded_plearn += 1
            continue

        legs = doc.get("legs") or []
        p, t = _leg_id_coverage(legs, "pick_id")
        pid_present_total += p; pid_total_total += t
        p, t = _leg_id_coverage(legs, "line")
        ln_present_total += p; ln_total_total += t
        p, t = _leg_id_coverage(legs, "book_odds")
        od_present_total += p; od_total_total += t
        # snapshot_id and market_contract_id are never present on legacy legs.
        snap_total_total += len(legs)
        mc_total_total += len(legs)

        if rc.classification.startswith("excluded_"):
            continue

        report.eligible_p_star += 1

        # Status mapping breakdown (only for eligible rows).
        if rc.original_status is not None:
            legacy_key = f"legacy:{rc.original_status}"
            status_breakdown[legacy_key] = status_breakdown.get(legacy_key, 0) + 1
        if rc.canonical_status is not None:
            canon_key = f"canonical:{rc.canonical_status}"
            status_breakdown[canon_key] = status_breakdown.get(canon_key, 0) + 1

        # Payout gap counts (only for eligible rows).
        if rc.canonical_status == UBL.STATUS_WON and not rc.payout_coverage:
            payout_gaps["won_missing_payout"] += 1
        if rc.canonical_status == UBL.STATUS_LOST and not rc.stake_coverage:
            payout_gaps["lost_missing_stake"] += 1

        # Emit into the classifications list — but never emit any raw
        # user_id.  Only IDs, coverage, and analytic metadata.
        if include_manual_review or rc.classification != C_MANUAL_REVIEW:
            report.classifications.append(asdict(rc))

    # Aggregate leg identity coverage
    report.leg_identity_coverage = {
        "prediction_id":      _ratio(pid_present_total, pid_total_total),
        "snapshot_id":        _ratio(snap_present_total, snap_total_total),
        "market_contract_id": _ratio(mc_present_total, mc_total_total),
        "exact_line":         _ratio(ln_present_total, ln_total_total),
        "original_odds":      _ratio(od_present_total, od_total_total),
    }
    report.prediction_id_leg_ratio = report.leg_identity_coverage["prediction_id"]
    report.snapshot_id_leg_ratio   = report.leg_identity_coverage["snapshot_id"]
    report.counts_by_classification = counts
    report.status_mapping_breakdown = status_breakdown
    report.payout_gaps = payout_gaps

    # ── After counts + zero-write verification ──────────────────────
    report.collection_counts_after = await _collection_counts(db)
    forbidden = []
    for c in FORBIDDEN_MUTATION_COLLECTIONS:
        b = report.collection_counts_before.get(c, 0)
        a = report.collection_counts_after.get(c, 0)
        if b != a:
            forbidden.append(c)
    # user_bets must also be unchanged in count and in doc hashes,
    # but at count level we already verify here.
    if report.collection_counts_before.get(TARGET_COLLECTION) != \
       report.collection_counts_after.get(TARGET_COLLECTION):
        forbidden.append(TARGET_COLLECTION)
    report.forbidden_mutations = forbidden
    report.zero_write_verified = (len(forbidden) == 0)

    report.finished_at = _now_utc()
    return report


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 3G Step 4 — dry-run legacy p_* → user_bets analysis.",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True)
    grp.add_argument("--execute", action="store_true", default=False,
                     help="HARD-DISABLED in Step 4 — the script will refuse.")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--resume-from", type=str, default=None)
    p.add_argument("--user-id", type=str, default=None)
    p.add_argument("--report-path", type=str, default=None)
    p.add_argument("--include-manual-review", action="store_true", default=True)
    p.add_argument("--verbose", action="store_true", default=False)
    return p.parse_args(argv)


class ExecuteRefused(RuntimeError):
    pass


async def _amain(argv: Optional[list[str]] = None) -> DryRunReport:
    args = _parse_args(argv)
    if args.execute:
        msg = (
            "Phase 3G Step 4: --execute is HARD-DISABLED. "
            "Production execution of the legacy p_* → user_bets migration "
            "is not approved. Rerun without --execute to produce the "
            "dry-run report."
        )
        print(msg, file=sys.stderr)
        raise ExecuteRefused(msg)
    _shared_db.initialize_database()
    db = _shared_db.get_database()
    report = await run_dry_run(
        db=db,
        batch_size=int(args.batch_size),
        limit=(int(args.limit) if args.limit is not None else None),
        resume_from=args.resume_from,
        user_id=args.user_id,
        include_manual_review=bool(args.include_manual_review),
        verbose=bool(args.verbose),
    )
    payload = json.dumps(report.to_dict(), indent=2, default=str)
    if args.verbose:
        print(payload)
    else:
        # Concise summary; the report-path file holds the full JSON.
        summary = {
            "eligible_p_star":            report.eligible_p_star,
            "excluded_plearn":            report.excluded_plearn,
            "counts_by_classification":   report.counts_by_classification,
            "index_preflight_ok":         report.index_preflight.get("ok"),
            "zero_write_verified":        report.zero_write_verified,
            "forbidden_mutations":        report.forbidden_mutations,
        }
        print(json.dumps(summary, indent=2, default=str))
    if args.report_path:
        Path(args.report_path).write_text(payload, encoding="utf-8")

    if not report.zero_write_verified:
        raise RuntimeError(
            f"zero-write invariant violated: forbidden={report.forbidden_mutations}"
        )
    return report


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(_amain())
    except ExecuteRefused:
        sys.exit(2)


if __name__ == "__main__":
    main()
