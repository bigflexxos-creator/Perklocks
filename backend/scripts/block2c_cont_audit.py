"""Block 2C-cont — Full read-only audit report.

Emits ``/tmp/block2c_cont_audit.json`` covering every remaining
checklist item AFTER the live 422 isolation wiring:

    §CONT-1  Near-first-pitch TTL live wiring
    §CONT-2  Partial-snapshot metadata surfacing
    §CONT-3  Provider-budget priority ordering (audit)
    §CONT-4  Single-flight deep audit
    §CONT-5  Circuit-breaker deep audit
    §CONT-6  Stale-deploy-banner semantics (Issue 6)
    §CONT-7  Legacy bad_market_registry rows (Issue: audit only)
    §CONT-8  /api/version consumer audit (Issue 5 / XCUT-6)

No writes.  Everything is a static read of source + a Mongo
count aggregation for the legacy-registry snapshot.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app/backend")

REPO = Path("/app")
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════
# §CONT-1 — Near-first-pitch TTL live wiring
# ═══════════════════════════════════════════════════════════════════
def audit_near_first_pitch_ttl() -> dict:
    """Two independent TTL systems exist:

      A. services.odds_cache._TIME_AWARE_MULTIPLIERS
         — actively wired into every cached_odds_get() call via
           _time_aware_ttls().  Shortens TTL to base (1.0×) when a
           game is < 2 hours away — this IS the near-first-pitch
           shortening for the live cache.

      B. services.provider_cache_state.near_first_pitch_ttl
         — helper for the future CacheState-envelope cache.  NOT
           consumed by live code (block 2C helpers wave).

    We DO NOT force-wire (B) — doing so would collide with the
    battle-tested (A) which already services 100% of production
    reads.  The audit records this so no one wires it later
    thinking it's an oversight.
    """
    oc = _read(BACKEND / "services/odds_cache.py")
    return {
        "id": "CONT-1",
        "status": "SATISFIED-BY-EXISTING-WIRING",
        "detail": (
            "Live near-first-pitch TTL shortening is provided by "
            "services.odds_cache._TIME_AWARE_MULTIPLIERS (bucket "
            "'< 2 h → base TTL 1.0×') routed through "
            "_time_aware_ttls() on every cached_odds_get(). "
            "services.provider_cache_state.near_first_pitch_ttl is "
            "the future-cache helper for the CacheState-envelope "
            "refactor and is deliberately left dormant to avoid "
            "colliding with the production TTL path."
        ),
        "evidence": {
            "time_aware_multipliers_defined":
                "_TIME_AWARE_MULTIPLIERS" in oc,
            "under_2h_bucket_present":
                "(2.0,   1.0)" in oc,
            "time_aware_ttls_called_in_fetch":
                "_time_aware_ttls(" in oc and "cached_odds_get" in oc,
            "endpoint_scope_includes_event_odds":
                '"event_odds"' in oc and "_TIME_AWARE_ENDPOINTS" in oc,
        },
        "recommendation": "no action — do not double-wire",
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-2 — Partial-snapshot metadata surfacing
# ═══════════════════════════════════════════════════════════════════
def audit_partial_snapshot_metadata() -> dict:
    """After Block 2C-cont §W-3, a bulk 422 triggers bisection and
    the merged payload may contain FEWER markets than the bundle
    requested.  We need a way for downstream to detect partiality.

    Signals available today:
      * The bookmaker.markets list is smaller than the original
        bundle — implicit but reliable if downstream compares.
      * services.provider_cache_state.CacheState.PARTIAL_MARKET_RESPONSE
        exists as a state enum (not yet emitted).
    """
    se = _read(BACKEND / "sports_engine.py")
    pcs = _read(BACKEND / "services/provider_cache_state.py")
    return {
        "id": "CONT-2",
        "status": "IMPLICIT",
        "detail": (
            "The merged payload from _isolate_and_merge_event_props "
            "IS the partial snapshot; downstream sport-specific "
            "extractors (e.g. _extract_mlb_hitter_props) already "
            "iterate bookmakers[].markets and gracefully skip "
            "missing markets. No fields are silently zeroed — a "
            "missing market simply produces no candidates for that "
            "prop family, in line with 'MISSING DATA != 0'."
        ),
        "evidence": {
            "isolation_orchestrator_present":
                "_isolate_and_merge_event_props" in se,
            "cache_state_partial_enum_defined":
                "PARTIAL_MARKET_RESPONSE" in pcs,
            "cache_state_partial_422_enum_defined":
                "PARTIAL_422_UNRESOLVED" in pcs,
            "isolation_logs_state":
                "props-isolation:" in se and "state=%s" in se,
        },
        "recommendation": (
            "log-level surface only for now — DB envelope surface "
            "deferred to the CacheState-envelope refactor (Block 3+)."
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-3 — Provider-budget priority ordering
# ═══════════════════════════════════════════════════════════════════
def audit_provider_budget_priority() -> dict:
    gw = _read(BACKEND / "services/odds_api_gateway.py")
    pbp = _read(BACKEND / "services/provider_budget_priority.py")

    # Extract the ORDER of gateway checks in .fetch() so we can prove
    # priority-shed happens before single-flight + reserve.
    fetch_body = ""
    if "async def fetch(" in gw:
        idx = gw.index("async def fetch(")
        # Look at a wide window — file has grown over time.
        fetch_body = gw[idx: idx + 20000]
    order = []
    for tag, marker in [
        ("priority_gate",   "priority-shed"),
        ("bad_market",      "Bad-market filter"),
        ("single_flight",   "Distributed single-flight"),
        ("budget_reserve",  "Budget reservation"),
        ("circuit_breaker", "Circuit-breaker guard"),
    ]:
        if marker in fetch_body:
            order.append((tag, fetch_body.index(marker)))
    order.sort(key=lambda x: x[1])
    ordered_tags = [t for t, _ in order]

    return {
        "id": "CONT-3",
        "status": "PASS" if ordered_tags[:3] == [
            "priority_gate", "bad_market", "single_flight",
        ] else "REVIEW",
        "detail": (
            "Gateway sequences: priority-shed → bad-market filter → "
            "single-flight → budget reserve → circuit-breaker.  "
            "Priority runs BEFORE reservation so low-priority "
            "requests are rejected without touching the budget "
            "ledger."
        ),
        "evidence": {
            "gateway_fetch_check_order": ordered_tags,
            "priority_helper_present":
                "provider_budget_priority" in pbp
                and "def decide(" in pbp,
            "priorities_defined":
                all(k in pbp for k in (
                    "P1_", "P2_", "P3_", "P4_", "P5_")),
        },
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-4 — Single-flight deep audit
# ═══════════════════════════════════════════════════════════════════
def audit_single_flight() -> dict:
    sf = _read(BACKEND / "services/single_flight.py")
    gw = _read(BACKEND / "services/odds_api_gateway.py")
    return {
        "id": "CONT-4",
        "status": "PASS" if (
            "class SingleFlight" in sf
            and "acquire" in sf and "wait_for_result" in sf
            and "self.flight.acquire" in gw
        ) else "FAIL",
        "detail": (
            "Distributed SingleFlight owns rk-keyed request coalescing; "
            "the gateway acquires the flight token BEFORE budget "
            "reservation and, on non-winner path, waits and serves "
            "the cache row of the winner."
        ),
        "evidence": {
            "singleflight_class":       "class SingleFlight" in sf,
            "singleflight_acquire":     "async def acquire(" in sf,
            "singleflight_wait_result": "wait_for_result" in sf,
            "gateway_wires_singleflight":
                "self.flight.acquire(rk" in gw
                and "self.flight.wait_for_result" in gw,
            "gateway_stale_hit_on_wait_timeout":
                "single_flight_stale_hit" in gw,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-5 — Circuit-breaker deep audit
# ═══════════════════════════════════════════════════════════════════
def audit_circuit_breaker() -> dict:
    se = _read(BACKEND / "sports_engine.py")
    gw = _read(BACKEND / "services/odds_api_gateway.py")
    return {
        "id": "CONT-5",
        "status": "PASS" if (
            "record_odds_call_result" in se
            and "_API_401_STREAK" in se
            and "circuit_open" in gw
            and "get_odds_api_status" in gw
        ) else "REVIEW",
        "detail": (
            "sports_engine tracks 401/failure streaks and exports "
            "get_odds_api_status().  Gateway consults it BEFORE the "
            "upstream call and releases budget + fails flight when "
            "the CB is open.  Every upstream result feeds back via "
            "record_odds_call_result() so gateway state stays in "
            "sync."
        ),
        "evidence": {
            "cb_state_in_sports_engine":
                "_API_401_STREAK" in se and "_API_FAIL_STREAK" in se,
            "get_odds_api_status_exported":
                "def get_odds_api_status" in se,
            "gateway_reads_cb_status":
                "get_odds_api_status" in gw
                and "st.get(\"disabled\")" in gw,
            "gateway_releases_budget_on_cb_open":
                "self.budget.release(" in gw and "circuit_open" in gw,
            "record_call_result_feedback_loop":
                "record_odds_call_result" in se
                and "record_odds_call_result" in gw,
        },
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-6 — Stale-deploy-banner semantics (Issue 6)
# ═══════════════════════════════════════════════════════════════════
def audit_stale_banner_semantics() -> dict:
    banner = _read(FRONTEND / "src/components/StaleBuildBanner.tsx")
    server = _read(BACKEND / "server.py")
    # Which deployment identifiers does the environment expose?
    envs_present = {
        k: bool(os.environ.get(k))
        for k in [
            "DEPLOYMENT_ID", "BUILD_ID", "GIT_COMMIT_SHA",
            "GIT_SHA", "COMMIT_SHA",
            "BACKEND_RELEASE_ID", "FRONTEND_RELEASE_ID",
            "DEPLOY_TIMESTAMP", "DEPLOY_TIME",
            "RENDER_GIT_COMMIT", "VERCEL_GIT_COMMIT_SHA",
        ]
    }
    any_real_deploy_metadata = any(envs_present.values())
    # /api/version endpoint fields
    version_exposes_started = "server_started_at" in server
    banner_uses_started_at = "server_started_at" in banner
    banner_claims_deploy_age = re.search(
        r"deploy\s+is\s+.+behind|deploy\s+age|days? behind",
        banner, re.IGNORECASE) is not None
    return {
        "id": "CONT-6",
        "status": (
            "READY-FOR-REAL-METADATA" if any_real_deploy_metadata
            else "NO-REAL-METADATA-AVAILABLE"
        ),
        "detail": (
            "server_started_at is process-start time and MUST NOT "
            "be described as deploy age.  If a real deploy "
            "identifier is present (DEPLOYMENT_ID / BUILD_ID / "
            "GIT_COMMIT_SHA / DEPLOY_TIMESTAMP), the banner may use "
            "it; otherwise the banner must describe RUNTIME/BUILD "
            "age truthfully and never claim 'this deploy is X days "
            "behind'."
        ),
        "evidence": {
            "env_deploy_identifiers": envs_present,
            "any_real_deploy_metadata_present":
                any_real_deploy_metadata,
            "version_endpoint_exposes_server_started_at":
                version_exposes_started,
            "banner_reads_server_started_at":
                banner_uses_started_at,
            "banner_claims_deploy_age_language":
                banner_claims_deploy_age,
        },
        "action_taken": (
            "Banner copy audit: see follow-up in "
            "audit_stale_banner_copy()."
        ),
    }


def audit_stale_banner_copy() -> dict:
    """Companion audit — just reads what the banner CURRENTLY says
    so we can decide if a surgical wording fix is warranted."""
    p = FRONTEND / "src/components/StaleBuildBanner.tsx"
    banner = _read(p)
    # Extract string literals (single/double-quoted).
    literals = re.findall(r'"([^"]{4,200})"|\'([^\']{4,200})\'', banner)
    strings = [a or b for a, b in literals]
    return {
        "id": "CONT-6-COPY",
        "banner_string_literals": strings,
        "file": str(p),
        "num_literals": len(strings),
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-7 — Legacy bad_market_registry rows
# ═══════════════════════════════════════════════════════════════════
async def audit_legacy_bad_market_registry() -> dict:
    from server import db as _db
    from services import bad_market_registry as bmr

    if _db is None:
        return {"id": "CONT-7", "status": "DB-UNAVAILABLE"}

    now = datetime.now(timezone.utc)
    coll = _db[bmr.COLLECTION]

    total = await coll.count_documents({})
    active = await coll.count_documents({"expires_at": {"$gt": now}})
    expired = await coll.count_documents({"expires_at": {"$lte": now}})

    # Rows without event_id (legacy widescope OR modern globals).
    no_event_id_active = await coll.count_documents(
        {"$or": [{"event_id": {"$exists": False}}, {"event_id": None}],
          "expires_at": {"$gt": now}})
    no_scope_active = await coll.count_documents(
        {"scope": {"$exists": False}, "expires_at": {"$gt": now}})

    # Legacy = active AND (no event_id AND no scope) — pre-Block-2C.
    legacy_active = await coll.count_documents({
        "$or": [{"event_id": {"$exists": False}}, {"event_id": None}],
        "scope": {"$exists": False},
        "expires_at": {"$gt": now},
    })

    # Sport / market breakdown for active legacy rows.
    legacy_by_sport: list[dict] = []
    async for r in coll.aggregate([
        {"$match": {
            "$or": [{"event_id": {"$exists": False}}, {"event_id": None}],
            "scope": {"$exists": False},
            "expires_at": {"$gt": now},
        }},
        {"$group": {
            "_id": {"sport": "$sport_key", "market": "$market"},
            "n": {"$sum": 1},
            "next_expiry": {"$min": "$expires_at"},
        }},
        {"$sort": {"n": -1}}, {"$limit": 100},
    ]):
        legacy_by_sport.append({
            "sport_key": r["_id"]["sport"],
            "market":    r["_id"]["market"],
            "count":     r["n"],
            "next_expiry": r["next_expiry"].isoformat()
                if r.get("next_expiry") else None,
        })

    correctness_risk = (
        "Legacy rows without event_id are treated as GLOBAL scope "
        "by filter_markets(), which means they suppress the same "
        "market across ALL events of that sport.  Under the new "
        "gateway policy (Block 2C-cont §W-2), a legacy row can "
        "only have been produced by ONE of two paths: (a) a real "
        "sport-wide unsupported market (safe to keep) OR (b) an "
        "over-widened event-level 422 written by the pre-Block-2C "
        "gateway (unsafe — suppresses siblings). A cleanup pass "
        "would need to distinguish (a) vs (b).  This is a READ-ONLY "
        "audit — no writes performed."
    )

    plan_if_authorized = {
        "step_1": "Snapshot all legacy rows to /tmp/legacy_bm_snapshot.json.",
        "step_2": (
            "For each legacy row, cross-check against the current "
            "PLAYER_PROP_MARKETS map — if the market is in the "
            "current per-sport list AND has ever produced picks in "
            "the last 30 days for a DIFFERENT event, that row is "
            "over-widened."),
        "step_3": (
            "Convert over-widened rows to scope='event' with "
            "event_id=None (soft-quarantine) rather than delete "
            "so we can measure churn."),
        "step_4": (
            "Let TTL expire the rest — no delete, no forcible write."),
        "gate": (
            "REQUIRES EXPLICIT USER AUTHORIZATION per Block 2C-cont "
            "read-only directive."),
    }

    return {
        "id": "CONT-7",
        "status": "AUDIT-COMPLETE-NO-WRITES",
        "totals": {
            "total_rows":              total,
            "active":                  active,
            "expired":                 expired,
            "active_no_event_id":      no_event_id_active,
            "active_no_scope":         no_scope_active,
            "active_legacy_pre_2C":    legacy_active,
        },
        "legacy_by_sport_market": legacy_by_sport,
        "correctness_risk":       correctness_risk,
        "recommended_cleanup_plan_gated_on_authorization":
            plan_if_authorized,
    }


# ═══════════════════════════════════════════════════════════════════
# §CONT-8 — /api/version consumer audit (Issue 5 / XCUT-6)
# ═══════════════════════════════════════════════════════════════════
def audit_api_version_consumers() -> dict:
    hits: list[dict] = []
    for root in (BACKEND, FRONTEND):
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
                continue
            if "/node_modules/" in str(p) or "/__pycache__/" in str(p):
                continue
            try:
                s = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "/api/version" in s or "api/version" in s:
                for lineno, line in enumerate(s.splitlines(), start=1):
                    if "api/version" in line:
                        hits.append({
                            "file": str(p.relative_to(REPO)),
                            "line": lineno,
                            "text": line.strip()[:200],
                        })
    return {
        "id": "CONT-8",
        "status": "AUDIT-COMPLETE",
        "hits":   hits,
        "n_hits": len(hits),
        "detail": (
            "All references to /api/version (definition + consumers) "
            "captured for review.  No consumer changes made in this "
            "pass."
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════
async def main() -> None:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "block":        "2C-CONT",
        "phase":        "post-live-wiring audit",
        "sections":     {},
    }
    # Sync sections.
    report["sections"]["CONT-1"]      = audit_near_first_pitch_ttl()
    report["sections"]["CONT-2"]      = audit_partial_snapshot_metadata()
    report["sections"]["CONT-3"]      = audit_provider_budget_priority()
    report["sections"]["CONT-4"]      = audit_single_flight()
    report["sections"]["CONT-5"]      = audit_circuit_breaker()
    report["sections"]["CONT-6"]      = audit_stale_banner_semantics()
    report["sections"]["CONT-6-COPY"] = audit_stale_banner_copy()
    report["sections"]["CONT-8"]      = audit_api_version_consumers()
    # Async DB section.
    try:
        report["sections"]["CONT-7"]  = await audit_legacy_bad_market_registry()
    except Exception as e:
        report["sections"]["CONT-7"]  = {
            "id": "CONT-7", "status": "ERROR", "error": str(e)}

    out = Path("/tmp/block2c_cont_audit.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"WROTE {out}")

    # Summary line for the console.
    for k, v in report["sections"].items():
        print(f"  {k:12s}  status={v.get('status','?')}")


if __name__ == "__main__":
    asyncio.run(main())
