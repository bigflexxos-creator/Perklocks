"""NFL Feature Engine — Phase 3 M3 (2026-07-22).

The Phase-3 counterpart to services/mlb_feature_engine.py and
services/soccer_feature_engine.py. Combines:

  • Historical rolling averages (M1: nfl_features.py)
  • Volume trend (L3 vs season)
  • Home/away splits
  • Career vs-opponent hit-rate
  • Position-specific defensive matchup (M2: nfl_opp_defense.py)
  • Book implied probability

Into a `build_nfl_prop_factors()` output that mirrors the MLB engine
shape: (factors_dict, source_list). Every value is real historical
data — no RNG, no placeholders. Returns None per-factor when data
insufficient; the gate `has_enough_real_data_nfl()` rejects picks
without ≥3 real factors.

Supported prop stats:
    passing_yards, attempts, passing_tds, completions,
    rushing_yards, carries, rushing_tds,
    receptions, targets, receiving_yards, receiving_tds,
    anytime_td (rush_td + rec_td)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.nfl_feature_engine")

MIN_FACTORS_NFL_PROP = 3


def _scale(value: float, low: float, high: float,
           out_low: float = 0.30, out_high: float = 0.95) -> float:
    """Linear scale a raw value into an [out_low, out_high] factor."""
    if high == low:
        return (out_low + out_high) / 2
    v = (value - low) / (high - low)
    v = max(0.0, min(1.0, v))
    return out_low + v * (out_high - out_low)


def has_enough_real_data_nfl(factors: dict) -> bool:
    """Return True iff at least MIN_FACTORS_NFL_PROP non-None real factors."""
    return sum(1 for v in factors.values() if isinstance(v, (int, float))) >= MIN_FACTORS_NFL_PROP


# ── Factor builders ──────────────────────────────────────────────────

def _factor_rolling_avg_vs_line(rolling: dict, stat: str, line: float) -> Optional[float]:
    """L5 rolling avg / line ratio → 0.30-0.95 factor.
    Ratio 1.0 → 0.55, 1.5 → 0.90, 2.0+ → 0.95.
    """
    l5 = (rolling or {}).get("l5") or {}
    val = l5.get(stat)
    if not isinstance(val, (int, float)) or line <= 0:
        return None
    ratio = val / line
    if ratio >= 1.0:
        v = 0.55 + min(0.40, (ratio - 1.0) * 0.80)
    else:
        v = 0.30 + max(0.0, ratio - 0.5) * 0.50
    return round(max(0.30, min(0.95, v)), 3)


def _factor_l3_vs_season(rolling: dict, stat: str) -> Optional[float]:
    """L3 average delta vs season — captures heating up / cooling down."""
    l3 = (rolling or {}).get("l3") or {}
    season = (rolling or {}).get("season_avg") or {}
    a, b = l3.get(stat), season.get(stat)
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0):
        return None
    delta = (a - b) / b
    # +20% (heating up) → 0.85, 0% → 0.60, -20% (cold) → 0.35
    v = 0.60 + delta * 1.25
    return round(max(0.30, min(0.95, v)), 3)


def _factor_home_away(splits: dict, stat_prefix: str, is_home: bool) -> Optional[float]:
    """Which venue produces the higher output? Player-specific home/away."""
    hs = (splits or {}).get("home") or {}
    aw = (splits or {}).get("away") or {}
    key = {
        "passing":   "passing_yards",
        "rushing":   "rushing_yards",
        "receiving": "receiving_yards",
        "receptions": "receptions",
    }.get(stat_prefix)
    if not key:
        return None
    h_val = hs.get(key); a_val = aw.get(key)
    if not (isinstance(h_val, (int, float)) and isinstance(a_val, (int, float))):
        return None
    if h_val + a_val <= 0:
        return None
    if is_home:
        share = h_val / (h_val + a_val)
    else:
        share = a_val / (h_val + a_val)
    # share > 0.55 → 0.75, share < 0.45 → 0.40
    return round(_scale(share, 0.40, 0.60), 3)


def _factor_prop_hit_rate(hit_rate_row: dict) -> Optional[float]:
    """Career hit rate vs this opponent → factor.
    100% → 0.95, 50% → 0.55, 0% → 0.30. Weight by sample size.
    """
    if not hit_rate_row:
        return None
    hr = hit_rate_row.get("hit_rate")
    n = hit_rate_row.get("games") or 0
    if not isinstance(hr, (int, float)):
        return None
    # Bayesian shrinkage toward 0.5 for small samples
    prior_weight = 3.0
    posterior = (hr * n + 0.5 * prior_weight) / (n + prior_weight)
    return round(max(0.30, min(0.95, 0.30 + posterior * 0.65)), 3)


def _factor_matchup(opp_pos_allowances: dict, prop_stat: str, line: float) -> Optional[float]:
    """Wraps M2's matchup_factor to avoid a circular import in tests."""
    try:
        from services.nfl_opp_defense import matchup_factor
        return matchup_factor(opp_pos_allowances, prop_stat, line)
    except Exception:
        return None


