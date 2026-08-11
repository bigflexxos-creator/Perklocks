"""Consumption Proof (§7).

Given a real pick_id, prove which applicable production stages
actually occurred.  This is the *narrative* side of the contract:

    * Reachability  — did each stage's proof survive on the record?
    * Custody       — did the record originate from real data, or
                       from a seed/DB-insert/mock?
    * Snapshot      — is the pregame snapshot present + hash-valid?
    * Settlement    — has the pick reached PUBLISHED / SETTLED /
                       MEASURABLE?

Design contract
---------------
* Read-only — never mutates the database.
* If a stage cannot be proven, the proof reports UNKNOWN or FAIL
  with an appropriate DropReason.  Module existence is NEVER
  fabricated as PASS (§7, §14).
* Tolerates missing collections / legacy records (§11) — the
  service degrades gracefully and reports UNKNOWN.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .reachability import build_reachability_report
from .chain_of_custody import (
    build_custody_record,
    distinguish_code_exists_from_real_path,
)
from .pregame_snapshot import (
    PREGAME_SNAPSHOTS_COLLECTION,
    verify_snapshot_hash,
)
from .settlement_linkage import (
    classify_measurability,
    MeasurabilityState,
)
from .publication_observer import (
    OBSERVATIONS_COLLECTION,
    read_observation,
)


PICKS_COLLECTION = "picks"
SETTLEMENTS_COLLECTION = "pick_settlements"
ANALYTICS_COLLECTION = "pick_analytics"


async def _safe_find_one(db, coll: str, query: dict) -> Optional[dict]:
    """Wrap ``find_one`` so a missing collection / index does not
    crash the proof endpoint on legacy databases."""
    try:
        doc = await db[coll].find_one(query)
    except Exception:
        return None
    if doc:
        doc.pop("_id", None)
    return doc


async def _find_pick(db, pick_id: str) -> Optional[dict]:
    # Support both string ids and external_ids.
    for query in ({"id": pick_id}, {"external_id": pick_id},
                    {"pick_id": pick_id}):
        doc = await _safe_find_one(db, PICKS_COLLECTION, query)
        if doc:
            return doc
    return None


async def _find_snapshot(db, pick: dict) -> Optional[dict]:
    for query in (
        {"canonical_prediction_id": pick.get("canonical_prediction_id"),
          "supersedes": None},
        {"pick_id": pick.get("id") or pick.get("external_id"),
          "supersedes": None},
    ):
        if not any(v for v in query.values() if v is not None):
            continue
        doc = await _safe_find_one(db, PREGAME_SNAPSHOTS_COLLECTION, query)
        if doc:
            return doc
    return None


async def _find_settlement(db, pick: dict) -> Optional[dict]:
    pid = pick.get("id") or pick.get("external_id")
    cpid = pick.get("canonical_prediction_id")
    for query in ({"pick_id": pid}, {"canonical_prediction_id": cpid}):
        if not any(v for v in query.values() if v is not None):
            continue
        doc = await _safe_find_one(db, SETTLEMENTS_COLLECTION, query)
        if doc:
            return doc
    return None


async def _find_analytics(db, pick: dict) -> Optional[dict]:
    pid = pick.get("id") or pick.get("external_id")
    cpid = pick.get("canonical_prediction_id")
    for query in ({"pick_id": pid}, {"canonical_prediction_id": cpid}):
        if not any(v for v in query.values() if v is not None):
            continue
        doc = await _safe_find_one(db, ANALYTICS_COLLECTION, query)
        if doc:
            return doc
    return None


async def build_consumption_proof(db, pick_id: str) -> dict:
    """Return a JSON-serialisable proof document for the given pick.

    Response shape:
        {
            "pick_id": ...,
            "found": True/False,
            "generated_at": ISO,
            "reachability": {...},
            "custody": {...},
            "path_verdict": REAL_PRODUCTION_PATH_PROVEN | PARTIALLY_PROVEN | CODE_EXISTS_ONLY,
            "pregame_snapshot": {"present": bool, "hash_valid": bool},
            "settlement": {...},
            "measurability": FULLY_MEASURABLE | ...
        }
    """
    proof: dict[str, Any] = {
        "pick_id":      pick_id,
        "found":        False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    pick = await _find_pick(db, pick_id)
    if not pick:
        proof["reason"] = "pick_not_found"
        return proof
    proof["found"] = True

    snapshot   = await _find_snapshot(db, pick)
    settlement = await _find_settlement(db, pick)
    analytics  = await _find_analytics(db, pick)
    observation = await read_observation(
        db,
        canonical_prediction_id=pick.get("canonical_prediction_id"),
        pick_id=pick.get("id") or pick.get("external_id"),
    )

    # Analytics linkage is boolean-ish for reachability.
    analytics_linked: Optional[bool] = None
    if analytics is not None:
        row_cpid = analytics.get("canonical_prediction_id")
        row_pid  = analytics.get("pick_id") or analytics.get("external_id")
        if (row_cpid and row_cpid == pick.get("canonical_prediction_id")) \
           or (row_pid and row_pid == (pick.get("id") or pick.get("external_id"))):
            analytics_linked = True
        else:
            analytics_linked = False

    reachability = build_reachability_report(
        pick,
        pregame_snapshot=snapshot,
        settlement_record=settlement,
        analytics_linked=analytics_linked,
    )
    custody = build_custody_record(pick)
    # If we have a recorded observation, upgrade the custody
    # PRODUCTION_CONSUMER origin using the recorded publication_source.
    if observation and observation.get("origin"):
        try:
            from .chain_of_custody import CustodyStage, VALID_ORIGINS
            if observation["origin"] in VALID_ORIGINS:
                custody.note(CustodyStage.PRODUCTION_CONSUMER,
                              origin=observation["origin"],
                              proof=f"observed@{observation.get('ts')}",
                              detail=observation.get("publication_source"))
                # Producer origin — publications from real canonical
                # sources count as DATA; direct-inject remains marked
                # as such.  Never fake real when observation says
                # otherwise.
                custody.note(CustodyStage.PRODUCER,
                              origin=observation["origin"],
                              proof=observation.get("publication_source"))
        except Exception:                        # pragma: no cover
            pass
    verdict = distinguish_code_exists_from_real_path(custody)
    # Refine verdict to match the required §7 vocabulary — a
    # PARTIALLY_PROVEN custody + a real observed publication maps to
    # PARTIAL_PRODUCTION_PATH.
    verdict_map = {
        "REAL_PRODUCTION_PATH_PROVEN": "REAL_PRODUCTION_PATH_PROVEN",
        "PARTIALLY_PROVEN":            "PARTIAL_PRODUCTION_PATH",
        "CODE_EXISTS_ONLY":            "CODE_EXISTS_ONLY",
    }
    path_verdict = verdict_map.get(verdict, verdict)
    measurability = classify_measurability(
        pick,
        pregame_snapshot=snapshot,
        settlement_record=settlement,
        analytics_row=analytics,
    )

    proof["reachability"] = reachability.to_dict()
    proof["custody"] = custody.to_dict()
    proof["path_verdict"] = path_verdict
    proof["observation"] = observation      # None when the pick
                                              # predates the observer,
                                              # never fabricated
    proof["pregame_snapshot"] = {
        "present":     snapshot is not None,
        "hash_valid":  verify_snapshot_hash(snapshot) if snapshot else False,
        "hash":        (snapshot or {}).get("snapshot_hash"),
    }
    proof["settlement"] = {
        "present":   settlement is not None,
        "source":    (settlement or {}).get("source"),
        "status":    pick.get("settlement_status"),
    }
    proof["measurability"] = measurability.to_dict()
    return proof


__all__ = [
    "build_consumption_proof",
    "PICKS_COLLECTION",
    "SETTLEMENTS_COLLECTION",
    "ANALYTICS_COLLECTION",
]
