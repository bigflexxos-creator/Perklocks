"""Publication Reconciliation — Session A (2026-06).

Safe retry path for picks stuck in ``PUBLICATION_PENDING`` or
``FAILED``.  Bounded and idempotent — the underlying
``PredictionPublicationService.publish`` is already idempotent (unique
key on ``prediction_id + idempotency_key``), so republishing a
successful pick is a no-op.

Scope
─────
Only picks that ALREADY passed the canonical boundary once and
subsequently entered ``FAILED`` (transient) or remain in
``PUBLICATION_PENDING`` (never completed) are retried.

Picks in ``REJECTED`` state are NEVER retried — that state is
permanent by design.  This is what enforces "no infinite retry" for
permanently invalid picks.

Bounded retry
─────────────
* Per-pick attempts are tracked in ``publication_attempts``.
* A pick that exceeds ``MAX_PUBLICATION_ATTEMPTS`` (see boundary
  module) transitions to ``REJECTED`` with reason
  ``MAX_ATTEMPTS_EXCEEDED`` — the reconciler drops it from future
  runs.

Never scheduled from this module — Session A ships the function.  A
scheduler wiring is intentionally OUT of scope so we don't quietly
start a recurring job during a delicate closure.  The final report
MUST tag this as "IMPLEMENTED BUT NOT SCHEDULED".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.canonical_publication_boundary import (
    MAX_PUBLICATION_ATTEMPTS,
    PublicationState,
)

logger = logging.getLogger("lockscore.publication_reconciler")


async def reconcile_stuck_publications(
    db: AsyncIOMotorDatabase,
    *,
    max_age_minutes: int = 5,
    limit: int = 200,
    publication_source: Optional[str] = None,
) -> dict:
    """Find PENDING/FAILED picks older than ``max_age_minutes`` and
    safely retry them through ``publish_batch``.

    Parameters
    ----------
    max_age_minutes :
        Only picks whose ``publication_last_state_at`` is older than
        this age are considered.  Small windows starve the reconciler;
        large windows delay recovery.  5-minute default is safe.
    limit :
        Cap on the number of picks retried per invocation.  Prevents
        a runaway sweep from blocking the event loop.
    publication_source :
        Optional filter — reconcile only picks from a specific
        producer.  ``None`` reconciles ALL sources.

    Returns
    -------
    dict summary with retry counters.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat().replace(
        "+00:00", "Z",
    )

    query: dict[str, Any] = {
        "publication_state": {
            "$in": [
                PublicationState.PUBLICATION_PENDING.value,
                PublicationState.FAILED.value,
            ],
        },
        "publication_last_state_at": {"$lt": cutoff},
    }
    if publication_source:
        query["publication_source"] = publication_source

    stuck = await db.picks.find(query).limit(int(limit)).to_list(
        length=int(limit),
    )
    if not stuck:
        return {
            "ok":         True,
            "cutoff":     cutoff,
            "scanned":    0,
            "retried":    0,
            "published":  0,
            "rejected":   0,
            "failed":     0,
            "exhausted":  0,
        }

    # ── Handle exhausted attempts BEFORE retrying ──────────────────
    exhausted_ids: list[str] = []
    fresh: list[dict] = []
    for p in stuck:
        att = int(p.get("publication_attempts") or 0)
        if att >= MAX_PUBLICATION_ATTEMPTS:
            exhausted_ids.append(p.get("id"))
            continue
        fresh.append(p)

    if exhausted_ids:
        now_iso = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )
        try:
            await db.picks.update_many(
                {"id": {"$in": exhausted_ids}},
                {"$set": {
                    "publication_state": PublicationState.REJECTED.value,
                    "publication_rejected_at": now_iso,
                    "publication_rejection_reasons":
                        ["MAX_ATTEMPTS_EXCEEDED"],
                    "off_board": True,
                    "no_bet":    True,
                }},
            )
        except Exception as e:                      # pragma: no cover
            logger.debug("exhausted mark failed: %s", e)

    if not fresh:
        return {
            "ok": True, "cutoff": cutoff,
            "scanned": len(stuck), "retried": 0,
            "published": 0, "rejected": 0, "failed": 0,
            "exhausted": len(exhausted_ids),
        }

    # ── Group by publication_source and re-run through publish_batch.
    by_src: dict[str, list[dict]] = {}
    for p in fresh:
        src = p.get("publication_source") or "reconciler"
        by_src.setdefault(src, []).append(p)

    from services.prediction_publication_service import (
        PredictionPublicationService,
    )
    publisher = PredictionPublicationService(db)
    try:
        await publisher.ensure_indices()
    except Exception:                                # pragma: no cover
        pass

    total_published = 0
    total_rejected  = 0
    total_failed    = 0
    for src, batch in by_src.items():
        try:
            summary = await publisher.publish_batch(
                batch, publication_source=src, dual_write=True,
            )
            total_published += int(summary.get("new_snapshots", 0)) + int(
                summary.get("existing_snapshots", 0),
            )
            total_rejected += int(summary.get("boundary_rejected", 0)) + int(
                summary.get("integrity_rejected", 0),
            )
            total_failed += int(summary.get("publication_failed", 0))
        except Exception as e:                      # pragma: no cover
            logger.warning("reconciler batch failure for %s: %s", src, e)

    return {
        "ok":         True,
        "cutoff":     cutoff,
        "scanned":    len(stuck),
        "retried":    len(fresh),
        "published":  total_published,
        "rejected":   total_rejected,
        "failed":     total_failed,
        "exhausted":  len(exhausted_ids),
    }


