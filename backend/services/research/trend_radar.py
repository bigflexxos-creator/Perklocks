"""Trend Radar — MLB / NFL / NBA classification engine.

Strategy Lab 10X §1/§2/§3/§4 — one universal contract, three sport-
specific classifiers.  All outputs are RESEARCH_ONLY (SHADOW provenance).

Contract:  `classify(sport, subject, features) -> TrendSignal | None`

Where `features` is the FACTUAL research payload already assembled by
`services/research/{mlb,nfl,nba}.py`.  This module never re-queries
providers; it only classifies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .trends import (
    TrendDataQuality, TrendDirection, TrendSignal, TrendStrength, TrendType,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── MLB (multi-hit / power / discipline) ─────────────────────────────
def classify_mlb(subject: str, features: dict[str, Any],
                 event_id: str | None = None,
                 player_id: str | None = None) -> TrendSignal | None:
    f = features or {}
    multi = int(f.get("multi_hit_games") or 0)
    n = int(f.get("exact_game_log_n") or 0)
    ops = float(f.get("l15_ops") or 0)
    obp = float(f.get("l15_obp") or 0)
    avg = float(f.get("l15_avg") or 0)
    streak = int(f.get("hit_streak") or 0)
    zero = int(f.get("zero_hit_games") or 0)
    if n < 5:
        return None
    supporting: list[str] = []
    contra: list[str] = []
    ttype = TrendType.NEUTRAL
    direction = TrendDirection.NEUTRAL
    strength = TrendStrength.MODERATE
    confidence = 0.55
    multi_rate = multi / n if n else 0
    if multi_rate >= 0.35 and ops >= 0.850 and streak >= 3:
        ttype = TrendType.HOT_CONFIRMED
        direction = TrendDirection.OVER
        strength = TrendStrength.STRONG
        confidence = 0.75
        supporting = [f"multi-hit in {multi}/{n}", f"OPS {ops:.3f}",
                      f"{streak}-game hit streak"]
    elif ops >= 0.900 and n <= 8 and streak >= 4:
        ttype = TrendType.BREAKOUT; direction = TrendDirection.OVER
        strength = TrendStrength.MODERATE; confidence = 0.6
        supporting = [f"OPS spike {ops:.3f}", f"streak {streak}"]
    elif avg >= 0.330 and multi_rate < 0.20:
        ttype = TrendType.OVERPERFORMING; direction = TrendDirection.UNDER
        strength = TrendStrength.MODERATE; confidence = 0.55
        supporting = [f"AVG {avg:.3f} without multi-hit sustain"]
        contra = [f"multi-hit only {multi}/{n}"]
    elif obp >= 0.360 and avg <= 0.240:
        ttype = TrendType.POSITIVE_REGRESSION; direction = TrendDirection.OVER
        strength = TrendStrength.MODERATE; confidence = 0.58
        supporting = [f"OBP {obp:.3f} vs AVG {avg:.3f}"]
    elif streak <= 1 and zero / n >= 0.55 and ops <= 0.650:
        ttype = TrendType.ROLE_DECLINE; direction = TrendDirection.UNDER
        strength = TrendStrength.STRONG; confidence = 0.7
        supporting = [f"0-hit games {zero}/{n}", f"OPS {ops:.3f}"]
    else:
        return None
    return TrendSignal(
        sport="MLB", event_id=event_id, player_id=player_id,
        subject=subject, trend_type=ttype,
        market_relevance=["Over 0.5 Hits", "Over 1.5 Hits", "Total Bases"],
        direction=direction, strength=strength, confidence=confidence,
        supporting_features=supporting, contradicting_features=contra,
        sample_size=n,
        data_quality=(TrendDataQuality.STRONG if n >= 10 else TrendDataQuality.PARTIAL),
        observed_at=_now(),
    )


# ── NFL (opportunity-driven) ─────────────────────────────────────────
def classify_nfl(subject: str, features: dict[str, Any],
                 event_id: str | None = None,
                 player_id: str | None = None) -> TrendSignal | None:
    f = features or {}
    snap = float(f.get("nfl_snap_pct") or 0)
    tgt = float(f.get("nfl_target_share") or 0)
    carry = float(f.get("nfl_carry_share") or 0)
    rz = float(f.get("nfl_rz_touches_pg") or 0)
    l4t = float(f.get("nfl_l4_targets_avg") or 0)
    l4c = float(f.get("nfl_l4_carries_avg") or 0)
    supporting: list[str] = []
    ttype: TrendType | None = None
    direction = TrendDirection.OVER
    strength = TrendStrength.MODERATE
    confidence = 0.55
    market: list[str] = []
    if snap >= 75 and (tgt >= 20 or carry >= 40):
        ttype = TrendType.ROLE_BREAKOUT
        strength = TrendStrength.STRONG; confidence = 0.72
        supporting = [f"snap {snap:.0f}%", f"tgt {tgt:.0f}% / carry {carry:.0f}%"]
        market = ["Receiving Yards", "Rushing Yards", "Anytime TD"]
    elif l4t >= 8 and tgt >= 18:
        ttype = TrendType.TARGET_SURGE
        supporting = [f"L4 tgt {l4t:.1f}/g, share {tgt:.0f}%"]
        market = ["Receiving Yards", "Receptions"]
    elif l4c >= 15 and carry >= 55:
        ttype = TrendType.RUSH_VOLUME_SURGE
        supporting = [f"L4 carries {l4c:.1f}/g, share {carry:.0f}%"]
        market = ["Rushing Yards", "Anytime TD"]
    elif rz >= 2.0:
        ttype = TrendType.RED_ZONE_SURGE
        supporting = [f"RZ touches {rz:.1f}/g"]
        market = ["Anytime TD"]
    elif tgt >= 25 and l4t < 6:
        ttype = TrendType.POSITIVE_REGRESSION
        supporting = [f"role {tgt:.0f}% but L4 {l4t:.1f} under-realized"]
        market = ["Receiving Yards", "Receptions"]
    elif snap <= 40 and (tgt < 12 and carry < 25):
        ttype = TrendType.ROLE_DECLINE
        direction = TrendDirection.UNDER
        strength = TrendStrength.STRONG; confidence = 0.7
        supporting = [f"snap {snap:.0f}% + share collapse"]
        market = ["Any receiving/rushing under"]
    if ttype is None:
        return None
    return TrendSignal(
        sport="NFL", event_id=event_id, player_id=player_id,
        subject=subject, trend_type=ttype,
        market_relevance=market, direction=direction,
        strength=strength, confidence=confidence,
        supporting_features=supporting,
        contradicting_features=[],
        sample_size=int(max(l4t, l4c) * 4) if (l4t or l4c) else 4,
        data_quality=TrendDataQuality.STRONG,
        observed_at=_now(),
    )


# ── NBA (usage / pace / minutes) ─────────────────────────────────────
def classify_nba(subject: str, features: dict[str, Any],
                 event_id: str | None = None,
                 player_id: str | None = None) -> TrendSignal | None:
    f = features or {}
    l10_pts = float(f.get("nba_l10_pts") or 0)
    l10_reb = float(f.get("nba_l10_reb") or 0)
    l10_ast = float(f.get("nba_l10_ast") or 0)
    l10_3pm = float(f.get("nba_l10_fg3m") or 0)
    l10_min = float(f.get("nba_l10_min") or 0)
    opp_pace = float(f.get("nba_opp_pace") or 0)
    opp_def = float(f.get("nba_opp_def_rating") or 0)
    supporting: list[str] = []
    ttype: TrendType | None = None
    market: list[str] = []
    direction = TrendDirection.OVER
    strength = TrendStrength.MODERATE
    confidence = 0.55
    if l10_pts >= 22 and l10_min >= 32:
        ttype = TrendType.SCORING_SURGE
        strength = TrendStrength.STRONG; confidence = 0.72
        supporting = [f"L10 PTS {l10_pts:.1f}", f"min {l10_min:.1f}"]
        market = ["Points", "Points+Rebounds+Assists"]
    elif l10_min >= 34:
        ttype = TrendType.MINUTES_INCREASE
        supporting = [f"L10 min {l10_min:.1f}"]
        market = ["Points", "PRA"]
    elif l10_ast >= 6.5:
        ttype = TrendType.PLAYMAKING_SURGE
        supporting = [f"L10 AST {l10_ast:.1f}"]
        market = ["Assists"]
    elif l10_reb >= 9:
        ttype = TrendType.REBOUND_OPPORTUNITY
        supporting = [f"L10 REB {l10_reb:.1f}"]
        market = ["Rebounds"]
    elif l10_3pm >= 3:
        ttype = TrendType.THREE_POINT_VOLUME_SURGE
        supporting = [f"L10 3PM {l10_3pm:.1f}"]
        market = ["Threes Made"]
    elif l10_pts >= 18 and opp_def and opp_def >= 115:
        ttype = TrendType.POSITIVE_REGRESSION
        supporting = [f"L10 PTS {l10_pts:.1f} vs weak DEF {opp_def:.1f}"]
        market = ["Points"]
    elif l10_min > 0 and l10_min <= 22 and l10_pts <= 8:
        ttype = TrendType.ROLE_DECLINE
        direction = TrendDirection.UNDER
        supporting = [f"min {l10_min:.1f}", f"PTS {l10_pts:.1f}"]
        market = ["Points UNDER"]
    if ttype is None:
        return None
    if opp_pace and opp_pace >= 102 and ttype in (TrendType.SCORING_SURGE,
                                                   TrendType.THREE_POINT_VOLUME_SURGE):
        supporting.append(f"opp pace {opp_pace:.1f}")
        confidence = min(0.85, confidence + 0.05)
    return TrendSignal(
        sport="NBA", event_id=event_id, player_id=player_id,
        subject=subject, trend_type=ttype,
        market_relevance=market, direction=direction,
        strength=strength, confidence=confidence,
        supporting_features=supporting, contradicting_features=[],
        sample_size=10,
        data_quality=TrendDataQuality.STRONG,
        observed_at=_now(),
    )


def classify(sport: str, subject: str, features: dict[str, Any],
             event_id: str | None = None,
             player_id: str | None = None) -> TrendSignal | None:
    s = (sport or "").upper()
    if s == "MLB": return classify_mlb(subject, features, event_id, player_id)
    if s == "NFL": return classify_nfl(subject, features, event_id, player_id)
    if s == "NBA": return classify_nba(subject, features, event_id, player_id)
    return None
