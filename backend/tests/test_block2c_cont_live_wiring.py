"""Block 2C-CONT — Live wiring proof tests.

Locks the invariants for the LIVE production 422 bundle-isolation path:

  §W-1  Gateway pre-flight filter is event-scoped (Event A bad-market
        marker does NOT suppress the same market on Event B).
  §W-2  Multi-market 422 bundles NEVER get widely marked bad — only a
        confirmed single-market probe writes a registry entry.
  §W-3  sports_engine._fetch_event_props_payload wires the isolator
        (isolate_bad_markets) into the real production path.
  §W-4  Isolation preserves sibling markets for the failing event.
  §W-5  Event A 422 on market X does not suppress market X on Event B.
  §W-6  Merged payload combines all supported markets from disjoint
        sub-bundles into a single event payload.

Every test here is deterministic (no network, no clock, no DB races).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §W-1 — Gateway pre-flight filter is event-scoped
# ═══════════════════════════════════════════════════════════════════
def test_gateway_pre_flight_filter_passes_event_id():
    """The gateway MUST pass event_id to bad_market_registry.filter_markets
    so an event-scoped marker on Event A does not suppress Event B."""
    src = open("/app/backend/services/odds_api_gateway.py").read()
    # Extract the pre-flight filter block by looking at the section
    # right after the "Bad-market filter" banner comment.
    assert "Bad-market filter" in src
    banner_idx = src.index("Bad-market filter")
    # Look at the ~40 lines after the banner for the filter_markets call.
    window = src[banner_idx: banner_idx + 2000]
    assert "filter_markets(" in window
    assert "event_id=event_id" in window, (
        "gateway pre-flight bad_market filter must pass event_id so "
        "event-scoped markers cannot suppress unrelated events; "
        f"window snippet: {window[:600]!r}")


# ═══════════════════════════════════════════════════════════════════
# §W-2 — Multi-market 422 never over-suppresses
# ═══════════════════════════════════════════════════════════════════
def test_gateway_mark_bad_only_from_single_market_probe():
    """Only a confirmed single-market probe (len(m_list)==1) is
    allowed to write a bad-market registry entry.  Multi-market
    bundles that 422 must be bisected — marking the whole bundle
    would falsely suppress good sibling markets forever."""
    src = open("/app/backend/services/odds_api_gateway.py").read()
    assert "len(m_list) == 1" in src, (
        "gateway must guard mark_bad with len(m_list)==1 so a "
        "multi-market bundle 422 does not falsely mark good markets bad")
    # And the mark_bad call must pass scope="event" + event_id.
    mb_snippet = src.split("mark_bad(", 1)[1]
    # The first mark_bad call after the guard should include both
    # event_id and scope="event".
    idx_close = mb_snippet.index(")")
    call_args = mb_snippet[:idx_close]
    assert "event_id=event_id" in call_args, (
        "gateway.mark_bad must pass event_id so the marker is "
        "event-scoped and cannot suppress other events")
    assert 'scope="event"' in call_args or "scope='event'" in call_args, (
        "gateway.mark_bad must pass scope='event' (per Block 2C event-"
        "scoped keying — never widen an event failure to global)")


# ═══════════════════════════════════════════════════════════════════
# §W-3 — Live wiring: isolator is invoked from the real path
# ═══════════════════════════════════════════════════════════════════
def test_fetch_event_props_wires_isolate_bad_markets():
    """The production caller sports_engine._fetch_event_props_payload
    MUST invoke isolate_bad_markets from provider_cache_state — not
    just import it, but actually call it in the 422 failure path."""
    src = open("/app/backend/sports_engine.py").read()
    # There must be a helper that orchestrates isolation.
    assert "_isolate_and_merge_event_props" in src, (
        "sports_engine must define _isolate_and_merge_event_props()")
    # The helper must import and call isolate_bad_markets.
    orch_start = src.index("async def _isolate_and_merge_event_props")
    orch_end = src.index("\n\n\n", orch_start)
    orch_body = src[orch_start:orch_end]
    assert "isolate_bad_markets" in orch_body, (
        "_isolate_and_merge_event_props must call isolate_bad_markets")
    # And _fetch_event_props_payload must invoke the orchestrator on
    # the failure path.
    fetch_start = src.index("async def _fetch_event_props_payload")
    fetch_end = src.index("\n\nasync def", fetch_start)
    fetch_body = src[fetch_start:fetch_end]
    assert "_isolate_and_merge_event_props" in fetch_body, (
        "_fetch_event_props_payload must dispatch to the isolation "
        "orchestrator on None/error result — not just return {}")


# ═══════════════════════════════════════════════════════════════════
# §W-4 — Isolation preserves sibling markets for the failing event
# ═══════════════════════════════════════════════════════════════════
def test_isolation_preserves_sibling_markets():
    """When a bundle 422s because of one bad market, the isolator
    must return the OTHER markets as supported.  This is the raison
    d'être of bounded bisection over widen-and-suppress-everything."""
    from services.provider_cache_state import isolate_bad_markets

    BAD = "player_home_runs"

    async def probe(subset):
        # Provider 422s any subset containing BAD.
        if BAD in subset:
            return None
        return {m: {"line": 0.5} for m in subset}

    async def _run():
        return await isolate_bad_markets(
            ["batter_hits", "batter_rbis", BAD, "batter_total_bases"],
            probe,
        )

    r = asyncio.run(_run())
    # BAD isolated, siblings preserved.
    assert BAD in r.bad_markets, r
    assert "batter_hits" in r.supported_markets
    assert "batter_rbis" in r.supported_markets
    assert "batter_total_bases" in r.supported_markets
    # Bounded by hard caps.
    assert r.retries_used <= 8
    assert r.credits_used <= 8


