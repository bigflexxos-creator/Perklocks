"""Authoritative tennis match-format resolver.

PERKLOCKS-MAIN 35 · P0-1 — TENNIS ALT-TOTAL TRUTH (BO3/BO5).

The crude "everything is best-of-3" assumption baked into the tennis
alt-total pricing path was the root cause of false 99% Win Expected on
ATP Grand Slam alt-totals (39.5 / 41.5 / 42.5).

Rules (authoritative, per ITF / ATP / WTA / Grand Slam Board):
  • ATP MEN'S Grand Slam singles → BEST-OF-5 SETS
  • WTA WOMEN'S Grand Slam singles → BEST-OF-3 SETS
  • Every other ATP / WTA event (Masters 1000, 500, 250, Finals, etc.)
    → BEST-OF-3 SETS
  • Team competitions (Davis / BJK / United Cup / Laver / ATP Cup) →
    BEST-OF-3 SETS by contract (matches remain BO3 even under an ITF
    Davis Cup umbrella — no BO5 rubbers since 2019).

Never assumes BO5 for a WTA slam. Never assumes BO3 for an ATP slam.
Explicit metadata on the event payload always wins over defaults.

The resolver is intentionally pure/deterministic so it can be trivially
unit tested and reused by the settlement layer.
"""
from __future__ import annotations
from typing import Any, Mapping, Optional


# ATP Men's Grand Slam sport keys used across the app (The Odds API
# canonical keys). These are the ONLY tennis events that pricing must
# treat as best-of-5 by default.
_ATP_GRAND_SLAM_SPORT_KEYS: frozenset[str] = frozenset({
    "tennis_atp_aus_open_singles",
    "tennis_atp_french_open",
    "tennis_atp_wimbledon",
    "tennis_atp_us_open",
})

# WTA Grand Slam sport keys — kept explicit so mis-routing an ATP slam
# through the WTA path (or vice versa) fails closed rather than silently
# assuming the wrong format.
_WTA_GRAND_SLAM_SPORT_KEYS: frozenset[str] = frozenset({
    "tennis_wta_aus_open_singles",
    "tennis_wta_french_open",
    "tennis_wta_wimbledon",
    "tennis_wta_us_open",
})

# Grand Slam name fragments (used only when sport_key is absent).
_ATP_SLAM_NAME_FRAGMENTS: tuple[str, ...] = (
    "atp_aus_open", "atp_australian_open",
    "atp_french_open", "atp_roland_garros",
    "atp_wimbledon",
    "atp_us_open",
)
_WTA_SLAM_NAME_FRAGMENTS: tuple[str, ...] = (
    "wta_aus_open", "wta_australian_open",
    "wta_french_open", "wta_roland_garros",
    "wta_wimbledon",
    "wta_us_open",
)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def resolve_tennis_match_format(
    *,
    sport_key: Optional[str] = None,
    league: Optional[str] = None,
    event_payload: Optional[Mapping[str, Any]] = None,
    tournament_name: Optional[str] = None,
) -> int:
    """Return authoritative match format (3 or 5) for a tennis event.

    Precedence (highest to lowest authority):
        1. Explicit ``best_of`` / ``match_format`` field on the event
           payload (from provider metadata) — 3 or 5 only.
        2. Sport-key match against known ATP Grand Slam singles keys.
        3. Sport-key match against known WTA Grand Slam singles keys
           (forces BO3 even if a downstream heuristic wanted BO5).
        4. Case-insensitive fragment scan of ``sport_key`` / ``league``
           / ``tournament_name`` for known Grand Slam names.
        5. Default → BO3 (regular ATP / WTA + all team events).

    The resolver NEVER returns anything other than 3 or 5.
    """
    # (1) Explicit event metadata wins.
    if isinstance(event_payload, Mapping):
        for k in ("best_of", "match_format", "format"):
            v = event_payload.get(k)
            if isinstance(v, int) and v in (3, 5):
                return v
            # Providers sometimes ship "BO5" / "best_of_5" / "5-set"…
            if isinstance(v, str):
                sv = v.strip().lower()
                if "5" in sv and "3" not in sv:
                    return 5
                if "3" in sv and "5" not in sv:
                    return 3

    sport_key_n = _norm(sport_key)
    league_n = _norm(league)
    tournament_n = _norm(tournament_name)

    # (2) ATP Grand Slam sport-key allowlist → BO5.
    if sport_key_n in _ATP_GRAND_SLAM_SPORT_KEYS:
        return 5

    # (3) WTA Grand Slam sport-key allowlist → BO3 (explicit veto).
    if sport_key_n in _WTA_GRAND_SLAM_SPORT_KEYS:
        return 3

    # (4) Fragment scan. Check WTA first so an ambiguous
    # "us_open" hint does not accidentally promote a WTA event to BO5.
    scan = " ".join(x for x in (sport_key_n, league_n, tournament_n) if x)
    for frag in _WTA_SLAM_NAME_FRAGMENTS:
        if frag in scan:
            return 3
    for frag in _ATP_SLAM_NAME_FRAGMENTS:
        if frag in scan:
            return 5

    # (5) Default: BO3 (regular tour + all team events).
    return 3


def is_grand_slam(*, sport_key: Optional[str] = None,
                  league: Optional[str] = None,
                  tournament_name: Optional[str] = None) -> bool:
    """Convenience predicate for downstream provenance/labeling only.

    Grand Slam = one of the four ITF majors, regardless of tour.
    """
    key = _norm(sport_key)
    if key in _ATP_GRAND_SLAM_SPORT_KEYS or key in _WTA_GRAND_SLAM_SPORT_KEYS:
        return True
    scan = " ".join(x for x in (key, _norm(league), _norm(tournament_name)) if x)
    for frag in (*_ATP_SLAM_NAME_FRAGMENTS, *_WTA_SLAM_NAME_FRAGMENTS):
        if frag in scan:
            return True
    return False
