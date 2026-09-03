"""Settlement Hard Gate — SHARED chokepoint enforcing the
``SettlementCapabilityRegistry.is_gradeable(...)`` invariant.

PERKLOCKS-MAIN 35 · P0-2.

The prior state: every settlement adapter (settlement_engine.settle_pick,
prop_settlement._grade, kbo_settlement, espn_settlement, service bridges)
did its own missing-data handling. Some returned ``None`` (safe: caller
keeps status ``pending``), some legacy branches could fabricate ``lost``
or ``0`` actuals. This module is the SHARED reference used at the real
grading entry points so:

  * MISSING_ACTUAL_DATA   → UNRESOLVED (never LOSS / zero / VOID)
  * IDENTITY_FAILURE      → UNRESOLVED
  * UNSUPPORTED_MARKET    → UNRESOLVED
  * EVENT_NOT_FINAL       → UNRESOLVED

Never fabricates an outcome. Never coerces missing to zero.

Design contract:
  * Pure/deterministic. Zero DB / zero network / zero I/O.
  * Takes canonical fields off the pick + score_payload only.
  * Returns ``(gradeable: bool, reason: str, canonical: dict)`` where
    ``canonical`` carries the resolved (sport, canonical_market_family).
  * Callers stamp ``pick["_hard_gate_reason"]`` when the gate refuses
    so telemetry / audit can prove no forced result was emitted.
"""
from __future__ import annotations
import re
from typing import Any, Dict, Optional, Tuple

from services.settlement_capability_registry import (
    is_gradeable,
    REASON_MISSING_ACTUAL,
    REASON_UNSUPPORTED_MARKET,
    REASON_EVENT_NOT_FINAL,
    REASON_IDENTITY_FAILURE,
)
from services.universal_market_contract import (
    resolve_provider_key,
    Family,
)


# ── Canonical (sport, family) resolution from a legacy pick ─────────
# The pick record's ``market`` string is legacy free-form
# ("Yankees Team Total Over 3.5 (Alt)", "St. Louis Cardinals -1.5 Run Line",
# "Aaron Judge Over 0.5 Hits (Alt)", "Over 8.5 Total Games"). Map to the
# canonical family the SettlementCapabilityRegistry knows about.
_MLB_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("moneyline",             Family.MONEYLINE),
    ("run line",              Family.RUN_LINE),
    ("runline",               Family.RUN_LINE),
    ("team total",            Family.GAME_TOTAL),  # graded via team score in engine
    ("total runs",            Family.GAME_TOTAL),
    ("hits",                  Family.HITTER_HITS),
    ("strikeouts",            Family.PITCHER_STRIKEOUTS),
)
_NFL_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("moneyline",             Family.MONEYLINE),
    ("point spread",          Family.POINT_SPREAD),
    ("spread",                Family.POINT_SPREAD),
    ("receiving yards",       Family.WR_RECEIVING_YDS),
    ("receptions",            Family.WR_RECEPTIONS),
)
_TENNIS_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("moneyline",             Family.TENNIS_MATCH_WIN),
    ("match winner",          Family.TENNIS_MATCH_WIN),
    ("games (alt)",           Family.TENNIS_TOTAL_GAMES),
    ("total games",           Family.TENNIS_TOTAL_GAMES),
    ("game spread",           Family.TENNIS_GAME_HANDICAP),
    ("games",                 Family.TENNIS_TOTAL_GAMES),
)
_SOCCER_FAMILY_HINTS: tuple[tuple[str, str], ...] = (
    ("anytime goalscorer",    Family.GOALSCORER_ANY),
    ("goalscorer",            Family.GOALSCORER_ANY),
    ("total goals",           Family.GAME_TOTAL),
    ("moneyline",             Family.MONEYLINE),
)
_FAMILY_MAP: Dict[str, tuple[tuple[str, str], ...]] = {
    "MLB":    _MLB_FAMILY_HINTS,
    "NFL":    _NFL_FAMILY_HINTS,
    "Tennis": _TENNIS_FAMILY_HINTS,
    "Soccer": _SOCCER_FAMILY_HINTS,
}


