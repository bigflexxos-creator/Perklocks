"""
Iteration 149 — Backend verification for two P0 repairs:
  1) CFB high-lock restoration (via GET /api/picks/today?sport=CFB)
  2) NFL Magic evidence role/matchup/model/market restoration (via services.magic)
  3) CFB canonical parity regression guard (direct DB scan)

Run:
    pytest /app/backend/tests/test_iter149_cfb_nfl_repairs.py -v \
        --tb=short --junitxml=/app/test_reports/pytest/iter149.xml
"""
import os
import sys
import json
import asyncio
from typing import List, Dict, Any
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")

# Give backend imports access
sys.path.insert(0, "/app/backend")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def auth_token():
    last_err = None
    for attempt in range(5):
        try:
            r = requests.post(
                f"{BASE_URL}/api/auth/login",
                json={"email": "demo@lockscore.ai", "password": "demo123"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()["access_token"]
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        import time
        time.sleep(2)
    pytest.fail(f"login failed after 5 retries: {last_err}")


@pytest.fixture(scope="session")
def cfb_picks_response(auth_token):
    last_err = None
    for attempt in range(5):
        try:
            r = requests.get(
                f"{BASE_URL}/api/picks/today",
                params={"sport": "CFB"},
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=60,
            )
            if r.status_code == 200:
                data = r.json()
                with open("/tmp/iter149_cfb_picks.json", "w") as f:
                    json.dump(data, f)
                return data
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        import time
        time.sleep(2)
    pytest.fail(f"CFB fetch failed after 5 retries: {last_err}")


@pytest.fixture(scope="session")
def cfb_picks(cfb_picks_response):
    return cfb_picks_response.get("picks", [])


# =============================================================================
# 1) CFB HIGH-LOCK RESTORATION
# =============================================================================
class TestCFBHighLockRestoration:
    def test_ge_40_picks_at_85(self, cfb_picks):
        hi = [p for p in cfb_picks if (p.get("lock_score") or 0) >= 85]
        assert len(hi) >= 40, (
            f"Expected >=40 CFB picks with lock_score >=85, got {len(hi)}. "
            f"Total picks fetched={len(cfb_picks)}"
        )

    def test_ge_30_picks_at_90(self, cfb_picks):
        hi = [p for p in cfb_picks if (p.get("lock_score") or 0) >= 90]
        assert len(hi) >= 30, (
            f"Expected >=30 with lock_score >=90, got {len(hi)}"
        )

    def test_ge_8_picks_at_98(self, cfb_picks):
        hi = [p for p in cfb_picks if (p.get("lock_score") or 0) >= 98]
        ids = [p.get("id") for p in hi]
        assert len(hi) >= 8, (
            f"Expected >=8 with lock_score >=98 (target 10 at 98.5), got {len(hi)}. "
            f"pick_ids at 98+: {ids}"
        )

    def test_all_three_market_families_ge_5_each(self, cfb_picks):
        # Restrict to on-board (off_board != True) for the market coverage check
        on_board = [p for p in cfb_picks if not p.get("off_board")]
        counts = {"Moneyline": 0, "Spread": 0, "Total": 0}
        raw_markets = {}
        for p in on_board:
            m = (p.get("market") or "").lower()
            raw_markets[m] = raw_markets.get(m, 0) + 1
            if "moneyline" in m or m == "h2h" or m == "ml":
                counts["Moneyline"] += 1
            elif "spread" in m or m == "ats":
                counts["Spread"] += 1
            elif "total" in m or m in ("over_under", "ou", "totals"):
                counts["Total"] += 1
        missing = {k: v for k, v in counts.items() if v < 5}
        assert not missing, (
            f"Market families not all >=5: {counts}. "
            f"Raw markets seen: {raw_markets}"
        )

    def test_canonical_parity_100pct(self, cfb_picks):
        drifted = []
        for p in cfb_picks:
            ls = p.get("lock_score")
            pls = p.get("published_lock_score")
            if ls is None or pls is None or ls != pls:
                drifted.append({
                    "id": p.get("id"),
                    "lock_score": ls,
                    "published_lock_score": pls,
                })
        assert not drifted, (
            f"Canonical parity FAIL — {len(drifted)} picks with drift. "
            f"First 10: {drifted[:10]}"
        )

    def test_no_lock_score_100_unless_apex(self, cfb_picks):
        offenders = [
            {"id": p.get("id"), "lock_score": p.get("lock_score"), "apex_lock": p.get("apex_lock")}
            for p in cfb_picks
            if (p.get("lock_score") or 0) == 100 and not p.get("apex_lock")
        ]
        assert not offenders, (
            f"Picks with lock_score==100 but apex_lock != True: {offenders}"
        )

    def test_chalk_trap_heavy_chalk_off_boarded(self, cfb_picks):
        # Any moneyline pick with odds <= -390 should carry off_board=True + chalk_trap=True
        offenders = []
        for p in cfb_picks:
            m = (p.get("market") or "").lower()
            if not ("moneyline" in m or m == "h2h" or m == "ml"):
                continue
            odds = p.get("american_odds") or p.get("odds") or p.get("book_odds")
            try:
                odds_val = int(odds) if odds is not None else None
            except (TypeError, ValueError):
                odds_val = None
            if odds_val is None or odds_val > -390:
                continue
            off_board = p.get("off_board")
            chalk_trap = p.get("chalk_trap")
            if not (off_board is True and chalk_trap is True):
                offenders.append({
                    "id": p.get("id"),
                    "odds": odds_val,
                    "off_board": off_board,
                    "chalk_trap": chalk_trap,
                })
        assert not offenders, (
            f"Heavy-chalk ML picks (odds <= -390) not correctly off_boarded/chalk_trapped: "
            f"{offenders}"
        )


# =============================================================================
# 2) NFL MAGIC EVIDENCE RESTORATION (role / matchup / model / market)
# =============================================================================
class TestNFLMagicEvidence:
    @pytest.fixture(scope="class")
    def evidence_results(self):
        """Fetch top-20 upcoming NFL picks from DB, run adapters+integrator."""
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            from services.magic.adapters import build_evidence
            from services.magic.lock_score_integrator import (
                categorize_evidence,
                collapse_history_form,
            )
        except Exception as e:
            pytest.skip(f"backend imports unavailable: {e}")

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "lockscore_db")

        async def _run():
            client = AsyncIOMotorClient(mongo_url)
            db = client[db_name]
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            query = {
                "sport": "NFL",
                "off_board": {"$ne": True},
                "event_time": {"$gte": now_iso},
            }
            cursor = db.picks.find(query).sort("lock_score", -1).limit(20)
            picks = await cursor.to_list(length=20)
            results = []
            for pick in picks:
                pick.pop("_id", None)
                try:
                    ev = await build_evidence(db, pick)
                except Exception as e:
                    results.append({
                        "id": pick.get("id"),
                        "error": f"build_evidence: {e}",
                        "categorized": None,
                        "collapsed": None,
                    })
                    continue
                try:
                    categorized = categorize_evidence(ev) if ev else None
                except Exception as e:
                    categorized = {"error": str(e)}
                try:
                    collapsed = collapse_history_form(categorized) if categorized else None
                except Exception:
                    collapsed = categorized
                results.append({
                    "id": pick.get("id"),
                    "lock_score": pick.get("lock_score"),
                    "market": pick.get("market"),
                    "pick": pick.get("pick"),
                    "evidence": ev,
                    "categorized": categorized,
                    "collapsed": collapsed,
                })
            client.close()
            return results

        try:
            data = asyncio.run(_run())
        except Exception as e:
            pytest.skip(f"async evidence run failed: {e}")

        # Persist for post-mortem
        try:
            with open("/tmp/iter149_nfl_magic_evidence.json", "w") as f:
                json.dump(data, f, default=str)
        except Exception:
            pass

        if not data:
            pytest.skip("No upcoming NFL picks (off_board!=True) in DB")

        return data

    @staticmethod
    def _cat_status(rec: Dict[str, Any], category: str) -> str:
        """Extract AVAILABLE / MISSING for a category from categorized evidence.

        `categorize_evidence` returns a dict of CategoryVote dataclasses with
        `.available` (bool). We prefer that; fall back to collapsed if used.
        """
        source = rec.get("categorized") or rec.get("collapsed") or {}
        if not isinstance(source, dict):
            return "MISSING"
        vote = source.get(category)
        if vote is None:
            # also try nested containers
            vote = (source.get("categories") or {}).get(category)
        if vote is None:
            return "MISSING"
        # CategoryVote dataclass path
        avail = getattr(vote, "available", None)
        if avail is not None:
            return "AVAILABLE" if bool(avail) else "MISSING"
        if isinstance(vote, dict):
            status = vote.get("status") or vote.get("availability")
            if status:
                return str(status).upper()
            if vote.get("available") is True:
                return "AVAILABLE"
        if isinstance(vote, str):
            return vote.upper()
        return "MISSING"

    def test_role_opportunity_available_ge_15(self, evidence_results):
        avail_ids = [
            r["id"] for r in evidence_results
            if self._cat_status(r, "role_opportunity") == "AVAILABLE"
        ]
        assert len(avail_ids) >= 15, (
            f"role_opportunity AVAILABLE = {len(avail_ids)}/20, expected >=15. "
            f"Failed pick_ids (not AVAILABLE): "
            f"{[r['id'] for r in evidence_results if r['id'] not in avail_ids]}"
        )

    def test_matchup_available_ge_8(self, evidence_results):
        avail_ids = [
            r["id"] for r in evidence_results
            if self._cat_status(r, "matchup") == "AVAILABLE"
        ]
        assert len(avail_ids) >= 8, (
            f"matchup AVAILABLE = {len(avail_ids)}/20, expected >=8. "
            f"Failed pick_ids: "
            f"{[r['id'] for r in evidence_results if r['id'] not in avail_ids]}"
        )

    def test_model_family_available_ge_18(self, evidence_results):
        avail_ids = [
            r["id"] for r in evidence_results
            if self._cat_status(r, "model_family") == "AVAILABLE"
        ]
        assert len(avail_ids) >= 18, (
            f"model_family AVAILABLE = {len(avail_ids)}/20, expected >=18. "
            f"Failed pick_ids: "
            f"{[r['id'] for r in evidence_results if r['id'] not in avail_ids]}"
        )

    def test_market_intel_available_20_of_20(self, evidence_results):
        avail_ids = [
            r["id"] for r in evidence_results
            if self._cat_status(r, "market_intel") == "AVAILABLE"
        ]
        n = len(evidence_results)
        assert len(avail_ids) == n, (
            f"market_intel AVAILABLE = {len(avail_ids)}/{n}, expected {n}/{n}. "
            f"Failed pick_ids: "
            f"{[r['id'] for r in evidence_results if r['id'] not in avail_ids]}"
        )

    def test_at_least_one_pick_5plus_positive_categories(self, evidence_results):
        target_cats = [
            "role_opportunity", "matchup", "model_family", "market_intel",
            "history_exact", "recent_form",
        ]
        winners = []
        for r in evidence_results:
            count = sum(1 for c in target_cats if self._cat_status(r, c) == "AVAILABLE")
            if count >= 5:
                winners.append({"id": r["id"], "positive": count})
        assert winners, (
            "No pick had >=5 positive Magic categories (Apex #6 unreachable). "
            "Per-pick positive counts: "
            f"{[{'id': r['id'], 'n': sum(1 for c in target_cats if self._cat_status(r, c) == 'AVAILABLE')} for r in evidence_results]}"
        )


# =============================================================================
# 3) CFB CANONICAL PARITY REGRESSION GUARD (direct DB scan)
# =============================================================================
class TestCFBCanonicalParityRegression:
    @pytest.fixture(scope="class")
    def db_cfb_picks(self):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except Exception as e:
            pytest.skip(f"motor unavailable: {e}")

        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "lockscore_db")

        async def _run():
            client = AsyncIOMotorClient(mongo_url)
            db = client[db_name]
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            cur = db.picks.find({
                "sport": "CFB",
                "off_board": {"$ne": True},
                "event_time": {"$gte": now_iso},
            })
            docs = await cur.to_list(length=5000)
            client.close()
            return docs

        docs = asyncio.run(_run())
        if not docs:
            pytest.skip("No upcoming CFB picks (off_board != True) in DB")
        return docs

    def test_lock_score_eq_published_lock_score(self, db_cfb_picks):
        drifted = []
        for p in db_cfb_picks:
            ls = p.get("lock_score")
            pls = p.get("published_lock_score")
            if ls is None or pls is None or ls != pls:
                drifted.append({
                    "id": p.get("id"),
                    "lock_score": ls,
                    "published_lock_score": pls,
                })
        assert not drifted, (
            f"CFB parity drift on {len(drifted)}/{len(db_cfb_picks)} picks. "
            f"First 10 offenders: {drifted[:10]}"
        )

    def test_cfb_engine_version_prefix(self, db_cfb_picks):
        offenders = [
            {"id": p.get("id"), "cfb_engine_version": p.get("cfb_engine_version")}
            for p in db_cfb_picks
            if not (p.get("cfb_engine_version") or "").startswith("cfb_sp_game.v3")
        ]
        assert not offenders, (
            f"{len(offenders)} CFB picks with missing/wrong cfb_engine_version "
            f"(expected prefix 'cfb_sp_game.v3'). First 10: {offenders[:10]}"
        )

    def test_factor_sources_contract(self, db_cfb_picks):
        offenders_ratings = []
        offenders_odds = []
        for p in db_cfb_picks:
            fs = p.get("factor_sources") or []
            if not isinstance(fs, list):
                fs = list(fs) if fs else []
            if "cfb_sp_ratings" not in fs:
                offenders_ratings.append({"id": p.get("id"), "factor_sources": fs})
            has_book = p.get("book_odds") is not None
            if has_book and "the_odds_api" not in fs:
                offenders_odds.append({
                    "id": p.get("id"),
                    "book_odds": p.get("book_odds"),
                    "factor_sources": fs,
                })
        assert not offenders_ratings, (
            f"{len(offenders_ratings)} CFB picks missing 'cfb_sp_ratings' in factor_sources. "
            f"First 10: {offenders_ratings[:10]}"
        )
        assert not offenders_odds, (
            f"{len(offenders_odds)} CFB picks with book_odds but missing 'the_odds_api' "
            f"in factor_sources. First 10: {offenders_odds[:10]}"
        )
