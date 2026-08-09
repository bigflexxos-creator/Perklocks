"""P0-1 (2026-08-11) — Prediction Contract Repair.

Canonical units — exactly one representation per scoring dimension:

    Snapshot field                Internal unit
    ────────────────────────────  ─────────────────────
    published_probability         float in [0.0, 1.0]  (canonical fraction)
    published_edge                Optional[float]      (percentage-point delta or None)
    published_lock_score          float in [0.0, 100.0]
    published_confidence          str  label
    published_confidence_score    Optional[float]

    Legacy pick-doc alias         Legacy unit          (dual-write output)
    ────────────────────────────  ─────────────────────
    win_probability               float in [0.0, 100.0]  (0-100 percentage)
    edge_percent                  Optional[float]        (None preserved)
    confidence                    str  label

Covers the 5 confirmed bugs:
  1. 68.2 must NOT round-trip as 0.682 on the legacy `win_probability`.
  2. `edge_percent=None` must NOT be silently coerced to 0.0.
  3. Confidence label ("Very High") must NOT be coerced to 0.0.
  4. 0% / 50% / 68.2% / 85% / 99% probabilities must all round-trip.
  5. Every confidence label ("Very High"/"High"/"Medium"/"Low"/"Very Low"/"Pass")
     must survive publication and dual-write.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import pathlib
import pytest
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TID = "p01probfix_"


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(coro):
    return asyncio.run(coro)


def _base_pick(**overrides) -> dict:
    d = {
        "id": _TID + uuid.uuid4().hex[:16],
        "sport": "MLB",
        "league": "MLB",
        "event": "Alpha vs Bravo",
        "event_time": (datetime.now(timezone.utc)
                       + timedelta(hours=6)).isoformat(),
        "market": "Alpha Moneyline",
        "selection": "Alpha",
        "win_probability": 62.0,     # 0-100 percentage
        "edge_percent": 3.5,          # percentage-point delta
        "lock_score": 88.0,
        "grade": "Strong Lock",
        "confidence": "Very High",   # label string (canonical form)
        "line": None,
        "book_odds": -140,
        "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "no_bet": False,
    }
    d.update(overrides)
    return d


# ── 1. Canonical unit — normalize helper ────────────────────────────
def test_normalize_probability_percent_to_fraction():
    from services.prediction_publication_service import (
        _normalize_probability_at_publish,
    )
    assert _normalize_probability_at_publish(68.2) == pytest.approx(0.682)
    assert _normalize_probability_at_publish(0.0) == 0.0
    assert _normalize_probability_at_publish(50.0) == 0.5
    assert _normalize_probability_at_publish(85.0) == 0.85
    assert _normalize_probability_at_publish(99.0) == pytest.approx(0.99)


def test_normalize_probability_fraction_passthrough():
    from services.prediction_publication_service import (
        _normalize_probability_at_publish,
    )
    assert _normalize_probability_at_publish(0.5) == 0.5
    assert _normalize_probability_at_publish(0.682) == pytest.approx(0.682)
    assert _normalize_probability_at_publish(0.99) == pytest.approx(0.99)


def test_normalize_probability_none_and_bad_values():
    from services.prediction_publication_service import (
        _normalize_probability_at_publish,
    )
    assert _normalize_probability_at_publish(None) == 0.0
    assert _normalize_probability_at_publish(-5.0) == 0.0
    assert _normalize_probability_at_publish(200.0) == 1.0


# ── 2. PublishedPayload types express the new contract ──────────────
def test_published_payload_confidence_is_str():
    """`published_confidence` must be typed str.  The previous
    numeric type silently coerced "Very High" → 0.0 on every publish."""
    from services.prediction_publication_service import PublishedPayload
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(PublishedPayload)}
    assert fields["published_confidence"].type == "str", (
        "published_confidence must be typed as str (label)")


def test_published_payload_edge_is_optional_float():
    """`published_edge` must allow None so a pick without a book
    line survives publication as unknown, not as 0% edge."""
    from services.prediction_publication_service import PublishedPayload
    import dataclasses, typing
    fields = {f.name: f for f in dataclasses.fields(PublishedPayload)}
    # Type stringification varies across Python versions — assert the
    # source string contains Optional (or Union[..., None]).
    t = fields["published_edge"].type
    assert "Optional[float]" in str(t) or "None" in str(t), t


# ── 3. Publish + read-back — every canonical value round-trips ──────
@pytest.mark.parametrize("wp_pct", [0.0, 50.0, 68.2, 85.0, 99.0])
def test_probability_roundtrips_percentage_through_publication(wp_pct):
    """Publish a pick with 0/50/68.2/85/99 percent win_probability,
    then read back the picks doc via `hydrate()` and confirm the
    legacy alias is STILL the same 0-100 percentage (never 0.682%)."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        from services.published_prediction_reader import hydrate
        db = _db()
        pub = PredictionPublicationService(db)
        pick = _base_pick(win_probability=wp_pct)
        await db.picks.delete_many({"id": pick["id"]})
        await db.prediction_snapshots.delete_many(
            {"prediction_id": pick["id"]})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, publication_source="canonical_pipeline",
                              dual_write=True)
            # Read the raw picks doc after dual-write.
            after = await db.picks.find_one({"id": pick["id"]}, {"_id": 0})
            # Legacy alias MUST be the percentage input, not a fraction.
            assert abs(float(after["win_probability"]) - wp_pct) < 1e-6, (
                f"win_probability was mutated: expected {wp_pct}, got "
                f"{after['win_probability']!r} — this is the 0.682% bug"
            )
            # Snapshot value is the canonical fraction.
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pick["id"]}, {"_id": 0})
            assert abs(float(snap["published_probability"])
                       - (wp_pct / 100.0)) < 1e-6
            # hydrate() on a snapshot-backed row produces the legacy
            # percentage — never leaks the fraction.
            hydrated = hydrate(after)
            assert abs(float(hydrated["win_probability"]) - wp_pct) < 1e-6
        finally:
            await db.picks.delete_many({"id": pick["id"]})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pick["id"]})
    _run(go())


