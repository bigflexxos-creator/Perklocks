"""MAIN 41 · P0-A Expo parity source-defect regression protection.

1. Native API resolver MUST NOT silently fall back to
   ``PINNED_PREVIEW_URL`` when ``EXPO_PUBLIC_BACKEND_URL`` is absent.
2. ``locks_picks_cache_v1`` MUST be in ``KNOWN_CACHE_KEYS`` so an
   orphaned Locks board cache clears on the next launch after any
   version bump.
3. ``APP_DATA_VERSION`` bumped so existing Expo Go devices actually
   wipe on next launch.
"""
from __future__ import annotations

import os


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_native_api_resolver_has_no_silent_dev_fallback():
    src = _read("/app/frontend/src/lib/api.ts")
    # Strip comments to check the ACTUAL code path — the disabled
    # pattern may legitimately appear in an explanatory comment.
    import re
    code_only = re.sub(r"//[^\n]*", "", src)
    code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.DOTALL)
    # The regression pattern the user flagged: the exact silent
    # fallback must be gone from executable code.
    assert 'if (__DEV__) return PINNED_PREVIEW_URL' not in code_only, (
        "resolveBaseUrl still silently falls back to PINNED_PREVIEW_URL "
        "when EXPO_PUBLIC_BACKEND_URL is absent — this masks Preview↔Expo "
        "parity defects."
    )
    # The new resolver must LOG the resolved backend for parity audits.
    assert '[api] Native backend origin resolved' in src


def test_locks_cache_key_now_in_cachebust_list():
    src = _read("/app/frontend/src/lib/cachebust.ts")
    assert '"locks_picks_cache_v1"' in src, (
        "Locks board cache key missing from KNOWN_CACHE_KEYS — orphaned "
        "cached picks can survive across version bumps."
    )


def test_app_data_version_bumped_for_locks_cache_wipe():
    src = _read("/app/frontend/src/lib/cachebust.ts")
    # New version must clearly identify this closure so any future
    # rollback / bisect is straightforward.
    assert '20260906-main41-locks-cache-bust-v1' in src


def test_locks_cache_key_matches_the_actual_writer():
    """Guard against drift between the writer and the cache-bust list."""
    home_src = _read("/app/frontend/app/(tabs)/index.tsx")
    bust_src = _read("/app/frontend/src/lib/cachebust.ts")
    # Extract the exact string the Home screen writes.
    assert 'const PICKS_CACHE_KEY = "locks_picks_cache_v1"' in home_src
    assert '"locks_picks_cache_v1"' in bust_src


if __name__ == "__main__":
    test_native_api_resolver_has_no_silent_dev_fallback()
    test_locks_cache_key_now_in_cachebust_list()
    test_app_data_version_bumped_for_locks_cache_wipe()
    test_locks_cache_key_matches_the_actual_writer()
    print("OK — MAIN 41 Expo parity source-defects protected.")
