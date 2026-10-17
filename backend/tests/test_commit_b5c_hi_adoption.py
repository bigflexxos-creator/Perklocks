"""Commit B/5c — HI direct-H2H + Trevor Lawrence canary + ESPN
canonical enrichment adoption tests.

Three real adoption contracts:

  1. ``query_historical`` universally augments VS OPP samples with
     the shared ``direct_h2h.direct_player_vs_opp`` /
     ``direct_team_vs_opp`` helpers — not just CFB's bespoke path.
  2. The Trevor Lawrence HI canary (NFL player prop, VS DEN) flows
     through the HI consumer and surfaces Model-visible == HI-visible
     parity: every observation the model sees via
     ``NFLPlayerHistoricalAdapter.fetch_observations`` is present in
     the deduped ``all_obs`` set delivered by ``query_historical``.
  3. ``services.historical_intelligence_projectors`` adopts
     ``services.espn_canonical_enrichment`` as a real complementary
     projector pass (not merely re-exported).

These tests are LOCAL (no network, no live DB) — they use the real
in-memory async-mongo stub to prove runtime adoption.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ─────────────────────────────────────────────────────────────────────
# Minimal async-Mongo stub (match a subset of the motor cursor API)
# ─────────────────────────────────────────────────────────────────────
class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._limit = None

    def sort(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        self._it = iter(self._rows if self._limit is None
                        else self._rows[:self._limit])
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Coll:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def find(self, q=None, projection=None):
        q = q or {}
        return _Cursor([r for r in self._rows if _matches(r, q)])

    async def find_one(self, q=None, projection=None):
        for r in self._rows:
            if _matches(r, q or {}):
                return r
        return None

    async def count_documents(self, q=None):
        q = q or {}
        return sum(1 for r in self._rows if _matches(r, q))

    async def insert_one(self, doc):
        self._rows.append(dict(doc))

    async def update_one(self, filt, update, upsert=False):
        for i, r in enumerate(self._rows):
            if _matches(r, filt):
                if "$set" in update:
                    r.update(update["$set"])
                if "$setOnInsert" in update:
                    pass
                return
        if upsert:
            new = {}
            new.update(filt)
            if "$set" in update:
                new.update(update["$set"])
            if "$setOnInsert" in update:
                new.update(update["$setOnInsert"])
            self._rows.append(new)


def _matches(doc, query):
    if not isinstance(query, dict):
        return False
    for k, v in query.items():
        if k == "$and":
            if not all(_matches(doc, c) for c in v):
                return False
            continue
        if k == "$or":
            if not any(_matches(doc, c) for c in v):
                return False
            continue
        if isinstance(v, dict) and ("$in" in v or "$lt" in v
                                     or "$gt" in v):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$lt" in v and not (doc.get(k) and doc.get(k) < v["$lt"]):
                return False
            if "$gt" in v and not (doc.get(k) and doc.get(k) > v["$gt"]):
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class _DB(dict):
    def __init__(self, **colls):
        super().__init__()
        for k, v in colls.items():
            self[k] = v

    def __getattr__(self, name):
        if name not in self:
            self[name] = _Coll()
        return self[name]

    def __getitem__(self, k):
        if k not in self:
            super().__setitem__(k, _Coll())
        return super().__getitem__(k)


# ═══════════════════════════════════════════════════════════════════
# Item 1 — query_historical universally augments VS OPP with direct_h2h
# ═══════════════════════════════════════════════════════════════════

def test_b5c_query_historical_imports_direct_h2h():
    """``query_historical`` must call the shared ``direct_h2h`` helper
    at the HI consumer boundary — not merely from CFB's adapter."""
    src = open(
        "/app/backend/services/historical_intelligence.py").read()
    qh_idx = src.index("async def query_historical(")
    body = src[qh_idx: qh_idx + 20_000]
    assert "direct_h2h" in body, (
        "query_historical must import the shared direct_h2h helper")
    assert ("direct_player_vs_opp" in body
            and "direct_team_vs_opp" in body), (
        "query_historical must call both direct_player_vs_opp and "
        "direct_team_vs_opp at the HI consumer boundary (B5c)")


