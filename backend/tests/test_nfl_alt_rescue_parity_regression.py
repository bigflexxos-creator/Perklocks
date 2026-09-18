"""
Regression fixture — NFL ALT-LINE LITE/DETAIL PARITY (2026-09-18).

Observed defect: ``Jaxson Dart Over 29.5 Player Rush Yds`` showed
win_probability 75.1 on /picks/today?lite=true but 70.67 on
/picks/{id} and in the frozen ``published_probability`` snapshot.

Root cause: ``services.locks_eligibility.rescue_missing_eligible``
injected raw ``db.picks`` docs onto the lite board without running the
canonical reader (``published_prediction_reader.hydrate``), so the
MUTABLE top-level aliases (win_probability / edge_percent / grade /
lock_score) leaked while every other read path hydrated the snapshot.

Two layers of protection:
  1. Unit — a fixture doc reproducing the exact drift is rescued and
     MUST come back hydrated (published truth wins on every scored field).
  2. Live — every NFL alt row on the lite board MUST equal /picks/{id}
     on all frozen scored fields.
"""
import asyncio
import os

import pytest
import requests

from services.locks_eligibility import rescue_missing_eligible

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://canonical-parity.preview.emergentagent.com",
).rstrip("/")

SCORED_FIELDS = (
    "win_probability", "edge_percent", "lock_score", "grade",
    "line", "book_odds", "selection", "model_version", "publication_version",
)

# Exact drift observed in production (id 715e6aef-a566-53af-8f6f-81a54bb8418a).
JAXSON_DART_DRIFT_DOC = {
    "id": "715e6aef-a566-53af-8f6f-81a54bb8418a",
    "sport": "NFL",
    "market": "Jaxson Dart Over 29.5 Player Rush Yds",
    "event": "New York Giants @ Los Angeles Rams",
    "event_time": "2099-01-01T00:00:00Z",
    "status": "pending",
    "publication_state": "PUBLISHED",
    "is_alt": True,
    # mutable aliases (stale re-score) — MUST NOT be served
    "win_probability": 75.1,
    "edge_percent": 21.8,
    "lock_score": 88.3,
    "grade": "Lock",
    # frozen publication truth
    "published_probability": 0.7067,
    "published_edge": 17.37,
    "published_lock_score": 88.3,
    "published_grade": "Playable",
    "published_line": 29.5,
    "published_odds": -114,
    "published_side": "over",
    "book_odds": -114,
    "line": 29.5,
    "snapshot_version": 1,
}


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def limit(self, _n):
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return dict(next(self._it))
        except StopIteration:
            raise StopAsyncIteration


class _Picks:
    def __init__(self, docs):
        self._docs = docs

    def find(self, _query, projection=None):
        return _Cursor(self._docs)


class _DB:
    def __init__(self, docs):
        self.picks = _Picks(docs)


class TestRescueHydratesPublishedTruth:
    def test_rescued_row_reads_snapshot_not_mutable_alias(self):
        db = _DB([JAXSON_DART_DRIFT_DOC])
        rescued, ebm_ids, rejected = asyncio.run(
            rescue_missing_eligible(db, set(), {})
        )
        assert ebm_ids == [JAXSON_DART_DRIFT_DOC["id"]], rejected
        row = rescued[0]
        assert row["win_probability"] == 70.67, row["win_probability"]
        assert row["edge_percent"] == 17.37
        assert row["lock_score"] == 88.3
        assert row["grade"] == "Playable"
        assert row.get("_prediction_source") == "snapshot"


def _auth_headers():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"live API unavailable: {r.status_code}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class TestLiveNflAltParity:
    def test_every_nfl_alt_lite_row_matches_detail(self):
        h = _auth_headers()
        board = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true", headers=h, timeout=120,
        ).json()
        nfl_alt = [
            p for p in board.get("picks", [])
            if (p.get("sport") or "").upper() == "NFL"
            and ("Player" in (p.get("market") or "") or p.get("is_alt"))
        ]
        if not nfl_alt:
            pytest.skip("no NFL alt rows on today's board")
        diffs = []
        for row in nfl_alt:
            det = requests.get(
                f"{BASE_URL}/api/picks/{row['id']}", headers=h, timeout=60,
            ).json()
            for f in SCORED_FIELDS:
                lv, dv = row.get(f), det.get(f)
                if isinstance(lv, float):
                    lv = round(lv, 2)
                if isinstance(dv, float):
                    dv = round(dv, 2)
                if lv is not None and dv is not None and lv != dv:
                    diffs.append((row["id"], row.get("market"), f, lv, dv))
        assert not diffs, f"{len(diffs)} NFL alt lite/detail mismatches: {diffs[:5]}"
