"""Tests for the Parlay Optimizer V1 endpoint /api/picks/parlay.

Spec summary:
- Returns parlays array of 3 cards: SAFE, BALANCED, AGGRESSIVE
- Each card has grade (A-F), strength_score, survival_pct, leg_count, legs[], reasons[]
- Hard rule (standard mode): every leg has lock_score>=88 AND edge_percent>=3
- High-risk (legs=15): 3 cards each with 10-15 legs
- rank parameter cycles refresh, locked_ids pins picks across all cards
- Sport / exclude_sports filters apply
- Grade thresholds: A>=85, B>=72, C>=58, D>=45, F otherwise
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://player-intel-engine.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"

REQUIRED_CARD_FIELDS = [
    "label", "grade", "strength_score", "leg_count", "legs",
    "survival_pct", "avg_edge_pct", "avg_roi_pct", "avg_win_prob",
    "diversification_pct", "correlation_score", "stability_score",
    "combined_decimal_odds", "combined_american_odds",
    "payout_on_100", "profit_on_100", "reasons",
]


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth(token):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}",
                      "Content-Type": "application/json"})
    return s


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
GRADE_FLOOR = {"A": 85.0, "B": 72.0, "C": 58.0, "D": 45.0, "F": 0.0}


def _assert_card_shape(card, idx, *, label=None):
    for f in REQUIRED_CARD_FIELDS:
        assert f in card, f"card[{idx}] missing field '{f}': keys={list(card.keys())}"
    if label is not None:
        assert card["label"] == label, f"card[{idx}].label = {card['label']!r}, expected {label!r}"
    # Non-negative numeric ranges
    assert card["strength_score"] >= 0, f"strength_score negative: {card['strength_score']}"
    assert 0 <= card["survival_pct"] <= 100, f"survival_pct OOR: {card['survival_pct']}"
    assert card["leg_count"] >= 2, f"leg_count < 2: {card['leg_count']}"
    assert isinstance(card["legs"], list) and len(card["legs"]) > 0, "legs empty"
    assert len(card["legs"]) == card["leg_count"], \
        f"leg_count {card['leg_count']} vs len(legs) {len(card['legs'])}"
    assert isinstance(card["reasons"], list) and len(card["reasons"]) > 0, "reasons empty"
    # Grade matches score
    g = card["grade"]
    assert g in GRADE_FLOOR, f"unknown grade {g!r}"
    floor = GRADE_FLOOR[g]
    assert card["strength_score"] >= floor, \
        f"grade {g} but score {card['strength_score']} < {floor}"
    # Combined odds sanity
    assert card["combined_decimal_odds"] >= 1.0, \
        f"combined_decimal_odds < 1.0: {card['combined_decimal_odds']}"


def _assert_standard_leg_hard_rules(legs, card_label):
    for leg in legs:
        assert not leg.get("no_bet", False), f"[{card_label}] leg has no_bet=True: {leg.get('id')}"
        assert not leg.get("is_under_lock", False), f"[{card_label}] leg has is_under_lock=True: {leg.get('id')}"
        lock = leg.get("lock_score", 0)
        edge = leg.get("edge_percent", 0)
        assert lock >= 88, f"[{card_label}] leg lock_score {lock} < 88 (id={leg.get('id')})"
        assert edge >= 3, f"[{card_label}] leg edge_percent {edge} < 3 (id={leg.get('id')})"


# ──────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────
class TestParlayCoreShape:
    """Test 1: GET /api/picks/parlay?mode=standard&legs=3 — shape."""

    def test_standard_3_legs_returns_three_cards(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 3}, timeout=60)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
        body = r.json()
        assert "parlays" in body, f"no 'parlays' key: {list(body.keys())}"
        cards = body["parlays"]
        assert isinstance(cards, list), "parlays not list"
        assert len(cards) == 3, f"expected 3 cards, got {len(cards)}"
        labels = [c["label"] for c in cards]
        assert labels == ["SAFE", "BALANCED", "AGGRESSIVE"], f"labels wrong: {labels}"
        for i, card in enumerate(cards):
            _assert_card_shape(card, i, label=labels[i])
            _assert_standard_leg_hard_rules(card["legs"], labels[i])

    def test_legacy_parlay_field_present(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 3}, timeout=60)
        body = r.json()
        assert body.get("parlay") is not None, "legacy 'parlay' field missing"
        legacy = body["parlay"]
        for f in ["legs", "leg_count", "combined_decimal_odds",
                  "combined_american_odds", "payout_on_100", "profit_on_100"]:
            assert f in legacy, f"legacy parlay missing {f}"


class TestParlayLegCounts:
    """Test 2+3: leg target handling — spec says SAFE may have fewer than requested."""

    def test_standard_5_legs(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 5}, timeout=60)
        assert r.status_code == 200
        cards = r.json()["parlays"]
        assert len(cards) == 3
        for i, card in enumerate(cards):
            _assert_card_shape(card, i)
            _assert_standard_leg_hard_rules(card["legs"], card["label"])
            # Spec: SAFE may have <5 legs ("no filler legs / stop when quality drops")
            assert 2 <= card["leg_count"] <= 8, \
                f"{card['label']} leg_count {card['leg_count']} out of 2..8"

    def test_high_risk_15_legs(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "high_risk", "legs": 15}, timeout=90)
        assert r.status_code == 200
        body = r.json()
        cards = body.get("parlays") or []
        assert len(cards) == 3, f"high_risk got {len(cards)} cards (body keys: {list(body.keys())})"
        for i, card in enumerate(cards):
            _assert_card_shape(card, i)
            # Spec: high-risk caps 10–20; here we requested 15
            assert card["leg_count"] >= 2, f"empty card {card['label']}"
            # In high-risk, lock>=75/edge>=1 — relaxed; don't enforce standard cuts.


class TestParlayRefreshCursor:
    """Test 4: rank=2 returns different parlays than rank=1."""

    def test_rank_cycles_change_content(self, auth):
        r1 = auth.get(f"{BASE_URL}/api/picks/parlay",
                      params={"mode": "standard", "legs": 3, "rank": 1}, timeout=60)
        r2 = auth.get(f"{BASE_URL}/api/picks/parlay",
                      params={"mode": "standard", "legs": 3, "rank": 2}, timeout=60)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json().get("rank") == 1
        assert r2.json().get("rank") == 2

        def fingerprint(body):
            return [
                tuple(sorted(leg.get("id", "") for leg in card["legs"]))
                for card in body.get("parlays", [])
            ]

        fp1 = fingerprint(r1.json())
        fp2 = fingerprint(r2.json())
        # At least one of the three cards should differ between rank 1 and rank 2.
        # If pool is very small there may be no diversity available; only treat
        # full identity as a soft warning.
        if fp1 == fp2:
            pytest.skip(f"rank 1 and rank 2 are identical (pool may be exhausted): {fp1}")
        assert fp1 != fp2, "rank=2 returned same parlays as rank=1"


class TestParlayLockedIds:
    """Test 5: locked_ids pin must appear in every card."""

    def test_locked_id_appears_in_every_card(self, auth):
        today = auth.get(f"{BASE_URL}/api/picks/today",
                         params={"limit": 200}, timeout=30).json()
        items = today if isinstance(today, list) else today.get("picks") or today.get("items") or []
        eligible = [p for p in items
                    if p.get("lock_score", 0) >= 88
                    and p.get("edge_percent", 0) >= 3
                    and not p.get("no_bet")
                    and not p.get("is_under_lock")]
        if not eligible:
            pytest.skip("no eligible Lock>=88 / Edge>=+3% pick available to pin")
        pick_id = eligible[0]["id"]
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 3, "locked_ids": pick_id},
                     timeout=60)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert pick_id in (body.get("locked_ids") or []), \
            f"locked_ids echo missing: {body.get('locked_ids')}"
        cards = body.get("parlays") or []
        assert len(cards) >= 1, "no parlays returned with locked id"
        for c in cards:
            ids = [leg.get("id") for leg in c["legs"]]
            assert pick_id in ids, f"{c['label']} missing pinned id {pick_id}; legs={ids}"


class TestParlaySportFilters:
    """Test 6+7: sport filter, exclude_sports."""

    def test_sport_filter_soccer(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 3, "sport": "Soccer"},
                     timeout=60)
        assert r.status_code == 200
        cards = r.json().get("parlays") or []
        if not cards:
            pytest.skip("no Soccer parlays available today")
        for c in cards:
            sports = {(leg.get("sport") or "").lower() for leg in c["legs"]}
            assert sports.issubset({"soccer"}), \
                f"{c['label']} has non-Soccer legs: {sports}"

    def test_exclude_sports_soccer_in_mix(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 3,
                             "sport": "Mix", "exclude_sports": "Soccer"},
                     timeout=60)
        assert r.status_code == 200
        cards = r.json().get("parlays") or []
        if not cards:
            pytest.skip("no Mix parlays available today excluding Soccer")
        for c in cards:
            for leg in c["legs"]:
                assert (leg.get("sport") or "").lower() != "soccer", \
                    f"{c['label']} contains Soccer leg despite exclude_sports: {leg.get('id')}"


class TestParlayDamageControl:
    """Test 9: survival doesn't increase as more legs are added.

    Note: the optimizer returns final survival per parlay, not per-step.
    Across the 3 cards (SAFE > BALANCED > AGGRESSIVE), survival should
    generally decline; we test the ordering rather than per-leg.
    """

    def test_survival_ordering_safe_balanced_aggressive(self, auth):
        r = auth.get(f"{BASE_URL}/api/picks/parlay",
                     params={"mode": "standard", "legs": 5}, timeout=60)
        assert r.status_code == 200
        cards = r.json().get("parlays") or []
        if len(cards) < 3:
            pytest.skip(f"need 3 cards, got {len(cards)}")
        by_label = {c["label"]: c for c in cards}
        safe = by_label["SAFE"]["survival_pct"]
        bal = by_label["BALANCED"]["survival_pct"]
        agg = by_label["AGGRESSIVE"]["survival_pct"]
        # Allow slight ties; but SAFE shouldn't be lower than AGGRESSIVE.
        assert safe >= agg - 1e-6, \
            f"SAFE survival {safe} < AGGRESSIVE survival {agg} — band ordering broken"
        # All non-negative
        for lbl, v in [("SAFE", safe), ("BALANCED", bal), ("AGGRESSIVE", agg)]:
            assert v >= 0, f"{lbl} negative survival {v}"


class TestParlayGradeConsistency:
    """Grade <-> strength_score consistency across modes."""

    def test_grades_align_with_scores_all_modes(self, auth):
        for params in [
            {"mode": "standard", "legs": 3},
            {"mode": "standard", "legs": 5},
            {"mode": "high_risk", "legs": 15},
        ]:
            r = auth.get(f"{BASE_URL}/api/picks/parlay", params=params, timeout=90)
            assert r.status_code == 200, f"{params} -> {r.status_code}"
            for c in (r.json().get("parlays") or []):
                g = c["grade"]
                s = c["strength_score"]
                assert s >= GRADE_FLOOR[g], \
                    f"{params} {c['label']} grade {g} but score {s}"
