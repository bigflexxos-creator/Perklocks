"""
Iteration 147 narrow certification — surgical production closure checks.

Covers:
- Iter 144: /api/picks/today?lite=true truth_manifest shape, generation_state,
            secret-free, pick_count == len(picks); board stability over 5 polls.
- Iter 146: /api/probability-authority/shadow-report mode=='active',
            exactly one PROMOTED family == MLB_HRR;
            POST /promote/SOCCER_TOTAL -> 409; POST /promote/NFL_PASS_YARDS -> 404.
- Historical Intelligence: pick 6d76acfa... AVAILABLE_WITH_DATA, n=10, hits=10;
  plus spot-check NFL picks for Rush Yds / Reception Yds/Receiving / Receptions.
"""
import os
import re
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"
RECORD_PICK = "6d76acfa-6c03-560f-b0b8-8a1626adeee6"

SECRET_PATTERN = re.compile(r"(mongo|password|passwd|secret|api_key|apikey|access_key|token)", re.IGNORECASE)


@pytest.fixture(scope="session")
def auth_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"auth login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no token in login response: {r.json()}"
    return tok


@pytest.fixture(scope="session")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


def _get_lite_board(auth_headers, retries=6, delay=5):
    """GET /api/picks/today?lite=true — retry while generation_state == 'BUILDING'."""
    last = None
    for _ in range(retries):
        r = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"picks/today?lite=true -> {r.status_code} {r.text[:300]}"
        body = r.json()
        manifest = body.get("truth_manifest") or {}
        state = manifest.get("generation_state")
        last = (body, manifest, state)
        if state != "BUILDING":
            return body, manifest, state
        time.sleep(delay)
    return last


