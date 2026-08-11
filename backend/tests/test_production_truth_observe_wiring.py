"""PERKLOCKS — Universal Production-Truth OBSERVE wiring tests (session 2).

Locks the real-flow instrumentation:

  §1  publication_helpers wires the OBSERVE hook after publish_batch.
  §2  mls_direct_inject wires the OBSERVE hook (DIRECT_INJECT origin).
  §3  soccer_prop_inject wires the OBSERVE hook (DIRECT_INJECT origin).
  §4  Observer produces one immutable snapshot per canonical publication.
  §5  Retrying an already-published prediction produces ALREADY_FROZEN,
      never a duplicate snapshot, never mutation.
  §6  Superseding predictions use the append-only supersedes mechanism.
  §7  OBSERVE mode records violations without rejecting candidates.
  §8  Generated-but-publication-rejected picks are NOT frozen and
      remain distinguishable in the observation record.
  §9  Published-but-not-visible is diagnosable via the observation.
  §10 DIRECT_INJECT origin flows through the Consumption Proof.
  §11 UNKNOWN remains UNKNOWN when production evidence is absent.
  §12 NOT_APPLICABLE remains distinct from PASS in the observation.
  §13 Consumer eligibility is observed but never changed.
  §14 Consumption Proof uses recorded observation, not module existence.
"""
from __future__ import annotations

import asyncio
import sys
import pytest

sys.path.insert(0, "/app/backend")


pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# Fake DB primitives (async-compatible, per collection)
# ═══════════════════════════════════════════════════════════════════
class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        class _R:
            inserted_id = "fake"
        return _R()

    async def update_one(self, query, update, upsert=False):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                for k, v in update.get("$set", {}).items():
                    self.docs[i][k] = v
                return
        if upsert:
            merged = dict(query)
            merged.update(update.get("$set", {}))
            self.docs.append(merged)

    async def create_index(self, *a, **kw):
        return None


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}
    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection())
    # Also allow attribute-style access (``db.picks``)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.__getitem__(name)


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# §1/§2/§3 — Static wiring: each publication path calls the observer
# ═══════════════════════════════════════════════════════════════════
def test_publication_helpers_wires_observer():
    src = open("/app/backend/services/publication_helpers.py").read()
    assert "from services.production_truth.publication_observer import" in src
    assert "observe_publication" in src
    # Must be AFTER publish_batch — not before.
    idx_pub = src.index("await publisher.publish_batch(")
    idx_obs = src.index("await observe_publication(")
    assert idx_pub < idx_obs, "observer must run AFTER publish_batch"


def test_mls_direct_inject_wires_observer():
    src = open("/app/backend/services/mls_direct_inject.py").read()
    assert "from services.production_truth.publication_observer import" in src
    assert "observe_publication" in src
    assert 'publication_source="mls_direct_inject"' in src


def test_soccer_prop_inject_wires_observer():
    src = open("/app/backend/services/soccer_prop_inject.py").read()
    assert "from services.production_truth.publication_observer import" in src
    assert "observe_publication" in src
    assert 'publication_source="soccer_prop_inject"' in src


# ═══════════════════════════════════════════════════════════════════
# §4 — Observer freezes one immutable snapshot per canonical pick
# ═══════════════════════════════════════════════════════════════════
def test_observer_freezes_snapshot_for_qualifying_pick():
    from services.production_truth import observe_publication
    from services.production_truth.pregame_snapshot import (
        PREGAME_SNAPSHOTS_COLLECTION,
    )

    db = _FakeDB()
    pick = {
        "id":                "cpid-real-1",
        "sport":             "MLB",
        "market":            "batter_hits",
        "book_odds":        -140,
        "lock_score":        91,
        "publication_gate":  "canonical_barrier_passed",
        "commence_time":     "2026-09-01T00:00:00Z",
        "player_name":       "Aaron Judge",
        "canonical_player_id": "cid-judge",
        "current_team":      "NYY",
        "model_probability": 0.62,
    }
    summary = _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    assert summary["frozen"] == 1
    assert summary["already"] == 0
    assert len(db[PREGAME_SNAPSHOTS_COLLECTION].docs) == 1
    frozen = db[PREGAME_SNAPSHOTS_COLLECTION].docs[0]
    assert frozen["snapshot_hash"]
    assert frozen["canonical_prediction_id"] == "cpid-real-1"


