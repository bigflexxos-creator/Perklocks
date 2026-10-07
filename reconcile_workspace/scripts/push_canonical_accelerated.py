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
   with ``CONCURRENCY`` workers (default 4 after the 2026-10-05
   Emergent-Support event-loop-starvation fix; hard cap 8) issues
   POSTs in parallel.  Each batch still goes through the full
   server-side atomic protocol — concurrency is purely at the HTTP
   transport layer.  Different batches target different documents
   (logical-key upserts), so per-batch write races are impossible by
   Mongo's own guarantees.

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
- ``CONCURRENCY`` — thread pool size (default 4, hard-capped at 8).
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


# ─── Server-identical hashing (replicates services/canonical_cutover.py) ──
# These helpers MUST remain byte-for-byte compatible with the server's
# ``batch_content_hash`` + ``extract_logical_key`` + ``is_excluded``.
# Any divergence here guarantees ALTERED_REPLAY_REJECTED on Prod.
_QUARANTINE_STATUSES = {"UNRESOLVED_IMMUTABLE_CONFLICT"}
_EXCLUSION_FIELD      = "excluded_from_canonical_runtime"


def _logical_key_fields(coll: str) -> tuple[str, ...]:
    return LOGICAL_KEYS.get(coll, ("_id",))


def _extract_logical_key(coll: str, doc: dict) -> tuple:
    return tuple(doc.get(f) for f in _logical_key_fields(coll))


def _server_is_excluded(doc: dict) -> bool:
    """Mirror of services.canonical_cutover.is_excluded (True side only)."""
    if doc.get(_EXCLUSION_FIELD) is True:
        return True
    if doc.get("status") in _QUARANTINE_STATUSES:
        return True
    return False


def _server_batch_content_hash(coll: str, accepted_batch: list[dict]) -> str:
    """Byte-for-byte replica of
    ``services.canonical_cutover.batch_content_hash``.

    * Sorts rows by ``extract_logical_key`` (stringified tuple).
    * Encodes each row as ``json.dumps([list(lk), doc],
      default=str, sort_keys=True)``.
    * SHA-256 hex digest of the concatenation.
    """
    rows = []
    for doc in accepted_batch:
        lk = _extract_logical_key(coll, doc)
        rows.append((lk, doc))
    rows.sort(key=lambda r: tuple(str(x) for x in r[0]))
    h = hashlib.sha256()
    for lk, doc in rows:
        h.update(json.dumps([list(lk), doc], default=str, sort_keys=True).encode())
    return h.hexdigest()


