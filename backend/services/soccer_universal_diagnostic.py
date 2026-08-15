"""Universal Soccer production-truth diagnostic — Phase 2A.5 UNIVERSAL.

Produces the cross-league evidence matrix required for
``PHASE2A5_UNIVERSAL_SOCCER_PRODUCTION_TRUTH`` certification.

For every league currently returning provider data, it walks the full
pipeline and reports:

    provider fixtures found
    real game markets found
    real player markets found
    candidates created
    candidates evaluated
    candidates >= 85
    board-visible
    rejected
    rejection reasons per code

Reads-only.  Never mutates the DB.  Callable from an admin route AND
from CLI (``python -m services.soccer_universal_diagnostic``) so
operators can capture the certification evidence on-demand.
"""
from __future__ import annotations
import argparse, asyncio, json, os
from datetime import datetime, timezone
from typing import Any, Iterable

from services.soccer_rejection_taxonomy import ALL_CODES


GAME_MARKETS  = ("totals", "alternate_totals", "btts", "both_teams_to_score",
                 "h2h", "spreads", "alternate_spreads", "double_chance")
PLAYER_MARKETS = ("player_goal_scorer_anytime", "player_first_goal_scorer",
                  "player_last_goal_scorer", "player_to_score_or_assist",
                  "player_anytime_assist", "player_shots_on_target",
                  "player_shots")


async def _collect_live_alt_lines(db) -> dict[str, dict[str, int]]:
    """Return {league_sport_key: {"game": n, "player": n, "events": n}}."""
    pipeline = [
        {"$match": {"sport": {"$in": ["soccer", "Soccer"]}}},
        {"$group": {
            "_id": {"sk": "$odds_api_sport", "mk": "$market_key",
                    "ev": "$event_id"},
        }},
    ]
    out: dict[str, dict[str, Any]] = {}
    async for r in db.live_alt_lines.aggregate(pipeline):
        sk = r["_id"]["sk"] or "?"
        mk = r["_id"]["mk"] or "?"
        ev = r["_id"]["ev"] or "?"
        entry = out.setdefault(sk, {"game": 0, "player": 0,
                                    "events": set(), "markets": set()})
        if mk in GAME_MARKETS:
            entry["game"] += 1
        elif mk in PLAYER_MARKETS:
            entry["player"] += 1
        entry["events"].add(ev)
        entry["markets"].add(mk)
    # Materialise sets to counts / sorted lists for JSON.
    for sk in list(out.keys()):
        entry = out[sk]
        entry["events"]  = len(entry["events"])
        entry["markets"] = sorted(entry["markets"])
    return out


async def _collect_pick_stats(db, today: str) -> dict[str, Any]:
    """Return per-league pick funnel counts based on today's slate."""
    per_league: dict[str, dict[str, Any]] = {}
    rejection_totals: dict[str, int] = {code: 0 for code in ALL_CODES}
    q = {"sport": "Soccer", "pick_date": today}
    async for p in db.picks.find(q, projection={
        "league": 1, "sport_key": 1, "market_family": 1, "market_key": 1,
        "off_board": 1, "off_board_reasons": 1, "lock_score": 1,
        "source": 1, "selection": 1, "event": 1, "book_odds": 1,
        "bookmaker": 1, "edge_percent": 1, "model_probability": 1,
        "implied_probability": 1, "no_real_book_line": 1,
    }):
        league = p.get("league") or p.get("sport_key") or "?"
        entry = per_league.setdefault(league, {
            "candidates":      0,
            "evaluated":       0,
            "on_board":        0,
            "ge_85":           0,
            "rejected":        0,
            "by_family":       {},
            "by_rejection":    {},
            "sources":         {},
            "top_on_board":    [],
        })
        entry["candidates"] += 1
        fam = p.get("market_family") or "unknown"
        entry["by_family"][fam] = entry["by_family"].get(fam, 0) + 1
        src = p.get("source") or "?"
        entry["sources"][src] = entry["sources"].get(src, 0) + 1
        ls = p.get("lock_score") or 0
        if ls > 0:
            entry["evaluated"] += 1
        if p.get("off_board"):
            entry["rejected"] += 1
            for r in (p.get("off_board_reasons") or []):
                entry["by_rejection"][r] = entry["by_rejection"].get(r, 0) + 1
                if r in rejection_totals:
                    rejection_totals[r] += 1
        else:
            entry["on_board"] += 1
            if ls >= 85:
                entry["ge_85"] += 1
            if len(entry["top_on_board"]) < 3:
                entry["top_on_board"].append({
                    "market":       p.get("market_key"),
                    "selection":    p.get("selection"),
                    "event":        p.get("event"),
                    "book_odds":    p.get("book_odds"),
                    "bookmaker":    p.get("bookmaker"),
                    "edge_percent": p.get("edge_percent"),
                    "lock_score":   ls,
                    "model_p":      p.get("model_probability"),
                    "implied_p":    p.get("implied_probability"),
                    "source":       src,
                })
    return {
        "per_league":         per_league,
        "rejection_totals":   {k: v for k, v in rejection_totals.items() if v},
    }


