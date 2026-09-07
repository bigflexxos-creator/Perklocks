"""Item P0-B — Frozen Identity Priority.

Verifies SettlementService.settle_from_pick prefers frozen canonical
publication identity over legacy display strings.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_frozen_identity_priority_canonical_first():
    """When canonical_event_id is present, it MUST be preferred
    over provider_event_id, fanduel_event_id, event_id, and event."""
    from services.settlement_service import SettlementService

    captured = {}
    async def _fake_record(**kw):
        captured.update(kw)
        return {"status": "NEW_SETTLEMENT"}

    svc = SettlementService(db=None)
    svc.record = _fake_record  # type: ignore

    pick = {
        "id":                  "pk-1",
        "canonical_event_id":  "CANON_EVENT_X",  # ← must win
        "provider_event_id":   "PROV_EVENT_Y",
        "fanduel_event_id":    "FD_EVENT_Z",
        "event_id":            "OLD_EVENT_W",
        "event":               "Team A @ Team B",  # display fallback
        "market":              "Moneyline",
        "side":                "Home",
        "line":                None,
    }
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        svc.settle_from_pick(pick, result="won", source="test"),
    )
    assert captured["canonical_event_id"] == "CANON_EVENT_X"
    assert captured["expected_event_id"]  == "CANON_EVENT_X"


def test_frozen_identity_falls_through_gracefully():
    """When only legacy event_id is present, it becomes the id."""
    from services.settlement_service import SettlementService
    captured = {}
    async def _fake_record(**kw):
        captured.update(kw)
        return {"status": "NEW_SETTLEMENT"}
    svc = SettlementService(db=None)
    svc.record = _fake_record  # type: ignore
    pick = {
        "id":        "pk-2",
        "event_id":  "OLD_EVENT_W",
        "event":     "Team A @ Team B",
        "market":    "Moneyline",
        "side":      "Home",
    }
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        svc.settle_from_pick(pick, result="won", source="test"),
    )
    assert captured["canonical_event_id"] == "OLD_EVENT_W"


def test_frozen_identity_last_resort_display_string():
    """Absolute last resort is the display string ``event``."""
    from services.settlement_service import SettlementService
    captured = {}
    async def _fake_record(**kw):
        captured.update(kw)
        return {"status": "NEW_SETTLEMENT"}
    svc = SettlementService(db=None)
    svc.record = _fake_record  # type: ignore
    pick = {
        "id":       "pk-3",
        "event":    "Team A @ Team B",
        "market":   "Moneyline",
        "side":     "Home",
    }
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        svc.settle_from_pick(pick, result="won", source="test"),
    )
    assert captured["canonical_event_id"] == "Team A @ Team B"


if __name__ == "__main__":
    test_frozen_identity_priority_canonical_first()
    test_frozen_identity_falls_through_gracefully()
    test_frozen_identity_last_resort_display_string()
    print("OK — frozen identity priority verified.")
