"""Phase 2 — Lock Score / 98 / 99 / APEX Authority invariants.

Root-class proofs the directive requires:

  R1. No post-publication Lock mutation
       — Any writer that touches lock_score / lock_score_v2 /
         lock_score_peak / published_lock_score / grade / confidence
         on a picks doc that already carries `published_lock_score`
         must raise `PublishedFieldMutationError` unless it is the
         publication service itself.
  R2. No artificial rank-based promotion
       — `sports_engine.top_5_elite_composite_promotion` no longer
         mutates lock_score.
  R3. No elite-name manufactured 99
       — `learning_system_v2` no longer force-sets lock_score=99 /
         is_apex=True on `elite_player` goalscorer picks.
  R4. No CLV-based Lock demotion
       — `closing_line_snapshotter` writes only `closing_odds` /
         `clv_value`; never mutates lock_score.
  R5. No unjustified provider-fallback penalty
       — `odds_provider.decorate_pick` no longer subtracts 10 from
         lock_score when the source is api_sports / espn.
  R6. Multiple legitimate 98s / 99s / APEX picks can coexist
       (no count cap on legitimately earned high scores).
  R7. Duplicate representations of the SAME canonical wager cannot
       coexist as ACTIVE (canonical uniqueness ≠ count cap).
  R8. ≥85 eligibility includes exactly 85.
  R9. Locks and Pick Breakdown surface the identical frozen
       Lock Score (both hydrate from the snapshot).

These are enforced against SOURCE CODE (guaranteeing the retirements
stick) and against the runtime helpers where behaviour matters.
"""
from __future__ import annotations
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

REPO_ROOT = pathlib.Path("/app/backend")


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ── R1 ── Post-publication Lock mutation guarded ────────────────
def test_immutable_set_covers_all_shadow_lock_fields():
    """The write-guard must forbid lock_score_v2 / lock_score_peak /
    lock_score_raw in addition to lock_score."""
    from services.published_write_guard import IMMUTABLE_FIELDS
    for f in ("lock_score", "lock_score_v2", "lock_score_peak",
              "lock_score_raw",
              "grade", "confidence",
              "published_lock_score", "published_grade",
              "published_confidence"):
        assert f in IMMUTABLE_FIELDS, f"{f} must be immutable"


def test_write_guard_blocks_lock_score_v2_mutation():
    from services.published_write_guard import (
        assert_no_published_mutation, PublishedFieldMutationError,
    )
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"lock_score_v2": 99.0}}, caller="test",
        )


def test_write_guard_blocks_grade_mutation():
    from services.published_write_guard import (
        assert_no_published_mutation, PublishedFieldMutationError,
    )
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"grade": "Elite Lock"}}, caller="test",
        )


# ── R2 ── Rank-based 95-99 promotion retired ────────────────────
def test_sports_engine_rank_boost_retired():
    src = _read("sports_engine.py")
    # The exact literal that used to lift top-5 into 95-99:
    assert "rank_boost" not in src, "rank_boost variable must be retired"
    assert "max(95.0, min(99.0, p[\"lock_score\"] + rank_boost)" not in src
    # Positive proof of the retirement comment (locks the removal in).
    assert "RANK-BASED 95-99 PROMOTION" in src


# ── R3 ── Elite-name manufactured 99 retired ────────────────────
def test_learning_system_v2_marquee_99_retired():
    src = _read("learning_system_v2.py")
    # The prior branch used to hard-set 99 + is_apex + Apex-Lock tier.
    assert 'p["lock_score"] = 99.0' not in src, \
        "elite-name manufactured 99 must not reappear"
    assert '"marquee_locked"' not in src or \
        "ELITE-NAME AUTO-99 PROMOTION RETIRED" in src


# ── R4 ── No CLV-based Lock demotion ────────────────────────────
def test_closing_line_snapshotter_does_not_mutate_lock_score():
    src = _read("closing_line_snapshotter.py")
    assert 'lock_score' not in src, \
        "closing_line_snapshotter must never touch lock_score"


# ── R5 ── Provider-fallback dock retired ────────────────────────
def test_odds_provider_no_longer_docks_lock_score():
    src = _read("services/odds_provider.py")
    # The prior -10 dock line was:
    #   pick["lock_score"] = max(0.0, min(99.0, _ls - 10.0))
    assert "_ls - 10.0" not in src, \
        "provider-fallback lock dock must be retired"
    assert "PROVIDER-FALLBACK LOCK\n        # SCORE DOCK RETIRED" in src \
        or "PROVIDER-FALLBACK LOCK" in src


# ── R6 ── Multiple legitimate 98s / 99s can coexist ─────────────
def test_no_hardcoded_count_cap_on_98_99_apex():
    """No source file may implement a `top_N_98_cap` or similar
    numeric cap on how many 98/99/APEX picks can survive."""
    # Search every backend .py for a suspicious cap pattern.
    banned_patterns = [
        r"MAX_APEX_PICKS\s*=",
        r"APEX_COUNT_LIMIT\s*=",
        r"max_98s\s*=",
        r"max_99s\s*=",
        r"cap_apex\s*=",
    ]
    offenders: list[str] = []
    for p in REPO_ROOT.rglob("*.py"):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "test_phase2" in p.name:
            continue
        for pat in banned_patterns:
            if re.search(pat, txt):
                offenders.append(f"{p.relative_to(REPO_ROOT)}: {pat}")
    assert not offenders, f"count caps found: {offenders}"


