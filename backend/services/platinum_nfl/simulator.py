"""Platinum NFL simulator entry point + Champion/Challenger provenance
(Block 2B.1A §16, §20, §22, §32).

CHAMPION / CHALLENGER contract
──────────────────────────────
Champion   = existing NFL production model / probability (the
             ``model_probability`` field on the pick).
Challenger = Platinum causal-chain simulator output produced here.

Attachment rule (§16, §20):
    * Champion output remains untouched.  ``model_probability`` is
      NEVER overwritten.
    * Challenger output is stamped under
      ``pick["platinum_challenger"]`` with a full FROZEN row so it
      can be replayed / audited without recomputation.
    * ``pick["sim_probability"]`` is stamped ONLY when Challenger
      ran successfully — never = model_probability on failure.

Failure contract (§32):
    ran=False → sim_probability is NULL.  Never fake agreement.

Time-aware truth (§22):
    Attachment stores ``prediction_timestamp`` at the moment of
    simulation.  Downstream calibration must respect this timestamp
    and never use post-hoc info.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from services.platinum_nfl.season_type import (
    SeasonType, classify_season_type,
)
from services.platinum_nfl.football_core import (
    sim_seed, expected_possessions, expected_plays,
    LEAGUE_PASS_RATE_NEUTRAL,
)
from services.platinum_nfl.opportunity import (
    QBOpportunity, RBOpportunity, WROpportunity, apply_preseason_regime,
)
from services.platinum_nfl.game_markets import simulate_game_market
from services.platinum_nfl.player_markets import simulate_player_market


PLATINUM_NAME    = "platinum_nfl"
PLATINUM_VERSION = "2b.1a.v1"
PLATINUM_TYPE    = "causal_monte_carlo"


# ═════════════════════════════════════════════════════════════════════
# Frozen Champion/Challenger row (§20, §22)
# ═════════════════════════════════════════════════════════════════════
@dataclass
class ChampionChallengerFrozenRow:
    """Immutable audit row emitted with every Challenger evaluation.
    Preserved for later validation (§21) without recomputation.
    """
    prediction_timestamp: str
    event_id:             Optional[str]
    market:               Optional[str]
    side:                 Optional[str]
    line:                 Optional[float]
    odds:                 Optional[float]
    season_type:          str
    champion_probability: Optional[float]
    challenger_probability: Optional[float]
    challenger_version:   str
    challenger_ran:       bool
    challenger_reason:    Optional[str]
    challenger_summary:   dict
    role_evidence:        dict
    input_provenance:     dict


# ═════════════════════════════════════════════════════════════════════
# Main entry point
# ═════════════════════════════════════════════════════════════════════
def simulate(
    pick: dict,
    *,
    ctx: Optional[dict] = None,
    seed: Optional[int] = None,
    n_sims: int = 5000,
) -> dict:
    """Run the Platinum NFL simulator on a single candidate pick.

    Parameters
    ----------
    pick
        The NFL candidate (as produced by the runtime).  Must carry
        ``sport``, ``market``, ``side``, ``line`` for player/game
        markets.  Must also carry ``home_team``, ``away_team``,
        ``event_time`` or ``commence_time`` for season detection.
    ctx
        Optional runtime context.  Consumed keys:
            expected_margin_home:  float   (game markets)
            total_line:            float   (game markets)
            expected_pace_home:    float   (sec/play)
            expected_pace_away:    float   (sec/play)
            qb_opportunity:        QBOpportunity
            rb_opportunity:        RBOpportunity
            wr_opportunity:        WROpportunity
            position:              "QB"|"RB"|"WR"|"TE"
            team_side:             "home"|"away"     — for the player's team
            season_type:           SeasonType (override; else classify)
            role_certainty:        float
            role_evidence:         dict
    seed
        Deterministic seed (§33).  Same seed + same pick + same ctx →
        reproducible output.  Default derives from (event_id, market,
        side, line).
    n_sims
        Monte Carlo sample count.  Default 5000.

    Returns
    -------
    A structured simulator output dict.  On any failure returns
    the ``ran=False`` shape.  NEVER writes ``sim_probability =
    model_probability`` (§32).
    """
    ctx = ctx or {}
    if str(pick.get("sport") or "").upper() != "NFL":
        return _failed(pick, "WRONG_SPORT")

    # ── Season type (§10) ────────────────────────────────────────
    st: SeasonType = ctx.get("season_type") or classify_season_type(pick)
    if not isinstance(st, SeasonType):
        st = SeasonType.UNKNOWN
    if st is SeasonType.UNKNOWN:
        return _failed(pick, "SEASON_TYPE_UNKNOWN",
                       season_type=st.value)

    # ── Seed ─────────────────────────────────────────────────────
    if seed is None:
        base = 0xC0FFEE
    else:
        base = int(seed)
    rng = sim_seed(base, pick.get("event_id") or pick.get("event"),
                   pick.get("market"), pick.get("side"),
                   pick.get("line"), pick.get("player_name") or "",
                   PLATINUM_VERSION, st.value)

    market = str(pick.get("market") or "").lower()
    is_game_market = any(k in market for k in (
        "moneyline", "spread", "total"
    )) and not any(k in market for k in (
        "passing", "rushing", "receiving", "receptions",
        "targets", "carries", "anytime",
    ))

    # ── Route to appropriate simulator ───────────────────────────
    if is_game_market:
        out = simulate_game_market(
            pick,
            expected_margin_home=ctx.get("expected_margin_home"),
            total_line=ctx.get("total_line"),
            seed=rng, n_sims=n_sims,
        )
    else:
        position = str(ctx.get("position") or _infer_position(pick) or "").upper()
        opp = _resolve_opportunity(ctx, position, st)
        if opp is None:
            return _failed(pick, "MISSING_OPPORTUNITY", season_type=st.value)
        # Preseason regime (§11).
        if st is SeasonType.PRESEASON:
            apply_preseason_regime(opp)
        team_plays = float(ctx.get("team_plays") or _default_team_plays(st))
        pass_rate  = float(ctx.get("game_pass_rate")
                            or LEAGUE_PASS_RATE_NEUTRAL)
        out = simulate_player_market(
            pick, opportunity=opp,
            team_plays=team_plays, game_pass_rate=pass_rate,
            position=position, seed=rng, n_sims=n_sims,
        )

    # ── Stamp provenance + versioning ────────────────────────────
    out["simulator_name"]    = PLATINUM_NAME
    out["simulator_version"] = PLATINUM_VERSION
    out["simulator_type"]    = PLATINUM_TYPE
    out["season_type"]       = st.value
    out["role_uncertainty"]  = _role_uncertainty(ctx, st)
    out["input_provenance"]  = _input_provenance(pick, ctx)
    return out


def _resolve_opportunity(ctx: dict, position: str, st: SeasonType):
    """Fetch or synthesize a role/opportunity object from ctx.
    NEVER fabricates a role; if ctx doesn't carry one AND the
    position is unrecognized, returns None so the sim fails safely.
    """
    key = {"QB": "qb_opportunity", "RB": "rb_opportunity",
            "WR": "wr_opportunity", "TE": "wr_opportunity"}.get(position)
    if not key:
        return None
    opp = ctx.get(key)
    if opp is not None:
        return opp
    # Only synthesize a neutral opportunity if the position is
    # known — this avoids fabricating a role that doesn't exist.
    if position == "QB":
        return QBOpportunity(role_certainty=0.85)
    if position == "RB":
        return RBOpportunity(role_certainty=0.75)
    if position in ("WR", "TE"):
        return WROpportunity(role_certainty=0.75)
    return None


def _infer_position(pick: dict) -> Optional[str]:
    m = str(pick.get("market") or "").lower()
    if "pass" in m:
        return "QB"
    if "rush" in m or "carr" in m:
        return "RB"
    if "receiv" in m or "reception" in m or "target" in m:
        return "WR"
    if "anytime_td" in m or "anytime td" in m:
        return None
    return None


def _default_team_plays(st: SeasonType) -> float:
    return {
        SeasonType.PRESEASON:      54.0,
        SeasonType.REGULAR_SEASON: 65.0,
        SeasonType.POSTSEASON:     67.0,
    }.get(st, 60.0)


def _role_uncertainty(ctx: dict, st: SeasonType) -> float:
    """Return a 0..1 role uncertainty scalar for provenance."""
    ru = ctx.get("role_certainty")
    if ru is None:
        return 0.35 if st is SeasonType.PRESEASON else 0.10
    return max(0.0, min(1.0, 1.0 - float(ru)))


def _input_provenance(pick: dict, ctx: dict) -> dict:
    """Return the frozen input snapshot for later replay (§22)."""
    return {
        "event_id":     pick.get("event_id") or pick.get("event"),
        "home_team":    pick.get("home_team"),
        "away_team":    pick.get("away_team"),
        "event_time":   pick.get("event_time") or pick.get("commence_time"),
        "market":       pick.get("market"),
        "side":         pick.get("side"),
        "line":         pick.get("line"),
        "book_odds":    pick.get("book_odds"),
        "sportsbook":   pick.get("sportsbook"),
        "player_name":  pick.get("player_name"),
        "ctx_keys":     sorted(list((ctx or {}).keys())),
    }


def _failed(pick: dict, reason: str, **extra) -> dict:
    """§32 failure contract."""
    return {
        "ran":              False,
        "reason":           reason,
        "sim_probability":  None,
        "simulator_name":   PLATINUM_NAME,
        "simulator_version": PLATINUM_VERSION,
        "simulator_type":   PLATINUM_TYPE,
        "input_provenance": _input_provenance(pick, {}),
        **extra,
    }


# ═════════════════════════════════════════════════════════════════════
# Champion/Challenger attachment (§20)
# ═════════════════════════════════════════════════════════════════════
def attach_challenger_output(pick: dict, sim_output: dict,
                              *, role_evidence: Optional[dict] = None,
                              ) -> dict:
    """Attach the Platinum Challenger output to ``pick`` under an
    explicit ``platinum_challenger`` block.  Does NOT overwrite
    ``model_probability`` or ``sim_probability`` unless the sim ran
    successfully.  Idempotent within a single call.

    Also stamps ``pick["champion_challenger"]`` with a frozen row
    for later validation.
    """
    pick.setdefault("platinum_challenger", copy.deepcopy(sim_output))
    frozen = ChampionChallengerFrozenRow(
        prediction_timestamp=datetime.now(timezone.utc).isoformat(),
        event_id=pick.get("event_id") or pick.get("event"),
        market=pick.get("market"),
        side=pick.get("side"),
        line=pick.get("line"),
        odds=pick.get("book_odds"),
        season_type=sim_output.get("season_type", "UNKNOWN"),
        champion_probability=pick.get("model_probability"),
        challenger_probability=(sim_output.get("sim_probability")
                                 if sim_output.get("ran") else None),
        challenger_version=PLATINUM_VERSION,
        challenger_ran=bool(sim_output.get("ran")),
        challenger_reason=sim_output.get("reason"),
        challenger_summary={
            k: sim_output.get(k)
            for k in ("distribution_mean", "distribution_median",
                       "q10", "q25", "q75", "q90",
                       "variance", "std", "simulation_count",
                       "market_threshold", "role_uncertainty")
            if k in sim_output
        },
        role_evidence=role_evidence or {},
        input_provenance=sim_output.get("input_provenance", {}),
    )
    # Store the frozen row on the pick for auditability but do NOT
    # let it overwrite existing champion_challenger blocks from
    # other simulators — key by simulator_name.
    pick.setdefault("champion_challenger", {})
    pick["champion_challenger"][PLATINUM_NAME] = asdict(frozen)
    # Only stamp the top-level sim_probability when Challenger ran
    # successfully.  Contract §32 explicitly forbids assigning
    # sim_probability equal to model_probability on failure — the
    # code path below only executes on ``ran=True`` and always uses
    # the Challenger's own value, never Champion's.
    if sim_output.get("ran") and sim_output.get("sim_probability") is not None:
        # Keep whatever sim_probability the caller already set (e.g.
        # from the empirical Magic 3H simulator, which is a
        # SEPARATE Challenger).  Only set it when nothing else has.
        # This makes Platinum the DEFAULT sim source in 2B.1B when
        # runtime wiring adopts it, without stomping on other
        # simulators during the coexistence window.
        pick.setdefault("sim_probability", float(sim_output["sim_probability"]))
        pick.setdefault("simulator_version_stamped", PLATINUM_VERSION)
    return pick


__all__ = [
    "PLATINUM_NAME", "PLATINUM_VERSION", "PLATINUM_TYPE",
    "simulate", "attach_challenger_output",
    "ChampionChallengerFrozenRow",
]