def _factor_book_implied(book_implied: Optional[float]) -> Optional[float]:
    """Convert book_implied prob (0-1) to a factor value. Always defined
    when odds are present — acts as the anchor floor factor."""
    if not isinstance(book_implied, (int, float)):
        return None
    return round(max(0.30, min(0.95, float(book_implied))), 3)


# ── Composite feature engine ─────────────────────────────────────────

async def build_nfl_prop_factors(
    db,
    *,
    player: str,
    opponent: str,           # opposing team abbrev (e.g. "SF")
    position: str,           # QB, RB, WR, TE
    prop_stat: str,          # passing_yards / rushing_yards / etc.
    line: float,             # book line
    side: str = "over",      # over / under
    season: int,
    week: int,
    is_home: bool = True,
    book_implied: Optional[float] = None,
) -> tuple[dict, list[str]]:
    """Build the Phase-3 NFL prop factor set.

    Returns (factors_dict, source_list). Callers should gate emission
    on `has_enough_real_data_nfl(factors)`.
    """
    from services.nfl_features import (
        player_recent_averages, home_away_splits,
        player_prop_hit_rate_vs_opponent,
    )
    from services.nfl_opp_defense import team_defense_allowances

    # Fetch all raw data in parallel-friendly sequence.
    rolling = await player_recent_averages(db, player, season, week)
    splits = await home_away_splits(db, player, current_season=season)
    hit_row = await player_prop_hit_rate_vs_opponent(
        db, player, prop_stat, line, opponent, side
    )
    opp_all = await team_defense_allowances(db, opponent, season)
    opp_pos = opp_all.get(position) or opp_all.get(position.upper()) or {}

    # Build the factors ────────────────────────────────────────────
    stat_prefix = (
        "passing"  if prop_stat.startswith("passing_") or prop_stat in ("attempts", "completions")
        else "rushing"  if prop_stat.startswith("rushing_") or prop_stat == "carries"
        else "receiving"  if prop_stat.startswith("receiving_") or prop_stat in ("receptions", "targets")
        else "misc"
    )

    factors: dict[str, Optional[float]] = {
        "L5 Avg vs Line":            _factor_rolling_avg_vs_line(rolling, prop_stat, line),
        "L3 vs Season Trend":        _factor_l3_vs_season(rolling, prop_stat),
        "Home/Away Split":           _factor_home_away(splits, stat_prefix, is_home),
        "Career vs Opponent Hit%":   _factor_prop_hit_rate(hit_row),
        "Opponent Defense Allowance": _factor_matchup(opp_pos, prop_stat, line),
        "Book Implied Anchor":       _factor_book_implied(book_implied),
    }

    sources = []
    if factors["L5 Avg vs Line"] is not None:
        sources.append("nflverse_weekly_L5")
    if factors["L3 vs Season Trend"] is not None:
        sources.append("nflverse_weekly_L3_trend")
    if factors["Home/Away Split"] is not None:
        sources.append("nflverse_home_away_splits")
    if factors["Career vs Opponent Hit%"] is not None:
        sources.append("nflverse_career_vs_opp")
    if factors["Opponent Defense Allowance"] is not None:
        sources.append("nflverse_opp_defense_agg")
    if factors["Book Implied Anchor"] is not None:
        sources.append("odds_api_book_implied")

    # Rationale — the "Why this pick" prose we surface on the UI.
    rationale_bits = []
    l5 = ((rolling or {}).get("l5") or {}).get(prop_stat)
    if isinstance(l5, (int, float)):
        rationale_bits.append(
            f"{player}'s L5 avg is {l5:.1f} vs a line of {line:g}."
        )
    if hit_row and hit_row.get("games"):
        rationale_bits.append(hit_row.get("rationale", ""))
    if opp_pos:
        # Report the specific position matchup allowance
        key_map = {
            "passing_yards": "pass_yds_per_game", "rushing_yards": "rush_yds_per_game",
            "receiving_yards": "rec_yds_per_game", "carries": "carries_per_game",
            "receptions": "receptions_per_game", "targets": "targets_per_game",
        }
        k = key_map.get(prop_stat)
        if k and isinstance(opp_pos.get(k), (int, float)):
            rationale_bits.append(
                f"{opponent} defense allows {opp_pos[k]:.1f} {prop_stat.replace('_', ' ')} "
                f"per game to opposing {position}s in {season}."
            )

    return factors, sources


