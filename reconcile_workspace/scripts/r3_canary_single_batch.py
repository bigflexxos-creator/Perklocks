"""R3 canary — single 500-doc batch for games.

Verifies that with MONGO_SOCKET_TIMEOUT_MS=300000 live on Prod,
a single 500-doc bulk_write completes comfortably under budget
AND the Phase-5-R3 atomic protocol marks the batch succeeded ONLY
after persistence.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tarfile
import time
import urllib.request
import urllib.error

CHKP = pathlib.Path("/app/reconcile_workspace/checkpoints")
P5_TAR = CHKP / "phase5_20261003_190628Z.tar.gz"
SESSION = "perklocks-cutover-20261003-r3"

API      = os.environ["PROD_API_BASE"].rstrip("/")
EMAIL    = os.environ["PROD_ADMIN_EMAIL"]
PASSWORD = os.environ["PROD_ADMIN_PASSWORD"]
TOKEN    = os.environ["CANONICAL_IMPORT_TOKEN"]


def _post(url, headers, body, timeout=320):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(url, data=data,
                                   headers={**headers, "Content-Type": "application/json"},
                                   method="POST")
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
    code, body = _post(f"{API}/api/auth/login", {}, {"email": EMAIL, "password": PASSWORD})
    if code != 200 or not body.get("access_token"):
        print(f"ERROR login {code}", file=sys.stderr); return 1
    jwt = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    hdr_t = {**hdr_a, "X-Canonical-Import-Token": TOKEN}
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    code, status = _get(f"{API}/api/admin/data-authority/status", hdr_a)
    if code != 200: print(f"ERROR preflight {code}", file=sys.stderr); return 2
    flags = status.get("env_flags", {})
    print(f"[preflight] env_flags = {json.dumps(flags, sort_keys=True)}")
    for k, want in {"CANONICAL_FALLBACK_MODE": True, "USE_CANONICAL_DB": False,
                     "CANONICAL_IMPORT_ENABLED": True,
                     "BACKGROUND_WORKERS_ENABLED": False}.items():
        if flags.get(k) != want:
            print(f"ERROR {k}={flags.get(k)} want {want}", file=sys.stderr); return 3

    print("\n[phase4.5] scoped canonical reset …")
    code, rst = _post(f"{API}/api/admin/canonical-cutover/reset-canonical-dataset",
                        hdr_t, {"confirm": True,
                                "i_understand_this_drops_canonical": "YES-DROP-CANONICAL-DATASET",
                                "only_canonical_prefix_collections_allowed": True,
                                "session_id_to_clear_bookkeeping_for": SESSION})
    if code != 200 or not rst.get("all_dropped"):
        print(f"ERROR reset {code} {rst}", file=sys.stderr); return 4
    print(f"[phase4.5] all_dropped={rst.get('all_dropped')}  bookkeeping_cleared={rst.get('bookkeeping_cleared')}")

    tmp = pathlib.Path("/opt/reconcile_tmp/canary_r3")
    if tmp.exists():
        import shutil; shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    with tarfile.open(P5_TAR) as tf:
        for m in tf.getmembers():
            if m.name.endswith("canonical/games.ndjson"):
                tf.extract(m, path=tmp); break
    src = tmp / "v2_phase5" / "canonical" / "games.ndjson"
    docs = []
    with open(src) as f:
        for ln in f:
            if len(docs) >= 500: break
            r = json.loads(ln); docs.append(r.get("doc", r))
    print(f"[canary] loaded {len(docs)} games docs for batch #0")

    body_req = {"collection": "games", "docs": docs, "session_id": SESSION,
                 "batch_no": 0, "source_checkpoints": {"p5": P5_TAR.name}}
    print(f"[canary] POST /api/admin/canonical-import (500 docs, client-timeout 320 s) …")
    t0 = time.monotonic()
    code, resp = _post(f"{API}/api/admin/canonical-import", hdr_t, body_req, timeout=320)
    dt = time.monotonic() - t0
    print(f"\n[canary] client wall-clock = {dt:.2f} s")
    print(f"[canary] HTTP status       = {code}")
    print(f"[canary] response          = {json.dumps(resp, indent=2) if isinstance(resp, dict) else resp}")

    if code != 200 or not isinstance(resp, dict):
        print("\n❌ CANARY FAILED — STOP.", file=sys.stderr); return 5

    code, fa = _get(
        f"{API}/api/admin/canonical-import/forensic-audit"
        f"?session_id={SESSION}&collection=games", hdr_a)
    if code != 200:
        print(f"ERROR forensic audit {code} {fa}", file=sys.stderr); return 6
    first_batch = (fa.get("first_10_batches") or [None])[0] or {}
    print(f"\n[forensic] batch_count={fa.get('batch_count')}  "
          f"sum_upserted={fa.get('audit',{}).get('sum_upserted_count')}  "
          f"sum_matched={fa.get('audit',{}).get('sum_matched_count')}  "
          f"bulk_write_errors={fa.get('audit',{}).get('n_bulk_write_error_events')}")
    print(f"[forensic] batch[0].status={first_batch.get('status')}")
    print(f"[forensic] batch[0].doc_count={first_batch.get('doc_count')}")
    print(f"[forensic] canonical_games count={fa.get('canonical_now',{}).get('count_total')}")

    upserted = int(resp.get("upserted") or 0)
    matched  = int(resp.get("matched")  or 0)
    accepted = int(resp.get("accepted") or 0)
    skipped = sum(1 for r in (resp.get("rejections") or [])
                     if (r or {}).get("reason") == "MISSING_LOGICAL_KEY_COMPONENT")
    ops = accepted - skipped
    verify_ok = (upserted + matched) == ops
    status_ok = (first_batch.get("status") == "succeeded")
    under_80pct = dt < 240

    print(f"\n[VERDICT]")
    print(f"  bulk_write wall-clock      = {dt:.2f} s  (<240 s = {under_80pct})")
    print(f"  verify_gate upserted+matched = {upserted+matched} / ops={ops}  → {verify_ok}")
    print(f"  status=succeeded (after persist) = {status_ok}")
    print(f"  idempotent_replay          = {resp.get('idempotent_replay')}")
    print(f"  upserted                   = {upserted}")
    print(f"  matched                    = {matched}")

    overall = (verify_ok and status_ok and under_80pct
                 and resp.get("idempotent_replay") is False)
    print(f"\n[CANARY] {'✅ PASS' if overall else '❌ FAIL / NEAR-LIMIT — STOP'}")
    return 0 if overall else 7


if __name__ == "__main__":
    sys.exit(main())
