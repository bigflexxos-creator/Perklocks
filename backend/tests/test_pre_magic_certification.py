"""Pre-Magic Certification — deterministic tests.

Every test is self-contained and uses an in-memory fake Mongo (no
external services).  Tests cover the §16 requirements:

* live/canonical pick → history resolution
* exact threshold
* missing != zero
* future leakage
* canonical identity
* opponent identity
* market normalization
* insufficient samples
* unavailable sport
* distributions
* Tennis context
* model-only vs sportsbook-backed pick
* synthetic odds rejection/detection
* null implied probability detection
* history availability vs history consumption distinction
* Magic remains NOT_WIRED regardless of outcomes
"""
from __future__ import annotations

import asyncio
import sys
import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# Fake async Mongo — supports find({}).sort().limit(), count_documents,
# aggregate helpers used by the certification module.
# ═══════════════════════════════════════════════════════════════════
class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        if isinstance(key, str):
            self._docs.sort(
                key=lambda d: d.get(key) or "",
                reverse=(direction == -1),
            )
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]
        self._i += 1
        return dict(d)


def _get_nested(doc, key):
    """Support dotted paths like 'actuals.r' for the fake Mongo."""
    parts = key.split(".")
    cur = doc
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _matches(doc, query):
    for k, v in query.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
            continue
        if k == "$and":
            if not all(_matches(doc, sub) for sub in v):
                return False
            continue
        cur = _get_nested(doc, k)
        if isinstance(v, dict):
            if "$lt" in v and not (cur is not None and cur < v["$lt"]):
                return False
            if "$exists" in v:
                exists = v["$exists"]
                if exists and cur is None:
                    return False
                if not exists and cur is not None:
                    return False
            if "$ne" in v and cur == v["$ne"]:
                return False
            if "$regex" in v:
                import re
                pat = v["$regex"]
                flags = re.IGNORECASE if "i" in (v.get("$options") or "") else 0
                if not re.search(pat, str(cur or ""), flags):
                    return False
        elif cur != v:
            return False
    return True


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    def find(self, query=None, projection=None):
        query = query or {}
        return _AsyncCursor([d for d in self.docs if _matches(d, query)])

    async def count_documents(self, query=None):
        query = query or {}
        return sum(1 for d in self.docs if _matches(d, query))

    async def estimated_document_count(self):
        return len(self.docs)


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.__getitem__(name)


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# Section 1 — State vocabulary
# ═══════════════════════════════════════════════════════════════════
def test_state_vocabulary_has_five_explicit_values():
    """§14: no true/false — every state must be one of five explicit
    values (plus NOT_APPLICABLE and NOT_WIRED which are also explicit).
    """
    from services.pre_magic_certification.states import CertificationState

    for v in ("PASS", "FAIL", "PARTIAL", "UNAVAILABLE", "UNKNOWN",
              "NOT_WIRED", "NOT_APPLICABLE"):
        assert CertificationState(v).value == v


def test_matrix_rollup_ready_for_magic_requires_no_fail():
    from services.pre_magic_certification.states import (
        CertificationEntry, CertificationMatrix, CertificationState,
    )

    m = CertificationMatrix()
    m.add(CertificationEntry(
        sport="MLB", market="player_hits",
        evidence_type="PLAYER_HISTORY",
        certification_status=CertificationState.PASS.value,
    ))
    m.rollup()
    assert m.ready_for_magic == CertificationState.PASS.value
    assert m.magic_consumption == CertificationState.NOT_WIRED.value

    m.add(CertificationEntry(
        sport="NFL", market="player_rushing_yards",
        evidence_type="PLAYER_HISTORY",
        certification_status=CertificationState.FAIL.value,
    ))
    m.rollup()
    assert m.ready_for_magic == CertificationState.FAIL.value


