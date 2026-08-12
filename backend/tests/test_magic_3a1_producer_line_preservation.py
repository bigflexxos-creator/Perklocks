"""MAGIC 3A.1 — Producer Line Preservation Closure tests.

Covers
──────
1. `attach_line_fields` idempotency.
2. Structured-source preservation (never overwritten).
3. Parse-fallback provenance tag.
4. Unrecoverable rows stay None/None — NEVER 0 or 0.0.
5. Side is deterministically parseable — NEVER inferred from
   model direction.
6. Dedup key includes the line (Over 0.5 ≠ Over 1.5).
7. PublishedPayload carries + emits published_side + published_line_source.
8. Dual-write includes side + line_source in legacy alias.
"""
import pytest

from services.magic.line_wire import (
    attach_line_fields, attach_line_fields_batch,
    dedupe_key_with_line, _STRUCTURED_SOURCES,
)
from services.prediction_publication_service import (
    PublishedPayload, PUBLISHED_FIELDS, LEGACY_ALIAS_MAP,
)


# ── attach_line_fields ────────────────────────────────────────────

def test_attach_line_fields_parses_over_pattern():
    c = {"market": "Mikal Bridges Over 1.5 Assists",
         "selection": "Mikal Bridges"}
    attach_line_fields(c)
    assert c["line"] == 1.5
    assert c["side"] == "over"
    assert c["line_source"] == "selection_parse_fallback"


def test_attach_line_fields_parses_spread():
    c = {"market": "Miami Marlins +1.5 Spread",
         "selection": "Miami Marlins"}
    attach_line_fields(c)
    assert c["line"] == 1.5
    assert c["side"] == "positive_spread"
    assert c["line_source"] == "selection_parse_fallback"


def test_attach_line_fields_moneyline_yields_none():
    c = {"market": "Detroit Tigers Moneyline",
         "selection": "Detroit Tigers"}
    attach_line_fields(c)
    assert c["line"] is None
    assert c["line_source"] is None
    # Side is None too — no over/under/spread token.
    assert c["side"] is None
    # Explicitly not 0 or 0.0.
    assert c["line"] != 0
    assert c["line"] != 0.0


def test_attach_line_fields_anytime_stays_none():
    c = {"market": "Harry Kane Anytime Goal Scorer",
         "selection": "Harry Kane"}
    attach_line_fields(c)
    # NEVER synthesize 0.5 — anytime carries no explicit threshold.
    assert c["line"] is None
    assert c["line_source"] is None


def test_attach_line_fields_preserves_structured_source():
    c = {
        "market": "Over 2.5",
        "selection": "Over",
        "line": 3.5,                           # structured value
        "line_source": "sportsbook_structured",
        "side": "over",
    }
    attach_line_fields(c)
    # Structured value never overwritten by parse fallback (which
    # would have produced 2.5 from the market string).
    assert c["line"] == 3.5
    assert c["line_source"] == "sportsbook_structured"
    assert c["side"] == "over"


def test_attach_line_fields_upgrades_book_odds_to_structured():
    # Producer forgot to tag source but did carry a book price
    # and a numeric line — upgrade to sportsbook_structured.
    c = {
        "market": "Nikola Jokic Over 25.5 Points",
        "selection": "Over",
        "line": 25.5,
        "book_odds": -110,
        "odds_source": "draftkings",
    }
    attach_line_fields(c)
    assert c["line"] == 25.5
    assert c["line_source"] == "sportsbook_structured"


def test_attach_line_fields_idempotent():
    c = {"market": "Over 1.5", "selection": "Over"}
    attach_line_fields(c)
    snap = dict(c)
    for _ in range(5):
        attach_line_fields(c)
    assert c == snap


def test_attach_line_fields_never_infers_side_from_model_direction():
    c = {"market": "Harry Kane Anytime Goal Scorer",
         "selection": "Harry Kane",
         "model_probability": 0.85,        # STRONG bull direction —
         "win_probability": 0.85}          # STILL must produce side=None.
    attach_line_fields(c)
    assert c["side"] is None


