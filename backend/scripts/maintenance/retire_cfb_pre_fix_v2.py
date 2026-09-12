"""One-time DB purge — CFB pre-fix legacy rows with LS>=95 lacking
factor-level current-engine provenance.

Companion to the read-time safety net in `routes.picks_routes.picks_today`.
The runtime safety net prevents these rows from surfacing on the API
response; this maintenance script also RETIRES them in the DB so:

  * `db.picks.count_documents(sport=CFB, lock_score >= 95)` reflects
    truth for analytics.
  * Any downstream consumer that bypasses the API (rare, but possible
    for internal notifiers or CSV exports) sees the same universe.

Marks — never deletes:
    off_board = True
    retirement_reason = "cfb_pre_fix_stale_v2_factor_level"
    retired_at = <utc-iso-8601>

Run:
    cd /app/backend && python -m scripts.maintenance.retire_cfb_pre_fix_v2
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from deps import db


_CURRENT_FACTOR_KEYS = (
    "Model Fair Prob", "__data_quality",
    "SP+ Margin Base", "Sportsbook Implied Prob",
)


def _has_current_engine_factors(p: dict) -> bool:
    factors = p.get("factors") or {}
    if not isinstance(factors, dict) or not factors:
        return False
    for fk in _CURRENT_FACTOR_KEYS:
        if factors.get(fk) not in (None, "", {}, []):
            return True
    return False


async def main() -> None:
    q = {"sport": "CFB",
         "lock_score": {"$gte": 95.0},
         "off_board": {"$ne": True}}
    cursor = db.picks.find(q, {"_id": 0})
    victims: list[dict] = []
    async for p in cursor:
        if not _has_current_engine_factors(p):
            victims.append(p)

    if not victims:
        print("No stale pre-fix CFB rows found (LS>=95 lacking factor "
              "provenance).  Nothing to retire.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    ids = [p.get("id") for p in victims if p.get("id")]
    result = await db.picks.update_many(
        {"id": {"$in": ids}},
        {"$set": {
            "off_board":         True,
            "retired_at":        now_iso,
            "retirement_reason": "cfb_pre_fix_stale_v2_factor_level",
        }},
    )

    print(f"Retired {result.modified_count} of {len(victims)} candidate "
          f"stale CFB rows.")
    for p in victims[:20]:
        print(f"  · {p.get('event')} · {p.get('market')} · "
              f"LS={p.get('lock_score')} · odds={p.get('book_odds')}")
    if len(victims) > 20:
        print(f"  ... ({len(victims) - 20} more)")


if __name__ == "__main__":
    asyncio.run(main())