# ═══════════════════════════════════════════════════════════════════
# PERKLOCKS ROOT FIX (2026-09-03) — Rejected Publication Healer
# ───────────────────────────────────────────────────────────────────
# The Canonical Publication Boundary rejects picks whose enrichment
# fields (``model_probability`` and ``identity_class``) are missing
# at first ``publish_batch()`` — but those fields become available
# minutes later via subsequent pipeline passes (scoring, sim, apex,
# identity healer).  Nothing re-evaluates a REJECTED pick, so a
# legitimate MLB hitter / player-prop pick can sit permanently
# off-board with lock_score 95-99 and an empty rejection reason set.
#
# The healer is a **narrow, idempotent, fail-closed** re-evaluation
# sweep that runs alongside the existing FAILED reconciler:
#
#   1. Scan REJECTED picks for the current slate (``pick_date``).
#   2. Run the SAME enrichers ``publish_batch`` uses (identity +
#      model_evidence) in-memory only.  No producer state is
#      mutated on picks that still fail.
#   3. Re-run ``evaluate_publication``.  Fail-closed: any pick that
#      does NOT now PASS is left untouched.
#   4. For picks that NOW pass, atomically:
#        * publication_state           = PUBLISHED
#        * publication_published_at    = now
#        * publication_rejection_reasons = None
#        * publication_last_state_at   = now
#        * off_board                   = False
#        * no_bet                      = False
#        * <enrichment fields>         = <healed value>
#      All in a single ``$set`` so the row can never be observed
#      partially-updated.
#
# NEVER retries a pick that legitimately fails the boundary
# (e.g. MODEL_LINE_NOT_REAL_OFFERING, PLAYER_EVENT_IDENTITY_MISMATCH,
# SETTLEMENT_UNSUPPORTED).  Those are permanent policy failures by
# design — the healer is only for picks the boundary WOULD accept if
# it re-evaluated them right now.
# ═══════════════════════════════════════════════════════════════════
async def heal_rejected_publications(
    db: AsyncIOMotorDatabase,
    *,
    pick_date: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """Re-evaluate REJECTED picks and publish those that now pass.

    Returns a summary dict with ``scanned`` / ``healed`` /
    ``still_rejected`` counters.  Never raises — a healer failure
    degrades to a no-op so the caller's request path is never
    blocked.
    """
    from services.canonical_publication_boundary import evaluate_publication
    from services.pick_identity_enricher import enrich_pick_identity_async
    from services.pick_model_evidence import extract_model_evidence

    query: dict[str, Any] = {
        "publication_state": PublicationState.REJECTED.value,
    }
    if pick_date:
        query["pick_date"] = pick_date

    try:
        rejected = await db.picks.find(query).limit(int(limit)).to_list(
            length=int(limit),
        )
    except Exception as e:                              # pragma: no cover
        logger.warning("heal_rejected: query failed: %s", e)
        return {"ok": False, "scanned": 0, "healed": 0, "still_rejected": 0,
                "error": str(e)}

    if not rejected:
        return {"ok": True, "scanned": 0, "healed": 0, "still_rejected": 0}

    healed_ids: list[str] = []
    still_rejected = 0
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for p in rejected:
        pid = p.get("id") or p.get("prediction_id")
        if not pid:
            continue
        # ── Enrichment step (identity + model evidence) — mirrors
        #    the exact block inside ``publish_batch``.  Only fills
        #    empty fields so downstream producer state is preserved.
        try:
            ident = await enrich_pick_identity_async(db, p)
        except Exception:
            ident = {}
        try:
            model = extract_model_evidence(p)
        except Exception:
            model = {}
        healed_fields: dict[str, Any] = {}
        for k, v in {**ident, **model}.items():
            if v is None:
                continue
            cur = p.get(k)
            if cur in (None, "", []):
                p[k] = v
                healed_fields[k] = v
            elif k == "identity_class" and (
                cur not in (
                    "AUTHORITATIVE", "MAPPED",
                    "PROVISIONAL", "UNRESOLVED",
                )
            ):
                p[k] = v
                healed_fields[k] = v

        # ── MAGIC 3A.1 line/side/line_source attach (matches
        #    publish_batch).  Idempotent when fields already exist.
        try:
            from services.magic.line_wire import attach_line_fields
            attach_line_fields(p)
            # If the wire mutated the pick, capture the healed fields
            # for the atomic $set (only fields not already saved).
            for _k in ("line", "side", "line_source"):
                _v = p.get(_k)
                if _v is not None and _k not in healed_fields:
                    healed_fields[_k] = _v
        except Exception:
            pass

        # ── Re-run the canonical boundary.  Fail-closed: leave
        #    still-invalid picks REJECTED and untouched.
        try:
            verdict = evaluate_publication(p)
        except Exception as _be:                        # pragma: no cover
            logger.debug("heal_rejected: boundary raised for %s: %s",
                         pid, _be)
            still_rejected += 1
            continue
        if verdict.state != PublicationState.PUBLISHED:
            still_rejected += 1
            continue

        # ── Atomic PUBLISHED write.  Includes:
        #    * lifecycle fields (state + timestamps)
        #    * board-visibility flags (off_board + no_bet)
        #    * every healed enrichment field discovered above
        #      (so downstream readers see the enriched row).
        set_fields: dict[str, Any] = {
            "publication_state":            PublicationState.PUBLISHED.value,
            "publication_published_at":     now_iso,
            "publication_last_state_at":    now_iso,
            "publication_rejection_reasons": None,
            "publication_last_error":       None,
            "publication_source":           p.get("publication_source")
                                             or "healer",
            "off_board":                    False,
            "no_bet":                       False,
            "publication_healed_at":        now_iso,
        }
        for _hk, _hv in healed_fields.items():
            # Never overwrite a canonical lifecycle field with an
            # enrichment side-effect.
            if _hk in set_fields:
                continue
            set_fields[_hk] = _hv
        try:
            await db.picks.update_one(
                {"id": pid,
                 "publication_state": PublicationState.REJECTED.value},
                {"$set": set_fields},
            )
            healed_ids.append(pid)
        except Exception as _we:                        # pragma: no cover
            logger.debug("heal_rejected: write failed for %s: %s",
                         pid, _we)
            still_rejected += 1

    if healed_ids:
        logger.info(
            "heal_rejected_publications: healed=%d still_rejected=%d "
            "pick_date=%s",
            len(healed_ids), still_rejected, pick_date or "*",
        )
    return {
        "ok":              True,
        "scanned":         len(rejected),
        "healed":          len(healed_ids),
        "still_rejected":  still_rejected,
    }


__all__ = ["reconcile_stuck_publications", "heal_rejected_publications"]