# ── R6/R7 ── Grade band boundaries preserved exactly ────────────
def test_grade_bands_preserve_98_99_apex():
    from sports_engine import _grade
    # 85-89 qualifying (Playable in current vocab), 90-92 Lock,
    # 93-95 Strong Lock, 96-98 Strong→Elite Lock crossover,
    # 99 Elite Lock, 100 APEX.
    assert _grade(85.0) == "Playable"
    assert _grade(89.9) == "Playable"
    assert _grade(90.0) == "Lock"
    assert _grade(92.5) == "Lock"
    assert _grade(95.0) == "Strong Lock"
    assert _grade(97.9) == "Strong Lock"
    assert _grade(98.0) == "Elite Lock"
    assert _grade(99.0) == "Elite Lock"
    assert _grade(100.0) == "APEX Lock"


# ── R7 ── Canonical uniqueness is per-wager, not per-score ──────
def test_totals_truth_guard_uniqueness_is_side_neutral():
    """Duplicate representations of the SAME wager (same event/line,
    both sides emitted) collapse to one ACTIVE via revision_state
    SUPERSEDED_IN_RUN — NOT via count cap on 98/99/APEX."""
    from services.totals_truth_guard import _canonical_totals_key
    p_over = {"sport": "MLB", "event_id": "E1", "period": "FULL_GAME",
              "market": "Total Runs Over 8.5", "line": 8.5,
              "selection": "Over"}
    p_under = {"sport": "MLB", "event_id": "E1", "period": "FULL_GAME",
               "market": "Total Runs Under 8.5", "line": 8.5,
               "selection": "Under"}
    # Same canonical wager (event + line) => same key.
    k1 = _canonical_totals_key(p_over)
    k2 = _canonical_totals_key(p_under)
    assert k1 == k2, "same canonical wager must share the key"
    # Different line => distinct wager => distinct key (multiple
    # legitimate APEX totals across lines can coexist).
    p_other_line = dict(p_over); p_other_line["line"] = 9.5
    k3 = _canonical_totals_key(p_other_line)
    assert k3 != k1, "different lines are distinct canonical wagers"


# ── R8 ── ≥85 eligibility includes exactly 85 ───────────────────
def test_locks_eligibility_includes_exactly_85():
    from services.main_board_eligibility import is_main_board_eligible
    # Bare-minimum eligible pick (real book line + published snapshot).
    pick_at_85 = {
        "lock_score": 85.0,
        "published_lock_score": 85.0,
        "sport": "MLB", "market": "Moneyline",
        "selection": "Home",
        "book_odds": -115,
        "implied_probability": 53.5,
        "no_real_book_line": False,
        "status": "pending",
    }
    ok = is_main_board_eligible(pick_at_85)
    assert ok, "lock_score=85 must be main-board eligible"


def test_locks_eligibility_excludes_below_85():
    from services.main_board_eligibility import is_main_board_eligible
    pick_low = {
        "lock_score": 84.9,
        "published_lock_score": 84.9,
        "sport": "MLB", "market": "Moneyline",
        "selection": "Home",
        "book_odds": -115,
        "implied_probability": 53.5,
        "no_real_book_line": False,
        "status": "pending",
    }
    ok = is_main_board_eligible(pick_low)
    assert ok is False


def test_locks_eligibility_mongo_query_uses_gte_85():
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query()
    # Some code path enforces the ≥85 rule via a Mongo predicate;
    # verify the boundary IS inclusive (>=), never > 85.
    import json
    s = json.dumps(q, default=str)
    assert "$gte" in s
    assert '"$gt": 85' not in s and '"$gt":85' not in s


# ── R9 ── Locks + Pick Breakdown display the identical frozen score
def test_hydrate_shows_snapshot_score_not_legacy_field():
    """A pick that was mutated legacy-side (simulating a bad writer)
    but still carries a snapshot MUST render the snapshot value —
    proving Locks and Pick Breakdown see the same immutable score."""
    from services.published_prediction_reader import hydrate
    pick = {
        "id": "p_freeze",
        "lock_score": 45.0,                     # simulated tampering
        "win_probability": 12.0,
        "published_lock_score":  92.0,
        "published_probability": 0.71,          # canonical fraction
        "published_edge":        6.2,
        "published_grade":       "Lock",
        "published_confidence":  "Very High",
        "published_odds":        -140,
        "published_line":        None,
    }
    h = hydrate(pick)
    assert h["_prediction_source"] == "snapshot"
    # Snapshot value wins over the tampered legacy field.
    assert h["lock_score"] == 92.0
    assert h["win_probability"] == pytest.approx(71.0, rel=1e-6)
    assert h["edge_percent"] == 6.2
    assert h["grade"] == "Lock"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
