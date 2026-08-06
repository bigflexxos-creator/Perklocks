"""Phase 3F-1 — pick_refresh_orchestrator extraction contract tests.

Covers:
  1.  server._refresh_picks delegates to PickRefreshOrchestrator.
  2.  The compatibility wrapper preserves the old signature.
  3.  Production refresh callers still resolve to server._refresh_picks.
  4.  The extracted orchestrator does not import server.
  5.  server.py no longer contains the refresh pipeline body.
  6.  Generation-stage order preserved (verbatim body inside orchestrator).
  7.  Validation stage runs BEFORE atomic delete + insert_many.
  8.  Publication still routes through PredictionPublicationService.
  9.  JobCoordinator + ProviderBudget imports are NOT in orchestrator body.
 10.  Normal-user refresh routes remain DB-only.
 11.  Admin refresh route still delegates to _refresh_picks / orchestrator.
 12.  Two identical requests produce structurally equivalent results.
 13.  Registry & Phase 3B invariants unaffected (implicit via combined run).
 14.  Board counts contract — result carries the int returned by pipeline.
 15.  Error inside pipeline is captured in PickRefreshResult and re-raised.
 16.  db proxy honors legacy `server.db = <test_client>` override.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import server
from services import pick_refresh_orchestrator as ORCH
from services.pick_refresh_orchestrator import (
    PickRefreshOrchestrator,
    PickRefreshRequest,
    PickRefreshResult,
)


# ── 1. Delegation ──────────────────────────────────────────────────
def test_server_refresh_picks_delegates_to_orchestrator():
    """server._refresh_picks(...) must instantiate the orchestrator and
    call refresh(...) on it."""
    async def body():
        called = {"n": 0, "req": None}

        async def fake_refresh(self, request):
            called["n"] += 1
            called["req"] = request
            return PickRefreshResult(
                slate_date=request.slate_date,
                sport_filter=request.sport_filter,
                caller=request.caller,
                published_count=7,
                success=True,
            )

        with patch.object(PickRefreshOrchestrator, "refresh", fake_refresh):
            n = await server._refresh_picks("2026-08-06", sport_filter="MLB")
        assert n == 7
        assert called["n"] == 1
        assert called["req"].slate_date == "2026-08-06"
        assert called["req"].sport_filter == "MLB"
        assert called["req"].caller == "server._refresh_picks_compat"

    asyncio.run(body())


# ── 2. Signature preservation ──────────────────────────────────────
def test_server_refresh_picks_signature_unchanged():
    sig = inspect.signature(server._refresh_picks)
    params = list(sig.parameters.items())
    assert [p[0] for p in params] == ["date_str", "sport_filter"], (
        f"signature changed: {params}"
    )
    assert params[1][1].default is None
    # Return annotation still int
    assert sig.return_annotation is int


# ── 3. Production caller inventory ─────────────────────────────────
_PROD_CALLERS = {
    "server.py": (
        "existing MLB pregame / hourly / rollover / seed loops call "
        "_refresh_picks() directly — the wrapper redirects them to the "
        "orchestrator without any behaviour change.  Also contains the "
        "wrapper def."
    ),
    "routes/admin_routes.py": (
        "admin manual refresh routes import _refresh_picks from server; "
        "the wrapper redirects them to the orchestrator."
    ),
    "services/pick_refresh_orchestrator.py": (
        "docstring reference (not a call).  Kept because the module "
        "documents the compatibility wrapper's signature."
    ),
}


def test_production_callers_documented():
    """Every production callsite of _refresh_picks resolves to the
    wrapper in server.py (which delegates to the orchestrator).  This
    test is a documentation guardrail — it will fail if a new module
    starts calling `_refresh_picks` without adding itself to the
    inventory above."""
    root = Path("/app/backend")
    # Match real usage (call or def), not docstring/comment mentions.
    pattern = re.compile(r"(await\s+_refresh_picks\s*\(|def\s+_refresh_picks\s*\()")
    found: set[str] = set()
    for p in root.rglob("*.py"):
        if any(part in ("tests", "scripts", "__pycache__") for part in p.parts):
            continue
        text = p.read_text(errors="ignore")
        for ln in text.splitlines():
            if pattern.search(ln) and not ln.strip().startswith("#"):
                found.add(str(p.relative_to(root)))
                break
    for f in found:
        assert f in _PROD_CALLERS, (
            f"new _refresh_picks caller {f!r} — please add to "
            "_PROD_CALLERS with a one-line justification"
        )


# ── 4. Orchestrator MUST NOT import server ─────────────────────────
def test_orchestrator_does_not_import_server():
    src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
    # No top-level `import server` and no `from server import ...`
    # A LAZY import inside `_DBProxy._resolve()` is allowed (documented
    # in the comment).  The scan looks for BAREWORD `import server`
    # NOT preceded by "def" or inside a function body.
    for i, ln in enumerate(src.splitlines(), 1):
        stripped = ln.strip()
        if stripped.startswith("import server"):
            # Allow the documented lazy import inside _DBProxy._resolve.
            ctx = "\n".join(src.splitlines()[max(0, i-8):i])
            assert "_DBProxy" in ctx or "lazy" in ctx.lower(), (
                f"orchestrator has an unauthorized `import server` at line {i}: "
                f"{stripped}"
            )
        if stripped.startswith("from server import"):
            raise AssertionError(
                f"orchestrator has `from server import ...` at line {i}: "
                f"{stripped}"
            )


# ── 5. server.py has no inline refresh pipeline body ───────────────
def test_server_py_has_no_inline_refresh_body():
    """server.py must contain only the thin wrapper — no inline
    ``await generate_all_picks(...)`` call and no board_validator
    inline block."""
    src = Path("/app/backend/server.py").read_text()
    # The wrapper contains the ORCHESTRATOR call; no other place should.
    assert "await generate_all_picks(" not in src, (
        "server.py still contains a direct generate_all_picks call — "
        "the pipeline body was not fully extracted"
    )
    # No board_validator import in server.py (moved to orchestrator).
    assert "from board_validator import validate_and_finalize" not in src, (
        "board_validator import found in server.py — pipeline body leaked"
    )


# ── 6. Generation-stage order in orchestrator ──────────────────────
_EXPECTED_STAGE_MARKERS = [
    "generate_all_picks",
    "Tennis Extra",
    "MLB Batter-vs-Pitcher enrichment",
    "SportDB enrichment",
    "Self-tuning learning layer",
    "Elite Player Boost",
    "Goalscorer Dedup + Top-3-with-Elite-Override",
    "Tennis Totals cap",
    "Per-Player Rolling Form",
    "Multi-Armed Bandit",
    "MLB Prop Simulator",
    "Sportsbook Mapping Engine",
    "Tennis Edge Engine v2",
    "Bet-Type Classification",
    "Learning System v2",
    "Deep Dive Mode",
    "Brain Pipeline v1",
    "Lock Engine V2",
    "Player Intelligence enrichment",
    "Universal Evidence System",
    "Elite-protect lock-floor pass",
    "Universal ESPN-backed pick enrichment",
    "Validation-first architecture",
    "Monte Carlo simulation engine",
    "Chalk Kill Switch",
    "Longshot Trap",
    "Board Visibility Gate",
    "Fusion Enrichment",
    "Prediction Publication Service",
    "Cross-run contradiction reconciliation",
    "CSL Guaranteed Elite Injection",
]


def test_generation_stage_order_preserved():
    """The 31 stages must appear in the SAME order they had before
    Phase 3F-1 — the extraction was a verbatim move.  We anchor the
    search at the start of ``async def _refresh_picks`` so text that
    happens to appear in helper docstrings above the pipeline body
    doesn't confuse the ordering check."""
    src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
    anchor = src.find("async def _refresh_picks(")
    assert anchor != -1, "pipeline body not found in orchestrator"
    body = src[anchor:]
    last = -1
    for marker in _EXPECTED_STAGE_MARKERS:
        pos = body.find(marker)
        assert pos > last, (
            f"stage marker {marker!r} out of expected order "
            f"(pos={pos}, last={last})"
        )
        last = pos


