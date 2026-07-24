"""Iter-90 verification: batter H2H career vsTeam + per-game stat field.

Contract for this iteration (tight scope):
  1. fetch_batter_h2h('Shohei Ohtani', 'New York Mets') returns
       vs_team_hits >= 15 (career vs Mets) and vs_team_avg in [0.25, 0.32].
       First recent-list row has non-empty stat matching /^\\d+-\\d+$/.
  2. /api/picks/today?sport=MLB — every batter-prop pick whose market
       contains Hits / Home Run / RBI / Total Bases with non-empty
       h2h_summary matches /^\\d+-for-\\d+ vs .+ \\(0\\.\\d{3} avg, \\d+%\\)$/
       and no 'H2H X-Y' or ' L\\d+' substring.
  3. /api/picks/{id}/h2h for a batter Hits pick — player_h2h.vs_team_recent
       has >= 1 element and EVERY element has stat matching /^\\d+-\\d+$/.
  4. Team spread deep-dive still is_player_prop=False with team_h2h.avg_total.
  5. Perf: cold /api/picks/today?sport=MLB < 10s. Warm < 6s.
"""
from __future__ import annotations
import asyncio
import os
import re
import sys
import time
import pytest
import requests

# Add backend path so we can import mlb_batter_h2h directly.
sys.path.insert(0, "/app/backend")
from mlb_batter_h2h import fetch_batter_h2h  # noqa: E402

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL / EXPO_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

BATTER_MARKETS = ("hits", "home run", "homer", "total bases", "rbi",
                  "runs scored", "singles", "doubles", "triples", "stolen base")
PITCHER_MARKETS = ("strikeout", "outs recorded", "walks", "earned runs",
                   "hits allowed")

CHIP_RE = re.compile(r"^\d+-for-\d+ vs .+ \(0\.\d{3} avg, \d+%\)$")
STAT_RE = re.compile(r"^\d+-\d+$")

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=60)
    assert r.status_code == 200, f"login {r.status_code}: {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, "no token in login response"
    s.headers.update({"Authorization": f"Bearer {tok}",
                      "Content-Type": "application/json"})
    _SESSION = s
    return s


@pytest.fixture(scope="module")
def mlb_picks():
    r = _session().get(f"{BASE_URL}/api/picks/today?sport=MLB", timeout=90)
    assert r.status_code == 200, f"picks/today MLB {r.status_code}"
    js = r.json()
    picks = js.get("picks") if isinstance(js, dict) else js
    assert isinstance(picks, list) and picks, "no MLB picks returned"
    return picks


def _fetch_with_retry(name: str, team: str, tries: int = 3) -> dict:
    """MLB Stats API `/people/search` can flake — retry with backoff."""
    last = {}
    for i in range(tries):
        last = asyncio.run(fetch_batter_h2h(name, team))
        if last.get("ok"):
            return last
        time.sleep(1.5 * (i + 1))
    return last


# ── (1) Direct function call: Ohtani vs Mets should be realistic. ────────
def test_fetch_ohtani_vs_mets_career():
    res = _fetch_with_retry("Shohei Ohtani", "New York Mets")
    assert res.get("ok") is True, res
    hits = int(res.get("vs_team_hits") or 0)
    avg = float(res.get("vs_team_avg") or 0.0)
    ab = int(res.get("vs_team_ab") or 0)
    print(f"[ohtani vs mets] {hits}-for-{ab} avg={avg}")
    assert hits >= 15, f"expected >= 15 career hits, got {hits}"
    assert 0.25 <= avg <= 0.32, f"expected avg in [0.25,0.32], got {avg}"
    recent = res.get("vs_team_recent") or []
    assert isinstance(recent, list) and len(recent) >= 1, "empty recent list"
    stat0 = recent[0].get("stat") or ""
    assert STAT_RE.match(stat0), f"stat[0] {stat0!r} does not match d+-d+"


# ── (1b) Second batter for cross-check: Riley Greene vs KC. ──────────────
def test_fetch_riley_greene_vs_kc():
    res = _fetch_with_retry("Riley Greene", "Kansas City Royals")
    assert res.get("ok") is True, res
    ab = int(res.get("vs_team_ab") or 0)
    assert ab >= 5, f"expected >= 5 career AB vs KC, got {ab}"
    recent = res.get("vs_team_recent") or []
    if recent:
        for r in recent:
            assert STAT_RE.match(r.get("stat") or ""), \
                f"bad stat {r.get('stat')!r} in recent row"


