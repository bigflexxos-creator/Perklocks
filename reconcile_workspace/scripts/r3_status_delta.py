"""r3_status_delta — strictly READ-ONLY Perklocks R3 progress delta tool.

Purpose
───────
Take two snapshots of the Production R3 session status endpoint
``interval_seconds`` apart (default 300 = 5 min) and classify progress
WITHOUT mutating anything on Production.  Invoked by
``.github/workflows/perklocks-r3-status-check.yml`` as a separate,
manually-dispatched diagnostic.  Safe to run while R3 Resume is
executing — the running push driver already polls the same status
endpoint at its own startup and MongoDB reads do not block writes.

Hard safety invariants
──────────────────────
1. Only HTTP verbs issued: POST /api/auth/login (JWT mint, stateless)
   and GET /api/admin/canonical-import/status (pure read).
2. The write-side route POST /api/admin/canonical-import is NEVER
   called; the script refuses to touch the CANONICAL_IMPORT_TOKEN env
   even if provided.
3. No PATCH, PUT, DELETE anywhere.  No reset, no replay, no new
   session, no mutation of succeeded / failed / in_progress state.
4. All credential values flow through environment variables; never
   written to stdout.  JWT additionally masked via
   ``::add-mask::`` for GitHub Actions log safety.
5. Script exits cleanly on all paths — stall detectors in the Resume
   workflow cannot be perturbed because they only track the push
   driver's own POST timing.

Verdict mapping (per 2026-10-06 user directive)
───────────────────────────────────────────────
* ACTIVELY PROGRESSING       — Δsucceeded > 0
* ACTIVITY WITHOUT COMPLETION — Δsucceeded == 0 AND (Δin_progress != 0 OR last_updated changed)
* NO PROGRESS OBSERVED       — Δsucceeded == 0 AND Δin_progress == 0 AND last_updated unchanged
* CANNOT VERIFY              — any auth / status request did not return a usable snapshot

NO PROGRESS OBSERVED is evidence for further investigation, NOT
authorization to cancel the running Resume workflow.

Environment contract
────────────────────
Required (set by the workflow from GH secrets, never printed):
    PROD_API_BASE
    PROD_ADMIN_EMAIL
    PROD_ADMIN_PASSWORD

Optional:
    SESSION_ID           — default perklocks-cutover-20261003-r3
    INTERVAL_SECONDS     — default 300, hard cap 600
    REPORT_PATH          — path to write the JSON report (default
                            /tmp/perklocks-logs/r3_status_delta_report.json)

Exit codes
──────────
    0 — ACTIVELY PROGRESSING | ACTIVITY WITHOUT COMPLETION | NO PROGRESS OBSERVED
    1 — CANNOT VERIFY (auth or status failure)
    2 — argument/invariant violation
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


# ─── Guards & helpers ───────────────────────────────────────────────
def _refuse_write_token() -> None:
    """Hard invariant: the diagnostic MUST NOT be given write
    credentials.  If the caller leaked CANONICAL_IMPORT_TOKEN into the
    env, abort BEFORE any HTTP traffic.
    """
    if os.environ.get("CANONICAL_IMPORT_TOKEN"):
        print("[r3-status] FATAL: CANONICAL_IMPORT_TOKEN is set in the "
              "environment.  This diagnostic is strictly read-only and "
              "refuses to run when a write token is present.",
              file=sys.stderr)
        sys.exit(2)


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"[r3-status] FATAL: env var {k} not set", file=sys.stderr)
        sys.exit(2)
    return v


def _redact_url(url: str) -> str:
    """Return a safely redacted host form for logging — hides the
    first DNS label so audit trails don't leak the Production host."""
    try:
        p = urlparse(url)
        h = (p.hostname or "?").split(".")
        if len(h) >= 3:
            h[0] = "*"
        host = ".".join(h) + (f":{p.port}" if p.port else "")
        return host
    except Exception:
        return "<redacted>"


def _mask_github_actions(value: str) -> None:
    """Emit the ``::add-mask::`` directive so GitHub Actions scrubs the
    value from any subsequent step output.  No-op when the env var
    GITHUB_ACTIONS != 'true'."""
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        # Workflow commands must be emitted on stdout but not with any
        # trailing content; keep the value isolated.
        print(f"::add-mask::{value}", flush=True)


# ─── HTTP ────────────────────────────────────────────────────────────
def _post_json(url: str, headers: dict, body: dict, timeout_s: int = 30):
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
        txt = e.read().decode(errors="replace")[:800]
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _get_json(url: str, headers: dict, timeout_s: int = 60):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try:
            parsed = json.loads(txt)
        except Exception:
            parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ─── Status aggregation ─────────────────────────────────────────────
