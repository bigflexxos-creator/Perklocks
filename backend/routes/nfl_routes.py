"""HTTP routes for the NFL intelligence engines.

Exposes:
  • Safe-Bets engine  — `GET /api/nfl/safe-bets`
       Highest TRUE win-probability player-prop picks across rushing,
       receiving, receptions, passing, ATD. Filtered by ALT RULES
       (median ≥ line, floor p10 ≥ line, ≥10 attempts, ≥5 games, no
       single-game outlier inflation).

  • ATD Leaderboard   — `GET /api/nfl/atd/leaderboard`
       Per-player TRUE probability of scoring ≥ 1 TD, ranked by
       confidence. Neutral matchup unless caller supplies opponent.

  • ATD Predict       — `GET /api/nfl/atd/predict`
       Single-player matchup-adjusted prediction. Accepts optional
       `opponent` (team displayName) and `spread`.

Authentication: read-only public — no admin gate. Heavy aggregates
are bounded by MIN_GAMES_SAMPLE / volume floors so a noisy caller can't
DOS the DB.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from deps import db

router = APIRouter(prefix="/api/nfl")


@router.get("/safe-bets")
async def nfl_safe_bets(
    limit: int = Query(10, ge=1, le=50),
    min_probability: float = Query(0.62, ge=0.5, le=0.99),
):
    """Top NFL player-prop locks in the TRUE-VALUE BAND (-200 to -450).

    NEW (2026-06-29 v2): Default `min_probability=0.62` (≈ -163 American)
    surfaces real value, not extreme chalk. The engine internally targets
    the [0.67, 0.82] band first (≈ -200 to -456) and falls back to
    [0.62, 0.67) if nothing better is available. Picks above 0.86 (-614)
    are hard-rejected — user mandate to filter out trap-juice chalk.
    """
    try:
        from nfl_safe_engine import compute_safe_bets
        return await compute_safe_bets(
            db, limit=limit, min_probability=min_probability,
        )
    except Exception as e:
        raise HTTPException(500, f"nfl safe-bets failed: {e}")


@router.get("/atd/leaderboard")
async def nfl_atd_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    min_probability: float = Query(0.30, ge=0.05, le=0.95),
    min_opportunity_rating: str = Query("med", regex="^(low|med|high)$"),
):
    """Rank every eligible NFL player by P(TD ≥ 1) under a neutral matchup.

    `min_opportunity_rating` filters by L10 weighted touch volume:
      low: ≥0  ·  med: ≥7.0  ·  high: ≥12.0 touches/game.
    """
    try:
        from nfl_atd_engine import atd_leaderboard
        return await atd_leaderboard(
            db,
            limit=limit,
            min_probability=min_probability,
            min_opportunity_rating=min_opportunity_rating,
        )
    except Exception as e:
        raise HTTPException(500, f"nfl atd leaderboard failed: {e}")


@router.get("/atd/predict")
async def nfl_atd_predict(
    player_id: str = Query(..., min_length=2),
    opponent: Optional[str] = Query(None, description="Opponent team displayName, e.g. 'Dallas Cowboys'"),
    spread: Optional[float] = Query(None, description="Player's TEAM spread (negative = favored)"),
):
    """Single-player ATD prediction with optional matchup + game script."""
    try:
        from nfl_atd_engine import predict_player_atd
        out = await predict_player_atd(
            db, player_id=player_id, opponent=opponent, spread=spread,
        )
        if out.get("reject"):
            raise HTTPException(422, f"Rejected: {out['reject']} · {out}")
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"nfl atd predict failed: {e}")


# ─────────────────────────── Game-bets engine ───────────────────────────
# Wraps nfl_game_engine.py — ML / Spread / Total true-probability models.
# Completely separate from the player-prop layer. Lives behind /api/nfl/games.

@router.get("/games/predict")
async def nfl_game_predict(
    home: str = Query(..., min_length=2, description="Home team displayName"),
    away: str = Query(..., min_length=2, description="Away team displayName"),
    market: str = Query("ml", regex="^(ml|spread|total)$"),
    spread: Optional[float] = Query(None, description="HOME spread (negative if favored)"),
    total: Optional[float] = Query(None, description="O/U total line"),
):
    """Single-matchup true probability across ML / Spread / Total."""
    try:
        from nfl_game_engine import predict_game
        out = await predict_game(
            db, home=home, away=away, market=market,
            spread=spread, total=total,
        )
        if out.get("reject"):
            raise HTTPException(422, f"Rejected: {out['reject']}")
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"nfl game predict failed: {e}")


@router.get("/games/safe-alts")
async def nfl_game_safe_alts(
    home: str = Query(..., min_length=2),
    away: str = Query(..., min_length=2),
    min_probability: float = Query(0.78, ge=0.5, le=0.99),
):
    """Strongest alt-line locks for ML / Spread / Total in one matchup."""
    try:
        from nfl_game_engine import safe_alt_locks
        out = await safe_alt_locks(
            db, home=home, away=away, min_probability=min_probability,
        )
        if out.get("reject"):
            raise HTTPException(422, f"Rejected: {out['reject']}")
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"nfl safe-alts failed: {e}")


@router.get("/games/teams")
async def nfl_team_leaderboard(limit: int = Query(32, ge=1, le=64)):
    """Team strength leaderboard — recency-weighted ppg differential."""
    try:
        from nfl_game_engine import team_strength_leaderboard
        return await team_strength_leaderboard(db, limit=limit)
    except Exception as e:
        raise HTTPException(500, f"nfl team leaderboard failed: {e}")


@router.get("/games/safe-bets")
async def nfl_game_safe_bets(
    limit: int = Query(10, ge=1, le=30),
    min_probability: float = Query(0.78, ge=0.5, le=0.99),
):
    """Sweep every upcoming NFL matchup on the slate and return the
    highest-probability ML / Spread / Total locks across all of them.

    Source of "upcoming matchups":
      • `games` collection where status indicates pregame (not Final).
      • Falls back to today's `picks` collection grouped by event when
        we don't have a pregame games index for the day.
    """
    try:
        from nfl_game_engine import safe_alt_locks
        from datetime import datetime, timezone, timedelta

        # 1) Discover upcoming matchups for the next 7 days.
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=7)
        matchups: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        # Prefer pregame `games` entries (have explicit home/away).
        async for g in db.games.find(
            {"sport": "nfl", "status": {"$nin": ["Final", "final", "Completed"]}},
            {"_id": 0, "home": 1, "away": 1, "kickoff": 1},
        ).limit(60):
            home = g.get("home") or ""
            away = g.get("away") or ""
            if not home or not away:
                continue
            kickoff = g.get("kickoff")
            if kickoff:
                try:
                    k = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                    if k < now or k > horizon:
                        continue
                except Exception:
                    pass
            key = (home, away)
            if key in seen:
                continue
            seen.add(key)
            matchups.append(key)

        # 2) Fall back to NFL picks event strings ("Away @ Home" convention).
        if not matchups:
            async for p in db.picks.find(
                {"sport": "NFL"}, {"_id": 0, "event": 1},
            ).limit(200):
                ev = (p.get("event") or "").strip()
                if " @ " not in ev:
                    continue
                away, home = [s.strip() for s in ev.split(" @ ", 1)]
                if not away or not home:
                    continue
                key = (home, away)
                if key in seen:
                    continue
                seen.add(key)
                matchups.append(key)

        # 3) Compute safe alts per matchup and flatten into one ranked list.
        rows: list[dict] = []
        for home, away in matchups[:30]:
            try:
                r = await safe_alt_locks(
                    db, home=home, away=away, min_probability=min_probability,
                )
            except Exception:
                continue
            if r.get("reject"):
                continue
            matchup = r.get("matchup")
            for slot, market_name in (
                ("ml_pick", "moneyline"),
                ("spread_pick", "spread"),
                ("total_pick", "total"),
            ):
                pk = r.get(slot)
                if not pk:
                    continue
                rows.append({
                    "matchup": matchup,
                    "market": market_name,
                    "favored": r.get("favored"),
                    "expected_margin": r.get("expected_margin"),
                    "expected_total": r.get("expected_total"),
                    "pick": pk,
                    "true_probability": pk.get("true_probability", 0.0),
                })

        rows.sort(key=lambda x: x["true_probability"], reverse=True)
        return {
            "count": len(rows),
            "min_probability": min_probability,
            "matchups_evaluated": len(matchups),
            "bets": rows[: max(1, int(limit))],
        }
    except Exception as e:
        raise HTTPException(500, f"nfl game safe-bets failed: {e}")
