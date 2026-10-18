"""Perklocks — UNIVERSAL Historical Freshness Contract tests.

Covers the single shared module (``services.historical_freshness``)
that every HI adapter now consumes.  These tests prove the contract
is identical across every supported sport and that no sport-specific
hack is required.
"""
from __future__ import annotations

import datetime as _dt
import pytest
from unittest.mock import MagicMock

from services.historical_freshness import (
    derive_season, current_season, normalize_context,
    identity_candidates, sort_observations_newest_first,
    SEASON_CALENDARS,
)


# ──────────────────────────────────────────────────────────────────
# derive_season — deterministic, data-only, sport-aware
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sport, date_str, expected", [
    # NFL (Aug→Feb)
    ("nfl",    "2026-10-04",  2026),
    ("nfl",    "2026-01-25",  2025),  # Jan → prior season
    ("nfl",    "2026-02-08",  2025),  # Super Bowl → prior season
    ("nfl",    "2026-07-15",  2025),  # off-season → prior season
    ("nfl",    "2026-08-01",  2026),  # start of season Y
    # CFB (Aug→Jan)
    ("cfb",    "2026-10-04",  2026),
    ("cfb",    "2026-01-10",  2025),
    # NHL (Oct→Jun)
    ("nhl",    "2026-10-18",  2026),
    ("nhl",    "2026-04-15",  2025),  # April → prior
    ("nhl",    "2026-06-15",  2025),  # Finals → prior
    ("nhl",    "2026-09-15",  2025),  # preseason → prior
    # NBA (Oct→Jun) — same calendar as NHL
    ("nba",    "2026-05-01",  2025),
    ("nba",    "2026-10-20",  2026),
    # MLB (Mar→Nov)
    ("mlb",    "2026-08-10",  2026),
    ("mlb",    "2026-02-15",  2025),  # pre-ST → prior
    ("mlb",    "2026-03-20",  2026),
    # Soccer (Aug→May) EU default
    ("soccer", "2026-08-20",  2026),
    ("soccer", "2026-05-20",  2025),
    ("soccer", "2026-07-01",  2025),  # mid-off-season
    # Tennis / UFC / MMA — calendar year
    ("tennis", "2026-07-01",  2026),
    ("ufc",    "2026-02-01",  2026),
    ("mma",    "2026-12-31",  2026),
])
def test_derive_season_universal(sport, date_str, expected):
    assert derive_season(sport, date_str) == expected


def test_derive_season_trusts_stored_value_when_valid():
    # Even when the date would compute 2025, an explicit stored 2026 wins.
    assert derive_season("nfl", "2026-01-25", stored_season=2026) == 2026
    # But an obviously invalid stored value (string "foo") is ignored.
    assert derive_season("nfl", "2026-01-25", stored_season="foo") == 2025


def test_derive_season_honest_zero_without_date():
    assert derive_season("nfl", None) is None
    assert derive_season("nfl", "") is None
    assert derive_season("nfl", "garbage") is None


def test_derive_season_unknown_sport_is_honest_zero():
    assert derive_season("quidditch", "2026-10-04") is None


def test_current_season_matches_clock_and_calendar():
    now = _dt.datetime(2026, 10, 18, 12, 0, 0)
    assert current_season("nfl",    now_utc=now) == 2026
    assert current_season("nhl",    now_utc=now) == 2026
    assert current_season("mlb",    now_utc=now) == 2026
    now2 = _dt.datetime(2026, 2, 1)
    # Feb — NFL season 2025 is still active (playoffs)
    assert current_season("nfl", now_utc=now2) == 2025
    # MLB is off-season — season Y is prior calendar year
    assert current_season("mlb", now_utc=now2) == 2025


# ──────────────────────────────────────────────────────────────────
# normalize_context — one-shot output for every HI adapter
# ──────────────────────────────────────────────────────────────────

def test_normalize_context_fills_null_season_from_event_time():
    now = _dt.datetime(2026, 10, 18)
    row = {"event_time": "2026-09-28T00:20:00.000+00:00",
            "season": None, "source": "live_gamelog_nfl_v1"}
    ctx = normalize_context("nfl", row, now_utc=now)
    assert ctx["season"] == 2026
    assert ctx["event_date"] == "2026-09-28"
    assert ctx["is_current_season"] is True
    assert ctx["source"] == "live_gamelog_nfl_v1"


def test_normalize_context_prior_season_not_marked_current():
    now = _dt.datetime(2026, 10, 18)
    row = {"event_time": "2026-01-25T18:30:00", "season": 2025}
    ctx = normalize_context("nfl", row, now_utc=now)
    assert ctx["season"] == 2025
    assert ctx["is_current_season"] is False


def test_normalize_context_honest_zero_when_date_missing():
    """NHL historical rows with no event_time, no date, no season must
    surface HONEST ZERO — never a fabricated 2026."""
    row = {"event_time": None, "date": None, "season": None}
    ctx = normalize_context("nhl", row)
    assert ctx["season"] is None
    assert ctx["event_date"] is None
    assert ctx["is_current_season"] is False