def resolve_family(sport: Optional[str], market: Optional[str]) -> Optional[str]:
    """Map (sport, legacy market string) → canonical market family, or
    None if unsupported by the registry."""
    if not sport or not market:
        return None
    m = market.lower()
    hints = _FAMILY_MAP.get(sport)
    if not hints:
        return None
    for needle, family in hints:
        if needle in m:
            return family
    return None


def _is_event_final(score_payload: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(score_payload, dict):
        return False
    return bool(score_payload.get("completed"))


def _score_for(scores: Any, team: Optional[str]) -> Optional[float]:
    if not isinstance(scores, list) or not team:
        return None
    tlow = team.strip().lower()
    for s in scores:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip().lower()
        if name and (name == tlow or name in tlow or tlow in name):
            try:
                return float(s.get("score"))
            except (TypeError, ValueError):
                return None
    return None


def _parse_event_teams(event: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not event or "@" not in event:
        return (None, None)
    parts = [p.strip() for p in event.split("@", 1)]
    if len(parts) != 2:
        return (None, None)
    return (parts[0], parts[1])   # (away, home)


def extract_actuals(pick: Dict[str, Any],
                    score_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract the ACTUAL fields required by the SettlementCapabilityRegistry
    for this pick's canonical family. Returns keys with value=None when
    the score payload lacks the field — the registry's ``is_gradeable``
    treats None as MISSING_ACTUAL_DATA."""
    out: Dict[str, Any] = {}
    if not isinstance(score_payload, dict):
        return out
    scores = score_payload.get("scores")
    away, home = _parse_event_teams(pick.get("event"))
    home_score = _score_for(scores, home)
    away_score = _score_for(scores, away)
    out["home_score"] = home_score
    out["away_score"] = away_score
    if home_score is not None and away_score is not None:
        out["match_winner"] = home if home_score > away_score else (
            away if away_score > home_score else None
        )
        out["total_games"] = home_score + away_score  # Tennis: games; approx
    # Player-prop actuals are pulled via prop_settlement; leave None here
    # so the gate correctly refuses to grade a prop from a game-score payload.
    return out


def evaluate(pick: Dict[str, Any],
             score_payload: Optional[Dict[str, Any]] = None,
             *,
             canonical_identity_resolved: Optional[bool] = None,
             actuals_override: Optional[Dict[str, Any]] = None,
             ) -> Tuple[bool, str, Dict[str, Any]]:
    """Return ``(gradeable, reason, canonical)``.

    ``canonical = {"sport": ..., "family": ..., "actuals": {...}}``.
    """
    sport = pick.get("sport")
    market = pick.get("market")
    family = resolve_family(sport, market)
    canonical: Dict[str, Any] = {"sport": sport, "family": family}

    if family is None:
        return (False, REASON_UNSUPPORTED_MARKET, canonical)

    event_final = _is_event_final(score_payload)
    if canonical_identity_resolved is None:
        # Default: identity is resolved iff we can parse both team names
        # from the event string. Player props override with an explicit
        # boolean once player identity is matched.
        away, home = _parse_event_teams(pick.get("event"))
        canonical_identity_resolved = bool(away and home)

    if actuals_override is not None:
        actuals = dict(actuals_override)
    else:
        actuals = extract_actuals(pick, score_payload)
    canonical["actuals"] = actuals

    gradeable, reason = is_gradeable(
        sport=sport or "",
        canonical_market_family=family,
        event_final=event_final,
        canonical_identity_resolved=bool(canonical_identity_resolved),
        actuals=actuals,
    )
    return (gradeable, reason, canonical)


def stamp_refusal(pick: Dict[str, Any], reason: str) -> None:
    """Callers stamp the pick with the refusal reason for telemetry.
    Never mutates canonical status — that stays ``pending`` (UNRESOLVED)."""
    try:
        pick["_hard_gate_refused"] = True
        pick["_hard_gate_reason"] = reason
    except Exception:
        pass


__all__ = [
    "resolve_family",
    "extract_actuals",
    "evaluate",
    "stamp_refusal",
    "REASON_MISSING_ACTUAL",
    "REASON_UNSUPPORTED_MARKET",
    "REASON_EVENT_NOT_FINAL",
    "REASON_IDENTITY_FAILURE",
]