async def run_diagnostic(db, *, today: str | None = None,
                          sample_examples: bool = True) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    live = await _collect_live_alt_lines(db)
    picks = await _collect_pick_stats(db, today)

    # ── Cross-league evidence rows ────────────────────────────────
    matrix: list[dict[str, Any]] = []
    all_sport_keys = sorted(set(live.keys()) | {
        v.get("sport_key", "") for _, v in picks["per_league"].items()
        if isinstance(v, dict)
    })
    all_sport_keys = [sk for sk in all_sport_keys if sk]

    # Build reverse index: league name → picks entry.
    from services.real_line_scorer_ingest import _league_from_sport_key
    league_by_sport_key = {sk: _league_from_sport_key(sk) for sk in all_sport_keys}

    for sk in all_sport_keys:
        lv = live.get(sk, {})
        league_label = league_by_sport_key.get(sk) or sk
        pick_by_league = picks["per_league"].get(league_label) or \
                          picks["per_league"].get(sk) or {}
        row = {
            "sport_key":              sk,
            "league":                 league_label,
            "provider_events":        lv.get("events", 0),
            "provider_game_markets":  lv.get("game", 0),
            "provider_player_markets": lv.get("player", 0),
            "provider_market_keys":   lv.get("markets", []),
            "candidates":             pick_by_league.get("candidates", 0),
            "on_board":               pick_by_league.get("on_board", 0),
            "ge_85":                  pick_by_league.get("ge_85", 0),
            "rejected":               pick_by_league.get("rejected", 0),
            "rejection_breakdown":    pick_by_league.get("by_rejection", {}),
        }
        if not (row["provider_events"] or row["candidates"]):
            row["note"] = "NO_LIVE_FIXTURE_FOR_PROOF"
        matrix.append(row)

    # ── Sample real end-to-end evaluations (if requested) ─────────
    examples: dict[str, list[dict[str, Any]]] = {}
    if sample_examples:
        # Real player prop — on-board evaluation across at least 2 leagues.
        async for p in db.picks.find({
            "sport": "Soccer", "pick_date": today,
            "market_family": "player_prop", "off_board": {"$ne": True},
        }).limit(5):
            examples.setdefault("player_prop_on_board", []).append(
                _example_dict(p))
        # Real player prop — legitimate rejection.
        async for p in db.picks.find({
            "sport": "Soccer", "pick_date": today,
            "market_family": "player_prop", "off_board": True,
            "off_board_reasons": {"$ne": None},
        }).limit(5):
            examples.setdefault("player_prop_rejection", []).append(
                _example_dict(p))
        # Game-market examples (BTTS / totals / h2h).
        for label, q in (
            ("btts",          {"market_key": {"$in": ["btts","both_teams_to_score"]}}),
            ("totals_over",   {"market_key": {"$in": ["totals","alternate_totals"]},
                                "selection":  {"$regex": "^over", "$options": "i"}}),
            ("totals_under",  {"market_key": {"$in": ["totals","alternate_totals"]},
                                "selection":  {"$regex": "^under", "$options": "i"}}),
            ("h2h",           {"market_key": "h2h"}),
        ):
            async for p in db.picks.find({
                "sport": "Soccer", "pick_date": today, **q,
            }).limit(3):
                examples.setdefault(label, []).append(_example_dict(p))

    return {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "pick_date":          today,
        "leagues_scanned":    len(all_sport_keys),
        "matrix":             matrix,
        "rejection_totals":   picks["rejection_totals"],
        "examples":           examples,
    }


def _example_dict(p: dict) -> dict[str, Any]:
    return {
        "league":       p.get("league"),
        "event":        p.get("event"),
        "market":       p.get("market"),
        "market_key":   p.get("market_key"),
        "selection":    p.get("selection"),
        "line":         p.get("line"),
        "book_odds":    p.get("book_odds"),
        "bookmaker":    p.get("bookmaker"),
        "model_prob":   p.get("model_probability"),
        "implied_prob": p.get("implied_probability"),
        "edge_percent": p.get("edge_percent"),
        "lock_score":   p.get("lock_score"),
        "grade":        p.get("grade"),
        "off_board":    p.get("off_board"),
        "reasons":      p.get("off_board_reasons"),
        "source":       p.get("source"),
        "no_real_book_line": p.get("no_real_book_line"),
    }


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", default=None)
    parser.add_argument("--out", default=None,
                        help="Optional path to dump JSON report.")
    args = parser.parse_args()

    from services.database import initialize_database, get_database
    initialize_database()
    db = get_database()
    report = await run_diagnostic(db, today=args.today)
    js = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(js)
        print(f"Wrote {args.out}")
    else:
        print(js)


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["run_diagnostic"]
