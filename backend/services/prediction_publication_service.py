"""PredictionPublicationService — the single write barrier.

Phase 1a — 2026-08.  Contract owner for every published prediction.

See:
  • /app/PUBLICATION_CONTRACT.md — behavioural contract
  • /app/ARCHITECTURE.md — pipeline placement
  • /app/PHASE1_AUDIT.md — mutation inventory this service supplants

Design notes
────────────
1. Every candidate that survives the canonical pipeline (fetch → feature
   → model → fusion → magic-tier → quality-gate → board-validator)
   passes through `publish_batch()`.
2. Publication is **idempotent**.  Retrying with the same candidate
   payload returns the existing snapshot instead of creating a new one.
3. Publication is **atomic per candidate** — the snapshot insert is a
   single MongoDB document operation, guaranteed atomic even on our
   standalone deployment.  The dual-write to `picks` is a best-effort
   projection that a future refresh will heal via idempotent re-publish.
4. Publication is **versioned** — `snapshot_version` is a monotone
   integer per `prediction_id`.  Phase 1a only writes v1; v0 is
   reserved for the legacy backfill (Phase 1c).
5. **Dual-write mode (Phase 1a)** — this service does NOT yet strip
   legacy fields from `picks`; endpoints continue to read the current
   fields.  Any drift between the snapshot and the legacy fields is
   recorded in `publication_mismatch_report` for later analysis.

Contract snapshot (what gets stored)
────────────────────────────────────
    {
      prediction_id, pick_id, snapshot_version,
      board_version,
      published_probability, published_edge, published_lock_score,
      published_grade, published_confidence, published_reasoning,
      published_line, published_odds,
      model_version, fusion_version, scoring_version,
      calibration_version, validator_version, simulation_version,
      feature_snapshot_version,
      published_at, publication_source,
      is_legacy, payload_hash, idempotency_key, is_active,
    }
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("lockscore.publication")

SNAPSHOT_COLLECTION = "prediction_snapshots"
MISMATCH_COLLECTION = "publication_mismatch_report"

# ─────────────────────────────────────────────────────────────────
# Immutable contract fields — see /app/PUBLICATION_CONTRACT.md §2.
# ─────────────────────────────────────────────────────────────────
PUBLISHED_FIELDS: tuple[str, ...] = (
    "published_probability",
    "published_edge",
    "published_lock_score",
    "published_grade",
    "published_confidence",
    "published_reasoning",
    "published_line",
    "published_odds",
)

VERSION_FIELDS: tuple[str, ...] = (
    "model_version",
    "fusion_version",
    "scoring_version",
    "calibration_version",
    "validator_version",
    "simulation_version",
    "feature_snapshot_version",
    "board_version",
)

# Explicit tokens (never invent version numbers).
LEGACY_UNKNOWN = "legacy_unknown"

# Legacy → published field aliases used during dual-write.
LEGACY_ALIAS_MAP: dict[str, str] = {
    "lock_score":       "published_lock_score",
    "win_probability":  "published_probability",
    "edge_percent":     "published_edge",
    "grade":            "published_grade",
    "confidence":       "published_confidence",
    "book_odds":        "published_odds",
    "line":             "published_line",
}


# ─────────────────────────────────────────────────────────────────
# Data class — the payload we compute and freeze.
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PublishedPayload:
    """Immutable payload as it will appear on a snapshot document."""
    prediction_id: str
    pick_id: str
    snapshot_version: int
    board_version: str
    published_probability: float
    published_edge: float
    published_lock_score: float
    published_grade: str
    published_confidence: float
    published_reasoning: Any
    published_line: Optional[float]
    published_odds: Optional[int]
    model_version: str
    fusion_version: str
    scoring_version: str
    calibration_version: str
    validator_version: str
    simulation_version: str
    feature_snapshot_version: str
    publication_source: str
    is_legacy: bool = False

    def to_snapshot_dict(self, *, payload_hash: str,
                          idempotency_key: str,
                          published_at: datetime,
                          is_active: bool = True) -> dict:
        d = {
            "prediction_id": self.prediction_id,
            "pick_id": self.pick_id,
            "snapshot_version": self.snapshot_version,
            "board_version": self.board_version,
            "published_probability": self.published_probability,
            "published_edge": self.published_edge,
            "published_lock_score": self.published_lock_score,
            "published_grade": self.published_grade,
            "published_confidence": self.published_confidence,
            "published_reasoning": self.published_reasoning,
            "published_line": self.published_line,
            "published_odds": self.published_odds,
            "model_version": self.model_version,
            "fusion_version": self.fusion_version,
            "scoring_version": self.scoring_version,
            "calibration_version": self.calibration_version,
            "validator_version": self.validator_version,
            "simulation_version": self.simulation_version,
            "feature_snapshot_version": self.feature_snapshot_version,
            "published_at": published_at.isoformat(),
            "publication_source": self.publication_source,
            "is_legacy": self.is_legacy,
            "payload_hash": payload_hash,
            "idempotency_key": idempotency_key,
            "is_active": is_active,
        }
        return d


@dataclass
class PublicationResult:
    prediction_id: str
    snapshot_version: int
    payload_hash: str
    idempotency_key: str
    published_at: str
    was_new: bool               # True when a snapshot row was created
    dual_write_applied: bool    # True when picks was updated
    mismatch_logged: bool = False


# ─────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────
class PredictionPublicationService:
    """One-owner service for publishing predictions.

    Phase 1a — dual-write only.  Endpoints are not yet cut over.
    """

    def __init__(self, db: AsyncIOMotorDatabase,
                  *, board_version: Optional[str] = None) -> None:
        self.db = db
        self._board_version = board_version or _default_board_version()

    # ── Index management ────────────────────────────────────────
    async def ensure_indices(self) -> None:
        coll = self.db[SNAPSHOT_COLLECTION]
        # A prediction has at most ONE snapshot per version.
        await coll.create_index(
            [("prediction_id", 1), ("snapshot_version", 1)],
            name="prediction_snapshot_version_uniq", unique=True,
        )
        # A retry with the same key must not create a duplicate.
        await coll.create_index(
            [("prediction_id", 1), ("idempotency_key", 1)],
            name="prediction_idempotency_uniq", unique=True,
        )
        await coll.create_index("board_version", name="board_version_idx")
        await coll.create_index("published_at", name="published_at_idx")
        await coll.create_index("model_version", name="model_version_idx")
        await coll.create_index("is_active", name="is_active_idx")
        # Mismatch report — for dual-write drift analysis.
        mc = self.db[MISMATCH_COLLECTION]
        await mc.create_index(
            [("prediction_id", 1), ("board_version", 1)],
            name="mismatch_prediction_board_idx",
        )
        await mc.create_index("logged_at", name="mismatch_logged_at_idx")

    # ── Public API ──────────────────────────────────────────────
    async def publish(self, candidate: dict, *,
                       publication_source: str = "canonical_pipeline",
                       dual_write: bool = True,
                       ) -> PublicationResult:
        """Publish a single candidate.  Idempotent + atomic per doc."""
        payload = self._build_payload(candidate, publication_source)
        payload_hash = _sha256_canonical(payload.to_snapshot_dict(
            payload_hash="", idempotency_key="",
            published_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        ))
        idempotency_key = _compute_idempotency_key(payload)
        now = datetime.now(timezone.utc)
        snap_doc = payload.to_snapshot_dict(
            payload_hash=payload_hash,
            idempotency_key=idempotency_key,
            published_at=now,
            is_active=True,
        )
        # Attempt idempotent insert.
        was_new = True
        try:
            await self.db[SNAPSHOT_COLLECTION].insert_one(snap_doc)
        except DuplicateKeyError:
            was_new = False
            existing = await self.db[SNAPSHOT_COLLECTION].find_one(
                {"prediction_id": payload.prediction_id,
                 "idempotency_key": idempotency_key},
                {"_id": 0},
            )
            if existing is None:
                # Extraordinarily rare — some OTHER unique key rejected
                # us (e.g. an earlier snapshot_version already exists).
                # Fetch the latest snapshot for logging and continue.
                existing = await self.db[SNAPSHOT_COLLECTION].find_one(
                    {"prediction_id": payload.prediction_id},
                    sort=[("snapshot_version", -1)], projection={"_id": 0},
                )
            if existing and existing.get("payload_hash") != payload_hash:
                logger.warning(
                    "publication drift: prediction_id=%s existing_hash=%s "
                    "new_hash=%s (idempotency_key match)",
                    payload.prediction_id,
                    existing.get("payload_hash"), payload_hash,
                )
            snap_doc = existing or snap_doc

        # Dual-write to `picks` — best effort, mismatches logged.
        dual_write_applied = False
        mismatch_logged = False
        if dual_write:
            dual_write_applied, mismatch_logged = await self._dual_write(
                payload, snap_doc,
            )

        return PublicationResult(
            prediction_id=payload.prediction_id,
            snapshot_version=snap_doc.get("snapshot_version",
                                           payload.snapshot_version),
            payload_hash=snap_doc.get("payload_hash", payload_hash),
            idempotency_key=snap_doc.get("idempotency_key",
                                          idempotency_key),
            published_at=snap_doc.get("published_at", now.isoformat()),
            was_new=was_new,
            dual_write_applied=dual_write_applied,
            mismatch_logged=mismatch_logged,
        )

    async def publish_batch(
        self, candidates: Iterable[dict], *,
        publication_source: str = "canonical_pipeline",
        dual_write: bool = True,
    ) -> dict:
        """Publish many candidates.  Never raises for a bad candidate —
        each failure is captured in `errors` so the batch continues."""
        results: list[PublicationResult] = []
        errors: list[dict] = []
        n_new = 0
        n_existing = 0
        n_mismatches = 0
        for cand in candidates:
            try:
                r = await self.publish(
                    cand, publication_source=publication_source,
                    dual_write=dual_write,
                )
                results.append(r)
                if r.was_new: n_new += 1
                else:         n_existing += 1
                if r.mismatch_logged: n_mismatches += 1
            except Exception as e:
                errors.append({
                    "prediction_id": cand.get("id") or cand.get("prediction_id"),
                    "error": f"{e.__class__.__name__}: {e}",
                })
        return {
            "board_version": self._board_version,
            "attempted": n_new + n_existing + len(errors),
            "new_snapshots": n_new,
            "existing_snapshots": n_existing,
            "errors": errors,
            "mismatches_logged": n_mismatches,
        }

    # ── Query helpers ───────────────────────────────────────────
    async def get_active_snapshot(self, prediction_id: str) -> Optional[dict]:
        return await self.db[SNAPSHOT_COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"_id": 0},
        )

    async def get_snapshot(self, prediction_id: str,
                            snapshot_version: int) -> Optional[dict]:
        return await self.db[SNAPSHOT_COLLECTION].find_one(
            {"prediction_id": prediction_id,
             "snapshot_version": snapshot_version},
            {"_id": 0},
        )

    # ── Internals ───────────────────────────────────────────────
    def _build_payload(
        self, candidate: dict, publication_source: str,
    ) -> PublishedPayload:
        pid = str(candidate.get("id") or candidate.get("prediction_id") or "")
        if not pid:
            raise ValueError(
                "candidate missing stable id / prediction_id — cannot publish")

        def _f(key: str, default: float = 0.0) -> float:
            v = candidate.get(key)
            try:
                return float(v) if v is not None else default
            except Exception:
                return default

        def _i_or_none(key: str) -> Optional[int]:
            v = candidate.get(key)
            if v is None or v == "":
                return None
            try:
                return int(round(float(v)))
            except Exception:
                return None

        def _f_or_none(key: str) -> Optional[float]:
            v = candidate.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except Exception:
                return None

        def _s(key: str, default: str = LEGACY_UNKNOWN) -> str:
            v = candidate.get(key)
            return str(v) if v not in (None, "") else default

        published_reasoning = (
            candidate.get("reasoning")
            or candidate.get("pick_rationale")
            or {}
        )

        return PublishedPayload(
            prediction_id=pid,
            pick_id=pid,
            snapshot_version=1,   # Phase 1a always writes v1
            board_version=self._board_version,
            published_probability=_f("win_probability"),
            published_edge=_f("edge_percent"),
            published_lock_score=round(_f("lock_score"), 2),
            published_grade=_s("grade", default="Pass"),
            published_confidence=_f("confidence"),
            published_reasoning=published_reasoning,
            published_line=_f_or_none("line"),
            published_odds=_i_or_none("book_odds"),
            model_version=_s("model_version"),
            fusion_version=_s("fusion_version"),
            scoring_version=_s("scoring_version"),
            calibration_version=_s("calibration_version"),
            validator_version=_s("validator_version"),
            simulation_version=_s("simulation_version"),
            feature_snapshot_version=_s("feature_snapshot_version"),
            publication_source=publication_source,
            is_legacy=(publication_source == "legacy_backfill"),
        )

    async def _dual_write(self, payload: PublishedPayload,
                           snap_doc: dict) -> tuple[bool, bool]:
        """Best-effort dual-write of published_* onto the picks doc.
        Returns (applied, mismatch_logged)."""
        set_payload: dict[str, Any] = {}
        for f in PUBLISHED_FIELDS:
            set_payload[f] = snap_doc.get(f)
        for f in VERSION_FIELDS:
            set_payload[f] = snap_doc.get(f)
        set_payload["published_at"] = snap_doc.get("published_at")
        set_payload["publication_source"] = snap_doc.get(
            "publication_source")
        set_payload["snapshot_version"] = snap_doc.get("snapshot_version")
        set_payload["payload_hash"] = snap_doc.get("payload_hash")
        set_payload["idempotency_key"] = snap_doc.get("idempotency_key")
        try:
            # Existing pick doc may not yet exist in picks (e.g. very
            # first publication before insert_many).  Use upsert=False
            # so we NEVER accidentally create a bare picks row from a
            # pure publication — the pipeline is responsible for the
            # picks row itself.
            res = await self.db.picks.update_one(
                {"id": payload.prediction_id},
                {"$set": set_payload},
                upsert=False,
            )
            applied = bool(getattr(res, "matched_count", 0))
        except Exception as e:
            logger.warning("dual-write update_one err: %s", e)
            return False, False

        # Compare legacy fields vs published_*.  Log any material drift.
        mismatch_logged = False
        if applied:
            pick = await self.db.picks.find_one(
                {"id": payload.prediction_id},
                projection={
                    "lock_score": 1, "win_probability": 1,
                    "edge_percent": 1, "grade": 1,
                    "confidence": 1, "book_odds": 1,
                    "line": 1, "_id": 0,
                },
            )
            if pick:
                drifts = _compute_drifts(pick, snap_doc)
                if drifts:
                    await self.db[MISMATCH_COLLECTION].insert_one({
                        "prediction_id": payload.prediction_id,
                        "board_version": payload.board_version,
                        "logged_at": datetime.now(timezone.utc).isoformat(),
                        "drifts": drifts,
                    })
                    mismatch_logged = True
        return applied, mismatch_logged


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def _default_board_version() -> str:
    return datetime.now(timezone.utc).strftime("board-%Y%m%dT%H%M%SZ")


def _sha256_canonical(payload: dict) -> str:
    """Canonical JSON → sha256 hex.  Reasoning field is coerced to a
    stable string so dict-order changes don't produce false drift."""
    d = {k: payload.get(k) for k in sorted(payload) if k not in
         ("payload_hash", "idempotency_key", "published_at", "_id")}
    if isinstance(d.get("published_reasoning"), (dict, list)):
        d["published_reasoning"] = json.dumps(
            d["published_reasoning"], sort_keys=True, default=str)
    return hashlib.sha256(
        json.dumps(d, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _compute_idempotency_key(p: PublishedPayload) -> str:
    line_str = "none" if p.published_line is None else f"{p.published_line:.4f}"
    odds_str = "none" if p.published_odds is None else str(p.published_odds)
    raw = "|".join([
        p.prediction_id,
        p.board_version,
        f"{p.published_probability:.6f}",
        f"{p.published_lock_score:.2f}",
        f"{p.published_edge:.3f}",
        line_str,
        odds_str,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_drifts(pick: dict, snap: dict) -> list[dict]:
    """Compare legacy fields on `picks` against snapshot `published_*`.
    Only materially different values are recorded."""
    out: list[dict] = []
    tolerance = {
        "lock_score":      0.05,
        "win_probability": 0.0005,
        "edge_percent":    0.01,
        "confidence":      0.5,
        "book_odds":       0.5,
        "line":            0.05,
    }
    for legacy, pub in LEGACY_ALIAS_MAP.items():
        lv = pick.get(legacy)
        pv = snap.get(pub)
        if lv is None and pv is None:
            continue
        try:
            if isinstance(lv, (int, float)) and isinstance(pv, (int, float)):
                if abs(float(lv) - float(pv)) > tolerance.get(legacy, 0.01):
                    out.append({"field": legacy, "picks": lv, "snapshot": pv})
            elif lv != pv:
                out.append({"field": legacy, "picks": lv, "snapshot": pv})
        except Exception:
            pass
    return out


__all__ = [
    "PredictionPublicationService",
    "PublishedPayload",
    "PublicationResult",
    "SNAPSHOT_COLLECTION",
    "MISMATCH_COLLECTION",
    "PUBLISHED_FIELDS",
    "VERSION_FIELDS",
    "LEGACY_UNKNOWN",
    "LEGACY_ALIAS_MAP",
]