def test_b5c_trevor_lawrence_canary_direct_h2h_augmentation():
    """Trevor Lawrence VS DEN — the HI consumer must deliver the
    DIRECT H2H row even when the player's recent-80 adapter buffer
    does NOT include that meeting.
    """
    from services.historical_intelligence import (
        HistoricalQuery, query_historical,
    )
    # Player's recent buffer (adapter view) — does NOT contain DEN.
    adapter_rows = [
        {"sport": "nfl",
         "canonical_player_id": "00-0036355",  # Trevor Lawrence GSIS
         "player_name": "Trevor Lawrence",
         "team": "JAX", "opponent": "HOU",
         "canonical_opponent_id": "nfl:t:HOU",
         "actuals": {"passing_yards": 275.0},
         "event_time": "2026-03-10T18:00:00Z",
         "canonical_event_id": "nfl_evt_jaxhou_2026",
         "event_id": "nfl_evt_jaxhou_2026"},
        {"sport": "nfl",
         "canonical_player_id": "00-0036355",
         "player_name": "Trevor Lawrence",
         "team": "JAX", "opponent": "TEN",
         "canonical_opponent_id": "nfl:t:TEN",
         "actuals": {"passing_yards": 240.0},
         "event_time": "2026-03-03T18:00:00Z",
         "canonical_event_id": "nfl_evt_jaxten_2026",
         "event_id": "nfl_evt_jaxten_2026"},
    ]
    # DIRECT H2H row — exists in canonical actuals but NOT in buffer.
    direct_rows = [
        {"sport": "NFL",
         "canonical_player_id": "00-0036355",
         "player_name": "Trevor Lawrence",
         "team": "JAX", "opponent": "DEN",
         "canonical_opponent_id": "nfl:t:DEN",
         "actuals": {"passing_yards": 311.0},
         "event_time": "2025-11-24T18:00:00Z",
         "canonical_event_id": "nfl_evt_jaxden_2025"},
    ]
    db = _DB(
        player_game_actuals=_Coll(adapter_rows + direct_rows),
    )
    q = HistoricalQuery(
        sport="NFL", entity_type="player",
        entity_id="00-0036355", entity_name="Trevor Lawrence",
        opponent_id="nfl:t:DEN", opponent_name="Denver Broncos",
        market_family="pass_yds",
        current_threshold=249.5, side="over",
        sample_scope="ALL",
    )
    resp = asyncio.get_event_loop().run_until_complete(
        query_historical(db, q))
    assert resp is not None
    # Direct-H2H row MUST have been merged into games.
    event_ids = {g.event_id for g in (resp.games or [])}
    assert "nfl_evt_jaxden_2025" in event_ids, (
        "B5c: Trevor Lawrence VS DEN direct-H2H row did not reach "
        f"query_historical.games; got event_ids={event_ids}")


def test_b5c_model_visible_equals_hi_visible_parity():
    """Every observation the MODEL sees via the adapter's
    ``fetch_observations`` must be present in the dedupe set the
    HI consumer returns.  B5c direct-H2H augmentation ADDS rows,
    never silently drops them."""
    from services.historical_intelligence import (
        HistoricalQuery, query_historical,
        get_adapter,
    )
    adapter_rows = [
        {"sport": "nfl",
         "canonical_player_id": "00-ADAPTER",
         "player_name": "Adapter Only",
         "team": "ABC", "opponent": "XYZ",
         "canonical_opponent_id": "nfl:t:XYZ",
         "actuals": {"passing_yards": 220.0},
         "event_time": "2026-02-01T18:00:00Z",
         "canonical_event_id": "nfl_evt_abcxyz_2026",
         "event_id": "nfl_evt_abcxyz_2026"},
    ]
    db = _DB(player_game_actuals=_Coll(adapter_rows))
    q = HistoricalQuery(
        sport="NFL", entity_type="player",
        entity_id="00-ADAPTER", entity_name="Adapter Only",
        opponent_id="nfl:t:XYZ", opponent_name="XYZ",
        market_family="pass_yds",
        current_threshold=249.5, side="over",
        sample_scope="ALL",
    )
    # Model view — direct adapter call.
    adapter = get_adapter("NFL")
    assert adapter is not None, (
        "NFL adapter must be registered for parity test")
    model_obs = asyncio.get_event_loop().run_until_complete(
        adapter.fetch_observations(db, q))
    # HI view — through the consumer boundary.
    resp = asyncio.get_event_loop().run_until_complete(
        query_historical(db, q))
    hi_eids = {g.event_id for g in (resp.games or [])}
    model_eids = {o.event_id for o in model_obs if o.event_id}
    assert model_eids.issubset(hi_eids), (
        f"HI parity broken — model sees {model_eids - hi_eids} that "
        f"HI does not")