def test_magic_consumption_change_forces_fail():
    """§15: any change of magic_consumption forces the matrix to FAIL."""
    from services.pre_magic_certification.states import (
        CertificationEntry, CertificationMatrix, CertificationState,
    )

    m = CertificationMatrix()
    m.add(CertificationEntry(
        sport="MLB", market="x", evidence_type="PLAYER_HISTORY",
        certification_status=CertificationState.PASS.value,
    ))
    m.magic_consumption = CertificationState.PASS.value   # DISALLOWED
    m.rollup()
    assert m.ready_for_magic == CertificationState.FAIL.value
    assert "REFUSE PROMOTION" in (m.recommendation or "").upper()


# ═══════════════════════════════════════════════════════════════════
# Section 2 — Missing != Zero
# ═══════════════════════════════════════════════════════════════════
def test_missing_not_zero_check_passes():
    from services.pre_magic_certification import checks

    e = checks.certify_missing_not_zero()
    assert e.certification_status == "PASS"
    assert "0 preserved" in (e.detail or "")


def test_missing_data_guard_preserves_legit_zero():
    from services.production_truth.missing_data_guard import (
        coerce_optional_number, UNKNOWN, is_unknown,
    )
    assert coerce_optional_number(0) == 0.0
    assert coerce_optional_number("0") == 0.0
    assert is_unknown(coerce_optional_number(None))
    assert UNKNOWN != 0
    assert UNKNOWN != 0.0


# ═══════════════════════════════════════════════════════════════════
# Section 3 — Exact Threshold
# ═══════════════════════════════════════════════════════════════════
def test_exact_threshold_engine_certifies_monotonic():
    from services.pre_magic_certification import checks

    e = checks.certify_exact_threshold_engine()
    assert e.certification_status == "PASS"


def test_exact_threshold_line_change_changes_hitrate():
    from services.player_history.threshold_engine import evaluate_threshold

    actuals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    over_1_5 = evaluate_threshold(actuals, 1.5, "over")
    over_5_5 = evaluate_threshold(actuals, 5.5, "over")
    over_9_5 = evaluate_threshold(actuals, 9.5, "over")
    # Same underlying raw actuals — only the line changes.
    assert over_1_5.actual_values == over_5_5.actual_values == over_9_5.actual_values
    # Hit rates strictly monotonic decreasing.
    assert over_1_5.hit_rate > over_5_5.hit_rate > over_9_5.hit_rate


# ═══════════════════════════════════════════════════════════════════
# Section 4 — Distributions
# ═══════════════════════════════════════════════════════════════════
def test_distributions_certified():
    from services.pre_magic_certification import checks
    e = checks.certify_distribution_engine()
    assert e.certification_status == "PASS"


def test_distributions_not_fabricated_for_tiny_sample():
    from services.player_history.threshold_engine import evaluate_threshold
    tiny = evaluate_threshold([5.0], 4.5, "over", quantiles=True)
    assert tiny.q25 is None
    assert tiny.median is None
    assert tiny.q75 is None
    assert tiny.variance is None


# ═══════════════════════════════════════════════════════════════════
# Section 5 — Market normalization
# ═══════════════════════════════════════════════════════════════════
def test_market_normalization_passes_for_defined_markets():
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import (
        PLAYER_MARKETS, TEAM_MARKETS,
    )
    e = checks.certify_market_normalization(PLAYER_MARKETS + TEAM_MARKETS)
    assert e.certification_status == "PASS"
    assert e.sample_size > 0


def test_unavailable_sports_correctly_classified_in_catalog():
    """UFC / NHL player / CFB player entries have empty atoms per handoff
    — verify the catalogue is honest about this."""
    from services.pre_magic_certification.market_catalog import (
        player_markets_for,
    )
    for sport in ("UFC", "NHL", "CFB"):
        for m in player_markets_for(sport):
            assert not m.atoms, (
                f"{sport}.{m.market} has atoms — should be UNAVAILABLE")


