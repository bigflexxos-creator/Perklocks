"""run_phase5_through_8 — orchestrator that:
 1. Authenticates against Production via POST /api/auth/login
 2. Preflights /api/admin/data-authority/status and verifies required env flags
 3. Runs the push driver (Phase 5)
 4. Executes Phase 6 approved out-of-scope legacy copies
 5. Creates canonical indexes (Phase 7)
 6. Runs certification (Phase 8)

Credentials consumed via env vars only, never printed/logged/persisted:
  PROD_API_BASE
  PROD_ADMIN_EMAIL
  PROD_ADMIN_PASSWORD
  CANONICAL_IMPORT_TOKEN
  CANONICAL_IMPORT_SESSION

Reports written to:
  /app/reconcile_workspace/logs/phase5_8_report.json  (no secrets)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import urllib.error

CHKP   = pathlib.Path("/app/reconcile_workspace/checkpoints")
LOGS   = pathlib.Path("/app/reconcile_workspace/logs")
LOGS.mkdir(parents=True, exist_ok=True)

RECONCILED_21 = [
    "games", "historical_ingestion_state", "nfl_ingest_meta", "nfl_player_weekly",
    "parlay_history", "picks", "player_game_actuals", "player_game_logs",
    "player_identities", "prediction_snapshots", "pregame_snapshots",
    "publication_events", "rollover_slate_events", "rollover_slates",
    "settlement_events", "soccer_matches", "soccer_player_game_logs",
    "team_game_actuals", "tennis_matches_history", "user_bets", "users",
]


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


def _post_json(url: str, headers: dict, body: dict, timeout: int = 60) -> tuple[int, dict | str]:
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try: return resp.status, json.loads(resp.read().decode())
            except Exception: return resp.status, "<unparseable>"
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _get_json(url: str, headers: dict, timeout: int = 60) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try: return resp.status, json.loads(resp.read().decode())
            except Exception: return resp.status, "<unparseable>"
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

    print(f"[auth] POST {_redact_host(api_base)}/api/auth/login (admin creds in-memory, not logged)")
    code, body = _post_json(f"{api_base}/api/auth/login",
                             {}, {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"ERROR: login failed status={code} body={body if isinstance(body, str) else json.dumps(body)[:300]}", file=sys.stderr)
        return 10
    jwt = body["access_token"]
    role = body.get("user", {}).get("role") or body.get("role") or "<unknown>"
    print(f"[auth] login OK   role={role}   jwt_len={len(jwt)}")
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    hdr_t = {**hdr_a, "X-Canonical-Import-Token": import_tok}

    # ── Preflight ────────────────────────────────────────────────────
    print(f"[preflight] GET /api/admin/data-authority/status")
    code, status = _get_json(f"{api_base}/api/admin/data-authority/status", hdr_a)
    if code != 200 or not isinstance(status, dict):
        print(f"ERROR: data-authority status failed code={code} body={status}", file=sys.stderr)
        return 11
    flags = status.get("env_flags", {})
    required = {
        "CANONICAL_FALLBACK_MODE":    True,
        "USE_CANONICAL_DB":           False,
        "CANONICAL_IMPORT_ENABLED":   True,
        "BACKGROUND_WORKERS_ENABLED": False,
    }
    print(f"[preflight] env_flags = {json.dumps(flags, sort_keys=True)}")
    print(f"[preflight] active_db_name = {status.get('active_db_name')}")
    print(f"[preflight] legacy_fp  = {status.get('legacy_fingerprint')}")
    print(f"[preflight] canon_fp   = {status.get('canonical_fingerprint')}")
    legacy_counts_before = status.get("legacy_summary", {}).get("collections", {})
    canonical_counts_before = status.get("canonical_summary", {}).get("collections", {})

    bad = {k: (flags.get(k), v) for k, v in required.items() if flags.get(k) != v}
    if bad:
        print("ERROR: preflight failed — env flags not in required state:", file=sys.stderr)
        for k, (actual, want) in bad.items():
            print(f"  {k}: got={actual}  want={want}", file=sys.stderr)
        return 12
    print("[preflight] ✓ all required env flags in expected state")

    # ── Phase 5 — invoke push driver ─────────────────────────────────
    print("\n==================== PHASE 5 — IMPORT 21 COLLECTIONS ====================")
    env = {**os.environ,
            "PROD_API_BASE":          api_base,
            "PROD_ADMIN_JWT":         jwt,
            "CANONICAL_IMPORT_TOKEN": import_tok,
            "CANONICAL_IMPORT_SESSION": session,
            "MAX_BATCH_SIZE":         os.environ.get("MAX_BATCH_SIZE", "2000"),
          }
    push_log = LOGS / f"push_canonical_phase5_{int(time.time())}.log"
    with open(push_log, "w") as lf:
        proc = subprocess.run(
            [sys.executable, "/app/reconcile_workspace/scripts/push_canonical_to_production.py"],
            env=env, stdout=lf, stderr=subprocess.STDOUT)
    print(f"[phase5] push driver exit={proc.returncode}, log={push_log}")
    # Last 60 lines (sanitised — no creds printed by the driver)
    try:
        tail = push_log.read_text().splitlines()[-80:]
        print("\n----- phase5 push driver tail -----")
        for ln in tail: print(ln)
        print("----- /tail -----\n")
    except Exception as e:
        print(f"could not read tail: {e}")
    if proc.returncode != 0:
        print("STOP: Phase 5 push driver failed; see log.", file=sys.stderr)
        return 20

    # ── Phase 5 verification via status endpoint ─────────────────────
    code, s2 = _get_json(f"{api_base}/api/admin/canonical-import/status?session_id={session}", hdr_a)
    if code != 200 or not isinstance(s2, dict):
        print(f"STOP: canonical-import/status failed code={code}", file=sys.stderr)
        return 21
    agg = s2.get("aggregated_batches", {})
    curr = s2.get("current_canonical", {})
    print("[phase5] session aggregated batches:")
    for c, v in agg.items():
        print(f"   {c:35s} batches={v['batches']:>3}  accepted={v['accepted']:>7}  rejected={v['rejected']:>5}")
    print("[phase5] current canonical counts:")
    for c in RECONCILED_21:
        n = curr.get(c, {}).get("canonical_count", "?")
        print(f"   {c:35s} count={n}")

    # ── Phase 6 — approved out-of-scope legacy copies ────────────────
    print("\n==================== PHASE 6 — COPY APPROVED LEGACY COLLECTIONS ====================")
    # Discover approved out-of-scope list from the push report / env override
    #   Spec: Phase 6 must only copy "approved" out-of-scope legacy collections.
    #   Approved list is sourced from env PHASE6_APPROVED_COLLECTIONS (comma-sep)
    #   or defaults to [] (safe no-op if not supplied).
    approved_csv = os.environ.get("PHASE6_APPROVED_COLLECTIONS", "").strip()
    approved = [c.strip() for c in approved_csv.split(",") if c.strip()]
    phase6_results = []
    if not approved:
        print("[phase6] no approved out-of-scope collections supplied via "
              "PHASE6_APPROVED_COLLECTIONS; skipping (safe no-op)")
    else:
        for coll in approved:
            print(f"[phase6] copy legacy → canonical: {coll}")
            code, body = _post_json(
                f"{api_base}/api/admin/canonical-cutover/copy-legacy-collection",
                hdr_t, {"collection": coll, "confirm": True})
            print(f"   status={code} body={body if isinstance(body, str) else json.dumps(body)[:300]}")
            phase6_results.append({"collection": coll, "code": code, "body": body})
            if code != 200:
                print(f"STOP: Phase 6 copy failed for {coll}", file=sys.stderr)
                return 30

    # ── Phase 7 — create canonical indexes ───────────────────────────
    print("\n==================== PHASE 7 — CREATE CANONICAL INDEXES ====================")
    code, idx = _post_json(
        f"{api_base}/api/admin/canonical-cutover/create-indexes",
        hdr_t, {"collections": RECONCILED_21}, timeout=240)
    if code != 200 or not isinstance(idx, dict):
        print(f"STOP: index creation failed code={code} body={idx}", file=sys.stderr)
        return 40
    print(f"[phase7] all_ok = {idx.get('all_ok')}")
    for r in idx.get("results", []):
        if r.get("ok", False):
            print(f"   ✓ {r.get('collection'):35s} indexes={r.get('indexes_ok', [])}")
        else:
            print(f"   ✗ {r.get('collection'):35s} error={r.get('error')}")
    if not idx.get("all_ok"):
        print("STOP: Phase 7 indexes failed (uniqueness or integrity).", file=sys.stderr)
        return 41

    # ── Phase 8 — pre-cutover certification ──────────────────────────
    print("\n==================== PHASE 8 — PRE-CUTOVER CERTIFICATION ====================")
    code, cert = _get_json(f"{api_base}/api/admin/canonical-cutover/certification",
                             hdr_a, timeout=240)
    if code != 200 or not isinstance(cert, dict):
        print(f"STOP: certification failed code={code} body={cert}", file=sys.stderr)
        return 50

    # Post-import legacy integrity check: fingerprints must match pre-phase5
    code, status_after = _get_json(f"{api_base}/api/admin/data-authority/status", hdr_a)
    legacy_counts_after = status_after.get("legacy_summary", {}).get("collections", {}) if isinstance(status_after, dict) else {}
    canonical_counts_after = status_after.get("canonical_summary", {}).get("collections", {}) if isinstance(status_after, dict) else {}
    legacy_fp_before = status.get("legacy_fingerprint")
    legacy_fp_after  = status_after.get("legacy_fingerprint") if isinstance(status_after, dict) else None
    legacy_intact = (legacy_fp_before == legacy_fp_after) and (legacy_counts_before == legacy_counts_after)

    print(f"[phase8] overall_pass                  = {cert.get('overall_pass')}")
    print(f"[phase8] duplicate_logical_identities  = {cert.get('duplicate_logical_identities_total')}")
    print(f"[phase8] excluded/quarantined leaked   = {cert.get('excluded_or_quarantined_leaked_total')}")
    print(f"[phase8] import_endpoint_disabled      = {cert.get('import_endpoint_disabled')}")
    print(f"[phase8] legacy_fp_before={legacy_fp_before}  legacy_fp_after={legacy_fp_after}")
    print(f"[phase8] legacy_intact                 = {legacy_intact}")

    report = {
        "generated_at_unix":      int(time.time()),
        "api_target_redacted":    _redact_host(api_base),
        "session_id":             session,
        "preflight_env_flags":    flags,
        "legacy_fingerprint_before":   legacy_fp_before,
        "canonical_fingerprint_before": status.get("canonical_fingerprint"),
        "legacy_fingerprint_after":    legacy_fp_after,
        "canonical_fingerprint_after": status_after.get("canonical_fingerprint") if isinstance(status_after, dict) else None,
        "legacy_counts_before":        legacy_counts_before,
        "legacy_counts_after":         legacy_counts_after,
        "canonical_counts_before":     canonical_counts_before,
        "canonical_counts_after":      canonical_counts_after,
        "legacy_intact":               legacy_intact,
        "phase5_status":          s2,
        "phase6_results":         phase6_results,
        "phase7_indexes":         idx,
        "phase8_certification":   cert,
        "safety_gates": {
            "duplicates_zero":           cert.get("duplicate_logical_identities_total") == 0,
            "excluded_leaked_zero":      cert.get("excluded_or_quarantined_leaked_total") == 0,
            "legacy_intact":             legacy_intact,
            "overall_pass":              bool(cert.get("overall_pass")),
            "indexes_all_ok":            bool(idx.get("all_ok")),
        },
    }
    report_path = LOGS / "phase5_8_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[report] {report_path}")

    gates = report["safety_gates"]
    all_pass = all(gates.values())
    if not all_pass:
        print(f"\n❌ SAFETY GATES FAILED: {gates}", file=sys.stderr)
        return 60
    print("\n✅ Phase 5 → 8 complete. All safety gates PASS. "
          "STOP here — awaiting user approval before Phase 9 flip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
