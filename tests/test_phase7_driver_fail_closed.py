"""test_phase7_driver_fail_closed — R3 Phase-7 driver fail-closed
regression tests for the census #22 bug.

The pre-#22 driver silently substituted zero counts for any
collection whose census call returned HTTP 500 or a malformed body,
then emitted ``verdict=CANARY_READY_FOR_APPLY``.  That is a
dangerous false green — the artifact looked clean but the 8
APPLY-scope collections had actually failed to be scanned.

#22 fail-closed contract (verified here):
  * Any HTTP non-200 OR malformed body OR missing required fields
    marks the collection as UNVERIFIED (``verified=False``).
  * UNVERIFIED collections carry ``None`` counts, NOT ``0``.
  * The report records per-failure context:
      collection, http_status, page_number, offset, limit,
      stage, exception_type, error.
  * If ANY of the 21 collections is UNVERIFIED, the driver writes
    ``verdict=CANNOT_VERIFY`` and returns a non-zero exit code.
  * If ANY of the 8 APPLY-scope collections is UNVERIFIED, the
    driver hard-stops BEFORE any plan generation.
  * ``verdict=CANARY_READY_FOR_APPLY`` is impossible while any
    collection is UNVERIFIED.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
from unittest.mock import patch

import pytest


_SCRIPTS = pathlib.Path("/app/reconcile_workspace/scripts")
_DRV_PATH = _SCRIPTS / "r3_phase7_dedupe_apply.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("drv", _DRV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drv = _load_driver()


def _write_min_pinned_source(dir_path: pathlib.Path) -> None:
    """Write 21 minimal pinned NDJSON files so _load_pinned_source
    does not fail the driver before the census step runs."""
    all_colls = drv.RECONCILIATION_COLLECTIONS_LOCAL
    dir_path.mkdir(parents=True, exist_ok=True)
    for c in all_colls:
        # One row with the collection's expected logical-key fields
        # populated so _load_pinned_source succeeds.
        fields = drv._LOGICAL_KEYS_PHASE7.get(c, ())
        doc = {f: f"test_{f}" for f in fields}
        if not fields:
            doc = {"placeholder": True}
        (dir_path / f"{c}.ndjson").write_text(json.dumps({"doc": doc}) + "\n")


def _run_driver_with_mocked_http(responses_by_url_contains: dict,
                                  pin_dir: pathlib.Path,
                                  report_path: pathlib.Path) -> tuple[int, dict]:
    """Monkey-patch drv._http to return deterministic responses by
    URL-substring match.  Returns (exit_code, report_dict)."""
    def _fake_http(url, headers=None, body=None, method="GET", timeout_s=600):
        # Auth login — always succeed.
        if "/api/auth/login" in url:
            return 200, {"access_token": "fake-jwt-token"}
        # Census endpoint — look up substring key.
        for pattern, resp in responses_by_url_contains.items():
            if pattern in url:
                return resp
        return 404, {"detail": "unmocked"}

    env_patch = {
        "PROD_API_BASE":          "https://prod.example.test",
        "PROD_ADMIN_EMAIL":       "admin@example.test",
        "PROD_ADMIN_PASSWORD":    "pw",
        "CANONICAL_IMPORT_TOKEN": "tok",
        "SESSION_ID":             "perklocks-cutover-20261003-r3",
        "PHASE7_MODE":            "CANARY_ONLY",
        "PINNED_SOURCE_DIR":      str(pin_dir),
        "REPORT_PATH":            str(report_path),
        "GITHUB_RUN_ID":          "99999",
    }
    with patch.dict(os.environ, env_patch, clear=False), \
         patch.object(drv, "_http", _fake_http):
        rc = drv.main()
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return rc, report


# ─── Branch 1: HTTP 500 on an APPLY-scope collection → FAIL CLOSED ─
def test_http_500_on_apply_scope_coll_produces_cannot_verify():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ph7fc_"))
    try:
        pin_dir = tmp / "pinned"
        _write_min_pinned_source(pin_dir)
        report_path = tmp / "report.json"

        # All collections succeed EXCEPT 'picks' (an APPLY-scope one).
        default_ok = (200, {
            "total_docs": 10, "logical_key_count": 10,
            "duplicate_group_count": 0, "page_group_count": 0,
            "has_more": False, "next_offset": None,
            "page_exact_count": 0, "page_conflicting_count": 0,
            "groups": [],
        })
        responses = {
            "collection=picks&": (500, {"detail": {
                "collection": "picks", "offset": 0, "limit": 100,
                "stage": "logical_key_count",
                "exception_type": "OperationFailure",
                "error": "memory limit exceeded for aggregation",
            }}),
        }
        # Pre-populate the fake-http default via a wildcard entry:
        for c in drv.RECONCILIATION_COLLECTIONS_LOCAL:
            if c == "picks":
                continue
            responses[f"collection={c}&"] = default_ok

        rc, report = _run_driver_with_mocked_http(responses, pin_dir, report_path)

        assert rc != 0, f"expected non-zero exit, got {rc}"
        assert report["verdict"]           == "CANNOT_VERIFY"
        assert report["stage"]             == "census"
        assert "picks" in report["unverified"]
        assert "picks" in report["unverified_in_apply_scope"]
        # Per-collection failure context captured.
        pc_by_name = {c["collection"]: c for c in report["per_collection"]}
        picks_entry = pc_by_name["picks"]
        assert picks_entry["verified"] is False
        # Counts are None, NOT 0 — fail-closed does not fabricate zeroes.
        assert picks_entry["total_docs"]              is None
        assert picks_entry["logical_key_count"]       is None
        assert picks_entry["duplicate_group_count"]   is None
        assert picks_entry["exact_group_count"]       is None
        assert picks_entry["conflicting_group_count"] is None
        # Failure context carries diagnosis.
        failure = picks_entry["failure"]
        assert failure["http_status"]    == 500
        assert failure["stage"]          == "logical_key_count"
        assert failure["exception_type"] == "OperationFailure"
        assert failure["offset"]         == 0
        assert failure["limit"]          == 100
        assert failure["page_number"]    == 1
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ─── Branch 2: malformed body (non-dict) → UNVERIFIED ──────────────
def test_malformed_response_marks_unverified():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ph7fc_mal_"))
    try:
        pin_dir = tmp / "pinned"
        _write_min_pinned_source(pin_dir)
        report_path = tmp / "report.json"

        default_ok = (200, {
            "total_docs": 10, "logical_key_count": 10,
            "duplicate_group_count": 0, "page_group_count": 0,
            "has_more": False, "next_offset": None,
            "page_exact_count": 0, "page_conflicting_count": 0,
            "groups": [],
        })
        # settlement_events returns a string body — not a dict.
        responses = {"collection=settlement_events&":
                        (200, "unexpected string body")}
        for c in drv.RECONCILIATION_COLLECTIONS_LOCAL:
            if c == "settlement_events": continue
            responses[f"collection={c}&"] = default_ok

        rc, report = _run_driver_with_mocked_http(responses, pin_dir, report_path)
        assert rc != 0
        assert report["verdict"]  == "CANNOT_VERIFY"
        assert "settlement_events" in report["unverified"]
        pc = {c["collection"]: c for c in report["per_collection"]}
        se = pc["settlement_events"]
        assert se["verified"] is False
        assert se["failure"]["stage"] == "malformed_response"
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ─── Branch 3: missing required fields in body → UNVERIFIED ────────
def test_missing_fields_marks_unverified():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ph7fc_miss_"))
    try:
        pin_dir = tmp / "pinned"
        _write_min_pinned_source(pin_dir)
        report_path = tmp / "report.json"

        default_ok = (200, {
            "total_docs": 10, "logical_key_count": 10,
            "duplicate_group_count": 0, "page_group_count": 0,
            "has_more": False, "next_offset": None,
            "page_exact_count": 0, "page_conflicting_count": 0,
            "groups": [],
        })
        # publication_events returns a dict missing total_docs.
        responses = {"collection=publication_events&":
                        (200, {"logical_key_count": 1,
                                 "duplicate_group_count": 0,
                                 "page_group_count": 0,
                                 "has_more": False})}
        for c in drv.RECONCILIATION_COLLECTIONS_LOCAL:
            if c == "publication_events": continue
            responses[f"collection={c}&"] = default_ok

        rc, report = _run_driver_with_mocked_http(responses, pin_dir, report_path)
        assert rc != 0
        assert report["verdict"]  == "CANNOT_VERIFY"
        assert "publication_events" in report["unverified"]
        pc = {c["collection"]: c for c in report["per_collection"]}
        se = pc["publication_events"]
        assert se["failure"]["stage"] == "malformed_response_missing_fields"
        assert "total_docs" in se["failure"]["error"]
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ─── Branch 4: partial failure (only non-APPLY-scope coll) is STILL UNVERIFIED ─
def test_non_apply_scope_coll_500_also_blocks_ready_for_apply():
    """Even a NON-APPLY-scope collection (e.g. games) returning 500
    marks the whole run UNVERIFIED and prevents READY_FOR_APPLY —
    strict fail-closed."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ph7fc_nonap_"))
    try:
        pin_dir = tmp / "pinned"
        _write_min_pinned_source(pin_dir)
        report_path = tmp / "report.json"

        default_ok = (200, {
            "total_docs": 10, "logical_key_count": 10,
            "duplicate_group_count": 0, "page_group_count": 0,
            "has_more": False, "next_offset": None,
            "page_exact_count": 0, "page_conflicting_count": 0,
            "groups": [],
        })
        responses = {"collection=games&":
                        (500, {"detail": {"collection": "games",
                                           "stage": "count_documents",
                                           "exception_type": "ConnectionTimeout",
                                           "error": "mongo timeout"}})}
        for c in drv.RECONCILIATION_COLLECTIONS_LOCAL:
            if c == "games": continue
            responses[f"collection={c}&"] = default_ok

        rc, report = _run_driver_with_mocked_http(responses, pin_dir, report_path)
        assert rc != 0
        assert report["verdict"] == "CANNOT_VERIFY"
        # games is unverified, but APPLY-scope list is empty.
        assert report["unverified"] == ["games"]
        assert report["unverified_in_apply_scope"] == []
        # verdict is NOT ready-for-apply even when scope is empty.
        assert report["verdict"] != "CANARY_READY_FOR_APPLY"
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ─── Branch 5: all 21 clean → CANARY_READY_FOR_APPLY possible ──────
def test_all_clean_can_still_produce_ready_for_apply():
    """Positive baseline — ensure the fail-closed logic does not
    accidentally break the happy path.  When every collection
    verifies and no blockers exist, the verdict is
    CANARY_READY_FOR_APPLY and the exit code is 0."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ph7fc_clean_"))
    try:
        pin_dir = tmp / "pinned"
        _write_min_pinned_source(pin_dir)
        report_path = tmp / "report.json"

        clean_body = (200, {
            "total_docs": 10, "logical_key_count": 10,
            "duplicate_group_count": 0, "page_group_count": 0,
            "has_more": False, "next_offset": None,
            "page_exact_count": 0, "page_conflicting_count": 0,
            "groups": [],
        })
        responses = {f"collection={c}&": clean_body
                      for c in drv.RECONCILIATION_COLLECTIONS_LOCAL}

        rc, report = _run_driver_with_mocked_http(responses, pin_dir, report_path)
        assert rc == 0
        assert report["verdict"] == "CANARY_READY_FOR_APPLY"
        assert report.get("unverified")                in (None, [])
        assert report.get("unverified_in_apply_scope") in (None, [])
    finally:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


# ─── Structural: READY_FOR_APPLY code path has UNVERIFIED gate ─────
def test_source_code_asserts_fail_closed_gate_before_plan_generation():
    """Structural guarantee: the driver source contains the
    fail-closed gate that returns BEFORE building apply plans when
    any collection is unverified."""
    src = _DRV_PATH.read_text()
    # The CANNOT_VERIFY verdict must come from the fail-closed
    # branch that short-circuits plan generation.
    gate_idx = src.find('"CANNOT_VERIFY"')
    ready_idx = src.find('"CANARY_READY_FOR_APPLY"')
    assert gate_idx != -1
    assert ready_idx != -1
    # The CANNOT_VERIFY emission must appear BEFORE the
    # READY_FOR_APPLY branch in file order, because the gate returns
    # early.  (This is a weak but useful structural check.)
    assert gate_idx < ready_idx, (
        "CANNOT_VERIFY fail-closed gate must be reached before "
        "READY_FOR_APPLY emission in driver source")
    # The ``verified`` flag must be used to decide the gate.
    assert "unverified_all" in src
    assert "verified=False" in src or '"verified":' in src