# ═══════════════════════════════════════════════════════════════════
# Section 6 — Player history × market
# ═══════════════════════════════════════════════════════════════════
def _seed_mlb_player_actuals(db, n=25):
    for i in range(n):
        db["player_game_actuals"].docs.append({
            "sport": "mlb",
            "canonical_player_id": f"mlb-p{i%3}",
            "player_id": f"mlb-p{i%3}",
            "player_name": f"Player {i%3}",
            "event_time": f"2026-05-{(i%28)+1:02d}T20:00:00Z",
            "opponent": "BOS",
            "hits": (i % 4),
            "total_bases": (i % 5),
            "home_runs": 1 if i % 6 == 0 else 0,
            "rbis": (i % 3),
        })


def test_player_history_mlb_hits_passes_with_data():
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import PLAYER_MARKETS

    mlb_hits = next(m for m in PLAYER_MARKETS if m.market == "player_hits")
    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=30)
    e = _run(checks.certify_player_history_market(db, mlb_hits))
    assert e.certification_status == "PASS"
    assert e.data_available == "PASS"
    assert e.reachable == "PASS"
    assert e.sample_size == 30


def test_player_history_empty_collection_returns_unavailable():
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import PLAYER_MARKETS

    nba_pts = next(m for m in PLAYER_MARKETS if m.market == "player_points")
    db = _FakeDB()   # empty
    e = _run(checks.certify_player_history_market(db, nba_pts))
    assert e.certification_status == "UNAVAILABLE"
    assert e.data_available == "UNAVAILABLE"
    # Missing data → UNAVAILABLE, NEVER PASS.
    assert e.reachable == "UNAVAILABLE"


def test_ufc_player_market_stays_unavailable_even_with_data():
    """§17: unavailable sport must remain unavailable even if the DB
    contains junk data — the catalogue is authoritative."""
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import PLAYER_MARKETS

    ufc_market = next(m for m in PLAYER_MARKETS if m.sport == "UFC")
    db = _FakeDB()
    for i in range(10):
        db["player_game_actuals"].docs.append({
            "sport": "ufc",
            "canonical_player_id": f"ufc-{i}",
            "event_time": f"2026-05-{i+1:02d}T20:00:00Z",
        })
    e = _run(checks.certify_player_history_market(db, ufc_market))
    assert e.certification_status == "UNAVAILABLE"
    assert e.drop_reason == "SOURCE_UNAVAILABLE"


def test_player_history_atom_gap_reports_unavailable_not_fail():
    """When the sport has rows but the specific market's atoms are
    populated on 0% of them, the certification must emit UNAVAILABLE
    (source gap) — NOT FAIL — so Magic can degrade gracefully."""
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import PLAYER_MARKETS

    mlb_runs = next(m for m in PLAYER_MARKETS
                    if m.market == "player_runs_scored")
    db = _FakeDB()
    # Rows exist for MLB — but none carry any runs field.
    for i in range(30):
        db["player_game_actuals"].docs.append({
            "sport": "mlb",
            "canonical_player_id": f"mlb-{i%3}",
            "event_time": f"2026-05-{(i%28)+1:02d}T20:00:00Z",
            "hits": 1,   # unrelated stat
        })
    e = _run(checks.certify_player_history_market(db, mlb_runs))
    # Should be UNAVAILABLE — the specific atom is a source gap.
    # Must NOT be FAIL (that would suggest a framework bug).
    assert e.certification_status == "UNAVAILABLE"
    assert e.drop_reason == "EVIDENCE_UNAVAILABLE"
    assert "not populated on ANY row" in (e.detail or "")


def test_player_history_atom_undercovered_reports_partial():
    """When some rows carry the atom and others don't, PARTIAL."""
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import PLAYER_MARKETS

    mlb_runs = next(m for m in PLAYER_MARKETS
                    if m.market == "player_runs_scored")
    db = _FakeDB()
    # 60 rows for MLB — atoms only on last 3, OUTSIDE the 50-row
    # first-sample window so the check falls through to the full-
    # collection existence probe.
    for i in range(60):
        row = {
            "sport": "mlb",
            "canonical_player_id": f"mlb-{i%3}",
            "event_time": f"2026-04-{(i%28)+1:02d}T20:00:00Z",
            "hits": 1,
        }
        if i >= 57:
            row["actuals"] = {"r": 2}
        db["player_game_actuals"].docs.append(row)
    e = _run(checks.certify_player_history_market(db, mlb_runs))
    assert e.certification_status == "PARTIAL"
    assert e.drop_reason == "INSUFFICIENT_EVIDENCE"


