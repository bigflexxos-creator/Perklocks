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


@router.get("/media/player/{media_key}")
async def player_media_lookup(media_key: str):
    """P3 — cached lightweight player media (Mongo only; no provider
    discovery at screen time).  404 is an OPTIONAL failure for clients."""
    from services.player_media import get_player_media
    row = await get_player_media(db, media_key)
    if not row:
        raise HTTPException(404, "no media")
    return row


@router.get("/atd/slate")
async def nfl_atd_slate(
    min_probability: float = Query(0.10, ge=0.01, le=0.99),
    top_n: int = Query(5, ge=1, le=20),
    top_n_per_game: int = Query(3, ge=1, le=20),
):
    """P4 — ONE ATD slate truth.  Top 5 and By Game derive from the SAME
    universe built once per request (canonical published ATD rows +
    explicit on-demand candidates).  Mongo-only; zero provider calls."""
    try:
        from services.nfl_atd_slate import build_atd_slate
        return await build_atd_slate(
            db, min_probability=min_probability, top_n=top_n,
            top_n_per_game=top_n_per_game,
        )
    except Exception as e:
        raise HTTPException(500, f"nfl atd slate failed: {e}")


@router.get("/atd/leaderboard")
async def nfl_atd_leaderboard(
    limit: int = Query(20, ge=1, le=100),
    min_probability: float = Query(0.10, ge=0.05, le=0.95),
    min_opportunity_rating: str = Query("low", regex="^(low|med|high)$"),
):
    """Return CURRENT BETTABLE ATD opportunities backed by canonical
    published NFL ATD picks (real sportsbook odds, real ATD engine
    output, current-roster team, current event membership).

    2026-06-09 · position-fairness update per user directive
    "ATD section should not just be running backs, should be top 5
    mathematically":
      • ``min_probability`` default lowered 0.30 → 0.10 so WRs / TEs
        with realistic red-zone-share TD probabilities (0.15-0.35)
        rank alongside RB workhorses (0.55-0.70) purely on math.
      • ``min_opportunity_rating`` default lowered "med" → "low" so
        elite WRs at 5-8 targets/game aren't filtered out before
        the sort.
      • Sort key changed from ``(confidence, td_probability)`` to
        ``(td_probability, confidence)`` — mathematical merit first,
        confidence as tiebreaker.
      • Query no longer restricted to ``publication_state=PUBLISHED``
        or non-off_board rows: an ATD *evaluation* whose truthful
        edge falls under chalk-trap on the main board is still a
        legitimate mathematical ranking entry for the ATD leaderboard
        (the frontend renders board-eligibility separately).

    Block 2D · P0 (2026-06-09) — RE-POINTED per user directive:
    the ATD tab must read the same canonical output as the main
    board, not a separate historical-player-ranking endpoint.  The
    historical leaderboard remains available as a *last-resort*
    fallback (``mode="research_only"`` in the response) so the
    screen never dead-ends when the canonical pipeline is warming up
    for the day.  The frontend contract (``picks``: NFLAtdPick[])
    is unchanged; new fields are additive.
    """
    try:
        from nfl_atd_engine import atd_leaderboard
        from datetime import datetime, timezone, timedelta
        # ── ATD FRESHNESS + MARKET PURITY (2026-09-13, Part H) ─────
        # (1) MARKET PURITY — restrict the canonical query to the
        #     ANYTIME-TD family ONLY.  ``1st TD`` and ``First TD``
        #     rows must NEVER surface on the ANYTIME-TD leaderboard —
        #     they are a distinct market with distinct exact-threshold
        #     semantics.
        # (2) FRESHNESS — only accept rows whose ``event_time`` is
        #     current or upcoming (with a 15-minute clock-skew
        #     grace).  Yesterday's ATD candidates are historical
        #     research, not a bettable live-board row.
        _now = datetime.now(timezone.utc)
        _min_event_time = (_now - timedelta(minutes=15)).isoformat()
        # ── P4: ONE universe (shared with /atd/by-game + /atd/slate) ──
        from services.nfl_atd_slate import build_atd_universe
        _uni = await build_atd_universe(db, min_probability=float(min_probability))
        canonical: list[dict] = _uni["candidates"]

        if canonical:
            return {
                "mode": "canonical_publication",
                "total_candidates": len(canonical),
                "passed_filters": len(canonical),
                "rejected": {},
                "rules": {
                    "min_probability": min_probability,
                    "min_opportunity_rating": min_opportunity_rating,
                    "min_event_time": _min_event_time,
                    "market_filter": "Anytime TD only",
                    "note": (
                        "sourced from PUBLISHED ATD picks "
                        "(canonical_publication) + on-demand ATD engine "
                        "expansion for events with fresh provider rows "
                        "not yet in the canonical universe"
                    ),
                },
                "league_means": {},
                "picks": canonical[: max(1, int(limit))],
            }

        # ── Fallback: EXPLICIT no-current-bettable-ATD state ─────────
        # Part H — the historical research view must NEVER masquerade
        # as a live betting card.  When the canonical bettable set is
        # empty we return an explicit ``no_current_bettable_atd`` mode
        # with an empty picks list.  The historical research view
        # remains discoverable via the dedicated
        # ``atd_leaderboard`` engine call for offline analytics but
        # this live endpoint no longer emits research rows dressed as
        # betting cards.
        return {
            "mode": "no_current_bettable_atd",
            "total_candidates": 0,
            "passed_filters": 0,
            "rejected": {},
            "rules": {
                "min_probability": min_probability,
                "min_opportunity_rating": min_opportunity_rating,
                "min_event_time": _min_event_time,
                "market_filter": "Anytime TD only",
                "note": ("no current bettable ATD candidates — historical "
                          "research view is intentionally not returned here"),
            },
            "league_means": {},
            "picks": [],
        }
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