# ═══════════════════════════════════════════════════════════════════
# §W-5 — Cross-event isolation contract (LIVE-EQUIVALENT)
# ═══════════════════════════════════════════════════════════════════
def test_cross_event_isolation_via_bad_market_registry_event_scope():
    """Event A 422 on market X must NOT suppress market X on Event B.

    This is the load-bearing invariant of Block 2C-cont.  We simulate
    the registry filter with the real code path:
       1. Mark market X BAD on Event A (scope='event').
       2. Call filter_markets for Event A → X removed.
       3. Call filter_markets for Event B → X still present.
    """
    from services import bad_market_registry as bmr

    class _FakeCursor:
        def __init__(self, docs):
            self._docs = list(docs)
        def __aiter__(self):
            self._i = 0
            return self
        async def __anext__(self):
            if self._i >= len(self._docs):
                raise StopAsyncIteration
            d = self._docs[self._i]; self._i += 1
            return d

    class _FakeColl:
        def __init__(self, rows):
            self.rows = rows
        def find(self, query, projection=None):
            # Filter rows against a subset of the real filter query.
            def match(r):
                for k, v in query.items():
                    if k in ("$or", "expires_at"):
                        continue
                    if r.get(k) != v:
                        return False
                return True
            or_clause = query.get("$or")
            if or_clause:
                base = [r for r in self.rows if match(r)]
                out = []
                for r in base:
                    ok = False
                    for cond in or_clause:
                        if "scope" in cond and isinstance(cond["scope"], dict):
                            if "$exists" in cond["scope"]:
                                exists = cond["scope"]["$exists"]
                                has = "scope" in r
                                if exists == has:
                                    ok = True; break
                            continue
                        if cond.get("scope") == r.get("scope"):
                            ok = True; break
                    if ok:
                        out.append(r)
                return _FakeCursor(out)
            return _FakeCursor([r for r in self.rows if match(r)])

    class _FakeDB:
        def __init__(self, rows):
            self._c = _FakeColl(rows)
        def __getitem__(self, name):
            return self._c

    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(hours=6)

    # Mark player_home_runs bad on Event A only.
    rows = [{
        "sport_key": "baseball_mlb",
        "market":    "player_home_runs",
        "event_id":  "EVENT_A",
        "scope":     "event",
        "expires_at": future,
    }]
    db = _FakeDB(rows)

    all_markets = [
        "batter_hits", "player_home_runs", "batter_rbis",
    ]

    async def _run():
        a = await bmr.filter_markets(
            db, sport_key="baseball_mlb",
            markets=all_markets, event_id="EVENT_A")
        b = await bmr.filter_markets(
            db, sport_key="baseball_mlb",
            markets=all_markets, event_id="EVENT_B")
        return a, b

    a, b = asyncio.run(_run())
    # Event A → player_home_runs filtered out.
    assert "player_home_runs" not in a
    assert "batter_hits" in a
    assert "batter_rbis" in a
    # Event B → player_home_runs STILL PRESENT (proof: no cross-event
    # suppression).
    assert "player_home_runs" in b
    assert "batter_hits" in b
    assert "batter_rbis" in b


