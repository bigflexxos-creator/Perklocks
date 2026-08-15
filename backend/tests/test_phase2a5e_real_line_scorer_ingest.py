"""Phase 2A.5E regression — Real MLS/Soccer scorer ingest wiring.

Covers the invariants that were broken before Phase 2A.5E and must
never regress:

* Cached `live_alt_lines` scorer rows land as picks with **real** book
  odds preserved (no synthetic `model_only` prices).
* Every ingested pick carries a deterministic UUID5 `id` so it survives
  restarts and dedupes idempotently on re-ingest.
* Missing form data → `off_board=True` + `MISSING_FEATURE_DATA`
  (candidate is retained for attribution — NOT silently dropped).
* Lowercase (`"soccer"`) and Title-case (`"Soccer"`) sport labels are
  both accepted by the reader (alt_lines_feed writes lowercase; older
  writers used Title-case).
* Ingested picks are marked out-of-band so the main refresh cycle's
  atomic delete never wipes them.
"""
from __future__ import annotations

import os, sys, asyncio, uuid, inspect, importlib
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ─────────────────────────────────────────────────────────────────────
# Minimal Fake Mongo double
# ─────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._rows):
            raise StopAsyncIteration
        r = self._rows[self._i]
        self._i += 1
        return r


class _FakeCollection:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.upserts = []
    def find(self, q=None):
        def _match(row):
            if not q:
                return True
            for k, v in q.items():
                if isinstance(v, dict) and "$in" in v:
                    if row.get(k) not in v["$in"]:
                        return False
                elif row.get(k) != v:
                    return False
            return True
        return _FakeCursor([r for r in self._rows if _match(r)])
    async def find_one(self, q=None):
        return None
    async def update_one(self, filt, update, upsert=False):
        self.upserts.append((filt, update))
        return type("R", (), {"upserted_id": None, "matched_count": 0})


class _FakeDB:
    def __init__(self, alt_rows):
        self.live_alt_lines = _FakeCollection(alt_rows)
        self.soccer_player_form = _FakeCollection([])
        self.soccer_player_game_logs = _FakeCollection([])
        self.picks = _FakeCollection([])


def _mk_row(**kw):
    base = dict(
        sport="soccer",
        odds_api_sport="soccer_usa_mls",
        event_id="evt_test_1",
        event_name="Inter Miami @ LA Galaxy",
        home_team="LA Galaxy",
        away_team="Inter Miami",
        commence_time="2026-08-15T23:00:00Z",
        sportsbook="draftkings",
        market_key="player_goal_scorer_anytime",
        selection="Lionel Messi",
        selection_norm="lionel messi",
        line=None,
        price=-110,
    )
    base.update(kw)
    return base


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# Invariant #1 — book odds preserved, deterministic id, off_board attribution
# ─────────────────────────────────────────────────────────────────────
def test_real_line_ingest_preserves_book_odds_and_writes_pick():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    db = _FakeDB([_mk_row()])
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["scanned"] == 1
    assert stats["written"] == 1
    assert len(db.picks.upserts) == 1
    _, upd = db.picks.upserts[0]
    doc = upd["$set"]
    assert doc["source"] == "real_line_alt_scorer_v1"
    assert doc["book_odds"] == -110
    assert doc["bookmaker"] == "draftkings"
    assert doc["odds_source"] == "real_book_line"
    assert doc["no_real_book_line"] is False
    # Missing form data → off_board attribution, not silent drop.
    assert doc["off_board"] is True
    assert "MISSING_FEATURE_DATA" in (doc.get("off_board_reasons") or [])
    # Deterministic pick id.
    uuid.UUID(doc["id"])  # raises if not a valid UUID
    assert doc["external_id"].startswith("real_line_alt_scorer_v1|")


# ─────────────────────────────────────────────────────────────────────
# Invariant #2 — sport-label tolerance (lowercase + title-case)
# ─────────────────────────────────────────────────────────────────────
def test_real_line_ingest_accepts_lowercase_and_title_case_sport():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    db = _FakeDB([
        _mk_row(sport="soccer", event_id="evt_a"),
        _mk_row(sport="Soccer", event_id="evt_b", selection="Evander"),
    ])
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["scanned"] == 2
    assert stats["written"] == 2


# ─────────────────────────────────────────────────────────────────────
# Invariant #3 — idempotent, deterministic pick id
# ─────────────────────────────────────────────────────────────────────
def test_real_line_ingest_is_idempotent_deterministic_id():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    row = _mk_row()
    db1 = _FakeDB([row])
    db2 = _FakeDB([row])
    _run(ingest_real_line_soccer_scorers(db1, today="2026-08-15"))
    _run(ingest_real_line_soccer_scorers(db2, today="2026-08-15"))
    id1 = db1.picks.upserts[0][1]["$set"]["id"]
    id2 = db2.picks.upserts[0][1]["$set"]["id"]
    assert id1 == id2, "Pick id must be deterministic (uuid5) across runs"


# ─────────────────────────────────────────────────────────────────────
# Invariant #4 — zero-priced rows skipped
# ─────────────────────────────────────────────────────────────────────
def test_real_line_ingest_skips_zero_price():
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers,
    )
    db = _FakeDB([_mk_row(price=0)])
    stats = _run(ingest_real_line_soccer_scorers(db, today="2026-08-15"))
    assert stats["scanned"] == 1
    assert stats["written"] == 0
    assert stats["skipped"] == 1


# ─────────────────────────────────────────────────────────────────────
# Invariant #5 — orchestrator treats real_line_alt_scorer_v1 as out-of-band
# ─────────────────────────────────────────────────────────────────────
def test_real_line_source_is_out_of_band_in_orchestrator():
    mod = importlib.import_module("services.pick_refresh_orchestrator")
    src = inspect.getsource(mod)
    assert "real_line_alt_scorer_v1" in src, (
        "real_line_alt_scorer_v1 must be listed in _OUT_OF_BAND_SOURCES"
    )


# ─────────────────────────────────────────────────────────────────────
# Invariant #6 — server.on_startup wires the healer + recurring task
# ─────────────────────────────────────────────────────────────────────
def test_real_line_scorer_ingest_wired_into_server_startup():
    import server
    src = inspect.getsource(server.on_startup)
    assert "ingest_real_line_soccer_scorers" in src
    assert "phase_2a5e_real_line_scorer_ingest" in src, (
        "Recurring ingest task must be registered so newly-cached "
        "alt lines land on the board between full refresh cycles"
    )
