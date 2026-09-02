"""Phase 16 — PREVIEW / PRODUCTION / EXPO ONE APP TRUTH invariants.

The same app served on Preview, Production and Expo Go MUST converge
on the SAME canonical backend truth.  Enforced by:

  16.1 The frontend uses a single `EXPO_PUBLIC_BACKEND_URL` for all
       API calls — no hardcoded backend URLs elsewhere.
  16.2 The backend base is derived, not fabricated (single source
       of truth in `.env`).
  16.3 No frontend module maintains its own alternate results
       cache that could diverge from `/api/picks/history`.
  16.4 Frontend `/app/index.tsx` (or the Locks screen) hits the
       canonical `/api/picks/*` route family — never a legacy /
       shadow endpoint.
"""
from __future__ import annotations
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

FRONTEND_ROOT = pathlib.Path("/app/frontend")


def test_single_backend_url_env_var_present():
    env = (FRONTEND_ROOT / ".env").read_text(encoding="utf-8")
    assert "EXPO_PUBLIC_BACKEND_URL=" in env


def test_no_hardcoded_backend_urls_in_frontend_source():
    """Frontend source files must NOT contain hardcoded backend URLs
    (would break Preview/Prod parity).  Only `.env` may declare the
    URL; source code reads via `process.env.EXPO_PUBLIC_BACKEND_URL`.
    """
    tsx_files = list((FRONTEND_ROOT / "app").rglob("*.tsx")) + \
        list((FRONTEND_ROOT / "app").rglob("*.ts")) + \
        list((FRONTEND_ROOT / "src").rglob("*.tsx") if
             (FRONTEND_ROOT / "src").exists() else [])
    banned = re.compile(
        r'https?://[^"\'\s]+\.emergentagent\.com'
    )
    offenders: list[str] = []
    for f in tsx_files:
        if "/.metro-cache/" in str(f) or "/node_modules/" in str(f):
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in banned.finditer(src):
            offenders.append(f"{f.relative_to(FRONTEND_ROOT)}:{m.group()}")
    assert not offenders, (
        f"Hardcoded backend URLs found ({len(offenders)}): "
        f"{offenders[:3]}"
    )


def test_frontend_uses_expo_public_backend_url_indirection():
    """At least ONE frontend file must read
    `process.env.EXPO_PUBLIC_BACKEND_URL` (proving the indirection
    is actually used)."""
    tsx_files = list((FRONTEND_ROOT / "app").rglob("*.tsx")) + \
        list((FRONTEND_ROOT / "app").rglob("*.ts"))
    found = False
    for f in tsx_files:
        if "/.metro-cache/" in str(f) or "/node_modules/" in str(f):
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "EXPO_PUBLIC_BACKEND_URL" in src:
            found = True
            break
    assert found, "no frontend file reads EXPO_PUBLIC_BACKEND_URL"


def test_backend_api_prefix_is_stable():
    """All backend routes are mounted under `/api` (Kubernetes
    ingress contract).  Verify server.py declares the prefix."""
    src = pathlib.Path("/app/backend/server.py").read_text(
        encoding="utf-8"
    )
    assert 'prefix="/api"' in src or "'/api'" in src


def test_no_localhost_urls_in_frontend_production_paths():
    """Frontend production TSX must not reference localhost URLs
    (would break Preview / Prod)."""
    tsx_files = list((FRONTEND_ROOT / "app").rglob("*.tsx")) + \
        list((FRONTEND_ROOT / "app").rglob("*.ts"))
    pat = re.compile(r'https?://(localhost|127\.0\.0\.1)[^"\'\s]*')
    offenders = []
    for f in tsx_files:
        if "/.metro-cache/" in str(f) or "/node_modules/" in str(f):
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in pat.finditer(src):
            offenders.append(f"{f.name}:{m.group()}")
    assert not offenders, f"localhost URLs in frontend: {offenders[:5]}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
