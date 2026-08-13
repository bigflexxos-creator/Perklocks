"""P0.2c — HistoryProjectionService.

History is now a *deterministic projection* of two canonical inputs:

    ┌─────────────────────────────┐        ┌────────────────────────────┐
    │ prediction_snapshots (v0/v1)│        │   settlement_events        │
    │   frozen pregame truth      │        │   canonical W/L/P/V ledger │
    │   line / odds / lock_score  │        │   versioned + correctable  │
    │   sportsbook / evidence     │        │   is_active=True → current │
    └──────────────┬──────────────┘        └──────────────┬─────────────┘
                   │                                      │
                   └───────────────┬──────────────────────┘
                                   ▼
                    HistoryProjectionService.project(pick)
                                   ▼
              History-shaped record (deterministic, idempotent)
                                   ▼
                 /api/picks/history · Analytics · Consumers

Design rules (P0.2c spec):

  §3  When a canonical settlement event exists for a pick, History
      MUST derive `result / status / settled_at / units_profit /
      final_score` from the ledger — NEVER re-grade from raw scores.
  §4  Corrections (v2 supersedes v1) change the *current* history view
      but never destroy prior lineage.  `settlement_event_id`,
      `settlement_version`, `supersedes_settlement_id`, and
      `correction_reason` are all surfaced.
  §5  Idempotency: projecting the same pick twice yields the same
      record; no duplicate history rows.  Deterministic key =
      canonical pick_id.
  §6  PUSH ≠ VOID.  The projector NEVER collapses one into the other.
  §7  Frozen pregame values are preserved verbatim from the snapshot;
      settlement can never rewrite line/odds/sportsbook/lock_score.
  §10 Missing snapshot fields stay `None` (unavailable) — never
      fabricated from post-game data.
  §11 Wrong-identity protection: canonical settlement events are only
      joined when the `prediction_id` matches exactly.
  §12 LIVE picks (no active canonical settlement event) do NOT project
      as WON/LOST/PUSH/VOID.

This service does NOT write to `picks` or to any settlement collection.
It is READ-ONLY.  Canonical settlement writes remain the sole province
of `services/settlement_service.py` (locked in the static-guard test).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


# ─── Canonical settlement field surface ─────────────────────────────
CANONICAL_SETTLEMENT_FIELDS = (
    "settlement_event_id",
    "settlement_version",
    "supersedes_settlement_id",
    "correction_reason",
    "grader_version",
    "settlement_source",
    "settled_at",
    "actual_result",
)

# Fields owned by the frozen pregame prediction snapshot — never
# overwritten by settlement projection.
FROZEN_PREGAME_FIELDS = (
    "line",
    "book_odds",
    "odds_at_pick",
    "sportsbook",
    "book",
    "lock_score",
    "published_lock_score",
    "published_line",
    "published_odds",
    "published_grade",
    "model_probability",
    "sim_probability",
    "calibrated_probability",
    "market_probability",
    "magic_evidence",
    "apex_status",
    "published_at",
    "publication_source",
    "board_version",
    "model_version",
    "simulator_version",
)


def _active_settlement_for(events: list[dict], pid: str) -> Optional[dict]:
    """Return the active settlement_events row for a prediction, or None.

    P0.2a guarantees at most one is_active=True row per prediction_id.
    """
    for ev in events:
        if ev.get("prediction_id") == pid and ev.get("is_active"):
            return ev
    return None


def _prior_settlements_for(events: list[dict], pid: str) -> list[dict]:
    """All (inactive) prior settlements for a prediction — used to
    reconstruct the correction lineage without destroying history."""
    return [ev for ev in events
            if ev.get("prediction_id") == pid and not ev.get("is_active")]


def project_pick(
    pick: dict,
    *,
    active_event: Optional[dict] = None,
    prior_events: Optional[list[dict]] = None,
    snapshot: Optional[dict] = None,
) -> dict:
    """Project one canonical History record for `pick`.

    Priority order for each field:

      1. Frozen pregame snapshot value  (line/odds/lock_score/...)
      2. Active canonical settlement    (status/result/settled_at/...)
      3. Original pick field            (fallback for legacy rows)
      4. `None`                         (never fabricated)

    Corrections: when `active_event.settlement_version > 1`, the
    returned dict carries `supersedes_settlement_id`, `old_result`,
    `new_result`, and `correction_reason` for provenance.  It does
    NOT duplicate the pick — the same `pick_id` still keys it.
    """
    pid = pick.get("id") or pick.get("pick_id")
    if not pid:
        # Wrong-identity guard: without a canonical id, we cannot
        # attach a settlement event.  Return the pick as-is with a
        # marker so downstream doesn't silently mis-attribute.
        proj = dict(pick)
        proj["_history_projection_error"] = "missing_canonical_pick_id"
        return proj

    prior_events = prior_events or []
    proj: dict = dict(pick)   # start from the pick

    # ─── 1.  Frozen pregame overlay (snapshot > pick fallback) ─────
    if snapshot:
        for field in FROZEN_PREGAME_FIELDS:
            snap_val = snapshot.get(field)
            if snap_val is not None:
                proj[field] = snap_val

    # ─── 2.  Canonical settlement overlay ──────────────────────────
    if active_event:
        # `result` is the canonical WON/LOST/PUSH/VOID.  Mirror to
        # `status` for legacy consumers, but PRESERVE the distinction.
        result = active_event.get("result")
        if result in ("won", "lost", "push", "void", "cancelled"):
            proj["status"] = result if result != "cancelled" else "void"
            proj["result"] = result
        # Settlement provenance
        proj["settlement_event_id"]      = active_event.get("settlement_id")
        proj["settlement_version"]       = active_event.get("settlement_version")
        proj["supersedes_settlement_id"] = active_event.get("supersedes_settlement_id")
        proj["correction_reason"]        = active_event.get("correction_reason")
        proj["grader_version"]           = active_event.get("grader_version")
        proj["settlement_source"]        = active_event.get("source")
        proj["settled_at"]               = active_event.get("settled_at")
        # Actual-result payload (final score / player stat).  Preserve
        # the entire dict so cross-sport consumers can render whatever
        # they need without a second lookup.
        proj["actual_result"]            = active_event.get("actual_result") or {}
        # Correction detail (only present on v2+)
        if active_event.get("settlement_version", 1) > 1:
            proj["old_result"]    = active_event.get("old_result")
            proj["new_result"]    = active_event.get("new_result")
            proj["corrected_at"]  = active_event.get("corrected_at")
        # Full lineage — so History can prove "this was a correction"
        # without duplicating the pick row.
        proj["settlement_lineage"] = [
            {
                "settlement_id":       ev.get("settlement_id"),
                "settlement_version":  ev.get("settlement_version"),
                "result":              ev.get("result"),
                "is_active":           ev.get("is_active"),
                "source":              ev.get("source"),
                "settled_at":          ev.get("settled_at"),
                "correction_reason":   ev.get("correction_reason"),
            }
            for ev in sorted(
                prior_events + [active_event],
                key=lambda e: e.get("settlement_version") or 0,
            )
        ]
        proj["_canonical_settlement_present"] = True
    else:
        # ─── 3.  LIVE / unresolved picks — DO NOT PROJECT SETTLED
        # If no canonical settlement event exists AND the pick's
        # compat-mirror status suggests it was settled by a legacy
        # writer, we DO NOT trust that — clear the mirror to
        # `unresolved` so consumers can't accidentally display a
        # stale outcome as canonical.  P0.2b static-guard already
        # prevents new rogue writers; this guard covers the archival
        # tail.
        legacy_status = (proj.get("status") or "").lower()
        if legacy_status in ("won", "lost", "push", "void"):
            # Preserve the legacy value under `_legacy_status` for
            # audit + reconciliation, but the canonical view is
            # "no settlement event exists".
            proj["_legacy_status_without_canonical_event"] = legacy_status
            proj["status"] = "unresolved"
            proj["result"] = None
        proj["_canonical_settlement_present"] = False
        proj["settlement_lineage"] = []

    proj["_history_projection_version"] = "p02c.v1"
    return proj


class HistoryProjectionService:
    """P0.2c canonical History projector.

    READ-ONLY — this service NEVER writes to `picks`, `settlement_events`,
    or any downstream collection.  Its sole responsibility is to derive
    a deterministic History-shaped record from canonical inputs.
    """
    def __init__(self, db):
        self.db = db

    async def _load_events_for(self, prediction_ids: list[str]) -> list[dict]:
        if not prediction_ids:
            return []
        cursor = self.db["settlement_events"].find(
            {"prediction_id": {"$in": prediction_ids}},
            {"_id": 0},
        )
        try:
            rows = await cursor.to_list(length=len(prediction_ids) * 4)
        except AttributeError:
            # Fake DBs used in tests may return a list directly.
            rows = []
            async for r in cursor:
                rows.append(r)
        return rows

    async def _load_snapshots_for(self, prediction_ids: list[str]) -> dict[str, dict]:
        if not prediction_ids:
            return {}
        try:
            from services.prediction_publication_service import (
                SNAPSHOT_COLLECTION,
            )
        except Exception:
            SNAPSHOT_COLLECTION = "prediction_snapshots"
        try:
            rows = await self.db[SNAPSHOT_COLLECTION].find(
                {"prediction_id": {"$in": prediction_ids},
                 "is_active": True},
                {"_id": 0},
            ).to_list(length=len(prediction_ids))
        except AttributeError:
            rows = []
            async for r in self.db[SNAPSHOT_COLLECTION].find(
                    {"prediction_id": {"$in": prediction_ids},
                     "is_active": True}):
                rows.append(r)
        out: dict[str, dict] = {}
        for r in rows:
            pid = r.get("prediction_id")
            if pid:
                out[pid] = r
        return out

    async def project_many(self, picks: list[dict]) -> list[dict]:
        """Project a batch of picks — canonical joins done in bulk."""
        pids = [
            (p.get("id") or p.get("pick_id"))
            for p in picks if (p.get("id") or p.get("pick_id"))
        ]
        events = await self._load_events_for(pids)
        events_by_pid: dict[str, list[dict]] = {}
        for ev in events:
            events_by_pid.setdefault(ev["prediction_id"], []).append(ev)
        snapshots = await self._load_snapshots_for(pids)
        out: list[dict] = []
        for p in picks:
            pid = p.get("id") or p.get("pick_id")
            pev = events_by_pid.get(pid, [])
            active = next((e for e in pev if e.get("is_active")), None)
            prior  = [e for e in pev if not e.get("is_active")]
            out.append(project_pick(
                p,
                active_event=active,
                prior_events=prior,
                snapshot=snapshots.get(pid),
            ))
        return out

    async def project_one(self, pick: dict) -> dict:
        """Project a single pick record."""
        [proj] = await self.project_many([pick])
        return proj


__all__ = [
    "HistoryProjectionService",
    "project_pick",
    "CANONICAL_SETTLEMENT_FIELDS",
    "FROZEN_PREGAME_FIELDS",
]
