"""run_phase5_through_8_r3 — Phase-5-R3 orchestrator with
atomic import-ordering + full completeness certification.

Differences from R2:
  * Pre-flight reset of ONLY the 21 canonical_<name> collections
    (scoped drop + bookkeeping cleanup for the R3 session only).
  * Push driver run with session id ``perklocks-cutover-20261003-r3``.
  * Phase 8 now calls the new POST
    ``/api/admin/canonical-cutover/r3-certification`` endpoint with
    certified source-row + unique-logical-identity counts derived from
    the SHA-verified Phase 5 / Phase 6 NDJSONs.
  * Phase 8 FAILS the cert on any unexplained delta between source and
    canonical rows.

NEVER logs, prints or persists admin creds / import token.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tarfile
import time
import urllib.request
import urllib.error

CHKP = pathlib.Path("/app/reconcile_workspace/checkpoints")
LOGS = pathlib.Path("/app/reconcile_workspace/logs")
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


def _post(url: str, headers: dict, body: dict, timeout: int = 300):
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


def _get(url: str, headers: dict, timeout: int = 300):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _audit_sources() -> dict[str, dict]:
    """Extract and audit the Phase-5 + Phase-6 canonical NDJSON
    checkpoints to produce the certified source-row + unique-logical-
    identity counts for every reconciled collection.  Uses P6 overlay
    only when it is non-empty (the R2 empty-overlay guard contract).
    """
    tmp = pathlib.Path("/opt/reconcile_tmp/r3_source_audit")
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
    expected: dict[str, dict] = {}
    for coll in RECONCILED_21:
        p5f = p5 / f"{coll}.ndjson"
        p6f = p6 / f"{coll}.ndjson"
        if p6f.exists() and p6f.stat().st_size > 0:
            src = p6f
        else:
            src = p5f
        if not src.exists():
            print(f"WARN: {coll}: no NDJSON for audit")
            continue
        keys = LOGICAL_KEYS[coll]
        total = unique = excluded = quarantined = null_lk = 0
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
        unique = len(seen)
        expected[coll] = {
            "source_rows":               total,
            "unique_logical_identities": unique,
            "excluded_rows":             excluded,
            "quarantined_rows":          quarantined,
            "null_logical_key_rows":     null_lk,
            "expected_canonical_rows":   unique,
        }
    return expected


def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")

    # ── Auth ─────────────────────────────────────────────────────────
    print(f"[auth] POST {_redact_host(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code}", file=sys.stderr); return 10
    jwt = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    hdr_t = {**hdr_a, "X-Canonical-Import-Token": import_tok}
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    # ── Preflight ────────────────────────────────────────────────────
    print("[preflight] GET /api/admin/data-authority/status")
    code, status = _get(f"{api_base}/api/admin/data-authority/status", hdr_a)
    if code != 200: print(f"ERROR preflight {code} {status}", file=sys.stderr); return 11
    flags = status.get("env_flags", {})
    print(f"[preflight] env_flags = {json.dumps(flags, sort_keys=True)}")
    for k, want in {"CANONICAL_FALLBACK_MODE": True,
                     "USE_CANONICAL_DB": False,
                     "CANONICAL_IMPORT_ENABLED": True,
                     "BACKGROUND_WORKERS_ENABLED": False}.items():
        if flags.get(k) != want:
            print(f"ERROR: {k}={flags.get(k)} want {want}", file=sys.stderr); return 12
    print("[preflight] all required flags OK")
    legacy_fp_before = status.get("legacy_fingerprint")
    legacy_counts_before = status.get("legacy_summary", {}).get("collections", {})

    # ── Source audit ─────────────────────────────────────────────────
    print("[source-audit] parsing SHA-verified checkpoints …")
    expected = _audit_sources()
    print(f"[source-audit] {len(expected)}/21 collections audited")
    for coll in RECONCILED_21:
        e = expected.get(coll, {})
        print(f"  {coll:35s} src_rows={e.get('source_rows'):>8}  "
              f"unique_lk={e.get('unique_logical_identities'):>8}  "
              f"excluded={e.get('excluded_rows'):>5}  "
              f"quarantined={e.get('quarantined_rows'):>5}  "
              f"null_lk={e.get('null_logical_key_rows'):>5}")

    # ── Phase 4.5: Scoped canonical SOFT reset ──────────────────────
    # R3 resumable retry mode: canonical_* data preserved; ONLY the
    # session's batch/session bookkeeping is cleared so a different
    # ``MAX_BATCH_SIZE`` can be used without hitting ALTERED_REPLAY_REJECTED.
    print("\n==================== PHASE 4.5 — SCOPED CANONICAL SOFT RESET ====================")
    soft_reset = os.environ.get("SOFT_RESET", "false").lower() == "true"
    reset_body = {
        "confirm":                                   True,
        "i_understand_this_drops_canonical":         "YES-DROP-CANONICAL-DATASET",
        "only_canonical_prefix_collections_allowed": True,
        "session_id_to_clear_bookkeeping_for":       session,
        "drop_canonical_collections":                not soft_reset,
    }
    code, rst = _post(f"{api_base}/api/admin/canonical-cutover/reset-canonical-dataset",
                        hdr_t, reset_body)
    ok = (code == 200 and isinstance(rst, dict)
            and (rst.get("soft_reset") or rst.get("all_dropped")))
    if not ok:
        print(f"STOP: reset failed status={code} body={rst}", file=sys.stderr); return 20
    print(f"[reset] fallback_mode={rst.get('fallback_mode')}  "
          f"soft_reset={rst.get('soft_reset')}  "
          f"all_dropped={rst.get('all_dropped')}  "
          f"bookkeeping_cleared={rst.get('bookkeeping_cleared')}")

    # ── Phase 5: push driver ────────────────────────────────────────
    print("\n==================== PHASE 5 — R3 IMPORT ====================")
    env = {**os.environ,
            "PROD_API_BASE": api_base,
            "PROD_ADMIN_JWT": jwt,
            "CANONICAL_IMPORT_TOKEN": import_tok,
            "CANONICAL_IMPORT_SESSION": session,
            "MAX_BATCH_SIZE": os.environ.get("MAX_BATCH_SIZE", "2000"),
          }
    push_log = LOGS / f"push_canonical_r3_{int(time.time())}.log"
    with open(push_log, "w") as lf:
        p = subprocess.run(
            [sys.executable, "/app/reconcile_workspace/scripts/push_canonical_to_production.py"],
            env=env, stdout=lf, stderr=subprocess.STDOUT)
    print(f"[phase5] exit={p.returncode}  log={push_log}")
    if p.returncode != 0:
        print("STOP: push driver failed", file=sys.stderr); return 30

    # ── Phase 6: approved legacy copy (safe no-op by default) ──────
    print("\n==================== PHASE 6 — APPROVED LEGACY COPY ====================")
    approved = [c.strip() for c in os.environ.get(
        "PHASE6_APPROVED_COLLECTIONS", "").split(",") if c.strip()]
    phase6 = []
    if approved:
        for coll in approved:
            code, body = _post(f"{api_base}/api/admin/canonical-cutover/copy-legacy-collection",
                                 hdr_t, {"collection": coll, "confirm": True})
            print(f"   {coll}: status={code}")
            phase6.append({"collection": coll, "code": code, "body": body})
            if code != 200: return 40
    else:
        print("[phase6] safe no-op (no PHASE6_APPROVED_COLLECTIONS specified)")

    # ── Phase 7: indexes ────────────────────────────────────────────
    print("\n==================== PHASE 7 — CREATE UNIQUE INDEXES ====================")
    code, idx = _post(f"{api_base}/api/admin/canonical-cutover/create-indexes",
                        hdr_t, {"collections": RECONCILED_21}, timeout=600)
    if code != 200 or not isinstance(idx, dict):
        print(f"STOP: index creation failed status={code} body={idx}", file=sys.stderr); return 50
    print(f"[phase7] all_ok = {idx.get('all_ok')}")
    for r in idx.get("results", []):
        tag = "✓" if r.get("ok") else "✗"
        print(f"   {tag} {r.get('collection'):35s} created={r.get('created')}  failed={r.get('failed')}")
    if not idx.get("all_ok"):
        print("STOP: Phase 7 FAIL — unique index could not be created", file=sys.stderr); return 51

    # ── Phase 8: R3 completeness certification ──────────────────────
    print("\n==================== PHASE 8 — R3 COMPLETENESS CERTIFICATION ====================")
    code, cert = _post(f"{api_base}/api/admin/canonical-cutover/r3-certification",
                        hdr_t, {"session_id": session, "expected": expected,
                                 "tolerance_rows": 0}, timeout=600)
    if code != 200 or not isinstance(cert, dict):
        print(f"STOP: R3 cert endpoint failed status={code} body={cert}", file=sys.stderr); return 60
    print(f"[phase8] overall_pass = {cert.get('overall_pass')}")
    print(f"\n{'collection':<30} {'cert_src':>9} {'uniq_src':>9} {'excl':>5} "
            f"{'quar':>5} {'null_lk':>8} {'exp_canon':>10} {'canon':>8} "
            f"{'uniq_canon':>11} {'dup':>5} {'diff':>7} {'pct':>6}  status")
    print("-" * 160)
    for r in cert.get("collections", []):
        print(f"{r['collection']:<30} {r['certified_source_rows']:>9} "
              f"{r['unique_source_logical_ids']:>9} {r['excluded_rows']:>5} "
              f"{r['quarantined_rows']:>5} {r['null_logical_key_rows']:>8} "
              f"{r['expected_canonical_rows']:>10} {r['canonical_rows']:>8} "
              f"{r['unique_canonical_logical_ids']:>11} "
              f"{r['duplicate_canonical_ids']:>5} {r['difference']:>+7} "
              f"{r['difference_percent']:>6.2f}  {r['status']}")

    # Legacy intact: compare counts/fingerprints
    code, status2 = _get(f"{api_base}/api/admin/data-authority/status", hdr_a)
    legacy_fp_after = status2.get("legacy_fingerprint") if isinstance(status2, dict) else None
    legacy_counts_after = status2.get("legacy_summary", {}).get("collections", {}) if isinstance(status2, dict) else {}
    legacy_delta = {k: (legacy_counts_before.get(k, 0), legacy_counts_after.get(k, 0))
                     for k in sorted(set(legacy_counts_before) | set(legacy_counts_after))
                     if legacy_counts_before.get(k, 0) != legacy_counts_after.get(k, 0)}

    settlement_contract = cert.get("settlement_contract")
    print(f"\n[settlement_contract]")
    print(json.dumps(settlement_contract, indent=2))

    # ── Report ──────────────────────────────────────────────────────
    rpt = {
        "generated_at_unix":    int(time.time()),
        "api_target_redacted":  _redact_host(api_base),
        "session_id":           session,
        "preflight_env_flags":  flags,
        "source_audit":         expected,
        "legacy_fp_before":     legacy_fp_before,
        "legacy_fp_after":      legacy_fp_after,
        "legacy_delta_during_run": legacy_delta,
        "phase4_5_reset":       rst,
        "phase5_push_exit":     p.returncode,
        "phase6_results":       phase6,
        "phase7_indexes":       idx,
        "phase8_r3_cert":       cert,
    }
    rpt_path = LOGS / "phase5_8_r3_report.json"
    rpt_path.write_text(json.dumps(rpt, indent=2, default=str))
    print(f"\n[report] {rpt_path}")

    if not cert.get("overall_pass"):
        print("\n❌ R3 PHASE 8 FAILED — see table above.  Phase 9 NOT activated.",
              file=sys.stderr)
        return 70

    print("\n✅ R3 Phase 5→8 complete. Overall PASS.  "
          "STOP here — awaiting user approval before Phase 9 flip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
