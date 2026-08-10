"""Phase 1 (2026-08-11) — Core plumbing + complete sports wiring.

Covers:
  1. ESPN fallback hardcoded 401 wording removed.
  2. Frontend BASE_URL centralization (getBackendUrl helper, lab.tsx
     migrated, no silent PINNED_PREVIEW_URL fall-through on native
     production builds).
  3. Sport capability registry — single source of truth.
  4. NFL prop-fetch loop wired.
  5. `/api/picks/markets/{sport}` counts use the canonical >85 gate.
"""
from __future__ import annotations

import pathlib


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FRONTEND_ROOT = pathlib.Path("/app/frontend")


def _read(rel: str, root=None) -> str:
    return ((root or _BACKEND_ROOT) / rel).read_text()


# ── 1. ESPN fallback text ───────────────────────────────────────────
def test_espn_fallback_removes_hardcoded_401_language():
    src = _read("services/espn_soccer_fixtures.py")
    # The three hardcoded "Odds API 401" strings that used to render
    # on picks must be gone from the pick_rationale block.
    idx = src.find('"engine_version": "espn_soccer_fixtures.v1"')
    assert idx > 0
    window = src[idx:idx + 1000]
    assert "Odds API 401" not in window
    assert "Odds API subscription is currently unavailable" not in window


# ── 2. Frontend BASE_URL centralization ─────────────────────────────
def test_api_ts_exports_get_backend_url():
    src = _read("src/lib/api.ts", root=_FRONTEND_ROOT)
    assert "export function getBackendUrl" in src


def test_api_ts_native_prod_fails_loudly_when_env_missing():
    src = _read("src/lib/api.ts", root=_FRONTEND_ROOT)
    # The fail-loud path: on native, when env is empty AND not __DEV__,
    # we return "" and log — the old silent fall-back to PINNED_PREVIEW_URL
    # in that branch must be gone.
    idx = src.find("Native (Expo Go / built app)")
    assert idx > 0
    window = src[idx:idx + 1600]
    # New behaviour markers.
    assert "if (__DEV__) return PINNED_PREVIEW_URL" in window
    assert "console.error" in window
    # Old silent-fallback line removed from this branch.
    assert 'return (envUrl && envUrl.trim().length > 0) ? envUrl : PINNED_PREVIEW_URL' not in window


def test_lab_tsx_uses_centralized_backend_url():
    src = _read("app/(tabs)/lab.tsx", root=_FRONTEND_ROOT)
    # Import brought in.
    assert "getBackendUrl" in src
    # The old direct env read on the correlations-v2 URL is gone.
    assert "process.env.EXPO_PUBLIC_BACKEND_URL || \"\"" not in src


# ── 3. Sport capability registry ────────────────────────────────────
def test_registry_lists_enabled_sports():
    from services.sport_capability_registry import (
        enabled_sports, is_enabled,
    )
    expected_enabled = {"MLB", "NBA", "NFL", "CFB", "Soccer",
                         "Tennis", "UFC", "NHL"}
    assert set(enabled_sports()) == expected_enabled
    for sport in expected_enabled:
        assert is_enabled(sport) is True


def test_registry_disables_wnba_and_kbo():
    from services.sport_capability_registry import is_enabled
    assert is_enabled("WNBA") is False
    assert is_enabled("KBO") is False


def test_registry_nfl_has_prop_markets():
    from services.sport_capability_registry import prop_markets_for
    props = prop_markets_for("NFL")
    for tok in ("player_pass_yds", "player_rush_yds",
                "player_reception_yds", "player_anytime_td"):
        assert tok in props, f"NFL prop missing: {tok}"


def test_registry_cfb_and_nhl_have_no_props():
    from services.sport_capability_registry import prop_markets_for
    assert prop_markets_for("CFB") == []
    assert prop_markets_for("NHL") == []


def test_registry_ufc_has_no_props():
    from services.sport_capability_registry import prop_markets_for
    assert prop_markets_for("UFC") == []


def test_registry_all_enabled_support_locks():
    """>85 Locks board contract applies to every enabled sport."""
    from services.sport_capability_registry import (
        enabled_sports, supports_locks,
    )
    for s in enabled_sports():
        assert supports_locks(s) is True, s


# ── 4. NFL prop-fetch loop wired ────────────────────────────────────
def test_nfl_in_prop_sports_loop():
    src = _read("sports_engine.py")
    # The loop must include NFL now.
    marker = 'prop_sports = [s for s in ("MLB", "NBA", "NFL", "Soccer") if _want(s)]'
    assert marker in src, "NFL must be in the prop_sports loop"


# ── 5. /markets/{sport} uses canonical gate ─────────────────────────
def test_markets_endpoint_uses_canonical_gate():
    src = _read("routes/picks_routes.py")
    # The old buggy `_qualifies` (elite bypass + edge >= 0 + lock >= 85)
    # must be gone.  New version delegates to is_main_board_eligible.
    assert "from services.main_board_eligibility import is_main_board_eligible" in src
    # No `return lock >= 85 and edge >= 0` anywhere in the file.
    assert "return lock >= 85 and edge >= 0" not in src
    # No elite bypass in the /markets endpoint.
    idx = src.find("async def markets_for_sport(")
    assert idx > 0
    end = src.find("return {", idx)
    window = src[idx:end + 400]
    # Elite reads are removed.
    assert "if elite:" not in window
    assert 'p.get("off_board")' in window   # new off_board guard present


# ── 6. Regression: strict >85 contract intact ───────────────────────
def test_locks_gate_still_strict_gt_85():
    from services.main_board_eligibility import (
        is_main_board_eligible, main_board_lock_score_query,
    )
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
    q = main_board_lock_score_query()
    assert q["$or"][0] == {"published_lock_score": {"$gt": 85.0}}


# ── 7. Capability matrix summary is stable/complete ────────────────
def test_capability_matrix_contract():
    from services.sport_capability_registry import capability_matrix
    matrix = capability_matrix()
    for sport in ("MLB", "NBA", "NFL", "CFB", "Soccer", "Tennis",
                  "UFC", "NHL", "WNBA", "KBO"):
        assert sport in matrix, sport
        entry = matrix[sport]
        for key in ("enabled", "game_markets", "prop_markets",
                    "fallback_sources", "supports_alt_lines",
                    "supports_locks", "notes"):
            assert key in entry, f"{sport}.{key}"
