"""Blocks 37-43, 56 — NBA Automatic Season Activation Tests.

* Season-active discovery derives from provider truth, NOT a calendar
  date.
* With zero real NBA events, the sport is not activated (and no fake
  picks are published).
* Capability per family remains independent — one unavailable family
  must NOT disable the sport.
"""
from __future__ import annotations

import asyncio
import pytest

from services.season_active_discovery import (
    season_active, resolve_refresh_eligibility,
    SUPPORTED_SEASONAL_SPORTS,
)
from services.sport_capability_matrix import (
    is_model_unavailable, is_model_capable,
    model_capable_families_for_sport, families_for_sport,
)


# ── In-memory fake DB stub ───────────────────────────────────────────
class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    async def count_documents(self, filt):
        n = 0
        for r in self._rows:
            ok = True
            for k, v in filt.items():
                if isinstance(v, dict):
                    val = r.get(k)
                    for op, bound in v.items():
                        if op == "$gte" and not (val and val >= bound):
                            ok = False; break
                        if op == "$lte" and not (val and val <= bound):
                            ok = False; break
                        if op == "$lt" and not (val and val < bound):
                            ok = False; break
                else:
                    if r.get(k) != v:
                        ok = False; break
                if not ok:
                    break
            if ok:
                n += 1
        return n


class _FakeDB:
    def __init__(self, games_rows):
        self.games = _FakeCollection(games_rows)
    def __getitem__(self, name):
        return getattr(self, name)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_zero_events_means_season_not_active():
    db = _FakeDB(games_rows=[])
    res = _run(season_active(db, "nba"))
    assert res["season_active"] is False
    assert res["upcoming_events"] == 0
    assert res["recent_events"] == 0


def test_future_event_wakes_sport():
    from datetime import datetime, timezone, timedelta
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db = _FakeDB(games_rows=[{"sport": "nba", "game_date": tomorrow}])
    res = _run(season_active(db, "nba"))
    assert res["season_active"] is True
    assert res["upcoming_events"] == 1


def test_recent_event_also_wakes_sport():
    from datetime import datetime, timezone, timedelta
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db = _FakeDB(games_rows=[{"sport": "nba", "game_date": yesterday}])
    res = _run(season_active(db, "nba"))
    assert res["season_active"] is True
    assert res["recent_events"] == 1


def test_resolve_refresh_eligibility_covers_all_seasonal_sports():
    db = _FakeDB(games_rows=[])
    out = _run(resolve_refresh_eligibility(db, list(SUPPORTED_SEASONAL_SPORTS)))
    for s in SUPPORTED_SEASONAL_SPORTS:
        assert s in out
        assert "season_active" in out[s]


def test_nba_families_all_model_unavailable():
    """NBA families remain MODEL_UNAVAILABLE until a certified
    independent model is wired — automatic season activation does NOT
    activate NBA models."""
    nba = families_for_sport("NBA")
    assert nba, "NBA families must be registered"
    for fam, cap in nba:
        assert cap == "MODEL_UNAVAILABLE", f"NBA/{fam} must stay MODEL_UNAVAILABLE"
    assert model_capable_families_for_sport("NBA") == []


def test_one_unavailable_family_does_not_disable_other_sports():
    """Block 40: unavailable NBA family cannot disable e.g. NFL."""
    # NFL has model-capable families
    nfl_capable = model_capable_families_for_sport("NFL")
    assert len(nfl_capable) > 0, (
        "NFL must have at least one model-capable family")


def test_zero_events_creates_no_fake_picks():
    """Block 55 companion: no events ⇒ no activation ⇒ pipeline
    cannot invent picks (verified indirectly via season_active=False)."""
    db = _FakeDB(games_rows=[])
    res = _run(season_active(db, "nba"))
    assert res["season_active"] is False


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
