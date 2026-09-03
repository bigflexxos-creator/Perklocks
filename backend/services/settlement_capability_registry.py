"""SettlementCapabilityRegistry — sport × canonical market × required actuals
==============================================================================

PERKLOCKS-MAIN 34 · STEP 9 (minimal shared registry).

Complements the existing `settlement_capability.py` (which classifies
provider markets as supported/unsupported). This module adds the
SHARED authority declaration + a hard `is_gradeable(...)` guard so
adapters cannot convert a missing actual into a fake LOSS / zero / VOID.

CRITICAL invariant: missing actual data MUST become UNRESOLVED, never
automatic LOSS / zero / VOID. Callers use `is_gradeable(pick, actuals)`
to gate their settlement branch — when it returns False they emit an
UNRESOLVED settlement event with the returned `reason` code instead of
inventing a result.

This module does NOT rewrite existing sport-specific settlement
adapters. It supplies the SHARED capability declaration + the
`is_gradeable` guard so no adapter accidentally silences a missing-data
condition.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


REASON_MISSING_ACTUAL       = "MISSING_ACTUAL_DATA"
REASON_UNSUPPORTED_MARKET   = "UNSUPPORTED_MARKET"
REASON_EVENT_NOT_FINAL      = "EVENT_NOT_FINAL"
REASON_IDENTITY_FAILURE     = "IDENTITY_FAILURE"


@dataclass(frozen=True)
class SettlementAuthority:
    sport: str
    canonical_market_family: str
    required_actual_fields: Tuple[str, ...]
    primary_authority: str
    fallback_authorities: Tuple[str, ...] = ()


_REGISTRY: Dict[Tuple[str, str], SettlementAuthority] = {}


def register(cap: SettlementAuthority) -> None:
    key = (cap.sport, cap.canonical_market_family)
    if key in _REGISTRY:
        raise ValueError(f"duplicate SettlementAuthority: {key}")
    _REGISTRY[key] = cap


def get(sport: str, family: str) -> Optional[SettlementAuthority]:
    return _REGISTRY.get((sport, family))


def all_registrations() -> Dict[Tuple[str, str], SettlementAuthority]:
    return dict(_REGISTRY)


# ── Seed the currently ACTIVE surface (mirrors UniversalMarketContract) ──
_SEED = [
    SettlementAuthority("MLB", "moneyline",
        ("home_score", "away_score"), "mlb_statsapi", ("espn_scores",)),
    SettlementAuthority("MLB", "run_line",
        ("home_score", "away_score"), "mlb_statsapi", ("espn_scores",)),
    SettlementAuthority("MLB", "game_total",
        ("home_score", "away_score"), "mlb_statsapi", ()),
    SettlementAuthority("MLB", "hitter_hits",
        ("player_hits",), "mlb_statsapi", ("pitchapi",)),
    SettlementAuthority("MLB", "pitcher_strikeouts",
        ("player_strikeouts",), "mlb_statsapi", ()),
    # PERKLOCKS-MAIN 35 · FINAL — MLB player-prop settlement coverage.
    SettlementAuthority("MLB", "hitter_home_runs",
        ("player_home_runs",), "mlb_statsapi", ()),
    SettlementAuthority("MLB", "hitter_rbis",
        ("player_rbis",), "mlb_statsapi", ()),
    SettlementAuthority("MLB", "hitter_total_bases",
        ("player_total_bases",), "mlb_statsapi", ()),
    SettlementAuthority("MLB", "hitter_hits_runs_rbis",
        ("player_hits", "player_runs", "player_rbis"),
        "mlb_statsapi", ()),
    SettlementAuthority("MLB", "pitcher_outs",
        ("player_outs",), "mlb_statsapi", ()),
    SettlementAuthority("NFL", "moneyline",
        ("home_score", "away_score"), "espn_scores", ()),
    SettlementAuthority("NFL", "point_spread",
        ("home_score", "away_score"), "espn_scores", ()),
    SettlementAuthority("NFL", "game_total",
        ("home_score", "away_score"), "espn_scores", ()),
    SettlementAuthority("NFL", "wr_receiving_yards",
        ("player_receiving_yards",), "espn_boxscore", ()),
    SettlementAuthority("NFL", "wr_receptions",
        ("player_receptions",), "espn_boxscore", ()),
    SettlementAuthority("NFL", "qb_passing_yards",
        ("player_passing_yards",), "espn_boxscore", ()),
    SettlementAuthority("NFL", "qb_passing_tds",
        ("player_passing_tds",), "espn_boxscore", ()),
    SettlementAuthority("NFL", "rb_rushing_yards",
        ("player_rushing_yards",), "espn_boxscore", ()),
    SettlementAuthority("NFL", "player_anytime_td",
        ("player_rushing_tds", "player_receiving_tds"),
        "espn_boxscore", ()),
    SettlementAuthority("Tennis", "tennis_match_winner",
        ("match_winner",), "tennis_espn", ()),
    SettlementAuthority("Tennis", "tennis_total_games",
        ("total_games",), "tennis_espn", ()),
    SettlementAuthority("Tennis", "tennis_game_handicap",
        ("games_won_home", "games_won_away"), "tennis_espn", ()),
    SettlementAuthority("Soccer", "moneyline",
        ("home_score", "away_score"), "sportdb", ()),
    SettlementAuthority("Soccer", "game_total",
        ("home_score", "away_score"), "sportdb", ()),
    SettlementAuthority("Soccer", "handicap",
        ("home_score", "away_score"), "sportdb", ()),
    SettlementAuthority("Soccer", "btts",
        ("home_score", "away_score"), "sportdb", ()),
    SettlementAuthority("Soccer", "double_chance",
        ("home_score", "away_score"), "sportdb", ()),
    SettlementAuthority("Soccer", "goalscorer_anytime",
        ("player_goal_events",), "sportdb", ("understat",)),
    SettlementAuthority("Soccer", "goalscorer_score_or_assist",
        ("player_goal_events", "player_assist_events"),
        "sportdb", ("understat",)),
    SettlementAuthority("Soccer", "soccer_anytime_assist",
        ("player_assist_events",), "sportdb", ()),
    SettlementAuthority("CFB", "moneyline",
        ("home_score", "away_score"), "espn_scores", ()),
    SettlementAuthority("CFB", "point_spread",
        ("home_score", "away_score"), "espn_scores", ()),
    SettlementAuthority("CFB", "game_total",
        ("home_score", "away_score"), "espn_scores", ()),
]
for _cap in _SEED:
    register(_cap)


def is_gradeable(
    sport: str, canonical_market_family: str,
    event_final: bool, canonical_identity_resolved: bool,
    actuals: Dict[str, Any],
) -> Tuple[bool, str]:
    """Return `(gradeable, reason_code)`.

    * `gradeable=False, reason=UNSUPPORTED_MARKET`
    * `gradeable=False, reason=EVENT_NOT_FINAL`
    * `gradeable=False, reason=IDENTITY_FAILURE`
    * `gradeable=False, reason=MISSING_ACTUAL_DATA`
    * `gradeable=True,  reason=""`

    CRITICAL: on any False, callers MUST emit an UNRESOLVED settlement
    event with the returned reason. Never convert to LOSS / zero /
    forced VOID.
    """
    cap = _REGISTRY.get((sport, canonical_market_family))
    if cap is None:
        return False, REASON_UNSUPPORTED_MARKET
    if not event_final:
        return False, REASON_EVENT_NOT_FINAL
    if not canonical_identity_resolved:
        return False, REASON_IDENTITY_FAILURE
    for f in cap.required_actual_fields:
        if actuals.get(f) is None:
            return False, REASON_MISSING_ACTUAL
    return True, ""


def coverage_row(sport: str, canonical_market_family: str,
                  counts: Dict[str, int]) -> Dict[str, Any]:
    """Build one row of the settlement coverage matrix."""
    cap = get(sport, canonical_market_family)
    return {
        "sport": sport,
        "canonical_market_family": canonical_market_family,
        "primary_authority": cap.primary_authority if cap else None,
        "fallback_authorities": list(cap.fallback_authorities) if cap else [],
        "required_actuals": list(cap.required_actual_fields) if cap else [],
        "published":         int(counts.get("published", 0)),
        "gradeable":         int(counts.get("gradeable", 0)),
        "settled":           int(counts.get("settled", 0)),
        "unresolved":        int(counts.get("unresolved", 0)),
        "missing_actual":    int(counts.get("missing_actual", 0)),
        "identity_failure":  int(counts.get("identity_failure", 0)),
        "unsupported_rule":  int(counts.get("unsupported_rule", 0)),
        "provider_failure":  int(counts.get("provider_failure", 0)),
    }
