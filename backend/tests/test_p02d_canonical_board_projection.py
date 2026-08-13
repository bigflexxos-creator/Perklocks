"""P0.2d — Canonical Board Projection tests."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.board_projection_service import (
    BoardProjectionService,
    dedupe_canonical,
    deterministic_sort,
    filter_sport,
    _canonical_pick_id,
)


# Helper — build a canonical-ish pick fixture.  All fields required
# for `is_main_board_eligible` to pass are set by default.
def _pick(**over):
    base = {
        "id":                   over.pop("id", "p-1"),
        "sport":                "MLB",
        "market":               "Strikeouts Over 4.5",
        "side":                 "Over",
        "line":                 4.5,
        "book_odds":            -115,
        "implied_probability":  0.535,
        "lock_score":           88.0,
        "published_lock_score": 88.0,
        "event_id":             "e-1",
        "event_time":           "2026-08-13T20:00:00Z",
        "no_bet":               False,
        "off_board":            False,
        "hide_from_main_board": False,
    }
    base.update(over)
    return base


# ═════════════════════════════════════════════════════════════════════
# §A — Eligibility: canonical inputs
# ═════════════════════════════════════════════════════════════════════

class TestCanonicalEligibility:
    def test_eligible_pick_appears(self):
        svc = BoardProjectionService()
        p = _pick(id="ok-1", published_lock_score=90.0)
        assert svc.project([p]) == [p]

    def test_ineligible_lock_score_out(self):
        svc = BoardProjectionService()
        p = _pick(id="lo-1", published_lock_score=80.0, lock_score=80.0)
        assert svc.project([p]) == []

    def test_no_real_book_line_out(self):
        svc = BoardProjectionService()
        p = _pick(id="nb-1", book_odds=None, implied_probability=None)
        assert svc.project([p]) == []

    def test_off_board_out(self):
        svc = BoardProjectionService()
        p = _pick(id="ob-1", off_board=True)
        assert svc.project([p]) == []

    def test_no_bet_out(self):
        svc = BoardProjectionService()
        p = _pick(id="nb2-1", no_bet=True)
        assert svc.project([p]) == []

    def test_hide_from_main_board_out(self):
        svc = BoardProjectionService()
        p = _pick(id="h-1", hide_from_main_board=True)
        assert svc.project([p]) == []


# ═════════════════════════════════════════════════════════════════════
# §B — Cross-surface parity: ALL vs sport tab
# ═════════════════════════════════════════════════════════════════════

class TestCrossSurfaceParity:
    def test_all_vs_sport_tab_same_universe(self):
        svc = BoardProjectionService()
        picks = [
            _pick(id="mlb-1", sport="MLB"),
            _pick(id="mlb-2", sport="MLB", market="Hits Over 0.5",
                  event_id="e-2", published_lock_score=91.0),
            _pick(id="nfl-1", sport="NFL", market="Pass Yds Over 224.5",
                  event_id="e-3", published_lock_score=87.0),
        ]
        all_ids = svc.project_ids(picks, sport=None)
        mlb_ids = svc.project_ids(picks, sport="MLB")
        nfl_ids = svc.project_ids(picks, sport="NFL")
        # Sport tab is a strict subset of ALL — no new picks invented.
        assert set(mlb_ids).issubset(set(all_ids))
        assert set(nfl_ids).issubset(set(all_ids))
        # Union of sport tabs equals ALL when covering every sport.
        assert set(mlb_ids) | set(nfl_ids) == set(all_ids)

    def test_preview_main_mobile_membership_parity(self):
        """Equivalent Locks surfaces (preview / main / mobile) that
        route through the same projection produce identical membership."""
        svc = BoardProjectionService()
        picks = [_pick(id=f"parity-{i}") for i in range(5)]
        # Simulate three surfaces all calling the same projection.
        main    = svc.project_ids(picks, sport=None)
        preview = svc.project_ids(picks, sport=None)
        mobile  = svc.project_ids(picks, sport=None)
        assert set(main) == set(preview) == set(mobile)


# ═════════════════════════════════════════════════════════════════════
# §C — Deduplication
# ═════════════════════════════════════════════════════════════════════

class TestCanonicalDedupe:
    def test_exact_canonical_duplicate_collapses(self):
        # Two rows with same canonical id — must collapse to one.
        p1 = _pick(id="dup-1")
        p2 = _pick(id="dup-1", published_lock_score=89.0)   # duplicate id
        svc = BoardProjectionService()
        out = svc.project([p1, p2])
        assert len(out) == 1

    def test_distinct_alt_lines_remain_distinct(self):
        # Same player/market family, DIFFERENT lines → both survive.
        p1 = _pick(id="alt-1", market="Passing Yards Over 224.5",
                    line=224.5, published_lock_score=87.0)
        p2 = _pick(id="alt-2", market="Passing Yards Over 249.5",
                    line=249.5, published_lock_score=87.0)
        svc = BoardProjectionService()
        ids = svc.project_ids([p1, p2])
        assert set(ids) == {"alt-1", "alt-2"}

    def test_distinct_over_under_same_line_remain_distinct(self):
        p1 = _pick(id="ou-o", side="Over",  line=4.5)
        p2 = _pick(id="ou-u", side="Under", line=4.5,
                    published_lock_score=87.0)
        svc = BoardProjectionService()
        assert set(svc.project_ids([p1, p2])) == {"ou-o", "ou-u"}


# ═════════════════════════════════════════════════════════════════════
# §D — Pipeline order: filter/dedupe cannot hide a valid pick
# ═════════════════════════════════════════════════════════════════════

class TestPipelineOrder:
    def test_ineligible_duplicate_does_not_shadow_eligible(self):
        """A stale duplicate with the WRONG (ineligible) score must
        not shadow an eligible copy — canonical dedupe is deterministic
        and eligibility is applied FIRST."""
        p_bad  = _pick(id="pipe-1", published_lock_score=80.0)   # ineligible
        p_good = _pick(id="pipe-1", published_lock_score=91.0)   # eligible
        svc = BoardProjectionService()
        out = svc.project([p_bad, p_good])
        # Only the eligible copy survives the eligibility gate; dedupe
        # then collapses to one row (the good one).
        assert len(out) == 1
        assert out[0]["published_lock_score"] == 91.0


# ═════════════════════════════════════════════════════════════════════
# §E — Deterministic sort + rebuild
# ═════════════════════════════════════════════════════════════════════

class TestDeterministicOrder:
    def test_sort_by_lock_desc(self):
        picks = [
            _pick(id="s-1", published_lock_score=87.0),
            _pick(id="s-2", published_lock_score=95.0, event_id="e-2"),
            _pick(id="s-3", published_lock_score=90.0, event_id="e-3"),
        ]
        svc = BoardProjectionService()
        out = svc.project(picks)
        assert [p["id"] for p in out] == ["s-2", "s-3", "s-1"]

    def test_repeated_projection_deterministic(self):
        picks = [_pick(id=f"r-{i}", published_lock_score=87.0 + i,
                        event_id=f"e-{i}") for i in range(5)]
        svc = BoardProjectionService()
        a = svc.project_ids(picks)
        b = svc.project_ids(picks)
        c = svc.project_ids(picks)
        assert a == b == c

    def test_ties_use_stable_tiebreaker(self):
        # Same lock score → stable sort by (event_time, id).
        p1 = _pick(id="tie-a", published_lock_score=90.0,
                    event_time="2026-08-13T20:00:00Z")
        p2 = _pick(id="tie-b", published_lock_score=90.0,
                    event_time="2026-08-13T18:00:00Z", event_id="e-2")
        svc = BoardProjectionService()
        assert [p["id"] for p in svc.project([p1, p2])] == ["tie-b", "tie-a"]


# ═════════════════════════════════════════════════════════════════════
# §F — Read-only + frozen truth
# ═════════════════════════════════════════════════════════════════════

class TestReadOnlyFrozenTruth:
    def test_project_does_not_mutate_input(self):
        p = _pick(id="ro-1", published_lock_score=90.0)
        original = dict(p)
        svc = BoardProjectionService()
        svc.project([p])
        # Every field on the pick record is unchanged.
        for k, v in original.items():
            assert p[k] == v

    def test_frozen_line_odds_sportsbook_lock_preserved(self):
        p = _pick(id="fr-1", published_lock_score=90.0,
                   line=4.5, book_odds=-115, sportsbook="DraftKings")
        svc = BoardProjectionService()
        [out] = svc.project([p])
        assert out["line"] == 4.5
        assert out["book_odds"] == -115
        assert out["sportsbook"] == "DraftKings"
        assert out["lock_score"] == 88.0
        assert out["published_lock_score"] == 90.0


# ═════════════════════════════════════════════════════════════════════
# §G — Empty board / no filler
# ═════════════════════════════════════════════════════════════════════

class TestEmptyBoard:
    def test_empty_input_returns_empty(self):
        assert BoardProjectionService().project([]) == []

    def test_all_ineligible_returns_empty_not_filled(self):
        picks = [_pick(id=f"emp-{i}", published_lock_score=80.0)
                 for i in range(5)]
        assert BoardProjectionService().project(picks) == []


# ═════════════════════════════════════════════════════════════════════
# §H — Lifecycle visibility (delegated to caller)
# ═════════════════════════════════════════════════════════════════════

class TestLifecycleFilter:
    def test_lifecycle_callback_applied(self):
        picks = [
            _pick(id="lc-1", published_lock_score=90.0,
                   event_time="2026-08-13T20:00:00Z"),
            _pick(id="lc-2", published_lock_score=90.0,
                   event_time="2020-01-01T00:00:00Z", event_id="e-2"),
        ]
        svc = BoardProjectionService()

        def _drop_old(pool):
            return [p for p in pool
                    if (p.get("event_time") or "") >= "2026-01-01"]

        ids = svc.project_ids(picks, lifecycle_filter=_drop_old)
        assert ids == ["lc-1"]

    def test_settled_status_not_re_settled_by_board(self):
        # Board doesn't grade — a pick with status='won' should still
        # project (visibility) as long as canonical eligibility holds.
        # Real production wires lifecycle to exclude these; the service
        # itself does NOT decide settlement.
        p = _pick(id="set-1", status="won")
        svc = BoardProjectionService()
        [out] = svc.project([p])
        assert out["status"] == "won"


# ═════════════════════════════════════════════════════════════════════
# §I — Cross-sport coverage
# ═════════════════════════════════════════════════════════════════════

class TestCrossSportCoverage:
    @pytest.mark.parametrize("sport,market", [
        ("MLB",    "Hits Over 0.5"),
        ("MLB",    "1st_inning_runs"),      # NRFI/YRFI (would normally be hidden)
        ("Soccer", "Anytime Goal Scorer"),
        ("Tennis", "Match Winner"),
        ("NFL",    "Passing Yards Over 249.5"),
        ("NBA",    "Points Over 24.5"),
        ("NHL",    "Shots on Goal Over 2.5"),
        ("CFB",    "Rushing Yards Over 89.5"),
        ("UFC",    "Method of Victory"),
    ])
    def test_sport_projects(self, sport, market):
        p = _pick(id=f"{sport}-1", sport=sport, market=market)
        svc = BoardProjectionService()
        assert svc.project_ids([p], sport=sport) == [f"{sport}-1"]


# ═════════════════════════════════════════════════════════════════════
# §J — Rogue board-path static guard
# ═════════════════════════════════════════════════════════════════════

class TestNoRogueBoardPaths:
    """Detect production paths that independently construct alternate
    Locks-board membership by querying `db.picks` with a lock-score
    predicate AND returning them to a Locks-like endpoint WITHOUT
    routing through ``BoardProjectionService`` /
    ``is_main_board_eligible`` / ``main_board_lock_score_query``.

    Narrow allowlist — files that legitimately construct Locks
    membership or are the canonical projection boundary itself.
    """
    ALLOWED_FILES = {
        # Canonical projection boundary
        "services/board_projection_service.py",
        # Canonical eligibility contract (queried directly by projection)
        "services/main_board_eligibility.py",
        # Main board endpoints (consume the canonical helpers)
        "routes/picks_routes.py",
        # Server-side helpers (`_ensure_today_picks`, `_canonicalize_picks`,
        # `_filter_in_play_window`) — behind `picks_routes.py`
        "server.py",
        # Specialized products that intentionally maintain their own
        # eligibility contract (not Locks membership).
        "routes/parlay_routes.py",
        "routes/parlay_history_routes.py",
        "parlay_history.py",
        "parlay_leg_settle.py",
        "routes/nfl_routes.py",       # NFL ATD leaderboard (separate product)
        "routes/mlb_hr_routes.py",    # HR boards (separate product)
        "routes/admin_routes.py",     # admin diagnostics
        "routes/me_performance_routes.py",
        "routes/user_bets_routes.py",
        "routes/analytics_routes.py",
        "routes/lab_routes.py",
        "lab_routes.py",
        # Ingestion / pick generators (write path, not board projection)
        "sports_engine.py",
        "brain/nrfi_engine.py",
        # Rollover — specialized product with its own eligibility
        "rollover_history_tagger.py",
        # ── Analytics / calibration / research (NOT board projection) ──
        # These consume SETTLED picks for analytics, calibration,
        # backtesting, and training.  They never construct a Locks
        # membership list; they read history.  Explicitly allowlisted
        # so the P0.2d guard focuses on real board-building rogues.
        "soccer_lab.py",              # Lab research surface
        "lock_calibration.py",        # calibration training on settled picks
        "analytics.py",               # analytics dashboards on settled picks
        "backtest.py",                # backtesting research
        "learning_system_v2.py",      # learning-engine training
    }

    LOCK_PAT = re.compile(
        r"""(?:lock_score|published_lock_score|lock_score_v2)\s*"""
        r"""["']?\s*:\s*\{\s*["']\$g[te]"""
    )
    PICKS_QUERY_PAT = re.compile(
        r"""db\.picks\.(?:find|aggregate|count_documents)"""
    )

    def test_scan_backend_for_rogue_locks_paths(self):
        backend = Path("/app/backend")
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in self.ALLOWED_FILES:
                continue
            src = py.read_text(errors="ignore")
            if self.PICKS_QUERY_PAT.search(src) and self.LOCK_PAT.search(src):
                rogue.append(rel)
        assert not rogue, (
            "Rogue Locks-board paths (bypass BoardProjectionService / "
            "is_main_board_eligible): " + str(rogue))


# ═════════════════════════════════════════════════════════════════════
# §K — Wrong-identity fail-closed at the projection layer
# ═════════════════════════════════════════════════════════════════════

class TestWrongIdentityFailClosed:
    def test_missing_canonical_id_does_not_shadow_by_identity_alone(self):
        # Two rows with NO canonical id but SAME (event, market, side,
        # line) tuple should collapse — the second is a duplicate on
        # identity.
        p1 = {"sport": "MLB", "market": "Ks Over 4.5", "side": "Over",
              "line": 4.5, "event_id": "e-1", "book_odds": -115,
              "implied_probability": 0.53, "published_lock_score": 91.0,
              "no_bet": False, "off_board": False,
              "hide_from_main_board": False}
        p2 = dict(p1)
        svc = BoardProjectionService()
        out = svc.project([p1, p2])
        assert len(out) == 1

    def test_wrong_event_id_does_not_collapse(self):
        # SAME market/side/line, DIFFERENT event → keep both.
        p1 = _pick(id="w-1", event_id="e-A")
        p2 = _pick(id="w-2", event_id="e-B")
        svc = BoardProjectionService()
        out = svc.project([p1, p2])
        assert len(out) == 2
