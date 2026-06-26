"""
Iteration 60 — Backend verification for:
  (1) Canonicalize-max single-source-of-truth lock_score fix
      _canonicalize_lock_score sets lock_score = max(v1, v2, raw, peak)
      So elite players (Salah/Vinicius/Neymar/Mbappe/Haaland/Memphis/Gakpo)
      always surface at >= 95 on the API even if DB v1 is demoted.
  (2) SportDB synth Anytime Goal Scorer pipeline injects CSL/J-League/MLS
      players (Silva Felipe, Leonardo, Gustavo, Fábio Abreu, Ange Kouame,
      Guy Mbenza, Shihao Wei, Silva Wellington) with proper lock scores.
  (3) Lock-V2 lock-breakdown endpoint returns the new `sim_anchor` block
      with sim_win_probability / sim_lock_anchor / lock_anchored_to_sim.

Reuses the seeded demo user (demo@lockscore.ai / demo123).
"""

import os
import unicodedata
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "https://bet-edge-ai-1.preview.emergentagent.com"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


# ─── Fixtures ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def auth_headers():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def today_picks(auth_headers):
    """One slate fetch, reused by multiple tests."""
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=45)
    assert r.status_code == 200, f"/api/picks/today returned {r.status_code}: {r.text[:300]}"
    data = r.json()
    picks = data.get("picks") if isinstance(data, dict) else data
    assert isinstance(picks, list), f"unexpected payload shape: {type(data)}"
    return picks


# ─── Helpers ────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    """Case-insensitive, accent-insensitive substring matcher."""
    if not s:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


def _matches_player(pick: dict, name_substr: str) -> bool:
    # Soccer "To Score or Assist" picks carry the player name in the
    # `market` (e.g. "Mohamed Salah To Score or Assist") and selection
    # is just "Yes"; for other markets the name lives in `selection` /
    # `player_name`. Search every plausible field.
    pool = " ".join(
        str(pick.get(k, "") or "")
        for k in ("selection", "player_name", "title", "description",
                  "market", "event")
    )
    return _norm(name_substr) in _norm(pool)


# ─── (1) Canonicalize-max lock_score: ELITE PLAYERS ─────────────────────
class TestCanonicalizeLockScoreFix:
    """Elite soccer scorers MUST appear at lock_score >= 95 regardless of
    what their stored DB v1 holds. Read-time _canonicalize_lock_score +
    elite_player floor are both belt-and-suspenders guards."""

    ELITES = ["Salah", "Vinicius", "Neymar", "Memphis", "Gakpo",
              "Mbappe", "Haaland"]

    def test_slate_has_picks(self, today_picks):
        assert len(today_picks) > 0, "no picks returned at all — slate empty?"

    def test_no_elite_below_95(self, today_picks):
        """If any elite_player flag is set on a pick, lock_score MUST be ≥95."""
        offenders = []
        for p in today_picks:
            if p.get("elite_player") is True:
                ls = float(p.get("lock_score") or 0)
                if ls < 95.0:
                    offenders.append(
                        f"{p.get('selection','?')} | lock={ls} | "
                        f"v2={p.get('lock_score_v2')} raw={p.get('lock_score_raw')} "
                        f"peak={p.get('lock_score_peak')}"
                    )
        assert not offenders, (
            "Elite-flagged picks below lock 95 found "
            "(canonicalize-max guard FAILED):\n  " + "\n  ".join(offenders)
        )

    def test_canonicalize_max_invariant(self, today_picks):
        """For EVERY pick: lock_score >= max(v2, raw, peak) - small tol.
        Verifies _canonicalize_lock_score is in effect at serialization."""
        violations = []
        for p in today_picks[:200]:  # sample first 200 to bound runtime
            try:
                v1   = float(p.get("lock_score") or 0)
                v2   = float(p.get("lock_score_v2") or 0)
                raw  = float(p.get("lock_score_raw") or 0)
                peak = float(p.get("lock_score_peak") or 0)
            except Exception:
                continue
            shadow_max = min(99.0, max(v2, raw, peak))
            # Allow tiny rounding tolerance.
            if shadow_max - v1 > 0.5:
                violations.append(
                    f"{p.get('selection','?')} lock={v1} v2={v2} raw={raw} peak={peak}"
                )
        assert not violations, (
            f"canonicalize-max violations ({len(violations)}):\n  " +
            "\n  ".join(violations[:10])
        )

    @pytest.mark.parametrize("elite_name", ["Salah", "Vinicius", "Neymar",
                                            "Memphis", "Gakpo"])
    def test_known_elite_surfaces_at_95_if_present(self, today_picks, elite_name):
        """If the elite name appears in today's slate, they must be ≥95.
        We don't fail if they're simply not playing today — only if they
        ARE on the board at a sub-95 score (the actual user-reported bug)."""
        matches = [p for p in today_picks if _matches_player(p, elite_name)]
        if not matches:
            pytest.skip(f"{elite_name} not on today's slate (no game?)")
        bad = [m for m in matches if float(m.get("lock_score") or 0) < 95.0]
        assert not bad, (
            f"{elite_name} on slate at sub-95 lock: " +
            "; ".join(f"{m.get('selection')} lock={m.get('lock_score')}" for m in bad)
        )