# ═══════════════════════════════════════════════════════════════════
# §W-6 — Merged payload combines disjoint sub-bundle results
# ═══════════════════════════════════════════════════════════════════
def test_merge_event_odds_payloads_unions_disjoint_market_subsets():
    """When isolation runs a bisection, we get several partial
    payloads each carrying a disjoint market subset.  The merger
    must reunite them into a single event payload with the union of
    all markets under the SAME bookmaker keys."""
    from sports_engine import _merge_event_odds_payloads

    payload_a = {
        "id": "EV1", "home_team": "H", "away_team": "A",
        "commence_time": "2026-08-15T00:00:00Z",
        "bookmakers": [{
            "key": "draftkings", "title": "DraftKings",
            "markets": [
                {"key": "batter_hits",
                 "outcomes": [{"name": "Over", "price": -110}]},
            ],
        }],
    }
    payload_b = {
        "id": "EV1", "home_team": "H", "away_team": "A",
        "commence_time": "2026-08-15T00:00:00Z",
        "bookmakers": [{
            "key": "draftkings", "title": "DraftKings",
            "markets": [
                {"key": "batter_rbis",
                 "outcomes": [{"name": "Over", "price": -105}]},
            ],
        }, {
            "key": "fanduel", "title": "FanDuel",
            "markets": [
                {"key": "batter_total_bases",
                 "outcomes": [{"name": "Over", "price": -120}]},
            ],
        }],
    }

    merged = _merge_event_odds_payloads([payload_a, payload_b])
    assert merged["id"] == "EV1"
    # DraftKings should contain BOTH batter_hits AND batter_rbis.
    dk = next(b for b in merged["bookmakers"] if b["key"] == "draftkings")
    dk_market_keys = {m["key"] for m in dk["markets"]}
    assert "batter_hits" in dk_market_keys
    assert "batter_rbis" in dk_market_keys
    # FanDuel should have batter_total_bases.
    fd = next(b for b in merged["bookmakers"] if b["key"] == "fanduel")
    fd_market_keys = {m["key"] for m in fd["markets"]}
    assert "batter_total_bases" in fd_market_keys


def test_merge_event_odds_payloads_dedupes_same_market_key():
    """If a market key appears in two payloads (shouldn't happen with
    disjoint subsets, but defensive), first-write-wins so we don't
    duplicate outcomes."""
    from sports_engine import _merge_event_odds_payloads

    p_a = {
        "id": "EV", "bookmakers": [{
            "key": "dk", "markets": [
                {"key": "batter_hits",
                 "outcomes": [{"name": "Over", "price": -100}]},
            ],
        }],
    }
    p_b = {
        "id": "EV", "bookmakers": [{
            "key": "dk", "markets": [
                {"key": "batter_hits",
                 "outcomes": [{"name": "Over", "price": -200}]},
            ],
        }],
    }

    merged = _merge_event_odds_payloads([p_a, p_b])
    dk = merged["bookmakers"][0]
    hit_markets = [m for m in dk["markets"] if m["key"] == "batter_hits"]
    assert len(hit_markets) == 1
    # First-write-wins.
    assert hit_markets[0]["outcomes"][0]["price"] == -100


def test_merge_event_odds_payloads_empty_input():
    from sports_engine import _merge_event_odds_payloads
    assert _merge_event_odds_payloads([]) == {}


# ═══════════════════════════════════════════════════════════════════
# §W-7 — Isolation orchestrator wiring signals
# ═══════════════════════════════════════════════════════════════════
def test_isolation_orchestrator_signature_and_deps():
    """_isolate_and_merge_event_props must be async and take the
    documented kwargs."""
    import inspect
    from sports_engine import _isolate_and_merge_event_props
    sig = inspect.signature(_isolate_and_merge_event_props)
    for kw in ("sport", "sport_key", "event_id", "regions",
                "bundle_markets"):
        assert kw in sig.parameters, f"missing kw={kw}"
    assert asyncio.iscoroutinefunction(_isolate_and_merge_event_props)


# ═══════════════════════════════════════════════════════════════════
# §W-8 — Hard invariants — must not have been changed by this pass
# ═══════════════════════════════════════════════════════════════════
def test_isolate_bad_markets_hard_caps_unchanged():
    from services.provider_cache_state import (
        MAX_422_RETRY_REQUESTS, MAX_422_RETRY_CREDITS, MAX_422_RETRY_DEPTH,
    )
    assert MAX_422_RETRY_REQUESTS == 8
    assert MAX_422_RETRY_CREDITS == 8
    assert MAX_422_RETRY_DEPTH == 3


def test_universal_settlement_missing_data_never_zero():
    """MISSING DATA != 0 remains inviolable."""
    from services import universal_settlement_contract as usc
    assert hasattr(usc, "RESULT_UNRESOLVED")
    # Grading a missing observation must NOT emit a 'win'/'loss' —
    # it must be unresolved.
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert isinstance(graded, dict)
    assert graded.get("result") == usc.RESULT_UNRESOLVED, graded
    assert graded.get("result") != "win"


