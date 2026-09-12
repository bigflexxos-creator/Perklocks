"""
Focused regression: CFB stale pre-fix safety net (2026-06-11)

Verifies that CFB picks minted before the empty-factors + no-provenance
legacy scoring path was replaced cannot re-surface on the board even
if they remain in `db.picks` as PUBLISHED rows.  The safety net at
`routes.picks_routes.picks_today` suppresses any CFB pick that
carries BOTH:
  * an empty ``factors`` dict (pre-fix signature)
  * ``lock_score`` >= 90

Live examples the user reported:
  * Texas Southern ML +1500  → LS 98
  * Alabama State ML +950    → LS 98
"""
import pytest


def _apply_safety(picks):
    """Mirror of the routes.picks_routes safety-net block."""
    kept = []
    blocked = 0
    for p in picks:
        if str(p.get("sport") or "").upper() == "CFB" \
           and float(p.get("lock_score") or 0) >= 90 \
           and not (p.get("factors") or {}):
            blocked += 1
            continue
        kept.append(p)
    return kept, blocked


def test_texas_southern_stale_row_blocked():
    stale = {
        "sport": "CFB",
        "event": "Texas Southern Tigers @ UTEP Miners",
        "market": "Texas Southern Tigers Moneyline",
        "book_odds": 1500,
        "win_probability": 54.14,
        "lock_score": 98.0,
        "factors": {},
    }
    out, blocked = _apply_safety([stale])
    assert blocked == 1
    assert out == []


def test_alabama_state_stale_row_blocked():
    stale = {
        "sport": "CFB",
        "event": "Alabama State Hornets @ Troy Trojans",
        "market": "Alabama State Hornets Moneyline",
        "book_odds": 1000,
        "win_probability": 51.83,
        "lock_score": 98.0,
        "factors": {},
    }
    out, blocked = _apply_safety([stale])
    assert blocked == 1
    assert out == []


def test_new_cfb_pick_with_real_factors_passes():
    """Post-fix CFB pick with real Model Fair Prob / Projected Margin
    must survive — safety net cannot suppress legitimate rows."""
    good = {
        "sport": "CFB",
        "event": "Georgia Bulldogs @ Vanderbilt Commodores",
        "market": "Georgia Bulldogs Moneyline",
        "book_odds": -750,
        "win_probability": 87.1,
        "lock_score": 88.0,
        "factors": {
            "Projected Margin": 20.5,
            "Expected Total":   52.0,
            "Model Fair Prob":  87.1,
            "Sportsbook Implied Prob": 88.2,
            "__data_quality":   "sp_plus",
        },
    }
    out, blocked = _apply_safety([good])
    assert blocked == 0
    assert out == [good]


def test_cfb_low_lock_stale_row_passes():
    """LS < 90 stale rows are OK — the safety net targets ONLY the
    elite-authority claim on empty-factor rows."""
    low = {
        "sport": "CFB",
        "event": "X @ Y",
        "market": "X Moneyline",
        "lock_score": 72.0,
        "factors": {},
    }
    out, blocked = _apply_safety([low])
    assert blocked == 0


def test_non_cfb_rows_untouched():
    nfl = {
        "sport": "NFL",
        "market": "Player A 5+ Receptions",
        "lock_score": 96.0,
        "factors": {},
    }
    out, blocked = _apply_safety([nfl])
    assert blocked == 0


def test_mixed_batch_only_stale_cfb_dropped():
    picks = [
        # stale CFB high-lock
        {"sport": "CFB", "lock_score": 98.0, "factors": {}, "event": "TxSU"},
        # legitimate CFB with factors
        {"sport": "CFB", "lock_score": 88.0,
         "factors": {"Model Fair Prob": 87.1}, "event": "Georgia"},
        # NFL — never touched
        {"sport": "NFL", "lock_score": 96.0, "factors": {}, "event": "KC"},
        # MLB — never touched
        {"sport": "MLB", "lock_score": 92.0, "factors": {}, "event": "NYY"},
    ]
    out, blocked = _apply_safety(picks)
    assert blocked == 1
    assert len(out) == 3
    assert not any("TxSU" in (p.get("event") or "") for p in out)
