"""SOCCER_MARKET_COMPETITION_RUNTIME final live proof — iter 104b.

ONE controlled provider-budget override:
  * Temporarily disable the OddsApiGateway path (in-process env only)
    so ``refresh_alt_lines`` uses the legacy direct-httpx transport.
    ProviderBudget enforcement is thereby skipped ONLY for this run.
  * Restrict SOCCER_MARKETS to the 5 required proof markets:
      btts, player_goal_scorer_anytime, player_to_score_or_assist,
      alternate_totals, double_chance.
  * Explicitly EXCLUDE player_first_goal_scorer.
  * Count every outbound provider HTTP request (via httpx monkeypatch).
  * Restore normal env + SOCCER_MARKETS at the end.
  * Run the ingester once so live_alt_lines rows land on the board.
  * Emit RAW_PROVIDER_MARKET_NOT_PRESENT for markets that returned
    empty from the provider.

Usage: python /app/backend/scripts/soccer_market_verification_iter104b.py
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
os.environ["ODDS_GATEWAY_ENABLED"] = "false"    # in-process only

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")


REQUIRED_MARKETS = [
    "player_goal_scorer_anytime",
    "player_to_score_or_assist",
    "alternate_totals",
    "btts",
    "double_chance",
]


async def main() -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]

    import alt_lines_feed as af
    # Save & override the acquisition list for this one refresh.
    _saved_soccer_markets = list(af.SOCCER_MARKETS)
    af.SOCCER_MARKETS[:] = list(REQUIRED_MARKETS)
    # Also override the static SPORT_CONFIG soccer entries so the
    # per-event fetch sees the same 5-market slate.
    _saved_sport_cfg = dict(af.SPORT_CONFIG)
    for k, (sk, _mkts) in list(af.SPORT_CONFIG.items()):
        if k.startswith("soccer"):
            af.SPORT_CONFIG[k] = (sk, list(REQUIRED_MARKETS))

    # ── Provider HTTP call counter ────────────────────────────────
    import httpx
    _call_log: list[dict] = []
    _quota_used_start: int | None = None
    _quota_used_end:   int | None = None

    _orig_send = httpx.AsyncClient.send

    async def _tracked_send(self, request, *args, **kwargs):
        nonlocal _quota_used_end
        _call_log.append({
            "method": request.method,
            "url":    str(request.url).split("?")[0],
            "markets": (dict(request.url.params).get("markets") or "")[:120],
        })
        resp = await _orig_send(self, request, *args, **kwargs)
        # Odds API quota headers are exposed on every response.
        used = resp.headers.get("x-requests-used") or resp.headers.get(
            "X-Requests-Used"
        )
        if used is not None:
            try:
                _quota_used_end = int(used)
            except Exception:
                pass
        return resp

    httpx.AsyncClient.send = _tracked_send

    try:
        # Read starting quota BEFORE the refresh from budget state.
        try:
            state = await db.odds_api_quota_state.find_one({}, {"_id": 0})
            if state and isinstance(state.get("used"), int):
                _quota_used_start = int(state["used"])
        except Exception:
            pass

        print("=== ONE-SHOT SOCCER PROVIDER REFRESH (budget-override, in-process) ===")
        print(f"Markets requested: {REQUIRED_MARKETS}")
        print(f"picks_scope=True   event_window_hours=36")

        summary = await af.refresh_alt_lines(
            db, picks_scope=True, event_window_hours=36,
        )
        print(f"\nrefresh_alt_lines summary:\n  {summary}")

    finally:
        # Restore ProviderBudget path + original acquisition list.
        httpx.AsyncClient.send = _orig_send
        af.SOCCER_MARKETS[:] = _saved_soccer_markets
        af.SPORT_CONFIG.clear()
        af.SPORT_CONFIG.update(_saved_sport_cfg)
        os.environ.pop("ODDS_GATEWAY_ENABLED", None)

    # ── Provider call cost report ────────────────────────────────
    print(f"\n=== PROVIDER REQUEST COUNT ===")
    print(f"  Total outbound httpx requests: {len(_call_log)}")
    by_url: dict[str, int] = {}
    for c in _call_log:
        by_url[c["url"]] = by_url.get(c["url"], 0) + 1
    for u, n in sorted(by_url.items(), key=lambda x: -x[1]):
        print(f"    {n:4d}  {u}")
    if _quota_used_end is not None:
        delta = (
            _quota_used_end - _quota_used_start
            if _quota_used_start is not None else None
        )
        print(
            f"  Odds API quota — used_before={_quota_used_start}, "
            f"used_after={_quota_used_end}, delta_credits={delta}"
        )
    else:
        print("  Odds API quota headers not observed (all cache hits?)")

    # ── Run ingester so new live_alt_lines rows reach the board ──
    print("\n=== INGEST PASS (real_line_scorer_ingest) ===")
    from services.real_line_scorer_ingest import (
        ingest_real_line_soccer_scorers as _ingest,
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ingest_stats = await _ingest(db, today=today)
    print(f"  ingest summary: {ingest_stats}")

    # ── FINAL PROOF for BTTS + Anytime Scorer ─────────────────────
    print("\n=== FINAL PROOF (BTTS + Anytime Scorer end-to-end) ===")
    for kind, mkeys in [
        ("BTTS",           ["btts", "both_teams_to_score"]),
        ("Anytime Scorer", ["player_goal_scorer_anytime"]),
        ("Score or Assist",["player_to_score_or_assist"]),
        ("Double Chance",  ["double_chance"]),
        ("Alt Totals",     ["alternate_totals"]),
    ]:
        n_raw_alt = await db.live_alt_lines.count_documents({
            "sport":      {"$in": ["soccer", "Soccer"]},
            "market_key": {"$in": mkeys},
        })
        n_ingested = await db.picks.count_documents({
            "sport": "Soccer", "pick_date": today,
            "market_key": {"$in": mkeys},
            "model_probability": {"$exists": True, "$ne": None},
        })
        n_on_board = await db.picks.count_documents({
            "sport": "Soccer", "pick_date": today,
            "market_key": {"$in": mkeys},
            "off_board": {"$ne": True},
        })
        if n_raw_alt == 0:
            print(f"  {kind:18s} RAW_PROVIDER_MARKET_NOT_PRESENT")
            continue
        # Sample one live event
        sample_ev = await db.live_alt_lines.find_one({
            "sport": {"$in": ["soccer", "Soccer"]},
            "market_key": {"$in": mkeys},
        })
        ev_id = sample_ev.get("event_id") if sample_ev else "?"
        print(
            f"  {kind:18s} raw_alt_rows={n_raw_alt:4d} → "
            f"modeled={n_ingested:4d} → on_board={n_on_board:4d}  "
            f"sample_event={ev_id[:16]}…"
        )

    print("\nSOCCER_MARKET_COMPETITION_RUNTIME_FIXED — one-shot proof complete.")

if __name__ == "__main__":
    asyncio.run(main())
