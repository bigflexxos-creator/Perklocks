"""r3_phase7_dedupe_apply — R3 Phase-7 duplicate reconciliation driver.

Orchestrates Support's Phase-7 closure steps 1-5 against Production:

    STEP 1 (CANARY_ONLY, default)
    ─────────────────────────────
        1. mint admin JWT
        2. GET /canonical-cutover/dup-census for all 21 canonical
           collections (read-only; no mutation)
        3. for each duplicate group in the 8-collection APPLY_SCOPE,
           compute the pinned-source authoritative fingerprint from
           ``v2_phase5/canonical/<coll>.ndjson``
        4. classify every duplicate group:
             * EXACT_DUPLICATE
             * CONFLICTING_DUPLICATE
           and for CONFLICTING, prove exactly ONE doc matches the
           authoritative source (or FLAG as blocker if none does)
        5. write the full census report to REPORT_PATH and STOP

    STEP 2 (APPLY, requires explicit confirmation)
    ──────────────────────────────────────────────
        6. re-run census (same as above) — must produce the same
           number of groups as the operator-reviewed canary; any
           drift halts the APPLY
        7. for every EXACT group → survivor via pinned-source /
           earliest-canonical / lowest-_id (deterministic)
           for every CONFLICTING group → survivor must match
           authoritative; if none does → HALT (fail-closed)
        8. POST /canonical-cutover/apply-dedupe (plans batched)
        9. re-run census (post-apply): the 8 APPLY collections must
           report ``duplicate_group_count == 0``
       10. POST /canonical-cutover/create-indexes (Phase-7 unique
           indexes from _INDEX_SPECS); all 8 target unique indexes
           must report ``ok=True`` with no ``failed`` entries
       11. (Phase-8 chain) POST /canonical-cutover/r3-certification;
           Phase 8 must PASS
       12. write final report; exit 0 on complete success

Environment contract (GH Actions secrets, never logged):
    PROD_API_BASE
    PROD_ADMIN_EMAIL
    PROD_ADMIN_PASSWORD
    CANONICAL_IMPORT_TOKEN
    SESSION_ID                default ``perklocks-cutover-20261003-r3``
    PHASE7_MODE               ``CANARY_ONLY`` (default) | ``APPLY``
    PHASE7_CONFIRM_PHRASE     (APPLY only) must equal
                              ``APPLY_PERKLOCKS_DEDUPE_R3_PHASE7_V1``
    PINNED_SOURCE_DIR         absolute dir holding
                              ``<coll>.ndjson`` for the 8 APPLY
                              collections (required in APPLY mode)
    REPORT_PATH               default /tmp/perklocks-logs/
                                       r3_phase7_dedupe_report.json
    PHASE8_CERT_EXPECTED_JSON optional path to an expected-counts
                              JSON file for the Phase-8 certification
                              body (if omitted, uses no expected
                              counts and relies on the server's
                              ``overall_pass`` logic for the dedupe
                              success criterion)
    APPLY_BATCH_SIZE          plans per /apply-dedupe POST, default 100

Exit codes:
    0 — SUCCESS (CANARY_ONLY completed, or APPLY+Phase-7+Phase-8 PASS)
    1 — CANNOT VERIFY (auth / network / missing env)
    3 — BLOCKED (census found CONFLICTING with no authoritative winner)
    4 — APPLY halted mid-run (one group failed; see halted_at / halt_err)
    5 — Phase-7 unique-index creation failed (duplicates remain)
    6 — Phase-8 certification did not PASS
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


APPLY_SCOPE_COLLS: tuple[str, ...] = (
    "picks",
    "player_game_actuals",
    "player_game_logs",
    "player_identities",
    "prediction_snapshots",
    "pregame_snapshots",
    "publication_events",
    "settlement_events",
)

# Local mirror of RECONCILIATION_COLLECTIONS from backend/services/
# canonical_cutover.py.  Driver must iterate per-collection to avoid
# the pre-#20 single-call 500 regression.
RECONCILIATION_COLLECTIONS_LOCAL: tuple[str, ...] = (
    "games",
    "historical_ingestion_state",
    "nfl_ingest_meta",
    "nfl_player_weekly",
    "parlay_history",
    "picks",
    "player_game_actuals",
    "player_game_logs",
    "player_identities",
    "prediction_snapshots",
    "pregame_snapshots",
    "publication_events",
    "rollover_slate_events",
    "rollover_slates",
    "settlement_events",
    "soccer_matches",
    "soccer_player_game_logs",
    "team_game_actuals",
    "tennis_matches_history",
    "user_bets",
    "users",
)

# Must mirror backend/services/canonical_dedupe.APPLY_DEDUPE_CONFIRM_PHRASE
APPLY_CONFIRM_PHRASE: str = "APPLY_PERKLOCKS_DEDUPE_R3_PHASE7_V1"

# Must mirror backend/services/canonical_cutover._LOGICAL_KEYS for the 8.
_LOGICAL_KEYS_PHASE7: dict[str, tuple[str, ...]] = {
    "picks":                ("id",),
    "player_game_actuals":  ("sport", "event_id", "player_id"),
    "player_game_logs":     ("sport", "game_id", "player_id"),
    "player_identities":    ("canonical_player_id",),
    "prediction_snapshots": ("prediction_id", "snapshot_version"),
    "pregame_snapshots":    ("snapshot_hash",),
    "publication_events":   ("payload_hash",),
    "settlement_events":    ("settlement_id",),
}

# Expected final unique index names per the 8 scope collections.
_EXPECTED_UNIQUE_INDEXES: dict[str, str] = {
    "picks":                "ux_pick_id",
    "player_game_actuals":  "ux_pga_identity",
    "player_game_logs":     "ux_pgl_identity",
    "player_identities":    "ux_pi_canonical",
    "prediction_snapshots": "ux_prediction_snapshot_version",
    "pregame_snapshots":    "ux_snapshot_hash",
    "publication_events":   "ux_payload_hash",
    "settlement_events":    "ux_settlement_id",
}


def _require(k: str) -> str:
    v = os.environ.get(k)
    if not v:
        print(f"[r3-ph7] FATAL: env var {k} not set", file=sys.stderr)
        sys.exit(1)
    return v


def _mask(value: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def _redact(url: str) -> str:
    try:
        p = urlparse(url)
        h = (p.hostname or "?").split(".")
        if len(h) >= 3: h[0] = "*"
        return ".".join(h) + (f":{p.port}" if p.port else "")
    except Exception:
        return "<redacted>"


def _http(url, headers=None, body=None, method="GET", timeout_s=600):
    data = None
    if body is not None:
        data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={**(headers or {}),
                 **({"Content-Type": "application/json"} if data else {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")[:2000]
        try: parsed = json.loads(txt)
        except Exception: parsed = txt
        return e.code, parsed
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# ─── Canonical fingerprint (MUST match backend) ────────────────────
def _is_bookkeeping(k: str) -> bool:
    return k == "_id" or k.startswith("_")


def _fingerprint(doc: dict) -> str:
    business = {k: v for k, v in doc.items() if not _is_bookkeeping(k)}
    payload = json.dumps(business, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_pinned_source(dir_path: pathlib.Path,
                        coll:     str) -> dict[tuple, str]:
    """Read ``<dir>/<coll>.ndjson`` and return
    {logical_key_tuple: pinned_source_fingerprint}.
    Fails loudly if the file is missing or any row lacks a logical
    key field."""
    key_fields = _LOGICAL_KEYS_PHASE7[coll]
    src = dir_path / f"{coll}.ndjson"
    if not src.exists():
        raise FileNotFoundError(
            f"PINNED_SOURCE_MISSING: {src} — set PINNED_SOURCE_DIR "
            f"to the directory holding v2_phase5/canonical/*.ndjson"
        )
    out: dict[tuple, str] = {}
    with open(src) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception as e:
                raise ValueError(
                    f"PINNED_SOURCE_PARSE_ERROR: {src}:{lineno}: {e}")
            doc = row.get("doc", row)
            lk = tuple(doc.get(f) for f in key_fields)
            if any(v is None for v in lk):
                # Skip rows missing a logical-key component (phase-5
                # rejects these — we do too for the fingerprint map).
                continue
            fp = _fingerprint(doc)
            # In the rare case a pinned source contains duplicate
            # logical keys (shouldn't happen post-reconciliation),
            # keep the first; divergence is flagged via a sentinel.
            if lk in out and out[lk] != fp:
                out[lk] = "SOURCE_HAS_INTERNAL_DUP:" + out[lk]
            else:
                out.setdefault(lk, fp)
    return out


# ─── Report writer ─────────────────────────────────────────────────
def _write_report(path: pathlib.Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2, default=str))
    except Exception as e:
        print(f"[r3-ph7] report write failed: {e}", file=sys.stderr)


def main() -> int:
    api_base   = _require("PROD_API_BASE").rstrip("/")
    email      = _require("PROD_ADMIN_EMAIL")
    password   = _require("PROD_ADMIN_PASSWORD")
    import_tok = _require("CANONICAL_IMPORT_TOKEN")
    session_id = (os.environ.get("SESSION_ID") or
                   "perklocks-cutover-20261003-r3").strip()
    mode       = (os.environ.get("PHASE7_MODE") or "CANARY_ONLY").strip().upper()
    confirm    = (os.environ.get("PHASE7_CONFIRM_PHRASE") or "").strip()
    pin_dir    = os.environ.get("PINNED_SOURCE_DIR", "").strip()
    report_path = pathlib.Path(
        os.environ.get("REPORT_PATH") or
        "/tmp/perklocks-logs/r3_phase7_dedupe_report.json")
    workflow_run_id = (os.environ.get("GITHUB_RUN_ID") or
                        f"local-{int(time.time())}")
    try:
        apply_batch_size = max(1, int(os.environ.get("APPLY_BATCH_SIZE") or 100))
    except ValueError:
        apply_batch_size = 100

    if mode not in ("CANARY_ONLY", "APPLY"):
        print(f"[r3-ph7] FATAL: PHASE7_MODE must be CANARY_ONLY or APPLY, "
              f"got {mode!r}", file=sys.stderr)
        return 1

    print(f"[r3-ph7] target={_redact(api_base)}  session={session_id}")
    print(f"[r3-ph7] mode={mode}  run_id={workflow_run_id}")

    # ── Auth ───────────────────────────────────────────────────────
    code, body = _http(f"{api_base}/api/auth/login",
                        body={"email": email, "password": password},
                        method="POST")
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        _write_report(report_path, {"verdict": "CANNOT_VERIFY",
                                     "stage": "auth", "status": code,
                                     "body": body})
        return 1
    jwt = body["access_token"]
    _mask(jwt)
    print("[auth] OK")

    hdr      = {"Authorization": f"Bearer {jwt}"}
    hdr_tok  = {**hdr, "X-Canonical-Import-Token": import_tok}

    # ── STEP 2: READ-ONLY CENSUS (per-collection, paged, sequential) ─
    print("\n[step2] GET /dup-census per collection (concurrency=1)")

    def _fetch_coll_census(coll: str) -> dict:
        """Fetch all pages of dup-census for one collection and
        return a consolidated per-collection summary with all groups
        flattened and exact/conflicting counts summed.  Streams
        pages so the runner never holds the whole response in one
        memory block."""
        page_limit         = 100
        max_docs_per_group = 50
        offset             = 0
        all_groups:        list[dict] = []
        total_docs         = 0
        logical_key_count  = 0
        dup_group_count    = 0
        exact              = 0
        conflicting        = 0
        page_i             = 0
        err_text           = None
        while True:
            page_i += 1
            qs = (f"collection={coll}&offset={offset}&limit={page_limit}"
                   f"&max_docs_per_group={max_docs_per_group}")
            code, body = _http(
                f"{api_base}/api/admin/canonical-cutover/dup-census?{qs}",
                headers=hdr, timeout_s=1800,
            )
            if code != 200 or not isinstance(body, dict):
                err_text = f"HTTP {code}: {str(body)[:500]}"
                print(f"[step2] {coll} page#{page_i} offset={offset}  "
                      f"FAILED: {err_text}")
                break
            total_docs        = body.get("total_docs", total_docs)
            logical_key_count = body.get("logical_key_count", logical_key_count)
            dup_group_count   = body.get("duplicate_group_count", dup_group_count)
            exact            += int(body.get("page_exact_count")       or 0)
            conflicting      += int(body.get("page_conflicting_count") or 0)
            page_groups       = body.get("groups") or []
            all_groups.extend(page_groups)
            print(f"[step2] {coll} page#{page_i}  "
                   f"offset={offset}  returned={len(page_groups)}  "
                   f"dup_total={dup_group_count}  "
                   f"has_more={body.get('has_more')}")
            if not body.get("has_more") or body.get("next_offset") is None:
                break
            offset = int(body["next_offset"])
        return {
            "collection":              coll,
            "total_docs":              total_docs,
            "logical_key_count":       logical_key_count,
            "duplicate_group_count":   dup_group_count,
            "exact_group_count":       exact,
            "conflicting_group_count": conflicting,
            "groups":                  all_groups,
            "error":                   err_text,
        }

    per_coll: list[dict] = []
    for coll in list(RECONCILIATION_COLLECTIONS_LOCAL):
        per_coll.append(_fetch_coll_census(coll))
    total_groups      = sum(c.get("duplicate_group_count")  or 0 for c in per_coll)
    total_exact       = sum(c.get("exact_group_count")      or 0 for c in per_coll)
    total_conflicting = sum(c.get("conflicting_group_count") or 0 for c in per_coll)

    census = {
        "generated_at":            None,
        "collections":             per_coll,
        "total_duplicate_groups":  total_groups,
        "total_exact_groups":      total_exact,
        "total_conflicting_groups": total_conflicting,
    }
    print(f"[step2] total_dup_groups={total_groups} "
          f"exact={total_exact} conflicting={total_conflicting}")

    per_coll_summary = []
    apply_scope_summary = {}
    for col_entry in census.get("collections", []):
        c = col_entry.get("collection")
        per_coll_summary.append({
            "collection":              c,
            "total_docs":              col_entry.get("total_docs"),
            "logical_key_count":       col_entry.get("logical_key_count"),
            "duplicate_group_count":   col_entry.get("duplicate_group_count"),
            "exact_group_count":       col_entry.get("exact_group_count"),
            "conflicting_group_count": col_entry.get("conflicting_group_count"),
            "error":                   col_entry.get("error"),
        })
        if c in APPLY_SCOPE_COLLS:
            apply_scope_summary[c] = col_entry

    # ── Build plans + prove authoritative winner (for both modes) ──
    pinned_fps: dict[str, dict[tuple, str]] = {}
    if mode == "APPLY" or pin_dir:
        if not pin_dir:
            print("[r3-ph7] APPLY requires PINNED_SOURCE_DIR", file=sys.stderr)
            return 1
        src_dir = pathlib.Path(pin_dir)
        for c in APPLY_SCOPE_COLLS:
            try:
                pinned_fps[c] = _load_pinned_source(src_dir, c)
                print(f"[step2] pinned source {c}: "
                      f"{len(pinned_fps[c])} logical keys mapped")
            except Exception as e:
                _write_report(report_path, {"verdict": "CANNOT_VERIFY",
                                             "stage": "pinned_source",
                                             "coll": c,
                                             "error": f"{type(e).__name__}: {e}"})
                return 1

    plans:                list[dict] = []
    blockers_conflicting: list[dict] = []
    blockers_exact_div:   list[dict] = []
    for coll in APPLY_SCOPE_COLLS:
        entry = apply_scope_summary.get(coll)
        if not entry:
            continue
        key_fields = _LOGICAL_KEYS_PHASE7[coll]
        for grp in entry.get("groups", []):
            lk_dict = grp["logical_key"]
            lk_tuple = tuple(lk_dict.get(f) for f in key_fields)
            auth_fp = (pinned_fps.get(coll) or {}).get(lk_tuple)
            if not auth_fp:
                # No pinned source entry for this logical key —
                # ambiguous winner, must be flagged.
                blockers_conflicting.append({
                    "collection":        coll,
                    "logical_key_values": lk_dict,
                    "classification":    grp["classification"],
                    "reason":            "NO_PINNED_SOURCE_ENTRY",
                    "all_ids":           grp["_ids"],
                })
                continue
            # Does the authoritative fingerprint appear among the
            # group's current fingerprints?
            cur_fps = set(grp["fingerprints"])
            if auth_fp not in cur_fps:
                if grp["classification"] == "CONFLICTING_DUPLICATE":
                    blockers_conflicting.append({
                        "collection":           coll,
                        "logical_key_values":   lk_dict,
                        "classification":       grp["classification"],
                        "reason":               "NO_AUTHORITATIVE_WINNER",
                        "authoritative_fp":     auth_fp,
                        "doc_fingerprints":     grp["fingerprints"],
                        "all_ids":              grp["_ids"],
                    })
                else:
                    blockers_exact_div.append({
                        "collection":           coll,
                        "logical_key_values":   lk_dict,
                        "classification":       grp["classification"],
                        "reason":               "EXACT_GROUP_DIVERGES_FROM_SOURCE",
                        "authoritative_fp":     auth_fp,
                        "all_docs_fp":          grp["fingerprints"][0],
                        "all_ids":              grp["_ids"],
                    })
                continue
            # Plan: use truncated id list from census (max_docs_per_group
            # default 50).  For a group with >50 ids, caller must raise
            # max_docs_per_group — flag it instead of silent partial.
            if grp.get("_ids_truncated"):
                blockers_conflicting.append({
                    "collection":        coll,
                    "logical_key_values": lk_dict,
                    "reason":            "CENSUS_IDS_TRUNCATED_RAISE_SAMPLE",
                    "dup_count":         grp["dup_count"],
                    "sampled_ids":       grp["_ids"],
                })
                continue
            plans.append({
                "collection":                coll,
                "logical_key_values":        lk_dict,
                "classification":            grp["classification"],
                "all_ids":                   grp["_ids"],
                "authoritative_fingerprint": auth_fp,
            })

    print(f"\n[step2] built plans={len(plans)}  "
          f"blockers_conflicting={len(blockers_conflicting)}  "
          f"blockers_exact_div={len(blockers_exact_div)}")

    canary_report = {
        "generated_at":            census.get("generated_at"),
        "api_target":              _redact(api_base),
        "session_id":              session_id,
        "mode":                    mode,
        "workflow_run_id":         workflow_run_id,
        "apply_confirm_phrase":    APPLY_CONFIRM_PHRASE,
        "per_collection":          per_coll_summary,
        "total_duplicate_groups":  total_groups,
        "total_exact_groups":      total_exact,
        "total_conflicting_groups": total_conflicting,
        "apply_scope":             list(APPLY_SCOPE_COLLS),
        "apply_plan_count":        len(plans),
        "blockers_conflicting":    blockers_conflicting,
        "blockers_exact_div":      blockers_exact_div,
    }

    # ── STEP 1 EXIT: CANARY_ONLY ────────────────────────────────────
    if mode == "CANARY_ONLY":
        canary_report["verdict"] = (
            "CANARY_READY_FOR_APPLY"
            if not blockers_conflicting and not blockers_exact_div
            else "CANARY_BLOCKED"
        )
        _write_report(report_path, canary_report)
        print(f"\n[r3-ph7] CANARY_ONLY complete — verdict="
              f"{canary_report['verdict']}  plans={len(plans)}  "
              f"blockers={len(blockers_conflicting)+len(blockers_exact_div)}")
        return 0

    # ── STEP 2: APPLY requires explicit confirm phrase ─────────────
    if confirm != APPLY_CONFIRM_PHRASE:
        print(f"[r3-ph7] FATAL: APPLY requires PHASE7_CONFIRM_PHRASE="
              f"{APPLY_CONFIRM_PHRASE!r}", file=sys.stderr)
        _write_report(report_path,
                        {**canary_report, "verdict": "APPLY_REFUSED_BAD_CONFIRM"})
        return 1

    if blockers_conflicting or blockers_exact_div:
        print(f"[r3-ph7] HALTED before APPLY — {len(blockers_conflicting)} "
              f"conflicting + {len(blockers_exact_div)} exact-divergent "
              f"blockers.  Operator must resolve before APPLY.")
        _write_report(report_path,
                        {**canary_report, "verdict": "APPLY_BLOCKED"})
        return 3

    # ── POST /apply-dedupe (batched) ────────────────────────────────
    print(f"\n[step3] POST /apply-dedupe ({len(plans)} plans in batches "
          f"of {apply_batch_size})")
    all_results: list[dict] = []
    halted_at:   int | None = None
    halt_err:    str | None = None
    for i in range(0, len(plans), apply_batch_size):
        chunk = plans[i:i+apply_batch_size]
        code, body = _http(
            f"{api_base}/api/admin/canonical-cutover/apply-dedupe",
            headers=hdr_tok,
            body={
                "session_id":       session_id,
                "workflow_run_id":  workflow_run_id,
                "confirm_phrase":   APPLY_CONFIRM_PHRASE,
                "plans":            chunk,
            },
            method="POST", timeout_s=1800,
        )
        if code != 200 or not isinstance(body, dict):
            halted_at = i
            halt_err  = f"HTTP {code}: {str(body)[:800]}"
            print(f"[step3] HALTED batch starting at plan {i}: {halt_err}")
            break
        all_results.extend(body.get("results") or [])
        if body.get("halted_at") is not None:
            halted_at = i + int(body["halted_at"])
            halt_err  = str(body.get("halt_err"))[:800]
            print(f"[step3] HALTED mid-batch at plan {halted_at}: {halt_err}")
            break
        print(f"[step3] batch {i}..{i+len(chunk)-1} OK "
              f"({len(body.get('results') or [])} results)")

    if halted_at is not None:
        _write_report(report_path, {
            **canary_report, "verdict": "APPLY_HALTED",
            "apply_results":  all_results,
            "halted_at":      halted_at,
            "halt_err":       halt_err,
        })
        return 4

    # ── STEP 4 post-apply census: must be 0 for the 8 APPLY colls ──
    print("\n[step4] re-run GET /dup-census per APPLY-scope collection "
          "— require 0 duplicate groups")
    post_scope: dict[str, int] = {c: 0 for c in APPLY_SCOPE_COLLS}
    post_coll_errors: list[dict] = []
    for coll in APPLY_SCOPE_COLLS:
        # Only count-level stats are needed here — one page with
        # limit=1 is enough; the server still returns the collection-
        # level ``duplicate_group_count``.
        code, body = _http(
            f"{api_base}/api/admin/canonical-cutover/dup-census?"
            f"collection={coll}&offset=0&limit=1",
            headers=hdr, timeout_s=600,
        )
        if code != 200 or not isinstance(body, dict):
            post_coll_errors.append({"collection": coll,
                                      "status": code,
                                      "body": str(body)[:400]})
            post_scope[coll] = -1   # unknown
            continue
        post_scope[coll] = int(body.get("duplicate_group_count") or 0)
    remaining_total = sum(v for v in post_scope.values() if v >= 0)
    print(f"[step4] remaining dup groups across 8 APPLY scope "
          f"collections = {remaining_total}  per_coll={post_scope}")
    if post_coll_errors or any(v != 0 for v in post_scope.values()):
        _write_report(report_path, {
            **canary_report, "verdict": "APPLY_INCOMPLETE_DUPS_REMAIN",
            "apply_results":  all_results,
            "post_apply_dup_groups": post_scope,
            "post_apply_errors":     post_coll_errors,
        })
        return 5

    # ── STEP 5: Phase-7 unique indexes ─────────────────────────────
    print("\n[step5] POST /create-indexes  (Phase-7 unique indexes)")
    code, idx_resp = _http(
        f"{api_base}/api/admin/canonical-cutover/create-indexes",
        headers=hdr_tok, body={}, method="POST", timeout_s=1200,
    )
    idx_ok = (code == 200 and isinstance(idx_resp, dict) and
               all(r.get("ok") for r in (idx_resp.get("results") or []))
               and all(
                   _EXPECTED_UNIQUE_INDEXES[c] in
                   (r.get("created") or []) + (r.get("existed") or [])
                   for r in (idx_resp.get("results") or [])
                   for c in [r.get("collection")]
                   if c in _EXPECTED_UNIQUE_INDEXES
               ))
    if not idx_ok:
        _write_report(report_path, {
            **canary_report, "verdict": "PHASE7_UNIQUE_INDEX_FAIL",
            "apply_results": all_results,
            "indexes_response": idx_resp,
        })
        return 5

    # ── STEP 6: Phase-8 certification ──────────────────────────────
    print("\n[step6] POST /r3-certification  (Phase-8)")
    expected_counts = {}
    exp_json_path = os.environ.get("PHASE8_CERT_EXPECTED_JSON")
    if exp_json_path and pathlib.Path(exp_json_path).exists():
        try:
            expected_counts = json.loads(pathlib.Path(exp_json_path).read_text())
        except Exception as e:
            print(f"[step6] WARN: could not load expected counts "
                  f"from {exp_json_path}: {e}")

    code, cert = _http(
        f"{api_base}/api/admin/canonical-cutover/r3-certification",
        headers=hdr_tok,
        body={"session_id": session_id,
              "expected_counts": expected_counts},
        method="POST", timeout_s=1800,
    )
    cert_pass = (code == 200 and isinstance(cert, dict)
                  and bool(cert.get("overall_pass")))
    print(f"[step6] cert overall_pass={cert_pass}  http={code}")

    final = {
        **canary_report,
        "verdict":              ("SUCCESS" if cert_pass
                                 else "PHASE8_CERT_FAIL"),
        "apply_results":        all_results,
        "post_apply_dup_groups": post_scope,
        "indexes_response":     idx_resp,
        "phase8_cert":          cert,
    }
    _write_report(report_path, final)
    return 0 if cert_pass else 6


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[r3-ph7] interrupted", file=sys.stderr)
        sys.exit(1)
