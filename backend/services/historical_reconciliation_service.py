"""P0.2e — HistoricalReconciliationService.

Canonical reconciliation boundary for legacy / historical pick records.

Design principle (spec §1):

  PROVEN historical truth   → reconcile
  UNPROVEN historical truth → remain unresolved
  AMBIGUOUS identity        → fail closed
  CONFLICTING legacy truth  → canonical wins

The service is a **classifier over existing canonical inputs**.  It
does NOT:

  * settle picks             (SettlementService owns settlement)
  * re-score picks           (Lock Score / Magic / APEX untouched)
  * recompute pregame truth  (published snapshots preserved verbatim)
  * fabricate missing fields (v0-gap remains honest)
  * republish to the current board

It DOES:

  * classify each legacy pick against the canonical stack
    (settlement_events + prediction_snapshots)
  * emit a deterministic reconciliation outcome vocabulary
  * accumulate a hygiene report with counts + samples
  * optionally attach an idempotent `reconciliation_provenance`
    field to the pick when `dry_run=False`

Reconciliation-outcome vocabulary (spec §15):

  CANONICAL_ALREADY              — pick already carries canonical
                                    settlement provenance + snapshot
                                    from P0.2b/c pipelines; nothing to
                                    do.
  RECONCILED                     — canonical settlement exists and the
                                    legacy compat mirror agrees; row
                                    tagged with reconciliation
                                    provenance.
  CANONICAL_CONFLICT_CANONICAL_WINS  — legacy `pick.status` disagrees
                                    with canonical `settlement_events`
                                    active row; canonical wins.  Legacy
                                    value retained as provenance.
  MISSING_PREGAME_SNAPSHOT       — no `prediction_snapshots` row for
                                    the pick; frozen pregame values
                                    remain unavailable, snapshot is
                                    NOT fabricated.
  LEGACY_ONLY_UNPROVEN           — legacy row appears settled but no
                                    canonical event exists; refuse to
                                    promote legacy → canonical.
  UNRESOLVED_IDENTITY            — pick lacks a canonical id.
  UNRESOLVED_EVENT               — pick lacks a canonical event id.
  LEGACY_DEAD                    — retired product (e.g. KBO); ignore.

The service is exhaustively covered by the P0.2e test matrix.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


# ─── Reconciliation-outcome vocabulary ─────────────────────────────
CANONICAL_ALREADY               = "CANONICAL_ALREADY"
RECONCILED                      = "RECONCILED"
CANONICAL_CONFLICT_CANONICAL_WINS = "CANONICAL_CONFLICT_CANONICAL_WINS"
MISSING_PREGAME_SNAPSHOT        = "MISSING_PREGAME_SNAPSHOT"
LEGACY_ONLY_UNPROVEN            = "LEGACY_ONLY_UNPROVEN"
UNRESOLVED_IDENTITY             = "UNRESOLVED_IDENTITY"
UNRESOLVED_EVENT                = "UNRESOLVED_EVENT"
LEGACY_DEAD                     = "LEGACY_DEAD"

OUTCOMES = (
    CANONICAL_ALREADY,
    RECONCILED,
    CANONICAL_CONFLICT_CANONICAL_WINS,
    MISSING_PREGAME_SNAPSHOT,
    LEGACY_ONLY_UNPROVEN,
    UNRESOLVED_IDENTITY,
    UNRESOLVED_EVENT,
    LEGACY_DEAD,
)


# ─── Identity resolution (spec §5) ─────────────────────────────────
def _canonical_pick_id(pick: dict) -> str:
    return (pick.get("id")
            or pick.get("canonical_pick_id")
            or pick.get("prediction_id")
            or "")


def _canonical_event_id(pick: dict) -> str:
    return (pick.get("event_id")
            or pick.get("fanduel_event_id")
            or pick.get("canonical_event_id")
            or "")


# ─── Retired-product detection ─────────────────────────────────────
_LEGACY_DEAD_MARKERS = (
    # KBO was retired in P0.2b — no picks are generated, no
    # canonical events land.  Historical rows that reference KBO
    # should be classified LEGACY_DEAD, not falsely reconciled.
    ("league", "kbo"),
    ("sport", "kbo"),
    ("sport", "KBO"),
)


def _is_legacy_dead(pick: dict) -> bool:
    for field, needle in _LEGACY_DEAD_MARKERS:
        val = pick.get(field)
        if isinstance(val, str) and needle.lower() in val.lower():
            return True
    return False


# ─── Legacy vs canonical result comparison ─────────────────────────
def _canonical_result_for(pick: dict, canonical_event: dict) -> str:
    """The canonical settlement result string.  Preserves PUSH vs VOID."""
    r = (canonical_event.get("result") or "").strip().lower()
    return r


def _legacy_result_for(pick: dict) -> str:
    """The best available legacy result string from the pick doc.
    We check both `result` and `status` because pre-P0.2b writers
    populated one or the other inconsistently.
    """
    for key in ("result", "status"):
        v = pick.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


# ─── Idempotent provenance write (spec §9 + §18) ───────────────────
def _build_provenance(
    outcome: str,
    canonical_event: Optional[dict],
    legacy_result: str,
    snapshot_present: bool,
) -> dict:
    """Deterministic reconciliation-provenance blob attached to the
    pick.  Same inputs → same output → idempotent writes."""
    blob = {
        "outcome":            outcome,
        "reconciler_version": "p02e.v1",
        "legacy_result":      legacy_result or None,
        "snapshot_present":   bool(snapshot_present),
    }
    if canonical_event:
        blob["canonical_settlement_id"] = canonical_event.get("settlement_id")
        blob["canonical_result"]        = canonical_event.get("result")
        blob["canonical_version"]       = canonical_event.get("settlement_version")
        blob["canonical_supersedes"]    = canonical_event.get(
            "supersedes_settlement_id")
    return blob


class HistoricalReconciliationService:
    """P0.2e canonical reconciliation classifier.

    Args:
        db: motor client (or in-memory fake exposing the same shape).
        dry_run: default True.  When False the service attaches an
                 idempotent ``reconciliation_provenance`` field to
                 each classified pick doc via ``update_one`` (no
                 upsert).  Canonical `settlement_events` and
                 `prediction_snapshots` are NEVER mutated.
    """

    def __init__(self, db, *, dry_run: bool = True):
        self.db = db
        self.dry_run = dry_run

    # ── Canonical lookups ─────────────────────────────────────────
    async def _get_active_settlement(self, pid: str) -> Optional[dict]:
        if not pid:
            return None
        row = await self.db["settlement_events"].find_one(
            {"prediction_id": pid, "is_active": True},
            {"_id": 0},
        )
        return row

    async def _get_snapshot(self, pid: str) -> Optional[dict]:
        if not pid:
            return None
        row = await self.db["prediction_snapshots"].find_one(
            {"prediction_id": pid, "is_active": True},
            {"_id": 0},
        )
        return row

    # ── Single-pick classification (pure, no writes) ──────────────
    async def classify(self, pick: dict) -> dict:
        """Return `{'outcome': <str>, 'provenance': <dict>}` for a
        single pick.  Pure — no writes even when
        ``self.dry_run is False``.
        """
        # §14 — retired product
        if _is_legacy_dead(pick):
            return {
                "outcome":    LEGACY_DEAD,
                "provenance": _build_provenance(LEGACY_DEAD, None,
                                                  _legacy_result_for(pick),
                                                  False),
            }

        pid = _canonical_pick_id(pick)
        if not pid:
            # §5 — no canonical id, cannot reconcile at all.
            return {
                "outcome":    UNRESOLVED_IDENTITY,
                "provenance": _build_provenance(UNRESOLVED_IDENTITY, None,
                                                  _legacy_result_for(pick),
                                                  False),
            }

        eid = _canonical_event_id(pick)
        if not eid:
            return {
                "outcome":    UNRESOLVED_EVENT,
                "provenance": _build_provenance(UNRESOLVED_EVENT, None,
                                                  _legacy_result_for(pick),
                                                  False),
            }

        canonical_event = await self._get_active_settlement(pid)
        snapshot        = await self._get_snapshot(pid)
        legacy_result   = _legacy_result_for(pick)

        # §4 Case A / §7 — canonical exists
        if canonical_event is not None:
            canonical_result = _canonical_result_for(pick, canonical_event)
            # §4 Case A — legacy disagrees with canonical
            if legacy_result and legacy_result != canonical_result and \
               legacy_result in ("won", "lost", "push", "void"):
                outcome = CANONICAL_CONFLICT_CANONICAL_WINS
            elif pick.get("_reconciled_at") or \
                 pick.get("reconciliation_provenance"):
                outcome = CANONICAL_ALREADY
            else:
                outcome = RECONCILED
            return {
                "outcome":    outcome,
                "provenance": _build_provenance(
                    outcome, canonical_event,
                    legacy_result, snapshot is not None),
            }

        # §4 Case B — canonical missing.  If the legacy row claims an
        # outcome, refuse to promote it.
        if legacy_result in ("won", "lost", "push", "void"):
            return {
                "outcome":    LEGACY_ONLY_UNPROVEN,
                "provenance": _build_provenance(LEGACY_ONLY_UNPROVEN, None,
                                                  legacy_result,
                                                  snapshot is not None),
            }

        # No settlement and no legacy outcome — usually an active or
        # unsettled pick.  Report snapshot presence honestly.
        if snapshot is None:
            return {
                "outcome":    MISSING_PREGAME_SNAPSHOT,
                "provenance": _build_provenance(MISSING_PREGAME_SNAPSHOT,
                                                  None, legacy_result, False),
            }
        return {
            "outcome":    MISSING_PREGAME_SNAPSHOT,
            "provenance": _build_provenance(MISSING_PREGAME_SNAPSHOT, None,
                                              legacy_result, True),
        }

    # ── Batch classification + optional idempotent writes ─────────
    async def reconcile(self, picks: Iterable[dict]) -> dict:
        """Classify every pick and, when ``dry_run=False``, attach an
        idempotent ``reconciliation_provenance`` blob to each doc.

        Returns a hygiene report:
            {
              "total":               <int>,
              "outcomes":            {<outcome>: <count>},
              "sample":              {<outcome>: [<pick_id>, ...]},
              "wrote":               <int>,        # 0 when dry_run
              "dry_run":             <bool>,
            }
        """
        report: dict = {
            "total":      0,
            "outcomes":   {o: 0 for o in OUTCOMES},
            "sample":     {o: [] for o in OUTCOMES},
            "wrote":      0,
            "dry_run":    self.dry_run,
        }
        for pick in picks:
            report["total"] += 1
            classification = await self.classify(pick)
            outcome    = classification["outcome"]
            provenance = classification["provenance"]
            report["outcomes"][outcome] = report["outcomes"].get(outcome, 0) + 1
            sample_bucket = report["sample"].setdefault(outcome, [])
            if len(sample_bucket) < 5:
                sample_bucket.append(_canonical_pick_id(pick) or "<no-id>")
            if self.dry_run:
                continue
            # §18 — write only reconciliation_provenance to `picks`;
            # never mutate settlement_events / prediction_snapshots.
            # Idempotency (§9): read the *stored* pick row to see if
            # this exact provenance blob is already attached; skip if
            # so.  This also handles callers that reuse the same
            # in-memory pick dict across calls.
            pid = _canonical_pick_id(pick)
            if not pid:
                continue
            try:
                stored_row = await self.db["picks"].find_one(
                    {"id": pid}, {"reconciliation_provenance": 1,
                                    "_id": 0})
            except Exception:
                stored_row = None
            existing = (stored_row or {}).get(
                "reconciliation_provenance") or pick.get(
                "reconciliation_provenance") or {}
            if existing == provenance:
                # Already reconciled with the same result — no-op.
                continue
            try:
                await self.db["picks"].update_one(
                    {"id": pid},
                    {"$set": {"reconciliation_provenance": provenance}},
                )
                report["wrote"] += 1
            except Exception:
                pass
        return report

    # ── Convenience dry-run over the whole collection ─────────────
    async def report_all(self,
                          query: Optional[dict] = None) -> dict:
        """Dry-run classification of every legacy pick matching
        `query`.  Never writes; safe to run in production."""
        q = query or {}
        cursor = self.db["picks"].find(q, {"_id": 0})
        try:
            picks = await cursor.to_list(length=None)
        except AttributeError:
            picks = []
            async for r in cursor:
                picks.append(r)
        prev_dry = self.dry_run
        self.dry_run = True
        try:
            return await self.reconcile(picks)
        finally:
            self.dry_run = prev_dry


__all__ = [
    "HistoricalReconciliationService",
    "OUTCOMES",
    "CANONICAL_ALREADY",
    "RECONCILED",
    "CANONICAL_CONFLICT_CANONICAL_WINS",
    "MISSING_PREGAME_SNAPSHOT",
    "LEGACY_ONLY_UNPROVEN",
    "UNRESOLVED_IDENTITY",
    "UNRESOLVED_EVENT",
    "LEGACY_DEAD",
]