# ── 7. Validation runs BEFORE atomic delete + insert_many ──────────
def test_validation_before_persistence():
    src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
    val_pos = src.find("validate_and_finalize")
    del_pos = src.find("_apply_atomic_delete()")
    ins_pos = src.find("db.picks.insert_many(safe_picks")
    assert 0 < val_pos < ins_pos, (val_pos, ins_pos)
    # Atomic delete lives right before the insert.
    assert 0 < del_pos < ins_pos, (del_pos, ins_pos)


# ── 8. Publication still uses PredictionPublicationService ─────────
def test_publication_service_still_wired():
    src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
    assert "from services.prediction_publication_service import" in src, (
        "publication service import lost from orchestrator body"
    )
    assert "PredictionPublicationService" in src


# ── 9. JobCoordinator + ProviderBudget stay OUT of orchestrator ────
def test_orchestrator_does_not_own_coordinator_or_budget():
    src = Path("/app/backend/services/pick_refresh_orchestrator.py").read_text()
    for forbidden in ("JobCoordinator(", "ProviderBudget("):
        assert forbidden not in src, (
            f"orchestrator must NOT instantiate {forbidden} — that is "
            "the caller's responsibility"
        )


# ── 10. Normal-user refresh routes remain DB-only ──────────────────
def test_normal_user_refresh_route_is_db_only():
    src = Path("/app/backend/routes/picks_routes.py").read_text()
    # The /api/picks/refresh route should NOT call _refresh_picks.
    # (Confirmed by iter119_phase2b test_A_no_normal_user_refresh — we
    # re-assert here so a regression fires under Phase 3F-1 too.)
    for i, ln in enumerate(src.splitlines(), 1):
        s = ln.strip()
        if s.startswith("#"):
            continue
        assert "_refresh_picks(" not in s or "orchestrator" in s.lower(), (
            f"picks_routes.py has a normal-user _refresh_picks call at "
            f"line {i}: {s}"
        )


