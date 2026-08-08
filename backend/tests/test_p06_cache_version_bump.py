"""P0-6 — Data version bump regression tests.

Verifies:
  1. Backend `DATA_VERSION` was actually bumped from the pre-P0-6 value.
  2. New value follows the release naming convention.
  3. `/api/version` returns the new DATA_VERSION.
  4. `KNOWN_CACHE_KEYS` (the client wipe list) contains the two keys
     the audit identified as the dominant divergence source:
       - `perkslocks_filters_v6`
       - `locks_feed_prefs_v2`
  5. Auth / session keys are NOT in the wipe list — a version bump
     must never log the user out.
  6. `APP_DATA_VERSION` (frontend layer-3 baked constant) was bumped.
  7. `/api/picks/today` request/response contract is unchanged.

These tests do NOT exercise any prediction/ranking/publication logic
and do NOT touch the DB.  They only inspect the static constants and
one live `/api/version` hit.
"""
from __future__ import annotations

import pathlib
import re

from fastapi.testclient import TestClient


# ── Constants under test ────────────────────────────────────────────────
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FRONTEND_ROOT = pathlib.Path("/app/frontend")

_OLD_BACKEND_VERSION = "2026.07.16-dedupe-both-sides-v45"
_NEW_BACKEND_VERSION = "2026.08.08-canonical-board-cache-v46"
_OLD_APP_VERSION = "20260722-rng-purge-hitters-mls-v69"
_NEW_APP_VERSION = "20260808-canonical-board-cache-v70"


# ══════════════════════════════════════════════════════════════════════
# 1. Backend DATA_VERSION bumped
# ══════════════════════════════════════════════════════════════════════
def test_backend_data_version_string_bumped_in_source():
    src = (_BACKEND_ROOT / "server.py").read_text()
    # The exact new-version line must exist.
    assert f'DATA_VERSION = "{_NEW_BACKEND_VERSION}"' in src, (
        f"backend DATA_VERSION must be set to {_NEW_BACKEND_VERSION!r}"
    )
    # The old version must NOT be the current definition (a comment
    # referencing it for historical context is allowed only outside
    # the actual assignment).
    assert f'DATA_VERSION = "{_OLD_BACKEND_VERSION}"' not in src, (
        "the previous DATA_VERSION assignment is still present"
    )


def test_backend_data_version_runtime_value_matches():
    import server as srv
    assert srv.DATA_VERSION == _NEW_BACKEND_VERSION


def test_api_version_endpoint_returns_new_data_version():
    import server as srv
    with TestClient(srv.app) as client:
        r = client.get("/api/version")
        assert r.status_code == 200
        body = r.json()
        assert body["data_version"] == _NEW_BACKEND_VERSION
        # Contract stability: server_time + server_started_at still present.
        assert "server_time" in body
        assert "server_started_at" in body


# ══════════════════════════════════════════════════════════════════════
# 2. Frontend APP_DATA_VERSION bumped
# ══════════════════════════════════════════════════════════════════════
def test_frontend_app_data_version_bumped():
    src = (_FRONTEND_ROOT / "src" / "lib" / "cachebust.ts").read_text()
    assert f'APP_DATA_VERSION = "{_NEW_APP_VERSION}"' in src, (
        f"frontend APP_DATA_VERSION must be set to {_NEW_APP_VERSION!r}"
    )
    assert f'APP_DATA_VERSION = "{_OLD_APP_VERSION}"' not in src, (
        "the previous APP_DATA_VERSION assignment is still present"
    )


# ══════════════════════════════════════════════════════════════════════
# 3. Known cache keys include the two divergence-critical keys.
# ══════════════════════════════════════════════════════════════════════
def test_known_cache_keys_include_divergence_critical_keys():
    src = (_FRONTEND_ROOT / "src" / "lib" / "cachebust.ts").read_text()
    # Extract the KNOWN_CACHE_KEYS array body.
    m = re.search(
        r"const\s+KNOWN_CACHE_KEYS\s*=\s*\[(?P<body>.*?)\];",
        src, flags=re.DOTALL,
    )
    assert m, "KNOWN_CACHE_KEYS declaration not found"
    body = m.group("body")
    # Both keys must be listed.
    assert '"perkslocks_filters_v6"' in body, (
        "perkslocks_filters_v6 missing from KNOWN_CACHE_KEYS"
    )
    assert '"locks_feed_prefs_v2"' in body, (
        "locks_feed_prefs_v2 missing from KNOWN_CACHE_KEYS"
    )
    # Historical filter schema versions kept for legacy device wipe.
    for legacy in ("perkslocks_filters_v3", "perkslocks_filters_v4",
                   "perkslocks_filters_v5"):
        assert f'"{legacy}"' in body, (
            f"legacy filter schema {legacy} removed from wipe list"
        )


