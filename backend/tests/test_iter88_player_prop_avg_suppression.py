"""iter-88 — Player-prop AVG suppression + is_player_prop flag verification.

Rules being tested (per E1's iter-88 fix in services/h2h_enricher.py):
- On MLB player-prop picks (Hits, HRs, Strikeouts, RBIs, Total Bases,
  Runs Scored…), h2h_summary MUST NOT contain the substring 'avg'.
- On MLB team bets (Spread, Moneyline, Over/Under total), h2h_summary
  SHOULD contain 'avg runs' when the summary is non-empty.
- Same rule for Soccer: 'avg goals' present on team spreads/totals,
  absent on goalscorer/assist props.
- /api/picks/{id}/h2h returns `is_player_prop: bool` matching the market.
- Regression: iter-82 line-specific reconciler still holds, no 5xx.
"""
from __future__ import annotations

import os
import re
import pytest
import requests

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
            or "https://player-intel-engine.preview.emergentagent.com").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

# Keywords that indicate a player-prop market (mirrors backend classifier).
PLAYER_PROP_KEYWORDS = (
    "hits", "home run", "homer", "total bases", "rbi", "runs scored",
    "strikeout", "strikeouts", "outs recorded", "pitching outs",
    "walks", "earned runs", "hits allowed",
    "anytime goal scorer", "anytime scorer", "first goal scorer",
    "last goal scorer", "anytime assist", "to score or assist",
    "aces", "double faults",
    "points", "rebounds", "assists",
    "passing yards", "rushing yards", "receiving yards", "receptions",
    "anytime touchdown", "first touchdown",
)

TEAM_MARKET_HINTS = ("spread", "moneyline", "over/under", "total", "runline", "puck line")


def is_player_prop(market: str) -> bool:
    ml = (market or "").lower()
    return any(kw in ml for kw in PLAYER_PROP_KEYWORDS)


def is_team_market(market: str) -> bool:
    ml = (market or "").lower()
    if is_player_prop(market):
        return False
    return any(kw in ml for kw in TEAM_MARKET_HINTS)


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=60)
    assert r.status_code == 200, r.text[:200]
    tok = r.json().get("token") or r.json().get("access_token")
    s.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    return s