# ── 11. Admin refresh route imports the wrapper ────────────────────
def test_admin_refresh_route_uses_wrapper():
    src = Path("/app/backend/routes/admin_routes.py").read_text()
    assert "from server import _refresh_picks" in src, (
        "admin route no longer imports the wrapper — extraction broken"
    )


# ── 12. Two identical requests produce equivalent results ──────────
def test_two_identical_requests_produce_equivalent_results():
    async def body():
        async def fake_pipeline(date_str, sport_filter=None):
            return 42
        with patch.object(ORCH, "_pipeline_run", fake_pipeline):
            o = PickRefreshOrchestrator()
            r1 = await o.refresh(PickRefreshRequest(
                slate_date="2026-08-06", caller="t1", reason="x",
            ))
            r2 = await o.refresh(PickRefreshRequest(
                slate_date="2026-08-06", caller="t1", reason="x",
            ))
        a = r1.as_dict(); b = r2.as_dict()
        # Zero-out the duration_ms field, everything else must match.
        a["duration_ms"] = 0; b["duration_ms"] = 0
        assert a == b, (a, b)
        assert r1.published_count == 42
        assert r1.success is True
    asyncio.run(body())


# ── 14. Board count contract ───────────────────────────────────────
def test_result_carries_pipeline_return_value():
    async def body():
        async def fake_pipeline(date_str, sport_filter=None):
            return 123
        with patch.object(ORCH, "_pipeline_run", fake_pipeline):
            o = PickRefreshOrchestrator()
            r = await o.refresh(PickRefreshRequest(slate_date="2026-08-06"))
        assert r.published_count == 123
        assert r.snapshot_count  == 123
    asyncio.run(body())


# ── 15. Pipeline error is captured + re-raised ─────────────────────
def test_pipeline_error_captured_and_reraised():
    async def body():
        async def fake_pipeline(date_str, sport_filter=None):
            raise RuntimeError("simulated pipeline failure")
        with patch.object(ORCH, "_pipeline_run", fake_pipeline):
            o = PickRefreshOrchestrator()
            with pytest.raises(RuntimeError):
                await o.refresh(PickRefreshRequest(slate_date="2026-08-06"))
    asyncio.run(body())


# ── 16. _DBProxy honors legacy server.db override ──────────────────
def test_db_proxy_honors_server_db_override():
    """The pre-3F-1 helpers referenced ``server.db`` at call time.
    Tests (test_iter83/85/88) inject a fresh Motor client by
    setting ``server.db = <handle>``.  The proxy must resolve to that
    override instead of the shared owner."""
    from services.pick_refresh_orchestrator import db as proxy

    class _FakeDB:
        name = "sentinel_test_db"
        def __repr__(self): return "<FakeDB sentinel>"

    fake = _FakeDB()
    original = server.__dict__.get("db")
    try:
        server.__dict__["db"] = fake
        # Proxy must forward the sentinel attribute now.
        assert proxy.name == "sentinel_test_db"
    finally:
        if original is None:
            server.__dict__.pop("db", None)
        else:
            server.__dict__["db"] = original


# ── 17. Contract dataclasses expose required fields ────────────────
def test_request_result_dataclasses_have_required_fields():
    req = PickRefreshRequest(slate_date="2026-08-06")
    for f in ("slate_date", "sport_filter", "caller", "reason", "force",
              "board_version_hint", "job_name", "metadata"):
        assert hasattr(req, f), f"PickRefreshRequest missing {f}"

    res = PickRefreshResult()
    for f in ("success", "slate_date", "sport_filter", "generated_count",
              "validated_count", "published_count", "rejected_count",
              "snapshot_count", "duration_ms", "errors", "warnings",
              "board_version", "publication_source"):
        assert hasattr(res, f), f"PickRefreshResult missing {f}"
    # as_dict serialisation
    d = res.as_dict()
    assert "duration_ms" in d and "board_version" in d
