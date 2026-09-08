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


# ────────────────────────────────────────────────────────────────────
# Block 2D · P0 (2026-06-09) — CURRENT TEAM RESOLVER (SURGICAL FIX)
# ────────────────────────────────────────────────────────────────────
# The prop-outcome payload from OddsAPI carries the player name but
# NEVER the team.  Downstream identity gate rejects any player-prop
# whose ``player_team`` is unresolvable (``PLAYER_TEAM_UNRESOLVED``).
#
# Data sources (in priority order):
#   1. ``db.players`` (sport="nfl") — ESPN active-roster feed,
#      refreshed daily by ``services.nfl.espn_player_db`` and
#      persisted with the CURRENT team + position.  This is the
#      canonical current-roster authority.  Ships full team display
#      name ("San Francisco 49ers") — no season gate.
#   2. ``db.nfl_player_weekly`` — nflverse historical weekly stats
#      (seasons 2019-2025 pre-2026 kickoff; adds 2026 as games play).
#      Used ONLY to resolve the canonical GSIS ``player_id`` and as
#      a historical-team fallback when the ESPN row is missing.
#
# We distinguish:
#   * ``current_team``   — from ``db.players`` (or the most recent
#                           2026-season nfl_player_weekly row once it
#                           lands).  Used for identity gate + display.
#   * ``historical_team``— last-known team of ANY season (kept for
#                           back-compat + explanation panels).
#
# NEVER guesses.  Ambiguous names + no team hint → returns None.
_ESPN_TO_FULL_NAME: dict[str, str] = {
    # Handful of common abbrev fallbacks in case player_weekly stores
    # abbreviations while ``db.players`` stores full names.  Not used
    # for identity gate; only for the audit trail.
}


def _name_variants(name: str) -> list[str]:
    """Return probable canonical variants for OddsAPI ↔ ESPN name
    differences (Sr./Jr./II suffix drops, apostrophe/dash forms).
    Preserves original as first entry.
    """
    if not name:
        return []
    n = name.strip()
    out = [n]
    # Drop trailing Jr./Sr./II/III/IV suffixes.
    import re
    stripped = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV|V)$", "", n).strip()
    if stripped and stripped != n:
        out.append(stripped)
    # Try adding "Jr." if we haven't already (ESPN sometimes carries
    # suffix while OddsAPI drops it).
    if not re.search(r"\s+(Jr\.?|Sr\.?|II|III|IV|V)$", n):
        out.append(f"{n} Jr.")
    return out