# ────────────────────────────────────────────────────────────────────
# MLB — player-prop chip must not include 'avg'
# Team markets — chip should include 'avg runs' when non-empty
# ────────────────────────────────────────────────────────────────────
class TestMLBAvgSuppression:
    def test_mlb_player_props_no_avg_in_summary(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        assert r.status_code == 200
        picks = (r.json() or {}).get("picks") or []
        assert picks, "no MLB picks in slate"
        checked = 0
        violations: list[dict] = []
        for p in picks:
            mk = p.get("market") or ""
            summary = (p.get("h2h_summary") or "").strip()
            if not summary or not is_player_prop(mk):
                continue
            checked += 1
            # After the fix, no 'avg' substring should appear in the chip
            if re.search(r"\bavg\b", summary.lower()):
                violations.append({"id": p.get("id"), "market": mk,
                                   "selection": p.get("selection"),
                                   "summary": summary})
        print(f"MLB player-prop chips checked={checked} violations={len(violations)}")
        if violations:
            for v in violations[:5]:
                print("VIOLATION:", v)
        assert not violations, \
            f"{len(violations)} player-prop pick(s) still contain 'avg' in summary"

    def test_mlb_team_markets_include_avg_runs(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        picks = (r.json() or {}).get("picks") or []
        checked = 0
        with_avg = 0
        without_avg: list[dict] = []
        for p in picks:
            mk = p.get("market") or ""
            summary = (p.get("h2h_summary") or "").strip()
            if not summary or not is_team_market(mk):
                continue
            checked += 1
            if "avg runs" in summary.lower():
                with_avg += 1
            else:
                without_avg.append({"id": p.get("id"), "market": mk,
                                    "summary": summary})
        print(f"MLB team-market chips checked={checked} with_avg={with_avg}")
        if checked == 0:
            pytest.skip("no MLB team-market picks with summary in slate to verify")
        # Not every summary MUST have avg (some may be player_h2h-only), but
        # if none contain avg runs we know the unit label failed. Assert at
        # least one team-market summary carries 'avg runs'.
        assert with_avg > 0, (
            f"no MLB team-market pick emitted 'avg runs' in summary; "
            f"sample without_avg: {without_avg[:3]}"
        )


# ────────────────────────────────────────────────────────────────────
# Soccer — 'avg goals' on team totals/spreads, absent on scorer/assist
# ────────────────────────────────────────────────────────────────────
class TestSoccerAvgSuppression:
    def test_soccer_player_props_no_avg_in_summary(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Soccer"}, timeout=30)
        assert r.status_code == 200
        picks = (r.json() or {}).get("picks") or []
        if not picks:
            pytest.skip("no Soccer picks in slate")
        checked = 0
        violations: list[dict] = []
        for p in picks:
            mk = p.get("market") or ""
            summary = (p.get("h2h_summary") or "").strip()
            if not summary or not is_player_prop(mk):
                continue
            checked += 1
            if re.search(r"\bavg\b", summary.lower()):
                violations.append({"id": p.get("id"), "market": mk,
                                   "summary": summary})
        print(f"Soccer player-prop chips checked={checked} violations={len(violations)}")
        if checked == 0:
            pytest.skip("no Soccer player-prop chips carrying summary in slate")
        assert not violations, \
            f"{len(violations)} soccer player-prop pick(s) still contain 'avg' in summary"


# ────────────────────────────────────────────────────────────────────
# Deep-dive /api/picks/{id}/h2h — `is_player_prop` flag
# ────────────────────────────────────────────────────────────────────
class TestDeepDiveIsPlayerPropFlag:
    def test_hits_pick_marked_is_player_prop_true(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        picks = (r.json() or {}).get("picks") or []
        hits = [p for p in picks if "hits" in (p.get("market") or "").lower()
                and "hits allowed" not in (p.get("market") or "").lower()]
        if not hits:
            # Fallback to any player-prop
            hits = [p for p in picks if is_player_prop(p.get("market") or "")]
        assert hits, "no batter-Hits or player-prop pick in slate"
        p = hits[0]
        r2 = api.get(f"{BASE_URL}/api/picks/{p['id']}/h2h", timeout=30)
        assert r2.status_code == 200
        body = r2.json() or {}
        print(f"HITS DEEP-DIVE market={p.get('market')} is_player_prop={body.get('is_player_prop')} "
              f"summary='{body.get('summary')}' team_h2h={bool(body.get('team_h2h'))}")
        assert body.get("is_player_prop") is True, \
            f"expected is_player_prop=true for '{p.get('market')}', got {body.get('is_player_prop')}"
        # Also verify summary does NOT contain 'avg'
        summary = (body.get("summary") or "").strip()
        if summary:
            assert not re.search(r"\bavg\b", summary.lower()), \
                f"deep-dive summary for player-prop should not contain 'avg': {summary!r}"

    def test_team_spread_pick_marked_is_player_prop_false(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        picks = (r.json() or {}).get("picks") or []
        spreads = [p for p in picks
                   if is_team_market(p.get("market") or "")]
        if not spreads:
            pytest.skip("no team-market MLB pick in slate")
        # Choose one that has a summary if possible (means team_h2h exists)
        with_summary = [p for p in spreads if (p.get("h2h_summary") or "").strip()]
        p = with_summary[0] if with_summary else spreads[0]
        r2 = api.get(f"{BASE_URL}/api/picks/{p['id']}/h2h", timeout=30)
        assert r2.status_code == 200
        body = r2.json() or {}
        print(f"SPREAD DEEP-DIVE market={p.get('market')} is_player_prop={body.get('is_player_prop')} "
              f"summary='{body.get('summary')}'")
        assert body.get("is_player_prop") is False, \
            f"expected is_player_prop=false for team market '{p.get('market')}'"


# ────────────────────────────────────────────────────────────────────
# Regression — endpoints don't 5xx, iter-82 same-line reconciler
# ────────────────────────────────────────────────────────────────────
class TestNo5xx:
    def test_picks_today_ok(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", timeout=30)
        assert r.status_code == 200

    def test_invalid_pick_h2h_returns_4xx_not_5xx(self, api):
        r = api.get(f"{BASE_URL}/api/picks/nonexistent-pick-id/h2h", timeout=20)
        assert r.status_code < 500, f"invalid pick id returned 5xx: {r.status_code}"


class TestSameLineOURegression:
    def test_no_same_line_ou_contradictions_mlb(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
        picks = (r.json() or {}).get("picks") or []
        seen: dict = {}
        contradictions = []
        for p in picks:
            market = (p.get("market") or "").lower().strip()
            line = p.get("line") if p.get("line") is not None else p.get("line_value")
            player = (p.get("player_name") or p.get("player") or "").lower().strip()
            game = (p.get("game") or p.get("event") or "").lower().strip()
            sel = (p.get("selection") or "").lower()
            if line is None:
                continue
            key = (game, player, market, str(line))
            side = "over" if "over" in sel else ("under" if "under" in sel else None)
            if side is None:
                continue
            if key in seen and seen[key] != side:
                contradictions.append({"key": key, "sides": [seen[key], side]})
            seen[key] = side
        print(f"MLB same-line O/U keys={len(seen)} contradictions={len(contradictions)}")
        assert not contradictions, f"contradictions found: {contradictions[:5]}"
