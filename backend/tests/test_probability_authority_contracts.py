"""PROBABILITY AUTHORITY — acceptance contracts (targeted, not a project)."""
import math
import re

from services import probability_authority as pa
from services.cfb_game_model import cfb_over_probability


def test_book_implied_seed_cannot_publish():
    c = pa.evaluate({"sport": "NBA", "market": "Lakers Moneyline", "win_probability": 61.0,
                     "probability_source": "book_implied_seed", "book_odds": -150})
    assert c.publication_eligible is False and c.fallback_reason == "BOOK_IMPLIED_SEED_NOT_PUBLISHABLE"
    from services.canonical_publication_boundary import RejectionReason
    assert RejectionReason.BOOK_IMPLIED_SEED.value == "BOOK_IMPLIED_SEED"


def test_invalid_or_lineless_probability_fails_closed():
    assert pa.evaluate({"sport": "NFL", "market": "X Moneyline", "win_probability": float("nan"),
                        "book_odds": -110}).publication_eligible is False
    assert pa.evaluate({"sport": "NFL", "market": "X Moneyline", "win_probability": 70.0,
                        "book_odds": None}).publication_eligible is False


def test_shadow_mode_never_alters_production_probability(monkeypatch):
    monkeypatch.setenv("PROBABILITY_AUTHORITY_MODE", "shadow")
    p = {"sport": "MLB", "market": "Player (LAD) Over 0.5 Hits", "player_name": "P", "win_probability": 74.3, "book_odds": -248}
    pa.stamp(p)
    assert p["win_probability"] == 74.3 and p["probability_contract"]["authority_mode"] == "shadow"
    assert p["raw_model_probability"] == 0.743 and "calibrator_version" in p


def test_point_in_time_guard_rejects_leaked_rows():
    # published after event start must NEVER enter the calibration dataset
    import asyncio

    class _Cur:
        def __init__(self, docs): self.d = docs
        def __aiter__(self): self.i = iter(self.d); return self
        async def __anext__(self):
            try: return next(self.i)
            except StopIteration: raise StopAsyncIteration

    class _DB:
        class picks:
            @staticmethod
            def find(*_a, **_k):
                return _Cur([
                    {"sport": "MLB", "market": "A (X) Over 0.5 Hits", "player_name": "A", "published_probability": 0.7,
                     "result": "won", "event_time": "2026-09-01T20:00:00Z", "published_at": "2026-09-01T21:00:00Z"},
                    {"sport": "MLB", "market": "B (X) Over 0.5 Hits", "player_name": "B", "published_probability": 0.7,
                     "result": "won", "event_time": "2026-09-01T20:00:00Z", "published_at": "2026-09-01T12:00:00Z"},
                ])
    data = asyncio.run(pa.build_calibration_dataset(_DB()))
    assert sum(len(v) for v in data.values()) == 1


def test_walk_forward_thin_sample_keeps_identity():
    rows = [(f"2026-01-{i%28+1:02d}", 0.7, 1) for i in range(50)]
    doc = pa.walk_forward_fit(rows)
    assert doc["champion"] == "identity" and doc["status"] == "NOT_ENOUGH_EVIDENCE"


def test_calibrators_are_monotonic():
    for method, params in (("platt", {"a": 1.3, "b": -0.2}), ("beta", {"a": 1.1, "c": 0.9, "b": 0.1})):
        vals = [pa.apply_calibrator(method, params, p / 100) for p in range(5, 96)]
        assert all(x <= y for x, y in zip(vals, vals[1:]))


def test_cfb_over_under_push_coherent():
    for line in (55.5, 55.0, 48.0):
        o = cfb_over_probability(58.0, line, True); u = cfb_over_probability(58.0, line, False)
        assert math.isclose(o + u, 1.0, abs_tol=1e-3)
    assert cfb_over_probability(58.0, 55.0, True) < cfb_over_probability(58.0, 55.5, True) + 0.05


def test_tennis_has_no_base_90_or_direct_99_assignment():
    src = open("/app/backend/tennis_engine.py").read()
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert not re.search(r"\bbase\s*=\s*90(\.0)?\b", code)
    assert not re.search(r"new_lock\s*=\s*99(\.0)?\b", code)
    assert "canonical_compute_lock_score" in src


def test_publication_snapshot_carries_contract_fields():
    from services.prediction_publication_service import PublishedPayload
    import dataclasses
    names = {f.name for f in dataclasses.fields(PublishedPayload)}
    assert "probability_contract" in names
