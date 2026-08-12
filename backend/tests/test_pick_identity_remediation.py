"""Pre-Magic Blocker Remediation — deterministic tests.

Covers:
  * Identity enricher (team / player / total / individual / unknown)
  * Producer regression coverage — proves every publisher that flows
    through ``publish_upserted_picks`` attaches identity.
  * Negative identity tests (§16).
  * Model evidence extractor (§17).
  * End-to-end publish → persisted pick has canonical identity +
    model_probability.

All tests are self-contained and use an in-memory fake Mongo.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# Fake async Mongo (same shape as pre_magic tests)
# ═══════════════════════════════════════════════════════════════════
class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=1):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]
        self._i += 1
        return dict(d)


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
        self.updates: list = []

    def find(self, query=None, projection=None):
        return _AsyncCursor(list(self.docs))

    async def find_one(self, query=None, projection=None):
        q = query or {}
        for d in self.docs:
            ok = True
            for k, v in q.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                return dict(d)
        return None

    async def update_one(self, filter, update, upsert=False):
        self.updates.append({"filter": filter, "update": update})
        for d in self.docs:
            match = all(d.get(k) == v for k, v in filter.items())
            if match:
                d.update(update.get("$set") or {})
                return type("Res", (), {"matched_count": 1, "modified_count": 1})
        if upsert:
            new = dict(filter)
            new.update(update.get("$set") or {})
            self.docs.append(new)
        return type("Res", (), {"matched_count": 0, "modified_count": 0})

    async def count_documents(self, query=None):
        return len(self.docs)

    async def create_index(self, *a, **k):
        return "ok"


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection())

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.__getitem__(name)


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# §7. Event parsing
# ═══════════════════════════════════════════════════════════════════
def test_parse_event_at_separator():
    from services.pick_identity_enricher import parse_event_participants
    away, home = parse_event_participants("Arizona Diamondbacks @ Miami Marlins")
    assert away == "Arizona Diamondbacks"
    assert home == "Miami Marlins"


def test_parse_event_vs_separator():
    from services.pick_identity_enricher import parse_event_participants
    a, b = parse_event_participants("Team A vs Team B")
    assert (a, b) == ("Team A", "Team B")


def test_parse_event_unparsable_returns_none():
    from services.pick_identity_enricher import parse_event_participants
    assert parse_event_participants("no separator here") == (None, None)
    assert parse_event_participants("") == (None, None)
    assert parse_event_participants(None) == (None, None)


# ═══════════════════════════════════════════════════════════════════
# Market classification
# ═══════════════════════════════════════════════════════════════════
def test_classify_team_moneyline():
    from services.pick_identity_enricher import classify_market
    assert classify_market(market="Miami Marlins Moneyline",
                            selection="Miami Marlins") == "TEAM"


def test_classify_total():
    from services.pick_identity_enricher import classify_market
    assert classify_market(market="Total Goals Over 2.5",
                            selection="Over") == "TOTAL"
    assert classify_market(market="Total Points Under 220.5",
                            selection="Under") == "TOTAL"


def test_classify_player_prop():
    from services.pick_identity_enricher import classify_market
    assert classify_market(market="Mikal Bridges Over 1.5 Assists",
                            selection="Mikal Bridges") == "PLAYER"


def test_classify_unknown():
    from services.pick_identity_enricher import classify_market
    assert classify_market(market="???",
                            selection="???") == "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Team pick identity
# ═══════════════════════════════════════════════════════════════════
def test_team_pick_full_enrichment():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "id": "p-mlb-1",
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "bet_type": "STRAIGHT",
    }
    e = enrich_pick_identity(pick)
    assert e["home_team_name"] == "Miami Marlins"
    assert e["away_team_name"] == "Arizona Diamondbacks"
    assert e["team"] == "Miami Marlins"
    assert e["opponent_team"] == "Arizona Diamondbacks"
    assert e["canonical_team_id"] is not None
    assert e["canonical_opponent_id"] is not None
    assert e["canonical_event_id"] is not None
    # Fallback quality — no provider IDs on the pick.
    assert e["identity_quality"] == "fallback"
    assert e["pick_identity_version"] == 1


def test_team_pick_selection_is_away():
    """When we pick the away team, opponent must be the home team."""
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Arizona Diamondbacks Moneyline",
        "selection": "Arizona Diamondbacks",
    }
    e = enrich_pick_identity(pick)
    assert e["team"] == "Arizona Diamondbacks"
    assert e["opponent_team"] == "Miami Marlins"


# ═══════════════════════════════════════════════════════════════════
# Total pick — no team, but event resolved
# ═══════════════════════════════════════════════════════════════════
def test_total_pick_no_team_but_event_resolved():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "Soccer",
        "event": "Ponte Preta @ Juventude",
        "event_time": "2026-06-12T22:00:00Z",
        "market": "Total Goals Over 2.5",
        "selection": "Over",
    }
    e = enrich_pick_identity(pick)
    # Total markets get event + home/away names but NO team pick.
    assert e.get("canonical_team_id") is None
    assert e["home_team_name"] == "Juventude"
    assert e["away_team_name"] == "Ponte Preta"
    assert e["canonical_event_id"] is not None
    # Market class recorded.
    assert e["identity_resolution"]["market_class"] == "TOTAL"


# ═══════════════════════════════════════════════════════════════════
# Player-prop identity
# ═══════════════════════════════════════════════════════════════════
def test_player_prop_without_team_context_stays_unresolved():
    """§4: a player pick with no team context must NOT get a
    canonical_player_id.  Missing stays missing."""
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "NBA",
        "event": "New York Knicks @ San Antonio Spurs",
        "event_time": "2026-06-14T00:40:00Z",
        "market": "Mikal Bridges Over 1.5 Assists",
        "selection": "Mikal Bridges",
    }
    e = enrich_pick_identity(pick)
    assert e.get("player_name") == "Mikal Bridges"
    # No team context — canonical id must NOT be set.
    assert "canonical_player_id" not in e or e.get("canonical_player_id") is None
    assert e["identity_quality"] == "unresolved"


def test_player_prop_with_team_context_gets_canonical():
    """When we pass explicit team context, resolver returns a
    canonical fallback id (deterministic, still marked ``fallback``)."""
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "NBA",
        "event": "New York Knicks @ San Antonio Spurs",
        "event_time": "2026-06-14T00:40:00Z",
        "market": "Mikal Bridges Over 1.5 Assists",
        "selection": "Mikal Bridges",
        "team": "New York Knicks",   # team context
    }
    e = enrich_pick_identity(pick)
    assert e["canonical_player_id"] is not None
    assert e["identity_quality"] == "fallback"


def test_player_extraction_from_market_string():
    from services.pick_identity_enricher import extract_player_name_from_market
    assert extract_player_name_from_market(
        "Aaron Judge Over 1.5 Total Bases") == "Aaron Judge"
    assert extract_player_name_from_market(
        "Patrick Mahomes Anytime TD") == "Patrick Mahomes"
    assert extract_player_name_from_market("Total Goals Over 2.5") is None
    assert extract_player_name_from_market(None) is None


# ═══════════════════════════════════════════════════════════════════
# Individual sport (Tennis / UFC)
# ═══════════════════════════════════════════════════════════════════
def test_tennis_individual_pick():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "Tennis",
        "event": "Emma Raducanu @ Sorana Cirstea",
        "event_time": "2026-06-11T14:30:00Z",
        "market": "Sorana Cirstea Moneyline",
        "selection": "Sorana Cirstea",
    }
    e = enrich_pick_identity(pick)
    assert e["canonical_player_id"] is not None
    assert e["player_name"] == "Sorana Cirstea"
    assert e.get("canonical_opponent_id") is not None
    assert e["opponent_team"] == "Emma Raducanu"
    assert e["identity_resolution"]["market_class"] == "INDIVIDUAL"


def test_ufc_individual_pick():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "UFC",
        "event": "Steve Garcia Jr. @ Diego Lopes",
        "event_time": "2026-06-15T00:15:00Z",
        "market": "Diego Lopes Moneyline",
        "selection": "Diego Lopes",
    }
    e = enrich_pick_identity(pick)
    assert e["canonical_player_id"] is not None
    assert e["player_name"] == "Diego Lopes"
    assert e["opponent_team"] == "Steve Garcia Jr."


# ═══════════════════════════════════════════════════════════════════
# §16 — Negative identity tests
# ═══════════════════════════════════════════════════════════════════
def test_same_name_players_cannot_collide():
    """Two players named 'Aaron Judge' on DIFFERENT teams get
    different canonical fallback IDs.  The team_id is part of the
    identity key."""
    from services.identity_resolver import resolve_player
    p1 = resolve_player(display_name="Aaron Judge", sport="mlb",
                        team_id="fallback:nyy_hash")
    p2 = resolve_player(display_name="Aaron Judge", sport="mlb",
                        team_id="fallback:bos_hash")
    assert p1.canonical_player_id != p2.canonical_player_id


def test_unresolved_player_gets_no_guess_id():
    """A player without team_id and no provider id → unresolved."""
    from services.identity_resolver import resolve_player
    p = resolve_player(display_name="Some Player", sport="nba")
    assert p.identity_quality == "unresolved"
    assert p.canonical_player_id.startswith("unresolved:")


def test_sport_boundaries_cannot_collide():
    """Same team name in different sports gets different ids."""
    from services.identity_resolver import resolve_team
    t1 = resolve_team(display_name="Giants", sport="mlb")
    t2 = resolve_team(display_name="Giants", sport="nfl")
    assert t1.canonical_team_id != t2.canonical_team_id


def test_market_string_alone_cannot_certify():
    """A pick with ONLY a market string (no event, no selection)
    cannot get a canonical_event_id or canonical_team_id."""
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {"sport": "MLB", "market": "Miami Marlins Moneyline"}
    e = enrich_pick_identity(pick)
    assert e.get("canonical_event_id") is None
    assert e.get("canonical_team_id") is None
    assert e.get("home_team_name") is None
    # `selection` alone isn't enough either.


def test_missing_id_stays_missing():
    """No event, no selection, no market → nothing enriched beyond
    metadata."""
    from services.pick_identity_enricher import enrich_pick_identity
    e = enrich_pick_identity({"sport": "MLB"})
    assert e.get("canonical_event_id") is None
    assert e.get("canonical_team_id") is None
    assert e.get("canonical_player_id") is None
    assert e["identity_quality"] == "unresolved"


def test_wrong_event_cannot_attach_history():
    """The enricher must not derive canonical_event_id when either
    team, sport, or commence is missing (§6)."""
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "MLB",
        "event": "TeamA @ TeamB",
        # NO event_time
        "market": "TeamA Moneyline",
        "selection": "TeamA",
    }
    e = enrich_pick_identity(pick)
    assert e.get("canonical_event_id") is None
    # But the teams can still resolve.
    assert e.get("canonical_team_id") is not None


def test_idempotent_reruns_produce_same_ids():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
    }
    a = enrich_pick_identity(pick)
    b = enrich_pick_identity(pick)
    # Timestamps differ by design; canonical ids MUST be stable.
    for k in ("canonical_team_id", "canonical_opponent_id",
              "canonical_event_id", "home_team_name", "away_team_name"):
        assert a[k] == b[k]


def test_producer_supplied_canonical_preserved():
    """§3: apply_enrichment must NOT overwrite canonical ids already
    provided by a producer (authoritative source of truth)."""
    from services.pick_identity_enricher import apply_enrichment
    pick = {
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "canonical_team_id": "mlb:PROVIDER:MIA",   # authoritative
    }
    out = apply_enrichment(pick)
    assert out["canonical_team_id"] == "mlb:PROVIDER:MIA"


# ═══════════════════════════════════════════════════════════════════
# §17 — Model evidence tests
# ═══════════════════════════════════════════════════════════════════
def test_model_probability_from_win_probability():
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "win_probability": 62.1,          # percent form
        "implied_probability": 53.9,      # sportsbook — MUST NOT be picked
        "v2_engine_version": "1.0.0",
        "model_version": "mlb_v3",
        "calibration_version": "legacy_unknown",
    }
    e = extract_model_evidence(pick)
    assert 0.6 < e["model_probability"] < 0.63    # scaled to [0,1]
    assert e["model_probability_source"] == "win_probability"
    prov = e["model_probability_provenance"]
    assert prov["source_field"] == "win_probability"
    assert prov["v2_engine_version"] == "1.0.0"
    assert prov["model_version"] == "mlb_v3"
    assert prov["kind"] == "model"


def test_model_probability_prefers_existing_canonical_field():
    """If ``model_probability`` already exists, use that value."""
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "model_probability": 0.55,
        "win_probability": 62.1,
    }
    e = extract_model_evidence(pick)
    assert e["model_probability"] == 0.55


def test_implied_probability_does_not_masquerade():
    """§17: implied_probability MUST NOT be promoted to
    model_probability under any circumstance."""
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "implied_probability": 53.9,
        "book_odds": -117,
        # No win_probability / model_probability / published_probability
    }
    e = extract_model_evidence(pick)
    # No model source found → NO model_probability at all.
    assert "model_probability" not in e


def test_lock_score_does_not_masquerade():
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "lock_score": 92.5,
        "edge_percent": 8.17,
    }
    e = extract_model_evidence(pick)
    assert "model_probability" not in e


def test_simulator_probability_kept_distinct():
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "win_probability": 62.1,
        "simulator_probability": 0.58,
    }
    e = extract_model_evidence(pick)
    # Both present, distinct.
    assert e["model_probability"] != e["simulator_probability"]
    assert e["simulator_probability"] == 0.58
    assert e["simulator_probability_source"] == "simulator_probability"


def test_missing_model_stays_unknown_not_zero():
    """§17: missing → UNKNOWN (absent field), NEVER 0."""
    from services.pick_model_evidence import extract_model_evidence
    e = extract_model_evidence({})
    assert "model_probability" not in e
    # Do not fabricate a 0.
    assert e.get("model_probability") is None


def test_no_probability_is_manufactured_from_odds():
    """A pick with only sportsbook odds MUST NOT get a model_probability."""
    from services.pick_model_evidence import extract_model_evidence
    e = extract_model_evidence({"book_odds": -110, "implied_probability": 0.52})
    assert "model_probability" not in e


def test_calibrated_kind_when_calibration_version_present():
    from services.pick_model_evidence import extract_model_evidence
    pick = {
        "win_probability": 62.1,
        "calibration_version": "v4-lockband",  # real calibration applied
    }
    e = extract_model_evidence(pick)
    assert e["model_probability_provenance"]["kind"] == "calibrated"


def test_model_probability_accepts_zero_one_range():
    """Values already in [0,1] pass through."""
    from services.pick_model_evidence import extract_model_evidence
    e = extract_model_evidence({"win_probability": 0.62})
    assert 0.6 < e["model_probability"] < 0.63


def test_nan_probability_rejected():
    from services.pick_model_evidence import extract_model_evidence
    e = extract_model_evidence({"win_probability": float("nan")})
    assert "model_probability" not in e


# ═══════════════════════════════════════════════════════════════════
# §15 — Producer regression tests
# End-to-end: publisher writes pick → publish_upserted_picks →
# persisted pick document has canonical identity.
# ═══════════════════════════════════════════════════════════════════
def test_publish_upserted_picks_enriches_team_pick_end_to_end():
    """The central choke point must add canonical identity to a
    real-looking producer pick."""
    from services import publication_helpers

    db = _FakeDB()
    # Seed the picks collection as if a producer just upserted a pick.
    pick = {
        "id": "e2e-mlb-1",
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "win_probability": 62.1,
        "implied_probability": 53.9,
        "v2_engine_version": "1.0.0",
        "book_odds": -117,
    }
    db["picks"].docs.append(dict(pick))

    # publish_batch is monkey-patched to a no-op — we only care about
    # the enrichment side-effect on db.picks.
    from unittest.mock import patch, AsyncMock, MagicMock

    async def _fake_publish_batch(*a, **kw):
        return {"new_snapshots": 1, "existing_snapshots": 0,
                "errors": [], "mismatches_logged": 0,
                "board_version": "test"}

    class _FakeService:
        def __init__(self, db_):
            pass
        async def ensure_indices(self):
            return None
        async def publish_batch(self, *a, **kw):
            return await _fake_publish_batch(*a, **kw)

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        summary = _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    # The pick in db must now carry canonical identity.
    persisted = db["picks"].docs[0]
    assert persisted.get("canonical_team_id") is not None
    assert persisted.get("canonical_opponent_id") is not None
    assert persisted.get("canonical_event_id") is not None
    assert persisted.get("home_team_name") == "Miami Marlins"
    assert persisted.get("away_team_name") == "Arizona Diamondbacks"
    # Model probability persisted with provenance.
    assert 0.6 < persisted.get("model_probability") < 0.63
    assert persisted.get("model_probability_source") == "win_probability"
    prov = persisted.get("model_probability_provenance")
    assert prov and prov.get("v2_engine_version") == "1.0.0"
    # implied_probability preserved unchanged.
    assert persisted.get("implied_probability") == 53.9


def test_publish_upserted_picks_enriches_player_prop_pick():
    from services import publication_helpers
    from unittest.mock import patch, AsyncMock

    db = _FakeDB()
    pick = {
        "id": "e2e-nba-1",
        "sport": "NBA",
        "event": "New York Knicks @ San Antonio Spurs",
        "event_time": "2026-06-14T00:40:00Z",
        "market": "Mikal Bridges Over 1.5 Assists",
        "selection": "Mikal Bridges",
        "team": "New York Knicks",
        "win_probability": 55.3,
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw):
            return {"new_snapshots": 1}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    assert persisted.get("canonical_player_id") is not None
    assert persisted.get("player_name") == "Mikal Bridges"
    assert persisted.get("canonical_team_id") is not None
    assert 0.5 < persisted.get("model_probability") < 0.56


def test_publish_upserted_picks_enriches_individual_sport_pick():
    from services import publication_helpers
    from unittest.mock import patch, AsyncMock

    db = _FakeDB()
    pick = {
        "id": "e2e-ten-1",
        "sport": "Tennis",
        "event": "Emma Raducanu @ Sorana Cirstea",
        "event_time": "2026-06-11T14:30:00Z",
        "market": "Sorana Cirstea Moneyline",
        "selection": "Sorana Cirstea",
        "win_probability": 46.6,
        "calibration_version": "v4-lockband",
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw): return {}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    assert persisted.get("canonical_player_id") is not None
    assert persisted.get("canonical_opponent_id") is not None
    # Calibrated kind — calibration_version was set.
    prov = persisted.get("model_probability_provenance") or {}
    assert prov.get("kind") == "calibrated"


def test_publish_upserted_picks_does_not_alter_scoring():
    """§18: enrichment must NOT modify lock_score / model_probability
    when it already exists / grade / off_board / eligibility flags."""
    from services import publication_helpers
    from unittest.mock import patch, AsyncMock

    db = _FakeDB()
    pick = {
        "id": "e2e-guard-1",
        "sport": "MLB",
        "event": "Team A @ Team B",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Team B Moneyline",
        "selection": "Team B",
        "win_probability": 62.1,
        "model_probability": 0.99,   # authoritative producer value
        "lock_score": 85.0,
        "grade": "Playable",
        "off_board": False,
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw): return {}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    # Producer's model_probability preserved.
    assert persisted["model_probability"] == 0.99
    # Untouched scoring fields.
    assert persisted["lock_score"] == 85.0
    assert persisted["grade"] == "Playable"
    assert persisted["off_board"] is False


def test_enrichment_survives_missing_optional_fields():
    """Ingest paths sometimes omit event_time / bet_type — enrichment
    must still produce partial identity and not crash."""
    from services.pick_identity_enricher import enrich_pick_identity
    e = enrich_pick_identity({
        "sport": "MLB",
        "event": "A @ B",
        "market": "B Moneyline",
        "selection": "B",
    })
    assert e.get("canonical_team_id") is not None
    assert e.get("canonical_event_id") is None   # missing commence_time
    assert e["identity_quality"] == "fallback"


def test_totals_selection_over_under_not_treated_as_player():
    from services.pick_identity_enricher import enrich_pick_identity
    pick = {
        "sport": "NBA",
        "event": "A @ B",
        "event_time": "2026-06-01T00:00:00Z",
        "market": "Total Points Over 220.5",
        "selection": "Over",
    }
    e = enrich_pick_identity(pick)
    assert e.get("canonical_player_id") is None
    assert e.get("canonical_team_id") is None
    assert e["identity_resolution"]["market_class"] == "TOTAL"


# ═══════════════════════════════════════════════════════════════════
# §3 — Authoritative identity lookup (uses history collections)
# ═══════════════════════════════════════════════════════════════════
def test_authoritative_team_lookup_returns_history_id():
    """A pick's team_name that MATCHES a team_game_actuals row must
    resolve to the SAME canonical_team_id used by history."""
    from services.pick_identity_authority import (
        resolve_team_authoritative, clear_cache,
    )
    clear_cache()
    db = _FakeDB()
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Miami Marlins",
        "team_name": "Miami Marlins",
    })
    got = _run(resolve_team_authoritative(
        db, sport="MLB", name="Miami Marlins"))
    assert got == "Miami Marlins"


def test_authoritative_team_lookup_miss_returns_none():
    from services.pick_identity_authority import (
        resolve_team_authoritative, clear_cache,
    )
    clear_cache()
    db = _FakeDB()
    got = _run(resolve_team_authoritative(
        db, sport="MLB", name="Nonexistent Team"))
    assert got is None


def test_authoritative_player_lookup_returns_provider_id():
    from services.pick_identity_authority import (
        resolve_player_authoritative, clear_cache,
    )
    clear_cache()
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "405395",
        "player_name": "Aaron Judge",
        "team": "New York Yankees",
    })
    got = _run(resolve_player_authoritative(
        db, sport="MLB", name="Aaron Judge"))
    assert got == "405395"


def test_authoritative_player_lookup_ambiguous_returns_none():
    """§4: two players with the same name and no team hint → NONE,
    never a guess."""
    from services.pick_identity_authority import (
        resolve_player_authoritative, clear_cache,
    )
    clear_cache()
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "111",
        "player_name": "Aaron Judge", "team": "NYY",
    })
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "222",
        "player_name": "Aaron Judge", "team": "BOS",
    })
    got = _run(resolve_player_authoritative(
        db, sport="MLB", name="Aaron Judge"))
    assert got is None


def test_authoritative_player_lookup_team_hint_disambiguates():
    from services.pick_identity_authority import (
        resolve_player_authoritative, clear_cache,
    )
    clear_cache()
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "111",
        "player_name": "Aaron Judge", "team": "NYY",
    })
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "222",
        "player_name": "Aaron Judge", "team": "BOS",
    })
    got = _run(resolve_player_authoritative(
        db, sport="MLB", name="Aaron Judge", team_hint="NYY"))
    assert got == "111"


def test_async_enricher_upgrades_team_to_authoritative():
    """§3: the async enricher, when history has a matching team,
    returns the AUTHORITATIVE canonical_team_id, not the fallback
    hash."""
    from services.pick_identity_enricher import enrich_pick_identity_async
    from services.pick_identity_authority import clear_cache

    clear_cache()
    db = _FakeDB()
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Miami Marlins",
        "team_name": "Miami Marlins",
    })
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Arizona Diamondbacks",
        "team_name": "Arizona Diamondbacks",
    })
    pick = {
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
    }
    e = _run(enrich_pick_identity_async(db, pick))
    assert e["canonical_team_id"] == "Miami Marlins"
    assert e["canonical_opponent_id"] == "Arizona Diamondbacks"
    assert e["identity_quality"] == "authoritative"


def test_async_enricher_falls_back_when_no_history_match():
    """No history match → deterministic fallback hash, marked
    ``fallback`` (identity_quality)."""
    from services.pick_identity_enricher import enrich_pick_identity_async
    from services.pick_identity_authority import clear_cache

    clear_cache()
    db = _FakeDB()   # empty history
    pick = {
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
    }
    e = _run(enrich_pick_identity_async(db, pick))
    assert e["canonical_team_id"].startswith("fallback:")
    assert e["identity_quality"] == "fallback"


def test_async_enricher_upgrades_player_to_provider_id():
    from services.pick_identity_enricher import enrich_pick_identity_async
    from services.pick_identity_authority import clear_cache

    clear_cache()
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "nba", "canonical_player_id": "2490149",
        "player_name": "Mikal Bridges", "team": "New York Knicks",
    })
    pick = {
        "sport": "NBA",
        "event": "New York Knicks @ San Antonio Spurs",
        "event_time": "2026-06-14T00:40:00Z",
        "market": "Mikal Bridges Over 1.5 Assists",
        "selection": "Mikal Bridges",
        "team": "New York Knicks",
    }
    e = _run(enrich_pick_identity_async(db, pick))
    assert e["canonical_player_id"] == "2490149"
    assert e["identity_quality"] == "authoritative"


def test_publish_upserted_picks_uses_authoritative_when_available():
    """End-to-end: producer publishes pick → publication_helpers
    → authoritative canonical id from history collections stamped."""
    from services import publication_helpers
    from services.pick_identity_authority import clear_cache
    from unittest.mock import patch, AsyncMock

    clear_cache()
    db = _FakeDB()
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Miami Marlins",
        "team_name": "Miami Marlins",
    })
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Arizona Diamondbacks",
        "team_name": "Arizona Diamondbacks",
    })
    pick = {
        "id": "e2e-auth-1",
        "sport": "MLB",
        "event": "Arizona Diamondbacks @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "win_probability": 62.1,
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw): return {}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    # AUTHORITATIVE id used — matches the history collection.
    assert persisted["canonical_team_id"] == "Miami Marlins"
    assert persisted["canonical_opponent_id"] == "Arizona Diamondbacks"
    assert persisted["identity_quality"] == "authoritative"


def test_publish_upserted_picks_upgrades_fallback_to_authoritative():
    """A pick previously stamped with ``fallback:*`` ids gets
    upgraded when authoritative history data becomes available."""
    from services import publication_helpers
    from services.pick_identity_authority import clear_cache
    from unittest.mock import patch, AsyncMock

    clear_cache()
    db = _FakeDB()
    db["team_game_actuals"].docs.append({
        "sport": "mlb", "canonical_team_id": "Miami Marlins",
        "team_name": "Miami Marlins",
    })
    pick = {
        "id": "e2e-upgrade-1",
        "sport": "MLB",
        "event": "A @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        # Previously stamped fallback (from a pre-authority run):
        "canonical_team_id": "fallback:abc123",
        "identity_quality": "fallback",
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw): return {}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    # Fallback upgraded.
    assert persisted["canonical_team_id"] == "Miami Marlins"
    assert persisted["identity_quality"] == "authoritative"


def test_publish_never_downgrades_authoritative_to_fallback():
    """A pick with an authoritative id must NOT be downgraded to a
    fallback hash on republication (idempotence)."""
    from services import publication_helpers
    from services.pick_identity_authority import clear_cache
    from unittest.mock import patch, AsyncMock

    clear_cache()
    db = _FakeDB()   # empty history — authority lookup misses
    pick = {
        "id": "e2e-noflap-1",
        "sport": "MLB",
        "event": "A @ Miami Marlins",
        "event_time": "2026-06-11T17:11:00Z",
        "market": "Miami Marlins Moneyline",
        "selection": "Miami Marlins",
        "canonical_team_id": "Miami Marlins",     # already authoritative
        "identity_quality": "authoritative",
    }
    db["picks"].docs.append(dict(pick))

    class _FakeService:
        def __init__(self, db_): pass
        async def ensure_indices(self): return None
        async def publish_batch(self, *a, **kw): return {}

    with patch(
        "services.prediction_publication_service.PredictionPublicationService",
        _FakeService,
    ), patch(
        "services.production_truth.publication_observer.observe_publication",
        AsyncMock(return_value={}),
    ):
        _run(publication_helpers.publish_upserted_picks(
            db, [pick], publication_source="test", caller_label="test"))
    persisted = db["picks"].docs[0]
    # Still authoritative — never downgraded.
    assert persisted["canonical_team_id"] == "Miami Marlins"
    assert persisted["identity_quality"] == "authoritative"
