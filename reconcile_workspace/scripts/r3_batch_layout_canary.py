"""r3_batch_layout_canary — ZERO-WRITE hash-match proof for the R3
same-session Phase-5 resume.

Context
───────
R3 Resume #10 failed in Phase 5 because the accelerated resume driver
repartitioned existing session batches using a fresh 250-record
slicing plan.  Local payloads for existing batch_no values no longer
matched what the server had stored for session
``perklocks-cutover-20261003-r3``, so Production correctly rejected
them with HTTP 409 ``ALTERED_REPLAY_REJECTED``.

The server replay-safety contract is correct.  The driver's batch-
layout reconstruction was wrong.

This script proves — before any write — that the FIXED driver
reproduces every prior batch's payload bytes/hash exactly.

What it does
────────────
1. Authenticates against Prod (same credential flow as the resume
   script).  JWT lives in memory only.
2. Preflights env flags.  No flag flips.
3. Invokes ``push_canonical_accelerated.py`` with ``CANARY_ONLY=1``
   so the driver:
     * fetches the authoritative per-batch manifest
     * infers each collection's ORIGINAL partition size
     * materialises each existing batch locally
     * computes server-identical payload hashes
     * compares to the server's stored ``content_hash``
     * issues ZERO POSTs
4. The canary report is written to ``CANARY_REPORT_PATH`` as JSON and
   also printed to stdout in a human-readable
   ``collection | batch_id | expected_hash | computed_hash | MATCH``
   table.

Exit codes
──────────
    0  — every planned batch's local hash matched the server's
         stored content_hash.  SAFE to dispatch a same-session
         Phase-5 resume once this proof is on file.
    44 — BATCH_LAYOUT_MISMATCH on at least one batch.  The driver
         STOPPED before any POST.  Investigate the mismatch report
         before re-running.
    45 — Canary report produced but at least one row failed MATCH.
   10+ — Pre-flight / login / status failure.

Zero-write guarantee
────────────────────
The driver honours ``CANARY_ONLY=1`` by returning before every POST
site.  No write path reaches the HTTP transport.  See
``push_canonical_accelerated.py`` for the exact short-circuit sites.
Nothing in Production is mutated by this script.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr); sys.exit(2)
    return v


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _post(url, headers, body, timeout=120):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(url, data=data,
            headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")

    print(f"[auth] POST {_redact(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code}", file=sys.stderr); return 10
    jwt = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    print("[preflight] GET /api/admin/data-authority/status")
    code, status = _get(f"{api_base}/api/admin/data-authority/status", hdr_a)
    if code != 200:
        print(f"ERROR preflight {code}", file=sys.stderr); return 11
    flags = status.get("env_flags", {})
    print(f"[preflight] env_flags = {json.dumps(flags, sort_keys=True)}")

    # Invoke the accelerated driver with CANARY_ONLY=1.  Driver
    # performs the authoritative per-batch manifest fetch, local
    # reconstruction, hash computation, and comparison.  ZERO POSTs.
    print("\n==================== CANARY — ZERO-WRITE HASH PROOF ====================")
    env = {**os.environ,
            "PROD_API_BASE":            api_base,
            "PROD_ADMIN_JWT":           jwt,
            "CANONICAL_IMPORT_TOKEN":   import_tok,
            "CANONICAL_IMPORT_SESSION": session,
            "CANARY_ONLY":              "1",
            # new-collection batch size is irrelevant in CANARY_ONLY
            # mode (there is no POST path) but we keep the value
            # consistent with the production workflow for parity.
            "NEW_COLLECTION_BATCH_SIZE": os.environ.get("NEW_COLLECTION_BATCH_SIZE", "1000"),
          }
    scripts_dir = pathlib.Path(__file__).resolve().parent
    push_driver_path = str(scripts_dir / "push_canonical_accelerated.py")

    # Stream driver output directly to stdout so GH-Actions log shows
    # the full MATCH table in real-time.
    p = subprocess.run([sys.executable, "-u", push_driver_path], env=env)
    print(f"\n[canary] driver exit={p.returncode}")

    report_path = env.get("CANARY_REPORT_PATH") or ""
    if report_path and pathlib.Path(report_path).exists():
        try:
            rpt = json.loads(pathlib.Path(report_path).read_text())
            all_match = rpt.get("all_match")
            print(f"[canary] all_match={all_match}  "
                  f"total_batches_planned={rpt.get('total_batches_planned')}  "
                  f"session={session}")
        except Exception as _e:
            print(f"[canary] report parse failed: {_e}", file=sys.stderr)

    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