__all__ = [
    "build_nfl_prop_factors",
    "build_nfl_game_context",
    "has_enough_real_data_nfl",
    "MIN_FACTORS_NFL_PROP",
]


# ── Async pre-loader (called from the sports_engine props fetcher) ────
# One call per game populates ctx["nfl_precomputed"][player][market_key]
# with the ready-to-consume factor dict. The synchronous pick-generation
# branch just looks up from this cache — no DB access inside the sync
# loop. This mirrors the MLB pattern (build_mlb_game_context → sync
# feature engine reads from ctx["hitters"], ctx["starting_pitcher_*"]).

async def build_nfl_game_context(
    db,
    *,
    game: dict,
    prop_candidates: list[dict],
    season: int,
    week: int,
) -> dict:
    """Pre-compute NFL prop factors for every prop candidate on a game.

    `prop_candidates` — list of dicts with keys:
        {"player": str, "market": str, "line": float, "side": str,
         "position": str | None, "book_implied": float}

    Returns a dict shaped for injection into `payload._ctx`:
        {
          "nfl_precomputed": {
             "josh allen": {
                "player_pass_yds": {
                   "factors": {...},
                   "sources": [...],
                },
             },
             ...
          },
        }
    """
    out: dict[str, dict[str, dict]] = {}
    home_team = (game.get("home_team") or "").strip()
    away_team = (game.get("away_team") or "").strip()

    for cand in prop_candidates:
        player = cand.get("player") or ""
        if not player:
            continue
        market = cand.get("market") or ""
        # Import inside to avoid circular
        from services.nfl_feature_engine import build_nfl_prop_factors
        # Determine which team is the player's team → opponent + is_home
        player_team = cand.get("team") or ""
        opponent = ""
        is_home = False
        if player_team and (player_team.upper() == _abbrev(home_team).upper()):
            opponent = _abbrev(away_team)
            is_home = True
        elif player_team and (player_team.upper() == _abbrev(away_team).upper()):
            opponent = _abbrev(home_team)
            is_home = False
        else:
            # Fallback: use home team as opp guess. Will still return
            # real factors for the non-opp-conditioned ones (L5, trend).
            opponent = _abbrev(away_team) or _abbrev(home_team)
            is_home = True

        # Map market → stat field name (best-effort; unknown markets skipped)
        _NFL_MARKET_TO_STAT_LOCAL = {
            "player_pass_yds": "passing_yards",
            "player_pass_yds_alternate": "passing_yards",
            "player_pass_tds": "passing_tds",
            "player_pass_attempts": "attempts",
            "player_pass_completions": "completions",
            "player_rush_yds": "rushing_yards",
            "player_rush_yds_alternate": "rushing_yards",
            "player_rush_attempts": "carries",
            "player_rush_tds": "rushing_tds",
            "player_receptions": "receptions",
            "player_receptions_alternate": "receptions",
            "player_reception_yds": "receiving_yards",
            "player_reception_yds_alternate": "receiving_yards",
            "player_reception_tds": "receiving_tds",
        }
        stat = _NFL_MARKET_TO_STAT_LOCAL.get(market)
        if not stat:
            continue

        # Block 2D Closure §2 (2026-08) — resolve the ACTUAL player
        # position from the canonical NFL registry, not from the market
        # name.  Prevents QB rushing → RB, RB receiving → WR, TE
        # receiving → WR misattribution.  Falls back to market-key
        # inference only when the player cannot be resolved (rookie
        # not yet in the weekly data, etc.).
        canonical_pos = None
        try:
            from sports_engine import resolve_nfl_position_for_player
            canonical_pos = await resolve_nfl_position_for_player(
                db, name=player, team=cand.get("team") or None)
        except Exception:
            canonical_pos = None
        position = canonical_pos or cand.get("position") or _infer_position(market)
        try:
            factors, sources = await build_nfl_prop_factors(
                db,
                player=player, opponent=opponent, position=position,
                prop_stat=stat,
                line=float(cand.get("line") or 0.0),
                side=str(cand.get("side") or "over"),
                season=int(season), week=int(week),
                is_home=is_home,
                book_implied=cand.get("book_implied"),
            )
            key_l = player.strip().lower()
            out.setdefault(key_l, {})[market] = {
                "factors": factors,
                "sources": sources,
                "position_used": position,
                "position_source": ("canonical_registry"
                                     if canonical_pos else "market_inference"),
            }
        except Exception as e:
            logger.debug("nfl precompute failed for %s/%s: %s", player, market, e)

    # ── Block 2D A1 (2026-08) — NFL ATD specialized-engine precompute ──
    # For any anytime_td / 1st_td candidate on this game, resolve the
    # player identity and call the specialized nfl_atd_engine.  Result
    # stored in ctx["nfl_atd_precomputed"][player_lower] so the sync
    # emission loop can consume it without doing an await.
    #
    # Missing history / low sample / unresolved identity all fall
    # through to a ``reject`` marker — the sync emitter drops the
    # pick.  MISSING DATA never becomes a manufactured probability.
    atd_out: dict[str, dict] = {}
    atd_candidates = [
        c for c in prop_candidates
        if (c.get("market") or "") in ("player_anytime_td", "player_1st_td")
    ]
    if atd_candidates:
        from nfl_atd_engine import (
            predict_player_atd, resolve_player_id_from_name,
        )
        for cand in atd_candidates:
            player = (cand.get("player") or "").strip()
            if not player:
                continue
            key_l = player.lower()
            if key_l in atd_out:
                continue  # dedupe (Yes market often appears in multiple bookmakers)
            player_team = cand.get("team") or ""
            # Resolve to nflverse GSIS.  Refuses to guess on ambiguity.
            try:
                pid = await resolve_player_id_from_name(
                    db, name=player, team=player_team or None)
            except Exception as e:
                logger.debug("nfl_atd resolve err %s: %s", player, e)
                pid = None
            if not pid:
                atd_out[key_l] = {
                    "reject": "unresolved_player_identity",
                    "player_name": player,
                }
                continue
            # Determine opponent + spread (spread unavailable at this
            # layer — kept None to skip game-script factor).
            opp_full = away_team if is_home else home_team
            opp_abbrev = _abbrev(opp_full)
            try:
                result = await predict_player_atd(
                    db, player_id=pid,
                    opponent=opp_abbrev or None,
                    spread=None,
                )
            except Exception as e:
                logger.debug("nfl_atd predict err %s: %s", player, e)
                atd_out[key_l] = {
                    "reject": "engine_error",
                    "engine_error": str(e)[:120],
                }
                continue
            atd_out[key_l] = result or {"reject": "engine_returned_none"}
    if atd_out:
        return {"nfl_precomputed": out, "nfl_atd_precomputed": atd_out}
    return {"nfl_precomputed": out}


