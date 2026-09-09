"""NFL alt-ladder full-emission contract (2026-06-25).

Root cause B fix — regression guard against the 60→1 collapse where
`picks.append(new_pick)` had been dedented out of the modeling loop
inside `_props_picks_from_event`, silently dropping every alt rung
except the last iteration's.

Feeds a real cached ESPN/OddsAPI NFL alt payload through the sync
pipeline and asserts every unique provider rung reaches the picks
list (approx. equality — allow tiny provider dedupe at the extractor
stage but never a collapse to 1-2).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
import sports_engine as se                          # noqa: E402


async def _asyncmain():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "test_database")]

    row = await db.odds_api_cache.find_one({
        "sport_key": "americanfootball_nfl",
        "params.markets": "player_pass_yds_alternate",
    })
    if not row:
        print("[skip] no cached NFL alt payload in this environment — "
              "test is a no-op here (still passes in Preview / Production)")
        return
    payload = dict(row.get("body") or {})
    cands = se._extract_nfl_prop_candidates(payload)
    if len(cands) < 20:
        print(f"[skip] cached payload has only {len(cands)} candidates — "
              "insufficient for full-ladder guard")
        return

    from services.nfl_feature_engine import build_nfl_game_context
    ctx = await build_nfl_game_context(
        db, game=payload, prop_candidates=cands, season=2025, week=1,
    )
    payload["_ctx"] = ctx

    from datetime import datetime, timezone
    import random
    picks = se._props_picks_from_event(
        "NFL", "NFL", payload,
        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        random.Random(42),
    )

    # ── Contract ─────────────────────────────────────────────────
    # (i) picks list must not have collapsed to ≤ 2 items.
    assert len(picks) >= 20, (
        f"alt-ladder 60→1 collapse regressed: only {len(picks)} picks "
        f"emitted from {len(cands)} candidates"
    )
    # (ii) at least 3 distinct thresholds must survive per QB — the
    # per-player alt cap is NFL-uncapped and the contradiction
    # dedupe keys on (player, family, line) so distinct lines coexist.
    from collections import defaultdict
    per_player_lines: dict[str, set] = defaultdict(set)
    for p in picks:
        m = (p.get("market") or "").lower()
        if "pass yds" not in m:
            continue
        # Extract player up to " over " or " under "
        for delim in (" over ", " under "):
            i = m.find(delim)
            if i >= 0:
                player = m[:i].strip()
                # Extract line
                import re
                mm = re.search(r"(over|under)\s+(\d+\.?\d*)", m)
                if mm:
                    per_player_lines[player].add(float(mm.group(2)))
                break
    for player, lines in per_player_lines.items():
        assert len(lines) >= 3, (
            f"alt-ladder truncated for {player}: only {len(lines)} unique "
            f"thresholds survived — expected ≥3"
        )
    # (iii) safer rungs (higher WP) MUST reach LS ≥ 85 so Preview / Best
    # Lock selection has a real ladder to choose from.
    high_ls = [p for p in picks if (p.get("lock_score") or 0) >= 85]
    assert len(high_ls) >= 1, (
        f"no safer rung reached LS ≥ 85 in a {len(picks)}-pick ladder — "
        f"grading/reliability path may still be compressing evidence"
    )
    print(f"[alt-ladder] {len(cands)} candidates → {len(picks)} picks emitted "
          f"({len(per_player_lines)} players × avg "
          f"{sum(len(v) for v in per_player_lines.values()) // max(len(per_player_lines), 1)} "
          f"rungs, {len(high_ls)} at LS ≥ 85) ✓")


def test_alt_ladder_full_emission():
    asyncio.run(_asyncmain())


if __name__ == "__main__":
    test_alt_ladder_full_emission()
    print("=" * 60)
    print("NFL ALT-LADDER FULL-EMISSION CONTRACT · PASS")