def test_normalize_context_accepts_multiple_date_shapes():
    now = _dt.datetime(2026, 7, 1)
    # Tennis uses `date`.
    ctx1 = normalize_context("tennis", {"date": "2026-05-10"}, now_utc=now)
    assert ctx1["season"] == 2026
    # Soccer uses `match_date`.
    ctx2 = normalize_context("soccer", {"match_date": "2026-09-01"},
                              now_utc=_dt.datetime(2026, 10, 18))
    assert ctx2["season"] == 2026 and ctx2["is_current_season"] is True
    # MLB uses `game_date`.
    ctx3 = normalize_context("mlb", {"game_date": "2026-08-10"},
                              now_utc=_dt.datetime(2026, 10, 18))
    assert ctx3["season"] == 2026 and ctx3["is_current_season"] is True


# ──────────────────────────────────────────────────────────────────
# identity_candidates — universal multi-pipeline bridge
# ──────────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, docs): self._docs = list(docs)
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._docs): raise StopAsyncIteration
        d = self._docs[self._i]; self._i += 1; return d


class _FakeDB:
    def __init__(self, idents, distinct):
        self._idents = idents
        self._distinct = distinct
        self.player_identities = MagicMock()
        self.player_identities.find = lambda *a, **kw: _FakeCursor(self._idents)
        async def _dist(field, filter_):
            return list(self._distinct)
        self.player_game_actuals = MagicMock()
        self.player_game_actuals.distinct = _dist


@pytest.mark.asyncio
async def test_identity_candidates_unions_both_pipelines():
    db = _FakeDB(
        idents=[{"canonical_player_id": "cpid_x",
                 "provider_ids": {"espn": "4242557"}}],
        distinct=["00-0036244", "4242557"],
    )
    ids = await identity_candidates(db, "nfl", "00-0036244", "Colby Parkinson")
    assert "00-0036244" in ids
    assert "4242557"   in ids
    assert "cpid_x"    in ids


@pytest.mark.asyncio
async def test_identity_candidates_handles_empty_identity_doc_gracefully():
    db = _FakeDB(idents=[], distinct=["single_id"])
    ids = await identity_candidates(db, "nhl", "nhl_123", "Connor McDavid")
    # Must at least contain the pick's own id even when name scan empty.
    assert "nhl_123" in ids
    # And must add rows discovered via actuals-distinct.
    assert "single_id" in ids


@pytest.mark.asyncio
async def test_identity_candidates_universal_across_sports():
    """The same function is correct for every supported sport."""
    for sport in ("nfl", "nhl", "nba", "mlb", "cfb", "soccer", "tennis"):
        db = _FakeDB(idents=[], distinct=[])
        ids = await identity_candidates(db, sport, "stamped_id", "Some Name")
        assert "stamped_id" in ids, (
            f"identity_candidates must always include the stamped id "
            f"for sport={sport}"
        )


@pytest.mark.asyncio
async def test_identity_candidates_soft_fail_returns_seed():
    class _Exploding:
        def find(self, *a, **kw):
            raise RuntimeError("db exploded")
    class _DB:
        player_identities = _Exploding()
        player_game_actuals = _Exploding()
    ids = await identity_candidates(_DB(), "nfl", "the_id", "A Name")
    assert ids == {"the_id"}, (
        "On DB failure the resolver must still return the seed id — "
        "never dropping the single-id query that was already working."
    )


# ──────────────────────────────────────────────────────────────────
# sort_observations_newest_first — deterministic ordering contract
# ──────────────────────────────────────────────────────────────────

def test_sort_newest_first_across_date_fields():
    docs = [
        {"event_time": "2026-01-25T00:00:00"},  # middle
        {"event_time": "2026-09-28T00:20:00.000+00:00"},  # newest
        {"date": "2025-12-01"},                 # oldest
    ]
    out = sort_observations_newest_first(docs)
    assert out[0].get("event_time", "").startswith("2026-09")
    assert out[1].get("event_time", "").startswith("2026-01")
    assert out[2].get("date", "").startswith("2025-12")


def test_sort_newest_first_rows_without_date_sink_to_end():
    docs = [
        {"event_time": "2026-09-28"},
        {"event_time": None, "date": None},
        {"event_time": "2026-01-25"},
    ]
    out = sort_observations_newest_first(docs)
    # First two are the dated rows newest-first; the dateless row sinks.
    assert out[0]["event_time"] == "2026-09-28"
    assert out[1]["event_time"] == "2026-01-25"
    assert out[2]["event_time"] is None


# ──────────────────────────────────────────────────────────────────
# Universal invariants — one calendar, no sport exceptions
# ──────────────────────────────────────────────────────────────────

def test_every_supported_sport_has_a_calendar():
    required = {"nfl", "nhl", "nba", "cfb", "mlb",
                "soccer", "tennis", "ufc", "mma"}
    missing = required - set(SEASON_CALENDARS)
    assert not missing, f"Universal contract missing calendar(s): {missing}"


def test_calendar_start_months_are_valid_1_through_12():
    for sport, cfg in SEASON_CALENDARS.items():
        sm = cfg.get("start_month")
        assert isinstance(sm, int) and 1 <= sm <= 12, (
            f"Invalid start_month for {sport}: {sm}"
        )


def test_current_season_year_sane_for_all_sports():
    now = _dt.datetime(2026, 10, 18)
    for sport in ("nfl", "nhl", "nba", "cfb", "mlb",
                  "soccer", "tennis", "ufc", "mma"):
        y = current_season(sport, now_utc=now)
        assert y is not None and 2024 <= y <= 2028, (
            f"current_season sanity fail for {sport}: {y}"
        )
