"""KBO Settlement Engine — RETIRED (P0.2b, 2026-08-13).

KBO was removed from the product on 2026-07-04 (sports_engine.py blocks
KBO pick generation, analytics dashboard excludes it by league regex).
No picks are generated, no callers remain, and the settlement_engine
top-level loop no longer invokes this module.

This file is intentionally reduced to a no-op stub.  The former
Naver-Sports scraping code is preserved in git history; do NOT restore
it here without also:

  1. Reviving KBO pick generation in ``sports_engine.py``.
  2. Removing this stub and routing the writer through
     ``services.settlement_service.SettlementService.settle_from_pick``
     with the full P0.2a contract (authoritative_event_final=True,
     canonical identity fields, and the wrong-identity fail-closed
     guards).  Direct ``db.picks.update`` is FORBIDDEN.

The static-guard test (``test_scan_backend_for_direct_status_writers``)
should no longer include ``kbo_settlement.py`` on its allowlist because
the file no longer contains a direct settlement write.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("lockscore.kbo_settle")


async def settle_kbo_picks(db) -> dict:  # noqa: D401 — kept for import
    """Retired.  Returns an empty settlement summary."""
    logger.info("KBO settler invoked but is RETIRED — no-op returned.")
    return {
        "settled":   0,
        "won":       0,
        "lost":      0,
        "push":      0,
        "skipped":   0,
        "no_match":  0,
        "retired":   True,
    }