async def resolve_nfl_current_team_for_player(
    db, *, name: str, historical_team_fallback: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(current_team, historical_team, player_id)`` triplet.

    Priority order (highest first):
        1. ``db.players`` sport=nfl exact/variant match      →  current_team
        2. ``db.nfl_player_weekly`` most-recent-season row   →  historical_team + GSIS
    """
    if not name:
        return None, historical_team_fallback, None

    variants = _name_variants(name)
    current_team: Optional[str] = None
    gsis_id: Optional[str] = None
    historical_team: Optional[str] = None

    # ── 1. ESPN active-roster source (db.players) ─────────────────
    try:
        # Match either 'name' / 'display_name' / 'full_name' / 'canonical_name'
        # across variants.  There may be MULTIPLE rows per player (legacy
        # ingest + current ingest); pick the freshest by ``updated_at``.
        for v in variants:
            v_low = v.lower()
            cursor = db.players.find(
                {"sport": "nfl",
                 "$or": [
                     {"name": v},
                     {"display_name": v},
                     {"full_name": v},
                     {"canonical_name": v_low},
                 ]},
                {"_id": 0, "player_id": 1, "team": 1, "team_name": 1,
                 "name": 1, "updated_at": 1},
            ).sort("updated_at", -1).limit(5)
            rows = [r async for r in cursor]
            if not rows:
                continue
            # Prefer the freshest row that has ``team_name`` (full name).
            row = next((r for r in rows if r.get("team_name")), rows[0])
            t = (row.get("team_name") or row.get("team") or "").strip() or None
            if t:
                current_team = t
            pid = row.get("player_id")
            if pid and isinstance(pid, str) and pid.startswith("00-"):
                gsis_id = pid
            break
    except Exception as e:
        logger.debug("nfl players lookup err %s: %s", name, e)

    # ── 2. nfl_player_weekly (GSIS + historical team) ─────────────
    if not gsis_id or not historical_team:
        for field in ("player_display_name", "player_name"):
            for v in variants:
                try:
                    cursor = db.nfl_player_weekly.find(
                        {field: v},
                        {"_id": 0, "player_id": 1, "team": 1,
                         "season": 1, "week": 1},
                    ).sort([("season", -1), ("week", -1)]).limit(30)
                    rows = [d async for d in cursor]
                except Exception:
                    rows = []
                if not rows:
                    continue
                ids = {r.get("player_id") for r in rows if r.get("player_id")}
                # Prefer 2026 rows as current-team source; fall back to
                # newest row for historical_team.
                _2026 = next(
                    (r for r in rows if int(r.get("season") or 0) >= 2026),
                    None,
                )
                if _2026 and not current_team:
                    current_team = _2026.get("team") or None
                historical_team = historical_team or (
                    rows[0].get("team") if rows else None
                )
                # Only accept GSIS when unambiguous.
                if not gsis_id and len(ids) == 1:
                    gsis_id = next(iter(ids))
                elif not gsis_id and len(ids) > 1 and current_team:
                    # Narrow by resolved current team.
                    _upper = (current_team or "").upper()
                    matching = {
                        r["player_id"] for r in rows
                        if r.get("player_id") and (
                            (r.get("team") or "").upper() == _upper
                            or (r.get("team") or "").upper() in _upper
                        )
                    }
                    if len(matching) == 1:
                        gsis_id = next(iter(matching))
                break
            if gsis_id and historical_team:
                break

    return current_team, historical_team or historical_team_fallback, gsis_id


def _scale(value: float, low: float, high: float,
           out_low: float = 0.30, out_high: float = 0.95) -> float:
    """Linear scale a raw value into an [out_low, out_high] factor."""
    if high == low:
        return (out_low + out_high) / 2
    v = (value - low) / (high - low)
    v = max(0.0, min(1.0, v))
    return out_low + v * (out_high - out_low)


def has_enough_real_data_nfl(factors: dict) -> bool:
    """Return True iff at least MIN_FACTORS_NFL_PROP non-None real factors.
    Sidecar keys (leading ``__``) are metadata and never count toward the
    real-data minimum — the callers that consume ``factors`` for the
    factor mean apply the SAME exclusion so the two views stay honest.
    """
    return sum(
        1 for k, v in factors.items()
        if isinstance(v, (int, float)) and not str(k).startswith("__")
    ) >= MIN_FACTORS_NFL_PROP


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


def _factor_distribution_support(
    dist: Optional[dict], line: float, side: str = "over",
) -> Optional[float]:
    """Convert a coherent empirical distribution + threshold into a
    factor value in the 0.30-0.95 anchor range.

    * P̂(X ≥ threshold) 0.95+  → 0.95   (near-certain hit — mean far above line)
    * P̂ 0.75                 → 0.80
    * P̂ 0.55                 → 0.60
    * P̂ 0.25                 → 0.35
    * P̂ 0.05-                → 0.30

    None when the distribution has fewer than 3 games.
    """
    try:
        from services.nfl_features import distribution_hit_probability
        p_hat = distribution_hit_probability(dist, line, side)
    except Exception:
        return None
    if p_hat is None:
        return None
    # Direct linear projection onto the 0.30-0.95 anchor band so the
    # factor is a plain-english "how likely is THIS exact rung" signal.
    v = 0.30 + float(p_hat) * 0.65
    return round(max(0.30, min(0.95, v)), 3)


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
        player_stat_distribution, distribution_hit_probability,
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
    # ── 2026-06-09 · Stage-2 P0 · per-rung independent distribution ──
    # Empirical mean/SD of the last 12 regular-season games for this
    # exact stat_field.  Used by ``recompute_line_dependent_factors``
    # to produce a per-threshold P(X ≥ line) that varies coherently
    # by rung — same coherent distribution feeds every alt rung.
    _dist = None
    try:
        _dist = await player_stat_distribution(
            db, player, prop_stat, season, week, limit=12,
        )
    except Exception:
        _dist = None

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
        # ── 2026-06-09 · Stage-2 P0 · per-rung distribution support ──
        # Threshold-specific hit probability drawn from the coherent
        # 12-game empirical distribution.  Independent of the book
        # line — separates the "how likely is THIS threshold" signal
        # from the "how does the player usually perform" signals so
        # every rung earns its own evidence value.
        "Threshold Distribution Support": _factor_distribution_support(
            _dist, line, side,
        ),
        # 2026-08-23 NFL MODEL-INTEGRITY (Pass 2) — Book Implied removed
        # from the factor set.  It was the confirmed "silent book-implied
        # model authority" defect.  Preserved in `sources` only as an
        # audit hint (visible to the pick rationale) but no longer
        # participates in evidence scoring.
    }
    # ── 2026-06-09 · alt-line line-relative factor recompute ─────────
    # Preserve raw metrics that are LINE-INDEPENDENT so the sync
    # emission loop in ``sports_engine._props_picks_from_event`` can
    # rebuild the LINE-DEPENDENT factors (``L5 Avg vs Line`` and
    # ``Opponent Defense Allowance``) at the exact alt-line threshold
    # instead of reusing whichever threshold got iterated last in
    # ``build_nfl_game_context``.  This unblocks alt-lines that share
    # a market_key with different points (e.g. Player Rec Yds Over
    # 30.5 / 50.5 / 80.5 all key ``player_reception_yds_alternate``).
    # No new DB round-trips (all sourced from ``rolling``/``opp_pos``
    # already fetched above).  Under-side factor mirroring is applied
    # after this snapshot so recomputed Over factors are honestly
    # mirrored to Under.
    _raw_l5_avg = None
    try:
        _raw_l5_avg = ((rolling or {}).get("l5") or {}).get(prop_stat)
    except Exception:
        _raw_l5_avg = None
    _raw_opp_pos = opp_pos or {}
    # 2026-08-23 side-aware mirroring — every naturally Over-flavoured
    # factor above is mirrored on Under so evidence cannot silently
    # reward the opposite selected side (§Model-Integrity — "L5/L3/
    # trend/matchup evidence must be side-aware").
    # `Career vs Opponent Hit%` is already computed with `side` passed
    # to `player_prop_hit_rate_vs_opponent` upstream so it stays as-is.
    _side_norm = str(side or "over").lower()
    if _side_norm == "under":
        for _k in ("L5 Avg vs Line", "L3 vs Season Trend", "Home/Away Split",
                    "Opponent Defense Allowance"):
            v = factors.get(_k)
            if isinstance(v, (int, float)):
                factors[_k] = round(1.0 - v, 3)

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
    # Book Implied retained as audit-only source (removed from factors).
    if isinstance(book_implied, (int, float)):
        sources.append("odds_api_book_implied[audit_only]")

    # Line-independent raw metrics for alt-line line-relative recompute
    # in the sync emission loop (see sports_engine.py L6684 area).
    # Callers may ignore ``raw_l5_avg`` / ``raw_opp_pos`` — they are
    # additive and don't affect any existing consumer.
    _raw_metrics = {
        "raw_l5_avg": (
            round(float(_raw_l5_avg), 3)
            if isinstance(_raw_l5_avg, (int, float)) else None
        ),
        "raw_opp_pos": _raw_opp_pos or None,
        "prop_stat": prop_stat,
        "side": _side_norm,
        "position": position,
        "orig_line": float(line),
        # ── 2026-06-09 · Stage-2 P0 · per-rung distribution ─────────
        # Stashing the ENTIRE distribution (mean/sd/samples/n_games)
        # so the sync alt-line loop can recompute the "Threshold
        # Distribution Support" factor AT EACH RUNG using the same
        # coherent distribution.  Prevents the "one shared evidence
        # score across a 20-rung ladder" defect the audit surfaced.
        "distribution": _dist,
    }

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

    return factors, sources, _raw_metrics


__all__ = [
    "build_nfl_prop_factors",
    "build_nfl_game_context",
    "has_enough_real_data_nfl",
    "MIN_FACTORS_NFL_PROP",
    "recompute_line_dependent_factors",
]


def recompute_line_dependent_factors(
    factors: dict, raw_metrics: dict, *, line: float, side: str = "over",
) -> dict:
    """Rebuild ONLY the line-dependent NFL factors at a fresh ``line``
    using the raw metrics stashed by ``build_nfl_prop_factors``.

    ``L3 vs Season Trend``, ``Home/Away Split``, ``Career vs Opponent
    Hit%`` are line-INDEPENDENT and passed through unchanged.
    ``L5 Avg vs Line`` and ``Opponent Defense Allowance`` are rebuilt
    against ``line``.  Under-side mirror is reapplied consistently
    with the original build path.

    Called by the sync emission loop in
    ``sports_engine._props_picks_from_event`` so alt-line NFL props
    that share a market_key with different points get honestly
    evaluated at their own threshold (previously all alt rungs used
    whichever threshold was iterated last in the precompute).
    """
    if not raw_metrics or not isinstance(raw_metrics, dict):
        return dict(factors or {})
    stat = raw_metrics.get("prop_stat") or ""
    raw_l5 = raw_metrics.get("raw_l5_avg")
    opp_pos = raw_metrics.get("raw_opp_pos") or {}
    _dist = raw_metrics.get("distribution")
    # Rebuild L5 Avg vs Line
    new_factors = dict(factors or {})
    if isinstance(raw_l5, (int, float)) and stat:
        _fake_rolling = {"l5": {stat: raw_l5}}
        new_factors["L5 Avg vs Line"] = _factor_rolling_avg_vs_line(
            _fake_rolling, stat, line
        )
    if opp_pos and stat:
        new_factors["Opponent Defense Allowance"] = _factor_matchup(
            opp_pos, stat, line
        )
    # ── 2026-06-09 · Stage-2 P0 · per-rung distribution recompute ────
    # The stashed empirical distribution is line-INDEPENDENT (built
    # from raw game samples), but the resulting probability is
    # threshold-specific.  Rebuild the "Threshold Distribution Support"
    # factor at THIS rung so every alt line gets an honest, coherent
    # per-threshold hit probability.  Also stash the raw P̂(X ≥ line)
    # on the pick metadata via ``__rung_p_hat`` so the sync emission
    # loop can promote it into ``model_win_prob`` at the exact rung.
    if _dist:
        try:
            from services.nfl_features import distribution_hit_probability
            p_hat = distribution_hit_probability(_dist, line, side)
        except Exception:
            p_hat = None
        new_factors["Threshold Distribution Support"] = _factor_distribution_support(
            _dist, line, side,
        )
        # Sidecar payload the caller can consume without touching the
        # existing factor blender.  Sentinel key uses a `__` prefix
        # so downstream `_fv = [v for v in factors.values() if
        # isinstance(v, (int, float))]` naturally SKIPS the raw
        # probability (it stays a factor-blender-safe value only).
        new_factors["__rung_p_hat"] = float(p_hat) if p_hat is not None else None
    # Reapply Under-side mirror (Career vs Opponent Hit% is already
    # side-aware from the fetch call so we do NOT re-mirror it).
    if str(side or "over").lower() == "under":
        # Undo any pre-mirroring stashed by the precompute — the
        # precompute already mirrored Under factors *before* stashing.
        # Detect whether the original was in the Over frame by checking
        # ``raw_metrics.side`` == the current ``side``; if they agree,
        # the factors passed in are already correctly mirrored for the
        # current side and only the two rebuilt ones (Over frame) need
        # to be mirrored now.
        orig_side = str(raw_metrics.get("side") or "over").lower()
        if orig_side == "under":
            for _k in ("L5 Avg vs Line", "Opponent Defense Allowance"):
                v = new_factors.get(_k)
                if isinstance(v, (int, float)):
                    new_factors[_k] = round(1.0 - v, 3)
    return new_factors


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
    # Per-invocation memo cache — same player often appears across many
    # bookmakers / alt-lines (382 candidates for 30 unique players in a
    # single NFL event).  Without this cache we hit ``db.players`` +
    # ``nfl_player_weekly`` + ``resolve_nfl_position_for_player`` twice
    # per outcome → ~1500 DB round-trips per event.  With the cache we
    # collapse to O(unique_players).
    _team_cache: dict[str, tuple] = {}
    _pos_cache: dict[str, str] = {}

    for cand in prop_candidates:
        player = cand.get("player") or ""
        if not player:
            continue
        market = cand.get("market") or ""
        # Import inside to avoid circular
        from services.nfl_feature_engine import build_nfl_prop_factors
        # Block 2D · P0 (2026-06-09) — resolve CURRENT team for this
        # player (2026-season nfl_player_weekly row).  Used both for
        # opponent selection here AND to unlock the publication gate
        # downstream (see sports_engine.py ~L6913).
        cur_team = None
        hist_team = None
        gsis_id = None
        _pk = player.strip().lower()
        if _pk in _team_cache:
            cur_team, hist_team, gsis_id = _team_cache[_pk]
        else:
            try:
                cur_team, hist_team, gsis_id = await resolve_nfl_current_team_for_player(
                    db, name=player, historical_team_fallback=cand.get("team") or None,
                )
            except Exception as e:
                logger.debug("nfl current-team resolve err %s: %s", player, e)
            _team_cache[_pk] = (cur_team, hist_team, gsis_id)
        # Determine which team is the player's team → opponent + is_home
        # Prefer the resolved CURRENT team; fall back to cand-supplied.
        player_team = cur_team or cand.get("team") or ""
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
        if _pk in _pos_cache:
            canonical_pos = _pos_cache[_pk]
        else:
            try:
                from sports_engine import resolve_nfl_position_for_player
                canonical_pos = await resolve_nfl_position_for_player(
                    db, name=player, team=cand.get("team") or None)
            except Exception:
                canonical_pos = None
            _pos_cache[_pk] = canonical_pos
        position = canonical_pos or cand.get("position") or _infer_position(market)
        try:
            factors, sources, raw_metrics = await build_nfl_prop_factors(
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
                "raw_metrics": raw_metrics,   # for line-relative recompute
                "position_used": position,
                "position_source": ("canonical_registry"
                                     if canonical_pos else "market_inference"),
                # Block 2D · P0 — carry the resolved CURRENT team all
                # the way to the sync pick-emission stage so the pick
                # doc can attach ``player_team`` for the identity gate.
                "current_team":      cur_team,
                "historical_team":   hist_team,
                "canonical_player_id": gsis_id,
                "opponent_team":     opponent,
                "is_home":           is_home,
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
            # Block 2D · P0 (2026-06-09) — resolve CURRENT team FIRST so
            # the ATD candidate is anchored on 2026 roster truth (fixes
            # the stale "Etienne → Jaguars" class of regressions).  If
            # no current team, fail closed — a real bettable ATD needs
            # a current active-roster player.
            cur_team = None
            hist_team = None
            gsis_id = None
            try:
                cur_team, hist_team, gsis_id = \
                    await resolve_nfl_current_team_for_player(
                        db, name=player,
                        historical_team_fallback=cand.get("team") or None,
                    )
            except Exception as e:
                logger.debug("nfl_atd cur-team err %s: %s", player, e)
            if not cur_team:
                atd_out[key_l] = {
                    "reject": "current_team_unresolved",
                    "player_name": player,
                    "historical_team": hist_team,
                }
                continue
            # Reject if resolved current team isn't part of THIS event.
            # Membership validated against event home/away abbreviations.
            _home_ab = _abbrev(home_team).upper()
            _away_ab = _abbrev(away_team).upper()
            _cur_ab = _abbrev(cur_team).upper() if cur_team else ""
            if _cur_ab and _cur_ab not in (_home_ab, _away_ab):
                atd_out[key_l] = {
                    "reject": "current_team_not_in_event",
                    "player_name": player,
                    "current_team": cur_team,
                    "event": f"{away_team} @ {home_team}",
                }
                continue
            is_home_current = (_cur_ab == _home_ab)
            # Resolve to nflverse GSIS. Prefer GSIS learned above; if
            # missing, fall back to the historical resolver (may be
            # ambiguous — returns None then).
            pid = gsis_id
            if not pid:
                try:
                    pid = await resolve_player_id_from_name(
                        db, name=player, team=cur_team or None)
                except Exception as e:
                    logger.debug("nfl_atd resolve err %s: %s", player, e)
                    pid = None
            if not pid:
                atd_out[key_l] = {
                    "reject": "unresolved_player_identity",
                    "player_name": player,
                    "current_team": cur_team,
                }
                continue
            # Determine opponent based on CURRENT team (not historical
            # is_home from the outer loop).
            opp_full = away_team if is_home_current else home_team
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
            # Overwrite the engine's stale ``team`` field with the
            # resolved CURRENT team so downstream consumers never
            # display JAX-Etienne-style ghosts.  Historical team stays
            # available for audit under ``historical_team``.
            if isinstance(result, dict) and not result.get("reject"):
                result["team"] = cur_team
                result["historical_team"] = hist_team
                result["opponent"] = opp_abbrev
                result["is_home"] = is_home_current
                result["canonical_player_id"] = pid
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
