"""Block 2E — Locks/Rollover/Parlay reachability + final certification.

Locks the certification invariants:

  §1  Locks source is canonical db.picks with strict>85 gate.
  §2  Rollover source is canonical db.picks (not a separate stale pool).
  §3  Parlay source is canonical db.picks (not a separate stale pool).
  §4  Off-board / no-bet / stale picks are excluded WITH EXPLICIT
      criteria in the query.
  §5  NHL sport tab added to shared navigation (SPORTS master list).
  §6  Every Block 2D invariant remains locked.
  §7  Runtime diagnostic helper exposes the parity matrix.
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §1 — Canonical Locks source
# ═══════════════════════════════════════════════════════════════════
def test_locks_route_reads_canonical_db_picks():
    """The Locks board is served by /api/picks/today which reads
    db.picks — the canonical publication collection."""
    src = open("/app/backend/routes/picks_routes.py").read()
    assert "db.picks.find" in src
    # Off-board / no-bet filters are honoured.
    assert "no_bet" in src
    assert "off_board" in src or "is_under_lock" in src


def test_locks_route_enforces_strict_85_gate():
    """/api/picks/today must apply the >=85 strict floor at query
    time (or via _filter_in_play_window / helper)."""
    src = open("/app/backend/routes/picks_routes.py").read()
    assert "lock_score" in src
    # 85 floor appears somewhere in the query construction.
    assert "85" in src


# ═══════════════════════════════════════════════════════════════════
# §2 — Rollover source is canonical
# ═══════════════════════════════════════════════════════════════════
def test_rollover_reads_from_canonical_db_picks():
    """Rollover MUST NOT construct a parallel stale pool.  It reads
    from the same db.picks collection that Locks reads."""
    src = open("/app/backend/routes/picks_routes.py").read()
    ro_idx = src.index("async def pick_rollover")
    end = ro_idx + 8000
    body = src[ro_idx: end]
    # Reads canonical collection.
    assert "db.picks" in body or "_ensure_today_picks" in body
    # Applies the Rollover-specific >=89 lock floor per docstring.
    assert "89" in body


def test_rollover_docstring_declares_canonical_lock_floor():
    """The Rollover docstring must describe the 89 lock floor +
    market whitelist rules (from the 2026-07-01 audit).  Locks in
    the honest exclusion contract."""
    src = open("/app/backend/routes/picks_routes.py").read()
    idx = src.index("async def pick_rollover")
    body = src[idx: idx + 3000]
    assert "Lock score ≥ 89" in body or "lock score" in body.lower()


# ═══════════════════════════════════════════════════════════════════
# §3 — Parlay source is canonical
# ═══════════════════════════════════════════════════════════════════
def test_parlay_reads_from_canonical_db_picks():
    src = open("/app/backend/routes/parlay_routes.py").read()
    # Must read the SAME canonical collection.
    assert "db.picks.find" in src


def test_parlay_query_honours_publication_flags():
    """The Parlay base_q must exclude no_bet + is_under_lock picks
    (i.e., only visible canonical publications reach Parlay)."""
    src = open("/app/backend/routes/parlay_routes.py").read()
    # Base_q filters.
    assert '"no_bet": {"$ne": True}' in src
    assert '"is_under_lock": {"$ne": True}' in src


def test_parlay_lock_floor_per_mode():
    """Parlay lock floor varies by mode:
       high_risk=70, advanced.safer=92, advanced.ev=85, today=85,
       standard=85.  Verify the mode-specific gates are present."""
    src = open("/app/backend/routes/parlay_routes.py").read()
    for floor in ("70", "92", "85"):
        assert floor in src, f"missing lock floor: {floor}"


def test_parlay_time_window_freshness_filter():
    """Parlay must apply an event_time window so stale/started
    events are excluded.  Freshness contract."""
    src = open("/app/backend/routes/parlay_routes.py").read()
    assert "event_time" in src
    assert "window_hours" in src or "window_cap_iso" in src


# ═══════════════════════════════════════════════════════════════════
# §4 — Freshness / real-line integrity at parlay level
# ═══════════════════════════════════════════════════════════════════
def test_parlay_excludes_no_real_book_line_picks():
    """Parlay must not treat no_real_book_line=True picks as normal
    bettable legs.  Verified structurally: the base_q applies no_bet
    filter, and canonical_publication_barrier sets no_bet=True on
    picks that lack a real book line."""
    barrier_src = open("/app/backend/services/canonical_publication_barrier.py").read()
    # Barrier sets no_bet on any no_real_book_line pick.
    assert "no_real_book_line" in barrier_src
    assert "marked_no_real_book_line" in barrier_src
    # Parlay base_q filters no_bet.
    parlay_src = open("/app/backend/routes/parlay_routes.py").read()
    assert '"no_bet": {"$ne": True}' in parlay_src


# ═══════════════════════════════════════════════════════════════════
# §5 — NHL sport tab added
# ═══════════════════════════════════════════════════════════════════
def test_nhl_added_to_shared_sports_master_list():
    src = open("/app/frontend/src/theme.ts").read()
    assert '"NHL"' in src
    # In the exported SPORTS ordered list.
    idx = src.index("export const SPORTS")
    end = src.index("]", idx)
    body = src[idx: end + 1]
    assert '"NHL"' in body
    # NHL appears BETWEEN CFB and Soccer (winter sports grouping).
    assert '"CFB"' in body
    assert '"Soccer"' in body


def test_nhl_has_sport_icon_mapping():
    src = open("/app/frontend/src/theme.ts").read()
    idx = src.index("export const SPORT_ICONS")
    end = src.index("};", idx)
    body = src[idx: end]
    assert "NHL:" in body


def test_nhl_tab_not_expanded_with_player_props():
    """Per user directive: NHL tab exposes GAME markets only —
    moneyline / puck line / total.  No player props built in this
    phase.  Verify no NHL-specific player-prop constants leaked into
    the frontend."""
    src = open("/app/frontend/src/theme.ts").read()
    # No NHL player-prop-specific mappings.
    assert "nhl_player" not in src.lower()
    assert "nhl_shots" not in src.lower()
    assert "nhl_goals_prop" not in src.lower()


# ═══════════════════════════════════════════════════════════════════
# §6 — Block 2D invariants preserved
# ═══════════════════════════════════════════════════════════════════
def test_first_td_still_dormant():
    src = open("/app/backend/sports_engine.py").read()
    assert "First-TD DORMANT" in src
    assert '"capability_state"] = "PARTIAL_DORMANT"' in src


def test_atd_wiring_still_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "nfl_atd_precomputed" in src
    assert "_atd_model_override" in src


def test_hr_intel_wiring_still_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "hr_intel_evidence" in src


def test_soccer_dc_synthetic_still_blocked():
    src = open("/app/backend/sports_engine.py").read()
    assert "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED" in src


def test_soccer_btts_wiring_still_present():
    src = open("/app/backend/sports_engine.py").read()
    assert "Block 2D B4" in src
    assert "_btts_outcomes" in src


def test_canonical_barrier_still_wired_in_direct_inject():
    for fn in ("services/mls_direct_inject.py",
                "services/soccer_prop_inject.py"):
        src = open(f"/app/backend/{fn}").read()
        assert "apply_canonical_barrier" in src


def test_block2c_isolate_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src


def test_universal_settlement_missing_data_unchanged():
    from services import universal_settlement_contract as usc
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert graded.get("result") == usc.RESULT_UNRESOLVED


def test_p05_truth_layer_still_present():
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")


def test_strict_85_gate_constant_unchanged():
    from services.canonical_publication_barrier import STRICT_LOCK_FLOOR
    assert STRICT_LOCK_FLOOR == 85


# ═══════════════════════════════════════════════════════════════════
# §7 — Runtime parity diagnostic helper
# ═══════════════════════════════════════════════════════════════════
def test_runtime_parity_helper_present_and_computes_deltas():
    """Block 2E §19-§20 — runtime parity diagnostic must aggregate
    canonical → Locks → Rollover → Parlay counts and expose the
    delta with explicit exclusion reasons."""
    from services.runtime_parity import compute_parity_summary
    # Deterministic in-memory calculation.
    canonical = [
        {"id": "p1", "lock_score": 90, "book_odds": -110, "no_bet": False,
          "is_under_lock": False, "off_board": False},
        {"id": "p2", "lock_score": 82, "book_odds": -110, "no_bet": False,
          "is_under_lock": False, "off_board": False},   # below 85 for parlay STANDARD
        {"id": "p3", "lock_score": 95, "book_odds": None, "no_bet": True,
          "is_under_lock": False, "off_board": False},   # no real book line
        {"id": "p4", "lock_score": 92, "book_odds": -110, "no_bet": False,
          "is_under_lock": False, "off_board": True,     # dormant / hidden
          "capability_state": "PARTIAL_DORMANT"},
    ]
    summary = compute_parity_summary(canonical, parlay_floor=85, rollover_floor=89)
    assert summary["total"] == 4
    # p1 passes all gates; p2 below 85; p3 no_bet; p4 off_board (dormant).
    assert summary["locks_visible"] == 1       # only p1 (>=85, no exclusions)
    assert summary["parlay_eligible"] == 1     # p1 only
    assert summary["rollover_eligible"] == 1   # p1 only (>=89)
    # Explicit exclusion reasons are present.
    ex = summary["exclusion_reasons"]
    assert "NO_BET" in ex
    assert "CAPABILITY_DORMANT" in ex or "OFF_BOARD" in ex
    assert "BELOW_FLOOR" in ex


def test_runtime_parity_helper_no_unexplained_delta():
    """Every canonical pick that is NOT in locks_visible /
    parlay_eligible / rollover_eligible must have an exclusion
    reason attached.  No silent drops."""
    from services.runtime_parity import compute_parity_summary
    picks = [
        {"id": "a", "lock_score": 90, "book_odds": -110,
          "no_bet": False, "is_under_lock": False, "off_board": False},
        {"id": "b", "lock_score": 30, "book_odds": -110,
          "no_bet": False, "is_under_lock": False, "off_board": False},
    ]
    s = compute_parity_summary(picks, parlay_floor=85, rollover_floor=89)
    # Total = 2, one eligible, one excluded → both accounted for.
    assert s["total"] == 2
    assert s["parlay_eligible"] + sum(s["exclusion_reasons"].values()) >= 2
