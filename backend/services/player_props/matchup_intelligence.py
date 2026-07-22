"""Matchup Intelligence Module — Phase 3.

USER MANDATE: build a Matchup Intelligence layer that adjusts model
probabilities based on:
  • Opponent defensive vulnerability (goals conceded rate, xGA)
  • Home/away splits (players score more at home)
  • Player fatigue / rest days
  • Recent form trend (last 3 vs season baseline)
  • Historical matchup (already surfaced via MatchupSplit)

Where data is available, we lift/drop the raw probability +/− up to
25%. When we don't have a signal, we return a neutral (1.0) multiplier
so callers stay data-honest (no fabrication).

Consumed by `market_selector.py` and `soccer_prop_inject.py`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models import PlayerStats, MatchupSplit

logger = logging.getLogger("lockscore.player_props.matchup")


@dataclass
class MatchupContext:
    """All contextual signals wrapped for a single (player, event) tuple."""
    opponent: str = ""
    is_home: Optional[bool] = None

    # Rest / fatigue
    days_rest: Optional[int] = None
    rest_mult: float = 1.0
    rest_note: str = ""

    # Home/away
    home_away_mult: float = 1.0
    home_away_note: str = ""

    # Opponent defense (0-1 scale, lower = more porous)
    opp_defense_strength: Optional[float] = None
    defense_mult: float = 1.0
    defense_note: str = ""

    # Recent form trend (from PlayerStats.form_score)
    form_trend_mult: float = 1.0
    form_trend_note: str = ""

    # Combined evidence for surfacing in the UI
    evidence: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    def total_multiplier(self) -> float:
        """Aggregate of all context multipliers. Clamped to ±25%."""
        raw = (self.rest_mult * self.home_away_mult
               * self.defense_mult * self.form_trend_mult)
        return max(0.75, min(1.25, raw))


# ─────────── Signal computers ───────────
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _days_rest_signal(event_commence: str,
                      last_match_iso: Optional[str] = None) -> tuple[float, str, Optional[int]]:
    """Compute rest multiplier from days since last known match.

    Bell curve peak around 4-6 days (typical league week):
       ≤ 2 days   → 0.90  (fatigued)
       3 days     → 0.96
       4-6 days   → 1.05  (optimal)
       7-9 days   → 1.02  (fresh)
       10+ days   → 0.98  (a bit rusty)
    """
    if not last_match_iso or not event_commence:
        return 1.0, "", None
    try:
        e = datetime.fromisoformat(event_commence.replace("Z", "+00:00"))
        l = datetime.fromisoformat(last_match_iso.replace("Z", "+00:00"))
        delta_days = int((e - l).total_seconds() / 86400.0)
    except Exception:
        return 1.0, "", None

    if delta_days <= 1:
        return 0.88, f"❄️ Only {delta_days}d rest — heavy fatigue risk", delta_days
    if delta_days == 2:
        return 0.92, f"⚠️ 2d rest — congested schedule", delta_days
    if delta_days == 3:
        return 0.97, f"3d rest — playable", delta_days
    if 4 <= delta_days <= 6:
        return 1.05, f"✅ {delta_days}d rest — optimal turnaround", delta_days
    if 7 <= delta_days <= 9:
        return 1.03, f"✅ {delta_days}d rest — fresh legs", delta_days
    if delta_days <= 14:
        return 1.00, f"{delta_days}d rest", delta_days
    return 0.97, f"⚠️ {delta_days}d without a match — potential rust", delta_days


def _home_away_signal(is_home: Optional[bool]) -> tuple[float, str]:
    """Home advantage: attackers score ~10% more at home league-wide."""
    if is_home is None:
        return 1.0, ""
    if is_home:
        return 1.06, "🏠 Playing at home — slight scoring advantage"
    return 0.97, "🚌 Away trip — modest suppression"


def _form_trend_signal(form_score: float) -> tuple[float, str]:
    """Refine form multiplier at the extremes for the matchup layer."""
    # This is SECONDARY to the base model's form multiplier — we only
    # add extra pop at true extremes so the layers don't double-count.
    if form_score >= 80:
        return 1.05, f"🔥 Blistering form ({form_score:.0f}/100)"
    if form_score >= 70:
        return 1.02, f"🔥 Hot form ({form_score:.0f}/100)"
    if form_score <= 20:
        return 0.94, f"❄️ Ice cold ({form_score:.0f}/100)"
    if form_score <= 30:
        return 0.98, f"❄️ Cold streak ({form_score:.0f}/100)"
    return 1.0, ""


def _defense_signal(opp_defense_strength: Optional[float]
                    ) -> tuple[float, str]:
    """Opponent defensive strength (0-1). Lower = more porous.

    Currently populated only when caller passes a computed value.
    Placeholder gracefully returns neutral.
    """
    if opp_defense_strength is None:
        return 1.0, ""
    # Invert: porous defense (0.2) → 1.15 boost.
    # Rock-solid defense (0.85) → 0.90 suppression.
    delta = 0.5 - opp_defense_strength
    mult = 1.0 + _clamp(delta * 0.30, -0.15, 0.20)
    if opp_defense_strength <= 0.30:
        note = f"🎯 Porous defense ({opp_defense_strength*100:.0f}% strength) — boost"
    elif opp_defense_strength >= 0.75:
        note = f"🛡 Elite defense ({opp_defense_strength*100:.0f}% strength) — suppress"
    else:
        note = ""
    return mult, note


# ─────────── Public API ───────────
def build_matchup_context(
    stats: PlayerStats,
    opponent: str,
    *,
    is_home: Optional[bool] = None,
    event_commence: Optional[str] = None,
    last_match_iso: Optional[str] = None,
    split: Optional[MatchupSplit] = None,
    opp_defense_strength: Optional[float] = None,
) -> MatchupContext:
    """Build a full MatchupContext from available signals.

    All args except `stats` and `opponent` are optional. Missing signals
    → neutral 1.0 multipliers (no fabrication).
    """
    ctx = MatchupContext(opponent=opponent, is_home=is_home,
                          opp_defense_strength=opp_defense_strength)

    # Rest / fatigue
    if last_match_iso:
        ctx.rest_mult, ctx.rest_note, ctx.days_rest = \
            _days_rest_signal(event_commence or "", last_match_iso)
    if ctx.rest_note:
        (ctx.concerns if ctx.rest_mult < 1.0 else ctx.evidence).append(ctx.rest_note)

    # Home/away
    ctx.home_away_mult, ctx.home_away_note = _home_away_signal(is_home)
    if ctx.home_away_note:
        (ctx.evidence if ctx.home_away_mult >= 1.0 else ctx.concerns).append(ctx.home_away_note)

    # Defense
    ctx.defense_mult, ctx.defense_note = _defense_signal(opp_defense_strength)
    if ctx.defense_note:
        (ctx.evidence if ctx.defense_mult >= 1.0 else ctx.concerns).append(ctx.defense_note)

    # Form trend (extremes only — base model already handles the main range)
    ctx.form_trend_mult, ctx.form_trend_note = _form_trend_signal(stats.form_score)
    if ctx.form_trend_note:
        (ctx.evidence if ctx.form_trend_mult >= 1.0 else ctx.concerns).append(ctx.form_trend_note)

    # Historical head-to-head boost (already carried by split, but we
    # surface additional summary evidence for the UI here).
    if split and split.matches >= 3:
        gi = split.gi_rate() * 100
        ctx.evidence.append(
            f"📊 H2H vs {split.opponent}: {split.matches} games, GI rate {gi:.0f}%"
        )

    return ctx


def apply_matchup_context(base_prob: float, ctx: MatchupContext) -> float:
    """Apply the aggregated matchup context to a base probability."""
    return max(0.02, min(0.92, base_prob * ctx.total_multiplier()))
