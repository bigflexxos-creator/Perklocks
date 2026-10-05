"""resume_r3_no_reset — resume the R3 cutover on the SAME session
without any reset / soft-reset / collection drop.

Why a separate script?
──────────────────────
``run_phase5_through_8_r3.py`` always runs Phase 4.5 (either hard-drop
of canonical_* collections or a soft-reset of session bookkeeping).
Neither is allowed under the current directive — the 655 already-
succeeded batches of session ``perklocks-cutover-20261003-r3`` must
be preserved in-place, and the 218 Pass-1 persistent failures from
the previous process invocation must be re-attempted against the
now-fixed Production Mongo timeout.

What this script does
─────────────────────
1. Login against Prod (POST /api/auth/login) — JWT lives in memory only.
2. Preflight read of /api/admin/data-authority/status (sanity check:
   USE_CANONICAL_DB=false, BACKGROUND_WORKERS_ENABLED=false,
   CANONICAL_IMPORT_ENABLED=true).  NO flag flips.
3. **SKIP Phase 4.5 entirely.**
4. Phase 5: invoke ``push_canonical_to_production.py`` as a subprocess
   with the SAME session id.  The driver iterates every source row;
   batches already recorded ``status=succeeded`` on the server are
   returned as ``idempotent_replay=True`` and skipped in-fast-path.
   Pass 1 → Pass 2 retry logic (3-attempt cap) is unchanged.
5. Phase 6: safe no-op (no PHASE6_APPROVED_COLLECTIONS).
6. Phase 7: POST /api/admin/canonical-cutover/create-indexes.
7. Phase 8: POST /api/admin/canonical-cutover/r3-certification with
   the certified source audit (same math as the main orchestrator).

STOP at Phase 9.  NEVER touches USE_CANONICAL_DB or
BACKGROUND_WORKERS_ENABLED.

Never prints, logs, or persists admin password / JWT / import token.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error

CHKP = pathlib.Path(os.environ.get("CHKP_DIR") or "/app/reconcile_workspace/checkpoints")
LOGS = pathlib.Path(os.environ.get("LOGS_DIR") or "/app/reconcile_workspace/logs")
LOGS.mkdir(parents=True, exist_ok=True)

P5_TAR = CHKP / "phase5_20261003_190628Z.tar.gz"
P6_TAR = CHKP / "phase6_20261003_192150Z.tar.gz"

RECONCILED_21 = [
    "games", "historical_ingestion_state", "nfl_ingest_meta", "nfl_player_weekly",
    "parlay_history", "picks", "player_game_actuals", "player_game_logs",
    "player_identities", "prediction_snapshots", "pregame_snapshots",
    "publication_events", "rollover_slate_events", "rollover_slates",
    "settlement_events", "soccer_matches", "soccer_player_game_logs",
    "team_game_actuals", "tennis_matches_history", "user_bets", "users",
]

LOGICAL_KEYS = {
    "games":                       ("sport", "game_id"),
    "historical_ingestion_state":  ("_id",),
    "nfl_ingest_meta":             ("_id",),
    "nfl_player_weekly":           ("player_id", "season", "week"),
    "parlay_history":              ("_id",),
    "picks":                       ("id",),
    "player_game_actuals":         ("sport", "event_id", "player_id"),
    "player_game_logs":            ("sport", "game_id", "player_id"),
    "player_identities":           ("canonical_player_id",),
    "prediction_snapshots":        ("prediction_id", "snapshot_version"),
    "pregame_snapshots":           ("snapshot_hash",),
    "publication_events":          ("payload_hash",),
    "rollover_slate_events":       ("slate_date", "event", "at"),
    "rollover_slates":             ("slate_id",),
    "settlement_events":           ("settlement_id",),
    "soccer_matches":              ("league", "season", "home_team", "away_team", "date"),
    "soccer_player_game_logs":     ("match_id", "player_id"),
    "team_game_actuals":           ("sport", "event_id", "canonical_team_id"),
    "tennis_matches_history":      ("tourney_id", "winner_id", "loser_id"),
    "user_bets":                   ("id",),
    "users":                       ("id",),
}


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr); sys.exit(2)
    return v


def _redact_host(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _post(url, headers, body, timeout=300):
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


def _get(url, headers, timeout=300):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _audit_sources():
    tmp_root = pathlib.Path(os.environ.get("RECONCILE_TMP") or tempfile.gettempdir())
    tmp = tmp_root / "r3_resume_audit"
    if tmp.exists():
        import shutil; shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    for tar in (P5_TAR, P6_TAR):
        with tarfile.open(tar) as tf:
            for m in tf.getmembers():
                if m.isfile() and "/canonical/" in m.name and m.name.endswith(".ndjson"):
                    tf.extract(m, path=tmp)
    p5 = tmp / "v2_phase5" / "canonical"
    p6 = tmp / "v2_phase6" / "canonical"
    expected = {}
    for coll in RECONCILED_21:
        p5f = p5 / f"{coll}.ndjson"
        p6f = p6 / f"{coll}.ndjson"
        src = p6f if (p6f.exists() and p6f.stat().st_size > 0) else p5f
        if not src.exists(): continue
        keys = LOGICAL_KEYS[coll]
        total = excluded = quarantined = null_lk = 0
        seen = set()
        with open(src) as f:
            for ln in f:
                try: r = json.loads(ln)
                except Exception: continue
                total += 1
                d = r.get("doc", r)
                if d.get("excluded_from_canonical_runtime") is True:
                    excluded += 1; continue
                if d.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT":
                    quarantined += 1; continue
                lk = tuple(d.get(f) for f in keys)
                if any(v is None for v in lk):
                    null_lk += 1; continue
                seen.add(lk)
        expected[coll] = {
            "source_rows":               total,
            "unique_logical_identities": len(seen),
            "excluded_rows":             excluded,
            "quarantined_rows":          quarantined,
            "null_logical_key_rows":     null_lk,
            "expected_canonical_rows":   len(seen),
        }
    return expected


def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")

    print(f"[auth] POST {_redact_host(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code}", file=sys.stderr); return 10
    jwt = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    hdr_t = {**hdr_a, "X-Canonical-Import-Token": import_tok}
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    print("[preflight] GET /api/admin/data-authority/status")
    code, status = _get(f"{api_base}/api/admin/data-authority/status", hdr_a)
    if code != 200: print(f"ERROR preflight {code}", file=sys.stderr); return 11
    flags = status.get("env_flags", {})
    for k, want in {"CANONICAL_FALLBACK_MODE": True,
                     "USE_CANONICAL_DB": False,
                     "CANONICAL_IMPORT_ENABLED": True,
                     "BACKGROUND_WORKERS_ENABLED": False}.items():
        if flags.get(k) != want:
            print(f"ERROR: {k}={flags.get(k)} want {want}", file=sys.stderr); return 12
    print(f"[preflight] env_flags OK = {json.dumps(flags, sort_keys=True)}")

    print("[source-audit] parsing SHA-verified checkpoints …")
    expected = _audit_sources()
    print(f"[source-audit] {len(expected)}/21 collections audited")

    print("\n==================== PHASE 4.5 — SKIPPED (NO RESET DIRECTIVE) ====================")
    print("[phase4.5] skipped: preserving all 655 succeeded batches and current session bookkeeping")

    print("\n==================== PHASE 5 — R3 IMPORT (RESUME, SAME SESSION) ====================")
    env = {**os.environ,
            "PROD_API_BASE":              api_base,
            "PROD_ADMIN_JWT":             jwt,
            "CANONICAL_IMPORT_TOKEN":     import_tok,
            "CANONICAL_IMPORT_SESSION":   session,
            "MAX_BATCH_SIZE":             os.environ.get("MAX_BATCH_SIZE", "250"),
          }
    push_log = LOGS / f"push_canonical_r3_resume_{int(time.time())}.log"
    with open(push_log, "w") as lf:
        p = subprocess.run(
            [sys.executable, "/app/reconcile_workspace/scripts/push_canonical_to_production.py"],
            env=env, stdout=lf, stderr=subprocess.STDOUT)
    print(f"[phase5] exit={p.returncode}  log={push_log}")
    if p.returncode != 0:
        print("STOP: push driver failed — see log", file=sys.stderr); return 30

    print("\n==================== PHASE 6 — APPROVED LEGACY COPY (SAFE NO-OP) ====================")
    print("[phase6] safe no-op (no PHASE6_APPROVED_COLLECTIONS)")

    print("\n==================== PHASE 7 — CREATE UNIQUE INDEXES ====================")
    code, idx = _post(f"{api_base}/api/admin/canonical-cutover/create-indexes",
                        hdr_t, {"collections": RECONCILED_21}, timeout=600)
    if code != 200 or not isinstance(idx, dict):
        print(f"STOP: index creation failed status={code}", file=sys.stderr); return 50
    print(f"[phase7] all_ok = {idx.get('all_ok')}")
    for r in idx.get("results", []):
        tag = "✓" if r.get("ok") else "✗"
        print(f"   {tag} {r.get('collection'):35s} created={r.get('created')}  failed={r.get('failed')}")
    if not idx.get("all_ok"):
        print("STOP: Phase 7 FAIL", file=sys.stderr); return 51

    print("\n==================== PHASE 8 — R3 COMPLETENESS CERTIFICATION ====================")
    code, cert = _post(f"{api_base}/api/admin/canonical-cutover/r3-certification",
                        hdr_t, {"session_id": session, "expected": expected,
                                 "tolerance_rows": 0}, timeout=600)
    if code != 200 or not isinstance(cert, dict):
        print(f"STOP: R3 cert endpoint failed status={code}", file=sys.stderr); return 60
    print(f"[phase8] overall_pass = {cert.get('overall_pass')}")
    print(f"\n{'collection':<30} {'exp_canon':>10} {'canon':>8} "
            f"{'uniq_canon':>11} {'dup':>5} {'diff':>7} {'pct':>6}  status")
    print("-" * 100)
    for r in cert.get("collections", []):
        print(f"{r['collection']:<30} {r['expected_canonical_rows']:>10} "
              f"{r['canonical_rows']:>8} {r['unique_canonical_logical_ids']:>11} "
              f"{r['duplicate_canonical_ids']:>5} {r['difference']:>+7} "
              f"{r['difference_percent']:>6.2f}  {r['status']}")

    rpt = {
        "generated_at_unix":   int(time.time()),
        "api_target_redacted": _redact_host(api_base),
        "session_id":          session,
        "preflight_env_flags": flags,
        "source_audit":        expected,
        "phase4_5_reset":      "SKIPPED (no-reset directive)",
        "phase5_push_exit":    p.returncode,
        "phase5_push_log":     str(push_log),
        "phase7_indexes":      idx,
        "phase8_r3_cert":      cert,
    }
    rpt_path = LOGS / "phase5_8_r3_resume_report.json"
    rpt_path.write_text(json.dumps(rpt, indent=2, default=str))
    print(f"\n[report] {rpt_path}")

    if not cert.get("overall_pass"):
        print("\n❌ R3 PHASE 8 FAILED — Phase 9 NOT activated.", file=sys.stderr)
        return 70

    print("\n✅ R3 Phase 5→8 complete.  STOP here — awaiting Phase 9 approval.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
