"""Shared helper for post-upsert canonical publication.

Purpose
───────
`PredictionPublicationService.publish_batch()` is the ONE write barrier
per PUBLICATION_CONTRACT.md.  Ingest paths that upsert candidates into
`db.picks` outside the main `pick_refresh_orchestrator` (e.g.
`espn_soccer_fixtures`, `soccer/pipeline`, `soccer_hot_scorers`,
`ufc_espn_ingest`) must call this helper right after their upsert
completes so an immutable snapshot exists for each pick before the
canonical board eligibility gate examines it.

This helper is a THIN wrapper — it does not modify prediction values,
does not recompute lock_score / probability / grade / confidence /
Magic Tier, and does not touch Rollover, ranking, or simulators.  It
delegates 100% of the publication payload construction to the
existing `PredictionPublicationService`.

Idempotency, atomicity, mismatch reporting, and error isolation are
all handled inside `publish_batch()`.  A publication failure never
raises — it is logged at WARNING and the upsert path continues.

Usage:
    from services.publication_helpers import publish_upserted_picks
    await publish_upserted_picks(
        db, all_picks, publication_source="ufc_espn_v1",
        caller_label="UFC ESPN sync",
    )
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger("lockscore.publication_helpers")


async def publish_upserted_picks(
    db,
    picks: Iterable[dict],
    *,
    publication_source: str,
    caller_label: str = "ingest",
    dual_write: bool = True,
) -> dict:
    """Publish an iterable of already-upserted picks canonically.

    Returns the publish_batch summary dict, or an empty ``{}`` when
    the publication step raised (never re-raised — degraded
    visibility is preferable to a broken ingest loop).

    Parameters
    ----------
    db :
        Motor `AsyncIOMotorDatabase` instance (same one used by the
        caller's upsert).
    picks :
        Iterable of pick dicts that were just upserted.  Every dict
        must carry the same stable `id` used in the upsert filter so
        publication's `prediction_id` matches.
    publication_source :
        The `publication_source` string that appears on the resulting
        snapshot and the dual-write.  Should identify the ingest path
        (e.g. ``"ufc_espn_v1"``, ``"espn_soccer_fixtures"``,
        ``"soccer_v1"``, ``"soccer_hot_scorers_v1"``).
    caller_label :
        Human-friendly label used only for log messages.
    dual_write :
        Passthrough — always `True` in Phase 1a per contract.
    """
    picks_list = list(picks)
    if not picks_list:
        return {}

    # ── Phase 2 (2026-08-11) Layer-B integrity gate ─────────────────
    # Immediately before canonical publication we drop any player-based
    # Soccer pick whose player's CURRENT team is not on the fixture.
    # Invalid picks NEVER receive `publication_source` — they are
    # excluded from the publish batch and re-marked `off_board=True`
    # with a structured `player_team_invalid_reason` on the picks doc.
    #
    # Fresh roster observation comes from `services.mls_scorer_gate`
    # (ESPN MLS stats + any additional roster snapshots).  When the
    # gate is empty we DO NOT auto-approve — we require an observation
    # per player (rejection reason `roster_unverified`).
    try:
        from services.player_team_fixture_validator import (
            validate_player_fixture_pick, tag_pick_with_verdict, _norm,
        )
        # Build the roster lookup from any snapshot the caller
        # already hydrated onto the pick as `player_current_team`,
        # OR from the MLS scorer gate module-global (best-effort).
        roster_lookup: dict[str, str] = {}
        fresh_names: set[str] = set()
        try:
            from services import mls_scorer_gate as _mls
            snap = getattr(_mls, "_espn_by_name", None) or {}
            for name, entry in snap.items():
                t = entry.get("team") if isinstance(entry, dict) else None
                if t:
                    key = _norm(name)
                    roster_lookup[key] = t
                    fresh_names.add(key)
        except Exception:
            pass
        # Also merge any per-pick `player_current_team` fields the
        # writer may have stamped upstream.
        for p in picks_list:
            pn = p.get("player_name") or p.get("player")
            pct = p.get("player_current_team")
            if isinstance(pn, str) and isinstance(pct, str):
                key = _norm(pn)
                roster_lookup[key] = pct
                # Consider caller-supplied team as fresh evidence.
                fresh_names.add(key)

        valid_picks: list[dict] = []
        rejected = 0
        for p in picks_list:
            if p.get("sport") != "Soccer":
                valid_picks.append(p)
                continue
            verdict = validate_player_fixture_pick(
                p, roster_lookup,
                fresh_roster_names=(fresh_names or None),
            )
            if verdict.get("verified"):
                valid_picks.append(p)
                continue
            # Player-market mismatch OR roster unverified — quarantine.
            tag_pick_with_verdict(p, verdict)
            rejected += 1
            try:
                await db.picks.update_one(
                    {"id": p.get("id")},
                    {"$set": {
                        "off_board": True,
                        "off_board_reasons": [
                            "player_team_invalid",
                            verdict.get("reason") or "unknown",
                        ],
                        "player_team_invalid": True,
                        "player_team_invalid_reason": verdict.get("reason"),
                    }}
                )
            except Exception as _upd_err:
                logger.debug(
                    "player_team invalidate mark failed for %s: %s",
                    p.get("id"), _upd_err,
                )
        if rejected:
            logger.info(
                "%s player↔team gate: %d Soccer player-props quarantined "
                "(will NOT be published)",
                caller_label, rejected,
            )
        picks_list = valid_picks
        if not picks_list:
            return {"new_snapshots": 0, "existing_snapshots": 0,
                    "errors": [], "player_team_rejected": rejected}
    except Exception as _pt_err:
        # Validator failure must not block publication — log and
        # continue with the original batch.
        logger.warning(
            "%s player↔team gate skipped (non-fatal): %s",
            caller_label, _pt_err,
        )

    try:
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        publisher = PredictionPublicationService(db)
        try:
            await publisher.ensure_indices()
        except Exception as _idx_err:  # pragma: no cover
            logger.debug("publication ensure_indices for %s: %s",
                         caller_label, _idx_err)
        summary = await publisher.publish_batch(
            picks_list,
            publication_source=publication_source,
            dual_write=dual_write,
        )
        logger.info(
            "%s publication: new=%d existing=%d errors=%d mismatches=%d "
            "board=%s",
            caller_label,
            summary.get("new_snapshots", 0),
            summary.get("existing_snapshots", 0),
            len(summary.get("errors", []) or []),
            summary.get("mismatches_logged", 0),
            summary.get("board_version"),
        )
        # ── Production-Truth OBSERVE hook (2026-06) ─────────────
        # Read-only observation of the just-published batch.
        # NEVER blocks or mutates publication.  Freezes an immutable
        # pregame snapshot for newly-published qualifying picks
        # and records reachability violations in OBSERVE mode.
        try:
            from services.production_truth.publication_observer import (
                observe_publication,
            )
            await observe_publication(
                db, picks_list,
                publication_source=publication_source,
                caller_label=caller_label,
            )
        except Exception as _obs_err:            # pragma: no cover
            logger.debug(
                "%s production_truth observer failed (non-fatal): %s",
                caller_label, _obs_err,
            )
        return summary
    except Exception as e:
        # Never let publication failure break the ingest loop.
        logger.warning(
            "%s publication step failed (non-fatal): %s",
            caller_label, e,
        )
        return {}


__all__ = ["publish_upserted_picks"]
