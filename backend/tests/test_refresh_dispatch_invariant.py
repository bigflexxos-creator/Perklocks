"""Regression test — scheduled refresh dispatch must include every
ACTIVE sport in the model authority registry.

Why this exists
---------------
Twice in 2026 the same bug shipped:
  * 2026-10-03 — NHL was ACTIVE in the authority registry but omitted
    from ``PERKLOCKS_REFRESH_SPORTS`` default + ``_REQUIRED_FLOOR``.
    Preview had NHL on the board; Production did not.
  * 2026-10-20 — NBA was ACTIVE in the authority registry but omitted
    from both lists.  Same symptom: Preview had NBA, Production did
    not — because Preview tested NBA via direct manual refreshes
    while Production relied solely on the scheduled dispatch.

Both regressions were silent — nothing in the capability audit
failed, provider feeds were healthy, pods were on current image.
The ONLY tell was a ``SPORT_REFRESH_STARVED`` log line.

Invariant
---------
Every sport that owns at least one canonical (non-UNAVAILABLE) model
in ``services.sport_model_authority.AUTHORITY`` must be represented
in BOTH ``PERKLOCKS_REFRESH_SPORTS`` (default) AND ``_REQUIRED_FLOOR``.

This test does NOT weaken fail-closed behavior — it only asserts
that activated sports are scheduled.
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sport_model_authority import AUTHORITY, UNAVAILABLE


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def _active_sports() -> set[str]:
    """Return the set of sports with ≥1 canonical model !≈ UNAVAILABLE."""
    active: set[str] = set()
    for sport, families in AUTHORITY.items():
        for _fam, cfg in (families or {}).items():
            canonical = (cfg or {}).get("canonical")
            if canonical and canonical != UNAVAILABLE:
                active.add(sport)
                break
    return active


def _read_server_source() -> str:
    return SERVER_PATH.read_text(encoding="utf-8")


def _default_refresh_sports(src: str) -> set[str]:
    """Parse the hard-coded fallback string in
    ``PERKLOCKS_REFRESH_SPORTS`` env lookup."""
    m = re.search(
        r'"PERKLOCKS_REFRESH_SPORTS"\s*,\s*"([^"]+)"',
        src,
    )
    assert m, "PERKLOCKS_REFRESH_SPORTS default literal not found in server.py"
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def _required_floor(src: str) -> set[str]:
    m = re.search(
        r'_REQUIRED_FLOOR\s*=\s*\(([^)]+)\)',
        src,
    )
    assert m, "_REQUIRED_FLOOR tuple not found in server.py"
    return {
        tok.strip().strip('"').strip("'")
        for tok in m.group(1).split(",")
        if tok.strip().strip('"').strip("'")
    }


def test_every_active_sport_is_in_default_refresh_dispatch():
    active = _active_sports()
    src = _read_server_source()
    defaults = _default_refresh_sports(src)
    missing = sorted(active - defaults)
    assert not missing, (
        f"Active sports absent from PERKLOCKS_REFRESH_SPORTS default: "
        f"{missing}. Add them to the default literal in server.py. "
        "This is the same silent-exclusion bug that hid NHL in Oct '26 "
        "and NBA in Oct '26 — never reintroduce it."
    )


def test_every_active_sport_is_in_required_floor():
    active = _active_sports()
    src = _read_server_source()
    floor = _required_floor(src)
    missing = sorted(active - floor)
    assert not missing, (
        f"Active sports absent from _REQUIRED_FLOOR: {missing}. "
        "Add them so an operator PERKLOCKS_REFRESH_SPORTS override "
        "can no longer silently drop a supported sport."
    )


def test_nba_is_active_and_scheduled():
    """Explicit guard for the Oct '26 Production incident."""
    assert "NBA" in _active_sports(), (
        "NBA capability was intentionally removed from the authority "
        "registry. If this is deliberate, also remove NBA from the "
        "scheduler and update these tests."
    )
    src = _read_server_source()
    assert "NBA" in _default_refresh_sports(src), (
        "NBA is ACTIVE but missing from PERKLOCKS_REFRESH_SPORTS "
        "default — this is the exact bug that caused the Oct 20 2026 "
        "Production live-board blocker.  Do not reintroduce."
    )
    assert "NBA" in _required_floor(src), (
        "NBA is ACTIVE but missing from _REQUIRED_FLOOR — the operator "
        "override would silently drop NBA again."
    )
    # The dedicated pregame refresh loop must also be registered.
    assert "_nba_pregame_loop" in src, (
        "NBA pregame refresh loop is not defined in server.py."
    )
    assert "nba_pregame_refresh_today" in src, (
        "NBA pregame today-slate lease key not registered in server.py."
    )


if __name__ == "__main__":
    # Allow `python backend/tests/test_refresh_dispatch_invariant.py`
    # as a cheap smoke run outside pytest.
    test_every_active_sport_is_in_default_refresh_dispatch()
    test_every_active_sport_is_in_required_floor()
    test_nba_is_active_and_scheduled()
    print("OK — refresh-dispatch invariants hold.")