def _abbrev(team_name: str) -> str:
    """Fallback team → abbrev helper for the pre-loader."""
    if not team_name:
        return ""
    # Try common patterns — if it's already 2-3 letters, return as is
    if len(team_name) <= 3 and team_name.isupper():
        return team_name
    # nfl team name → abbrev (small hand-map)
    mapping = {
        "arizona cardinals": "ARI", "atlanta falcons": "ATL",
        "baltimore ravens": "BAL", "buffalo bills": "BUF",
        "carolina panthers": "CAR", "chicago bears": "CHI",
        "cincinnati bengals": "CIN", "cleveland browns": "CLE",
        "dallas cowboys": "DAL", "denver broncos": "DEN",
        "detroit lions": "DET", "green bay packers": "GB",
        "houston texans": "HOU", "indianapolis colts": "IND",
        "jacksonville jaguars": "JAX", "kansas city chiefs": "KC",
        "las vegas raiders": "LV", "los angeles chargers": "LAC",
        "los angeles rams": "LA", "miami dolphins": "MIA",
        "minnesota vikings": "MIN", "new england patriots": "NE",
        "new orleans saints": "NO", "new york giants": "NYG",
        "new york jets": "NYJ", "philadelphia eagles": "PHI",
        "pittsburgh steelers": "PIT", "san francisco 49ers": "SF",
        "seattle seahawks": "SEA", "tampa bay buccaneers": "TB",
        "tennessee titans": "TEN", "washington commanders": "WAS",
    }
    return mapping.get(team_name.strip().lower(), team_name[:3].upper())


def _infer_position(market: str) -> str:
    m = (market or "").lower()
    if "pass" in m:
        return "QB"
    if "rush" in m:
        return "RB"
    if "reception" in m or "reception_yds" in m or "receptions" in m:
        return "WR"
    return "WR"
