"""r3_dual_path_forensic — ZERO-WRITE two-driver byte-level diff for
one (collection, batch_no) pair.

Context (Forensic #1)
─────────────────────
Single-batch forensic #1 on ``settlement_events`` batch 5:
  expected_hash = 93667abc…
  computed_hash = ae638b6b…
  expected_count == reconstructed_count == 250
  exclusion_count_up_to_batch == 0
  checkpoint_drift = False      (server_source_checkpoints == {})
  all_logical_keys_null_sample = False

So:
  • batch SIZE is right
  • batch COUNT is right
  • NDJSON SHA on disk matches pinned
  • mismatch is in exact payload CONSTRUCTION
    (membership, ordering, or serialization)

What this forensic does
───────────────────────
1. Executes the ORIGINAL driver's code path EXACTLY — loaded as an
   importable module from ``reconcile_workspace/scripts/
   push_canonical_to_production.py``.  Uses its OWN
   ``_ndjson_rows`` + ``_is_excluded`` + append-chunk-at-batch_size
   loop with ``batch_size=250`` (the only size that produces
   ``doc_count=250`` for the batch).  No inference from the
   accelerated driver — we invoke the ORIGINAL code's symbols
   directly.
2. Executes the CURRENT accelerated driver's code path EXACTLY —
   fetches the authoritative per-batch manifest, uses per-batch
   authoritative doc_count for membership.  Captures the resulting
   250-doc buffer.
3. Fetches the server's expected ``content_hash`` from the
   authoritative batch manifest.
4. Byte-level diffs the two buffers:
     • membership (set-equality of inner-doc ``_id``)
     • ordering (first index with a differing doc)
     • per-doc canonical-JSON byte comparison
5. Also tries alternative orderings on the ORIGINAL buffer in case
   the ORIGINAL successful import sorted docs before hashing (the
   remaining hypothesis C — "original importer sorted differently
   before batching").  Candidate orderings:
     • raw NDJSON order           (what both drivers do)
     • sorted by inner-doc ``_id``
     • sorted by inner-doc ``event_id``
     • sorted by first-element of outer-wrapper ``logical_key``
     • sorted by ``extract_logical_key`` then stable-by-``_id``
6. Reports:
     • original_driver_hash  (raw NDJSON order)
     • accelerated_driver_hash
     • server_expected_hash
     • per-ordering hash candidate table
     • first_differing_index (orig vs accel)
     • first_differing_logical_key / _id / event_id
     • membership diff
     • order diff
     • serialization comparison (byte repr for one differing doc)

Zero-write guarantee
────────────────────
Only ``GET /api/admin/canonical-import/batch-manifest`` is called.
No ``/canonical-import`` POST.  No mutation of any kind.

Exit codes
──────────
    0  — at least one ordering candidate matches server's expected
         hash → forensic concluded + prescribed fix is in the report
   30  — no candidate matches → root cause is NOT ordering alone;
         report flags "needs deeper membership investigation"
   10+ — pre-flight / login / HTTP failure
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


# ─── Portable path resolution (NEVER hardcode /app) ──────────────────
_SCRIPT_DIR            = pathlib.Path(__file__).resolve().parent
_ORIGINAL_DRIVER_PATH  = _SCRIPT_DIR / "push_canonical_to_production.py"
_ACCEL_DRIVER_PATH     = _SCRIPT_DIR / "push_canonical_accelerated.py"
if not _ORIGINAL_DRIVER_PATH.exists():
    raise FileNotFoundError(
        f"Original driver not found at {_ORIGINAL_DRIVER_PATH}.  "
        "Place r3_dual_path_forensic.py in the same directory as "
        "push_canonical_to_production.py."
    )
if not _ACCEL_DRIVER_PATH.exists():
    raise FileNotFoundError(
        f"Accelerated driver not found at {_ACCEL_DRIVER_PATH}."
    )


def _import(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Original driver module (loaded via its OWN file, zero inference
# from the accelerated driver).
_orig = _import("push_canonical_to_production", _ORIGINAL_DRIVER_PATH)

# Accelerated driver module (for its hash helper + manifest-aware
# per-batch target reconstruction).
_accel = _import("push_canonical_accelerated", _ACCEL_DRIVER_PATH)


CHKP_DIR = pathlib.Path(os.environ.get("CHKP_DIR") or "/tmp/perklocks-checkpoints")


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"ERROR: env var {k} not set", file=sys.stderr); sys.exit(2)
    return v


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url); h = (p.hostname or "?").split(".")
    if len(h) >= 3: h[0] = "*"
    return ".".join(h) + (f":{p.port}" if p.port else "")


def _post(url, headers, body, timeout=120):
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


def _get(url, headers, timeout=120):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:800]
        try: return e.code, json.loads(txt)
        except Exception: return e.code, txt


def _sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def _extract(tarball: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as t:
        t.extractall(dest)
    for c in dest.iterdir():
        if c.is_dir():
            return c
    return dest


# ─── Server-identical batch_content_hash + variants ──────────────────
# ``_accel._server_batch_content_hash`` is the proven-parity replica
# of ``services.canonical_cutover.batch_content_hash``.  We reuse it
# for ALL hash candidates so the only variable is docs/ordering.
def _hash(coll: str, docs: list[dict]) -> str:
    return _accel._server_batch_content_hash(coll, docs)


def _canonical_bytes(coll: str, doc: dict) -> bytes:
    """Byte representation of a single doc as the server would hash
    it (inside batch_content_hash): ``json.dumps([list(lk), doc],
    default=str, sort_keys=True)``.  Used for per-doc byte-level
    diffing."""
    lk = tuple(doc.get(f) for f in _accel._logical_key_fields(coll))
    return json.dumps([list(lk), doc], default=str, sort_keys=True).encode()


# ─── Original-driver reconstruction (uses ORIGINAL driver's symbols) ─
def _original_batch_buffer(coll: str, src: pathlib.Path, bno: int,
                            bsize: int) -> list[dict]:
    """Execute the ORIGINAL driver's exact batching loop (lines 288-326
    of push_canonical_to_production.py: default-size append + flush at
    ``len(batch) >= batch_size``), using the ORIGINAL driver's own
    ``_ndjson_rows`` + ``_is_excluded`` symbols — zero inference from
    the accelerated driver.

    Returns the raw buffer (list[dict]) that the ORIGINAL driver would
    have POSTed as ``req.docs`` for batch ``bno``.
    """
    cur_bno = 0
    buf: list[dict] = []
    for doc in _orig._ndjson_rows(src):
        if _orig._is_excluded(doc):
            continue
        buf.append(doc)
        if len(buf) >= bsize:
            if cur_bno == bno:
                return list(buf)
            cur_bno += 1
            buf = []
    if cur_bno == bno:
        return list(buf)
    return []


# ─── Accelerated-driver reconstruction (uses accelerated symbols) ────
def _accelerated_batch_buffer(coll: str, src: pathlib.Path, bno: int,
                               historical_counts: dict[int, int],
                               tail_bsize: int) -> list[dict]:
    """Execute the CURRENT accelerated driver's per-batch
    authoritative-manifest reconstruction (Resume #12 contract)."""
    max_hb = max(historical_counts.keys(), default=-1)

    def _tgt(b: int) -> int:
        return historical_counts.get(b, tail_bsize) if b <= max_hb else tail_bsize

    cur_bno = 0
    cur_t   = _tgt(cur_bno)
    buf: list[dict] = []
    for doc in _accel._ndjson(src):
        if _accel._is_excluded(doc):
            continue
        buf.append(doc)
        if len(buf) >= cur_t:
            if cur_bno == bno:
                return list(buf)
            cur_bno += 1
            buf = []
            cur_t = _tgt(cur_bno)
    if cur_bno == bno:
        return list(buf)
    return []


# ─── Ordering candidates for the hypothesis-C sweep ──────────────────
def _ordering_candidates(coll: str, buf: list[dict],
                          src: pathlib.Path) -> dict[str, list[dict]]:
    """Return a dict mapping ordering-name → ordered doc list.
    All candidates use the SAME 250 docs (membership of ``buf``)
    with different sort orders.  Hypothesis C — the original
    importer sorted docs before batching — is tested by hashing
    each candidate.

    One extra "outer_logical_key" candidate uses the outer NDJSON
    wrapper's ``logical_key`` field (not the inner doc), which may
    be where the historical importer sourced its sort key.
    """
    # Precompute outer-wrapper logical_key for each inner-doc _id.
    inner_ids_in_buf = {d.get("_id") for d in buf}
    outer_lk_by_id: dict[str, list] = {}
    with open(src) as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            d = r.get("doc") if isinstance(r, dict) and "doc" in r else r
            if d.get("_id") in inner_ids_in_buf:
                outer_lk_by_id[d.get("_id")] = r.get("logical_key") if isinstance(r, dict) else None
                if len(outer_lk_by_id) == len(inner_ids_in_buf):
                    break

    def _by_inner_id(d):           return str(d.get("_id"))
    def _by_event_id(d):           return str(d.get("event_id"))
    def _by_logical_key(d):
        lk = _accel._extract_logical_key(coll, d)
        return tuple(str(x) for x in lk) + (str(d.get("_id")),)  # stable
    def _by_outer_wrapper_lk(d):
        olk = outer_lk_by_id.get(d.get("_id")) or []
        return tuple(str(x) for x in olk) + (str(d.get("_id")),)

    return {
        "raw_ndjson_order":           list(buf),
        "sorted_by_inner__id":        sorted(buf, key=_by_inner_id),
        "sorted_by_event_id":         sorted(buf, key=_by_event_id),
        "sorted_by_extract_logical_key": sorted(buf, key=_by_logical_key),
        "sorted_by_outer_wrapper_logical_key": sorted(buf, key=_by_outer_wrapper_lk),
        "reversed_ndjson_order":      list(reversed(buf)),
    }


def main() -> int:
    api_base = _require("PROD_API_BASE").rstrip("/")
    email    = _require("PROD_ADMIN_EMAIL")
    password = _require("PROD_ADMIN_PASSWORD")
    session  = _require("CANONICAL_IMPORT_SESSION")

    coll        = os.environ.get("FORENSIC_COLLECTION", "settlement_events")
    bno         = int(os.environ.get("FORENSIC_BATCH_NO", "5"))
    orig_bsize  = int(os.environ.get("FORENSIC_ORIGINAL_BATCH_SIZE", "250"))
    report_path = os.environ.get("FORENSIC_REPORT_PATH") or \
                   "/tmp/perklocks-logs/r3_dual_path_forensic.json"
    pathlib.Path(report_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Auth + manifest fetch (ZERO-WRITE) ───────────────────────────
    print(f"[auth] POST {_redact(api_base)}/api/auth/login")
    code, body = _post(f"{api_base}/api/auth/login", {},
                        {"email": email, "password": password})
    if code != 200 or not body.get("access_token"):
        print(f"ERROR login status={code}", file=sys.stderr); return 10
    jwt   = body["access_token"]
    hdr_a = {"Authorization": f"Bearer {jwt}"}

    print(f"[manifest] GET /batch-manifest coll={coll}")
    code, bm = _get(
        f"{api_base}/api/admin/canonical-import/batch-manifest"
        f"?session_id={session}&collection={coll}", hdr_a, timeout=60)
    if code != 200:
        print(f"ERROR manifest status={code}", file=sys.stderr); return 11
    batches_by_bno = {int(b["batch_no"]): b for b in bm.get("batches", [])}
    target = batches_by_bno.get(bno)
    if target is None:
        print(f"ERROR: no manifest entry for {coll} batch_no={bno}",
              file=sys.stderr); return 12
    server_expected_hash = target["content_hash"]
    server_expected_count = int(target.get("doc_count") or 0)
    print(f"[manifest] server_expected_hash  = {server_expected_hash}")
    print(f"[manifest] server_expected_count = {server_expected_count}")

    # ── Extract pinned checkpoints ───────────────────────────────────
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="dual_forensic_"))
    try:
        p5_root = _extract(CHKP_DIR / "phase5_20261003_190628Z.tar.gz", workdir / "p5")
        p6_root = _extract(CHKP_DIR / "phase6_20261003_192150Z.tar.gz", workdir / "p6")
        canon5 = p5_root / "canonical"
        canon6 = p6_root / "canonical"
        p5f = canon5 / f"{coll}.ndjson"
        p6f = canon6 / f"{coll}.ndjson"
        if p6f.exists() and p6f.stat().st_size > 0:
            src = p6f; src_tag = "phase6_overlay"
        else:
            src = p5f; src_tag = "phase5"
        src_sha = _sha256(src)
        print(f"[source] file={src.name} tag={src_tag} sha={src_sha} "
              f"total_lines={sum(1 for _ in open(src))}")

        # ── PATH A: original driver's buffer for batch bno ───────────
        print(f"\n[PATH A] original driver batching @ batch_size={orig_bsize}")
        buf_orig = _original_batch_buffer(coll, src, bno, orig_bsize)
        hash_orig = _hash(coll, buf_orig)
        print(f"[PATH A] count={len(buf_orig)}  hash={hash_orig}")

        # ── PATH B: accelerated driver's buffer for batch bno ────────
        print(f"\n[PATH B] accelerated driver per-batch authoritative reconstruction")
        historical_counts = {b: v["doc_count"] for b, v in batches_by_bno.items()
                              if v.get("doc_count", 0) > 0}
        succ_sizes = [v["doc_count"] for v in batches_by_bno.values()
                       if v.get("status") == "succeeded"
                       and v.get("doc_count", 0) > 0]
        tail_bsize = max(succ_sizes) if succ_sizes else (
            max(historical_counts.values()) if historical_counts else 1000)
        buf_accel = _accelerated_batch_buffer(coll, src, bno,
                                               historical_counts, tail_bsize)
        hash_accel = _hash(coll, buf_accel)
        print(f"[PATH B] count={len(buf_accel)}  hash={hash_accel}")

        # ── Byte-level diff A vs B ───────────────────────────────────
        print(f"\n[diff A vs B] running byte-level comparison")
        ids_a = [d.get("_id") for d in buf_orig]
        ids_b = [d.get("_id") for d in buf_accel]
        set_a, set_b = set(ids_a), set(ids_b)
        only_in_a = sorted(set_a - set_b)
        only_in_b = sorted(set_b - set_a)
        same_membership = (not only_in_a and not only_in_b)
        first_order_diff_idx = None
        first_order_diff_a   = None
        first_order_diff_b   = None
        for i, (x, y) in enumerate(zip(buf_orig, buf_accel)):
            if _canonical_bytes(coll, x) != _canonical_bytes(coll, y):
                first_order_diff_idx = i
                first_order_diff_a   = x
                first_order_diff_b   = y
                break
        # Serialization/byte diff for first-differing doc or doc 0.
        probe_a = buf_orig[0]  if buf_orig  else {}
        probe_b = buf_accel[0] if buf_accel else {}
        probe_sample = {
            "probe_idx":            0 if first_order_diff_idx is None else first_order_diff_idx,
            "a_canonical_bytes":    _canonical_bytes(coll, first_order_diff_a or probe_a).decode(errors="replace")[:500],
            "b_canonical_bytes":    _canonical_bytes(coll, first_order_diff_b or probe_b).decode(errors="replace")[:500],
            "a__id":                (first_order_diff_a or probe_a).get("_id"),
            "b__id":                (first_order_diff_b or probe_b).get("_id"),
            "a_logical_key":        list(_accel._extract_logical_key(coll, first_order_diff_a or probe_a)),
            "b_logical_key":        list(_accel._extract_logical_key(coll, first_order_diff_b or probe_b)),
        }

        # ── Hypothesis-C ordering sweep (on original buffer) ─────────
        print(f"\n[orderings] trying variants on PATH A buffer")
        candidates = _ordering_candidates(coll, buf_orig, src)
        ordering_results = []
        matched_candidate = None
        for name, ordered in candidates.items():
            h = _hash(coll, ordered)
            match = (h == server_expected_hash)
            ordering_results.append({"ordering": name, "hash": h, "match": match})
            print(f"  {name:<42}  hash={h}  "
                  f"MATCH={'YES' if match else 'no'}")
            if match and matched_candidate is None:
                matched_candidate = name

        # Also try orderings on the ACCELERATED buffer (should mirror
        # PATH A if membership is identical, but we test anyway).
        accel_candidates = _ordering_candidates(coll, buf_accel, src)
        accel_ordering_results = []
        for name, ordered in accel_candidates.items():
            h = _hash(coll, ordered)
            match = (h == server_expected_hash)
            accel_ordering_results.append({"ordering": name, "hash": h, "match": match})

        print(f"\n[verdict] server_expected_hash   = {server_expected_hash}")
        print(f"[verdict] original_driver_hash    = {hash_orig}  "
              f"MATCH={'YES' if hash_orig == server_expected_hash else 'no'}")
        print(f"[verdict] accelerated_driver_hash = {hash_accel}  "
              f"MATCH={'YES' if hash_accel == server_expected_hash else 'no'}")
        print(f"[verdict] same_membership (A==B)  = {same_membership}  "
              f"only_in_A={len(only_in_a)}  only_in_B={len(only_in_b)}")
        print(f"[verdict] first_order_diff_idx    = {first_order_diff_idx}")
        print(f"[verdict] matched_candidate       = {matched_candidate}")

        verdict = []
        if hash_orig == server_expected_hash:
            verdict.append(
                "PATH A (original driver) hash matches server — port its "
                "exact batching loop/order into the accelerated driver."
            )
        elif hash_accel == server_expected_hash:
            verdict.append(
                "PATH B (accelerated driver) hash matches server — no "
                "change needed; verify why Canary #3 reported mismatch."
            )
        elif matched_candidate:
            verdict.append(
                f"Ordering candidate '{matched_candidate}' matches server. "
                "Port that sort into the accelerated driver before "
                "flushing each batch for settlement_events."
            )
        else:
            if not same_membership:
                verdict.append(
                    "PATH A and PATH B have DIFFERENT membership — "
                    f"only_in_A={len(only_in_a)} only_in_B={len(only_in_b)}. "
                    "Root cause is upstream of batching (NDJSON contents "
                    "or filter chain)."
                )
            else:
                verdict.append(
                    "PATH A and PATH B have IDENTICAL membership AND no "
                    "tested ordering matches server. Remaining hypotheses: "
                    "(1) server received docs in an order NOT produced by "
                    "either NDJSON walk — e.g., sorted at Mongo query time "
                    "during the ORIGINAL phase5 export, which produces a "
                    "different NDJSON file whose SHA was never recorded "
                    "(empty server_source_checkpoints is consistent with "
                    "this); (2) server-side pre-hash transformation we "
                    "haven't modeled.  Next step: inspect the ORIGINAL "
                    "phase5 exporter + any persisted push logs for the "
                    "exact ordered record_ids posted for batch_no={bno}."
                )

        report = {
            "schema":              "r3_dual_path_forensic_v1",
            "generated_at_unix":   int(time.time()),
            "api_target_redacted": _redact(api_base),
            "session_id":          session,
            "collection":          coll,
            "batch_no":            bno,
            "server_expected_hash":   server_expected_hash,
            "server_expected_count":  server_expected_count,
            "original_driver_hash":   hash_orig,
            "original_driver_count":  len(buf_orig),
            "original_batch_size":    orig_bsize,
            "accelerated_driver_hash":   hash_accel,
            "accelerated_driver_count":  len(buf_accel),
            "same_membership":          same_membership,
            "only_in_original_path":    only_in_a[:50],
            "only_in_accelerated_path": only_in_b[:50],
            "first_order_diff_idx":     first_order_diff_idx,
            "probe_sample":             probe_sample,
            "ordering_results_on_original":    ordering_results,
            "ordering_results_on_accelerated": accel_ordering_results,
            "matched_ordering_candidate":      matched_candidate,
            "ndjson_source":            {
                "file": src.name, "tag": src_tag, "sha256": src_sha,
                "total_lines": sum(1 for _ in open(src)),
            },
            "verdict":                  verdict,
        }
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n[report] written to {report_path}")
        return 0 if (hash_orig == server_expected_hash
                     or hash_accel == server_expected_hash
                     or matched_candidate) else 30
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
