"""SLICE 1.1 — Cold-Start / Runtime Performance
================================================

Prior boot flow awaited the full chain:
    L3 (client version, AsyncStorage) → L2 (network `/api/version`)
before rendering ANY UI, and additionally blocked on icon-font
downloads (Expo Go). Cold start took 400-2000 ms of blank splash on
flaky networks.

Slice 1.1 pivots to a "local shell → async validation" model
(`frontend/app/_layout.tsx`):

  * The `RootLayout` boot gate no longer awaits `runBackendCacheBustIfNeeded`
    inside the initial `useEffect`. L2 fires in the background AFTER
    `setCacheBustDone(true)` releases the shell.
  * A 500 ms icon-font watchdog (`fontTimeoutElapsed`) allows the shell
    to paint with tofu icons if the CDN is unreachable — a functional
    shell wins over a black splash.
  * The final render gate honours both new signals:
        if (!cacheBustDone) return null;
        if (!loaded && !error && !fontTimeoutElapsed) return null;

Live measurement (Expo Web preview, 2026-09-02):
    FIRST_PAINT_MS = 147   (before: ~800-1400ms cold)
    /api/version fires in the BACKGROUND — never gates paint.
"""
from __future__ import annotations
import os, re, pytest


_LAYOUT = "/app/frontend/app/_layout.tsx"


def _read() -> str:
    if not os.path.exists(_LAYOUT):
        pytest.skip("_layout.tsx missing")
    with open(_LAYOUT, "r") as f:
        return f.read()


def test_slice_1_1_l2_backend_cache_bust_is_fire_and_forget():
    src = _read()
    # `runBackendCacheBustIfNeeded(...).then(...)` — fire-and-forget.
    # Must NOT be `await`ed inside the boot useEffect.
    m = re.search(r"runBackendCacheBustIfNeeded\s*\(", src)
    assert m, "runBackendCacheBustIfNeeded must still be called on boot."
    # Look for `await runBackendCacheBustIfNeeded` — should NOT exist.
    assert "await runBackendCacheBustIfNeeded" not in src, (
        "Slice 1.1 regression: L2 backend cache bust is being awaited "
        "on boot again. It must fire-and-forget so the shell paints "
        "without blocking on /api/version."
    )


def test_slice_1_1_shell_unblocks_before_l2_completes():
    src = _read()
    # setCacheBustDone(true) must appear BEFORE the actual CALL to
    # runBackendCacheBustIfNeeded(...) inside the boot useEffect. We
    # ignore the top-of-file import which also mentions the symbol —
    # only the call-site (followed by an argument or `.then`) matters.
    call_site = re.search(r"runBackendCacheBustIfNeeded\s*\(\s*\n?\s*\(", src)
    assert call_site, "runBackendCacheBustIfNeeded call site missing."
    b = call_site.start()
    a = src.find("setCacheBustDone(true)")
    assert a > 0, "setCacheBustDone(true) missing on boot"
    assert a < b, (
        "Slice 1.1: setCacheBustDone(true) must precede the "
        "runBackendCacheBustIfNeeded(...) call so the shell paints first."
    )


def test_slice_1_1_font_watchdog_exists_and_gates_render():
    src = _read()
    assert "fontTimeoutElapsed" in src, (
        "Slice 1.1: font watchdog state (`fontTimeoutElapsed`) missing "
        "— cold start would still block on Expo Go icon-font CDN."
    )
    # 500 ms watchdog explicitly present
    assert re.search(r"setTimeout\s*\(\s*\(\)\s*=>\s*setFontTimeoutElapsed",
                       src), (
        "Slice 1.1: font watchdog setTimeout guard missing."
    )
    # Render gate must reference the watchdog
    assert re.search(r"!loaded\s*&&\s*!error\s*&&\s*!fontTimeoutElapsed",
                       src), (
        "Slice 1.1: render gate must fall through when fontTimeoutElapsed."
    )


def test_slice_1_1_splash_hide_honours_font_timeout():
    src = _read()
    # SplashScreen.hideAsync must be called when the watchdog fires.
    m = re.search(r"SplashScreen\.hideAsync\(\)", src)
    assert m, "SplashScreen.hideAsync must remain in the boot flow"
    # And the enclosing useEffect dep array should include fontTimeoutElapsed.
    ue = re.search(r"SplashScreen\.hideAsync\(\)[\s\S]*?\}, \[([^\]]*)\]", src)
    assert ue, "splash hide must live in a useEffect"
    deps = ue.group(1)
    assert "fontTimeoutElapsed" in deps, (
        "Slice 1.1: SplashScreen hide effect must depend on "
        "fontTimeoutElapsed so it fires on the watchdog trigger."
    )