def _agg_totals(status: dict) -> dict:
    """Flatten ``aggregated_batches`` into a single totals dict.
    Returns succeeded / failed / in_progress at the BATCH level
    (which is what the server tracks)."""
    ab = (status or {}).get("aggregated_batches") or {}
    totals = {
        "batches_succeeded":   0,
        "batches_failed":      0,
        "batches_in_progress": 0,
        "accepted":            0,
        "rejected":            0,
    }
    for coll, d in ab.items():
        for k in totals.keys():
            totals[k] = totals[k] + int(d.get(k, 0) or 0)
    return totals


def _session_last_updated(status: dict, session_id: str) -> str:
    """Pick the ``last_updated`` (or best-available timestamp) for the
    specific session we're observing.  Returns '' if the session is
    not present in the response."""
    for s in (status or {}).get("sessions") or []:
        if (s.get("session_id") or "") == session_id:
            # Try the richest timestamp first, falling back through
            # commonly populated fields.
            for k in ("last_updated", "updated_at", "last_batch_at",
                      "last_completed_at", "started_at"):
                if s.get(k):
                    return str(s[k])
            return ""
    return ""


def _per_collection_delta(a: dict, b: dict) -> list[dict]:
    """Compute per-collection succeeded/failed/in_progress delta."""
    ab_a = (a or {}).get("aggregated_batches") or {}
    ab_b = (b or {}).get("aggregated_batches") or {}
    colls = sorted(set(ab_a.keys()) | set(ab_b.keys()))
    rows = []
    for c in colls:
        da = ab_a.get(c, {}) or {}
        db = ab_b.get(c, {}) or {}
        row = {
            "collection":            c,
            "succeeded_before":      int(da.get("batches_succeeded", 0) or 0),
            "succeeded_after":       int(db.get("batches_succeeded", 0) or 0),
            "delta_succeeded":       int(db.get("batches_succeeded", 0) or 0)
                                      - int(da.get("batches_succeeded", 0) or 0),
            "in_progress_before":    int(da.get("batches_in_progress", 0) or 0),
            "in_progress_after":     int(db.get("batches_in_progress", 0) or 0),
            "delta_in_progress":     int(db.get("batches_in_progress", 0) or 0)
                                      - int(da.get("batches_in_progress", 0) or 0),
            "failed_before":         int(da.get("batches_failed", 0) or 0),
            "failed_after":          int(db.get("batches_failed", 0) or 0),
            "delta_failed":          int(db.get("batches_failed", 0) or 0)
                                      - int(da.get("batches_failed", 0) or 0),
        }
        rows.append(row)
    return rows