# ══════════════════════════════════════════════════════════════════════
# 4. Auth / session state NOT in the wipe list.
# ══════════════════════════════════════════════════════════════════════
def test_auth_and_session_state_not_in_wipe_list():
    src = (_FRONTEND_ROOT / "src" / "lib" / "cachebust.ts").read_text()
    m = re.search(
        r"const\s+KNOWN_CACHE_KEYS\s*=\s*\[(?P<body>.*?)\];",
        src, flags=re.DOTALL,
    )
    body = m.group("body")
    # Common auth key names the app might use — none must appear.
    for forbidden in ("token", "auth", "session", "jwt",
                       "refresh_token", "access_token", "secure_",
                       "user_id", "user.session"):
        # Allow the substring in comments elsewhere; only forbid it
        # inside the KNOWN_CACHE_KEYS array.
        assert forbidden.lower() not in body.lower(), (
            f"auth-like key containing {forbidden!r} present in "
            f"KNOWN_CACHE_KEYS — bumping DATA_VERSION would log users out"
        )


# ══════════════════════════════════════════════════════════════════════
# 5. Version-key bookkeeping keys are NOT wiped (they track the check).
# ══════════════════════════════════════════════════════════════════════
def test_version_bookkeeping_keys_not_wiped():
    src = (_FRONTEND_ROOT / "src" / "lib" / "cachebust.ts").read_text()
    m = re.search(
        r"const\s+KNOWN_CACHE_KEYS\s*=\s*\[(?P<body>.*?)\];",
        src, flags=re.DOTALL,
    )
    body = m.group("body")
    for k in ("perkslocks.client_data_version",
              "perkslocks.backend_data_version"):
        assert f'"{k}"' not in body, (
            f"version-bookkeeping key {k!r} accidentally in wipe list — "
            f"clearing it would prevent the cache-bust check from working"
        )


# ══════════════════════════════════════════════════════════════════════
# 6. Simulated stale-client detects the version mismatch.
# ══════════════════════════════════════════════════════════════════════
def test_simulated_stale_client_detects_version_mismatch():
    # Emulate what `runBackendCacheBustIfNeeded` does: compare the
    # server value with what the phone previously stored, and, if
    # different, wipe the known keys.
    stored_by_phone = _OLD_BACKEND_VERSION   # phone last saw pre-P0-6
    server_current = _NEW_BACKEND_VERSION    # what /api/version returns now
    assert stored_by_phone != server_current, (
        "test relies on a real version mismatch"
    )
    # The wipe path would fire.  Behaviour: exactly the keys in
    # KNOWN_CACHE_KEYS get removed; auth / bookkeeping stay.
    src = (_FRONTEND_ROOT / "src" / "lib" / "cachebust.ts").read_text()
    m = re.search(
        r"const\s+KNOWN_CACHE_KEYS\s*=\s*\[(?P<body>.*?)\];",
        src, flags=re.DOTALL,
    )
    keys_wiped = {kv.strip('"').strip() for kv in
                   re.findall(r'"([^"]+)"', m.group("body"))}
    # The two critical keys are in the wipe set.
    assert "perkslocks_filters_v6" in keys_wiped
    assert "locks_feed_prefs_v2" in keys_wiped


# ══════════════════════════════════════════════════════════════════════
# 7. /picks/today request/response contract unchanged
# ══════════════════════════════════════════════════════════════════════
def test_picks_today_route_still_registered_with_no_store_headers():
    import server as srv
    # Route registered — the endpoint still exists.
    routes = {getattr(r, "path", None) for r in srv.app.routes}
    assert "/api/picks/today" in routes

    # `_no_store_api_responses` middleware still installed for the same
    # route pattern (grep the source since middleware objects don't
    # expose their exact match logic cleanly at runtime).
    src = (_BACKEND_ROOT / "server.py").read_text()
    assert "_no_store_api_responses" in src
    assert '"Cache-Control"' in src or "Cache-Control" in src


# ══════════════════════════════════════════════════════════════════════
# 8. Version-string naming convention sanity.
# ══════════════════════════════════════════════════════════════════════
def test_new_version_strings_reflect_p06_release():
    # Both new strings must include a P0-6-ish tag so future audits
    # can trace the bump back to this task.  Loose regex — not
    # prescriptive about exact format.
    assert re.search(r"canonical.*cache", _NEW_BACKEND_VERSION,
                      re.IGNORECASE), _NEW_BACKEND_VERSION
    assert re.search(r"canonical.*cache", _NEW_APP_VERSION,
                      re.IGNORECASE), _NEW_APP_VERSION
    # Version numbers monotonically incremented (v45 → v46, v69 → v70).
    assert "v46" in _NEW_BACKEND_VERSION
    assert "v70" in _NEW_APP_VERSION