# ── 4. Edge=None preserved through publication ──────────────────────
def test_edge_none_survives_publication_as_none():
    """A pick without a real book line (edge_percent=None) MUST NOT
    become 0.0 after canonical publication."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        db = _db()
        pub = PredictionPublicationService(db)
        pick = _base_pick(edge_percent=None, book_odds=None)
        await db.picks.delete_many({"id": pick["id"]})
        await db.prediction_snapshots.delete_many(
            {"prediction_id": pick["id"]})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, publication_source="canonical_pipeline",
                              dual_write=True)
            after = await db.picks.find_one({"id": pick["id"]}, {"_id": 0})
            assert after["edge_percent"] is None, (
                f"edge_percent lost None → became {after['edge_percent']!r}"
            )
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pick["id"]}, {"_id": 0})
            assert snap["published_edge"] is None
        finally:
            await db.picks.delete_many({"id": pick["id"]})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pick["id"]})
    _run(go())


def test_edge_zero_and_negative_survive_verbatim():
    """A real 0% edge (very rare but valid) and a real negative edge
    (book has us beat) must both survive verbatim.  Only None means
    "no line"."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        db = _db()
        pub = PredictionPublicationService(db)
        for edge_in in (0.0, -3.5, 2.4):
            pick = _base_pick(edge_percent=edge_in)
            await db.picks.delete_many({"id": pick["id"]})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pick["id"]})
            await db.picks.insert_one(pick)
            try:
                await pub.publish(pick, dual_write=True)
                after = await db.picks.find_one({"id": pick["id"]}, {"_id": 0})
                assert after["edge_percent"] == pytest.approx(edge_in)
            finally:
                await db.picks.delete_many({"id": pick["id"]})
                await db.prediction_snapshots.delete_many(
                    {"prediction_id": pick["id"]})
    _run(go())