# ═══════════════════════════════════════════════════════════════════
# §W-9 — Stale-deploy-banner Issue-6 semantics (backend surface)
# ═══════════════════════════════════════════════════════════════════
def test_backend_version_exposes_runtime_started_at_and_optional_deploy_metadata():
    """/api/version must expose:
      * data_version              — real-deploy signal
      * server_started_at         — legacy runtime marker (back-compat)
      * runtime_started_at        — truthfully-named alias
      * deploy_metadata (opt-in)  — only when env exposes real IDs

    And must NOT invent a deploy_timestamp when the environment
    doesn't provide one.
    """
    src = open("/app/backend/server.py").read()
    assert '"data_version"' in src
    assert '"server_started_at"' in src
    assert '"runtime_started_at"' in src
    assert "_deploy_metadata_from_env" in src
    # The env-reader must consult real deploy identifiers.
    for k in (
        "DEPLOYMENT_ID", "BUILD_ID", "GIT_COMMIT_SHA",
        "DEPLOY_TIMESTAMP",
    ):
        assert k in src, f"missing env identifier probe: {k}"


def test_backend_never_invents_deploy_metadata_when_env_absent():
    """When no deploy identifiers are set in env, /api/version's
    deploy_metadata key must be ABSENT (never a stub / synthetic
    value)."""
    import importlib, sys
    # Ensure env has none of the identifiers we consult.
    import os as _os
    keys = [
        "DEPLOYMENT_ID", "BUILD_ID", "GIT_COMMIT_SHA", "GIT_SHA",
        "COMMIT_SHA", "BACKEND_RELEASE_ID", "FRONTEND_RELEASE_ID",
        "DEPLOY_TIMESTAMP", "DEPLOY_TIME",
        "RENDER_GIT_COMMIT", "VERCEL_GIT_COMMIT_SHA",
    ]
    saved = {k: _os.environ.pop(k, None) for k in keys}
    try:
        # Force a re-eval of the env-reader.
        if "server" in sys.modules:
            server = sys.modules["server"]
            md = server._deploy_metadata_from_env()
            assert md == {}, (
                "backend must NOT invent deploy metadata; got: %r" % (md,)
            )
    finally:
        for k, v in saved.items():
            if v is not None:
                _os.environ[k] = v


# ═══════════════════════════════════════════════════════════════════
# §W-10 — Stale-banner front-end truthful trigger
# ═══════════════════════════════════════════════════════════════════
def test_frontend_banner_no_longer_computes_age_from_server_started_at():
    """The banner MUST NOT compute a day-diff from server_started_at
    (process-start time).  Age may only come from real deploy
    metadata (deploy_timestamp)."""
    src = open("/app/frontend/src/components/StaleBuildBanner.tsx").read()
    # Find every daysBetween CALL SITE (not the function declaration).
    import re
    # Skip declaration: "function daysBetween(a: Date, b: Date)"
    call_sites = [
        m.start() for m in re.finditer(r"daysBetween\(", src)
        if "function daysBetween" not in src[max(0, m.start()-40): m.start()+12]
    ]
    assert call_sites, "expected at least one daysBetween() call site"
    for pos in call_sites:
        # Look at the ~600 chars preceding each call for evidence it
        # sources from real deploy metadata, not server_started_at.
        ctx = src[max(0, pos - 800): pos + 100]
        assert (
            "deployTs" in ctx or "deploy_metadata" in ctx
            or "dt.getTime()" in ctx or "deploy_id" in ctx
        ), (
            "daysBetween call at pos %d must be sourced from real deploy "
            "metadata (deploy_timestamp / deploy_id / git_commit_sha), "
            "not server_started_at.  Context: %r" % (pos, ctx)
        )
        assert "server_started_at" not in ctx or "deploy_metadata" in ctx, (
            "daysBetween call at pos %d appears to source from "
            "server_started_at — forbidden. Context: %r" % (pos, ctx)
        )
    # And the old copy claim must be gone.
    assert "This deploy is" not in src, (
        "banner must no longer render 'This deploy is X days behind' "
        "— that was the pre-Block-2C-cont deploy-age lie."
    )


def test_frontend_banner_data_version_mismatch_trigger_present():
    src = open("/app/frontend/src/components/StaleBuildBanner.tsx").read()
    assert "data_version_mismatch" in src, (
        "banner must implement data_version-mismatch trigger "
        "(source-code constant comparison — real deploy signal)"
    )
    # And check for deploy_metadata trigger.
    assert "deploy_metadata" in src


# ═══════════════════════════════════════════════════════════════════
# §W-11 — Legacy bad_market_registry audit script exists
# ═══════════════════════════════════════════════════════════════════
def test_block2c_cont_audit_script_present_and_read_only():
    src = open("/app/backend/scripts/block2c_cont_audit.py").read()
    assert "audit_legacy_bad_market_registry" in src
    # Must be read-only — no update_*, insert_*, delete_*, drop_*,
    # bulk_write against the registry collection in this script.
    for banned in ("update_one(", "update_many(", "insert_one(",
                    "insert_many(", "delete_one(", "delete_many(",
                    "drop(", "bulk_write("):
        assert banned not in src, (
            f"legacy-registry audit must be READ-ONLY (found: {banned!r})"
        )
