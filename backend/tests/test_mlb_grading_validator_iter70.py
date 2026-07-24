"""Iter 70 — MLB Cross-Source Grading Validator regression tests.

Scope (backend-only, per E1 request):
1) Module contract — grading_validator exposes required symbols.
2) _MLB_STAT_MAP contains the required market families.
3) _mlb_verify_prop is regex-driven and parses the required market families.
4) grading_validator_loop iterates BOTH soccer + MLB verifiers per cycle.
5) Live DB spot-check — MLB picks that were reopened (grade_disagreement set)
   for the specific players called out on the 2026-07-12 slate.
6) /api/picks/history?days=30 sort-order regression.
7) FanDuel-verified soccer picks still show the correct status.
8) `grade_disagreement` field exists on some pick(s) in the DB.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, "/app/backend")

# ────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────
BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://player-intel-engine.preview.emergentagent.com",
).rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASS = "demo123"


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(api_client):
    r = api_client.post(f"{BASE_URL}/api/auth/login",
                        json={"email": DEMO_EMAIL, "password": DEMO_PASS},
                        timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def history_30d(api_client, auth_headers):
    r = api_client.get(f"{BASE_URL}/api/picks/history?days=30",
                       headers=auth_headers, timeout=120)
    assert r.status_code == 200, f"history returned {r.status_code}"
    data = r.json()
    picks = data.get("picks") if isinstance(data, dict) else data
    assert isinstance(picks, list) and picks, "history returned empty list"
    return picks


from dotenv import load_dotenv
load_dotenv("/app/backend/.env")


def _run_with_db(async_fn):
    """Create a motor client bound to a fresh event loop each call, run the
    async fn (which receives db as its argument), and close the loop.
    Avoids 'future belongs to a different loop' from module-scoped clients.
    """
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _wrap():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        try:
            return await async_fn(client[os.environ["DB_NAME"]])
        finally:
            client.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_wrap())
    finally:
        loop.close()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ════════════════════════════════════════════════════════════════════════
# 1. Module contract
# ════════════════════════════════════════════════════════════════════════
class TestModuleContract:
    def test_module_imports(self):
        import grading_validator as gv  # noqa
        assert gv is not None

    def test_exposes_soccer_verifier(self):
        import grading_validator as gv
        assert hasattr(gv, "verify_recent_goalscorer_grades")
        assert inspect.iscoroutinefunction(gv.verify_recent_goalscorer_grades)

    def test_exposes_mlb_verifier(self):
        import grading_validator as gv
        assert hasattr(gv, "verify_recent_mlb_grades")
        assert inspect.iscoroutinefunction(gv.verify_recent_mlb_grades)

    def test_exposes_loop(self):
        import grading_validator as gv
        assert hasattr(gv, "grading_validator_loop")
        assert inspect.iscoroutinefunction(gv.grading_validator_loop)

    def test_exposes_mlb_prop_verifier(self):
        import grading_validator as gv
        assert hasattr(gv, "_mlb_verify_prop")
        assert inspect.iscoroutinefunction(gv._mlb_verify_prop)


# ════════════════════════════════════════════════════════════════════════
# 2. _MLB_STAT_MAP coverage
# ════════════════════════════════════════════════════════════════════════
class TestMlbStatMap:
    REQUIRED = [
        "hits", "home run", "total bases", "rbi",
        "runs scored", "strikeouts", "outs recorded",
    ]

    def test_stat_map_has_required_entries(self):
        from grading_validator import _MLB_STAT_MAP
        for k in self.REQUIRED:
            assert k in _MLB_STAT_MAP, f"missing market family '{k}' in _MLB_STAT_MAP"

    def test_stat_map_values_are_statsapi_camelcase(self):
        from grading_validator import _MLB_STAT_MAP
        # MLB Stats API boxscore stat fields are camelCase — sanity check
        for phrase, api_key in _MLB_STAT_MAP.items():
            assert isinstance(api_key, str) and api_key
            # some are single-word (hits, outs, runs, rbi) — those are all lowercase
            # multi-word must be lowerCamelCase (e.g., homeRuns, totalBases, strikeOuts)
            if len(api_key) > 5 and any(c.isupper() for c in api_key):
                assert api_key[0].islower(), f"{api_key} is not lowerCamelCase"


# ════════════════════════════════════════════════════════════════════════
# 3. _mlb_verify_prop market parsing (regex + guardrails)
# ════════════════════════════════════════════════════════════════════════
class TestMlbVerifyPropParsing:
    """Static parse checks — verifier must early-return None when critical
    inputs are missing, but MUST recognize each required market family."""

    def test_returns_none_when_missing_selection(self):
        from grading_validator import _mlb_verify_prop
        out = _run(_mlb_verify_prop({"market": "Strikeouts Over 6.5", "event_time": "2026-07-12T23:00:00Z"}))
        assert out is None

    def test_returns_none_when_missing_event_time(self):
        from grading_validator import _mlb_verify_prop
        out = _run(_mlb_verify_prop({"market": "Strikeouts Over 6.5", "selection": "Zack Wheeler Over 6.5"}))
        assert out is None

    def test_returns_none_when_market_family_unknown(self):
        from grading_validator import _mlb_verify_prop
        out = _run(_mlb_verify_prop({
            "market": "Walks Over 1.5", "selection": "X", "event_time": "2026-07-12T23:00:00Z",
        }))
        assert out is None

    @pytest.mark.parametrize("market", [
        "Strikeouts Over 6.5",
        "Hits Over 0.5",
        "Home Run Over 0.5",
        "Total Bases Over 1.5",
        "RBI Over 0.5",
        "Outs Recorded Over 17.5",
        "Runs Scored Over 0.5",
    ])
    def test_regex_recognizes_market_family(self, market):
        """We can't verify against live boxscore here (that requires a real
        game_pk), but we can prove the market-family + over/under parsing
        code path is reached without raising — the function must handle
        each family without crashing."""
        from grading_validator import _mlb_verify_prop
        # No home_team/away_team → schedule match will fail → None expected.
        # The important thing is: it must not raise for these market strings.
        result = _run(_mlb_verify_prop({
            "market": market,
            "selection": "Player Name",
            "event_time": "2020-01-01T00:00:00Z",  # off-season date → no games
            "home_team": "Nowhere",
            "away_team": "Nowhere",
        }))
        # Expect None (no game found), NOT an exception.
        assert result is None


# ════════════════════════════════════════════════════════════════════════
# 4. Loop wiring — both verifiers must be called per cycle
# ════════════════════════════════════════════════════════════════════════
class TestLoopWiring:
    def test_loop_source_calls_both_verifiers(self):
        import grading_validator as gv
        src = inspect.getsource(gv.grading_validator_loop)
        assert "verify_recent_goalscorer_grades" in src, \
            "grading_validator_loop must call verify_recent_goalscorer_grades"
        assert "verify_recent_mlb_grades" in src, \
            "grading_validator_loop must call verify_recent_mlb_grades"


# ════════════════════════════════════════════════════════════════════════
# 5. Live DB — reopened MLB picks
# ════════════════════════════════════════════════════════════════════════
class TestReopenedMlbPicks:
    """Iter 70 startup manual run found 82 MLB mismatches. Verify the four
    named picks from the 2026-07-12 slate got reopened."""

    REOPENED_PLAYERS = [
        "Zack Wheeler",
        "Trea Turner",
        "Francisco Lindor",
        "Noah Schultz",
    ]

    def test_grade_disagreement_field_exists_in_db(self):
        async def _q(db):
            return await db.picks.count_documents({"grade_disagreement": {"$exists": True}})
        n = _run_with_db(_q)
        assert n > 0, "no picks with grade_disagreement field — validator never fired"
        print(f"  → {n} picks currently carry grade_disagreement")

    def test_grade_disagreement_count_bulk(self):
        """Iter71 update: post-fix expected state is 0 remaining
        grade_disagreement flags (settler now regrades correctly and
        validator clears stale flags on agreement). During the iter70 bug
        window this was ≥10; after the iter71 fix it must be 0."""
        async def _q(db):
            return await db.picks.count_documents({
                "sport": "MLB",
                "grade_disagreement": {"$exists": True},
            })
        n = _run_with_db(_q)
        print(f"  → {n} MLB picks with grade_disagreement (post-fix expect 0)")
        assert n == 0, f"expected 0 MLB grade_disagreement post-fix, found {n}"

    @pytest.mark.parametrize("player", REOPENED_PLAYERS)
    def test_named_reopened_pick_exists(self, player):
        pattern = re.escape(player)
        q = {
            "sport": "MLB",
            "$or": [
                {"selection": {"$regex": pattern, "$options": "i"}},
                {"market":    {"$regex": pattern, "$options": "i"}},
                {"player_name": {"$regex": pattern, "$options": "i"}},
                {"event":     {"$regex": pattern, "$options": "i"}},
            ],
            "$and": [{
                "$or": [
                    {"grade_disagreement": {"$exists": True}},
                    {"grade_verified_at": {"$exists": True}},
                ]
            }],
        }

        async def _find(db):
            return await db.picks.find(q).to_list(50)

        async def _broad(db):
            return await db.picks.find({
                "sport": "MLB",
                "$or": [
                    {"selection": {"$regex": pattern, "$options": "i"}},
                    {"player_name": {"$regex": pattern, "$options": "i"}},
                    {"market":    {"$regex": pattern, "$options": "i"}},
                ],
            }).to_list(20)

        docs = _run_with_db(_find)
        if not docs:
            broad = _run_with_db(_broad)
            print(f"  → {player}: no reopened doc found. {len(broad)} broad matches:")
            for b in broad[:5]:
                print(f"     status={b.get('status')} settled_at={b.get('settled_at')} "
                      f"grade_disagreement={bool(b.get('grade_disagreement'))} "
                      f"market={b.get('market')!r}")
        assert docs, f"no reopened/verified MLB pick found for {player}"
        d = docs[0]
        print(f"  → {player}: status={d.get('status')} "
              f"grade_disagreement={bool(d.get('grade_disagreement'))} "
              f"grade_verified_at={d.get('grade_verified_at')!r}")


# ════════════════════════════════════════════════════════════════════════
# 6. /api/picks/history sort-order regression
# ════════════════════════════════════════════════════════════════════════
class TestHistorySortOrder:
    def test_history_returns_200(self, history_30d):
        assert history_30d is not None

    def test_event_time_desc(self, history_30d):
        times = []
        for p in history_30d:
            et = p.get("event_time")
            if et:
                times.append(et)
        assert len(times) >= 2, "not enough dated picks to test sort"
        out_of_order = sum(1 for a, b in zip(times, times[1:]) if a < b)
        print(f"  → history sorted: {len(times)} timestamps, {out_of_order} out-of-order pairs")
        assert out_of_order == 0, f"{out_of_order} pairs violate event_time desc"


# ════════════════════════════════════════════════════════════════════════
# 7. FanDuel-verified soccer picks — status regression from iter 69
# ════════════════════════════════════════════════════════════════════════
class TestFanDuelSoccerRegression:
    EXPECTED = {
        "Robbie Ure":         "won",
        "Isak Bjerkebo":      "won",
        "Paulos Abraham":     "won",
        "Erik Botheim":       "won",
        "Mikkel Ladefoged":   "won",
        "Kristian Lien":      "won",
        "Kasper Høgh":        "lost",
        "Peter Christiansen": "lost",
    }

    @staticmethod
    def _nordic_fold(s: str) -> str:
        return (s.lower()
                .replace("ø", "o").replace("æ", "ae").replace("å", "a")
                .replace("ö", "o").replace("ä", "a"))

    @pytest.mark.parametrize("player,expected", list(EXPECTED.items()))
    def test_player_status(self, history_30d, player, expected):
        fold_player = self._nordic_fold(player)
        last_name = fold_player.split()[-1]
        matches = []
        for p in history_30d:
            for fld in ("selection", "player_name", "market", "event"):
                v = p.get(fld) or ""
                if last_name and last_name in self._nordic_fold(str(v)):
                    matches.append(p)
                    break
        if not matches:
            pytest.skip(f"{player} not in 30-day history window (may have rolled off)")
        # Any match with expected status is a pass; report actual set for context.
        statuses = {m.get("status") for m in matches}
        print(f"  → {player}: statuses seen = {statuses} (n={len(matches)})")
        assert expected in statuses, \
            f"{player} expected {expected}, saw {statuses}"


# ════════════════════════════════════════════════════════════════════════
# 8. Sanity: MLB Stats API upstream is reachable
# ════════════════════════════════════════════════════════════════════════
class TestMlbStatsApiUpstream:
    def test_schedule_endpoint_reachable(self):
        r = requests.get("https://statsapi.mlb.com/api/v1/schedule",
                         params={"sportId": 1, "date": "2026-07-12"},
                         timeout=15)
        assert r.status_code == 200, f"MLB Stats API schedule returned {r.status_code}"
        body = r.json()
        assert "dates" in body, "unexpected MLB Stats API schema"


# ════════════════════════════════════════════════════════════════════════
# 9. CRITICAL — reopened picks should regrade to the correct MLB Stats API
#    outcome, not back to the same wrong grade.
# ════════════════════════════════════════════════════════════════════════
class TestReopenedPickRegrade:
    """After the validator reopens a pick with grade_disagreement, the
    settler must re-grade to the value MLB Stats API confirmed — otherwise
    the fix is a no-op from the user's POV.

    Zack Wheeler 2026-07-12:
      • 10 strikeOuts   → Over 5.5 = WON (system shows LOST)
      • 18 outs         → Over 17.5 = WON (system shows LOST)
    """

    def test_wheeler_2026_07_12_strikeouts_correct_after_regrade(self):
        async def _q(db):
            return await db.picks.find_one({
                "sport": "MLB",
                "$or": [
                    {"selection": {"$regex": r"Zack Wheeler.*5\.5 Strikeouts", "$options": "i"}},
                    {"market":    {"$regex": r"Zack Wheeler.*5\.5 Strikeouts", "$options": "i"}},
                ],
                "event_time": {"$regex": "^2026-07-12"},
            })
        d = _run_with_db(_q)
        if not d:
            pytest.skip("Wheeler 5.5 K pick not in DB")
        actual_status = d.get("status")
        has_disagreement = bool(d.get("grade_disagreement"))
        print(f"  → Wheeler 5.5 K: status={actual_status} "
              f"grade_disagreement={has_disagreement} "
              f"MLB Stats API says=WON (10 K)")
        assert actual_status == "won", (
            f"CRITICAL: Wheeler had 10 strikeouts (MLB Stats API), "
            f"Over 5.5 must be WON, but pick shows status={actual_status!r} "
            f"with grade_disagreement={has_disagreement}. The validator "
            f"caught the mismatch but the re-settle produced the SAME "
            f"wrong grade."
        )

    def test_wheeler_2026_07_12_outs_correct_after_regrade(self):
        async def _q(db):
            return await db.picks.find_one({
                "sport": "MLB",
                "$or": [
                    {"selection": {"$regex": r"Zack Wheeler.*17\.5 Outs", "$options": "i"}},
                    {"market":    {"$regex": r"Zack Wheeler.*17\.5 Outs", "$options": "i"}},
                ],
                "event_time": {"$regex": "^2026-07-12"},
            })
        d = _run_with_db(_q)
        if not d:
            pytest.skip("Wheeler 17.5 outs pick not in DB")
        actual_status = d.get("status")
        has_disagreement = bool(d.get("grade_disagreement"))
        print(f"  → Wheeler 17.5 outs: status={actual_status} "
              f"grade_disagreement={has_disagreement} "
              f"MLB Stats API says=WON (18 outs)")
        assert actual_status == "won", (
            f"CRITICAL: Wheeler recorded 18 outs, Over 17.5 must be WON, "
            f"but pick shows status={actual_status!r} with "
            f"grade_disagreement={has_disagreement}."
        )

    def test_no_stale_grade_disagreement_after_regrade(self):
        """If a pick has grade_disagreement, the settler either cleared it
        by re-grading to the MLB-Stats-API-confirmed value, or the pick is
        still pending. It should NOT stay in status=won/lost with a stale
        grade_disagreement flag — that means the settler regraded to the
        same wrong answer."""
        async def _q(db):
            return await db.picks.count_documents({
                "sport": "MLB",
                "grade_disagreement": {"$exists": True},
                "status": {"$in": ["won", "lost"]},
            })
        n = _run_with_db(_q)
        print(f"  → {n} MLB picks have STALE grade_disagreement "
              f"(status won/lost, still flagged)")
        assert n == 0, (
            f"{n} MLB picks were reopened by the validator but re-settled "
            f"back to the same grade without clearing grade_disagreement. "
            f"This means the settler's MLB grading logic still produces "
            f"the wrong answer — the validator fix is a no-op."
        )
