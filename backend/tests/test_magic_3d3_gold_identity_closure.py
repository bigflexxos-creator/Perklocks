"""MAGIC 3D.3 — GOLD IDENTITY CLOSURE integration tests.

Proves the two producer-side identity gates behave correctly WITHOUT
running any backfill script:

  1. A newly-created MLB player-prop pick, published through the
     canonical `publish_upserted_picks` choke point, receives a
     ``canonical_player_id`` that joins to the Statcast/Stuff+ source
     collections and reaches Gold evidence AVAILABLE.

  2. A newly-created Tennis player pick with an event_time cutoff
     computes workload evidence from real `tennis_matches_history`
     without leaking any same-day or future match.

Also unit-tests:
  * Timestamp cascade (published_at → event_time → created_at → now).
  * Same-day match exclusion (temporal safety).
  * Existing AUTHORITATIVE ids are never overwritten.
  * Ambiguous MLB names refused.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from services.magic.gold_evidence import (
    Availability, build_tennis_workload, build_mlb_batter_matchup,
)
from services.mlb_producer_identity_stamp import (
    resolve_mlb_source_id, stamp_mlb_producer_identity, clear_cache,
    _existing_id_is_authoritative,
)


# ── Fake in-memory Mongo (matches the pattern used elsewhere) ────

class _Coll:
    def __init__(self, docs=None):
        self._docs = list(docs or [])
    async def find_one(self, q, projection=None):
        for d in self._docs:
            if self._match(d, q):
                return d
        return None
    def find(self, q=None, projection=None):
        q = q or {}
        matched = [d for d in self._docs if self._match(d, q)]
        class _C:
            def __init__(self, a): self.a = a; self.i = 0
            def limit(self, n): self.a = self.a[:n]; return self
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i >= len(self.a): raise StopAsyncIteration
                d = self.a[self.i]; self.i += 1; return d
        return _C(matched)
    async def count_documents(self, q):
        return sum(1 for d in self._docs if self._match(d, q))
    async def update_one(self, filt, update, upsert=False):
        for d in self._docs:
            if self._match(d, filt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                class _R:
                    matched_count = 1
                return _R()
        class _R:
            matched_count = 0
        return _R()

    @staticmethod
    def _match(d: dict, q: dict) -> bool:
        for k, v in q.items():
            if k == "$or":
                if not any(_Coll._match(d, sub) for sub in v):
                    return False
                continue
            dv = d.get(k)
            if isinstance(v, dict):
                # Support $in, $gte, $lt, $exists, $ne
                if "$in" in v and dv not in v["$in"]:
                    return False
                if "$gte" in v and not (dv is not None and dv >= v["$gte"]):
                    return False
                if "$lt"  in v and not (dv is not None and dv <  v["$lt"]):
                    return False
                if "$exists" in v:
                    exists = k in d and d[k] not in (None,)
                    if exists != bool(v["$exists"]):
                        return False
                if "$ne"  in v and dv == v["$ne"]:
                    return False
            else:
                if dv != v:
                    return False
        return True


class _DB:
    def __init__(self, colls):
        self._c = colls
    def __getattr__(self, n):
        return self._c.setdefault(n, _Coll())
    def __getitem__(self, n):
        return self._c.setdefault(n, _Coll())


# ── MLB Producer-Side Canonical Stamping ─────────────────────────

def test_mlb_stamp_authoritative_match_stamps_new_pick():
    """A brand-new MLB player-prop pick with a name matching exactly
    ONE row in the MLB source registry receives the MLB Stats API id
    at publication time — no backfill script."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "660271", "name": "shohei ohtani", "xslg": 0.62},
        ]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    pick = {"id": "new-pk-1", "sport": "MLB",
             "player_name": "Shohei Ohtani",
             "market": "Shohei Ohtani Over 1.5 Total Bases",
             "selection": "Over"}
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp.get("canonical_player_id") == "660271"
    assert stamp.get("canonical_player_id_source") == "mlb_source_producer_stamp"
    assert stamp.get("canonical_player_id_class") == "AUTHORITATIVE"


def test_mlb_stamp_never_overwrites_existing_authoritative_id():
    """An MLB pick that already carries a real (non-fallback) id
    MUST NOT be overwritten — even if a different match exists in
    the source registry."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "999999", "name": "shohei ohtani"},
        ]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    pick = {"id": "pk-2", "sport": "MLB",
             "player_name": "Shohei Ohtani",
             "canonical_player_id": "660271"}   # authoritative
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp == {}, f"must not overwrite; got {stamp}"


def test_mlb_stamp_overwrites_fallback_id():
    """A pick with a `fallback:<sha1>` provisional id CAN be upgraded
    to an authoritative MLB Stats id when the name resolves."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "660271", "name": "shohei ohtani"},
        ]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    pick = {"id": "pk-3", "sport": "MLB",
             "player_name": "Shohei Ohtani",
             "canonical_player_id": "fallback:abc123"}   # provisional
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp.get("canonical_player_id") == "660271"