# ─── Main ───────────────────────────────────────────────────────────
def main() -> int:
    _refuse_write_token()

    api_base    = _require("PROD_API_BASE").rstrip("/")
    email       = _require("PROD_ADMIN_EMAIL")
    password    = _require("PROD_ADMIN_PASSWORD")
    session_id  = (os.environ.get("SESSION_ID") or
                    "perklocks-cutover-20261003-r3").strip()
    try:
        interval = int(os.environ.get("INTERVAL_SECONDS") or 300)
    except ValueError:
        interval = 300
    interval = max(10, min(interval, 600))

    report_path = pathlib.Path(
        os.environ.get("REPORT_PATH") or
        "/tmp/perklocks-logs/r3_status_delta_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    status_url = (f"{api_base}/api/admin/canonical-import/status"
                   f"?session_id={session_id}")

    print(f"[r3-status] target={_redact_url(api_base)}  session={session_id}")
    print(f"[r3-status] endpoint=GET /api/admin/canonical-import/status "
          f"(READ-ONLY)")
    print(f"[r3-status] interval={interval}s  report={report_path}")

    # ── Mint JWT (stateless) ───────────────────────────────────────
    print("[auth] POST /api/auth/login")
    code, body = _post_json(f"{api_base}/api/auth/login", {},
                             {"email": email, "password": password},
                             timeout_s=30)
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        verdict = "CANNOT VERIFY"
        reason = f"auth status={code}"
        print(f"[auth] FAILED {reason}", file=sys.stderr)
        report = {
            "verdict": verdict, "reason": reason,
            "endpoint": status_url, "generated_unix": int(time.time()),
            "session_id": session_id,
        }
        report_path.write_text(json.dumps(report, indent=2, default=str))
        _emit_summary(verdict, snap1=None, snap2=None, interval_s=interval,
                       session_id=session_id, report_path=report_path,
                       extra_reason=reason)
        return 1
    jwt = body["access_token"]
    _mask_github_actions(jwt)   # scrub from any future log line
    role = (body.get("user") or {}).get("role") or body.get("role")
    print(f"[auth] OK role={role}")
    hdr = {"Authorization": f"Bearer {jwt}"}

    # ── Snapshot 1 ─────────────────────────────────────────────────
    t_snap1 = time.time()
    print(f"[snap1] GET {status_url}")
    code, snap1 = _get_json(status_url, hdr, timeout_s=60)
    snap1_elapsed_ms = (time.time() - t_snap1) * 1000.0
    print(f"[snap1] HTTP {code}  elapsed={snap1_elapsed_ms:.0f}ms")
    if code != 200 or not isinstance(snap1, dict):
        verdict = "CANNOT VERIFY"
        reason = f"snap1 status={code}"
        report = {
            "verdict": verdict, "reason": reason,
            "endpoint": status_url, "generated_unix": int(time.time()),
            "session_id": session_id,
            "snap1_http": code, "snap1_body": snap1,
        }
        report_path.write_text(json.dumps(report, indent=2, default=str))
        _emit_summary(verdict, snap1=None, snap2=None, interval_s=interval,
                       session_id=session_id, report_path=report_path,
                       extra_reason=reason)
        return 1

    tot1 = _agg_totals(snap1)
    lu1  = _session_last_updated(snap1, session_id)
    print(f"[snap1] totals: succeeded={tot1['batches_succeeded']}  "
          f"failed={tot1['batches_failed']}  "
          f"in_progress={tot1['batches_in_progress']}  "
          f"accepted_docs={tot1['accepted']}  "
          f"last_updated={lu1 or '(none)'}")

    # ── Wait ───────────────────────────────────────────────────────
    print(f"[wait] sleeping {interval}s")
    try:
        time.sleep(interval)
    except KeyboardInterrupt:
        print("[wait] interrupted", file=sys.stderr)
        return 2

    # ── Snapshot 2 ─────────────────────────────────────────────────
    t_snap2 = time.time()
    print(f"[snap2] GET {status_url}")
    code, snap2 = _get_json(status_url, hdr, timeout_s=60)
    snap2_elapsed_ms = (time.time() - t_snap2) * 1000.0
    print(f"[snap2] HTTP {code}  elapsed={snap2_elapsed_ms:.0f}ms")
    if code != 200 or not isinstance(snap2, dict):
        verdict = "CANNOT VERIFY"
        reason = f"snap2 status={code}"
        report = {
            "verdict": verdict, "reason": reason,
            "endpoint": status_url, "generated_unix": int(time.time()),
            "session_id": session_id,
            "snap1_totals": tot1, "snap1_last_updated": lu1,
            "snap2_http": code, "snap2_body": snap2,
        }
        report_path.write_text(json.dumps(report, indent=2, default=str))
        _emit_summary(verdict, snap1=tot1, snap2=None, interval_s=interval,
                       session_id=session_id, report_path=report_path,
                       extra_reason=reason)
        return 1

    tot2 = _agg_totals(snap2)
    lu2  = _session_last_updated(snap2, session_id)
    print(f"[snap2] totals: succeeded={tot2['batches_succeeded']}  "
          f"failed={tot2['batches_failed']}  "
          f"in_progress={tot2['batches_in_progress']}  "
          f"accepted_docs={tot2['accepted']}  "
          f"last_updated={lu2 or '(none)'}")

    # ── Deltas + verdict (per directive 2026-10-06) ────────────────
    d_succ  = tot2["batches_succeeded"]   - tot1["batches_succeeded"]
    d_fail  = tot2["batches_failed"]      - tot1["batches_failed"]
    d_ip    = tot2["batches_in_progress"] - tot1["batches_in_progress"]
    d_docs  = tot2["accepted"]            - tot1["accepted"]
    lu_changed = (lu1 or "") != (lu2 or "")

    if d_succ > 0:
        verdict = "ACTIVELY PROGRESSING"
    elif d_succ == 0 and d_ip == 0 and not lu_changed:
        verdict = "NO PROGRESS OBSERVED"
    else:
        # Δsucceeded == 0 but something moved: in_progress changed OR
        # last_updated advanced.  Writes are happening (or batches are
        # being claimed) but none completed in this 5-min window.
        verdict = "ACTIVITY WITHOUT COMPLETION"

    print()
    print("================= R3 STATUS DELTA =================")
    print(f"  endpoint         : GET /api/admin/canonical-import/status")
    print(f"  session_id       : {session_id}")
    print(f"  interval_s       : {interval}")
    print(f"  Δsucceeded_batches  = {d_succ:+d}")
    print(f"  Δfailed_batches     = {d_fail:+d}")
    print(f"  Δin_progress_batches = {d_ip:+d}")
    print(f"  Δaccepted_docs      = {d_docs:+d}")
    print(f"  last_updated_changed = {lu_changed}  "
          f"(before={lu1 or '(none)'} after={lu2 or '(none)'})")
    print(f"  verdict          : {verdict}")
    print("===================================================")
    print()
    print("NOTE: 'NO PROGRESS OBSERVED' is evidence for further "
          "investigation ONLY — it is not authorization to cancel the "
          "running Resume workflow.")

    report = {
        "schema":          "r3_status_delta_v1",
        "generated_unix":  int(time.time()),
        "verdict":         verdict,
        "endpoint":        "GET /api/admin/canonical-import/status",
        "api_target_redacted": _redact_url(api_base),
        "session_id":      session_id,
        "interval_s":      interval,
        "snap1": {
            "unix":          int(t_snap1),
            "http_code":     200,
            "totals":        tot1,
            "last_updated":  lu1,
            "elapsed_ms":    round(snap1_elapsed_ms, 1),
        },
        "snap2": {
            "unix":          int(t_snap2),
            "http_code":     200,
            "totals":        tot2,
            "last_updated":  lu2,
            "elapsed_ms":    round(snap2_elapsed_ms, 1),
        },
        "delta": {
            "succeeded_batches":   d_succ,
            "failed_batches":      d_fail,
            "in_progress_batches": d_ip,
            "accepted_docs":       d_docs,
            "last_updated_changed": lu_changed,
        },
        "per_collection_delta": _per_collection_delta(snap1, snap2),
    }
    report_path.write_text(json.dumps(report, indent=2, default=str))

    _emit_summary(verdict, snap1=tot1, snap2=tot2, interval_s=interval,
                   session_id=session_id, report_path=report_path,
                   extra_reason=None)
    return 0


