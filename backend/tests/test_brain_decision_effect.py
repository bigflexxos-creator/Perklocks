"""Brain Decision-Effect μ-closure — focused pre-publication tests.

Certifies that the shared convergence classifier's
``confidence_multiplier`` actively modulates the EXISTING ``lock_score``
input BEFORE the canonical publisher freezes it into
``published_lock_score``.

Contract:
  1. DECISION EFFECT — for otherwise identical candidates, a
     STRONG_CONVERGENCE + STRONG + REAL_PLAYER_CONTEXT candidate has a
     HIGHER pre-publication ``lock_score`` than a
     STRONG_DISAGREEMENT + WEAK + PRIOR_ONLY candidate.  The adjustment
     is applied to the SAME canonical field.

  2. CANONICAL SAFETY — the pipeline still emits ONE ``lock_score`` /
     ONE ``published_lock_score``.  No ``lock_score_v3``,
     ``brain_lock_score``, or ``adjusted_lock_score`` truth is created.

  3. FAVORITE / UNDERDOG NEUTRALITY — for identical convergence /
     evidence / provenance inputs, favorite (short odds) and underdog
     (long odds) candidates receive the SAME adjustment.

  4. SIMULATOR PROVENANCE — for identical convergence + evidence,
     REAL_PLAYER_CONTEXT provenance produces a strictly BETTER
     pre-publication ``lock_score`` than PRIOR_ONLY provenance.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── In-memory MongoDB stand-in ────────────────────────────────────────
class _StubUpdateResult:
    matched_count = 1
    modified_count = 1


class _StubPicksCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.updates: list[tuple[dict, dict]] = []

    async def update_one(self, flt: dict, update: dict, **_kw: Any):
        self.updates.append((flt, update))
        pid = flt.get("id")
        if pid is not None:
            doc = self.docs.setdefault(pid, {"id": pid})
            for k, v in (update.get("$set") or {}).items():
                doc[k] = v
        return _StubUpdateResult()

    async def find_one(self, flt: dict, *_a: Any, **_kw: Any):
        return self.docs.get(flt.get("id"))


class _StubDB:
    def __init__(self) -> None:
        self.picks = _StubPicksCollection()


# ── Isolate publish_upserted_picks from downstream side-effects ──────
async def _run_publication_helper(picks: list[dict], monkeypatch) -> _StubDB:
    """Runs the shared publication helper against an in-memory DB while
    stubbing every downstream side-effect — enrichers, MLB stamp,
    player↔team gate, publisher, observer.  The ONLY behavior exercised
    is the shared convergence pre-publication adjustment."""
    # Import the helper module fresh so monkeypatches apply.
    from services import publication_helpers as ph

    # 1. Neutralize identity/model enrichers.
    async def _noop_ident(_db, _p):  # noqa: D401
        return {}

    def _noop_model(_p):  # noqa: D401
        return {}

    monkeypatch.setattr(
        "services.pick_identity_enricher.enrich_pick_identity_async",
        _noop_ident,
    )
    monkeypatch.setattr(
        "services.pick_model_evidence.extract_model_evidence",
        _noop_model,
    )

    # 2. Neutralize MLB producer stamp (would try DB reads).
    async def _noop_mlb(_db, _p):  # noqa: D401
        return {}

    monkeypatch.setattr(
        "services.mlb_producer_identity_stamp.stamp_mlb_producer_identity",
        _noop_mlb,
    )

    # 3. Neutralize player-team fixture gate (Soccer only in the helper).
    #    Our test picks never claim sport=="Soccer", so the gate is a
    #    no-op naturally.  No stub needed.

    # 4. Stub PredictionPublicationService — capture what it would
    #    publish so we can assert exactly which lock_score value would
    #    have been frozen.
    published_lock_scores: dict[str, float] = {}

    class _StubPublisher:
        def __init__(self, _db):
            self._db = _db

        async def ensure_indices(self):
            return None

        async def publish_batch(self, picks_list, **_kw):
            for _p in picks_list:
                pid = _p.get("id")
                if pid is not None:
                    published_lock_scores[pid] = float(_p.get("lock_score"))
                    # Simulate the dual-write that stamps
                    # published_lock_score on the picks doc.
                    await self._db.picks.update_one(
                        {"id": pid},
                        {"$set": {
                            "published_lock_score": round(
                                float(_p.get("lock_score")), 2),
                            "published_probability": (
                                float(_p.get("win_probability") or 0) / 100.0),
                        }},
                    )
            return {"new_snapshots": len(picks_list),
                    "existing_snapshots": 0,
                    "errors": [],
                    "mismatches_logged": 0,
                    "board_version": "v-test"}

    monkeypatch.setattr(
        "services.prediction_publication_service."
        "PredictionPublicationService",
        _StubPublisher,
    )

    # 5. Neutralize the production_truth observer (best-effort noop).
    async def _noop_obs(*_a, **_kw):  # noqa: D401
        return None

    monkeypatch.setattr(
        "services.production_truth.publication_observer.observe_publication",
        _noop_obs,
    )

    db = _StubDB()
    # Seed the pre-existing pick docs so update_one's $set merges into
    # a real dict.  publish_upserted_picks assumes the pick has already
    # been upserted.
    for _p in picks:
        db.picks.docs[_p["id"]] = dict(_p)

    summary = await ph.publish_upserted_picks(
        db, picks,
        publication_source="brain_decision_effect_test",
        caller_label="decision_effect_test",
    )
    # Attach for assertions.
    db._published_lock_scores = published_lock_scores  # type: ignore[attr-defined]
    db._publish_summary = summary  # type: ignore[attr-defined]
    return db


def _make_candidate(
    *, pid: str,
    p_v1: float, p_v2: float, sim_p: float | None,
    sim_provenance: str, evidence_quality: str,
    book_odds: int, lock_score: float,
) -> dict:
    """Build a minimal pick dict that the shared helper can classify."""
    return {
        "id": pid,
        "sport": "MLB",
        "market": "Test",
        "selection": "Over",
        "win_probability": round(p_v1 * 100, 2),
        "model_probability": p_v1,
        "simulator_probability": sim_p,
        "sim_win_probability": sim_p if sim_p else 0.0,
        "simulator_provenance": sim_provenance,
        "evidence_quality": evidence_quality,
        "book_odds": book_odds,
        "lock_score": lock_score,
        "implied_probability": 50.0,  # informational
        # Ensure the second component is present — the classifier uses
        # p_v2 (which the helper derives from sim_p when sim_ran else
        # model_p).  We stamp explicit values here to keep the test
        # honest.
        "p_v2_stamp": p_v2,
    }


# ─────────────────────────────────────────────────────────────────────
# TEST 1 — DECISION EFFECT
# ─────────────────────────────────────────────────────────────────────
def test_decision_effect_strong_convergence_vs_disagreement(monkeypatch):
    """A: strong convergence / STRONG / REAL_PLAYER_CONTEXT
       B: strong disagreement / WEAK / PRIOR_ONLY

       Both start at the SAME lock_score.  Post-publication the
       canonical ``lock_score`` on A must be > B, and A must equal the
       original (multiplier 1.00 leaves it untouched)."""
    orig_lock = 95.0

    # A: model 0.62, sim 0.62 → spread ~0.00 → STRONG_CONVERGENCE
    cand_a = _make_candidate(
        pid="A",
        p_v1=0.62, p_v2=0.62, sim_p=0.62,
        sim_provenance="REAL_PLAYER_CONTEXT",
        evidence_quality="STRONG",
        book_odds=-150, lock_score=orig_lock,
    )
    # B: model 0.62, sim 0.30 → spread 0.32 → STRONG_DISAGREEMENT
    cand_b = _make_candidate(
        pid="B",
        p_v1=0.62, p_v2=0.30, sim_p=0.30,
        sim_provenance="PRIOR_ONLY",
        evidence_quality="WEAK",
        book_odds=-150, lock_score=orig_lock,
    )

    db = asyncio.run(_run_publication_helper([cand_a, cand_b], monkeypatch))
    doc_a = db.picks.docs["A"]
    doc_b = db.picks.docs["B"]

    # A is untouched (STRONG_CONVERGENCE multiplier = 1.00).
    assert doc_a["lock_score"] == pytest.approx(orig_lock, abs=1e-6), (
        f"STRONG_CONVERGENCE candidate must not have its lock_score "
        f"reduced (got {doc_a['lock_score']}, expected {orig_lock})"
    )
    assert doc_a["convergence_label"] == "STRONG_CONVERGENCE"

    # B is downgraded — multiplier ~0.55 → 70 + 25 * 0.55 = 83.75.
    assert doc_b["lock_score"] < orig_lock, (
        f"STRONG_DISAGREEMENT/WEAK/PRIOR_ONLY must lower lock_score "
        f"(got {doc_b['lock_score']}, expected < {orig_lock})"
    )
    assert doc_b["convergence_label"] == "STRONG_DISAGREEMENT"

    # DECISION EFFECT: quality(A) strictly > quality(B).
    assert doc_a["lock_score"] > doc_b["lock_score"], (
        f"Decision effect failed: quality(A)={doc_a['lock_score']} "
        f"must be > quality(B)={doc_b['lock_score']}"
    )

    # Pre-publication effect: publisher stub captured the SAME adjusted
    # value as published_lock_score — proving the mutation happens
    # BEFORE publish_batch reads it.
    assert db._published_lock_scores["B"] == doc_b["lock_score"]
    assert db._published_lock_scores["A"] == doc_a["lock_score"]


# ─────────────────────────────────────────────────────────────────────
# TEST 2 — CANONICAL SAFETY (single truth preserved)
# ─────────────────────────────────────────────────────────────────────
def test_canonical_single_truth_preserved(monkeypatch):
    """No shadow / v3 / brain_lock_score field is emitted, and the
    diagnostic sidecars are clearly NOT alternative Lock Scores."""
    cand = _make_candidate(
        pid="C",
        p_v1=0.62, p_v2=0.30, sim_p=0.30,
        sim_provenance="PRIOR_ONLY",
        evidence_quality="WEAK",
        book_odds=-150, lock_score=95.0,
    )
    db = asyncio.run(_run_publication_helper([cand], monkeypatch))
    doc = db.picks.docs["C"]

    # Forbidden alternative truths:
    for forbidden in ("lock_score_v3", "brain_lock_score",
                       "adjusted_lock_score", "convergence_lock_score",
                       "convergence_score"):
        assert forbidden not in doc, (
            f"Forbidden alternative Lock Score field '{forbidden}' "
            f"MUST NOT be written by the μ-closure"
        )

    # Exactly ONE canonical field ('lock_score') + ONE frozen field
    # ('published_lock_score').  The sidecars are DIAGNOSTIC ONLY.
    assert "lock_score" in doc
    assert "published_lock_score" in doc
    assert doc["published_lock_score"] == pytest.approx(doc["lock_score"])

    # Sidecars are allowed (diagnostic) but must NOT be treated as
    # additional Lock Score truths — asserted here by name.
    assert "lock_score_pre_convergence" in doc
    assert "convergence_lock_score_delta" in doc
    # The sidecar preserves the pre-adjustment value for audit only.
    assert doc["lock_score_pre_convergence"] == pytest.approx(95.0)


# ─────────────────────────────────────────────────────────────────────
# TEST 3 — FAVORITE / UNDERDOG NEUTRALITY
# ─────────────────────────────────────────────────────────────────────
def test_favorite_underdog_neutrality(monkeypatch):
    """Favorite (-350) and underdog (+280) with identical convergence /
    evidence / provenance must receive the SAME adjustment."""
    orig_lock = 92.0
    fav = _make_candidate(
        pid="FAV",
        p_v1=0.55, p_v2=0.30, sim_p=0.30,
        sim_provenance="PRIOR_ONLY",
        evidence_quality="WEAK",
        book_odds=-350, lock_score=orig_lock,
    )
    dog = _make_candidate(
        pid="DOG",
        p_v1=0.55, p_v2=0.30, sim_p=0.30,
        sim_provenance="PRIOR_ONLY",
        evidence_quality="WEAK",
        book_odds=+280, lock_score=orig_lock,
    )
    db = asyncio.run(_run_publication_helper([fav, dog], monkeypatch))
    fav_doc = db.picks.docs["FAV"]
    dog_doc = db.picks.docs["DOG"]

    # Both dropped by the same amount — no chalk bias, no dog penalty.
    # (Small tolerance because implied is a spread component and
    # differs between the two — but the convergence LABEL and
    # multiplier bracket should be identical, and typically the
    # multiplier itself is identical because model↔sim spread
    # dominates.  The key contract: the difference is bounded by the
    # implied-derived spread only, NOT by favorite/underdog role.)
    delta_fav = orig_lock - fav_doc["lock_score"]
    delta_dog = orig_lock - dog_doc["lock_score"]
    # Same multiplier bucket (STRONG_DISAGREEMENT) → same base.  The
    # implied contribution to spread is bounded; assert deltas are
    # within 1.0 point of each other — proving no favorite/underdog
    # BIAS is introduced by the adjustment.
    assert abs(delta_fav - delta_dog) < 1.0, (
        f"Favorite/underdog neutrality violated: "
        f"favorite delta={delta_fav}, underdog delta={delta_dog}"
    )


# ─────────────────────────────────────────────────────────────────────
# TEST 4 — SIMULATOR PROVENANCE DECISION EFFECT
# ─────────────────────────────────────────────────────────────────────
def test_simulator_provenance_decision_effect(monkeypatch):
    """Same convergence + evidence, different provenance:
       REAL_PLAYER_CONTEXT should retain more quality than PRIOR_ONLY."""
    orig_lock = 95.0
    # Both candidates: same model↔sim agreement (spread 0.00 →
    # STRONG_CONVERGENCE), same STRONG evidence.  Only provenance
    # varies.
    real = _make_candidate(
        pid="REAL",
        p_v1=0.62, p_v2=0.62, sim_p=0.62,
        sim_provenance="REAL_PLAYER_CONTEXT",
        evidence_quality="STRONG",
        book_odds=-150, lock_score=orig_lock,
    )
    prior = _make_candidate(
        pid="PRIOR",
        p_v1=0.62, p_v2=0.62, sim_p=0.62,
        sim_provenance="PRIOR_ONLY",
        evidence_quality="STRONG",
        book_odds=-150, lock_score=orig_lock,
    )
    db = asyncio.run(_run_publication_helper([real, prior], monkeypatch))
    real_doc = db.picks.docs["REAL"]
    prior_doc = db.picks.docs["PRIOR"]

    # REAL_PLAYER_CONTEXT: mult = 1.00 → lock_score unchanged.
    assert real_doc["lock_score"] == pytest.approx(orig_lock, abs=1e-6)

    # PRIOR_ONLY: mult capped at 0.72 → 70 + 25*0.72 = 88.0.
    assert prior_doc["lock_score"] < orig_lock

    # Decision effect: full-context provenance yields strictly better
    # canonical Lock Score than PRIOR_ONLY.
    assert real_doc["lock_score"] > prior_doc["lock_score"]
