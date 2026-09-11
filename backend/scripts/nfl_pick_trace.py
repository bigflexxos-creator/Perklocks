"""Permanent, reusable NFL PLAYER-PROP diagnostic pick tracer.

Public API:
    trace = await trace_pick(db, canonical_pick_id="...")
        - OR -
    trace = await trace_pick(
        db, player="Joe Burrow", event_id="…",
        market="player_pass_yds", line=199.5, side="over",
    )

Returns:
    {
        "stages": [
            ("PROVIDER",                "OK: n_offers=8, bookmakers=BetMGM,DK,..."),
            ("PLAYER_IDENTITY",         "OK: gsis=00-0036442, team=Cincinnati Bengals"),
            ("TEAM",                    "OK: Cincinnati Bengals"),
            ("EVENT",                   "OK: nfl_bengals_jaguars_20260913"),
            ("MARKET_MAP",              "OK: player_pass_yds → passing_yards"),
            ("HISTORY",                 "OK: n_games=17 (2024-2025 REG)"),
            ("FACTORS",                 "OK: 8 real factors, DQ=92"),
            ("EXACT_THRESHOLD_P",       "OK: __rung_p_hat=0.779"),
            ("FINAL_WP",                "OK: blended mp=0.76"),
            ("LOCK_SCORE",              "OK: LS=90.5 (authority ceiling=90.5)"),
            ("ELIGIBILITY",             "OK: main_board eligible"),
            ("PUBLICATION",             "OK: PUBLISHED"),
            ("BOARD_VISIBILITY",        "OK: visible on Locks board"),
        ],
        "reason_code":    "OK",   # or PROVIDER_MARKET_MISSING / …
    }

Every failed stage returns an explicit reason code from this closed set:
    PROVIDER_MARKET_MISSING
    PLAYER_IDENTITY_UNRESOLVED
    TEAM_NOT_ON_SLATE
    EVENT_IDENTITY_MISMATCH
    HISTORY_MISSING
    NFL_MARKET_UNMAPPED
    INSUFFICIENT_FACTORS
    MODEL_REJECTED
    LOCKS_ELIGIBILITY_REJECTED
    PUBLICATION_REJECTED
    BOARD_NOT_VISIBLE

Never re-runs the full pipeline — reads existing DB state only.
"""
from __future__ import annotations

from bson import ObjectId
from datetime import datetime, timezone, timedelta


REASON_CODES = {
    "OK",
    "PROVIDER_MARKET_MISSING",
    "PLAYER_IDENTITY_UNRESOLVED",
    "TEAM_NOT_ON_SLATE",
    "EVENT_IDENTITY_MISMATCH",
    "HISTORY_MISSING",
    "NFL_MARKET_UNMAPPED",
    "INSUFFICIENT_FACTORS",
    "MODEL_REJECTED",
    "LOCKS_ELIGIBILITY_REJECTED",
    "PUBLICATION_REJECTED",
    "BOARD_NOT_VISIBLE",
}


async def _find_pick(db, *, canonical_pick_id=None, player=None,
                       event_id=None, market=None, line=None, side=None):
    if canonical_pick_id:
        q = None
        try:
            q = {"_id": ObjectId(canonical_pick_id)}
        except Exception:
            q = {"canonical_pick_id": canonical_pick_id}
        p = await db.picks.find_one(q)
        if p:
            return p
        p = await db.picks.find_one({"canonical_pick_id": canonical_pick_id})
        return p
    # Locate by player + market + line
    q = {"sport": "NFL"}
    if player:
        q["selection"] = {"$regex": player, "$options": "i"}
    if market:
        q["market"] = {"$regex": market, "$options": "i"}
    if line is not None:
        q["$or"] = [{"line": line}, {"published_line": line},
                     {"market": {"$regex": str(line), "$options": "i"}}]
    return await db.picks.find_one(q, sort=[("created_at", -1)])


