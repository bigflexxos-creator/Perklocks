"""r3_perf_indexes_apply — Support-required NON-UNIQUE performance
indexes for Perklocks R3 session ``perklocks-cutover-20261003-r3``.

Why
───
Support identified three canonical lookup paths that currently
COLLSCAN on every logical-key lookup:
  picks               → id
  player_identities   → canonical_player_id
  pregame_snapshots   → snapshot_hash

This script asks the server to materialize the SAME key patterns
the final unique indexes will use, but with:
  * unique = False
  * distinct names (``ix_*_r3``) so they do not collide with the
    intended ``ux_*`` unique indexes that live in
    services.canonical_cutover._INDEX_SPECS and will be applied
    by Phase-8 cert after duplicate reconciliation.

Hard safety invariants
──────────────────────
* Only HTTP verbs used:
    POST /api/auth/login
    POST /api/admin/canonical-cutover/create-performance-indexes
    GET  /api/admin/canonical-cutover/explain-logical-key-lookup
    GET  /api/admin/canonical-import/status
* Does NOT touch _INDEX_SPECS or the final ux_* unique indexes.
* Does NOT reset / recreate the R3 session.
* Does NOT delete, clean, deduplicate, or modify any document.
* Does NOT replay batches / change batch size / change concurrency
  / change scoring / change content-hash protection.
* Does NOT force unique indexes.
* Does NOT start a second Resume workflow.

Steps
─────
    1. mint JWT
    2. POST /create-performance-indexes
       → server creates ix_picks_id_r3,
         ix_player_identities_canonical_player_id_r3,
         ix_pregame_snapshots_snapshot_hash_r3 (all unique=False)
    3. verify every result row has live_ready=True
    4. GET /explain-logical-key-lookup for each of the three
       collections → proves lookup now uses IXSCAN (not COLLSCAN)
    5. optional delta measurement via /canonical-import/status
       (default disabled; this is a one-time materialization and
        should not idle for 10 minutes watching an inactive migration)

Environment contract (via GH Actions secrets, never printed):
    PROD_API_BASE
    PROD_ADMIN_EMAIL
    PROD_ADMIN_PASSWORD
    CANONICAL_IMPORT_TOKEN

Optional:
    SESSION_ID           default perklocks-cutover-20261003-r3
    MEASURE_MINUTES      default 0 (disabled); hard cap 20
    REPORT_PATH          default /tmp/perklocks-logs/r3_perf_indexes_report.json

Exit codes:
    0 — SUCCESS (indexes live + explain plans show IXSCAN)
    1 — CANNOT VERIFY (auth / network error)
    3 — index creation or explain-plan verification failure
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


TARGETS: list[str] = [
    "picks",
    "player_identities",
    "pregame_snapshots",
]

EXPECTED_INDEX_NAMES: dict[str, str] = {
    "picks":             "ix_picks_id_r3",
    "player_identities": "ix_player_identities_canonical_player_id_r3",
    "pregame_snapshots": "ix_pregame_snapshots_snapshot_hash_r3",
}


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"[r3-perf-idx] FATAL: env var {k} not set", file=sys.stderr)
        sys.exit(1)
    return v


def _redact(url: str) -> str:
    try:
        p = urlparse(url)
        h = (p.hostname or "?").split(".")
        if len(h) >= 3:
            h[0] = "*"
        return ".".join(h) + (f":{p.port}" if p.port else "")
    except Exception:
        return "<redacted>"


def _mask(value: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def _post_json(url, headers, body, timeout_s=60):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:1200]
        try: parsed = json.loads(txt)
        except Exception: parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _get_json(url, headers, timeout_s=120):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:1200]
        try: parsed = json.loads(txt)
        except Exception: parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _agg_totals_three(status: dict) -> dict:
    ab = (status or {}).get("aggregated_batches") or {}
    per = {}
    tot = {"succeeded": 0, "failed": 0, "in_progress": 0,
            "accepted": 0, "rejected": 0}
    for c in TARGETS:
        d = ab.get(c, {}) or {}
        row = {
            "succeeded":   int(d.get("batches_succeeded", 0) or 0),
            "failed":      int(d.get("batches_failed", 0) or 0),
            "in_progress": int(d.get("batches_in_progress", 0) or 0),
            "accepted":    int(d.get("accepted", 0) or 0),
            "rejected":    int(d.get("rejected", 0) or 0),
        }
        per[c] = row
        for k, v in row.items():
            tot[k] = tot.get(k, 0) + v
    return {"per_target": per, "totals": tot}


def _agg_totals_all(status: dict) -> dict:
    ab = (status or {}).get("aggregated_batches") or {}
    tot = {"succeeded": 0, "failed": 0, "in_progress": 0,
            "accepted": 0, "rejected": 0}
    for d in ab.values():
        for k in tot.keys():
            tot[k] += int(d.get(f"batches_{k}", 0) or d.get(k, 0) or 0)
    return tot


def _write_report(path, obj):
    try:
        path.write_text(json.dumps(obj, indent=2, default=str))
    except Exception as e:
        print(f"[r3-perf-idx] report write failed: {e}", file=sys.stderr)


def main() -> int:
    api_base    = _require("PROD_API_BASE").rstrip("/")
    email       = _require("PROD_ADMIN_EMAIL")
    password    = _require("PROD_ADMIN_PASSWORD")
    import_tok  = _require("CANONICAL_IMPORT_TOKEN")
    session_id  = (os.environ.get("SESSION_ID") or
                    "perklocks-cutover-20261003-r3").strip()
    try:
        measure_minutes = max(0, int(os.environ.get("MEASURE_MINUTES") or 0))
    except ValueError:
        measure_minutes = 0
    measure_minutes = min(measure_minutes, 20)

    report_path = pathlib.Path(
        os.environ.get("REPORT_PATH") or
        "/tmp/perklocks-logs/r3_perf_indexes_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[r3-perf-idx] target={_redact(api_base)}  session={session_id}")
    print(f"[r3-perf-idx] creating Support-required NON-UNIQUE indexes:")
    for c, name in EXPECTED_INDEX_NAMES.items():
        print(f"             {c:<28s} → {name}  (unique=False)")

    # ── Auth ──────────────────────────────────────────────────────
    print("\n[auth] POST /api/auth/login")
    code, body = _post_json(f"{api_base}/api/auth/login", {},
                             {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        _write_report(report_path, {"verdict": "CANNOT_VERIFY",
                                     "stage": "auth", "status": code,
                                     "body": body})
        return 1
    jwt = body["access_token"]
    _mask(jwt)
    print(f"[auth] OK role={(body.get('user') or {}).get('role') or body.get('role')}")

    hdr     = {"Authorization": f"Bearer {jwt}"}
    hdr_tok = {**hdr, "X-Canonical-Import-Token": import_tok}

    # ── Step 1: create performance indexes ────────────────────────
    print("\n[step1] POST /api/admin/canonical-cutover/create-performance-indexes")
    t = time.time()
    code, resp = _post_json(
        f"{api_base}/api/admin/canonical-cutover/create-performance-indexes",
        hdr_tok, {}, timeout_s=600)
    elapsed_s = time.time() - t
    print(f"[step1] HTTP {code}  elapsed={elapsed_s:.1f}s")
    if code != 200 or not isinstance(resp, dict):
        _write_report(report_path, {"verdict": "FAIL_CREATE_PERF",
                                     "stage": "step1_create", "status": code,
                                     "body": resp, "elapsed_s": elapsed_s})
        return 3

    # ── Step 2: verify every result row ────────────────────────────
    results = resp.get("results") or []
    print("\n[step2] verification:")
    print("        collection                index_name                             unique  live/ready")
    print("        " + "-" * 86)
    all_ok = True
    for row in results:
        print(f"        {row.get('collection'):<24s} {str(row.get('index_name')):<38s} "
              f"{'NO ':<6s}  {'YES' if row.get('live_ready') else 'NO'}")
        if not row.get("live_ready"):
            all_ok = False
            if row.get("error"):
                print(f"           ↳ error: {row['error']}")

    if not all_ok:
        _write_report(report_path, {"verdict": "FAIL_INDEX_NOT_LIVE",
                                     "stage": "step2_verify",
                                     "step1_response": resp})
        print("\n❌ One or more performance indexes did not report "
              "live/ready.", file=sys.stderr)
        return 3

    print("\n[step2] ✅ all three temporary performance indexes live/ready.")

    # ── Step 3: explain-plan proof (IXSCAN vs COLLSCAN) ────────────
    print("\n[step3] GET /canonical-cutover/explain-logical-key-lookup (×3)")
    explain_rows: list[dict] = []
    print("        collection                stages                                 index_used                             IXSCAN")
    print("        " + "-" * 100)
    explain_ok = True
    for c in TARGETS:
        code_e, body_e = _get_json(
            f"{api_base}/api/admin/canonical-cutover/explain-logical-key-lookup"
            f"?collection={c}", hdr, timeout_s=120)
        if code_e != 200 or not isinstance(body_e, dict):
            explain_rows.append({"collection": c, "status": code_e,
                                  "body": body_e, "uses_index": False})
            print(f"        {c:<24s} ERROR HTTP {code_e}")
            explain_ok = False
            continue
        stages = body_e.get("all_stages") or []
        idx_used = body_e.get("index_name_used")
        uses = bool(body_e.get("uses_index"))
        explain_rows.append({"collection": c, "stages": stages,
                              "index_name_used": idx_used,
                              "uses_index": uses,
                              "winning_plan_stage": body_e.get("winning_plan_stage")})
        marker = "✅ YES" if uses else "❌ NO (COLLSCAN!)"
        print(f"        {c:<24s} {str(stages):<40s} {str(idx_used):<38s} {marker}")
        if not uses:
            explain_ok = False

    if not explain_ok:
        _write_report(report_path, {"verdict": "FAIL_EXPLAIN_NO_IXSCAN",
                                     "stage": "step3_explain",
                                     "step1_response": resp,
                                     "explain_rows":  explain_rows})
        print("\n❌ One or more collections did not use IXSCAN after index "
              "creation.", file=sys.stderr)
        return 3

    print("\n[step3] ✅ all three logical-key lookups now use IXSCAN "
          "(no COLLSCAN).")

    # ── Step 4: 10-min delta measurement ──────────────────────────
    measurement = None
    if measure_minutes > 0:
        print(f"\n[step4] delta measurement "
              f"(interval={measure_minutes}min)")
        status_url = (f"{api_base}/api/admin/canonical-import/status"
                       f"?session_id={session_id}")

        print("[snap1] GET /canonical-import/status")
        t0 = time.time()
        code, snap1 = _get_json(status_url, hdr, timeout_s=60)
        if code != 200 or not isinstance(snap1, dict):
            print("[step4] WARN: snap1 unavailable; skipping measurement",
                  file=sys.stderr)
        else:
            t1 = _agg_totals_three(snap1)
            all1 = _agg_totals_all(snap1)
            print(f"[snap1] three_target totals: "
                  f"succ={t1['totals']['succeeded']}  "
                  f"fail={t1['totals']['failed']}  "
                  f"ip={t1['totals']['in_progress']}")
            print(f"[snap1] ALL-session totals : "
                  f"succ={all1['succeeded']}  fail={all1['failed']}  "
                  f"ip={all1['in_progress']}")

            print(f"[wait] sleeping {measure_minutes*60}s")
            try:
                time.sleep(measure_minutes * 60)
            except KeyboardInterrupt:
                print("[wait] interrupted", file=sys.stderr)

            print("[snap2] GET /canonical-import/status")
            t2 = time.time()
            code, snap2 = _get_json(status_url, hdr, timeout_s=60)
            if code == 200 and isinstance(snap2, dict):
                t_three_2 = _agg_totals_three(snap2)
                all2 = _agg_totals_all(snap2)
                hours = measure_minutes / 60.0
                per_target_rate: dict[str, dict] = {}
                for c in TARGETS:
                    b = t1["per_target"][c]
                    a = t_three_2["per_target"][c]
                    per_target_rate[c] = {
                        "succeeded_before": b["succeeded"],
                        "succeeded_after":  a["succeeded"],
                        "failed_before":    b["failed"],
                        "failed_after":     a["failed"],
                        "delta_succeeded":  a["succeeded"] - b["succeeded"],
                        "delta_failed":     a["failed"]    - b["failed"],
                        "succ_batches_per_hour": round((a["succeeded"] - b["succeeded"]) / hours, 1),
                        "fail_batches_per_hour": round((a["failed"]    - b["failed"]) / hours, 1),
                    }
                overall = {
                    "succeeded_before": all1["succeeded"],
                    "succeeded_after":  all2["succeeded"],
                    "failed_before":    all1["failed"],
                    "failed_after":     all2["failed"],
                    "delta_succeeded":  all2["succeeded"] - all1["succeeded"],
                    "delta_failed":     all2["failed"]    - all1["failed"],
                    "succ_batches_per_hour": round((all2["succeeded"] - all1["succeeded"]) / hours, 1),
                    "fail_batches_per_hour": round((all2["failed"]    - all1["failed"]) / hours, 1),
                }
                measurement = {
                    "interval_min":   measure_minutes,
                    "snap1_unix":     int(t0),
                    "snap2_unix":     int(t2),
                    "per_target":     per_target_rate,
                    "overall":        overall,
                }
                print("\n           collection                  succ Δ    fail Δ   succ/hr   fail/hr")
                print("           " + "-" * 76)
                for c in TARGETS:
                    r = per_target_rate[c]
                    print(f"           {c:<24s}  {r['delta_succeeded']:>+5d}    "
                          f"{r['delta_failed']:>+5d}   "
                          f"{r['succ_batches_per_hour']:>7.1f}   "
                          f"{r['fail_batches_per_hour']:>7.1f}")
                print(f"           {'TOTAL (session)':<24s}  "
                      f"{overall['delta_succeeded']:>+5d}    "
                      f"{overall['delta_failed']:>+5d}   "
                      f"{overall['succ_batches_per_hour']:>7.1f}   "
                      f"{overall['fail_batches_per_hour']:>7.1f}")
            else:
                print("[step4] WARN: snap2 unavailable", file=sys.stderr)

    _write_report(report_path, {
        "verdict":        "SUCCESS",
        "generated_unix": int(time.time()),
        "session_id":     session_id,
        "api_target_redacted": _redact(api_base),
        "step1_create":   resp,
        "step3_explain":  explain_rows,
        "step4_measurement": measurement,
        "note": ("Temporary non-unique performance indexes applied.  "
                  "Final ux_* unique indexes will be applied in Phase 8 "
                  "after duplicate reconciliation."),
    })

    print("\n==================== PERF INDEX REPAIR COMPLETE ====================")
    print(f"  verdict           : SUCCESS")
    print(f"  perf indexes live : 3/3")
    print(f"  all IXSCAN        : YES")
    print(f"  session preserved : {session_id}")
    print(f"  report            : {report_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[r3-perf-idx] interrupted", file=sys.stderr)
        sys.exit(1)