def test_mlb_stamp_refuses_ambiguous_name():
    """Two source rows share the same normalized name → refuse to
    stamp (no fuzzy or heuristic guess)."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "1", "name": "jose ramirez"},
            {"player_id": "2", "name": "jose ramirez"},   # same normalized name
        ]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    pick = {"id": "pk-4", "sport": "MLB",
             "player_name": "Jose Ramirez"}
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp == {}


def test_mlb_stamp_unresolved_when_no_match():
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    pick = {"id": "pk-5", "sport": "MLB",
             "player_name": "Nobody Nowhere"}
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp == {}


def test_mlb_stamp_skips_non_mlb_sport():
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "660271", "name": "shohei ohtani"},
        ]),
    })
    pick = {"id": "pk-6", "sport": "Tennis",
             "player_name": "Shohei Ohtani"}
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp == {}


def test_mlb_stamp_ignores_name_parenthetical_via_normalize():
    """Producer often stamps ``player_name = 'Aaron Judge (NYY)'``.
    Normalize strips the team parenthetical before match."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "592450", "name": "aaron judge"},
        ]),
    })
    pick = {"id": "pk-7", "sport": "MLB",
             "player_name": "Aaron Judge (NYY)"}
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp.get("canonical_player_id") == "592450"


def test_mlb_existing_id_authority_helper():
    assert _existing_id_is_authoritative("660271") is True
    assert _existing_id_is_authoritative(660271) is True
    assert _existing_id_is_authoritative("fallback:abc") is False
    assert _existing_id_is_authoritative("unresolved:xyz") is False
    assert _existing_id_is_authoritative(None) is False
    assert _existing_id_is_authoritative("") is False


# ── Future-Pick Proof: MLB producer → Gold evidence ────────────

def test_future_mlb_pick_flows_producer_to_gold_evidence():
    """Producer creates a NEW MLB pick; publication_helpers-equivalent
    identity flow stamps canonical_player_id; then Magic Gold evidence
    consumes the stamped id and returns AVAILABLE — end-to-end without
    any backfill script."""
    clear_cache()
    db = _DB({
        "mlb_statcast_players": _Coll([
            {"player_id": "660271", "name": "shohei ohtani",
             "xslg": 0.62, "xba": 0.31, "barrel_pct": 22.5,
             "hard_hit": 55.0, "pa": 480,
             "updated_at": datetime.now(timezone.utc).isoformat()},
        ]),
        "mlb_stuff_plus_players": _Coll([]),
    })
    # Producer-created pick — no canonical_player_id yet.
    pick = {"id": "future-pk-1", "sport": "MLB",
             "player_name": "Shohei Ohtani",
             "market": "Shohei Ohtani Over 1.5 Total Bases",
             "selection": "Over", "line": 1.5, "side": "over",
             "canonical_event_id": "evt-abc"}
    # Producer-side stamp.
    stamp = asyncio.run(stamp_mlb_producer_identity(db, pick))
    assert stamp.get("canonical_player_id") == "660271"
    # Merge stamp onto the pick (as publication_helpers does).
    for k, v in stamp.items():
        pick[k] = v
    # Now Gold evidence must resolve AVAILABLE.
    ev = asyncio.run(build_mlb_batter_matchup(db, pick))
    assert ev.availability == Availability.AVAILABLE
    assert ev.value == 0.62
    assert ev.provenance.get("source") == "mlb_statcast_players"


# ── Tennis Workload — timestamp cascade + no leakage ───────────

def _tennis_pick(**kw):
    pick = {"id": "tp-1", "sport": "Tennis",
             "player_name": "Novak Djokovic",
             "market": "Novak Djokovic Moneyline",
             "selection": "Novak Djokovic",
             "line": None, "side": None}
    pick.update(kw)
    return pick


def test_tennis_workload_uses_event_time_when_published_at_missing():
    """Fallback #2 — event_time is used when published_at is absent."""
    from services.magic.gold_evidence import _pregame_cutoff_from_pick
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z")
    _, day = _pregame_cutoff_from_pick(pick)
    assert day == "2026-06-11"


def test_tennis_workload_published_at_beats_event_time():
    """Timestamp cascade — published_at takes precedence."""
    from services.magic.gold_evidence import _pregame_cutoff_from_pick
    pick = _tennis_pick(
        published_at="2026-06-01T12:00:00+00:00",
        event_time="2026-06-11T14:30:00Z",
    )
    _, day = _pregame_cutoff_from_pick(pick)
    assert day == "2026-06-01"


def test_tennis_workload_falls_back_to_created_at():
    from services.magic.gold_evidence import _pregame_cutoff_from_pick
    pick = _tennis_pick(created_at="2026-05-10T09:00:00Z")
    _, day = _pregame_cutoff_from_pick(pick)
    assert day == "2026-05-10"


