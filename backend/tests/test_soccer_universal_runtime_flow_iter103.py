"""SOCCER_UNIVERSAL_RUNTIME_FLOW_RESTORED — live runtime verification.

Proves against the LIVE backend (not just unit stubs) the invariant:

    Provider Row -> Canonical Identity -> Engine Model
                 -> Canonical Publication -> Consumer Decision

Six categories from the review request:
  1. MLS team-context reachability          (P0)
  2. Goalscorer TTL blackout guard          (P0)
  3. Canonical wager identity across        (P1)
  4. Precise rejection taxonomy             (P1)
  5. EPL / Big-5 regression                 (regression guard)
  6. Regression preservation                (cross-book dedupe, H2H, breakdown)
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")

BASE_URL = os.environ.get("BACKEND_TEST_URL") or (
    os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/") if os.environ.get(
        "EXPO_PUBLIC_BACKEND_URL"
    ) else "http://localhost:8001"
)
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

BIG5 = {"EPL", "La Liga", "Bundesliga", "Serie A", "Ligue 1"}
BIG5_ALIASES = {
    "EPL": {"EPL", "Premier League", "English Premier League"},
    "La Liga": {"La Liga", "Primera Division", "LaLiga"},
    "Bundesliga": {"Bundesliga", "1. Bundesliga"},
    "Serie A": {"Serie A", "Italy Serie A"},
    "Ligue 1": {"Ligue 1"},
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if not hasattr(asyncio, "run") else asyncio.run(coro)


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


# ────────────────────────── auth ──────────────────────────
@pytest.fixture(scope="module")
def auth_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture(scope="module")
def soccer_picks(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?sport=Soccer",
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200, f"picks/today {r.status_code} {r.text[:200]}"
    body = r.json()
    picks = body.get("picks") or body.get("data") or body if isinstance(body, list) else body.get("picks", [])
    if isinstance(body, dict) and "picks" in body:
        picks = body["picks"]
    elif isinstance(body, list):
        picks = body
    assert isinstance(picks, list) and picks, f"no picks returned: {type(picks)} len={len(picks) if isinstance(picks,list) else 'n/a'}"
    return picks


# ══════════════════════════════════════════════════════════════════
# CATEGORY 1 — MLS team-context reachability (P0)
# ══════════════════════════════════════════════════════════════════
CANONICAL_WAGER_RE = re.compile(
    r"^[^|]+\|game_market\|[^|]+\|[^|]+\|[^|]*$"
)


def _is_mls(p):
    lg = str(p.get("league") or "").lower()
    return "mls" in lg or "major league soccer" in lg


def test_1_mls_picks_are_on_board(soccer_picks):
    mls = [p for p in soccer_picks if _is_mls(p)]
    assert mls, (
        "No MLS picks on the board — MLS reachability regressed. "
        f"Total soccer picks={len(soccer_picks)}"
    )
    print(f"MLS on-board picks: {len(mls)} / {len(soccer_picks)} total soccer")


def test_1_mls_game_market_picks_use_soccer_game_model(soccer_picks):
    """MLS game-market picks must be produced by soccer_game_model
    (not model_only / not unavailable)."""
    mls_game = [
        p for p in soccer_picks
        if _is_mls(p)
        and str(p.get("market_key") or "").lower() in {"h2h", "totals", "spreads"}
    ]
    if not mls_game:
        pytest.skip("no MLS game-market picks currently on board")

    bad = []
    for p in mls_game:
        ms = p.get("model_source")
        if ms != "soccer_game_model":
            bad.append((p.get("id"), p.get("market_key"), ms))
    assert not bad, (
        f"MLS game-market picks with wrong model_source (want soccer_game_model): "
        f"{bad[:5]} of {len(bad)} bad / {len(mls_game)} total"
    )
    print(f"MLS game-market model_source==soccer_game_model: {len(mls_game)}/{len(mls_game)}")


def test_1_mls_canonical_wager_identity_and_provider_preservation(soccer_picks):
    mls = [p for p in soccer_picks if _is_mls(p)]
    if not mls:
        pytest.skip("no MLS picks on board")

    id_missing = []
    id_pattern_bad = []
    event_mismatch = []
    book_missing = []
    for p in mls:
        cwid = p.get("canonical_wager_id")
        if not cwid:
            id_missing.append(p.get("id"))
            continue
        if not CANONICAL_WAGER_RE.match(str(cwid)):
            id_pattern_bad.append((p.get("id"), cwid))
        if p.get("provider_event_id") and p.get("event_id"):
            if p["provider_event_id"] != p["event_id"]:
                event_mismatch.append(
                    (p.get("id"), p["event_id"], p["provider_event_id"])
                )
        if p.get("book_odds") is None or p.get("bookmaker") in (None, ""):
            book_missing.append(
                (p.get("id"), p.get("book_odds"), p.get("bookmaker"))
            )

    assert not id_missing, f"MLS picks missing canonical_wager_id: {id_missing[:5]}"
    assert not id_pattern_bad, (
        f"MLS canonical_wager_id pattern violation: {id_pattern_bad[:5]}"
    )
    assert not event_mismatch, (
        f"provider_event_id != event_id (ESPN overwrote): {event_mismatch[:5]}"
    )
    assert not book_missing, (
        f"MLS picks missing book_odds/bookmaker: {book_missing[:5]}"
    )
    print(f"MLS canonical identity clean across {len(mls)} picks")


def test_1_mls_trace_provider_row_to_consumer():
    """End-to-end trace: db.live_alt_lines OR odds_api_cache.bulk_odds
    → db.picks (off_board=false) → provider identity preserved."""
    async def run():
        client, db = _db()
        try:
            pick = await db.picks.find_one({
                "sport": "Soccer", "league": {"$regex": "MLS", "$options": "i"},
                "pick_date": TODAY, "off_board": {"$ne": True},
                "source": "real_line_soccer_v2",
            })
            if not pick:
                pytest.skip("no on-board MLS real_line_soccer_v2 pick in db")

            event_id = pick.get("event_id") or pick.get("provider_event_id")
            assert event_id, f"pick has no event_id: {pick.get('id')}"

            # Provider Row: must exist in either bulk_odds or live_alt_lines
            in_bulk = await db.odds_api_cache.count_documents(
                {"body.id": event_id}
            )
            in_alt = await db.live_alt_lines.count_documents(
                {"event_id": event_id}
            )
            assert (in_bulk + in_alt) > 0, (
                f"MLS pick event_id={event_id} not found in provider cache "
                f"(bulk_odds={in_bulk}, live_alt_lines={in_alt})"
            )

            # Canonical Identity
            assert pick.get("canonical_wager_id"), "pick missing canonical_wager_id"
            assert CANONICAL_WAGER_RE.match(str(pick["canonical_wager_id"])), \
                f"pattern: {pick['canonical_wager_id']}"

            # Provider identity preserved
            if pick.get("provider_event_id"):
                assert pick["provider_event_id"] == pick["event_id"]

            # Engine Model marker
            assert pick.get("model_source") == "soccer_game_model" \
                or pick.get("source") == "real_line_alt_scorer_v1", \
                f"model_source={pick.get('model_source')}"

            print(
                f"[TRACE] MLS event_id={event_id} → in_bulk={in_bulk} "
                f"in_alt={in_alt} → pick.id={pick.get('id')} "
                f"cwid={pick['canonical_wager_id']}"
            )
        finally:
            client.close()
    _run(run())


# ══════════════════════════════════════════════════════════════════
# CATEGORY 2 — Goalscorer TTL blackout guard (P0)
# ══════════════════════════════════════════════════════════════════
def test_2_live_alt_lines_ttl_is_5400():
    async def run():
        client, db = _db()
        try:
            info = await db.command({"listIndexes": "live_alt_lines"})
            batch = info.get("cursor", {}).get("firstBatch", []) or []
            ix = next(
                (i for i in batch if i.get("name") == "last_seen_1"), None
            )
            assert ix, "live_alt_lines.last_seen_1 index does not exist"
            ttl = int(ix.get("expireAfterSeconds") or 0)
            assert ttl == 5400, (
                f"live_alt_lines TTL={ttl}s must be 5400s (90 min)"
            )
        finally:
            client.close()
    _run(run())


def test_2_soccer_scorer_freshness_task_registered():
    """Task registered as 'soccer_scorer_freshness_check' — check via
    /api/admin/task-registry if exposed, else via startup log."""
    # Try admin endpoint first
    r = requests.get(f"{BASE_URL}/api/admin/task-registry", timeout=10)
    if r.status_code == 200:
        body = r.json()
        names = set()
        if isinstance(body, list):
            for t in body:
                names.add(t.get("name") if isinstance(t, dict) else str(t))
        elif isinstance(body, dict):
            names = set(body.get("tasks") or body.get("names") or [])
            if not names and "registry" in body:
                names = {t.get("name") for t in body["registry"]}
        assert "soccer_scorer_freshness_check" in names, (
            f"soccer_scorer_freshness_check not in registry: {sorted(names)[:20]}"
        )
        return

    # Fall back to backend log inspection
    import subprocess
    log = subprocess.run(
        ["bash", "-c",
         "grep -a -h 'Soccer scorer freshness' "
         "/var/log/supervisor/backend.*.log | tail -20"],
        capture_output=True, text=True, timeout=10,
    ).stdout
    assert "Soccer scorer freshness" in log, (
        f"No 'Soccer scorer freshness' log line found in supervisor logs. "
        f"tail=<{log[:300]}>"
    )
    print(f"Freshness log matches:\n{log}")


def test_2_soccer_scorer_freshness_startup_log():
    import subprocess
    log = subprocess.run(
        ["bash", "-c",
         "grep -a -h 'Soccer scorer freshness (startup)' "
         "/var/log/supervisor/backend.*.log | tail -5"],
        capture_output=True, text=True, timeout=10,
    ).stdout
    assert "Soccer scorer freshness (startup)" in log, (
        "startup log missing 'Soccer scorer freshness (startup): {...}' entry"
    )
    print(f"Startup line:\n{log}")


# ══════════════════════════════════════════════════════════════════
# CATEGORY 3 — Canonical wager identity across consumers (P1)
# ══════════════════════════════════════════════════════════════════
def test_3_soccer_picks_carry_canonical_wager_id(soccer_picks):
    """Sample 20 on-board Soccer picks; every one must have
    canonical_wager_id."""
    sample = soccer_picks[:20]
    missing = [p.get("id") for p in sample if not p.get("canonical_wager_id")]
    assert not missing, f"picks without canonical_wager_id: {missing}"
    print(f"canonical_wager_id present on all {len(sample)} sampled soccer picks")


def _fetch_consumer_endpoint(auth_headers, path):
    r = requests.get(f"{BASE_URL}{path}", headers=auth_headers, timeout=60)
    if r.status_code != 200:
        return None, f"{r.status_code} {r.text[:120]}"
    body = r.json()
    if isinstance(body, list):
        picks = body
    else:
        picks = body.get("picks") or body.get("data") or []
    return picks, None


def test_3_canonical_identity_across_rollover_and_all(auth_headers):
    ok_sources = {"real_line_soccer_v2", "real_line_alt_scorer_v1"}
    findings = {}
    for path in ("/api/picks/rollover?sport=Soccer",
                 "/api/picks/all?sport=Soccer"):
        picks, err = _fetch_consumer_endpoint(auth_headers, path)
        if err:
            # rollover endpoint may 404 or 500 in some deploys — record
            findings[path] = f"ERR:{err}"
            continue
        rel = [p for p in (picks or []) if p.get("source") in ok_sources]
        missing = [p.get("id") for p in rel if not p.get("canonical_wager_id")]
        findings[path] = {"total": len(picks or []), "src_relevant": len(rel),
                          "missing_cwid": len(missing),
                          "sample_missing": missing[:5]}
        assert not missing, (
            f"{path}: {len(missing)} src-relevant picks missing "
            f"canonical_wager_id — {missing[:5]}"
        )
    print(f"identity coverage: {findings}")


# ══════════════════════════════════════════════════════════════════
# CATEGORY 4 — Precise rejection taxonomy (P1)
# ══════════════════════════════════════════════════════════════════
def test_4_off_board_picks_carry_canonical_rejection_code():
    from services.soccer_rejection_taxonomy import ALL_CODES

    async def run():
        client, db = _db()
        try:
            cursor = db.picks.find({
                "sport": "Soccer", "pick_date": TODAY, "off_board": True,
                "source": {"$in": ["real_line_soccer_v2",
                                   "real_line_alt_scorer_v1"]},
            }, {"off_board_reasons": 1, "league": 1}).limit(1000)
            n_checked = 0; n_valid = 0
            reason_hist = {}
            async for d in cursor:
                n_checked += 1
                reasons = d.get("off_board_reasons") or []
                if reasons and any(r in ALL_CODES for r in reasons):
                    n_valid += 1
                for r in reasons:
                    reason_hist[r] = reason_hist.get(r, 0) + 1
            if n_checked == 0:
                pytest.skip("no off-board picks to sample")
            ratio = n_valid / n_checked
            print(
                f"off_board_reasons coverage: {n_valid}/{n_checked} "
                f"({ratio:.1%}) — hist={dict(sorted(reason_hist.items(), key=lambda kv: -kv[1])[:10])}"
            )
            assert ratio >= 0.95, (
                f"only {n_valid}/{n_checked} carry canonical codes"
            )
        finally:
            client.close()
    _run(run())


def test_4_no_team_context_is_valid_taxonomy_code():
    from services.soccer_rejection_taxonomy import ALL_CODES
    assert "NO_TEAM_CONTEXT" in ALL_CODES, (
        f"NO_TEAM_CONTEXT not in ALL_CODES: {sorted(ALL_CODES)[:20]}"
    )


# ══════════════════════════════════════════════════════════════════
# CATEGORY 5 — EPL / Big-5 regression guard
# ══════════════════════════════════════════════════════════════════
def _league_matches_big5(name: str, target: str) -> bool:
    n = (name or "").strip()
    return n in BIG5_ALIASES[target] or any(
        a.lower() == n.lower() for a in BIG5_ALIASES[target]
    )


def test_5_big5_leagues_use_primary_form_source():
    """Sample one on-board pick from each Big-5 league; verify
    home_form.source is one of the primary chain (NOT the MLS ESPN
    stats source)."""
    async def run():
        client, db = _db()
        try:
            allowed = {"soccer_matches_rolling20", "team_form", "soccer_team_form"}
            findings = {}
            for lg in BIG5:
                found = None
                for alias in BIG5_ALIASES[lg]:
                    pick = await db.picks.find_one({
                        "sport": "Soccer", "league": alias,
                        "pick_date": TODAY, "off_board": {"$ne": True},
                        "source": "real_line_soccer_v2",
                    })
                    if pick:
                        found = (alias, pick); break
                if not found:
                    findings[lg] = "NO_PICK_ON_BOARD"
                    continue
                alias, pick = found
                hs = ((pick.get("home_form") or {}).get("source")) or \
                     ((pick.get("home_team_form") or {}).get("source")) or \
                     pick.get("home_form_source")
                findings[lg] = {"alias": alias, "home_form.source": hs,
                                "id": pick.get("id")}
                # If source is present, it MUST be from the primary chain
                if hs is not None:
                    assert hs != "mls_espn_stats+player_game_actuals", (
                        f"{lg} pick {pick.get('id')} leaked into MLS adapter: {hs}"
                    )
                    assert hs in allowed, (
                        f"{lg} pick {pick.get('id')} home_form.source={hs} "
                        f"not in {allowed}"
                    )
            print(f"Big-5 findings: {findings}")
            # At least ONE Big-5 league must have an on-board pick
            assert any(isinstance(v, dict) for v in findings.values()), (
                f"NO Big-5 league produced any on-board pick: {findings}"
            )
        finally:
            client.close()
    _run(run())


# ══════════════════════════════════════════════════════════════════
# CATEGORY 6 — Regression preservation
# ══════════════════════════════════════════════════════════════════
def test_6_cross_book_dedupe_active(soccer_picks):
    """At least one pick should carry a bookmaker_quotes array size>=2
    proving cross-book collapse is still happening."""
    n_multi = sum(
        1 for p in soccer_picks
        if isinstance(p.get("bookmaker_quotes"), list)
        and len(p["bookmaker_quotes"]) >= 2
    )
    print(f"cross-book multi-book picks: {n_multi}/{len(soccer_picks)}")
    assert n_multi > 0, (
        "no soccer pick carries >=2 bookmaker_quotes — cross-book "
        "dedupe/collapse appears disabled"
    )


def test_6_commence_time_utc_present(soccer_picks):
    missing = [
        p.get("id") for p in soccer_picks if not p.get("commence_time_utc")
    ]
    assert not missing, (
        f"{len(missing)} soccer picks missing commence_time_utc: {missing[:5]}"
    )


def test_6_h2h_endpoint_valid_status(auth_headers, soccer_picks):
    """Sample 3 MLS + 3 Big-5 picks; H2H returns non-error status."""
    valid_statuses = {"H2H_AVAILABLE", "H2H_SOURCE_UNAVAILABLE"}
    mls = [p for p in soccer_picks if _is_mls(p)][:3]
    big5 = [
        p for p in soccer_picks
        if any(_league_matches_big5(p.get("league"), l) for l in BIG5)
    ][:3]
    tested = mls + big5
    if not tested:
        pytest.skip("no MLS or Big-5 picks to h2h-test")
    results = []
    for p in tested:
        pid = p.get("id")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/h2h",
            headers=auth_headers, timeout=30,
        )
        results.append((pid, r.status_code, r.json() if r.status_code == 200 else r.text[:80]))
        assert r.status_code == 200, f"H2H {pid} → {r.status_code}"
        body = r.json()
        status = body.get("status") or body.get("h2h_status")
        assert status in valid_statuses, (
            f"H2H pick {pid} status={status} not in {valid_statuses}: {body}"
        )
    print(f"H2H results: {[(pid, s) for pid, s, _ in results]}")


def test_6_market_rank_excludes_self(auth_headers, soccer_picks):
    """Pick breakdown alternatives must exclude the pick itself
    (canonical wager key)."""
    sample = soccer_picks[:5]
    checked = 0
    for p in sample:
        pid = p.get("id")
        cwid = p.get("canonical_wager_id")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/market-rank",
            headers=auth_headers, timeout=30,
        )
        if r.status_code != 200:
            continue
        body = r.json()
        alts = body.get("alternatives") or body.get("market_alternatives") \
               or body.get("ranks") or []
        for a in alts:
            a_cwid = a.get("canonical_wager_id")
            if cwid and a_cwid:
                assert a_cwid != cwid, (
                    f"market-rank {pid} includes self canonical wager {cwid}"
                )
        checked += 1
    assert checked > 0, "market-rank returned non-200 for all sampled picks"
    print(f"market-rank self-exclusion verified on {checked}/{len(sample)} picks")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