# ─── Canary (zero-write) mode knobs ──────────────────────────────────
# CANARY_ONLY=1 → fetch manifest, reconstruct layout, compute local
# payload hashes, compare to server's stored content_hash, print a
# ``collection | batch_id | expected_hash | computed_hash | MATCH``
# table.  Issues ZERO POSTs under any circumstance.
#
# Canary batch_id filters: the standard R3 canary set is
#   player_identities      → batch 0, 1
#   prediction_snapshots   → batch 0, 1
#   pregame_snapshots      → batch 0, 1
#   publication_events     → batch 0, 1
#   picks                  → first unfinished batch_no
_CANARY_FIXED_BATCH_IDS = {
    "player_identities":    [0, 1],
    "prediction_snapshots": [0, 1],
    "pregame_snapshots":    [0, 1],
    "publication_events":   [0, 1],
}
_CANARY_FIRST_UNFINISHED = {"picks"}
_CANARY_COLLECTIONS = set(_CANARY_FIXED_BATCH_IDS.keys()) | _CANARY_FIRST_UNFINISHED


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
          retries: int, timeout_s: int,
          metrics: dict | None = None) -> tuple[int, dict | str]:
    """POST with bounded retries + bounded backoff.  Caller supplies
    the retry/timeout budget so the accelerated driver can tighten it
    globally without touching this helper.

    When ``metrics`` dict is passed, the function increments the
    following keys atomically-ish (caller must guard with its own
    lock if cross-thread aggregation is needed):

        metrics["http_attempts_total"]       — every attempt made
        metrics["http_retries"]              — attempts beyond the first
        metrics["http_timeouts"]             — ``socket.timeout`` style
        metrics["http_connection_resets"]    — ``ConnectionResetError``
        metrics["http_http_errors"]          — non-5xx HTTPError kept
                                                (fail-fast codes land in
                                                caller via return)
    """
    data = json.dumps(body, default=str).encode()
    delays = [2, 4, 8]
    last_err = None
    for attempt in range(retries):
        if metrics is not None:
            metrics["http_attempts_total"] = metrics.get("http_attempts_total", 0) + 1
            if attempt > 0:
                metrics["http_retries"] = metrics.get("http_retries", 0) + 1
        req = urllib.request.Request(url, data=data,
                headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")[:400]
            if metrics is not None:
                metrics["http_http_errors"] = metrics.get("http_http_errors", 0) + 1
            # 400/401/403/413 are configuration errors — fail fast.
            if e.code in (400, 401, 403, 413):
                return e.code, txt
            # 409 ALTERED_REPLAY_REJECTED must never silently resolve.
            if e.code == 409:
                return e.code, txt
            last_err = f"HTTP {e.code}: {txt}"
        except Exception as e:
            msg = str(e)
            if metrics is not None:
                lname = type(e).__name__.lower()
                if "timeout" in lname or "timed out" in msg.lower():
                    metrics["http_timeouts"] = metrics.get("http_timeouts", 0) + 1
                if "connectionreset" in lname or "connection reset" in msg.lower():
                    metrics["http_connection_resets"] = metrics.get("http_connection_resets", 0) + 1
            last_err = f"{type(e).__name__}: {msg[:400]}"
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
    # Emergent Support 2026-10-05 — initial safe target is 4 concurrent
    # POSTs @ 1 000 docs/batch.  Only consider raising toward 6×2 000
    # after the bounded production benchmark proves p95 remains well
    # below the 85-sec request budget and health stays responsive.
    concurrency       = _env_int("CONCURRENCY", 4, hard_max=8)
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
    # ── R3 Resume #11 surgical fix: zero-write canary mode ───────────
    # When ``CANARY_ONLY=1`` the driver DOES NOT issue any POST under
    # any circumstance — it only reconstructs the authoritative batch
    # layout, computes the local payload hash, and compares to the
    # server's stored ``content_hash``.  Report format is machine- +
    # human-readable so the operator can paste it directly into the
    # Prod handoff.
    canary_only = (os.environ.get("CANARY_ONLY") or "").strip() in {"1", "true", "yes"}
    canary_report_path = os.environ.get("CANARY_REPORT_PATH") or ""

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

    # ── R3 Resume #11 surgical fix: fetch authoritative per-batch
    # manifest for every collection in the session.  This is the
    # ground truth for existing batch IDs and their stored
    # content_hash.  Without this, the driver cannot deterministically
    # reproduce prior batch payloads and will produce
    # ALTERED_REPLAY_REJECTED 409s.
    print("[3b/6] fetching authoritative per-collection batch manifests …")
    #   batch_manifest[coll] = {
    #     batch_no: {
    #       "content_hash": "...",
    #       "status": "succeeded"|"failed"|"in_progress"|"incomplete_write"|"unknown",
    #       "doc_count": int,
    #     }
    #   }
    batch_manifest: dict[str, dict[int, dict]] = {}
    #   inferred_bsize[coll] = int   (original batch size inferred from server)
    inferred_bsize: dict[str, int] = {}
    for coll in RECONCILED_21:
        if filter_set and coll not in filter_set: continue
        code, bm = _get(
            f"{api_base}/api/admin/canonical-import/batch-manifest"
            f"?session_id={session}&collection={coll}",
            headers_get, timeout_s=60)
        if code != 200 or not isinstance(bm, dict):
            print(f"ERROR: batch-manifest endpoint for {coll} failed "
                  f"{code}: {str(bm)[:200]}", file=sys.stderr)
            return 10
        per_batch = {}
        for b in bm.get("batches", []):
            bno = b.get("batch_no")
            if bno is None or bno < 0: continue
            per_batch[int(bno)] = {
                "content_hash": b.get("content_hash"),
                "status":       b.get("status"),
                "doc_count":    int(b.get("doc_count") or 0),
            }
        batch_manifest[coll] = per_batch
        # Infer the ORIGINAL partition size.  The max doc_count seen in
        # any existing batch is a safe lower bound on the original
        # bsize (the last partial batch may be smaller but never
        # larger than the intended bsize).  If no batches exist →
        # genuinely new collection → caller decides new_batch_size.
        max_dc = max((v["doc_count"] for v in per_batch.values()), default=0)
        inferred_bsize[coll] = max_dc  # 0 means "no prior batches"
        if per_batch:
            succ = sum(1 for v in per_batch.values() if v["status"] == "succeeded")
            fail = sum(1 for v in per_batch.values()
                        if v["status"] in {"failed", "incomplete_write"})
            inprog = sum(1 for v in per_batch.values() if v["status"] == "in_progress")
            print(f"[3b/6]   {coll:<30} manifest batches={len(per_batch):<5} "
                  f"succ={succ:<5} failed={fail:<5} in_prog={inprog:<3} "
                  f"max_doc_count={max_dc}")

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
    print("[5/6] planning work (authoritative-manifest-driven) …")

    def _source_for(coll: str) -> pathlib.Path | None:
        p5f = canon5 / f"{coll}.ndjson"
        p6f = canon6 / f"{coll}.ndjson"
        if p6f.exists() and p6f.stat().st_size > 0: return p6f
        if p5f.exists(): return p5f
        return None

    # First pass: count importable rows per collection to compute expected batches.
    plan: list[tuple[str, int, list[dict], str]] = []   # (coll, batch_no, docs, local_hash)
    plan_summary = []
    skip_counts = {"done_collections": 0, "done_batches": 0,
                    "succeeded_batch_local_skips": 0}
    # Preflight hash-match guard results.  If ANY collection has a
    # BATCH_LAYOUT_MISMATCH after planning, we STOP without any POST.
    layout_mismatches: list[dict] = []

    for coll in RECONCILED_21:
        if filter_set and coll not in filter_set: continue
        src = _source_for(coll)
        if src is None:
            plan_summary.append((coll, 0, 0, 0, "no-source")); continue

        pre_s = pre_succeeded.get(coll, 0)
        coll_manifest = batch_manifest.get(coll, {})
        inferred_server_bsize = inferred_bsize.get(coll, 0)
        has_prior_batches = len(coll_manifest) > 0  # ANY existing batch
                                                    # identity (succeeded,
                                                    # failed, in_progress,
                                                    # incomplete_write)

        # ── Earliest-authoritative-batch cutpoint derivation (R3 Resume #15 fix) ──
        #
        # Canary #4 proved that ``max(succeeded doc_counts)`` is NOT
        # safe.  settlement_events now has 1989 succeeded batches
        # with max=1000 — because a later Run-B successfully inserted
        # ONE new batch at the collection tail with doc_count=1000.
        # The ORIGINAL run (which successfully imported batches
        # 0..1987 at doc_count=250) is the one we need to replay
        # against; its authoritative bsize is encoded in the EARLIEST
        # succeeded batches (``batch_no=0`` and its immediate
        # neighbors).
        #
        # Rule:
        #   1. If any succeeded batch exists, take the doc_count of
        #      the LOWEST-NUMBERED succeeded batch_no as the
        #      authoritative historical bsize.  Validate by checking
        #      that at least one of the next few early succeeded
        #      batches agrees (full batches of the ORIGINAL partition
        #      share this value; only the final tail is smaller).
        #   2. If no succeeded batches exist but historical identities
        #      do, use the doc_count of the lowest-numbered batch
        #      (succeeded or not) as a conservative best-effort.  The
        #      preflight hash-match guard remains the final arbiter.
        #   3. If zero historical identities, this is a truly-new
        #      collection → use NEW_COLLECTION_BATCH_SIZE.
        #
        # Rationale for trusting batch_no=0:
        #   - record_batch_begin raises ALTERED_REPLAY_REJECTED if a
        #     new attempt's content_hash differs from the existing
        #     record's.  Once batch_no=0 reaches ``status=succeeded``,
        #     its ``content_hash`` and ``doc_count`` are IMMUTABLE
        #     by server contract (lines 316-332 of
        #     backend/services/canonical_cutover.py).
        #   - The ORIGINAL driver walks NDJSON linearly from batch 0
        #     upward with a fixed bsize; it cannot skip batch 0 or
        #     renumber it.  Hence batch_no=0's doc_count is the
        #     authoritative original bsize for the first partition,
        #     with the sole exception that a collection whose total
        #     fits in a single batch has batch_no=0 as the "tail" —
        #     but that is still correct (the whole collection fits in
        #     one fixed-size batch).
        historical_counts: dict[int, int] = {
            bno: v["doc_count"] for bno, v in coll_manifest.items()
            if v.get("doc_count", 0) > 0
        }
        max_hist_bno = max(historical_counts.keys(),
                           default=-1) if historical_counts else -1

        # Succeeded batches sorted ascending by batch_no.
        succeeded_by_bno = sorted(
            [(bno, v["doc_count"]) for bno, v in coll_manifest.items()
             if v.get("status") == "succeeded"
             and v.get("doc_count", 0) > 0]
        )
        # For observability: capture first-N doc_counts so operators
        # can audit the derivation in the plan summary + report.
        first_n_succeeded = succeeded_by_bno[:5]

        if succeeded_by_bno:
            earliest_bno, earliest_bsize = succeeded_by_bno[0]
            agreement = sum(1 for _, dc in succeeded_by_bno
                             if dc == earliest_bsize)
            fixed_bsize    = earliest_bsize
            bsize_rationale = (
                f"earliest succeeded batch_no={earliest_bno} has "
                f"doc_count={earliest_bsize}; {agreement}/"
                f"{len(succeeded_by_bno)} succeeded batches "
                f"({100*agreement//len(succeeded_by_bno)}%) agree; "
                f"first-5 by bno=[{', '.join(f'b{b}:{d}' for b,d in first_n_succeeded)}]"
            )
        elif historical_counts:
            # No succeeded batches but historical records exist.
            earliest_bno = min(historical_counts.keys())
            fixed_bsize    = historical_counts[earliest_bno]
            bsize_rationale = (
                f"no succeeded batches; using doc_count of "
                f"lowest-numbered historical batch_no={earliest_bno} "
                f"= {fixed_bsize} (preflight hash guard is the final arbiter)"
            )
        else:
            # Truly new collection (zero manifest entries).
            fixed_bsize    = new_batch_size
            bsize_rationale = f"new_collection_bsize={fixed_bsize}"

        # Materialize batches.
        docs_buffer: list[dict] = []
        batches_this_coll = 0
        this_coll_mismatches: list[dict] = []

        def _flush_batch(bno: int, buf: list[dict]) -> None:
            # Compute server-identical hash on the accepted buffer.
            local_hash = _server_batch_content_hash(coll, buf)
            # Preflight hash-match guard: if this batch_no has an
            # authoritative stored hash, require exact match.
            stored = coll_manifest.get(bno)
            if stored is not None:
                stored_hash = stored.get("content_hash")
                if stored_hash and local_hash != stored_hash:
                    this_coll_mismatches.append({
                        "collection":           coll,
                        "batch_no":             bno,
                        "expected_hash":        stored_hash,
                        "computed_hash":        local_hash,
                        "authoritative_count":  stored.get("doc_count"),
                        "reconstructed_count":  len(buf),
                        "target_bsize_used":    fixed_bsize,
                        "bsize_rationale":      bsize_rationale,
                        "authoritative_status": stored.get("status"),
                    })
                    return  # do NOT add to plan
                # Fast-skip succeeded batches locally — no POST,
                # relying on the authoritative stored hash match.
                if stored.get("status") == "succeeded":
                    skip_counts["succeeded_batch_local_skips"] += 1
                    return
            plan.append((coll, bno, list(buf), local_hash))

        # ── FIXED-size partitioning (same contract as original driver) ──
        # Every batch of this collection consumes exactly
        # ``fixed_bsize`` accepted (post-filter) docs in NDJSON file
        # order.  The final (tail) batch may be smaller.
        for d in _ndjson(src):
            if _is_excluded(d): continue
            docs_buffer.append(d)
            if len(docs_buffer) >= fixed_bsize:
                _flush_batch(batches_this_coll, docs_buffer)
                batches_this_coll += 1
                docs_buffer = []
        if docs_buffer:
            _flush_batch(batches_this_coll, docs_buffer)
            batches_this_coll += 1

        if this_coll_mismatches:
            layout_mismatches.extend(this_coll_mismatches)

        # DONE fast-skip: if every batch of this collection is already
        # succeeded, remove them from the plan entirely.
        remaining_in_plan = [t for t in plan if t[0] == coll]
        if pre_s >= batches_this_coll and batches_this_coll > 0 and not remaining_in_plan:
            skip_counts["done_collections"] += 1
            plan_summary.append((coll, batches_this_coll, pre_s, 0,
                                  f"DONE (all {batches_this_coll} succeeded)"))
        else:
            remaining = len(remaining_in_plan)
            if has_prior_batches:
                note = (f"fixed bsize={fixed_bsize}  "
                        f"[hist bnos 0..{max_hist_bno}  "
                        f"rationale: {bsize_rationale}]")
            else:
                note = f"new collection bsize={fixed_bsize}"
            plan_summary.append((coll, batches_this_coll, pre_s, remaining, note))

    total_in_plan = len(plan)
    print(f"[5/6] plan: {total_in_plan} batch POSTs enqueued "
          f"(done-collections={skip_counts['done_collections']} "
          f"succeeded-batch-local-skips={skip_counts['succeeded_batch_local_skips']} "
          f"layout-mismatches={len(layout_mismatches)})")
    print()
    print(f"{'collection':<30}{'total':>7}{'succ':>7}{'queue':>7}  note")
    print("-" * 100)
    for coll, total, succ, q, note in plan_summary:
        print(f"{coll:<30}{total:>7}{succ:>7}{q:>7}  {note}")
    print()

    # ── Preflight guard: any BATCH_LAYOUT_MISMATCH → STOP LOCALLY ────
    # We refuse to send POSTs that are KNOWN to produce
    # ALTERED_REPLAY_REJECTED on Prod.  The resume driver's job is to
    # faithfully replay the original layout, not to spray known-bad
    # altered replays and hope.
    if layout_mismatches:
        print(f"\n❌ BATCH_LAYOUT_MISMATCH on {len(layout_mismatches)} batch(es) — "
              f"STOP LOCALLY, zero POSTs sent", file=sys.stderr)
        print(f"\n{'collection':<30}{'batch_no':>10} {'expected_hash':<66} "
              f"{'computed_hash':<66} {'exp_cnt':>8} {'rec_cnt':>8} "
              f"{'target':>7} status", file=sys.stderr)
        print("-" * 220, file=sys.stderr)
        for m in layout_mismatches[:50]:
            print(f"{m['collection']:<30}{m['batch_no']:>10} "
                  f"{str(m['expected_hash']):<66} "
                  f"{str(m['computed_hash']):<66} "
                  f"{str(m['authoritative_count']):>8} "
                  f"{str(m['reconstructed_count']):>8} "
                  f"{str(m['target_bsize_used']):>7} "
                  f"{m['authoritative_status']}", file=sys.stderr)
        # Persist a machine-readable mismatch report alongside the
        # (optional) canary report.
        if canary_report_path:
            try:
                mm_path = canary_report_path + ".mismatches.json"
                with open(mm_path, "w") as f:
                    json.dump({"session_id": session,
                                "layout_mismatches": layout_mismatches}, f, indent=2)
                print(f"[mismatch] report written to {mm_path}", file=sys.stderr)
            except Exception as _me:
                print(f"[mismatch] write failed: {_me}", file=sys.stderr)
        shutil.rmtree(workdir, ignore_errors=True)
        return 44

    # ── CANARY_ONLY mode: zero-write hash-match proof ────────────────
    # When CANARY_ONLY=1, we have ALREADY proven every planned batch's
    # local hash matches the server's stored hash (by virtue of
    # getting past the preflight guard above).  Now produce the
    # operator-facing MATCH table limited to the standard canary set.
    if canary_only:
        print("\n==================== CANARY_ONLY — ZERO-WRITE HASH PROOF ====================")
        # Build canary candidates per collection.
        canary_rows: list[dict] = []
        for coll, candidates in _CANARY_FIXED_BATCH_IDS.items():
            if filter_set and coll not in filter_set: continue
            coll_manifest = batch_manifest.get(coll, {})
            # Local plan rows for this coll keyed by bno.
            plan_by_bno = {t[1]: t for t in plan if t[0] == coll}
            for bno in candidates:
                stored = coll_manifest.get(bno)
                if stored is None:
                    canary_rows.append({
                        "collection": coll, "batch_no": bno,
                        "expected_hash": None, "computed_hash": None,
                        "match": None,  # not-yet-created server-side
                        "note": "no server record for batch_no (not yet created)",
                    })
                    continue
                if bno in plan_by_bno:
                    local_hash = plan_by_bno[bno][3]
                else:
                    # Succeeded batch locally skipped — recompute to
                    # produce the canary proof row.  Use the SAME
                    # earliest-authoritative-batch contract as the
                    # main planner (R3 Resume #15 fix): derive bsize
                    # from the LOWEST-numbered succeeded batch, not
                    # from max over succeeded.
                    _tmp_buf: list[dict] = []
                    src = _source_for(coll)
                    if src is not None:
                        coll_mf = batch_manifest.get(coll, {})
                        _succ_sorted = sorted(
                            [(b, v["doc_count"]) for b, v in coll_mf.items()
                             if v.get("status") == "succeeded"
                             and v.get("doc_count", 0) > 0]
                        )
                        _hist_sorted = sorted(
                            [(b, v["doc_count"]) for b, v in coll_mf.items()
                             if v.get("doc_count", 0) > 0]
                        )
                        if _succ_sorted:
                            _fixed_bsize = _succ_sorted[0][1]
                        elif _hist_sorted:
                            _fixed_bsize = _hist_sorted[0][1]
                        else:
                            _fixed_bsize = new_batch_size
                        _idx = 0
                        _buf: list[dict] = []
                        for d in _ndjson(src):
                            if _is_excluded(d): continue
                            _buf.append(d)
                            if len(_buf) >= _fixed_bsize:
                                if _idx == bno:
                                    _tmp_buf = list(_buf); break
                                _idx += 1; _buf = []
                        else:
                            if _buf and _idx == bno:
                                _tmp_buf = list(_buf)
                    local_hash = _server_batch_content_hash(coll, _tmp_buf)
                stored_hash = stored.get("content_hash")
                match = (stored_hash is not None and local_hash == stored_hash)
                canary_rows.append({
                    "collection":     coll,
                    "batch_no":       bno,
                    "expected_hash":  stored_hash,
                    "computed_hash":  local_hash,
                    "match":          match,
                    "status":         stored.get("status"),
                })
        # picks: first unfinished boundary.
        for coll in _CANARY_FIRST_UNFINISHED:
            if filter_set and coll not in filter_set: continue
            coll_manifest = batch_manifest.get(coll, {})
            # First batch_no with status != succeeded (ascending).
            first_unfin = None
            for bno in sorted(coll_manifest.keys()):
                if coll_manifest[bno].get("status") != "succeeded":
                    first_unfin = bno; break
            if first_unfin is None:
                canary_rows.append({"collection": coll, "batch_no": None,
                                     "expected_hash": None, "computed_hash": None,
                                     "match": True,
                                     "note": "no unfinished batch (fully succeeded)"})
                continue
            stored = coll_manifest.get(first_unfin, {})
            plan_by_bno = {t[1]: t for t in plan if t[0] == coll}
            local_hash = None
            if first_unfin in plan_by_bno:
                local_hash = plan_by_bno[first_unfin][3]
            canary_rows.append({
                "collection":     coll,
                "batch_no":       first_unfin,
                "expected_hash":  stored.get("content_hash"),
                "computed_hash":  local_hash,
                "match":          (local_hash is not None and local_hash == stored.get("content_hash")),
                "status":         stored.get("status"),
                "note":           "first unfinished boundary",
            })

        # Pretty-print the operator table.
        print(f"\n{'collection':<30}{'batch_id':>10} {'expected_hash':<66} "
                f"{'computed_hash':<66} MATCH")
        print("-" * 180)
        all_match = True
        for r in canary_rows:
            m = r.get("match")
            tag = "MATCH" if m is True else ("MISMATCH" if m is False else "N/A")
            if m is False: all_match = False
            print(f"{r['collection']:<30}"
                  f"{str(r.get('batch_no') if r.get('batch_no') is not None else '-'):>10} "
                  f"{str(r.get('expected_hash') or '-'):<66} "
                  f"{str(r.get('computed_hash') or '-'):<66} {tag}"
                  + (f"  ({r['note']})" if r.get("note") else ""))

        canary_report = {
            "schema":             "r3_batch_layout_canary_v1",
            "generated_at_unix":  int(time.time()),
            "session_id":         session,
            "api_target_redacted": _redact(api_base),
            "canary_rows":        canary_rows,
            "all_match":          all_match,
            "total_batches_planned": total_in_plan,
            "layout_mismatches":  layout_mismatches,  # [] after preflight pass
        }
        if canary_report_path:
            try:
                with open(canary_report_path, "w") as f:
                    json.dump(canary_report, f, indent=2)
                print(f"\n[canary] report written to {canary_report_path}")
            except Exception as _ce:
                print(f"[canary] report write failed: {_ce}", file=sys.stderr)

        shutil.rmtree(workdir, ignore_errors=True)
        if not all_match:
            print("\n❌ CANARY FAIL — at least one hash mismatch", file=sys.stderr)
            return 45
        print("\n✅ CANARY PASS — zero writes, all planned batches hash-verified")
        return 0

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
    # Benchmark-mode instrumentation: opt-in via BENCHMARK_METRICS_PATH env.
    # Captures per-batch wall-clock latencies, HTTP retries/timeouts/
    # resets.  Does NOT change any write path or server contract.
    bench_metrics_path = os.environ.get("BENCHMARK_METRICS_PATH") or ""
    http_metrics = {
        "http_attempts_total": 0, "http_retries": 0,
        "http_timeouts": 0, "http_connection_resets": 0,
        "http_http_errors": 0,
    }
    batch_latencies_ms: list[float] = []
    failed_tasks: list[tuple[str, int, list[dict], str]] = []
    last_success_ts = time.time()
    stop_requested = threading.Event()
    wall_deadline = t0 + wall_clock_budget * 60

    def _submit_one(coll: str, bno: int, docs: list[dict]) -> dict:
        if stop_requested.is_set():
            return {"code": -1, "result": "stop_requested", "coll": coll, "bno": bno, "n": len(docs), "elapsed_ms": 0.0}
        t_start = time.time()
        code, result = _post(
            f"{api_base}/api/admin/canonical-import",
            headers_post,
            {"session_id":         session,
             "collection":         coll,
             "batch_no":           bno,
             "docs":               docs,
             "source_checkpoints": source_cps},
            retries=request_retries,
            timeout_s=request_timeout_s,
            metrics=http_metrics)
        elapsed_ms = (time.time() - t_start) * 1000.0
        with lock:
            batch_latencies_ms.append(elapsed_ms)
        return {"code": code, "result": result, "coll": coll, "bno": bno,
                "n": len(docs), "elapsed_ms": elapsed_ms}

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
                c, b, d, _lh = next(plan_iter)
                pending_futs[pool.submit(_submit_one, c, b, d)] = (c, b, d, _lh)
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
                    c, b, d, _lh = next(plan_iter)
                    pending_futs[pool.submit(_submit_one, c, b, d)] = (c, b, d, _lh)
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
            futures = {pool.submit(_submit_one, c, b, d): (c, b, d, _lh)
                        for c, b, d, _lh in failed_tasks}
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

    # Emit benchmark metrics JSON if BENCHMARK_METRICS_PATH is set.
    # Content is lossless summary + percentiles for the orchestrator
    # to consume.  Nothing is written to canonical collections.
    if bench_metrics_path:
        try:
            def _pct(xs, p):
                if not xs: return 0.0
                xs2 = sorted(xs)
                k = max(0, min(len(xs2) - 1, int(round((p / 100.0) * (len(xs2) - 1)))))
                return float(xs2[k])
            # Non-replay batch latencies (replays are near-instant fast-skip
            # on the server and distort the distribution).  Both views
            # emitted so the operator can compare.
            all_lats = list(batch_latencies_ms)
            summary = {
                "schema":                "benchmark_push_v1",
                "generated_at_unix":     int(time.time()),
                "elapsed_s":             round(total_elapsed, 2),
                "concurrency":           concurrency,
                "batch_size_new":        new_batch_size,
                "batch_size_partial":    max_batch_default,
                "wall_clock_budget_min": wall_clock_budget,
                "total_batches_planned": total_in_plan,
                "batches_committed":     counters["succeeded"],
                "batches_replayed":      counters["replayed"],
                "batches_failed_pass1":  len(failed_tasks),
                "docs_accepted":         counters["accepted"],
                "docs_rejected":         counters["rejected"],
                "pass1_rate_bpm":        round(final_rate, 2),
                "latency_ms": {
                    "n":    len(all_lats),
                    "min":  round(min(all_lats), 1) if all_lats else 0.0,
                    "max":  round(max(all_lats), 1) if all_lats else 0.0,
                    "mean": round(sum(all_lats) / len(all_lats), 1) if all_lats else 0.0,
                    "p50":  round(_pct(all_lats, 50), 1),
                    "p95":  round(_pct(all_lats, 95), 1),
                    "p99":  round(_pct(all_lats, 99), 1),
                },
                "http":                  dict(http_metrics),
                "wall_clock_budget_hit": budget_hit,
            }
            with open(bench_metrics_path, "w") as f:
                json.dump(summary, f, indent=2)
            print(f"[benchmark] metrics written to {bench_metrics_path}")
        except Exception as _berr:
            print(f"[benchmark] metrics write failed: {_berr}", file=sys.stderr)

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
