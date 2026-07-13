"""Soccer Deep Signals — Signal Engine Phase B.4.

Adds soccer-specific evidence layers on top of Phase A's universal
signals. Same additive design as `mlb_deep.py`:

  • Zero new external API calls at request time — every field we read
    is already populated on the pick document by upstream pipelines
    (Understat form loop, sim engine, xG-differential enrichers).
  • Reads existing enrichment (`understat_form`, `factors.xG Difference`,
    etc.) and combines them into signals Phase A doesn't touch.
  • Non-soccer picks or picks missing the source data → neutral 0-point
    block (identical fallback to Phase A calculators).

What we add per soccer pick:

  1. `xg_regression`   — HOT scorer with G/xG > 1.30 is due to regress
                         COLD; COLD scorer with G/xG < 0.75 is due for
                         POSITIVE regression. Different signal from
                         Phase A's HOT/COLD label — that just reflects
                         the trailing form; regression predicts the
                         reversal.
  2. `xg_differential` — For team picks read `factors["xG Difference"]`
                         and `factors["xGA Difference"]`. When both
                         lean strongly in the pick's favour, that's
                         a durable underlying-quality edge that book
                         moneylines under-price.
  3. `home_edge`       — For team picks in home/away markets, read
                         `factors["Home Advantage"]`. Big home-side
                         advantages (>75) in leagues known for home
                         cooking (Turkey, Greece, Argentina, MLS,
                         Liga MX) get a small bump.
  4. `league_tier`     — Top-5 EU + top continental competitions get
                         a small confidence bump (their data is
                         well-covered by Understat / Opta feeds).
                         Lower-tier leagues get a very small penalty
                         reflecting higher model variance.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.services.signal_engine.soccer_deep")


# ──────────────────────────────────────────────────────────────────────
# League tier table (data-quality proxy)
# ──────────────────────────────────────────────────────────────────────
# TIER_1 = full Understat/Opta coverage; model has 100+ matches of
# calibration per league.
_TIER_1_LEAGUES = {
    "premier league", "epl", "la liga", "laliga", "serie a", "bundesliga",
    "ligue 1", "uefa champions league", "uefa europa league",
    "uefa conference league", "champions league", "europa league",
    "conference league",
}

# TIER_2 = partial Understat coverage (MLS, Championship, Eredivisie,
# Primeira Liga, J1, K-League 1).
_TIER_2_LEAGUES = {
    "mls", "efl championship", "championship", "eredivisie",
    "primeira liga", "j1 league", "j-league", "k-league",
    "belgian pro league", "jupiler pro league",
}

# TIER_3 = model coverage from thesportsdb / wiki scrapes only; higher
# variance. Includes Nordic leagues, Brazilian Série B, CSL, Liga MX,
# lower-tier internationals.
_TIER_3_LEAGUES = {
    "allsvenskan", "eliteserien", "veikkausliiga", "brasileirão",
    "brasileirao", "brasileirão série b", "china super league",
    "chinese super league", "liga mx", "argentine primera",
    "primera nacional", "usl championship", "canadian premier",
    "brasileirão série c",
}

# Leagues where home advantage historically matters more than average
# (crowd noise / travel / referee bias). Empirical from 5-year top-25
# leagues home-win % ranking.
_HIGH_HOME_ADV_LEAGUES = {
    "turkish super lig", "super lig", "greek super league",
    "argentine primera", "liga mx", "mls", "brasileirão",
    "brasileirao", "chinese super league", "j1 league",
}


def _league_tier(league: str) -> int:
    """Returns 1 (top), 2 (mid), or 3 (long-tail) for a soccer league.
    Uses the FIRST matching tier's substring match — regionals get
    tier-3 by default."""
    if not league:
        return 3
    ll = league.lower()
    if any(t in ll for t in _TIER_1_LEAGUES):
        return 1
    if any(t in ll for t in _TIER_2_LEAGUES):
        return 2
    if any(t in ll for t in _TIER_3_LEAGUES):
        return 3
    # Anything else — treat as tier-3 (unknown / regional).
    return 3


def _is_high_home_adv_league(league: str) -> bool:
    if not league:
        return False
    ll = league.lower()
    return any(t in ll for t in _HIGH_HOME_ADV_LEAGUES)


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────────────────────────────────
# Public enrichment API
# ──────────────────────────────────────────────────────────────────────
def enrich_soccer_pick(pick: dict) -> dict:
    """Add `soccer_deep` block to a single soccer pick. Idempotent.
    Returns the pick (mutated). No-op if the pick is not Soccer or
    all source fields are missing (defensive — no soccer_deep block
    at all rather than an empty stub so downstream can trust its
    presence as a "has data" signal).

    Attaches:
      pick['soccer_deep'] = {
        'league_tier':      1|2|3,
        'high_home_adv':    bool,
        'xg_regression':    None | 'due_hot' | 'due_cold',
        'goals_over_xg':    float | None,       # from understat_form
        'xg_diff':          float | None,       # from factors
        'xga_diff':         float | None,
        'home_adv':         float | None,       # from factors
      }
    """
    if (pick.get("sport") or "").lower() != "soccer":
        return pick

    league = pick.get("league") or ""
    factors = pick.get("factors") or {}
    understat = pick.get("understat_form") or {}

    goals_over_xg = None
    if isinstance(understat, dict):
        gox = understat.get("goals_over_xg")
        if isinstance(gox, (int, float)):
            goals_over_xg = float(gox)

    # xG-regression classification: HOT scorers who are massively
    # over-performing xG regress; COLD scorers who are massively
    # under-performing rebound. Threshold picked from Understat's
    # published regression band (±30% of xG = statistical noise, ±30-50%
    # = trending, ±50%+ = due for reversion).
    xg_regression: Optional[str] = None
    if goals_over_xg is not None and isinstance(understat, dict):
        games = int(_f(understat.get("games")))
        # Need at least 10 games so we're not fooled by a hot 3-game
        # sample. Under-performers with high xG production are the
        # cleanest buy signal in the model.
        if games >= 10:
            if goals_over_xg >= 1.35:
                xg_regression = "due_cold"
            elif goals_over_xg <= 0.70:
                xg_regression = "due_hot"

    xg_diff = None
    xga_diff = None
    home_adv = None
    if isinstance(factors, dict):
        xg_raw = factors.get("xG Difference")
        if isinstance(xg_raw, (int, float)):
            xg_diff = float(xg_raw)
        xga_raw = factors.get("xGA Difference")
        if isinstance(xga_raw, (int, float)):
            xga_diff = float(xga_raw)
        ha_raw = factors.get("Home Advantage")
        if isinstance(ha_raw, (int, float)):
            home_adv = float(ha_raw)

    # If ALL source fields are missing, don't attach the block — keeps
    # `soccer_deep` presence a reliable "we have data" signal.
    if (goals_over_xg is None and xg_diff is None and xga_diff is None
            and home_adv is None):
        # Still attach tier info — it's a cheap constant.
        pick["soccer_deep"] = {
            "league_tier":   _league_tier(league),
            "high_home_adv": _is_high_home_adv_league(league),
            "xg_regression": None,
            "goals_over_xg": None,
            "xg_diff":       None,
            "xga_diff":      None,
            "home_adv":      None,
        }
        return pick

    pick["soccer_deep"] = {
        "league_tier":   _league_tier(league),
        "high_home_adv": _is_high_home_adv_league(league),
        "xg_regression": xg_regression,
        "goals_over_xg": round(goals_over_xg, 3) if goals_over_xg is not None else None,
        "xg_diff":       round(xg_diff, 2) if xg_diff is not None else None,
        "xga_diff":      round(xga_diff, 2) if xga_diff is not None else None,
        "home_adv":      round(home_adv, 2) if home_adv is not None else None,
    }
    return pick


def enrich_soccer_picks_bulk(picks: list[dict]) -> int:
    if not picks:
        return 0
    n = 0
    for p in picks:
        try:
            before = "soccer_deep" in p
            enrich_soccer_pick(p)
            if "soccer_deep" in p and not before:
                n += 1
        except Exception as e:
            logger.debug("soccer_deep enrich failed for %s: %s", p.get("id"), e)
    return n