# ═══════════════════════════════════════════════════════════════════
# Item 2 — ESPN canonical enrichment adoption
# ═══════════════════════════════════════════════════════════════════

def test_b5c_espn_canonical_backbone_projector_exists():
    """A shared ESPN canonical-backbone projector must exist and be
    exported from the HI projectors module (B5c runtime adoption)."""
    from services import historical_intelligence_projectors as hip
    assert hasattr(hip, "project_espn_canonical_actuals"), (
        "project_espn_canonical_actuals must be exported for B5c "
        "ESPN canonical enrichment adoption")


def test_b5c_espn_canonical_backbone_wires_canonical_enrichment():
    """``project_espn_canonical_actuals`` must call the shared
    ``espn_canonical_enrichment`` helpers (not reinvent)."""
    src = open(
        "/app/backend/services/historical_intelligence_projectors.py"
    ).read()
    fn_idx = src.index("def project_espn_canonical_actuals")
    body = src[fn_idx: fn_idx + 6_000]
    assert "canonical_team_row" in body
    assert "canonical_player_row" in body
    assert "espn_canonical_enrichment" in body


def test_b5c_espn_backbone_is_called_from_nfl_projector():
    """NFL projector must invoke the ESPN canonical backbone as a
    complementary pass (B5c runtime adoption)."""
    src = open(
        "/app/backend/services/historical_intelligence_projectors.py"
    ).read()
    nfl_idx = src.index("async def project_nfl_actuals(")
    nfl_body = src[nfl_idx: src.index("# ──", nfl_idx + 200)
                            if "# ──" in src[nfl_idx + 200:]
                            else nfl_idx + 6_000]
    # Allow either placement — the function must at least reference
    # the backbone call before returning.
    body = src[nfl_idx: src.index("async def project_mlb_actuals(",
                                    nfl_idx)]
    assert "project_espn_canonical_actuals" in body, (
        "project_nfl_actuals must call project_espn_canonical_actuals "
        "to adopt ESPN canonical enrichment")


def test_b5c_espn_canonical_backbone_runs_end_to_end():
    """Fire ``project_espn_canonical_actuals`` against an in-memory
    ESPN cache and verify ``team_game_actuals`` is written through
    the ``canonical_team_row`` helper (source=``espn_canonical_
    enrichment``)."""
    from services.historical_intelligence_projectors import (
        project_espn_canonical_actuals,
    )
    espn_rows = [{
        "sport": "nfl",
        "id": "espn_evt_1",
        "event_id": "espn_evt_1",
        "date": "2026-05-01T18:00:00Z",
        "season": 2026, "week": 5,
        "competitors": [
            {"id": "JAX", "team_id": "JAX", "homeAway": "home",
             "name": "Jaguars", "displayName": "Jacksonville Jaguars",
             "score": 27},
            {"id": "DEN", "team_id": "DEN", "homeAway": "away",
             "name": "Broncos", "displayName": "Denver Broncos",
             "score": 20},
        ],
    }]
    db = _DB(
        espn_scoreboard_cache=_Coll(espn_rows),
        team_game_actuals=_Coll(),
        player_game_actuals=_Coll(),
    )
    report = asyncio.get_event_loop().run_until_complete(
        project_espn_canonical_actuals(db, "nfl"))
    assert report.get("team_rows_written", 0) == 2, (
        f"B5c ESPN canonical backbone must write both team rows; "
        f"got {report}")
    # Verify the row shape (source stamp from canonical_team_row).
    assert any(
        r.get("source") == "espn_canonical_enrichment"
        for r in db.team_game_actuals._rows
    ), (
        "team_game_actuals rows must carry source="
        "'espn_canonical_enrichment'")


# ═══════════════════════════════════════════════════════════════════
# Preservation — B5a + B5b + B4.2 contracts still hold
# ═══════════════════════════════════════════════════════════════════

def test_b5c_preserves_b5a_scheduler_wiring():
    src = open("/app/backend/sports_engine.py").read()
    assert "classify_sport_activation" in src


def test_b5c_preserves_b5b_build_pick_instrumentation():
    src = open("/app/backend/sports_engine.py").read()
    assert "STAGE_MODEL_CANDIDATES" in src
    assert "STAGE_AT_OR_ABOVE_85" in src