# ═══════════════════════════════════════════════════════════════════
# §5 — Retrying the same prediction is idempotent
# ═══════════════════════════════════════════════════════════════════
def test_observer_is_idempotent_on_retry():
    from services.production_truth import observe_publication
    from services.production_truth.pregame_snapshot import (
        PREGAME_SNAPSHOTS_COLLECTION,
    )

    db = _FakeDB()
    pick = {
        "id":                "cpid-retry-1",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":        -110,
        "lock_score":        90,
        "publication_gate":  "canonical_barrier_passed",
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    s1 = _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    s2 = _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    s3 = _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    assert s1["frozen"] == 1
    assert s2["frozen"] == 0
    assert s2["already"] == 1
    assert s3["already"] == 1
    # Never a duplicate.
    assert len(db[PREGAME_SNAPSHOTS_COLLECTION].docs) == 1


# ═══════════════════════════════════════════════════════════════════
# §6 — Superseding predictions still use append-only semantics
# ═══════════════════════════════════════════════════════════════════
def test_supersede_appends_new_snapshot_without_mutating_first():
    from services.production_truth.pregame_snapshot import (
        freeze_pregame,
        PREGAME_SNAPSHOTS_COLLECTION,
    )
    db = _FakeDB()
    pick = {
        "canonical_prediction_id": "cpid-super-1",
        "id":                      "cpid-super-1",
        "sport":                   "NBA",
        "market":                  "h2h",
        "book_odds":              -105,
        "lock_score":              90,
    }
    s1 = _run(freeze_pregame(db, pick))
    original_hash = s1["snapshot_hash"]
    # Pretend the model updated — new payload.
    pick2 = {**pick, "lock_score": 93}
    s2 = _run(freeze_pregame(db, pick2, supersedes=original_hash))
    assert s2["snapshot_hash"] != original_hash or s2["supersedes"] == original_hash
    docs = db[PREGAME_SNAPSHOTS_COLLECTION].docs
    assert len(docs) == 2
    # The FIRST doc is unchanged (immutable).
    first_still = next(d for d in docs if d.get("supersedes") is None)
    assert first_still["snapshot_hash"] == original_hash
    assert first_still["lock_score"] == 90


# ═══════════════════════════════════════════════════════════════════
# §7 — OBSERVE records violations without rejecting the candidate
# ═══════════════════════════════════════════════════════════════════
def test_observe_mode_records_violations_but_never_rejects():
    from services.production_truth import (
        observe_publication,
        clear_violations,
        recent_violations,
        reset_mode_for_testing,
        current_mode,
        EnforcementMode,
    )
    reset_mode_for_testing()
    clear_violations()
    assert current_mode() is EnforcementMode.OBSERVE
    db = _FakeDB()
    # Pick with synthetic odds — should FAIL REAL_MARKET_AVAILABLE
    # but STILL flow through the observer (never rejected).
    bad_pick = {
        "id":                "cpid-bad-1",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":        -110,
        "odds_provenance":   "MODEL",       # synthetic!
        "lock_score":        90,
        "publication_gate":  "canonical_barrier_passed",
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    summary = _run(observe_publication(
        db, [bad_pick], publication_source="canonical_pipeline"))
    # OBSERVE mode processed the pick and recorded at least one
    # violation but did not raise / reject.
    assert summary["count"] == 1
    assert summary["violations"] >= 1
    violations = recent_violations(pick_id="cpid-bad-1")
    assert len(violations) >= 1
    # Freeze was SKIPPED (bad_pick has synthetic odds — but
    # book_odds is still numeric so the eligibility check passes and
    # a snapshot is created).  The important part: the violation was
    # captured without blocking anything.
    clear_violations()


# ═══════════════════════════════════════════════════════════════════
# §8 — Publication-rejected picks are NOT frozen but ARE observed
# ═══════════════════════════════════════════════════════════════════
def test_rejected_publication_is_not_frozen_but_is_observed():
    from services.production_truth import observe_publication
    from services.production_truth.pregame_snapshot import (
        PREGAME_SNAPSHOTS_COLLECTION,
    )
    from services.production_truth.publication_observer import (
        OBSERVATIONS_COLLECTION,
    )
    db = _FakeDB()
    rejected = {
        "id":                "cpid-rej-1",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_rejected",
        "barrier_failures":  ["lock_below_strict_floor_85"],
        "off_board":         True,
        "no_bet":            True,
        "lock_score":        70,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    summary = _run(observe_publication(
        db, [rejected], publication_source="canonical_pipeline"))
    assert summary["frozen"] == 0
    # No snapshot should exist.
    assert len(db[PREGAME_SNAPSHOTS_COLLECTION].docs) == 0
    # But an observation IS recorded.
    obs_docs = db[OBSERVATIONS_COLLECTION].docs
    assert len(obs_docs) == 1
    assert obs_docs[0]["publication_gate"] == "canonical_barrier_rejected"
    assert obs_docs[0]["snapshot"]["action"] == "SKIPPED_NOT_ELIGIBLE"


# ═══════════════════════════════════════════════════════════════════
# §9 — Published-but-not-visible is diagnosable
# ═══════════════════════════════════════════════════════════════════
def test_published_but_not_visible_is_captured_in_observation():
    from services.production_truth import observe_publication, ProductionStage
    from services.production_truth.publication_observer import (
        OBSERVATIONS_COLLECTION,
    )
    db = _FakeDB()
    pick = {
        "id":                "cpid-notvis-1",
        "sport":             "NBA",
        "market":            "h2h",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        80,     # below the 85 floor
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "LAL",
    }
    _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    obs = db[OBSERVATIONS_COLLECTION].docs[0]
    vis = obs["reachability"]["stages"][
        ProductionStage.VISIBLE_TO_CONSUMER.value]
    assert vis["status"] == "FAIL"


# ═══════════════════════════════════════════════════════════════════
# §10 — DIRECT_INJECT origin is preserved through the Consumption Proof
# ═══════════════════════════════════════════════════════════════════
def test_direct_inject_origin_flows_through_proof():
    from services.production_truth import observe_publication
    from services.production_truth.consumption_proof import (
        build_consumption_proof,
    )
    db = _FakeDB()
    pick = {
        "id":                "cpid-inj-1",
        "sport":             "Soccer",
        "market":            "player_goal_scorer_anytime",
        "book_odds":         200,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        88,
        "commence_time":     "2026-09-01T00:00:00Z",
        "player_name":       "Player X",
        "canonical_player_id": "pid-x",
        "current_team":      "SEA",
        "model_probability": 0.4,
    }
    _run(observe_publication(
        db, [pick], publication_source="mls_direct_inject"))
    # Seed the picks collection so the proof endpoint can find it.
    db["picks"].docs.append(dict(pick))
    proof = _run(build_consumption_proof(db, "cpid-inj-1"))
    assert proof["found"] is True
    assert proof["observation"] is not None
    assert proof["observation"]["origin"] == "DIRECT_INJECT"
    # Verdict must NOT claim REAL_PRODUCTION_PATH_PROVEN for a
    # direct-inject writer.
    assert proof["path_verdict"] != "REAL_PRODUCTION_PATH_PROVEN"


def test_canonical_pipeline_origin_flows_through_proof():
    from services.production_truth import observe_publication
    from services.production_truth.consumption_proof import (
        build_consumption_proof,
    )
    db = _FakeDB()
    pick = {
        "id":                "cpid-canonical-1",
        "canonical_prediction_id": "cpid-canonical-1",
        "sport":             "MLB",
        "market":            "batter_hits",
        "book_odds":        -140,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        91,
        "commence_time":     "2026-09-01T00:00:00Z",
        "player_name":       "Aaron Judge",
        "canonical_player_id": "cid-judge",
        "current_team":      "NYY",
        "model_probability": 0.62,
        "odds_provenance":   "draftkings",
    }
    _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    db["picks"].docs.append(dict(pick))
    proof = _run(build_consumption_proof(db, "cpid-canonical-1"))
    assert proof["found"] is True
    assert proof["observation"]["origin"] == "DATA"
    assert proof["path_verdict"] in ("REAL_PRODUCTION_PATH_PROVEN",
                                       "PARTIAL_PRODUCTION_PATH")


# ═══════════════════════════════════════════════════════════════════
# §11 — UNKNOWN remains UNKNOWN when production evidence is absent
# ═══════════════════════════════════════════════════════════════════
def test_observation_preserves_unknown_stages():
    from services.production_truth import observe_publication, ProductionStage
    from services.production_truth.publication_observer import (
        OBSERVATIONS_COLLECTION,
    )
    db = _FakeDB()
    # No model_probability, no commence_time — several stages UNKNOWN.
    pick = {
        "id":                "cpid-unknown-1",
        "sport":             "NHL",
        "market":            "h2h",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        90,
        "home_team":         "TOR",
    }
    _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    obs = db[OBSERVATIONS_COLLECTION].docs[0]
    stages = obs["reachability"]["stages"]
    # MODEL_CONSUMED cannot be proven → UNKNOWN (never PASS)
    assert stages[ProductionStage.MODEL_CONSUMED.value]["status"] == \
        "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# §12 — NOT_APPLICABLE remains distinct from PASS in observations
# ═══════════════════════════════════════════════════════════════════
def test_observation_preserves_not_applicable():
    from services.production_truth import observe_publication, ProductionStage
    from services.production_truth.publication_observer import (
        OBSERVATIONS_COLLECTION,
    )
    db = _FakeDB()
    game_pick = {
        "id":                "cpid-na-1",
        "sport":             "NBA",
        "market":            "h2h",     # roster N/A
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        90,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "LAL",
    }
    _run(observe_publication(
        db, [game_pick], publication_source="canonical_pipeline"))
    obs = db[OBSERVATIONS_COLLECTION].docs[0]
    stages = obs["reachability"]["stages"]
    roster = stages[ProductionStage.CURRENT_ROSTER_VALID.value]
    assert roster["status"] == "NOT_APPLICABLE"


# ═══════════════════════════════════════════════════════════════════
# §13 — Consumer eligibility observed but never modified
# ═══════════════════════════════════════════════════════════════════
def test_observer_never_mutates_pick_dict():
    from services.production_truth import observe_publication
    db = _FakeDB()
    pick = {
        "id":                "cpid-immut-1",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        91,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    frozen_before = dict(pick)     # snapshot
    _run(observe_publication(
        db, [pick], publication_source="canonical_pipeline"))
    # The caller's dict must be UNCHANGED (no injection of hash /
    # canonical_prediction_id / origin / etc.).
    assert pick == frozen_before


# ═══════════════════════════════════════════════════════════════════
# §14 — Consumption Proof uses recorded observation, not module existence
# ═══════════════════════════════════════════════════════════════════
def test_proof_falls_back_to_unknown_without_observation():
    """A pick that was NEVER observed cannot suddenly gain
    REAL_PRODUCTION_PATH_PROVEN just because the observer module
    exists."""
    from services.production_truth.consumption_proof import (
        build_consumption_proof,
    )
    db = _FakeDB()
    db["picks"].docs.append({
        "id":         "legacy-9",
        "sport":      "NFL",
        "market":     "h2h",
        "book_odds": -110,
        "lock_score": 90,
        # No publication_gate, no observation, no snapshot.
    })
    proof = _run(build_consumption_proof(db, "legacy-9"))
    assert proof["observation"] is None
    assert proof["path_verdict"] != "REAL_PRODUCTION_PATH_PROVEN"


# ═══════════════════════════════════════════════════════════════════
# Extra — origin classifier
# ═══════════════════════════════════════════════════════════════════
def test_classify_origin_marks_direct_inject_sources():
    from services.production_truth.publication_observer import (
        _classify_origin,
    )
    assert _classify_origin("mls_direct_inject") == "DIRECT_INJECT"
    assert _classify_origin("soccer_prop_inject") == "DIRECT_INJECT"
    assert _classify_origin("canonical_pipeline") == "DATA"
    assert _classify_origin("ufc_espn_v1") == "DATA"
    assert _classify_origin(None) == "UNKNOWN"
    assert _classify_origin("") == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Extra — read_observation returns None cleanly on missing collection
# ═══════════════════════════════════════════════════════════════════
def test_read_observation_returns_none_gracefully():
    from services.production_truth import read_observation
    db = _FakeDB()
    assert _run(read_observation(db, canonical_prediction_id="nope")) is None
    assert _run(read_observation(db, pick_id="nope")) is None
    assert _run(read_observation(db)) is None
