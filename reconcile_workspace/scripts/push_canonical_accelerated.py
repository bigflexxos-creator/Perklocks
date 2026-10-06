"""push_canonical_accelerated — Phase-5-R3 accelerated migration driver.

RELATION TO push_canonical_to_production.py
───────────────────────────────────────────
Same server contract, same request body shape, same session_id, same
atomic validate → bulk_write → verify → mark-succeeded protocol on the
server.  **All already-succeeded batches remain preserved** — this
driver cannot and does not overwrite, re-batch, or invalidate any
``status=succeeded`` record on the server.  Phase-8 completeness
certification remains compatible.

What this driver changes (migration-transport only)
───────────────────────────────────────────────────
1. **DONE-collection fast-skip.** On startup we call the existing
   ``GET /api/admin/canonical-import/status`` endpoint, compute each
   collection's authoritative succeeded-batch count, and compare to the
   expected total batches we would send for that collection.  If
   succeeded == expected, we **do not iterate the NDJSON file at all**.
   For 5 of the 21 collections (games / historical_ingestion_state /
   nfl_ingest_meta / nfl_player_weekly / parlay_history on current
   state) this eliminates ~615 fast-replay HTTP round-trips.

2. **Larger batches on NOT-STARTED collections only.** The server
   atomic protocol is batch-size-agnostic (content hash is over the
   logical-key tuples; Mongo bulk_write 16 MB ceiling easily fits
   1 000 upserts of our row shape at ~500 B/row).  For collections
   with 0 succeeded batches so far we use ``NEW_COLLECTION_BATCH_SIZE``
   (default 1 000) — 4× fewer round-trips per unit of imported data.

   For collections with a partial succeeded prefix (`picks`: 1 109
   already-done at 250), we **keep MAX_BATCH_SIZE=250** so every
   existing batch_no + content_hash lines up with the server record
   and fast-path skips.  No in-flight picks batch is re-written.

3. **Bounded concurrency.** A ``concurrent.futures.ThreadPoolExecutor``
   with ``CONCURRENCY`` workers (default 8) issues POSTs in parallel.
   Each batch still goes through the full server-side atomic protocol
   — concurrency is purely at the HTTP transport layer.  Different
   batches target different documents (logical-key upserts), so
   per-batch write races are impossible by Mongo's own guarantees.

4. **Same Pass 1 → Pass 2 retry structure.** Per-POST attempts capped
   at 3 with bounded exponential back-off (2 s / 4 s / 8 s).  Any
   batches that fail Pass 1 are retried once in Pass 2.  Still-failed
   after Pass 2 → driver exits non-zero and the orchestrator refuses
   Phases 6/7/8.

5. **Zero changes to server code / canonical collections / flags /
   session bookkeeping / checkpoint files.**

ENV OVERRIDES
─────────────
- ``PROD_API_BASE``, ``PROD_ADMIN_JWT``, ``CANONICAL_IMPORT_TOKEN``,
  ``CANONICAL_IMPORT_SESSION`` — required (same as legacy driver).
- ``CHKP_DIR`` — overrides checkpoint source dir.
- ``MAX_BATCH_SIZE`` — picks + any collection with a succeeded prefix
  (default 250).
- ``NEW_COLLECTION_BATCH_SIZE`` — not-yet-started collections
  (default 1 000, hard-capped at 2 000).
- ``CONCURRENCY`` — thread pool size (default 8, hard-capped at 16).
- ``COLLECTIONS_FILTER`` — comma-separated allowlist (legacy param).

NOT-STARTED-COLLECTION OVERRIDE
───────────────────────────────
The decision "partial succeeded prefix exists → stay at 250" is
authoritative.  We never upload a batch that could collide with an
existing content_hash under a different batch_no.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

CHKP = pathlib.Path(os.environ.get("CHKP_DIR") or "/app/reconcile_workspace/checkpoints")

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

EXPECTED_SHA = {
    "phase5_20261003_190628Z.tar.gz":
        "4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7",
    "phase6_20261003_192150Z.tar.gz":
        "327a38903daf3bd910ab50b68f69415cfcf181624655b42fb791af43042b6c22",
}


# ────────────────────────── utility helpers ──────────────────────────
def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr); sys.exit(2)
    return v


def _env_int(k: str, default: int, hard_max: int | None = None) -> int:
    try:
        v = int(os.environ.get(k) or default)
    except ValueError:
        v = default
    if hard_max is not None and v > hard_max:
        v = hard_max
    return v


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _sha_tar(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _extract(tar: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    with tarfile.open(tar) as tf:
        tf.extractall(out)
    for d in out.iterdir():
        if d.is_dir():
            return d
    raise RuntimeError(f"no inner dir in {tar}")


def _ndjson(path: pathlib.Path) -> Iterator[dict]:
    with open(path) as f:
        for ln in f:
            try: r = json.loads(ln)
            except Exception: continue
            yield r["doc"] if isinstance(r, dict) and "doc" in r else r


def _is_excluded(d: dict) -> bool:
    return (d.get("excluded_from_canonical_runtime") is True) \
        or (d.get("status") == "UNRESOLVED_IMMUTABLE_CONFLICT")


# ────────────────────────── HTTP ──────────────────────────────────────
def _post(url: str, headers: dict, body: dict, *,
          retries: int, timeout_s: int) -> tuple[int, dict | str]:
    """POST with bounded retries + bounded backoff.  Caller supplies
    the retry/timeout budget so the accelerated driver can tighten it
    globally without touching this helper."""
    data = json.dumps(body, default=str).encode()
    delays = [2, 4, 8]
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data,
                headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:400]
            # 400/401/403/413 are configuration errors — fail fast.
            if e.code in (400, 401, 403, 413):
                return e.code, txt
            # 409 ALTERED_REPLAY_REJECTED must never silently resolve.
            if e.code == 409:
                return e.code, txt
            last_err = f"HTTP {e.code}: {txt}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:400]}"
        if attempt < retries - 1:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    return 599, last_err or "unknown_error"


def _get(url: str, headers: dict, timeout_s: int = 120):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:800]
    except Exception as e:
        return -1, str(e)


# ────────────────────────── main ──────────────────────────────────────
def main() -> int:
    t0 = time.time()
    api_base   = _require("PROD_API_BASE").rstrip("/")
    admin_jwt  = _require("PROD_ADMIN_JWT")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session    = _require("CANONICAL_IMPORT_SESSION")

    max_batch_default = _env_int("MAX_BATCH_SIZE", 250, hard_max=2000)
    new_batch_size    = _env_int("NEW_COLLECTION_BATCH_SIZE", 1000, hard_max=2000)
    concurrency       = _env_int("CONCURRENCY", 8, hard_max=16)
    # Phase-5-R3 ACCELERATION HARDENING (post-Run-#5 forensic fix):
    #   Each worker's bounded request budget is now tight so a single
    #   slow bulk_write cannot block a worker for ~15 min.  The driver
    #   itself owns a wall-clock budget and exits CLEANLY at it so the
    #   GH 350-min cap cannot SIGKILL mid-write (Pass 7/8 can then run
    #   on the next resume).  Stall detection prevents silent hangs.
    request_timeout_s = _env_int("REQUEST_TIMEOUT_S", 180, hard_max=600)
    request_retries   = _env_int("REQUEST_RETRIES", 2, hard_max=5)
    wall_clock_budget = _env_int("WALL_CLOCK_BUDGET_MIN", 320, hard_max=350)
    stall_seconds     = _env_int("STALL_SECONDS", 600, hard_max=3600)
    filter_set = set((os.environ.get("COLLECTIONS_FILTER") or "").split(",")) - {""}

    headers_post = {
        "Authorization":            f"Bearer {admin_jwt}",
        "X-Canonical-Import-Token": import_tok,
    }
    headers_get = {"Authorization": f"Bearer {admin_jwt}"}

    print(f"[1/6] redacted Prod target: {_redact(api_base)}")
    print(f"[1/6] concurrency={concurrency}  batch(partial/picks)={max_batch_default}  batch(new)={new_batch_size}")
    print(f"[1/6] request_timeout={request_timeout_s}s  retries={request_retries}  "
          f"wall_clock_budget={wall_clock_budget}min  stall={stall_seconds}s")

    # SHA-verify both checkpoints before touching anything.
    print("[2/6] SHA-verifying checkpoints …")
    for fn, sha in EXPECTED_SHA.items():
        p = CHKP / fn
        if not p.exists():
            print(f"ERROR: missing checkpoint {p}", file=sys.stderr); return 3
        actual = _sha_tar(p)
        if actual != sha:
            print(f"ERROR: SHA mismatch {fn}: {actual} != {sha}", file=sys.stderr); return 3
    print("[2/6] both checkpoints SHA-verified")

    # Fetch authoritative session state → compute per-collection fast-skip.
    print("[3/6] fetching authoritative session state …")
    code, state = _get(f"{api_base}/api/admin/canonical-import/status?session_id={session}",
                        headers_get)
    if code != 200 or not isinstance(state, dict):
        print(f"ERROR: status endpoint {code}: {state}", file=sys.stderr); return 10
    ab = state.get("aggregated_batches", {})
    pre_succeeded: dict[str, int] = {
        coll: int(d.get("batches_succeeded", 0)) for coll, d in ab.items()
    }
    total_pre_succ = sum(pre_succeeded.values())
    print(f"[3/6] authoritative pre-run succeeded batches: {total_pre_succ}")

    # Reconstruct final canonical.
    print("[4/6] extracting checkpoints …")
    workdir = pathlib.Path(tempfile.mkdtemp(
        prefix="accel_push_",
        dir=os.environ.get("RECONCILE_TMP") or tempfile.gettempdir()))
    p5_root = _extract(CHKP / "phase5_20261003_190628Z.tar.gz", workdir / "p5")
    p6_root = _extract(CHKP / "phase6_20261003_192150Z.tar.gz", workdir / "p6")
    canon5 = p5_root / "canonical"
    canon6 = p6_root / "canonical"
    source_cps = {
        "phase5_sha":            EXPECTED_SHA["phase5_20261003_190628Z.tar.gz"],
        "phase6_sha":            EXPECTED_SHA["phase6_20261003_192150Z.tar.gz"],
        "preview_baseline_sha":  "26cd67d0648ef62f495807c5303fdc1acafb28a3015e249718a7b47a3728d78b",
    }

    # Plan per-collection work.
    print("[5/6] planning work …")

    def _source_for(coll: str) -> pathlib.Path | None:
        p5f = canon5 / f"{coll}.ndjson"
        p6f = canon6 / f"{coll}.ndjson"
        if p6f.exists() and p6f.stat().st_size > 0: return p6f
        if p5f.exists(): return p5f
        return None

    # First pass: count importable rows per collection to compute expected batches.
    plan: list[tuple[str, int, list[dict]]] = []   # (coll, batch_no, docs)
    plan_summary = []
    skip_counts = {"done_collections": 0, "done_batches": 0}
    for coll in RECONCILED_21:
        if filter_set and coll not in filter_set: continue
        src = _source_for(coll)
        if src is None:
            plan_summary.append((coll, 0, 0, 0, "no-source")); continue

        pre_s = pre_succeeded.get(coll, 0)
        # Batch size decision:
        #   - partial-succeeded collection → preserve existing batch size
        #   - zero-succeeded → use larger NEW_COLLECTION_BATCH_SIZE
        bsize = max_batch_default if pre_s > 0 else new_batch_size

        # Materialize batches.
        docs_buffer: list[dict] = []
        batches_this_coll = 0
        for d in _ndjson(src):
            if _is_excluded(d): continue
            docs_buffer.append(d)
            if len(docs_buffer) >= bsize:
                plan.append((coll, batches_this_coll, docs_buffer))
                batches_this_coll += 1
                docs_buffer = []
        if docs_buffer:
            plan.append((coll, batches_this_coll, docs_buffer))
            batches_this_coll += 1

        # DONE fast-skip: if every batch of this collection is already
        # succeeded, remove them from the plan entirely.
        if pre_s >= batches_this_coll and batches_this_coll > 0:
            removed = [t for t in plan if t[0] == coll]
            plan = [t for t in plan if t[0] != coll]
            skip_counts["done_collections"] += 1
            skip_counts["done_batches"] += len(removed)
            plan_summary.append((coll, batches_this_coll, pre_s, 0, f"DONE (skipped {len(removed)})"))
        else:
            remaining = batches_this_coll - pre_s
            plan_summary.append((coll, batches_this_coll, pre_s, max(remaining, 0),
                                  f"queue bsize={bsize}"))

    total_in_plan = len(plan)
    print(f"[5/6] plan: {total_in_plan} batch POSTs enqueued "
          f"(skipped {skip_counts['done_batches']} already-done)")
    print()
    print(f"{'collection':<30}{'total':>7}{'succ':>7}{'queue':>7}  note")
    print("-" * 90)
    for coll, total, succ, q, note in plan_summary:
        print(f"{coll:<30}{total:>7}{succ:>7}{q:>7}  {note}")
    print()

    if not plan:
        print("[plan] nothing to do — all collections DONE.  Driver exits 0.")
        shutil.rmtree(workdir, ignore_errors=True)
        return 0

    # ── Interleave plan across collections ────────────────────────────
    # Prior failure: picks (first in RECONCILED_21) had 1 301 batches
    # queued contiguously, consuming all 8 workers for ~2 h and
    # starving later collections — settlement_events only got 5 of
    # ~497 batches before the GH 350-min cap.  Round-robin ensures
    # every collection's first batch is committed within the first
    # few seconds; the queue remainder is dovetailed.
    by_coll: dict[str, list] = {}
    for item in plan:
        by_coll.setdefault(item[0], []).append(item)
    interleaved: list = []
    iters = [iter(v) for v in by_coll.values()]
    while iters:
        next_iters = []
        for it in iters:
            try:
                interleaved.append(next(it))
                next_iters.append(it)
            except StopIteration:
                pass
        iters = next_iters
    plan = interleaved
    total_in_plan = len(plan)

    # Execute Pass 1 with bounded concurrency, wall-clock budget,
    # and stall detection.
    print(f"[6/6] Pass 1: dispatching {total_in_plan} batches "
          f"concurrency={concurrency} wall={wall_clock_budget}min")
    lock = threading.Lock()
    counters = {"succeeded": 0, "replayed": 0, "accepted": 0, "rejected": 0, "failed": 0}
    failed_tasks: list[tuple[str, int, list[dict]]] = []
    last_success_ts = time.time()
    stop_requested = threading.Event()
    wall_deadline = t0 + wall_clock_budget * 60

    def _submit_one(coll: str, bno: int, docs: list[dict]) -> dict:
        if stop_requested.is_set():
            return {"code": -1, "result": "stop_requested", "coll": coll, "bno": bno, "n": len(docs)}
        code, result = _post(
            f"{api_base}/api/admin/canonical-import",
            headers_post,
            {"session_id":         session,
             "collection":         coll,
             "batch_no":           bno,
             "docs":               docs,
             "source_checkpoints": source_cps},
            retries=request_retries,
            timeout_s=request_timeout_s)
        return {"code": code, "result": result, "coll": coll, "bno": bno, "n": len(docs)}

    def _handle(outcome: dict) -> bool:
        nonlocal last_success_ts
        code = outcome["code"]; res = outcome["result"]
        coll = outcome["coll"]; bno = outcome["bno"]; n = outcome["n"]
        if code == 200 and isinstance(res, dict) and res.get("status") == "succeeded":
            with lock:
                counters["succeeded"] += 1
                if res.get("idempotent_replay"): counters["replayed"] += 1
                counters["accepted"] += int(res.get("accepted", 0))
                counters["rejected"] += int(res.get("rejected", 0))
                last_success_ts = time.time()
                tot = counters["succeeded"]
                # Progress every 10 commits + always on first/last.
                if tot <= 3 or tot % 10 == 0 or tot == total_in_plan:
                    elapsed = max(time.time() - t0, 0.1)
                    rate = tot / elapsed * 60
                    budget_left_min = max((wall_deadline - time.time()) / 60.0, 0)
                    print(f"  [{tot}/{total_in_plan}] {coll} b#{bno} n={n} "
                          f"replay={bool(res.get('idempotent_replay'))} "
                          f"| rate={rate:.1f} b/min  budget_left={budget_left_min:.1f} min  "
                          f"failed={counters['failed']}")
            return True
        with lock:
            counters["failed"] += 1
            print(f"  ✗ PASS1 FAIL {coll} b#{bno} n={n}  code={code}  "
                  f"err={str(res)[:200]}", flush=True)
        return False

    # Watchdog thread: on wall-clock OR stall, request stop.
    def _watchdog():
        while not stop_requested.is_set():
            time.sleep(5)
            now = time.time()
            if now >= wall_deadline:
                print(f"\n⏱  WALL-CLOCK BUDGET HIT ({wall_clock_budget} min) — "
                      f"stop_requested; in-flight will finish, no new submissions", flush=True)
                stop_requested.set()
                return
            with lock:
                stall = now - last_success_ts
                have_prog = counters["succeeded"] > 0
            if have_prog and stall >= stall_seconds:
                print(f"\n⚠  STALL DETECTED (no success for {stall:.0f}s ≥ "
                      f"{stall_seconds}s) — stop_requested; exiting cleanly", flush=True)
                stop_requested.set()
                return

    watchdog = threading.Thread(target=_watchdog, daemon=True, name="watchdog")
    watchdog.start()

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="pushw") as pool:
        pending_futs = {}
        plan_iter = iter(plan)
        # Prime up to concurrency*2 (small buffer).
        for _ in range(concurrency * 2):
            try:
                c, b, d = next(plan_iter)
                pending_futs[pool.submit(_submit_one, c, b, d)] = (c, b, d)
            except StopIteration:
                break

        while pending_futs:
            # Wait for next completed future.
            done_fut = None
            for fut in as_completed(list(pending_futs.keys()), timeout=None):
                done_fut = fut
                break
            if done_fut is None:
                break
            task = pending_futs.pop(done_fut)
            out = done_fut.result()
            if not _handle(out):
                failed_tasks.append(task)
            # Submit next unless stopped.
            if not stop_requested.is_set():
                try:
                    c, b, d = next(plan_iter)
                    pending_futs[pool.submit(_submit_one, c, b, d)] = (c, b, d)
                except StopIteration:
                    pass
        # If we exited due to stop_requested, pool.__exit__ waits for
        # in-flight workers to finish (which get stop_requested on
        # their first _submit_one call → return quickly).

    stop_requested.set()
    pass1_elapsed = time.time() - t0
    pass1_succ = counters["succeeded"]
    pass1_rate = pass1_succ / max(pass1_elapsed, 0.1) * 60
    print(f"\n[pass1] completed in {pass1_elapsed:.1f}s  succeeded={pass1_succ}  "
          f"failed={len(failed_tasks)}  rate={pass1_rate:.1f} batches/min  "
          f"budget_hit={time.time() >= wall_deadline}")

    budget_hit = time.time() >= wall_deadline

    # Pass 2 only if we have time left AND there are failures AND not stall-stopped.
    # If we hit wall-clock budget: skip Pass 2, exit 42 so the next GH run
    # resumes Pass 1 (which will retry the still-failed batches via
    # server-side status=failed → re-attempt).
    if failed_tasks and not budget_hit:
        print(f"\n[pass2] retrying {len(failed_tasks)} failed batch(es) with "
              f"bounded {request_retries}-attempt policy")
        still_failed: list[dict] = []
        with ThreadPoolExecutor(max_workers=max(2, concurrency // 2), thread_name_prefix="retryw") as pool:
            futures = {pool.submit(_submit_one, c, b, d): (c, b, d) for c, b, d in failed_tasks}
            for fut in as_completed(futures):
                out = fut.result()
                if not _handle(out):
                    still_failed.append({"coll": out["coll"], "bno": out["bno"],
                                          "n": out["n"], "code": out["code"],
                                          "err": str(out["result"])[:400]})
        if still_failed:
            print(f"\n❌ PASS 2 STILL FAILED on {len(still_failed)} batch(es):", file=sys.stderr)
            for s in still_failed[:20]:
                print(f"   {s['coll']} b#{s['bno']} n={s['n']}  code={s['code']}  err={s['err']}", file=sys.stderr)
            shutil.rmtree(workdir, ignore_errors=True)
            return 40
        print("[pass2] all retried batches recovered")

    total_elapsed = time.time() - t0
    final_rate = counters["succeeded"] / max(total_elapsed, 0.1) * 60
    print(f"\n[DONE] {counters['succeeded']}/{total_in_plan} batches committed in {total_elapsed:.1f}s "
          f"| rate={final_rate:.1f} batches/min "
          f"| accepted={counters['accepted']} rejected={counters['rejected']} "
          f"| replayed-fast-path={counters['replayed']} "
          f"| left_for_next_run={len(failed_tasks) + (total_in_plan - counters['succeeded'] - counters['failed'])}")

    shutil.rmtree(workdir, ignore_errors=True)

    # Exit 42 if wall-clock budget was hit with work remaining or
    # failures outstanding — orchestrator should NOT run Phase 7/8
    # yet but SHOULD NOT fail hard (next GH run will resume cleanly).
    if budget_hit and (failed_tasks or counters["succeeded"] < total_in_plan):
        print(f"\n⏱  WALL-CLOCK EXIT (42) — {total_in_plan - counters['succeeded']} batches "
              f"not yet attempted + {len(failed_tasks)} to retry on next run")
        return 42
    return 0


if __name__ == "__main__":
    sys.exit(main())