# ─── GitHub Actions step-summary emitter ────────────────────────────
def _emit_summary(verdict, *, snap1, snap2, interval_s, session_id,
                   report_path, extra_reason):
    """Append a Markdown block to $GITHUB_STEP_SUMMARY so the verdict
    is visible in the Actions UI without opening the JSON artifact.
    """
    sumfile = os.environ.get("GITHUB_STEP_SUMMARY")
    if not sumfile:
        return
    try:
        lines = [
            "## Perklocks R3 Status Delta",
            f"- **Verdict:** `{verdict}`",
            f"- **Session:** `{session_id}`",
            f"- **Interval:** {interval_s} seconds",
            f"- **Endpoint:** `GET /api/admin/canonical-import/status`",
            f"- **Report artifact:** `{report_path}`",
        ]
        if extra_reason:
            lines.append(f"- **Reason:** {extra_reason}")
        if snap1 is not None:
            lines.append("")
            lines.append("### Snapshot 1")
            lines.append(f"- succeeded: {snap1['batches_succeeded']}")
            lines.append(f"- failed: {snap1['batches_failed']}")
            lines.append(f"- in_progress: {snap1['batches_in_progress']}")
        if snap2 is not None:
            lines.append("")
            lines.append("### Snapshot 2")
            lines.append(f"- succeeded: {snap2['batches_succeeded']}")
            lines.append(f"- failed: {snap2['batches_failed']}")
            lines.append(f"- in_progress: {snap2['batches_in_progress']}")
        if snap1 is not None and snap2 is not None:
            lines.append("")
            lines.append("### Delta")
            lines.append(f"- Δsucceeded = "
                          f"{snap2['batches_succeeded']-snap1['batches_succeeded']:+d}")
            lines.append(f"- Δin_progress = "
                          f"{snap2['batches_in_progress']-snap1['batches_in_progress']:+d}")
            lines.append(f"- Δfailed = "
                          f"{snap2['batches_failed']-snap1['batches_failed']:+d}")
        lines.append("")
        lines.append("> `NO PROGRESS OBSERVED` is evidence for further "
                      "investigation only — not authorization to cancel "
                      "the running Resume workflow.")
        with open(sumfile, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[r3-status] interrupted", file=sys.stderr)
        sys.exit(2)