def test_attach_line_fields_batch():
    cands = [
        {"market": "Over 1.5", "selection": "Over"},
        {"market": "Under 2.5", "selection": "Under"},
        {"market": "Team Moneyline", "selection": "Team"},
    ]
    attach_line_fields_batch(cands)
    assert cands[0]["line"] == 1.5 and cands[0]["side"] == "over"
    assert cands[1]["line"] == 2.5 and cands[1]["side"] == "under"
    assert cands[2]["line"] is None and cands[2]["side"] is None


# ── Dedup safety ──────────────────────────────────────────────────

def test_dedup_key_distinguishes_thresholds():
    c1 = {"event": "gm-1", "player_name": "X",
          "market": "Over 0.5", "side": "over", "line": 0.5,
          "line_source": "selection_parse_fallback"}
    c2 = dict(c1); c2["market"] = "Over 1.5"; c2["line"] = 1.5
    k1, k2 = dedupe_key_with_line(c1), dedupe_key_with_line(c2)
    assert k1 != k2, "picks differing only by line must NOT collide"


def test_dedup_key_collapses_true_duplicates():
    c1 = {"event": "gm-1", "player_name": "X",
          "market": "Over 0.5", "side": "over", "line": 0.5,
          "line_source": "selection_parse_fallback"}
    c2 = dict(c1)
    assert dedupe_key_with_line(c1) == dedupe_key_with_line(c2)


# ── Publication contract carries the fields ───────────────────────

def test_published_fields_include_side_and_line_source():
    assert "published_side" in PUBLISHED_FIELDS
    assert "published_line_source" in PUBLISHED_FIELDS


def test_legacy_alias_map_includes_side_and_line_source():
    assert LEGACY_ALIAS_MAP.get("side") == "published_side"
    assert LEGACY_ALIAS_MAP.get("line_source") == "published_line_source"


def test_published_payload_snapshot_dict_carries_fields():
    from datetime import datetime, timezone
    p = PublishedPayload(
        prediction_id="p1", pick_id="p1", snapshot_version=1,
        board_version="v", published_probability=0.6,
        published_edge=None, published_lock_score=70.0,
        published_grade="Pass", published_confidence="Medium",
        published_confidence_score=None, published_reasoning={},
        published_line=1.5, published_odds=-110,
        model_version="v", fusion_version="v", scoring_version="v",
        calibration_version="v", validator_version="v",
        simulation_version="v", feature_snapshot_version="v",
        publication_source="canonical_pipeline",
        published_side="over",
        published_line_source="selection_parse_fallback",
    )
    d = p.to_snapshot_dict(
        payload_hash="h", idempotency_key="k",
        published_at=datetime.now(timezone.utc),
    )
    assert d["published_side"] == "over"
    assert d["published_line_source"] == "selection_parse_fallback"
    assert d["published_line"] == 1.5


# ── Exact-threshold reachability proof ────────────────────────────

def test_magic_exact_threshold_consumes_first_class_line():
    """The Magic 2 exact-threshold adapter reads the first-class `line`
    from the pick — proving Magic 3A.1's producer field is reachable
    end-to-end."""
    from services.magic.adapters.playerprop import build_playerprop_evidence
    import asyncio, types

    class _Cursor:
        def __init__(self, data): self._d = data
        def __aiter__(self): self._i = iter(self._d); return self
        async def __anext__(self):
            try: return next(self._i)
            except StopIteration: raise StopAsyncIteration
        def sort(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
    class _Coll:
        def find(self, *a, **k): return _Cursor([])
    class _DB:
        def __getattr__(self, _): return _Coll()

    pick = {
        "id": "pk1", "sport": "MLB",
        "market": "Aaron Judge Over 1.5 Total Bases",
        "selection": "Over", "player_name": "Aaron Judge",
        "line": 1.5, "side": "over",
        "line_source": "sportsbook_structured",
        "canonical_player_id": "aj-1",
        "identity_class": "MAPPED",
        "win_probability": 0.58,
        "model_probability": 0.58,
        "book_odds": -110,
    }
    out = asyncio.run(
        build_playerprop_evidence(_DB(), pick, sport="MLB"))
    # The adapter must have carried 1.5 through — never 0.5, never None.
    assert out.line == 1.5
