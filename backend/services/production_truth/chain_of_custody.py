"""Chain of Custody (§6).

A real pick must be traceable through:

    PRODUCER
        → NORMALIZER
        → PRODUCTION CONSUMER
        → CANDIDATE PATH
        → PUBLICATION PATH
        → USER CONSUMER
        → PREGAME SNAPSHOT
        → SETTLEMENT PATH
        → ANALYTICS PATH

The custody record is *not* an audit story — it is a machine-
readable structure that other services can populate as they touch
the record.  Each stage records:

    * ``proof``   — a short trace token (file:function or record id)
    * ``origin``  — DATA / DIRECT_INJECT / SEEDED / DB_INSERT / MOCK / UNKNOWN

Per §6:

    A direct function call is not sufficient proof.
    A seeded database record is not sufficient proof.
    A direct database insert is not sufficient proof.
    A mocked frontend response is not sufficient proof.

Hence ``distinguish_code_exists_from_real_path`` returns
``REAL_PRODUCTION_PATH_PROVEN`` only when every applicable stage's
``origin`` is ``DATA`` (or a legitimate normaliser).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class CustodyStage(str, enum.Enum):
    PRODUCER            = "PRODUCER"
    NORMALIZER          = "NORMALIZER"
    PRODUCTION_CONSUMER = "PRODUCTION_CONSUMER"
    CANDIDATE_PATH      = "CANDIDATE_PATH"
    PUBLICATION_PATH    = "PUBLICATION_PATH"
    USER_CONSUMER       = "USER_CONSUMER"
    PREGAME_SNAPSHOT    = "PREGAME_SNAPSHOT"
    SETTLEMENT_PATH     = "SETTLEMENT_PATH"
    ANALYTICS_PATH      = "ANALYTICS_PATH"


VALID_ORIGINS = frozenset({
    "DATA",          # a real upstream data producer/normaliser
    "DIRECT_INJECT", # e.g. mls_direct_inject — subject to barrier
    "SEEDED",        # seed script — NOT a real production path
    "DB_INSERT",     # arbitrary db insert — NOT a real production path
    "MOCK",          # unit/integration test fixture — NOT real
    "UNKNOWN",       # we cannot prove which category applies
})


REAL_ORIGINS = frozenset({"DATA"})   # Only DATA counts as real.


@dataclass
class CustodyRecord:
    pick_id:      Optional[str] = None
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stages:       dict[str, dict] = field(default_factory=dict)

    def note(self, stage: "CustodyStage | str",
              *,
              proof: Optional[str] = None,
              origin: str = "UNKNOWN",
              detail: Optional[str] = None) -> None:
        key = stage.value if isinstance(stage, CustodyStage) else str(stage)
        if origin not in VALID_ORIGINS:
            origin = "UNKNOWN"
        self.stages[key] = {
            "proof":  proof,
            "origin": origin,
            "detail": detail,
        }

    def to_dict(self) -> dict:
        return {
            "pick_id":      self.pick_id,
            "generated_at": self.generated_at,
            "stages":       self.stages,
        }

    @property
    def all_real(self) -> bool:
        """True IFF every noted stage originates from real data."""
        if not self.stages:
            return False
        return all(v.get("origin") in REAL_ORIGINS
                    for v in self.stages.values())

    @property
    def worst_origin(self) -> str:
        """Return the origin most damaging to real-production status.
        Priority (worst first): MOCK, DB_INSERT, SEEDED, DIRECT_INJECT,
        UNKNOWN, DATA."""
        priority = {"MOCK": 5, "DB_INSERT": 4, "SEEDED": 3,
                     "DIRECT_INJECT": 2, "UNKNOWN": 1, "DATA": 0}
        best = -1
        worst = "UNKNOWN"
        for v in self.stages.values():
            o = v.get("origin") or "UNKNOWN"
            p = priority.get(o, 1)
            if p > best:
                best = p
                worst = o
        return worst


def build_custody_record(pick: dict) -> CustodyRecord:
    """Build a best-effort custody record from an already-persisted
    pick document.  Every stage that cannot be proven from the pick
    alone is annotated with ``UNKNOWN`` — never faked to DATA.
    """
    rec = CustodyRecord(pick_id=pick.get("id") or pick.get("external_id"))

    # PRODUCER — must have originated from a real gateway; presence
    # of ``odds_provenance`` != MODEL implies real provider fetch.
    prov = (pick.get("odds_provenance") or "").upper()
    if prov and prov not in {"MODEL", "SYNTHETIC", "FAIR", "MODEL_ONLY", "COMPUTED"}:
        rec.note(CustodyStage.PRODUCER,
                  origin="DATA", proof=f"odds_provenance={prov}")
    elif prov:
        rec.note(CustodyStage.PRODUCER,
                  origin="UNKNOWN", proof=f"odds_provenance={prov}",
                  detail="model/synthetic provenance is not real")
    else:
        rec.note(CustodyStage.PRODUCER,
                  origin="UNKNOWN", detail="no odds_provenance recorded")

    # NORMALIZER — presence of canonical_prediction_id proves the
    # canonical publisher touched the record.
    if pick.get("canonical_prediction_id"):
        rec.note(CustodyStage.NORMALIZER,
                  origin="DATA",
                  proof=f"canonical_prediction_id={pick.get('canonical_prediction_id')}")
    else:
        rec.note(CustodyStage.NORMALIZER,
                  origin="UNKNOWN", detail="no canonical_prediction_id")

    # PRODUCTION_CONSUMER — a canonical publication gate marker.
    gate = pick.get("publication_gate")
    if gate == "canonical_barrier_passed":
        rec.note(CustodyStage.PRODUCTION_CONSUMER,
                  origin="DATA", proof=gate)
    elif gate == "canonical_barrier_rejected":
        rec.note(CustodyStage.PRODUCTION_CONSUMER,
                  origin="DIRECT_INJECT",
                  proof=gate,
                  detail=", ".join(pick.get("barrier_failures") or []))
    else:
        rec.note(CustodyStage.PRODUCTION_CONSUMER,
                  origin="UNKNOWN",
                  detail="no publication_gate marker (legacy)")

    # CANDIDATE_PATH — the pick document itself proves generation.
    rec.note(CustodyStage.CANDIDATE_PATH,
              origin="DATA", proof="pick document persisted")

    # PUBLICATION_PATH — same barrier signal as consumer above.
    if gate == "canonical_barrier_passed":
        rec.note(CustodyStage.PUBLICATION_PATH,
                  origin="DATA", proof=gate)
    else:
        rec.note(CustodyStage.PUBLICATION_PATH,
                  origin="UNKNOWN", detail=f"publication_gate={gate!r}")

    # USER_CONSUMER — visible to Locks/Rollover/Parlay.
    if pick.get("off_board") is True or pick.get("no_bet") is True:
        rec.note(CustodyStage.USER_CONSUMER,
                  origin="UNKNOWN",
                  detail="pick is off_board / no_bet")
    else:
        try:
            lock = float(pick.get("lock_score") or 0)
        except (TypeError, ValueError):
            lock = 0.0
        if lock >= 85:
            rec.note(CustodyStage.USER_CONSUMER,
                      origin="DATA",
                      proof=f"lock_score={lock} >= 85")
        else:
            rec.note(CustodyStage.USER_CONSUMER,
                      origin="UNKNOWN",
                      detail=f"lock_score={lock} < 85")

    # PREGAME_SNAPSHOT — must be proven by the snapshot lookup.
    if pick.get("pregame_snapshot_id") or pick.get("pregame_snapshot_hash"):
        rec.note(CustodyStage.PREGAME_SNAPSHOT,
                  origin="DATA",
                  proof=str(pick.get("pregame_snapshot_id")
                             or pick.get("pregame_snapshot_hash")))
    else:
        rec.note(CustodyStage.PREGAME_SNAPSHOT,
                  origin="UNKNOWN",
                  detail="no pregame_snapshot linkage on pick")

    # SETTLEMENT_PATH — settlement_status carries the proof.
    status = (pick.get("settlement_status") or "").lower()
    if status in {"won", "lost", "push", "void"}:
        rec.note(CustodyStage.SETTLEMENT_PATH,
                  origin="DATA", proof=f"settlement_status={status}")
    else:
        rec.note(CustodyStage.SETTLEMENT_PATH,
                  origin="UNKNOWN",
                  detail=f"settlement_status={status!r}")

    # ANALYTICS_PATH — proof is external to the pick doc.
    rec.note(CustodyStage.ANALYTICS_PATH,
              origin="UNKNOWN",
              detail="analytics linkage not evaluated from pick doc")

    return rec


def distinguish_code_exists_from_real_path(rec: CustodyRecord) -> str:
    """Return one of:

        ``REAL_PRODUCTION_PATH_PROVEN``  — every stage from real data
        ``PARTIALLY_PROVEN``             — some stages proven, others UNKNOWN
        ``CODE_EXISTS_ONLY``             — dominated by seeded/db_insert/mock
    """
    if rec.all_real:
        return "REAL_PRODUCTION_PATH_PROVEN"
    worst = rec.worst_origin
    if worst in {"MOCK", "DB_INSERT", "SEEDED"}:
        return "CODE_EXISTS_ONLY"
    return "PARTIALLY_PROVEN"


__all__ = [
    "CustodyStage",
    "CustodyRecord",
    "VALID_ORIGINS",
    "REAL_ORIGINS",
    "build_custody_record",
    "distinguish_code_exists_from_real_path",
]