# ─────────────────────────── ATD Game-by-Game ───────────────────────────
# §B9 · Universal NFL Prop Closure — Game-by-Game ATD rankings.
#
# The SAME canonical ATD scores from ``/atd/leaderboard`` are grouped
# by ``canonical_event_id`` and each game returns its own Top-N.
# Per user directive: "ONE PLAYER = ONE TD PROBABILITY + ONE ATD SCORE.
# Only the ranking universe changes."  This endpoint does NOT recompute;
# it re-uses the exact same canonical publication rows and re-groups.

@router.get("/atd/by-game")
async def nfl_atd_by_game(
    top_n_per_game: int = Query(5, ge=1, le=20,
        description="Number of top ATD candidates per game (default 5)"),
    min_probability: float = Query(0.10, ge=0.01, le=0.99),
    min_opportunity_rating: str = Query(
        "low", pattern="^(low|med|high|elite)$",
        description="Minimum opportunity rating (low = surface everything ranked mathematically)",
    ),
):
    """§B9 · Return Top-N ATD candidates for every NFL game on the slate.

    Groups the SAME canonical ATD publication rows used by the global
    ``/atd/leaderboard`` by ``canonical_event_id`` (falls back to
    ``event``).  The top-N per game is deterministic by
    ``(td_probability desc, confidence desc, canonical_player_id asc)``
    — identical tie-breaking to the global leaderboard so
    (global_rank, game_rank) reconcile mathematically:

        If player X is the highest ATD Score in game G, X will be
        game_rank=1 for G AND appear at whatever global_rank the
        slate-wide sort places him.  No screen-specific score
        mutation.  ONE player = ONE ATD score.

    Response shape:
        {
          "mode": "canonical_publication",
          "games_count": <int>,
          "candidates_total": <int>,
          "picks_returned": <int>,
          "games": [
            {
              "event": "<home vs away>",
              "canonical_event_id": "<canonical id>",
              "event_time": "<iso>",
              "home_team": "...",
              "away_team": "...",
              "candidates_in_game": <int>,
              "picks": [ <same shape as /atd/leaderboard picks[]> ],
            },
            …
          ],
          "note": "One player = one TD probability + one ATD score. "
                  "Only the ranking universe changes.",
        }
    """
    try:
        from datetime import datetime, timezone, timedelta
        # 2026-09-13 (Part H) — same freshness + market-purity gate
        # as ``/atd/leaderboard`` so both endpoints share the SAME
        # canonical current-slate ATD candidate universe.
        _now = datetime.now(timezone.utc)
        _min_event_time = (_now - timedelta(minutes=15)).isoformat()
        # ── P4: ONE universe (shared with /atd/leaderboard + /atd/slate) ──
        from services.nfl_atd_slate import build_atd_universe
        _uni = await build_atd_universe(db, min_probability=float(min_probability))
        all_candidates: list[dict] = _uni["candidates"]

        # Group by event NAME (with canonical_event_id fallback).
        # Canonical rows persist ``canonical_event_id = event_name``
        # while on-demand rows carry the Odds API event id hex — using
        # the event NAME as the primary group key coalesces both
        # sources onto the same matchup card.  Falls back to
        # ``canonical_event_id`` when event name is empty.
        by_game: dict[str, list[dict]] = {}
        for c in all_candidates:
            k = (c.get("event") or c.get("canonical_event_id") or "unknown").strip()
            by_game.setdefault(k, []).append(c)

        games_out = []
        picks_returned = 0
        for evt_id, rows in by_game.items():
            # Deterministic — no hash: (td desc, confidence desc, player_id asc)
            rows.sort(key=lambda r: (-float(r.get("td_probability") or 0.0),
                                     -float(r.get("confidence") or 0.0),
                                     str(r.get("player_id") or "")))
            picks = rows[: top_n_per_game]
            picks_returned += len(picks)
            # kickoff ordering key so the frontend can render chrono
            _kickoff = (picks[0].get("event_time") if picks else None) or ""
            games_out.append({
                "event":               (picks[0].get("event") if picks else evt_id),
                "canonical_event_id":  evt_id,
                "event_time":          _kickoff,
                "home_team":           picks[0].get("home_team") if picks else None,
                "away_team":           picks[0].get("away_team") if picks else None,
                "candidates_in_game":  len(rows),
                "picks":               picks,
            })
        # Kickoff chronological order per §B9.
        games_out.sort(key=lambda g: (g.get("event_time") or "", g.get("event") or ""))

        return {
            "mode": "canonical_publication",
            "games_count": len(games_out),
            "candidates_total": len(all_candidates),
            "picks_returned": picks_returned,
            "top_n_per_game": top_n_per_game,
            "rules": {
                "min_probability": min_probability,
                "min_opportunity_rating": min_opportunity_rating,
                "sort_key": "(td_probability desc, confidence desc, player_id asc)",
            },
            "games": games_out,
            "note": (
                "One player = one TD probability + one ATD score. "
                "Only the ranking universe changes.  Global rank comes "
                "from /atd/leaderboard; game rank comes from this "
                "endpoint — they reconcile mathematically from the "
                "SAME canonical ATD publication rows."
            ),
        }
    except Exception as e:
        raise HTTPException(500, f"nfl atd by-game failed: {e}")


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