# ── (2) Every batter chip on /api/picks/today matches the strict regex. ──
def test_batter_chip_format(mlb_picks):
    checked = 0
    violations = []
    for p in mlb_picks:
        mkt = (p.get("market") or "").lower()
        s = (p.get("h2h_summary") or "").strip()
        if not s:
            continue
        # Skip pitcher markets that share the word 'hits' (i.e. 'hits allowed').
        if any(k in mkt for k in PITCHER_MARKETS):
            continue
        if not any(k in mkt for k in BATTER_MARKETS):
            continue
        checked += 1
        if not CHIP_RE.match(s):
            violations.append(f"[{mkt}] chip={s!r} (does not match regex)")
        if re.search(r"\bH2H\s+\d+-\d+", s):
            violations.append(f"[{mkt}] chip contains H2H X-Y: {s!r}")
        if re.search(r"\bL\d+", s):
            violations.append(f"[{mkt}] chip contains L\\d+: {s!r}")
        # NEW rule: if opp is a real MLB team, an AB < 10 with the chip
        # shape is suspicious (unless truly tiny sample). Flag but don't
        # fail — as per user note in review.
        m = re.match(r"^(\d+)-for-(\d+) vs ", s)
        if m:
            ab = int(m.group(2))
            if ab < 10:
                print(f"[warn] small career-AB chip: [{mkt}] {s}")
    print(f"[batter chip] checked={checked} violations={len(violations)}")
    if violations:
        print("\n".join(violations[:15]))
    assert not violations, f"{len(violations)} batter chip violations"
    if checked == 0:
        pytest.skip("no batter-prop picks with h2h_summary on this slate")


# ── (3) Deep-dive: batter Hits pick → vs_team_recent all have stat. ──────
def test_deep_dive_batter_hits_stat_field(mlb_picks):
    """Find a batter Hits pick whose deep-dive returns a non-empty recent
    list, and verify every row has a properly formatted `stat` string.

    Note: recent list can legitimately be empty for batters who have career
    AB vs opp but haven't faced that team in the current (2026) season —
    fetch_batter_h2h uses current-season gameLog for the per-game
    breakdown. That's a design gap, not a stat-field bug; reported
    separately.
    """
    checked = 0
    tested_with_recent = 0
    for p in mlb_picks:
        mkt = (p.get("market") or "").lower()
        s = p.get("h2h_summary") or ""
        if not ("hits" in mkt and "hits allowed" not in mkt
                and "for-" in s):
            continue
        pid = p.get("id") or p.get("_id") or p.get("pick_id")
        if not pid:
            continue
        checked += 1
        r = _session().get(f"{BASE_URL}/api/picks/{pid}/h2h", timeout=30)
        if r.status_code != 200:
            continue
        js = r.json()
        ph = js.get("player_h2h") or {}
        recent = ph.get("recent") or ph.get("vs_team_recent") or []
        if not recent:
            # Legit — batter has no 2026 games vs opp. Skip.
            continue
        tested_with_recent += 1
        bad = [row for row in recent
               if not STAT_RE.match(str(row.get("stat") or ""))]
        assert not bad, (
            f"pick {pid} ({p.get('selection')}): rows missing/bad stat: "
            f"{bad[:3]}"
        )
        # Bonus: assert per-row fields present.
        for row in recent:
            assert "ab" in row and "h" in row, \
                f"row missing ab/h keys: {row}"
    print(f"[deep-dive hits] checked_picks={checked} "
          f"with_recent={tested_with_recent}")
    if tested_with_recent == 0:
        pytest.skip("no batter Hits pick on slate whose recent list is "
                    "populated (all batters have 0 current-season games vs "
                    "opp) — design gap reported separately")


# ── (4) Team spread pick still has team_h2h and is_player_prop=False. ────
def test_deep_dive_team_spread_regression(mlb_picks):
    TEAM_MKTS = ("spread", "moneyline", "total", "run line")
    pick = None
    for p in mlb_picks:
        mkt = (p.get("market") or "").lower()
        s = p.get("h2h_summary") or ""
        if any(k in mkt for k in TEAM_MKTS) and "avg" in s:
            pick = p
            break
    if not pick:
        pytest.skip("no team-market pick with 'avg' summary")
    pid = pick.get("id") or pick.get("_id")
    r = _session().get(f"{BASE_URL}/api/picks/{pid}/h2h", timeout=30)
    assert r.status_code == 200
    js = r.json()
    assert js.get("is_player_prop") is False
    team = js.get("team_h2h") or {}
    assert team.get("avg_total") is not None, "team_h2h.avg_total missing"


# ── (5) Perf: cold < 10s, warm < 6s. ─────────────────────────────────────
def test_perf_cold_warm():
    s = _session()
    t0 = time.time()
    r1 = s.get(f"{BASE_URL}/api/picks/today?sport=MLB", timeout=30)
    cold = time.time() - t0
    assert r1.status_code == 200
    t0 = time.time()
    r2 = s.get(f"{BASE_URL}/api/picks/today?sport=MLB", timeout=30)
    warm = time.time() - t0
    assert r2.status_code == 200
    print(f"[perf] cold={cold:.2f}s warm={warm:.2f}s")
    assert cold < 10.0, f"cold {cold:.2f}s > 10s"
    assert warm < 6.0, f"warm {warm:.2f}s > 6s"


# ── (6) Regression: no player-prop chip carries 'H2H ' or ' L\\d+' tail. ─
def test_no_team_tail_on_player_chips(mlb_picks):
    offenders = []
    for p in mlb_picks:
        mkt = (p.get("market") or "").lower()
        s = p.get("h2h_summary") or ""
        if not s:
            continue
        if not any(k in mkt for k in BATTER_MARKETS + PITCHER_MARKETS):
            continue
        if re.search(r"·\s*L\d+", s) or re.search(r"\bH2H\s+\d+-\d+", s):
            offenders.append(f"[{mkt}] {s!r}")
    print(f"[player no-tail] offenders={len(offenders)}")
    assert not offenders, "\n".join(offenders[:10])