# ═══════════════════════════════════════════════════════════════════
# Section 7 — Team history × market
# ═══════════════════════════════════════════════════════════════════
def _seed_mlb_team_actuals(db, n=20):
    for i in range(n):
        db["team_game_actuals"].docs.append({
            "sport": "mlb",
            "canonical_team_id": "NYY",
            "canonical_opponent_id": "BOS",
            "team_name": "New York Yankees",
            "event_time": f"2026-05-{(i%28)+1:02d}T20:00:00Z",
            "season": 2026,
            "team_score": 5 + (i % 3),
            "opponent_score": 3 + (i % 4),
            "home_away": "home" if i % 2 == 0 else "away",
            "result": "WIN" if (5 + i % 3) > (3 + i % 4) else "LOSS",
        })


def test_team_history_mlb_certified():
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import TEAM_MARKETS

    mlb_totals = next(m for m in TEAM_MARKETS
                      if m.sport == "MLB" and m.market == "totals")
    db = _FakeDB()
    _seed_mlb_team_actuals(db, n=25)
    e = _run(checks.certify_team_history_market(db, mlb_totals))
    assert e.certification_status == "PASS"
    assert e.sample_size == 25


def test_h2h_certified_when_opponent_id_present():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    _seed_mlb_team_actuals(db, n=25)
    e = _run(checks.certify_h2h(db, "MLB"))
    assert e.certification_status == "PASS"
    assert e.identity_resolved == "PASS"


def test_nhl_team_stays_unavailable_even_with_rows():
    from services.pre_magic_certification import checks
    from services.pre_magic_certification.market_catalog import TEAM_MARKETS

    nhl_spreads = next(m for m in TEAM_MARKETS
                       if m.sport == "NHL" and m.market == "spreads")
    db = _FakeDB()
    _seed_mlb_team_actuals(db, n=5)   # unrelated
    e = _run(checks.certify_team_history_market(db, nhl_spreads))
    # Catalog atoms are empty → UNAVAILABLE regardless of DB state.
    assert e.certification_status == "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# Section 8 — As-of safety
# ═══════════════════════════════════════════════════════════════════
def test_as_of_safety_blocks_future_leakage():
    """A dispatcher probe with as_of=1970 must return zero games even
    when the DB has data from 2026."""
    from services.pre_magic_certification import checks
    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=10)
    _seed_mlb_team_actuals(db, n=10)
    e = _run(checks.certify_as_of_safety(db))
    assert e.certification_status == "PASS"
    assert e.as_of_safe == "PASS"


# ═══════════════════════════════════════════════════════════════════
# Section 9 — Identity
# ═══════════════════════════════════════════════════════════════════
def test_identity_downgrades_when_canonical_missing():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    # Half the rows are missing canonical_player_id.
    for i in range(20):
        db["player_game_actuals"].docs.append({
            "sport": "mlb",
            "canonical_player_id": f"mlb-{i}" if i % 2 == 0 else None,
            "player_id": f"mlb-{i}",
            "event_time": f"2026-05-{i+1:02d}T20:00:00Z",
        })
    e = _run(checks.certify_identity(db))
    assert e.certification_status == "PARTIAL"
    assert e.identity_resolved == "PARTIAL"


def test_identity_passes_when_all_canonical():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=30)
    _seed_mlb_team_actuals(db, n=20)
    e = _run(checks.certify_identity(db))
    assert e.certification_status == "PASS"


