"""benchmark_r3 — 15-minute bounded Production R3 benchmark.

Purpose
───────
Run a strictly bounded (~15 min) performance verification against
Production for session ``perklocks-cutover-20261003-r3`` after the
Emergent-Support event-loop-starvation fix was Published.  This
script is READ-ONLY against canonical collections except for the
same ``POST /api/admin/canonical-import`` writes the normal R3 driver
would make — all writes go to the SAME session with the SAME content-
hash protection, so no R3 progress is reset / lost / duplicated.

Hard safety invariants
──────────────────────
1. Does NOT reset the session.
2. Does NOT create a new session.
3. Does NOT activate ``USE_CANONICAL_DB``.
4. Does NOT run Phase 7 / Phase 8 / Phase 9.
5. Does NOT launch the full "Perklocks R3 Resume" workflow.
6. Terminates no later than ``MAX_WINDOW_MIN`` wall-clock minutes.
7. Preflight ABORTS NO-GO before any write when ANY of these fail:
   * Production ``/api/health`` not 200 under 5s
   * ``BACKGROUND_WORKERS_ENABLED`` != false
   * ``USE_CANONICAL_DB`` != false
   * ``CANONICAL_IMPORT_ENABLED`` != true
   * ``build.full`` does not start with ``EXPECTED_BUILD_PREFIX``
     (when EXPECTED_BUILD_PREFIX env is set — see note on soft-check)

Note on build-SHA verification
──────────────────────────────
Production's ``/api/admin/data-authority/status`` now returns a
``build`` object with ``full`` / ``short`` / ``source``.  When the
backend was deployed with the ``build.full`` field feature (any
commit ≥ the SHA-add commit this file is co-committed with), the
check becomes HARD.  If Production is pinned to an older build that
does not yet expose ``build.full``, the field will be absent / null
→ the preflight treats this as SOFT WARNING and still verifies
behavior via ``env_flags``.  The soft path is a one-time grace while
the first Publish propagates; subsequent benchmarks become hard.

Environment contract
────────────────────
Required (passed from GitHub Actions secrets, never printed):
    PROD_API_BASE              — e.g. https://api.perklocks.com
    PROD_ADMIN_EMAIL           — admin login
    PROD_ADMIN_PASSWORD        — admin password
    CANONICAL_IMPORT_TOKEN     — gated import route token
    CANONICAL_IMPORT_SESSION   — must equal perklocks-cutover-20261003-r3

Optional:
    EXPECTED_BUILD_PREFIX      — commit SHA (any length ≥ 7)
                                 the deployed build must start with
    MAX_WINDOW_MIN             — bench window (default 15, hard-cap 20)
    BENCHMARK_BATCH_SIZE       — docs per batch for not-yet-started
                                 collections (default 1000, cap 2000)
    BENCHMARK_CONCURRENCY      — thread-pool size (default 4, cap 8)
    HEALTH_SAMPLE_INTERVAL_S   — health-ping cadence (default 2s)

Exit codes
──────────
    0  — GO (bench + preflight all passed, verdict GO)
    1  — Generic error / crash
    2  — Preflight NO-GO
    3  — Benchmark NO-GO (regression observed)
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


# ─── Env helpers ─────────────────────────────────────────────────────
def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"[benchmark] ERROR: env var {k} not set", file=sys.stderr)
        sys.exit(1)
    return v


def _env_int(k: str, default: int, hard_max: int | None = None,
              hard_min: int | None = None) -> int:
    try:
        v = int(os.environ.get(k) or default)
    except ValueError:
        v = default
    if hard_max is not None and v > hard_max:
        v = hard_max
    if hard_min is not None and v < hard_min:
        v = hard_min
    return v


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


# ─── HTTP (never logs secret headers) ────────────────────────────────
def _get_json(url: str, headers: dict, timeout_s: int = 30):
    t_start = time.time()
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, body, (time.time() - t_start) * 1000.0
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: parsed = json.loads(txt)
        except Exception: parsed = txt
        return e.code, parsed, (time.time() - t_start) * 1000.0
    except Exception as e:
        return -1, str(e), (time.time() - t_start) * 1000.0


def _post_json(url: str, headers: dict, body: dict, timeout_s: int = 30):
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(url, data=data,
            headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: parsed = json.loads(txt)
        except Exception: parsed = txt
        return e.code, parsed


# ─── Preflight ───────────────────────────────────────────────────────
def preflight(api_base: str, jwt: str, token: str, session: str) -> tuple[bool, dict]:
    hdr_a = {"Authorization": f"Bearer {jwt}"}

    # (a) health — must be 200 and respond < 5s.
    code, body, elapsed_ms = _get_json(f"{api_base}/api/health", {}, timeout_s=5)
    if code != 200:
        return False, {"check": "health", "status": code, "body": body}
    print(f"[preflight] /api/health → {code} in {elapsed_ms:.0f}ms")

    # (b) data-authority / env_flags.
    code, status, _ = _get_json(f"{api_base}/api/admin/data-authority/status",
                                 hdr_a, timeout_s=15)
    if code != 200 or not isinstance(status, dict):
        return False, {"check": "data_authority_status", "status": code, "body": status}
    flags = status.get("env_flags", {}) or {}
    required = {
        "USE_CANONICAL_DB":           False,
        "CANONICAL_IMPORT_ENABLED":   True,
        "BACKGROUND_WORKERS_ENABLED": False,
    }
    for k, want in required.items():
        if flags.get(k) != want:
            return False, {"check": f"env_flag:{k}", "actual": flags.get(k),
                            "expected": want, "all_flags": flags}
    print(f"[preflight] env_flags OK = USE_CANONICAL_DB=false, "
          f"CANONICAL_IMPORT_ENABLED=true, BACKGROUND_WORKERS_ENABLED=false")

    # (c) build-sha check — hard if EXPECTED_BUILD_PREFIX and build.full
    # is present; soft otherwise.
    expected_prefix = (os.environ.get("EXPECTED_BUILD_PREFIX") or "").strip()
    build = status.get("build") or {}
    build_full = (build.get("full") or "").strip()
    build_short = (build.get("short") or "").strip()
    build_source = build.get("source") or "none"
    if expected_prefix:
        if not build_full or build_full == "unknown":
            print(f"[preflight] ⚠  build.full unavailable (source={build_source}); "
                  f"SOFT-SKIP build-sha check — behavior gates still verified.")
        elif not build_full.startswith(expected_prefix):
            return False, {"check": "build_sha",
                            "expected_prefix": expected_prefix,
                            "actual_full":     build_full,
                            "actual_short":    build_short,
                            "source":          build_source}
        else:
            print(f"[preflight] build.short={build_short} source={build_source} ✓ "
                  f"matches expected prefix {expected_prefix[:12]}…")
    else:
        if build_full and build_full != "unknown":
            print(f"[preflight] build.short={build_short} source={build_source} "
                  f"(EXPECTED_BUILD_PREFIX not set — informational only)")

    # (d) authoritative R3 state.
    hdr_t = {**hdr_a, "X-Canonical-Import-Token": token}
    code, r3, _ = _get_json(
        f"{api_base}/api/admin/canonical-import/status?session_id={session}",
        hdr_a, timeout_s=30)
    if code != 200 or not isinstance(r3, dict):
        return False, {"check": "r3_status", "status": code, "body": r3}
    return True, {"health_elapsed_ms": elapsed_ms, "env_flags": flags,
                   "build": build, "r3": r3}


# ─── Health-pinger (runs during the bench) ───────────────────────────
class HealthPinger(threading.Thread):
    def __init__(self, api_base: str, interval_s: float, stop_event: threading.Event):
        super().__init__(daemon=True, name="health-pinger")
        self.api_base   = api_base
        self.interval_s = interval_s
        self.stop_event = stop_event
        self.samples: list[dict] = []

    def run(self) -> None:
        while not self.stop_event.is_set():
            t0 = time.time()
            code, _, elapsed_ms = _get_json(f"{self.api_base}/api/health", {},
                                              timeout_s=5)
            self.samples.append({"ts": int(t0), "code": code,
                                  "elapsed_ms": round(elapsed_ms, 1)})
            # Sleep up to interval_s but wake quickly on stop.
            self.stop_event.wait(timeout=self.interval_s)


def _pct(xs, p):
    if not xs: return 0.0
    xs2 = sorted(xs)
    k = max(0, min(len(xs2) - 1, int(round((p / 100.0) * (len(xs2) - 1)))))
    return float(xs2[k])


# ─── Status counting helpers ─────────────────────────────────────────
def _agg_counts(r3_status: dict) -> dict:
    """Translate ``/api/admin/canonical-import/status`` into a flat view
    of succeeded/failed/in_progress/pending counts.  ``succeeded`` is
    counted at the BATCH level (that is what the server tracks).
    """
    ab = (r3_status or {}).get("aggregated_batches") or {}
    totals = {"succeeded": 0, "failed": 0, "in_progress": 0,
              "incomplete_write": 0, "docs_accepted": 0, "docs_rejected": 0}
    per_coll: dict[str, dict] = {}
    for coll, d in ab.items():
        row = {
            "succeeded":        int(d.get("batches_succeeded", 0) or 0),
            "failed":           int(d.get("batches_failed", 0) or 0),
            "in_progress":      int(d.get("batches_in_progress", 0) or 0),
            "incomplete_write": int(d.get("batches_incomplete_write", 0) or 0),
            "docs_accepted":    int(d.get("docs_accepted", 0) or 0),
            "docs_rejected":    int(d.get("docs_rejected", 0) or 0),
        }
        per_coll[coll] = row
        for k, v in row.items():
            totals[k] = totals.get(k, 0) + v
    return {"totals": totals, "per_collection": per_coll}


# ─── Main ────────────────────────────────────────────────────────────
def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")
    if session != "perklocks-cutover-20261003-r3":
        print(f"[benchmark] FATAL: CANONICAL_IMPORT_SESSION must be "
              f"'perklocks-cutover-20261003-r3' (got {session!r})",
              file=sys.stderr)
        return 1

    window_min  = _env_int("MAX_WINDOW_MIN", 15, hard_max=20, hard_min=1)
    batch_size  = _env_int("BENCHMARK_BATCH_SIZE", 1000, hard_max=2000, hard_min=50)
    concurrency = _env_int("BENCHMARK_CONCURRENCY", 4, hard_max=8, hard_min=1)
    hp_interval = float(os.environ.get("HEALTH_SAMPLE_INTERVAL_S", "2") or 2)

    logs_dir = pathlib.Path(os.environ.get("LOGS_DIR") or "/tmp/perklocks-logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / "r3_benchmark_report.json"
    push_log    = logs_dir / "r3_benchmark_push.log"
    push_metrics = logs_dir / "r3_benchmark_push_metrics.json"

    print(f"[benchmark] target={_redact(api_base)}  session={session}")
    print(f"[benchmark] window={window_min}min  batch_size={batch_size}  concurrency={concurrency}")
    print(f"[benchmark] report={report_path}")

    # ── auth ───────────────────────────────────────────────────────
    print("[auth] POST /api/auth/login")
    code, body = _post_json(f"{api_base}/api/auth/login", {},
                              {"email": email, "password": password})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"[auth] ERROR status={code}", file=sys.stderr)
        return 1
    jwt = body["access_token"]
    print(f"[auth] OK role={body.get('user',{}).get('role') or body.get('role')}")

    # ── preflight ─────────────────────────────────────────────────
    ok, pre = preflight(api_base, jwt, import_tok, session)
    if not ok:
        print(f"\n❌ PREFLIGHT NO-GO: {json.dumps(pre, indent=2, default=str)}",
              file=sys.stderr)
        report = {
            "verdict":        "NO-GO",
            "stage":          "preflight",
            "reason":         pre,
            "generated_unix": int(time.time()),
        }
        report_path.write_text(json.dumps(report, indent=2, default=str))
        return 2

    r3_before = pre.get("r3") or {}
    before = _agg_counts(r3_before)
    print(f"\n[preflight] starting R3 batches:  "
          f"succeeded={before['totals']['succeeded']}  "
          f"failed={before['totals']['failed']}  "
          f"in_progress={before['totals']['in_progress']}  "
          f"incomplete_write={before['totals']['incomplete_write']}")
    print(f"[preflight] starting R3 docs:     "
          f"accepted={before['totals']['docs_accepted']}  "
          f"rejected={before['totals']['docs_rejected']}")

    # ── bench ─────────────────────────────────────────────────────
    stop_event = threading.Event()
    pinger = HealthPinger(api_base, hp_interval, stop_event)
    pinger.start()

    scripts_dir = pathlib.Path(__file__).resolve().parent
    push_path   = str(scripts_dir / "push_canonical_accelerated.py")
    env = {
        **os.environ,
        "PROD_API_BASE":              api_base,
        "PROD_ADMIN_JWT":             jwt,
        "CANONICAL_IMPORT_TOKEN":     import_tok,
        "CANONICAL_IMPORT_SESSION":   session,
        # Fixed benchmark knobs — override any upstream values.
        "NEW_COLLECTION_BATCH_SIZE":  str(batch_size),
        "MAX_BATCH_SIZE":             os.environ.get("MAX_BATCH_SIZE", "250"),
        "CONCURRENCY":                str(concurrency),
        "WALL_CLOCK_BUDGET_MIN":      str(window_min),
        # Shorter stall detection (6 min — longer than p95 ceiling but
        # bounded enough to protect the 15-min window).
        "STALL_SECONDS":              os.environ.get("STALL_SECONDS", "360"),
        "BENCHMARK_METRICS_PATH":     str(push_metrics),
    }

    t_bench_start = time.time()
    print(f"\n[bench] starting push driver (wall={window_min}min)")
    with open(push_log, "w") as lf:
        p = subprocess.run(
            [sys.executable, "-u", push_path],
            env=env, stdout=lf, stderr=subprocess.STDOUT,
            check=False,
        )
    elapsed_s = time.time() - t_bench_start
    stop_event.set()
    pinger.join(timeout=10)

    print(f"[bench] push exit={p.returncode}  elapsed={elapsed_s:.1f}s  log={push_log}")

    # ── post-drain state ──────────────────────────────────────────
    hdr_a = {"Authorization": f"Bearer {jwt}"}
    code, r3_after, _ = _get_json(
        f"{api_base}/api/admin/canonical-import/status?session_id={session}",
        hdr_a, timeout_s=60)
    if code != 200 or not isinstance(r3_after, dict):
        print(f"[bench] WARN: failed to fetch post-drain status code={code}",
              file=sys.stderr)
        r3_after = {}
    after = _agg_counts(r3_after)

    delta_succ = after["totals"]["succeeded"] - before["totals"]["succeeded"]
    delta_fail = after["totals"]["failed"]    - before["totals"]["failed"]
    delta_docs = after["totals"]["docs_accepted"] - before["totals"]["docs_accepted"]
    docs_per_sec = (delta_docs / elapsed_s) if elapsed_s > 0 else 0.0
    batches_per_hr = (delta_succ / elapsed_s * 3600.0) if elapsed_s > 0 else 0.0

    # ── push metrics ──────────────────────────────────────────────
    pm = {}
    if push_metrics.exists():
        try:
            pm = json.loads(push_metrics.read_text())
        except Exception:
            pm = {}

    # ── health latency during load ────────────────────────────────
    health_lat = [s["elapsed_ms"] for s in pinger.samples if s["code"] == 200]
    health_fail = [s for s in pinger.samples if s["code"] != 200]
    health_summary = {
        "samples":    len(pinger.samples),
        "ok_samples": len(health_lat),
        "fail_samples": len(health_fail),
        "p50_ms":  round(_pct(health_lat, 50), 1),
        "p95_ms":  round(_pct(health_lat, 95), 1),
        "p99_ms":  round(_pct(health_lat, 99), 1),
        "min_ms":  round(min(health_lat), 1) if health_lat else 0.0,
        "max_ms":  round(max(health_lat), 1) if health_lat else 0.0,
    }

    # ── remaining work estimate for the full run ──────────────────
    # The driver reconstructs the plan each run from the SHA-verified
    # checkpoints; we trust the server's authoritative counts for
    # succeeded, but to compute REMAINING HTTP batches at batch_size
    # we need the authoritative docs_total.  Server already exposes
    # ``aggregated_batches[coll].docs_accepted`` cumulatively.  Full
    # per-collection breakdown is left to the Phase-8 cert math; here
    # we provide a conservative transport-level estimate:
    #   remaining_batches_at_benchsize = ceil(remaining_docs / batch_size)
    # with remaining_docs = max(0, source_docs_total - docs_accepted)
    # when source_docs_total is unknown we fall back to NaN markers.
    # The authoritative total lives in the plan output printed by the
    # push driver (see ``plan_summary`` lines).  We parse them from
    # push_log as a best-effort signal.
    plan_rows: list[dict] = []
    try:
        with open(push_log) as f:
            text = f.read()
        # The driver prints a plan table with columns:
        #   collection                      total   succ  queue  note
        import re
        for m in re.finditer(
            r"^([a-z_]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.*)$",
            text, re.MULTILINE,
        ):
            plan_rows.append({
                "collection": m.group(1),
                "total_batches": int(m.group(2)),
                "succeeded_batches_pre_run": int(m.group(3)),
                "queued_this_run": int(m.group(4)),
                "note": m.group(5).strip(),
            })
    except Exception:
        plan_rows = []

    remaining_batches_total = 0
    for row in plan_rows:
        remaining_batches_total += max(0, row["total_batches"] - row["succeeded_batches_pre_run"]) - 0
    # Subtract what we just committed in the bench.
    remaining_batches_total = max(0, remaining_batches_total - delta_succ)

    # ── verdict ───────────────────────────────────────────────────
    # GO rules:
    #   * push exit code 0 OR 42 (42 = clean wall-clock exit).  Any
    #     other non-zero → NO-GO.
    #   * health p95 under load < 1500 ms (soft 1500 ms = 10× nominal).
    #   * health_fail_samples < 5% of total.
    verdict = "GO"
    verdict_reasons: list[str] = []
    if p.returncode not in (0, 42):
        verdict = "NO-GO"
        verdict_reasons.append(f"push driver exit={p.returncode}")
    if health_summary["samples"] >= 10:
        if health_fail and len(health_fail) / max(health_summary["samples"], 1) > 0.05:
            verdict = "NO-GO"
            verdict_reasons.append(
                f"health failures {len(health_fail)}/{health_summary['samples']} > 5%"
            )
        if health_summary["p95_ms"] > 1500.0:
            verdict = "NO-GO"
            verdict_reasons.append(
                f"health p95 {health_summary['p95_ms']}ms > 1500ms (loop-starvation regression)"
            )

    report = {
        "verdict":        verdict,
        "verdict_reasons": verdict_reasons,
        "generated_unix": int(time.time()),
        "window_min":     window_min,
        "batch_size":     batch_size,
        "concurrency":    concurrency,
        "api_target_redacted": _redact(api_base),
        "session_id":     session,
        "preflight":      {k: v for k, v in pre.items() if k != "r3"},
        "starting":       before["totals"],
        "ending":         after["totals"],
        "delta": {
            "succeeded_batches": delta_succ,
            "failed_batches":    delta_fail,
            "docs_accepted":     delta_docs,
        },
        "throughput": {
            "elapsed_s":      round(elapsed_s, 2),
            "docs_per_sec":   round(docs_per_sec, 2),
            "batches_per_hr": round(batches_per_hr, 1),
        },
        "push_driver": {
            "exit_code":     p.returncode,
            "metrics":       pm,
            "stdout_log":    str(push_log),
        },
        "health_during_load": health_summary,
        "health_samples_raw": pinger.samples[:200],  # cap to keep report small
        "plan_rows":          plan_rows,
        "remaining": {
            # Authoritative HTTP-batch remainders computed at the
            # BENCHMARK batch_size.  The actual mix will differ slightly
            # for collections with a partial succeeded prefix (bsize=250
            # preserved) — operators should read the push driver's plan
            # table in the stdout log for the exact mix.
            "http_batches_estimate_at_bench_bsize": remaining_batches_total,
            "pending_items_estimate_min":           remaining_batches_total * batch_size,
            "note": ("Items estimate is MIN only (bench bsize = 1000 for "
                      "new collections; the picks collection still runs "
                      "at bsize=250 to preserve prior succeeded prefix). "
                      "See plan_rows for authoritative per-collection numbers."),
        },
    }

    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\n[report] {report_path}")
    print(f"\n──────────── BENCHMARK VERDICT: {verdict} ────────────")
    for r in verdict_reasons:
        print(f"  • {r}")
    print(f"  elapsed={elapsed_s:.1f}s  Δsucc_batches={delta_succ}  Δdocs={delta_docs}")
    print(f"  docs/sec={docs_per_sec:.2f}  batches/hr={batches_per_hr:.1f}")
    print(f"  health p50={health_summary['p50_ms']}ms  "
          f"p95={health_summary['p95_ms']}ms  "
          f"fail={len(health_fail)}/{health_summary['samples']}")
    print(f"  push p50={pm.get('latency_ms',{}).get('p50','?')}ms  "
          f"p95={pm.get('latency_ms',{}).get('p95','?')}ms  "
          f"timeouts={pm.get('http',{}).get('http_timeouts',0)}  "
          f"retries={pm.get('http',{}).get('http_retries',0)}  "
          f"resets={pm.get('http',{}).get('http_connection_resets',0)}")
    print(f"  remaining HTTP batches (@ bench bsize={batch_size}): "
          f"{remaining_batches_total}")

    return 0 if verdict == "GO" else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[benchmark] interrupted", file=sys.stderr)
        sys.exit(1)