def _scan_for_secrets(obj, path="root"):
    """Return list of paths where a secret-like key/value appears."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SECRET_PATTERN.search(str(k)):
                hits.append(f"{path}.{k}")
            if isinstance(v, str) and SECRET_PATTERN.search(v):
                # value contains "mongo"/"password"/etc as a substring
                hits.append(f"{path}.{k}(value)")
            hits.extend(_scan_for_secrets(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_scan_for_secrets(v, f"{path}[{i}]"))
    return hits


# ── Iter 144: board generation truth manifest ──────────────────────────────────

class TestBoardGenerationTruthManifest:
    def test_manifest_shape_and_state(self, auth_headers):
        body, manifest, state = _get_lite_board(auth_headers)
        assert manifest, f"no truth_manifest in response, keys={list(body.keys())}"
        assert state in ("COMMITTED", "LEGACY_UNTRACKED"), f"unexpected generation_state={state!r}"

        # Required manifest fields
        assert "generation_id" in manifest and manifest["generation_id"], f"generation_id missing/empty: {manifest.get('generation_id')!r}"
        assert isinstance(manifest.get("revision"), int), f"revision not int: {type(manifest.get('revision')).__name__}={manifest.get('revision')!r}"
        assert manifest.get("schema_version") == "board.v1", f"schema_version={manifest.get('schema_version')!r}"

        picks = body.get("picks") or []
        assert manifest.get("pick_count") == len(picks), f"pick_count={manifest.get('pick_count')} != len(picks)={len(picks)}"

    def test_manifest_has_no_secret_like_keys(self, auth_headers):
        _body, manifest, _state = _get_lite_board(auth_headers)
        hits = _scan_for_secrets(manifest, "truth_manifest")
        # allowed: 'authority' key name (not secret); ensure we don't misflag it
        real_hits = [h for h in hits if not h.endswith(".authority")]
        assert not real_hits, f"secret-like keys/values in truth_manifest: {real_hits}"

    def test_board_stable_across_5_polls(self, auth_headers):
        """Poll 5 times over ~20s — generation_id/board_version must not flap
        between different values more than once."""
        gen_ids, board_versions = [], []
        for i in range(5):
            body, manifest, state = _get_lite_board(auth_headers, retries=6, delay=5)
            gen_ids.append(manifest.get("generation_id"))
            board_versions.append(manifest.get("board_version"))
            if i < 4:
                time.sleep(5)

        def _transitions(seq):
            return sum(1 for a, b in zip(seq, seq[1:]) if a != b)

        gen_trans = _transitions(gen_ids)
        bv_trans = _transitions(board_versions)
        assert gen_trans <= 1, f"generation_id flapped {gen_trans} times over 5 polls: {gen_ids}"
        assert bv_trans <= 1, f"board_version flapped {bv_trans} times over 5 polls: {board_versions}"


# ── Iter 146: probability-authority active mode with MLB_HRR promoted ─────────

class TestProbabilityAuthorityPromotion:
    def test_shadow_report_mode_active_and_mlb_hrr_promoted(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/probability-authority/shadow-report",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"shadow-report -> {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("mode") == "active", f"mode={body.get('mode')!r}"

        families = body.get("families")
        promoted = []
        if isinstance(families, dict):
            for fam_key, fam in families.items():
                readiness = (fam or {}).get("readiness") or {}
                if readiness.get("verdict") == "PROMOTED":
                    promoted.append(fam_key)
        elif isinstance(families, list):
            for fam in families:
                readiness = (fam or {}).get("readiness") or {}
                if readiness.get("verdict") == "PROMOTED":
                    key = fam.get("family") or fam.get("family_key") or fam.get("key")
                    promoted.append(key)
        else:
            pytest.fail(f"unexpected families type: {type(families).__name__}")
        assert promoted == ["MLB_HRR"], f"expected exactly [MLB_HRR] PROMOTED; got {promoted}"

    def test_promote_soccer_total_returns_409(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/probability-authority/promote/SOCCER_TOTAL",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 409, f"promote SOCCER_TOTAL -> {r.status_code} {r.text[:300]}"

    def test_promote_nfl_pass_yards_returns_404(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/probability-authority/promote/NFL_PASS_YARDS",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 404, f"promote NFL_PASS_YARDS -> {r.status_code} {r.text[:300]}"


# ── HI: record pick + NFL spot-checks ─────────────────────────────────────────

class TestHistoricalIntelligence:
    def test_record_pick_available_with_data(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/{RECORD_PICK}/historical-intelligence",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"HI record pick -> {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("status") == "AVAILABLE_WITH_DATA", f"status={body.get('status')!r}"
        assert body.get("sample_size") == 10, f"sample_size={body.get('sample_size')!r}"
        assert body.get("hits") == 10, f"hits={body.get('hits')!r}"
        served_by = body.get("served_by") or {}
        assert served_by.get("authority"), f"served_by.authority missing: {served_by}"

    def test_nfl_spot_check_variants(self, auth_headers):
        """Find one NFL pick each for Rush Yds, Reception Yds/Receiving, Receptions."""
        body, _manifest, _state = _get_lite_board(auth_headers)
        picks = body.get("picks") or []

        # Filter to NFL picks
        nfl_picks = []
        for p in picks:
            sport = (p.get("sport") or p.get("league") or "").upper()
            if "NFL" in sport:
                nfl_picks.append(p)

        def _find(matcher):
            for p in nfl_picks:
                m = (p.get("market") or "").lower()
                if matcher(m):
                    return p
            return None

        variants = {
            "Rush Yds": lambda m: "rush" in m and ("yd" in m or "yard" in m),
            "Reception Yds/Receiving": lambda m: ("recept" in m or "receiv" in m) and ("yd" in m or "yard" in m),
            "Receptions": lambda m: "recept" in m and "yd" not in m and "yard" not in m,
        }

        checked = 0
        skipped = []
        for label, matcher in variants.items():
            pick = _find(matcher)
            if not pick:
                skipped.append(label)
                continue
            pid = pick.get("pick_id") or pick.get("id")
            assert pid, f"no pick_id on found pick for {label}: {pick}"
            r = requests.get(
                f"{BASE_URL}/api/picks/{pid}/historical-intelligence",
                headers=auth_headers,
                timeout=30,
            )
            assert r.status_code == 200, f"HI[{label}] pid={pid} -> {r.status_code} {r.text[:300]}"
            hi = r.json()
            assert "status" in hi, f"HI[{label}] missing status: {hi}"
            assert hi.get("status") != "QUERY_FAILED", (
                f"HI[{label}] pid={pid} returned status QUERY_FAILED on 200"
            )
            checked += 1

        # We require at least one NFL variant available; report skipped
        assert checked >= 1, f"could not find any NFL variant on board; skipped={skipped}"
        # Emit info about coverage
        print(f"[HI NFL spot-check] variants checked={checked}, skipped={skipped}")
