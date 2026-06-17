"""Brain pipeline smoke tests.

Runs the whole pipeline on a synthetic in-memory pick slate to confirm
the seven layers don't crash + produce the expected fields. No Mongo
needed — uses a tiny stub that mimics the cursor protocol.
"""
from __future__ import annotations

import asyncio


class _StubCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._idx]
        self._idx += 1
        return d


class _StubPicksCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *args, **kwargs):
        return _StubCursor(self.docs)


class _StubDB:
    def __init__(self, docs):
        self.picks = _StubPicksCollection(docs)


def _settled_pick(sport: str, family_market: str, status: str, wp: float = 60.0,
                  units_risked: float = 1.0, units_profit: float = 0.91,
                  lock_score: float = 88.0) -> dict:
    return {
        "sport": sport,
        "status": status,
        "lock_score": lock_score,
        "win_probability": wp,
        "units_risked": units_risked,
        "units_profit": units_profit if status == "won" else -units_risked,
        "selection_v2": {"market": {"family": "moneyline"}},
        "market": family_market,
    }


def _live_pick(sport: str, market: str, lock_score: float, edge: float,
               factors: dict[str, float] | None = None,
               family: str = "moneyline") -> dict:
    return {
        "id": f"test-{sport}-{market}-{lock_score}",
        "sport": sport,
        "market": market,
        "lock_score": lock_score,
        "edge_percent": edge,
        "win_probability": 60.0,
        "book_odds": -110,
        "factors": factors or {"a": 0.7, "b": 0.6, "c": 0.5, "d": 0.65},
        "key_insights": ["x", "y", "z", "w"],
        "deep_dive_scores": {"edge": 75, "confidence": 70, "risk": 30},
        "selection_v2": {"market": {"family": family}},
    }


def test_brain_pipeline_smoke():
    from brain import process_brain

    settled = [_settled_pick("NBA", "Moneyline", "won" if i % 5 < 3 else "lost")
               for i in range(60)]
    settled += [_settled_pick("MLB", "Run Line", "lost" if i % 5 < 4 else "won",
                              units_profit=-1.0, lock_score=92.0)
                for i in range(60)]
    live = [
        _live_pick("NBA", "Lakers Moneyline", 95.0, 4.0),    # good
        _live_pick("MLB", "Yankees Run Line", 92.0, 8.0, family="spread"),  # should PASS (bad bucket)
        _live_pick("NBA", "Junk pick",  85.0, -1.0),         # should PASS (negative edge)
        _live_pick("NBA", "Elite anchor", 99.0, 5.0),        # elite override possible
    ]
    live[3]["elite_player"] = True
    db = _StubDB(settled)

    summary = asyncio.run(process_brain(live, db))
    assert summary["version"] == "1.0.0"
    assert summary["n_picks"] == 4
    assert summary["steps"]["calibration"]["calibrated_from_data"] + \
        summary["steps"]["calibration"]["calibrated_from_spec"] == 4
    assert summary["steps"]["candidates"]["ranked"] == 4
    # All four picks are flagged top_k (since < TOP_K_RANK=50)
    assert summary["steps"]["simulator"]["simulated"] == 4
    filter_step = summary["steps"]["filter"]
    assert filter_step["KEEP"] + filter_step["PASS"] == 4
    # Per-pick assertions
    for p in live:
        b = p["brain"]
        assert "confidence_calibrated" in b
        assert "confidence_band" in b
        assert 0.0 <= b["confidence_calibrated"] <= 1.0
        assert "candidate_rank" in b
        assert "candidate_score" in b
        assert b["candidate_components"].keys() == {
            "edge", "confidence", "roi", "data", "consistency"
        }
        assert b["verdict"] in ("KEEP", "PASS")
        if b["verdict"] == "PASS":
            assert "pass_reasons" in b
            assert p.get("no_bet") is True
            assert p.get("brain_pass") is True
    # Negative-edge junk pick MUST PASS
    junk = next(p for p in live if "Junk" in p["market"])
    assert junk["brain"]["verdict"] == "PASS"
    assert "edge<0.5%" in junk["brain"]["pass_reasons"]


def test_brain_handles_empty_slate():
    from brain import process_brain
    db = _StubDB([])
    summary = asyncio.run(process_brain([], db))
    assert summary["empty"] is True