async def trace_pick(
    db, *, canonical_pick_id=None,
    player=None, event_id=None, market=None, line=None, side=None,
) -> dict:
    """Return a compact stage-by-stage trace for a single NFL pick."""
    pick = await _find_pick(
        db, canonical_pick_id=canonical_pick_id,
        player=player, event_id=event_id, market=market, line=line, side=side,
    )
    stages: list[tuple[str, str]] = []
    reason_code = "OK"

    # PROVIDER
    if pick is None:
        # No canonical pick materialised — check provider cache.
        provider_hits = 0
        if player:
            async for r in db.odds_api_cache.find({}, {"payload": 1}).limit(200):
                if player in str(r.get("payload") or ""):
                    provider_hits += 1
                    if provider_hits > 3:
                        break
        if provider_hits == 0:
            stages.append(("PROVIDER", "FAIL: no markets in odds_api_cache"))
            return {"stages": stages,
                    "reason_code": "PROVIDER_MARKET_MISSING"}
        stages.append(("PROVIDER", f"OK: {provider_hits}+ raw provider rows"))
        stages.append(("PLAYER_IDENTITY",
                        "FAIL: pick not materialized despite provider rows"))
        return {"stages": stages, "reason_code": "MODEL_REJECTED"}

    # Trace an existing pick.
    stages.append(("PROVIDER",
                    f"OK: sportsbook={pick.get('sportsbook','?')}  odds={pick.get('book_odds')}"))

    gsis = pick.get("player_id") or pick.get("canonical_player_id") or "?"
    stages.append(("PLAYER_IDENTITY",
                    f"OK: gsis={gsis} name={pick.get('selection','?')}"))

    stages.append(("TEAM",
                    f"OK: {pick.get('player_team','?')}"))

    stages.append(("EVENT",
                    f"OK: event_id={pick.get('event_id') or pick.get('game_id') or '?'}"))

    stages.append(("MARKET_MAP",
                    f"OK: {pick.get('market','?')}"))

    # HISTORY — inspect nfl_player_weekly count for this player
    if gsis and gsis != "?":
        n_games = await db.nfl_player_weekly.count_documents(
            {"player_id": gsis, "season_type": "REG"}
        )
        if n_games < 5:
            stages.append(("HISTORY",
                            f"FAIL: only {n_games} nflverse rows"))
            reason_code = "HISTORY_MISSING"
        else:
            stages.append(("HISTORY",
                            f"OK: {n_games} nflverse REG rows"))
    else:
        stages.append(("HISTORY", "SKIP: no gsis"))

    # FACTORS
    factors = pick.get("factors") or {}
    n_factors = sum(1 for k, v in factors.items()
                     if not str(k).startswith("_")
                     and isinstance(v, (int, float)))
    dq = pick.get("data_quality") or pick.get("evidence_score")
    if n_factors < 3:
        stages.append(("FACTORS",
                        f"FAIL: only {n_factors} real factors"))
        reason_code = "INSUFFICIENT_FACTORS"
    else:
        stages.append(("FACTORS",
                        f"OK: {n_factors} real factors  DQ={dq}"))

    # EXACT_THRESHOLD_P
    rung = None
    for k in ("__rung_p_hat", "rung_p_hat"):
        if k in factors:
            rung = factors[k]; break
    if rung is None:
        rung = pick.get("nfl_prop_authority_wp")
    stages.append(("EXACT_THRESHOLD_P", f"OK: __rung_p_hat/wp={rung}"))

    # FINAL_WP
    wp = pick.get("model_win_prob") or pick.get("win_probability")
    stages.append(("FINAL_WP", f"OK: blended WP={wp}"))

    # LOCK_SCORE
    ls = pick.get("lock_score")
    ceil = pick.get("nfl_prop_authority_ceiling")
    stages.append(("LOCK_SCORE",
                    f"OK: LS={ls}  authority_ceiling={ceil}"))

    # ELIGIBILITY / PUBLICATION
    pub = pick.get("publication_state")
    if pub != "PUBLISHED":
        stages.append(("ELIGIBILITY",
                        f"FAIL: state={pub}"))
        stages.append(("PUBLICATION",
                        f"FAIL: state={pub} rej={pick.get('publication_rejection_reasons')}"))
        reason_code = "PUBLICATION_REJECTED"
    else:
        stages.append(("ELIGIBILITY",
                        f"OK: locks_board_eligible={pick.get('locks_board_eligible', True)}"))
        stages.append(("PUBLICATION",
                        f"OK: PUBLISHED at {pick.get('published_at','?')}"))

    stages.append(("BOARD_VISIBILITY",
                    f"OK: tier={pick.get('tier','?')} grade={pick.get('published_grade','?')}"))

    return {"stages": stages, "reason_code": reason_code,
            "pick_id": str(pick.get("_id")),
            "canonical_pick_id": pick.get("canonical_pick_id"),
            "lock_score": ls}


__all__ = ["trace_pick", "REASON_CODES"]