# ── 5. Confidence labels survive publication ────────────────────────
@pytest.mark.parametrize("label", [
    "Very High", "High", "Medium", "Low", "Very Low", "Pass",
])
def test_confidence_label_roundtrips(label):
    """Every valid confidence label must arrive at the picks doc as
    the same string after canonical publication."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        db = _db()
        pub = PredictionPublicationService(db)
        pick = _base_pick(confidence=label)
        await db.picks.delete_many({"id": pick["id"]})
        await db.prediction_snapshots.delete_many(
            {"prediction_id": pick["id"]})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, dual_write=True)
            after = await db.picks.find_one({"id": pick["id"]}, {"_id": 0})
            assert after["confidence"] == label, (
                f"confidence label lost: expected {label!r}, "
                f"got {after['confidence']!r} — this is the 0.0-confidence bug"
            )
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pick["id"]}, {"_id": 0})
            assert snap["published_confidence"] == label
        finally:
            await db.picks.delete_many({"id": pick["id"]})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pick["id"]})
    _run(go())


def test_confidence_missing_falls_back_to_legacy_unknown():
    """When the source pick genuinely has no confidence value we
    must NOT coerce to 0.0.  Contract emits `legacy_unknown`."""
    async def go():
        from services.prediction_publication_service import (
            PredictionPublicationService, LEGACY_UNKNOWN,
        )
        db = _db()
        pub = PredictionPublicationService(db)
        pick = _base_pick()
        pick.pop("confidence", None)
        await db.picks.delete_many({"id": pick["id"]})
        await db.prediction_snapshots.delete_many(
            {"prediction_id": pick["id"]})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, dual_write=True)
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pick["id"]}, {"_id": 0})
            assert snap["published_confidence"] == LEGACY_UNKNOWN
            assert snap["published_confidence"] != 0.0
        finally:
            await db.picks.delete_many({"id": pick["id"]})
            await db.prediction_snapshots.delete_many(
                {"prediction_id": pick["id"]})
    _run(go())


# ── 6. hydrate() converts snapshot fraction → legacy percentage ─────
def test_hydrate_maps_fraction_to_percentage():
    from services.published_prediction_reader import hydrate
    p = {
        "id": "x1",
        "published_lock_score": 90.0,
        "published_probability": 0.682,
        "published_edge": 3.5,
        "published_grade": "Strong Lock",
        "published_confidence": "Very High",
        "published_odds": -140,
        "published_line": 1.5,
        "published_reasoning": "test",
    }
    out = hydrate(p)
    assert abs(out["win_probability"] - 68.2) < 1e-6
    assert out["edge_percent"] == 3.5
    assert out["confidence"] == "Very High"


def test_hydrate_preserves_edge_none_when_snapshot_edge_none():
    from services.published_prediction_reader import hydrate
    p = {
        "id": "x2",
        "published_lock_score": 90.0,
        "published_probability": 0.85,
        "published_edge": None,
        "published_grade": "Strong Lock",
        "published_confidence": "Very High",
        "published_odds": None,
        "published_line": None,
        "published_reasoning": "",
    }
    out = hydrate(p)
    assert out["edge_percent"] is None
    assert abs(out["win_probability"] - 85.0) < 1e-6


def test_hydrate_defends_against_leaked_fraction_on_legacy_row():
    """Legacy row (no `published_lock_score`) that was mutated by
    the OLD buggy dual-write and now has `win_probability=0.682` —
    hydrate must recognise the fractional leak and normalise it back
    to the frontend-visible 68.2 percentage."""
    from services.published_prediction_reader import hydrate
    p = {"id": "x3", "win_probability": 0.682}  # no published_* → legacy path
    out = hydrate(p)
    assert abs(out["win_probability"] - 68.2) < 1e-6


# ── 7. Consumers using legacy units documented ──────────────────────
def test_publication_contract_is_documented_at_module_level():
    """The canonical/legacy contract must be documented in-source so
    future readers can find it without spelunking."""
    src = (_BACKEND_ROOT / "services"
           / "prediction_publication_service.py").read_text()
    assert "Canonical prediction units" in src
    assert "0-1 fraction" in src or "[0.0, 1.0]" in src
    assert "0-100 percentage" in src or "0-100" in src
    reader = (_BACKEND_ROOT / "services"
              / "published_prediction_reader.py").read_text()
    assert "canonical → legacy unit conversion" in reader.lower() or (
        "canonical" in reader and "legacy" in reader
    )
