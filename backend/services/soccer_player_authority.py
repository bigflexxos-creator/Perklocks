"""Soccer Player Authority & Goalscorer Model
──────────────────────────────────────────────────────────────────
Session 10 · SOCCER FINAL ROOT CLOSURE (2026-09-16).

Replaces league-name authority with PLAYER × FIXTURE evidence authority.
Everything below is deliberately league-agnostic — a real bookmaker
goalscorer from ANY supported current fixture goes through the same
evidence-driven qualification path.

Public entry points:
    * classify_authority(evidence)   → FULL / STRONG / LIMITED / INSUFFICIENT
    * estimate_player_lambda(evidence, team_lambda) → per-match player λ
    * price_atg(lambda_player, minutes_prob)  → P(≥1 goal)
    * price_score_or_assist(evidence, lambda_player, team_lambda)
    * enumerate_terminal_reason(evidence)     → single primary reason

Contracts:
    * League NAME is never an eligibility gate.  It may influence
      data reliability normalization only.
    * Missing evidence is MISSING — never zero-imputed, never
      fabricated.
    * Expected minutes has explicit truthful states; UNKNOWN cannot
      earn full elite authority.
    * Evidence families are provenance-tagged so correlated
      derivatives don't masquerade as independent votes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════
# Authority state machine
# ═══════════════════════════════════════════════════════════════════
class Authority(str, Enum):
    """Goalscorer authority states — replaces league-name whitelist."""
    FULL         = "FULL"          # excellent player + minutes + team + opp evidence
    STRONG       = "STRONG"        # enough for defensible probability; minor gaps
    LIMITED      = "LIMITED"       # real market + identity + partial evidence
    INSUFFICIENT = "INSUFFICIENT"  # cannot independently price responsibly


class MinutesState(str, Enum):
    CONFIRMED_STARTER  = "CONFIRMED_STARTER"
    PROJECTED_STARTER  = "PROJECTED_STARTER"
    ROTATION_RISK      = "ROTATION_RISK"
    BENCH_EXPECTED     = "BENCH_EXPECTED"
    UNKNOWN            = "UNKNOWN"


class PenaltyRole(str, Enum):
    PRIMARY    = "PRIMARY"
    SECONDARY  = "SECONDARY"
    NONE       = "NONE"
    UNKNOWN    = "UNKNOWN"


class TerminalReason(str, Enum):
    STALE_EVENT                    = "STALE_EVENT"
    EVENT_IDENTITY_FAILED          = "EVENT_IDENTITY_FAILED"
    TEAM_IDENTITY_FAILED           = "TEAM_IDENTITY_FAILED"
    PLAYER_IDENTITY_FAILED         = "PLAYER_IDENTITY_FAILED"
    # ── Session 10.1 Live-Truth Closure (2026-09-16) ─────────────
    # Current-event vs canonical current-team invariants.
    CURRENT_TEAM_MISMATCH          = "CURRENT_TEAM_MISMATCH"    # player is on team_X but event has teams (A, B) with X∉{A,B}
    STALE_PLAYER_TEAM              = "STALE_PLAYER_TEAM"        # pick's "team" field is the player's FORMER team
    STALE_PLAYER_EVENT             = "STALE_PLAYER_EVENT"       # event slate uses a former-competition context
    NO_REAL_ODDS                   = "NO_REAL_ODDS"
    ODDS_POLICY                    = "ODDS_POLICY"
    EXPECTED_MINUTES_MISSING       = "EXPECTED_MINUTES_MISSING"
    INSUFFICIENT_PLAYER_EVIDENCE   = "INSUFFICIENT_PLAYER_EVIDENCE"
    MODEL_NOT_RUN                  = "MODEL_NOT_RUN"
    MODEL_PROBABILITY_MISSING      = "MODEL_PROBABILITY_MISSING"
    UEA_COVERAGE                   = "UEA_COVERAGE"
    UEA_CEILING                    = "UEA_CEILING"
    LOCK_SCORE_BELOW_85            = "LOCK_SCORE_BELOW_85"
    OFF_BOARD                      = "OFF_BOARD"
    PUBLICATION_FILTER             = "PUBLICATION_FILTER"
    DEDUPE_COLLISION               = "DEDUPE_COLLISION"
    MARKET_MAPPING                 = "MARKET_MAPPING"
    OTHER                          = "OTHER"


# ═══════════════════════════════════════════════════════════════════
# Session 10.1 · Current-team invariant helpers
# ─────────────────────────────────────────────────────────────────
# Historical player logs describe FORM only.  They MUST NOT establish
# current roster membership — Robbie Ure (Sirius → Sevilla July 2026)
# is a live example of the failure mode where prior-season scoring
# history keeps generating "hot scorer" picks for the FORMER team.
#
# `verify_current_team` is the single choke-point: given a player and
# an event (home_team, away_team) plus a canonical current_team
# resolved from a transfer registry, decide whether the pick is
# CURRENT, STALE_PLAYER_TEAM, or CURRENT_TEAM_MISMATCH.
# ═══════════════════════════════════════════════════════════════════
def _normalize_team_name(s: Optional[str]) -> str:
    if not s: return ""
    return "".join(c for c in s.lower().strip() if c.isalnum() or c.isspace())


def verify_current_team(
    player_name: Optional[str],
    canonical_current_team: Optional[str],
    event_home_team: Optional[str],
    event_away_team: Optional[str],
    pick_team_hint: Optional[str] = None,
) -> tuple[bool, Optional[TerminalReason], str]:
    """Return (is_current, terminal_reason_if_stale, note).

    Contract:
      * When `canonical_current_team` is UNKNOWN we return
        (True, None, "unverified_no_registry") — fail-open at this
        layer.  Higher layers may still require registry hits for
        elite reachability, but we do not delete legitimate picks
        just because our roster registry hasn't been populated yet.
      * When registry knows the current team:
          - if it matches EITHER event side → CURRENT (pass)
          - if it matches the pick_team_hint but NOT any event side
            → STALE_PLAYER_EVENT (event is from former competition)
          - otherwise → CURRENT_TEAM_MISMATCH
      * When `pick_team_hint` is the player's FORMER team and
        registry has a different current_team → STALE_PLAYER_TEAM
    """
    if not player_name:
        return (False, TerminalReason.PLAYER_IDENTITY_FAILED,
                "no_player_name")
    if not canonical_current_team:
        return (True, None, "unverified_no_registry")
    cur = _normalize_team_name(canonical_current_team)
    h   = _normalize_team_name(event_home_team)
    a   = _normalize_team_name(event_away_team)
    hint = _normalize_team_name(pick_team_hint)
    # Loose containment either direction to handle canonical short/long forms.
    def _same(x, y):
        if not x or not y: return False
        return x == y or x in y or y in x
    if _same(cur, h) or _same(cur, a):
        return (True, None, f"current_team_matches_event:{canonical_current_team}")
    if hint and (_same(hint, h) or _same(hint, a)):
        # Event side matches the pick's declared team hint (former team)
        # but the canonical current team is neither event side.
        return (False, TerminalReason.STALE_PLAYER_TEAM,
                f"pick_hints_former_team_{pick_team_hint}_but_current_is_{canonical_current_team}")
    return (False, TerminalReason.CURRENT_TEAM_MISMATCH,
            f"current_team_{canonical_current_team}_absent_from_event_"
            f"{event_home_team}_vs_{event_away_team}")


# ═══════════════════════════════════════════════════════════════════
# Evidence container
# ═══════════════════════════════════════════════════════════════════
@dataclass
class PlayerEvidence:
    """Every field is Optional — MISSING means None.  Never fabricated."""
    # Identity
    player_name:       Optional[str] = None
    player_id:         Optional[str] = None
    team:              Optional[str] = None
    opponent:          Optional[str] = None
    event_id:          Optional[str] = None
    league:            Optional[str] = None
    is_home:           Optional[bool] = None
    # Market
    book_odds:         Optional[float] = None      # American odds
    market_implied:    Optional[float] = None      # 0..1
    devig_implied:     Optional[float] = None      # 0..1 (once both sides seen)
    # Playing time
    minutes_state:     MinutesState = MinutesState.UNKNOWN
    expected_minutes:  Optional[float] = None      # 0..90
    starter_prob:      Optional[float] = None      # 0..1
    lineup_confirmed:  bool = False
    # Scoring history (rate-per-90 unless otherwise noted)
    goals_per_90:      Optional[float] = None
    npxg_per_90:       Optional[float] = None
    xg_per_90:         Optional[float] = None
    shots_per_90:      Optional[float] = None
    sot_per_90:        Optional[float] = None
    touches_in_box_p90:Optional[float] = None
    sample_matches:    Optional[int]   = None
    # Assists
    xa_per_90:         Optional[float] = None
    assists_per_90:    Optional[float] = None
    key_passes_per_90: Optional[float] = None
    # Role
    penalty_role:      PenaltyRole = PenaltyRole.UNKNOWN
    role_stability:    Optional[float] = None      # 0..1
    # Team / opponent env
    team_lambda:       Optional[float] = None      # team expected goals this match
    opp_def_strength:  Optional[float] = None      # goals conceded per match adj
    # League normalization (does NOT gate — only informs reliability)
    league_reliability:Optional[float] = None      # 0..1 tier of data source
    # Provenance
    evidence_families: list[str] = field(default_factory=list)
    missing_flags:     list[str] = field(default_factory=list)

    def has(self, *fields: str) -> bool:
        return all(getattr(self, f, None) is not None for f in fields)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["minutes_state"] = self.minutes_state.value
        d["penalty_role"]  = self.penalty_role.value
        return d


# ═══════════════════════════════════════════════════════════════════
# Authority classifier
# ═══════════════════════════════════════════════════════════════════
def classify_authority(ev: PlayerEvidence) -> Authority:
    """Classify player evidence into an authority state.

    NEVER reads league name as a gate.  Uses evidence coverage only.
    """
    # ── INSUFFICIENT gate — anything below this can't price responsibly ──
    if not ev.player_name or not ev.event_id:
        return Authority.INSUFFICIENT
    if ev.book_odds is None:
        return Authority.INSUFFICIENT
    # Must have SOME scoring rate signal (any of goals/xG/npxG/shots).
    has_scoring_signal = any(v is not None for v in (
        ev.goals_per_90, ev.xg_per_90, ev.npxg_per_90, ev.shots_per_90,
    ))
    if not has_scoring_signal:
        return Authority.INSUFFICIENT
    # Team env is essential for coherence.
    if ev.team_lambda is None:
        return Authority.INSUFFICIENT

    # ── LIMITED — partial coverage / high uncertainty ────────────────
    signals_present = sum(1 for v in (
        ev.goals_per_90, ev.xg_per_90, ev.npxg_per_90, ev.shots_per_90,
        ev.sot_per_90, ev.touches_in_box_p90,
    ) if v is not None)
    reliability = ev.league_reliability if ev.league_reliability is not None else 0.5
    sample_n = ev.sample_matches or 0

    if ev.minutes_state == MinutesState.UNKNOWN and not ev.starter_prob:
        # unknown minutes cannot be FULL or STRONG
        return Authority.LIMITED
    if signals_present < 2 or sample_n < 5:
        return Authority.LIMITED

    # ── STRONG — enough for defensible probability, some gaps ────────
    if signals_present < 4 or reliability < 0.75 or sample_n < 10:
        return Authority.STRONG
    if ev.minutes_state == MinutesState.ROTATION_RISK:
        return Authority.STRONG

    # ── FULL — every family present with usable reliability ─────────
    return Authority.FULL


# ═══════════════════════════════════════════════════════════════════
# Player λ estimation — the SAME shape as team λ, coherent by design
# ═══════════════════════════════════════════════════════════════════
LEAGUE_AVG_TEAM_GOALS = 1.32   # per team per match (matches soccer_game_model)


def _minutes_multiplier(state: MinutesState,
                        expected_minutes: Optional[float],
                        starter_prob: Optional[float]) -> float:
    """Return 0..1 minutes-share multiplier applied to per-90 rates."""
    if expected_minutes is not None:
        return max(0.0, min(1.05, float(expected_minutes) / 90.0))
    if starter_prob is not None:
        return max(0.10, min(1.0, 0.40 + 0.55 * float(starter_prob)))
    if state == MinutesState.CONFIRMED_STARTER:  return 0.95
    if state == MinutesState.PROJECTED_STARTER:  return 0.85
    if state == MinutesState.ROTATION_RISK:      return 0.55
    if state == MinutesState.BENCH_EXPECTED:     return 0.20
    # UNKNOWN — cap conservatively so unknown minutes can't earn elite prob.
    return 0.60


def estimate_player_lambda(ev: PlayerEvidence) -> dict[str, Any]:
    """Estimate this-match player goal-intensity (λ_player).

    Combines per-90 rates with expected-minutes share and opponent
    strength.  Uses shrinkage on thin samples.  Returns MISSING when
    evidence is INSUFFICIENT.

    Coherence:
      * Where team_lambda is known, cap player share at team_lambda *
        opportunity_share_cap (0.65) — no single player can score more
        goals than his team.
      * When available, penalty role adds a small bump for PRIMARY.
    """
    auth = classify_authority(ev)
    if auth == Authority.INSUFFICIENT:
        return {
            "authority":       Authority.INSUFFICIENT.value,
            "lambda_player":   None,
            "reason":          "INSUFFICIENT_PLAYER_EVIDENCE",
        }
    # ── Blended base rate: prefer npxG (best isolation), fall back to
    #    xG, then goals, then shots-derived proxy.
    base_rate = None
    base_family = None
    if ev.npxg_per_90 is not None:
        base_rate = ev.npxg_per_90
        base_family = "npxG"
    elif ev.xg_per_90 is not None:
        base_rate = ev.xg_per_90
        base_family = "xG"
    elif ev.goals_per_90 is not None:
        # Shrink toward xG expectation if wildly overperformed.
        base_rate = ev.goals_per_90 * 0.85  # regression to xG mean
        base_family = "goals_shrunk"
    elif ev.shots_per_90 is not None:
        # Very rough conversion: shots × 0.09 ≈ goals for average finisher.
        base_rate = ev.shots_per_90 * 0.09
        base_family = "shots_proxy"
    if base_rate is None:
        return {
            "authority":     auth.value,
            "lambda_player": None,
            "reason":        "MODEL_PROBABILITY_MISSING",
        }
    # ── Sample-size shrinkage ────────────────────────────────────
    n = max(0, ev.sample_matches or 0)
    league_avg_scorer = 0.15  # per-90 goals for average attacker
    shrink_w = n / (n + 6)
    shrunk = shrink_w * base_rate + (1 - shrink_w) * league_avg_scorer

    # ── Opponent adjustment ──────────────────────────────────────
    opp_mult = 1.0
    if ev.opp_def_strength is not None:
        opp_mult = max(0.65, min(1.55, ev.opp_def_strength / LEAGUE_AVG_TEAM_GOALS))

    # ── Minutes multiplier ──────────────────────────────────────
    minutes_mult = _minutes_multiplier(
        ev.minutes_state, ev.expected_minutes, ev.starter_prob)

    # ── Penalty role bump ───────────────────────────────────────
    pk_bump = 0.0
    if ev.penalty_role == PenaltyRole.PRIMARY:   pk_bump = 0.06
    elif ev.penalty_role == PenaltyRole.SECONDARY: pk_bump = 0.02
    # UNKNOWN and NONE receive nothing.

    # ── Compose λ_player ────────────────────────────────────────
    lam = shrunk * opp_mult * minutes_mult + pk_bump

    # ── Team-coherence cap ──────────────────────────────────────
    opportunity_share_cap = 0.65
    if ev.team_lambda is not None:
        cap = float(ev.team_lambda) * opportunity_share_cap
        lam = min(lam, cap)

    lam = max(0.0, min(2.0, lam))    # sanity bounds
    p_atg = 1.0 - math.exp(-lam)

    return {
        "authority":     auth.value,
        "lambda_player": round(lam, 5),
        "atg_prob":      round(p_atg, 5),
        "base_rate":     round(base_rate, 5),
        "base_family":   base_family,
        "sample_n":      n,
        "minutes_mult":  round(minutes_mult, 4),
        "opp_mult":      round(opp_mult, 4),
        "pk_bump":       pk_bump,
        "team_capped":   (ev.team_lambda is not None and
                          shrunk * opp_mult * minutes_mult + pk_bump >
                          float(ev.team_lambda) * opportunity_share_cap),
    }


# ═══════════════════════════════════════════════════════════════════
# Score-or-Assist coherent model
# ═══════════════════════════════════════════════════════════════════
def estimate_assist_lambda(ev: PlayerEvidence) -> Optional[float]:
    """Return per-match λ_assist or None if evidence missing."""
    if ev.xa_per_90 is not None:
        base = ev.xa_per_90
    elif ev.assists_per_90 is not None:
        base = ev.assists_per_90 * 0.85    # shrink toward xA mean
    elif ev.key_passes_per_90 is not None:
        base = ev.key_passes_per_90 * 0.08 # rough kp→assist rate
    else:
        return None
    n = max(0, ev.sample_matches or 0)
    league_avg_creator = 0.12
    shrunk = (n / (n + 6)) * base + (6 / (n + 6)) * league_avg_creator
    opp_mult = 1.0
    if ev.opp_def_strength is not None:
        opp_mult = max(0.65, min(1.55, ev.opp_def_strength / LEAGUE_AVG_TEAM_GOALS))
    minutes_mult = _minutes_multiplier(
        ev.minutes_state, ev.expected_minutes, ev.starter_prob)
    lam = shrunk * opp_mult * minutes_mult
    return max(0.0, min(2.0, lam))


def price_score_or_assist(ev: PlayerEvidence) -> dict[str, Any]:
    """Coherent P(goal ∨ assist).  Uses inclusion-exclusion with a
    partial-correlation adjustment because goal and assist are NOT
    perfectly independent (a striker who scores often is also creating
    from the box)."""
    goal_est = estimate_player_lambda(ev)
    lam_a = estimate_assist_lambda(ev)
    if goal_est.get("lambda_player") is None:
        return {"authority": goal_est.get("authority"),
                "sga_prob": None,
                "reason":   goal_est.get("reason")}
    lam_g = goal_est["lambda_player"]
    if lam_a is None:
        # Fall back to ATG-only when no assist evidence.
        return {
            "authority":  goal_est["authority"],
            "goal_prob":  goal_est.get("atg_prob"),
            "assist_prob": None,
            "sga_prob":   goal_est.get("atg_prob"),
            "note":       "NO_ASSIST_EVIDENCE — sga defaults to atg",
        }
    p_goal   = 1.0 - math.exp(-lam_g)
    p_assist = 1.0 - math.exp(-lam_a)
    # Partial-correlation adjustment: assume ~0.20 correlation between
    # goal and assist involvement per match; use max-copula rather than
    # strict independence to prevent inflating the union.
    rho = 0.20
    p_both = p_goal * p_assist + rho * math.sqrt(
        max(0.0, p_goal * (1 - p_goal) * p_assist * (1 - p_assist))
    )
    p_both = min(p_both, min(p_goal, p_assist))
    p_union = p_goal + p_assist - p_both
    p_union = max(0.0, min(1.0, p_union))
    return {
        "authority":     goal_est["authority"],
        "lambda_goal":   round(lam_g, 5),
        "lambda_assist": round(lam_a, 5),
        "goal_prob":     round(p_goal, 5),
        "assist_prob":   round(p_assist, 5),
        "both_prob":     round(p_both, 5),
        "sga_prob":      round(p_union, 5),
        "correlation":   rho,
    }


# ═══════════════════════════════════════════════════════════════════
# Lock Score authority — evidence-first, market-once
# ═══════════════════════════════════════════════════════════════════
@dataclass
class LockAuthority:
    authority:     Authority
    reachable_max: float          # highest lock score this evidence can earn
    ceiling_reasons: list[str] = field(default_factory=list)


AUTHORITY_CEILINGS = {
    Authority.FULL:         99.0,   # 100 = Apex only, gated separately
    Authority.STRONG:       97.0,
    Authority.LIMITED:      88.0,
    Authority.INSUFFICIENT: 0.0,    # fail closed
}


def player_lock_authority(ev: PlayerEvidence,
                          model_prob: Optional[float],
                          devig_prob: Optional[float]) -> LockAuthority:
    """Return the ceiling this player's evidence can support.

    Rules (evidence-first, market-once):
        * Authority determines the CEILING (INSUFFICIENT → 0.0, hard fail).
        * Elite-tier reachability (95+) requires:
            - Authority.FULL or STRONG,
            - MINUTES not UNKNOWN,
            - devig market ± model agreement (|Δ| ≤ 0.10),
            - sample_matches ≥ 8.
        * Elite-tier 98+ additionally requires:
            - Authority.FULL AND CONFIRMED/PROJECTED starter,
            - sample_matches ≥ 12,
            - EVIDENCE_FAMILIES contains at least {opportunity, minutes,
              team_env, opp_env, market_context, distribution}.
        * NO league-name bonus.  NO star-name bonus.  Ceiling scales with
          coverage strength only.
    """
    auth = classify_authority(ev)
    ceiling = AUTHORITY_CEILINGS[auth]
    reasons: list[str] = [f"authority={auth.value}"]

    if auth == Authority.INSUFFICIENT:
        return LockAuthority(auth, 0.0, reasons + ["fail_closed_insufficient"])

    # Elite tier 95+
    if ceiling >= 95.0:
        if ev.minutes_state == MinutesState.UNKNOWN:
            ceiling = min(ceiling, 92.0); reasons.append("minutes_unknown_cap_92")
        if model_prob is not None and devig_prob is not None:
            if abs(model_prob - devig_prob) > 0.10:
                ceiling = min(ceiling, 92.0); reasons.append("model_market_delta_gt_10pp_cap_92")
        else:
            # Missing market context — cap at STRONG ceiling
            ceiling = min(ceiling, 92.0); reasons.append("no_devig_cap_92")
        if (ev.sample_matches or 0) < 8:
            ceiling = min(ceiling, 91.0); reasons.append("thin_sample_cap_91")

    # Elite tier 98+
    if ceiling >= 98.0:
        if auth != Authority.FULL:
            ceiling = min(ceiling, 96.0); reasons.append("not_full_authority_cap_96")
        if ev.minutes_state not in (MinutesState.CONFIRMED_STARTER,
                                    MinutesState.PROJECTED_STARTER):
            ceiling = min(ceiling, 95.0); reasons.append("not_starter_cap_95")
        if (ev.sample_matches or 0) < 12:
            ceiling = min(ceiling, 96.0); reasons.append("sample_lt_12_cap_96")
        needed = {"opportunity", "minutes", "team_env", "opp_env",
                  "market_context", "distribution"}
        if not needed.issubset(set(ev.evidence_families)):
            missing = needed - set(ev.evidence_families)
            ceiling = min(ceiling, 96.0)
            reasons.append(f"missing_families_{','.join(sorted(missing))}_cap_96")

    return LockAuthority(auth, round(ceiling, 1), reasons)


# ═══════════════════════════════════════════════════════════════════
# Terminal reason enumeration
# ═══════════════════════════════════════════════════════════════════
def enumerate_terminal_reason(ev: PlayerEvidence,
                              model_prob: Optional[float],
                              devig_prob: Optional[float],
                              lock_score: Optional[float],
                              stale: bool = False) -> TerminalReason:
    """Assign ONE primary terminal reason for why this player did or
    did not publish.  Order = strict priority top-to-bottom."""
    if stale:                                       return TerminalReason.STALE_EVENT
    if not ev.event_id:                             return TerminalReason.EVENT_IDENTITY_FAILED
    if not ev.team:                                 return TerminalReason.TEAM_IDENTITY_FAILED
    if not ev.player_name or not ev.player_id:      return TerminalReason.PLAYER_IDENTITY_FAILED
    if ev.book_odds is None:                        return TerminalReason.NO_REAL_ODDS
    auth = classify_authority(ev)
    if auth == Authority.INSUFFICIENT:
        # Sub-classify by dominant missing piece
        if ev.team_lambda is None:                  return TerminalReason.INSUFFICIENT_PLAYER_EVIDENCE
        if all(v is None for v in (ev.goals_per_90, ev.xg_per_90,
                                    ev.npxg_per_90, ev.shots_per_90)):
            return TerminalReason.INSUFFICIENT_PLAYER_EVIDENCE
        if ev.minutes_state == MinutesState.UNKNOWN and ev.starter_prob is None:
            return TerminalReason.EXPECTED_MINUTES_MISSING
        return TerminalReason.INSUFFICIENT_PLAYER_EVIDENCE
    if model_prob is None:                          return TerminalReason.MODEL_PROBABILITY_MISSING
    if lock_score is None:                          return TerminalReason.MODEL_NOT_RUN
    if lock_score < 85.0:                           return TerminalReason.LOCK_SCORE_BELOW_85
    return TerminalReason.OTHER    # ← published or filtered downstream


__all__ = [
    "Authority", "MinutesState", "PenaltyRole", "TerminalReason",
    "PlayerEvidence", "LockAuthority",
    "classify_authority",
    "estimate_player_lambda",
    "estimate_assist_lambda",
    "price_score_or_assist",
    "player_lock_authority",
    "enumerate_terminal_reason",
    "AUTHORITY_CEILINGS",
]