# ═══════════════════════════════════════════════════════════════════
# Section 9b — Pick identity tagging
# ═══════════════════════════════════════════════════════════════════
def test_pick_identity_tagging_fails_when_no_canonical():
    """§8: picks lacking BOTH canonical id AND name → FAIL with
    drop_reason IDENTITY_MISSING_ON_PICKS."""
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(30):
        db["picks"].docs.append({
            "id": f"mlb-{i}", "sport": "MLB",
            "market": "Miami Marlins Moneyline",
            # No canonical_player_id, canonical_team_id, player_name, team.
        })
    out = _run(checks.certify_pick_identity_tagging(db))
    mlb = next(e for e in out if e.sport == "MLB")
    assert mlb.certification_status == "FAIL"
    assert mlb.drop_reason == "IDENTITY_MISSING_ON_PICKS"


def test_pick_identity_tagging_partial_when_names_only():
    """Names only → PARTIAL (canonical is source of truth)."""
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(30):
        db["picks"].docs.append({
            "id": f"nba-{i}", "sport": "NBA",
            "team": "Lakers",       # name-only
            "player_name": "LeBron James",
        })
    out = _run(checks.certify_pick_identity_tagging(db))
    nba = next(e for e in out if e.sport == "NBA")
    assert nba.certification_status == "PARTIAL"


def test_pick_identity_tagging_pass_when_canonical():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(30):
        db["picks"].docs.append({
            "id": f"nfl-{i}", "sport": "NFL",
            "canonical_player_id": f"nfl-{i}",
            "player_name": "Player X",
        })
    out = _run(checks.certify_pick_identity_tagging(db))
    nfl = next(e for e in out if e.sport == "NFL")
    assert nfl.certification_status == "PASS"


def test_pick_identity_tagging_unavailable_when_no_picks():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    out = _run(checks.certify_pick_identity_tagging(db))
    # Every sport should be UNAVAILABLE (no picks).
    for e in out:
        assert e.certification_status == "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# Section 10 — Market readiness / synthetic odds detection
# ═══════════════════════════════════════════════════════════════════
def test_market_readiness_detects_synthetic_odds():
    """§11, §12: any pick with odds_provenance in {MODEL, SYNTHETIC,
    FAIR, MODEL_ONLY, COMPUTED} must be flagged."""
    from services.pre_magic_certification import checks
    db = _FakeDB()
    db["picks"].docs.append({
        "id": "p1", "sport": "SOCCER", "market": "h2h",
        "book_odds": -110, "odds_provenance": "REAL",
        "implied_probability": 0.52,
    })
    db["picks"].docs.append({
        "id": "p2", "sport": "SOCCER", "market": "h2h",
        "book_odds": -110, "odds_provenance": "MODEL",
        "implied_probability": None,
    })
    e = _run(checks.certify_market_readiness(db, sample_size=100))
    # One synthetic → FAIL.
    assert e.certification_status == "FAIL"
    assert e.drop_reason == "REAL_LINE_UNAVAILABLE"


def test_market_readiness_passes_on_clean_picks():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(50):
        db["picks"].docs.append({
            "id": f"p{i}", "sport": "MLB", "market": "player_hits",
            "book_odds": -110, "odds_provenance": "REAL",
            "implied_probability": 0.52,
        })
    e = _run(checks.certify_market_readiness(db, sample_size=100))
    assert e.certification_status == "PASS"


def test_soccer_producer_integrity_detects_null_ip_with_book_odds():
    """Known ESPN soccer fixture failure mode — book_odds present but
    implied_probability null."""
    from services.pre_magic_certification import checks
    db = _FakeDB()
    db["picks"].docs.append({
        "id": "s1", "sport": "soccer", "market": "h2h",
        "book_odds": -110, "odds_provenance": "REAL",
        "implied_probability": None,   # <-- flagged
    })
    e = _run(checks.certify_soccer_producer_integrity(db))
    assert e.certification_status == "FAIL"


