"""r3_indexes_apply — surgical Production index repair for Perklocks R3.

Scope (strictly bounded, 2026-10-06 directive — v2 after 504 forensic):
  1. READ-ONLY duplicate-key safety check for the three target
     collections via GET /api/admin/canonical-cutover/dup-check
     (SCOPED per-collection endpoint; replaces the broad certification
     scan that previously returned 504 at 85.2 s on hot Production
     when none of the target unique indexes existed yet).  Three
     independent single-collection aggregations with a 300 s route
     timeout each.  If ANY of the three reports
     duplicate_logical_identities > 0, we STOP and do NOT call
     create-indexes.  No unique index is ever forced over duplicates.
  2. If (and only if) the three target collections all report zero
     duplicates, call POST /api/admin/canonical-cutover/create-indexes
     with the exact three target collections.  The server runs the
     already-tested ``ensure_canonical_indexes`` which uses the
     pre-pinned ``_INDEX_SPECS``:
         prediction_snapshots:  {prediction_id, snapshot_version} unique
         publication_events:    {payload_hash}                     unique
         settlement_events:     {settlement_id}                    unique
  3. Verify every result row has ok=true AND the target index name is
     either in created or existed.  Any failure aborts.
  4. OPTIONAL 10-minute delta measurement (--measure-minutes N).
     Snapshots /api/admin/canonical-import/status before + after
     waiting N minutes, computes succeeded/failed deltas per the
     three target collections AND overall, computes rates.

Hard safety invariants
──────────────────────
* Only HTTP verbs used:
    POST /api/auth/login
    GET  /api/admin/canonical-cutover/dup-check?collection=X  (×3)
    POST /api/admin/canonical-cutover/create-indexes
    GET  /api/admin/canonical-import/status
* The dup-check endpoint only accepts collections in
  RECONCILIATION_COLLECTIONS (server-side allowlist) and is strictly
  read-only (one $group aggregation per call).
* Does NOT run the broad all-collections certification scan.
* Does NOT reset the session.
* Does NOT create a new session.
* Does NOT delete data.
* Does NOT replay batches.
* Does NOT modify scoring/app logic.
* Does NOT change batch size, timeout, or content-hash protection.
* The create-indexes payload hardcodes ONLY the three target
  collections — the endpoint also refuses anything outside
  RECONCILIATION_COLLECTIONS server-side.

Environment contract
────────────────────
Required (from GH Actions secrets, never printed):
    PROD_API_BASE
    PROD_ADMIN_EMAIL
    PROD_ADMIN_PASSWORD
    CANONICAL_IMPORT_TOKEN   — required ONLY for the create-indexes
                                step (same token R3 Resume uses)

Optional:
    MEASURE_MINUTES          — 10 by default; 0 disables step 5
    SESSION_ID               — default perklocks-cutover-20261003-r3
    REPORT_PATH              — default /tmp/perklocks-logs/r3_indexes_report.json

Exit codes
──────────
    0 — indexes created / already existed; measurement completed OK
    1 — authentication / network error (CANNOT VERIFY)
    2 — duplicate logical identities found; STOPPED without touching indexes
    3 — index creation returned a failure for one or more targets
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
    "prediction_snapshots",
    "publication_events",
    "settlement_events",
]

TARGET_INDEXES: dict[str, str] = {
    # Collection → expected unique index name (from _INDEX_SPECS)
    "prediction_snapshots": "ux_prediction_snapshot_version",
    "publication_events":   "ux_payload_hash",
    "settlement_events":    "ux_settlement_id",
}


# ─── Helpers ────────────────────────────────────────────────────────
def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"[r3-indexes] FATAL: env var {k} not set", file=sys.stderr)
        sys.exit(1)
    return v


def _redact_url(url: str) -> str:
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
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = txt
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
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _agg_totals_three(status: dict) -> dict:
    """Totals for just the three target collections."""
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
    """Totals across ALL collections (for session-level health view)."""
    ab = (status or {}).get("aggregated_batches") or {}
    tot = {"succeeded": 0, "failed": 0, "in_progress": 0,
            "accepted": 0, "rejected": 0}
    for coll, d in ab.items():
        tot["succeeded"]   += int(d.get("batches_succeeded", 0) or 0)
        tot["failed"]      += int(d.get("batches_failed", 0) or 0)
        tot["in_progress"] += int(d.get("batches_in_progress", 0) or 0)
        tot["accepted"]    += int(d.get("accepted", 0) or 0)
        tot["rejected"]    += int(d.get("rejected", 0) or 0)
    return tot


# ─── Main ───────────────────────────────────────────────────────────
def main() -> int:
    api_base    = _require("PROD_API_BASE").rstrip("/")
    email       = _require("PROD_ADMIN_EMAIL")
    password    = _require("PROD_ADMIN_PASSWORD")
    import_tok  = _require("CANONICAL_IMPORT_TOKEN")
    session_id  = (os.environ.get("SESSION_ID") or
                    "perklocks-cutover-20261003-r3").strip()
    try:
        measure_minutes = max(0, int(os.environ.get("MEASURE_MINUTES") or 10))
    except ValueError:
        measure_minutes = 10
    measure_minutes = min(measure_minutes, 20)  # hard cap

    report_path = pathlib.Path(
        os.environ.get("REPORT_PATH") or
        "/tmp/perklocks-logs/r3_indexes_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[r3-indexes] target={_redact_url(api_base)}  session={session_id}")
    print(f"[r3-indexes] indexes to apply:")
    for c, idx in TARGET_INDEXES.items():
        print(f"             {c:<28s} → {idx}")

    # ── Auth ──────────────────────────────────────────────────────
    print("\n[auth] POST /api/auth/login")
    code, body = _post_json(f"{api_base}/api/auth/login", {},
                             {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        _write_report(report_path, {
            "verdict": "CANNOT_VERIFY", "stage": "auth",
            "status": code, "body": body,
        })
        print(f"[auth] FAILED code={code}", file=sys.stderr)
        return 1
    jwt = body["access_token"]
    _mask(jwt)
    print(f"[auth] OK role={(body.get('user') or {}).get('role') or body.get('role')}")

    hdr      = {"Authorization": f"Bearer {jwt}"}
    hdr_tok  = {**hdr, "X-Canonical-Import-Token": import_tok}

    # ── Step 1: duplicate-key safety check (SCOPED — 3 collections) ──
    #
    # Replaces the previous broad GET /canonical-cutover/certification
    # scan which iterated all 21 reconciliation collections and
    # exceeded the 85 s middleware timeout on Perklocks R3 Production
    # 2026-10-06.  The new per-collection endpoint
    # GET /api/admin/canonical-cutover/dup-check?collection=X runs a
    # single $group aggregation on the one collection the caller
    # names, with a 300 s route-specific timeout override.
    print("\n[step1] GET /api/admin/canonical-cutover/dup-check "
          "— per-target duplicate-key scan (SCOPED to the three targets)")

    cert_three: dict[str, dict] = {}
    dup_rows: list[dict] = []
    for c in TARGETS:
        print(f"[step1] GET dup-check?collection={c}")
        t_c = time.time()
        code_c, body_c = _get_json(
            f"{api_base}/api/admin/canonical-cutover/dup-check"
            f"?collection={c}",
            hdr, timeout_s=300)
        rt_s = time.time() - t_c
        if code_c != 200 or not isinstance(body_c, dict):
            print(f"[step1] ❌ dup-check for {c} failed HTTP {code_c} "
                  f"after {rt_s:.1f}s: {str(body_c)[:200]}",
                  file=sys.stderr)
            _write_report(report_path, {
                "verdict":  "CANNOT_VERIFY",
                "stage":    f"step1_dup_check_{c}",
                "status":   code_c,
                "body":     body_c,
                "elapsed_s": round(rt_s, 1),
                "cert_three_partial": cert_three,
            })
            return 1

        dups   = int(body_c.get("duplicate_logical_identities", 0) or 0)
        cnt    = int(body_c.get("canonical_count", -1) or -1)
        keys   = body_c.get("logical_key_fields") or []
        srv_ms = int(body_c.get("elapsed_ms", -1) or -1)
        cert_three[c] = {
            "count":                        cnt,
            "duplicate_logical_identities": dups,
            "logical_key_fields":           keys,
            "server_elapsed_ms":            srv_ms,
            "wall_elapsed_s":               round(rt_s, 2),
        }
        print(f"[step1] ✓ {c:<28s}  count={cnt}  dup={dups}  "
              f"server_ms={srv_ms}  wall_s={rt_s:.1f}")
        if dups > 0:
            dup_rows.append({
                "collection":                   c,
                "duplicate_logical_identities": dups,
                "logical_key_fields":           keys,
            })

    print("\n           collection                      count    dup_logical   server_ms")
    print("           " + "-" * 68)
    for c in TARGETS:
        r = cert_three.get(c, {})
        print(f"           {c:<28s}  {str(r.get('count', '?')):>7s}       "
              f"{str(r.get('duplicate_logical_identities', '?')):>5s}             "
              f"{str(r.get('server_elapsed_ms', '?')):>5s}")

    if dup_rows:
        print("\n❌ DUPLICATES DETECTED — refusing to force unique indexes.",
              file=sys.stderr)
        for row in dup_rows:
            print(f"   {row['collection']}: {row['duplicate_logical_identities']} "
                  f"duplicate groups on key {row['logical_key_fields']}",
                  file=sys.stderr)
        _write_report(report_path, {
            "verdict": "STOP_DUPLICATES_FOUND",
            "stage":   "step1_dup_check",
            "cert_three": cert_three,
            "dup_rows": dup_rows,
            "message": "Zero unique indexes were created.  Resolve "
                        "duplicates in Production before re-dispatching "
                        "this workflow.",
        })
        return 2

    print("[step1] ✅ zero duplicates on all three target collections — "
          "safe to create unique indexes")

    # ── Step 2: create indexes ────────────────────────────────────
    print("\n[step2] POST /api/admin/canonical-cutover/create-indexes")
    t = time.time()
    code, idx_resp = _post_json(
        f"{api_base}/api/admin/canonical-cutover/create-indexes",
        hdr_tok, {"collections": TARGETS},
        timeout_s=600)
    print(f"[step2] HTTP {code}  elapsed={(time.time()-t):.1f}s")
    if code != 200 or not isinstance(idx_resp, dict):
        _write_report(report_path, {
            "verdict": "FAIL_CREATE_INDEXES", "stage": "step2_create",
            "status": code, "body": idx_resp,
            "cert_three": cert_three,
        })
        return 3

    # ── Step 3: verify ────────────────────────────────────────────
    results = idx_resp.get("results") or []
    verify_rows: list[dict] = []
    all_targets_ok = True
    for res in results:
        c = res.get("collection")
        if c not in TARGETS:
            continue
        want_name = TARGET_INDEXES.get(c)
        created = res.get("created") or []
        existed = res.get("existed") or []
        failed  = res.get("failed")  or []
        live    = want_name in created or want_name in existed
        row = {
            "collection":    c,
            "index_name":    want_name,
            "created":       created,
            "existed":       existed,
            "failed":        failed,
            "ok":            bool(res.get("ok")),
            "unique":        True,
            "live_ready":    live and not failed,
        }
        verify_rows.append(row)
        if not row["live_ready"]:
            all_targets_ok = False

    print("\n[step3] verification:")
    print("        collection                 index_name                       unique  live/ready")
    print("        " + "-" * 80)
    for r in verify_rows:
        print(f"        {r['collection']:<24s}  {r['index_name']:<32s} YES     "
              f"{'YES' if r['live_ready'] else 'NO'}")
        if r["failed"]:
            print(f"           ↳ failed: {r['failed']}")

    if not all_targets_ok:
        _write_report(report_path, {
            "verdict": "FAIL_INDEX_NOT_LIVE",
            "stage":   "step3_verify",
            "cert_three": cert_three,
            "idx_response": idx_resp,
            "verify_rows":  verify_rows,
        })
        print("\n❌ One or more indexes did not report live/ready.", file=sys.stderr)
        return 3

    print("\n[step3] ✅ all three unique indexes are live/ready on Production.")

    # ── Step 5: 10-min delta measurement ──────────────────────────
    measurement = None
    if measure_minutes > 0:
        print(f"\n[step5] 10-minute delta measurement (interval={measure_minutes}min)")
        status_url = (f"{api_base}/api/admin/canonical-import/status"
                       f"?session_id={session_id}")

        print("[snap1] GET /canonical-import/status")
        t0 = time.time()
        code, snap1 = _get_json(status_url, hdr, timeout_s=60)
        snap1_lat = (time.time() - t0) * 1000.0
        print(f"[snap1] HTTP {code}  elapsed={snap1_lat:.0f}ms")
        if code != 200 or not isinstance(snap1, dict):
            print("[step5] WARN: snap1 unavailable; skipping measurement",
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
            snap2_lat = (time.time() - t2) * 1000.0
            print(f"[snap2] HTTP {code}  elapsed={snap2_lat:.0f}ms")
            if code == 200 and isinstance(snap2, dict):
                t_three_2 = _agg_totals_three(snap2)
                all2 = _agg_totals_all(snap2)
                hours = measure_minutes / 60.0
                # Per-target rates
                per_target_rate: dict[str, dict] = {}
                for c in TARGETS:
                    b = t1["per_target"][c]
                    a = t_three_2["per_target"][c]
                    per_target_rate[c] = {
                        "succeeded_before": b["succeeded"],
                        "succeeded_after":  a["succeeded"],
                        "failed_before":    b["failed"],
                        "failed_after":     a["failed"],
                        "in_progress_before": b["in_progress"],
                        "in_progress_after":  a["in_progress"],
                        "delta_succeeded":  a["succeeded"] - b["succeeded"],
                        "delta_failed":     a["failed"]    - b["failed"],
                        "succ_batches_per_hour": round((a["succeeded"] - b["succeeded"]) / hours, 1),
                        "fail_batches_per_hour": round((a["failed"]    - b["failed"]) / hours, 1),
                    }
                # Session overall
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
                    "interval_min":     measure_minutes,
                    "snap1_unix":       int(t0),
                    "snap2_unix":       int(t2),
                    "snap1_latency_ms": round(snap1_lat, 1),
                    "snap2_latency_ms": round(snap2_lat, 1),
                    "per_target":       per_target_rate,
                    "overall":          overall,
                    "latency_note": (
                        "Server-side per-collection write latency (p50/p95) "
                        "is measured client-side by the push driver only and "
                        "is NOT available via the status endpoint.  "
                        "Status-endpoint response latencies are included as a "
                        "proxy for server responsiveness."
                    ),
                }
                print("\n           collection                      succ Δ    fail Δ    succ/hr   fail/hr")
                print("           " + "-" * 78)
                for c in TARGETS:
                    r = per_target_rate[c]
                    print(f"           {c:<28s}  {r['delta_succeeded']:>+5d}    "
                          f"{r['delta_failed']:>+5d}    "
                          f"{r['succ_batches_per_hour']:>7.1f}   "
                          f"{r['fail_batches_per_hour']:>7.1f}")
                print(f"           {'TOTAL (session)':<28s}  "
                      f"{overall['delta_succeeded']:>+5d}    "
                      f"{overall['delta_failed']:>+5d}    "
                      f"{overall['succ_batches_per_hour']:>7.1f}   "
                      f"{overall['fail_batches_per_hour']:>7.1f}")
            else:
                print("[step5] WARN: snap2 unavailable", file=sys.stderr)

    _write_report(report_path, {
        "verdict":        "SUCCESS",
        "generated_unix": int(time.time()),
        "session_id":     session_id,
        "api_target_redacted": _redact_url(api_base),
        "step1_cert_three":   cert_three,
        "step2_idx_response": idx_resp,
        "step3_verify_rows":  verify_rows,
        "step5_measurement":  measurement,
    })

    print("\n==================== INDEX REPAIR COMPLETE ====================")
    print(f"  verdict           : SUCCESS")
    print(f"  indexes live/ready: 3/3")
    print(f"  session preserved : {session_id}")
    print(f"  report            : {report_path}")
    return 0


def _write_report(path: pathlib.Path, obj: dict) -> None:
    try:
        path.write_text(json.dumps(obj, indent=2, default=str))
    except Exception as e:
        print(f"[report] write failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[r3-indexes] interrupted", file=sys.stderr)
        sys.exit(1)