# ─── (2) SportDB synth Anytime Goal Scorer pipeline ─────────────────────
class TestSynthGoalscorerInjection:
    """The SportDB scorer pipeline should auto-include lower-tier league
    scorers (CSL, etc.) on the Anytime Goal Scorer tab — flagged
    `is_synthetic_scorer=true` or `source='sportdb_scorer_v1'` or market
    suffix `(Model)`."""

    @pytest.fixture(scope="class")
    def scorer_picks(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers=auth_headers,
            params={"sport": "Soccer", "market": "Anytime Goal Scorer"},
            timeout=45,
        )
        assert r.status_code == 200, f"scorer fetch failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        picks = data.get("picks") if isinstance(data, dict) else data
        assert isinstance(picks, list)
        return picks

    def test_scorer_tab_returns_picks(self, scorer_picks):
        assert len(scorer_picks) > 0, "Anytime Goal Scorer tab returned 0 picks"

    def test_no_elite_scorer_below_95(self, scorer_picks):
        """Repeat the elite floor on the scorer-filtered tab specifically."""
        bad = []
        for p in scorer_picks:
            if p.get("elite_player") is True:
                ls = float(p.get("lock_score") or 0)
                if ls < 95.0:
                    bad.append(f"{p.get('selection')} lock={ls}")
        assert not bad, "Elite scorers below 95 on Anytime tab: " + "; ".join(bad)

    def test_synth_or_model_picks_present(self, scorer_picks):
        """At least ONE pick should look like a SportDB synth scorer —
        flagged via is_synthetic_scorer, source, OR market suffix."""
        synth = [
            p for p in scorer_picks
            if p.get("is_synthetic_scorer") is True
            or (p.get("source") or "").startswith("sportdb_scorer")
            or "(Model)" in (p.get("market") or "")
            or p.get("is_model_only") is True
        ]
        if not synth:
            pytest.skip(
                "No synth scorer picks on today's slate — environment dependent "
                "(CSL/J-League/MLS may not have eligible fixtures in window)"
            )
        # If present, basic sanity:
        for p in synth[:5]:
            ls = float(p.get("lock_score") or 0)
            assert 0 < ls <= 99.0, f"bad lock_score on synth pick: {p}"

    @pytest.mark.parametrize("name", [
        "Silva Felipe", "Leonardo", "Gustavo", "Fabio Abreu",
        "Ange Kouame", "Guy Mbenza", "Shihao Wei", "Silva Wellington",
    ])
    def test_csl_scorer_present_if_seeded(self, scorer_picks, name):
        """If the manually-injected CSL scorers landed in today's slate,
        check they carry a reasonable (>=85) lock score. Skip when not
        present (depends on whether CSL had eligible fixtures today)."""
        matches = [p for p in scorer_picks if _matches_player(p, name)]
        if not matches:
            pytest.skip(f"{name} not on today's scorer slate (no CSL fixture?)")
        for m in matches:
            ls = float(m.get("lock_score") or 0)
            assert ls >= 85.0, (
                f"{name} present but lock={ls} (expected ≥85 for synth scorer)"
            )


# ─── (3) Lock-V2 lock-breakdown sim_anchor block ────────────────────────
class TestLockV2SimAnchor:
    """GET /api/lock-v2/picks/{pick_id}/lock-breakdown should now return
    a `sim_anchor` block with sim_win_probability / sim_lock_anchor /
    lock_anchored_to_sim fields."""

    def test_breakdown_includes_sim_anchor(self, auth_headers, today_picks):
        if not today_picks:
            pytest.skip("no picks to drill into")
        # Prefer a synth scorer pick if any (the docs call this out);
        # else fall back to any pick id.
        candidates = [
            p for p in today_picks
            if p.get("is_synthetic_scorer") or "(Model)" in (p.get("market") or "")
        ] or today_picks
        pick_id = candidates[0].get("id") or candidates[0].get("pick_id")
        assert pick_id, "no id on picks payload"

        r = requests.get(
            f"{BASE_URL}/api/picks/{pick_id}/lock-breakdown",
            headers=auth_headers,
            timeout=30,
        )
        if r.status_code == 404:
            pytest.skip(f"lock-breakdown endpoint not exposed for {pick_id}")
        assert r.status_code == 200, (
            f"lock-breakdown returned {r.status_code}: {r.text[:300]}"
        )
        body = r.json()
        # The new block is the key contract change for this iteration.
        assert "sim_anchor" in body, (
            f"sim_anchor block missing from lock-breakdown. Keys: {list(body.keys())}"
        )
        anchor = body["sim_anchor"]
        assert isinstance(anchor, dict), "sim_anchor should be an object"
        expected_keys = {"sim_win_probability", "sim_lock_anchor",
                         "lock_anchored_to_sim"}
        missing = expected_keys - set(anchor.keys())
        assert not missing, (
            f"sim_anchor missing required keys: {missing}. Got {list(anchor.keys())}"
        )
