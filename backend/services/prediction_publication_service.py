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
# Canonical prediction units (P0-1, 2026-08-11)
# ─────────────────────────────────────────────────────────────────
# The publication CONTRACT owns exactly one representation of each
# scoring dimension:
#
#   Snapshot field                Internal unit          Notes
#   ────────────────────────────  ─────────────────────  ─────────────
#   published_probability         float in [0.0, 1.0]    canonical
#                                                        fraction; 0.682
#                                                        means 68.2%.
#   published_edge                Optional[float]        percentage-point
#                                                        delta (0-100
#                                                        scale) OR None
#                                                        when no book
#                                                        line exists.
#   published_lock_score          float in [0.0, 100.0]  Lock tier (not a
#                                                        probability).
#   published_confidence          str  label             ("Very High",
#                                                        "High", "Medium",
#                                                        "Low", "Very Low",
#                                                        or "Pass").
#   published_confidence_score    Optional[float]        Reserved for a
#                                                        future numeric
#                                                        [0, 100] confidence
#                                                        score.  Currently
#                                                        None on every
#                                                        snapshot.
#
# Legacy pick-doc aliases WRITTEN BY dual-write use the units the
# existing frontend consumes:
#
#   Legacy field                  Legacy unit            Notes
#   ────────────────────────────  ─────────────────────  ─────────────
#   win_probability               float in [0.0, 100.0]  0-100 percentage
#                                                        (frontend renders
#                                                        `${wp}%`).
#   edge_percent                  Optional[float]        percentage-point
#                                                        delta OR None.
#   confidence                    str  label             label string.
#
# Conversions happen at exactly TWO boundaries:
#   1. `_dual_write` — snapshot → legacy alias (fraction ⇒ percentage,
#      None preserved).
#   2. `published_prediction_reader.hydrate` — snapshot → legacy alias
#      at read time for any pick doc that predates the dual-write.
#
# ─────────────────────────────────────────────────────────────────
# Data class — the payload we compute and freeze.
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PublishedPayload:
    """Immutable payload as it will appear on a snapshot document.

    Units — see the CANONICAL UNITS block above.
    """
    prediction_id: str
    pick_id: str
    snapshot_version: int
    board_version: str
    # 0.0–1.0 fraction (canonical).
    published_probability: float
    # Percentage-point delta OR None when no book line is available.
    published_edge: Optional[float]
    published_lock_score: float
    published_grade: str
    # Label string ("Very High", "High", "Medium", "Low", "Very Low",
    # "Pass").  A previous version of this contract typed the field
    # as `float`, which caused `float("Very High")` to fall through
    # to a default of 0.0 and destroy the label on every canonical
    # publication.  Now typed as `str` and coerced explicitly.
    published_confidence: str
    # Optional numeric confidence in [0.0, 100.0].  Reserved for a
    # future dedicated numeric confidence score.  None on every
    # snapshot for now — the label is authoritative.
    published_confidence_score: Optional[float]
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
            "published_confidence_score": self.published_confidence_score,
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
        """Phase 3C — delegate to central registry."""
        try:
            from services import index_registry as _ir
            await _ir.ensure_collection(self.db, SNAPSHOT_COLLECTION)
            await _ir.ensure_collection(self.db, MISMATCH_COLLECTION)
        except Exception as e:  # pragma: no cover
            import logging
            logging.getLogger("lockscore.publication").debug(
                "prediction_publication ensure_indices via registry: %s", e)

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
        each failure is captured in `errors` so the batch continues.

        Phase 2 Follow-up (2026-08-11) — the player↔team↔fixture
        integrity gate lives INSIDE this barrier.  Any Soccer
        player-based prop whose player's CURRENT team is not on the
        fixture (or whose roster observation is stale/missing) is
        REJECTED here — never publishes, never receives
        ``publication_source``.  Direct callers of ``publish_batch``
        can no longer bypass the check by skipping the
        ``publication_helpers``/orchestrator wrappers.
        """
        candidates_list = list(candidates)

        # ── Layer-B integrity gate (definitive barrier) ─────────────
        rejected_integrity: list[dict] = []
        try:
            from services.player_team_fixture_validator import (
                validate_player_fixture_pick, tag_pick_with_verdict, _norm,
            )
            roster_lookup: dict[str, str] = {}
            fresh_names: set[str] = set()
            try:
                from services import mls_scorer_gate as _mls
                snap = getattr(_mls, "_espn_by_name", None) or {}
                for name, entry in snap.items():
                    t = entry.get("team") if isinstance(entry, dict) else None
                    if t:
                        k = _norm(name)
                        roster_lookup[k] = t
                        fresh_names.add(k)
            except Exception:
                pass
            for p in candidates_list:
                pn = p.get("player_name") or p.get("player")
                pct = p.get("player_current_team")
                if isinstance(pn, str) and isinstance(pct, str):
                    k = _norm(pn)
                    roster_lookup[k] = pct
                    fresh_names.add(k)
            gated: list[dict] = []
            for p in candidates_list:
                if p.get("sport") != "Soccer":
                    gated.append(p)
                    continue
                verdict = validate_player_fixture_pick(
                    p, roster_lookup,
                    fresh_roster_names=(fresh_names or None),
                )
                if verdict.get("verified"):
                    gated.append(p)
                    continue
                tag_pick_with_verdict(p, verdict)
                p["off_board"] = True
                rejected_integrity.append({
                    "prediction_id": p.get("id") or p.get("prediction_id"),
                    "reason": verdict.get("reason"),
                    "player": verdict.get("player"),
                    "player_team": verdict.get("player_team"),
                    "fixture_teams": verdict.get("fixture_teams"),
                })
            candidates_list = gated
        except Exception as _pt_err:
            logger.warning(
                "publish_batch integrity gate skipped (non-fatal): %s",
                _pt_err,
            )

        results: list[PublicationResult] = []
        errors: list[dict] = []
        n_new = 0
        n_existing = 0
        n_mismatches = 0
        for cand in candidates_list:
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
            # Phase 2 Follow-up: expose integrity rejections so callers
            # can log/report them.  Rejected picks are NOT published.
            "integrity_rejected": len(rejected_integrity),
            "integrity_rejections": rejected_integrity,
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

        # ── P0-1 (2026-08-11): confidence is a LABEL, not a float ──
        # `sports_engine._confidence(lock_score)` returns strings like
        # "Very High", "High", "Medium", "Low", "Very Low", or "Pass".
        # A previous version of this builder ran `float("Very High")`
        # through a numeric coercion helper, silently defaulting to
        # 0.0 and then propagating 0.0 into every legacy alias via
        # dual-write.  We now preserve the label verbatim.
        _conf_raw = candidate.get("confidence")
        if _conf_raw is None or _conf_raw == "":
            _conf_label = LEGACY_UNKNOWN
        else:
            _conf_label = str(_conf_raw)
        # Reserved for a future numeric confidence score.  Currently
        # emitted as None on every snapshot — do NOT synthesize a
        # value from `lock_score` here (that would leak the Lock
        # tier into a probability-shaped field and confuse callers).
        _conf_score: Optional[float] = None

        # ── P0-1: edge is Optional and MUST preserve None ──────────
        # `edge_percent` is the model-vs-book edge in
        # percentage-points.  A pick without a book line has NO
        # meaningful edge — that state must round-trip as `None`, not
        # as `0.0`.  Only real numeric edges (positive or negative)
        # are stored.
        _edge: Optional[float] = _f_or_none("edge_percent")

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
            # Canonical unit: 0-1 fraction.  Percentage inputs (e.g.
            # 68.2) are converted here; fraction inputs (0.682) pass
            # through unchanged.  See `_normalize_probability_at_publish`.
            published_probability=_normalize_probability_at_publish(
                _f_or_none("win_probability")),
            published_edge=_edge,
            published_lock_score=round(_f("lock_score"), 2),
            published_grade=_s("grade", default="Pass"),
            published_confidence=_conf_label,
            published_confidence_score=_conf_score,
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
        Returns (applied, mismatch_logged).

        This method IS the publication service itself, so it is the
        only caller allowed to touch the immutable published fields
        on `picks` — see `services/published_write_guard.py`.
        """
        from services.published_write_guard import (
            assert_no_published_mutation,
        )
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
        # Phase 1b — also sync the legacy aliases so endpoints that
        # still read `pick.lock_score` (before their per-endpoint
        # migration lands) return the published value.  After the
        # hydrate() pass fully rolls out, these aliases are strictly
        # a courtesy for backward compatibility.
        #
        # ── P0-1 (2026-08-11) legacy-unit conversion ────────────────
        # The snapshot stores probability as a 0-1 fraction (canonical),
        # but the frontend `LockPickCard` renders `${pick.win_probability}%`
        # — i.e. it expects 0-100 percentage.  Convert at THIS boundary
        # so the two units never mix on the wire.
        _snap_prob = snap_doc.get("published_probability")
        if _snap_prob is None:
            _legacy_wp: Optional[float] = None
        else:
            try:
                _pf = float(_snap_prob)
            except (TypeError, ValueError):
                _pf = 0.0
            # Canonical value is a fraction in [0, 1]; convert to
            # percentage for the legacy field.  Clamp defensively.
            _legacy_wp = round(max(0.0, min(1.0, _pf)) * 100.0, 2)
        # `published_edge` may be None (no book line); preserve that
        # state through the legacy alias so consumers can distinguish
        # "no line" from "0% edge".
        _snap_edge = snap_doc.get("published_edge")
        # `published_confidence` is now a label string.  Preserve it.
        _snap_conf = snap_doc.get("published_confidence")
        set_payload["lock_score"] = snap_doc.get("published_lock_score")
        set_payload["win_probability"] = _legacy_wp
        set_payload["edge_percent"] = _snap_edge
        set_payload["grade"] = snap_doc.get("published_grade")
        set_payload["confidence"] = _snap_conf
        set_payload["book_odds"] = snap_doc.get("published_odds")
        set_payload["line"] = snap_doc.get("published_line")
        set_payload["reasoning"] = snap_doc.get("published_reasoning")

        try:
            # Publication-owned write — explicitly allowed.
            assert_no_published_mutation(
                {"$set": set_payload},
                allow_publication_write=True,
                caller="prediction_publication_service._dual_write",
            )
            # Phase 1b: capture the picks doc BEFORE the dual-write
            # so we can compare its legacy fields against the fresh
            # snapshot values.  If a non-publication writer had
            # previously drifted a legacy field, that drift is
            # what we report.
            pre_state = await self.db.picks.find_one(
                {"id": payload.prediction_id},
                projection={
                    "lock_score": 1, "win_probability": 1,
                    "edge_percent": 1, "grade": 1,
                    "confidence": 1, "book_odds": 1,
                    "line": 1, "_id": 0,
                },
            )
            res = await self.db.picks.update_one(
                {"id": payload.prediction_id},
                {"$set": set_payload},
                upsert=False,
            )
            applied = bool(getattr(res, "matched_count", 0))
        except Exception as e:
            logger.warning("dual-write update_one err: %s", e)
            return False, False

        # Compare the PRE-write legacy fields vs the snapshot.  If any
        # writer had drifted from the previous publication, we log
        # it here so operators can trace who did it.
        mismatch_logged = False
        if applied and pre_state:
            drifts = _compute_drifts(pre_state, snap_doc)
            if drifts:
                _now = datetime.now(timezone.utc)
                await self.db[MISMATCH_COLLECTION].insert_one({
                    "prediction_id": payload.prediction_id,
                    "board_version": payload.board_version,
                    "logged_at":     _now.isoformat(),   # legacy compat
                    "logged_at_dt":  _now,               # Phase 3K TTL field (BSON Date)
                    "drifts":        drifts,
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
    edge_str = "none" if p.published_edge is None else f"{p.published_edge:.3f}"
    raw = "|".join([
        p.prediction_id,
        p.board_version,
        f"{p.published_probability:.6f}",
        f"{p.published_lock_score:.2f}",
        edge_str,
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
        # Phase 1b — normalize win_probability comparison to fractions
        # on both sides so we don't spuriously flag percent-vs-fraction.
        if legacy == "win_probability":
            lv = _normalize_probability_at_publish(lv)
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


def _normalize_probability_at_publish(value) -> float:
    """Coerce probability to canonical `[0, 1]` fraction at publish
    time.  Kept as a local helper (not imported from the reader) so
    that this module has no circular deps."""
    if value is None:
        return 0.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v <= 1.0:
        return v
    if v <= 100.0:
        return v / 100.0
    return 1.0


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
