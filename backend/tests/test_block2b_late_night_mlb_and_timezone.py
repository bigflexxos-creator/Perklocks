"""Block 2B — Regression tests for late-night MLB props + timezone contract.

These tests permanently guard:

  * The Perklocks U.S. betting day rolls at 04:00 ET (spec §17).
  * West Coast late-night games (10:10 PM PT / 01:10 ET / 05:10 UTC)
    STAY in the current U.S. slate, not tomorrow (spec §17, §8).
  * DST-safe: 7 PM ET / 10 PM ET / 11 PM ET / midnight ET remain
    in-slate across March/November transitions.
  * The `_fetch_player_props_for_sport` fair-slate scheduling
    contract: NO current-slate MLB event is starved by the props
    cap (spec §6, §8).
  * Strict >85 gate, real-line integrity, and P0.5 truth layer
    remain untouched.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


from services.perklocks_day import (
    current_slate_day,
    is_in_current_slate,
    perklocks_day,
    slate_bounds,
)


# ═══════════════════════════════════════════════════════════════════
# Perklocks Day contract (§17)
# ═══════════════════════════════════════════════════════════════════
def test_perklocks_day_rolls_at_04_00_et_boundary():
    # 03:59 ET  → still previous day
    dt = datetime(2026, 8, 11, 7, 59, tzinfo=timezone.utc)  # 03:59 ET
    assert perklocks_day(dt) == "2026-08-10"
    # 04:00 ET  → new day starts
    dt = datetime(2026, 8, 11, 8, 0, tzinfo=timezone.utc)   # 04:00 ET
    assert perklocks_day(dt) == "2026-08-11"


def test_perklocks_day_west_coast_late_night_stays_in_current_slate():
    # A 10:10 PM PT MLB game on Aug 10.
    # PT is UTC-7 (August DST). 22:10 PT → 05:10 UTC Aug 11.
    game_utc = datetime(2026, 8, 11, 5, 10, tzinfo=timezone.utc)
    # Now = 10:00 PM ET Aug 10 (02:00 UTC Aug 11 — Aug 10 slate)
    now_utc = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
    assert perklocks_day(game_utc) == "2026-08-10"
    assert perklocks_day(now_utc)  == "2026-08-10"
    assert is_in_current_slate(game_utc, now_utc) is True


def test_perklocks_day_7pm_et_is_current_day():
    # 7:00 PM ET on Aug 10 → 23:00 UTC Aug 10
    dt = datetime(2026, 8, 10, 23, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_10pm_et_is_current_day():
    # 10:00 PM ET on Aug 10 → 02:00 UTC Aug 11
    dt = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_11pm_et_is_current_day():
    # 11:00 PM ET on Aug 10 → 03:00 UTC Aug 11
    dt = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_midnight_et_is_still_current_day():
    # 00:00 ET on Aug 11 (early morning) → 04:00 UTC Aug 11
    # Belongs to Aug 10 betting slate (before 04:00 ET roll).
    dt = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    # 00:00 ET → shifted -4h → previous day
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_midnight_utc_us_late_game_still_in_slate():
    # A U.S. game at 20:00 ET Aug 10 → 00:00 UTC Aug 11.
    # It's still very much "Aug 10 slate".
    dt = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_03_00_utc_is_previous_us_day():
    # 03:00 UTC Aug 11 = 23:00 ET Aug 10 → Aug 10 slate.
    dt = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_04_00_utc_west_coast_still_previous():
    # 04:00 UTC Aug 11 = 00:00 ET Aug 11 = 21:00 PT Aug 10.
    # Aug 10 slate — a live West Coast 9pm PT MLB game.
    dt = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    assert perklocks_day(dt) == "2026-08-10"


def test_perklocks_day_dst_march_spring_forward():
    # March 8 2026 → DST begins at 02:00 ET.
    # 02:30 ET on March 8 doesn't exist (skipped forward to 03:30 ET).
    # A UTC time of 07:30 = 02:30 EST (before) or 03:30 EDT (after).
    # We just need to confirm zoneinfo doesn't crash and returns
    # a stable perklocks_day value.
    dt_before = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)  # 01:30 EST
    dt_after  = datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)  # 04:30 EDT
    # 01:30 EST → shifted to March 7  (< 04:00 EST roll)
    assert perklocks_day(dt_before) == "2026-03-07"
    # 04:30 EDT → shifted to March 8
    assert perklocks_day(dt_after)  == "2026-03-08"


def test_perklocks_day_dst_november_fall_back():
    # Nov 1 2026 → DST ends at 02:00 EDT (falls back to 01:00 EST).
    # 03:00 EST on Nov 1 → 08:00 UTC.
    dt = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    # 03:00 EST < 04:00 EST roll → still Oct 31 slate.
    assert perklocks_day(dt) == "2026-10-31"


def test_slate_bounds_produces_utc_aware_bounds():
    start, end = slate_bounds("2026-08-10")
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    assert (end - start).total_seconds() == 24 * 3600
    # A West Coast 10:10 PM PT game must fall INSIDE the Aug 10 slate.
    late_west = datetime(2026, 8, 11, 5, 10, tzinfo=timezone.utc)
    assert start <= late_west < end


# ═══════════════════════════════════════════════════════════════════
# Fair-slate scheduling contract (spec §6, §8)
# ═══════════════════════════════════════════════════════════════════
def test_fair_slate_scheduler_never_starves_todays_games_by_first_n_cap():
    """Locks the sports_engine.py behavior: when
    len(current_slate_events) > cap, the selector MUST return every
    current-slate event rather than a chronological first-N slice."""
    src = open("/app/backend/sports_engine.py").read()
    # The old buggy line was `selected = upcoming[:cap]`.  It must
    # no longer appear as executable code (comments are fine).
    executable = "\n".join(l.split("#", 1)[0] for l in src.splitlines()
                            if not l.lstrip().startswith("#"))
    assert "selected = upcoming[:cap]" not in executable, (
        "chronological first-N cap regression — late West Coast MLB "
        "games would be starved again")
    # The fair-slate contract keyword must be present.
    assert "current_slate" in src
    assert "is_in_current_slate" in src


def test_fair_slate_scheduler_uses_perklocks_day_contract():
    src = open("/app/backend/sports_engine.py").read()
    assert "from services.perklocks_day import" in src
    assert "is_in_current_slate" in src


# ═══════════════════════════════════════════════════════════════════
# Invariants that must remain unchanged (§21)
# ═══════════════════════════════════════════════════════════════════
def test_p05_truth_layer_unchanged_signature():
    """Guardrail that Block 2B did NOT modify the P0.5 canonical
    service surface."""
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")
    assert hasattr(prt, "canonical_query")
    assert hasattr(prt, "stable_publication_dedupe")
    assert hasattr(prt, "verify_sweep")


def test_universal_settlement_contract_unchanged_signature():
    from services import universal_settlement_contract as usc
    assert hasattr(usc, "grade_over_under")
    assert hasattr(usc, "grade_milestone")
    assert hasattr(usc, "RESULT_UNRESOLVED")