def test_soccer_producer_integrity_passes_clean_soccer_picks():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(10):
        db["picks"].docs.append({
            "id": f"s{i}", "sport": "soccer", "market": "h2h",
            "book_odds": -110, "odds_provenance": "REAL",
            "implied_probability": 0.52,
        })
    e = _run(checks.certify_soccer_producer_integrity(db))
    assert e.certification_status == "PASS"


# ═══════════════════════════════════════════════════════════════════
# Section 11 — Model readiness
# ═══════════════════════════════════════════════════════════════════
def test_model_readiness_passes_with_model_probability():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(20):
        db["picks"].docs.append({
            "id": f"m{i}", "sport": "MLB",
            "model_probability": 0.55,
            "engine": "mlb_hitter_intel_v3",
        })
    e = _run(checks.certify_model_readiness(db, sample_size=50))
    assert e.certification_status == "PASS"


def test_model_readiness_partial_without_provenance():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(20):
        db["picks"].docs.append({
            "id": f"m{i}", "sport": "MLB",
            "model_probability": 0.55,
            # no engine / model_source
        })
    e = _run(checks.certify_model_readiness(db, sample_size=50))
    assert e.certification_status == "PARTIAL"
    assert e.drop_reason == "MODEL_INPUT_INVALID"


# ═══════════════════════════════════════════════════════════════════
# Section 12 — Tennis context
# ═══════════════════════════════════════════════════════════════════
def test_tennis_context_reports_present_and_missing_fields():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    for i in range(10):
        db["player_game_actuals"].docs.append({
            "sport": "tennis",
            "canonical_player_id": f"t-{i}",
            "event_time": f"2026-05-{i+1:02d}T20:00:00Z",
            "surface": "hard",
            "tournament": "US Open",
            "round": "R32",
            "aces": 8, "double_faults": 2,
            # break_points_saved intentionally missing
        })
    e = _run(checks.certify_tennis_context(db))
    assert e.certification_status == "PARTIAL"
    assert "break_points_saved" in (e.detail or "")


# ═══════════════════════════════════════════════════════════════════
# Section 13 — Live pick reachability
# ═══════════════════════════════════════════════════════════════════
def test_live_pick_reachability_end_to_end():
    """§1: prove a REAL current pick can trace through the read-path
    to real historical actuals."""
    from services.pre_magic_certification import checks

    db = _FakeDB()
    # Seed history for player p1.
    for i in range(15):
        db["player_game_actuals"].docs.append({
            "sport": "mlb",
            "canonical_player_id": "mlb-p1",
            "player_id": "mlb-p1",
            "player_name": "Aaron Judge",
            "event_time": f"2026-05-{i+1:02d}T20:00:00Z",
            "hits": 1 + (i % 2),
            "total_bases": 2,
        })
    # Seed a real live pick.
    db["picks"].docs.append({
        "id": "pick-1",
        "sport": "MLB",
        "market": "player_hits",
        "player_name": "Aaron Judge",
        "canonical_player_id": "mlb-p1",
        "line": 1.5,
        "direction": "over",
        "commence_time": "2026-06-15T20:00:00Z",
        "book_odds": -110,
    })
    out = _run(checks.certify_live_pick_reachability(db, sample_size=5))
    assert len(out) == 1
    entry = out[0]
    assert entry.evidence_type == "LIVE_PICK_REACHABILITY"
    assert entry.certification_status == "PASS"
    assert entry.sample_size and entry.sample_size >= 1


def test_live_pick_reachability_no_picks_returns_unavailable():
    from services.pre_magic_certification import checks
    db = _FakeDB()
    out = _run(checks.certify_live_pick_reachability(db, sample_size=5))
    assert len(out) == 1
    assert out[0].certification_status == "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# Section 14 — Orchestrator end-to-end
