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

    # ═══════════════════════════════════════════════════════════════
    # Pre-Magic Remediation (2026-06) — Canonical Pick Identity +
    # Model Evidence attachment.
    #
    # These two enrichers run at the CENTRAL publication choke point
    # so every producer that flows through this helper gets identity
    # and model-evidence attached without producer-specific changes
    # (§1 — no producer bypass).
    #
    # * Enrichers are pure (no DB reads), deterministic (§4), and
    #   idempotent — safe on republication.
    # * They NEVER alter scoring, model probability values, publication
    #   eligibility, or off_board flags.
    # * A pick that already carries a canonical_*_id is authoritative
    #   — the enricher only fills in the fields that were missing (§3).
    # * A failure in either enricher is swallowed — the ingest loop
    #   must never break because identity resolution encountered an
    #   edge case.
    # ═══════════════════════════════════════════════════════════════
    try:
        from services.pick_identity_enricher import enrich_pick_identity_async
        from services.pick_model_evidence import extract_model_evidence
        # ── Brain Runtime Wiring μ-closure (2026-06) ─────────────────
        # Every candidate that passes through this canonical
        # publication chokepoint receives ONE shared convergence
        # classification.  Zero per-sport engine changes — the sport
        # runtime has already stamped whatever metadata it knows
        # (model_probability / simulator_probability /
        # simulator_provenance / evidence_quality / lineup context).
        # This shared pass reads that metadata (falling back to
        # inferences from existing pick fields when the sport
        # engine didn't stamp explicitly), calls the shared
        # ``classify_convergence`` classifier, and writes back
        # three DIAGNOSTIC fields on the pick doc:
        #   convergence_label
        #   convergence_spread_pp
        #   convergence_confidence_multiplier
        # It NEVER mutates ``win_probability`` / ``published_*`` /
        # ``lock_score`` — canonical truth remains frozen.
        try:
            from probability_engine import (
                classify_convergence,
                implied_probability_from_odds,
            )
        except Exception:
            classify_convergence          = None   # type: ignore
            implied_probability_from_odds = None   # type: ignore
        _enriched_count = 0
        _model_evidence_count = 0
        _convergence_count = 0
        for p in picks_list:
            try:
                ident = await enrich_pick_identity_async(db, p)
            except Exception as _e_id:
                logger.debug("identity enricher raised on pick %s: %s",
                              p.get("id"), _e_id)
                ident = {}
            try:
                model = extract_model_evidence(p)
            except Exception as _e_me:
                logger.debug("model evidence extractor raised on pick %s: %s",
                              p.get("id"), _e_me)
                model = {}
            # ── Shared convergence stamp ─────────────────────────
            if classify_convergence is not None:
                try:
                    # Pull whatever the sport runtime already provided.
                    _model_p = p.get("model_probability") \
                                if p.get("model_probability") is not None \
                                else p.get("win_probability")
                    _sim_p   = p.get("simulator_probability") \
                                if p.get("simulator_probability") is not None \
                                else p.get("sim_win_probability")
                    _sim_prov = (p.get("simulator_provenance")
                                    or p.get("sim_provenance")
                                    or (model or {}).get("simulator_provenance"))
                    _ev_q     = (p.get("evidence_quality")
                                    or (model or {}).get("evidence_quality")
                                    or "MODERATE")
                    _implied = None
                    if implied_probability_from_odds is not None:
                        _bo = p.get("book_odds") or p.get("odds_at_pick")
                        try:
                            _implied = implied_probability_from_odds(_bo)
                        except Exception:
                            _implied = None
                    _sim_ran = isinstance(_sim_p, (int, float)) and _sim_p > 0
                    if isinstance(_model_p, (int, float)) and _model_p > 0:
                        _p_v2 = _sim_p if _sim_ran else _model_p
                        _conv = classify_convergence(
                            p_v1              = float(_model_p),
                            p_v2              = float(_p_v2),
                            p_sim             = float(_sim_p) if _sim_ran else None,
                            implied           = _implied,
                            sim_provenance    = _sim_prov,
                            evidence_quality  = _ev_q,
                            sim_ran           = _sim_ran,
                        )
                        p["convergence_label"] = _conv["label"]
                        p["convergence_spread_pp"] = _conv["spread_pp"]
                        p["convergence_confidence_multiplier"] = (
                            _conv["confidence_multiplier"])
                        # Preserve evidence + provenance we resolved
                        # so downstream can trust the same tags.
                        if not p.get("evidence_quality"):
                            p["evidence_quality"] = _conv["evidence_quality"]
                        if not (p.get("simulator_provenance")
                                 or p.get("sim_provenance")):
                            p["simulator_provenance"] = _conv["sim_provenance"]
                        _convergence_count += 1
                except Exception as _e_cv:
                    logger.debug("convergence enricher raised on pick %s: %s",
                                  p.get("id"), _e_cv)
            # Compute update_fields FIRST against the original (pre-
            # merge) pick, so we know which values we actually need
            # to persist.  Producer-supplied canonical values are
            # authoritative (§3) — we only fill missing/empty keys.
            # A provisional ``fallback:*`` id IS considered upgradable
            # when the enricher returns a non-fallback (authoritative)
            # id — real producer IDs > deterministic hashes.
            update_fields: dict = {}
            _CANON_ID_KEYS = ("canonical_team_id", "canonical_player_id",
                                "canonical_opponent_id",
                                "canonical_event_id")
            # identity_class MUST be refreshed on every republication so
            # a previously PROVISIONAL pick can move to AUTHORITATIVE
            # once history/registry data lands (§1 Final Closure —
            # false canonical must decay to real canonical).
            _ALWAYS_REFRESH = ("identity_class", "identity_quality",
                                 "identity_resolution",
                                 "pick_identity_version",
                                 "identity_enriched_at",
                                 "event_identity_class")
            for k, v in {**ident, **model}.items():
                if v is None:
                    continue
                cur = p.get(k)
                if cur in (None, "", []):
                    update_fields[k] = v
                    continue
                if k in _ALWAYS_REFRESH:
                    update_fields[k] = v
                    continue
                # Upgrade fallback ids to authoritative ones when
                # the new value is NOT a fallback (§3).
                if k in _CANON_ID_KEYS and isinstance(cur, str) and \
                        cur.startswith("fallback:") and \
                        isinstance(v, str) and not v.startswith("fallback:") \
                        and not v.startswith("unresolved:"):
                    update_fields[k] = v
                    continue
                # Refresh identity_quality when upgrading.
                if k == "identity_quality" and cur == "fallback" and \
                        v == "authoritative":
                    update_fields[k] = v
            # Merge onto the in-memory pick dict so downstream calls
            # in this batch (publish_batch, observer) see the new
            # fields immediately.
            for k, v in update_fields.items():
                p[k] = v
            # ── Brain Runtime Wiring — persist convergence stamp ──
            # The convergence enricher above wrote fields directly onto
            # the in-memory ``p`` dict; add them to update_fields so
            # the DB row persists them alongside identity/model_evidence.
            for _cv_k in ("convergence_label", "convergence_spread_pp",
                           "convergence_confidence_multiplier",
                           "evidence_quality", "simulator_provenance"):
                _cv_v = p.get(_cv_k)
                if _cv_v is not None:
                    update_fields[_cv_k] = _cv_v
            # Persist to the ALREADY-UPSERTED pick document so future
            # queries (Pre-Magic cert, Magic 2.0 when eventually
            # wired, history joins) see canonical identity.
            if update_fields and p.get("id"):
                try:
                    await db.picks.update_one(
                        {"id": p["id"]},
                        {"$set": update_fields},
                    )
                    if "canonical_team_id" in update_fields or \
                       "canonical_player_id" in update_fields or \
                       "canonical_event_id" in update_fields:
                        _enriched_count += 1
                    if "model_probability" in update_fields:
                        _model_evidence_count += 1
                except Exception as _upd_err:
                    logger.debug(
                        "identity/model persist failed for %s: %s",
                        p.get("id"), _upd_err,
                    )
            # ── MAGIC 3D.3 — MLB producer-side canonical stamp ─────
            # After the generic identity enricher runs, MLB player-
            # market picks may still lack a canonical_player_id that
            # joins to mlb_statcast_players / mlb_stuff_plus_players.
            # Stamp the MLB Stats API id at the PRODUCER boundary so
            # every new MLB pick reaches Magic Gold evidence with an
            # authoritative id — no backfill script required.
            # * Existing AUTHORITATIVE id wins — never overwrite.
            # * Ambiguous / unresolved → leave as-is.
            try:
                from services.mlb_producer_identity_stamp import (
                    stamp_mlb_producer_identity,
                )
                mlb_stamp = await stamp_mlb_producer_identity(db, p)
                if mlb_stamp and p.get("id"):
                    for k, v in mlb_stamp.items():
                        p[k] = v
                    try:
                        await db.picks.update_one(
                            {"id": p["id"]},
                            {"$set": mlb_stamp},
                        )
                        _enriched_count += 1
                    except Exception as _mlb_upd_err:
                        logger.debug(
                            "mlb producer stamp persist failed for %s: %s",
                            p.get("id"), _mlb_upd_err,
                        )
            except Exception as _mlb_err:
                logger.debug(
                    "mlb producer stamp raised on pick %s: %s",
                    p.get("id"), _mlb_err,
                )
        if _enriched_count or _model_evidence_count:
            logger.info(
                "%s canonical identity/model enrichment: "
                "identity_added=%d model_probability_added=%d",
                caller_label, _enriched_count, _model_evidence_count,
            )
    except Exception as _enrich_err:
        # Never let enrichment break the publication loop.
        logger.warning(
            "%s canonical enrichment step skipped (non-fatal): %s",
            caller_label, _enrich_err,
        )

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
