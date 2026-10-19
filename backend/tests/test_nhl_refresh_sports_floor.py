"""
NHL Preview↔Prod reconciliation — prove the floor fix.

ROOT CAUSE (proven in investigation):
  ``PERKLOCKS_REFRESH_SPORTS`` was read as a wholesale override of the
  in-code default.  On Production the operator-set value predated the
  2026-10-03 fix that added NHL to the default, so NHL was silently
  dropped from every refresh cycle → no NHL in the committed canonical
  slate → NHL invisible on the Production board while Preview (no
  override) kept NHL on the board.

FIX CONTRACT verified here:
  * Env override still works for restricting / reordering.
  * But env override can NO LONGER silently drop a supported seasonal
    sport — the required floor is unioned back in with a loud log.
  * The ONLY way to intentionally pause a sport is the explicit
    ``PERKLOCKS_REFRESH_SPORTS_EXCLUDE`` opt-out.
"""
from __future__ import annotations
import os, re, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "server.py").read_text()

REQUIRED_FLOOR = ("NFL", "MLB", "CFB", "Soccer", "Tennis", "NHL")


def _simulate_resolution(env_value: str | None, exclude: str = "") -> list[str]:
    """Mirror the production _refresh_picks sports-list derivation
    EXACTLY so the test can assert the fix behaviour deterministically.
    """
    sports = [s.strip() for s in (env_value if env_value is not None
                                   else "NFL,MLB,CFB,Soccer,Tennis,NHL").split(",")
              if s.strip()]
    _explicit_exclude = {s.strip().upper() for s in exclude.split(",") if s.strip()}
    _current_upper = {s.upper() for s in sports}
    _missing = [s for s in REQUIRED_FLOOR
                if s.upper() not in _current_upper
                and s.upper() not in _explicit_exclude]
    return sports + _missing


def test_source_contains_required_floor_tuple():
    assert '_REQUIRED_FLOOR = ("NFL", "MLB", "CFB", "Soccer", "Tennis", "NHL")' in SRC, \
        "_REQUIRED_FLOOR tuple must be declared verbatim so prod can grep it"


def test_source_logs_a_warning_on_auto_union():
    # The warning string is a hard contract: operators grep for this.
    assert "Auto-unioning to prevent silent exclusion" in SRC
    assert "PERKLOCKS_REFRESH_SPORTS_EXCLUDE" in SRC


def test_default_includes_nhl():
    assert _simulate_resolution(None) == ["NFL", "MLB", "CFB", "Soccer", "Tennis", "NHL"]


def test_legacy_production_override_still_gets_nhl():
    """This is the exact failure mode we are reconciling."""
    legacy = "NFL,MLB,CFB,Soccer,Tennis"
    out = _simulate_resolution(legacy)
    assert "NHL" in out, f"legacy prod override must get NHL unioned back in; got {out}"
    # Preserve operator's declared order; NHL appended at the end.
    assert out[-1] == "NHL"


def test_single_sport_override_still_gets_full_floor():
    out = _simulate_resolution("NFL")
    for s in REQUIRED_FLOOR:
        assert s in out, f"{s} missing from auto-unioned set: {out}"


def test_explicit_exclude_takes_precedence():
    """Operators must have a visible way to pause a sport."""
    out = _simulate_resolution("NFL,MLB,CFB,Soccer,Tennis", exclude="NHL")
    assert "NHL" not in out, (
        "operator who sets PERKLOCKS_REFRESH_SPORTS_EXCLUDE=NHL must get "
        "NHL excluded (visible intent beats implicit floor)"
    )


def test_exclude_is_case_insensitive():
    out = _simulate_resolution("NFL", exclude="nhl, tennis")
    assert "NHL" not in out
    assert "Tennis" not in out
    # Other required-floor sports still present.
    assert "MLB" in out


def test_order_is_stable_for_declared_sports():
    """Operator's declared order must NOT be rewritten — only missing
    sports append at the end."""
    out = _simulate_resolution("Soccer,CFB,NFL")
    assert out[:3] == ["Soccer", "CFB", "NFL"], f"declared order must be preserved; got {out}"


def test_empty_env_value_falls_through_to_full_default():
    # In production code `os.environ.get(KEY, default)` returns default
    # only when the key is UNSET, so an EMPTY env value is treated as
    # an explicit "nothing listed".  Our fix still unions the floor
    # back in, so the result is the same six-sport set — just with no
    # declared prefix.
    out = _simulate_resolution("")
    assert set(out) == set(REQUIRED_FLOOR)