# ═══════════════════════════════════════════════════════════════════
def test_build_matrix_end_to_end_with_partial_data():
    from services.pre_magic_certification import build_certification_matrix
    from services.pre_magic_certification.states import CertificationState

    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=30)
    _seed_mlb_team_actuals(db, n=25)
    m = _run(build_certification_matrix(
        db, live_pick_sample=0, market_sample=10))
    d = m.to_dict()
    # Magic MUST remain NOT_WIRED regardless (§15).
    assert d["magic_consumption"] == CertificationState.NOT_WIRED.value
    assert d["lock_score_consumption"] == "UNCHANGED"
    # Matrix must contain multiple evidence types.
    types = {e["evidence_type"] for e in d["entries"]}
    assert "PLAYER_HISTORY" in types
    assert "TEAM_HISTORY" in types
    assert "H2H" in types
    assert "MISSING_NOT_ZERO" in types
    assert "EXACT_THRESHOLD" in types
    assert "DISTRIBUTIONS" in types
    assert "MARKET_NORMALIZATION" in types


def test_build_matrix_records_unavailable_for_missing_sports():
    """§17: NHL / CFB / UFC must be UNAVAILABLE in the matrix,
    never PASS.  Even with zero DB rows."""
    from services.pre_magic_certification import build_certification_matrix

    db = _FakeDB()
    m = _run(build_certification_matrix(
        db, live_pick_sample=0, market_sample=1))
    unavailable_sports = {
        e.sport for e in m.entries
        if e.certification_status == "UNAVAILABLE"
        and e.evidence_type in ("PLAYER_HISTORY", "TEAM_HISTORY")
    }
    # These sports MUST be marked unavailable.
    assert "UFC" in unavailable_sports
    assert "NHL" in unavailable_sports
    assert "CFB" in unavailable_sports


def test_matrix_findings_include_magic_not_wired_guardrail():
    from services.pre_magic_certification import build_certification_matrix

    db = _FakeDB()
    m = _run(build_certification_matrix(
        db, live_pick_sample=0, market_sample=1))
    codes = {f["code"] for f in m.findings}
    assert "MAGIC_NOT_WIRED" in codes


# ═══════════════════════════════════════════════════════════════════
# Section 15 — history availability vs history consumption
# ═══════════════════════════════════════════════════════════════════
def test_history_available_does_not_imply_magic_consumed():
    """§15: A PASS on PLAYER_HISTORY MUST NOT upgrade Magic's state.
    Magic remains NOT_WIRED even when every history check passes."""
    from services.pre_magic_certification import build_certification_matrix
    from services.pre_magic_certification.states import CertificationState

    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=100)
    _seed_mlb_team_actuals(db, n=100)
    m = _run(build_certification_matrix(
        db, live_pick_sample=0, market_sample=1))
    # Magic MUST remain NOT_WIRED even when everything else passes.
    assert m.magic_consumption == CertificationState.NOT_WIRED.value
    # And ready_for_magic is a RECOMMENDATION not an authorization.
    assert m.recommendation is not None
    assert "READY" in (m.recommendation or "").upper() or \
           "PARTIAL" in (m.recommendation or "").upper() or \
           "NOT READY" in (m.recommendation or "").upper() or \
           "UNAVAILABLE" in (m.recommendation or "").upper()


# ═══════════════════════════════════════════════════════════════════
# Section 16 — Report writing
# ═══════════════════════════════════════════════════════════════════
def test_write_report_produces_valid_json(tmp_path):
    from services.pre_magic_certification import (
        build_certification_matrix, write_certification_report,
    )
    import json

    db = _FakeDB()
    _seed_mlb_player_actuals(db, n=10)
    m = _run(build_certification_matrix(
        db, live_pick_sample=0, market_sample=1))
    p = str(tmp_path / "pre_magic.json")
    ret = write_certification_report(m, path=p)
    assert ret.endswith("pre_magic.json")
    with open(ret) as fh:
        data = json.load(fh)
    assert "magic_consumption" in data
    assert data["magic_consumption"] == "NOT_WIRED"
    assert "entries" in data
    assert len(data["entries"]) > 0