def test_tennis_workload_excludes_same_day_and_future_matches():
    """A history match on the same day OR after the pregame cutoff
    must NOT leak into evidence."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-08", "winner_name": "Novak Djokovic",
             "loser_name": "Rival A", "surface": "Clay"},   # in window
            {"date": "2026-06-11", "winner_name": "Novak Djokovic",   # same day — excluded
             "loser_name": "Rival B", "surface": "Clay"},
            {"date": "2026-06-15", "winner_name": "Novak Djokovic",   # future — excluded
             "loser_name": "Rival C", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z")
    ev = asyncio.run(build_tennis_workload(db, pick))
    # 1 pre-cutoff match — 14d window
    assert ev.availability in (Availability.AVAILABLE,)
    assert ev.provenance["player_matches_14d"] == 1
    assert ev.provenance["player_matches_7d"]  == 1
    assert "match_date < cutoff_date" in ev.provenance["temporal_rule"]


def test_tennis_workload_no_leak_when_only_same_day_or_future():
    """If ALL history entries are same-day-or-later, evidence stays
    without any pre-cutoff matches."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-11", "winner_name": "Novak Djokovic",
             "loser_name": "Rival A", "surface": "Clay"},
            {"date": "2026-06-12", "winner_name": "Novak Djokovic",
             "loser_name": "Rival B", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z")
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.availability == Availability.PARTIAL
    assert ev.provenance["player_matches_30d"] == 0


def test_tennis_workload_independent_player_and_opponent_resolution():
    """Player and opponent must be resolved INDEPENDENTLY and counted
    from separate queries."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-08", "winner_name": "Novak Djokovic",
             "loser_name": "X", "surface": "Clay"},
            {"date": "2026-06-09", "winner_name": "Rafael Nadal",
             "loser_name": "Y", "surface": "Clay"},
            {"date": "2026-06-10", "winner_name": "Rafael Nadal",
             "loser_name": "Z", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z",
                          opponent_team="Rafael Nadal")
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.provenance["player_matches_14d"] == 1
    assert ev.provenance["opponent_matches_14d"] == 2


def test_tennis_workload_resolves_from_canonical_player_id_only():
    """Even when player_name is absent, canonical_player_id ``tp:...``
    resolves deterministically via title-case fallback."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-08", "winner_name": "Novak Djokovic",
             "loser_name": "X", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z",
                          player_name=None,
                          canonical_player_id="tp:novak djokovic")
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.provenance["player_matches_14d"] == 1


def test_tennis_workload_partial_when_dataset_scope_misses_player():
    """WTA players don't appear in tennis_matches_history — evidence
    must PARTIAL, not AVAILABLE-with-0 or crash."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-08", "winner_name": "Someone Else",
             "loser_name": "Other", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z",
                          player_name="Coco Gauff")
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.availability == Availability.PARTIAL
    assert ev.provenance["player_matches_30d"] == 0


def test_tennis_workload_unavailable_without_any_player_signal():
    """No name, no cpid → UNAVAILABLE, no leakage risk."""
    db = _DB({"tennis_matches_history": _Coll([])})
    pick = _tennis_pick(player_name=None, selection=None,
                          canonical_player_id=None)
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.availability == Availability.UNAVAILABLE


# ── Future-Pick Proof: Tennis producer → Gold evidence ─────────

def test_future_tennis_pick_flows_producer_to_workload_evidence():
    """A newly-created Tennis player pick with only event_time (no
    published_at yet) receives correct workload evidence relative
    to that cutoff — no backfill script required."""
    db = _DB({
        "tennis_matches_history": _Coll([
            {"date": "2026-06-01", "winner_name": "Novak Djokovic",
             "loser_name": "A", "surface": "Clay"},
            {"date": "2026-06-05", "winner_name": "Novak Djokovic",
             "loser_name": "B", "surface": "Clay"},
            {"date": "2026-06-08", "winner_name": "Novak Djokovic",
             "loser_name": "C", "surface": "Clay"},
            {"date": "2026-06-11", "winner_name": "Novak Djokovic",   # cutoff-day — excluded
             "loser_name": "D", "surface": "Clay"},
        ]),
    })
    pick = _tennis_pick(event_time="2026-06-11T14:30:00Z")
    ev = asyncio.run(build_tennis_workload(db, pick))
    assert ev.availability == Availability.AVAILABLE
    # 3 pre-cutoff matches (Jun-01, 05, 08), all within 14 days of Jun-11
    assert ev.provenance["player_matches_14d"] == 3
    # Same-day (Jun-11) excluded
    assert "match_date < cutoff_date" in ev.provenance["temporal_rule"]


# ── Locked constants (regression pin) ──────────────────────────

def test_magic_3d3_does_not_change_lock_or_calibration_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import (
        MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER,
    )
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
