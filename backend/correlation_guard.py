"""Heuristic correlation guard for parlay leg combination.

Purpose: detect correlated legs in a parlay and downweight the combined
edge so we don't sell users on phantom +EV that's actually negative once
correlation is priced in.

Three tiers of correlation handled (highest → lowest impact):

  1. **same_player**       — two legs on the SAME player props. e.g.
                             "Lamar 250+ pass yards" + "Lamar 2+ pass
                             TDs". Strongly correlated (~0.85). We
                             BLOCK these legs from combining outright.

  2. **same_game_same_side** — two legs on the same game backing the
                             SAME outcome (e.g., Ravens ML + Ravens
                             -3.5 + Ravens team total OVER). Moderately
                             correlated (~0.55). We apply a downweight
                             factor of 0.85^N to the combined edge.

  3. **same_game_opposite** — two legs on the same game on OPPOSITE
                             sides (e.g., Ravens ML + game total
                             OVER 47.5). Slightly correlated (~0.25).
                             Mild 0.93^N downweight.

Full statistical correlation matrix (trained on historical settled
parlays) is the long-term plan — but a heuristic is shippable now and
catches the worst offenders (same-player parlay duplication, RBI/HR
double-dips, QB+WR same-game stacks).

Stateless and pure: the parlay optimiser and the bet-slip UI both call
`analyze_parlay(legs)` to surface a `{warnings, downweight_factor}`
block before showing combined odds.
"""

from __future__ import annotations

from typing import Optional


def _player_name(leg: dict) -> Optional[str]:
    """Best-effort extraction of the player name from a pick payload."""
    for k in ("player_name", "player", "selection"):
        v = leg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    # Fall back to first token of the market for player prop markets
    market = (leg.get("market") or "").lower()
    if any(t in market for t in ("anytime", "rush yards", "pass yards",
                                  "rec yards", "receptions", "passing tds",
                                  "rushing tds", "receiving tds")):
        return market.split(" ")[0] if market else None
    return None


def _event_id(leg: dict) -> Optional[str]:
    """Identifier shared by all legs from the same game.
    Falls back to home@away if no explicit event_id is stamped."""
    eid = leg.get("event_id") or leg.get("game_id") or leg.get("external_event_id")
    if eid:
        return str(eid)
    home, away = leg.get("home_team"), leg.get("away_team")
    if home and away:
        return f"{home}__{away}"
    return None


def _picked_team(leg: dict) -> Optional[str]:
    """The team this leg is rooting FOR. Spread/ML picks: the favored
    side. Total OVER: implicitly rooting for BOTH (returns None).
    Player props: the player's team (best-effort)."""
    market = (leg.get("market") or "").lower()
    if "total" in market or " over " in market or " under " in market:
        return None  # totals don't take sides
    side = leg.get("pick_side") or leg.get("selection") or ""
    return (side or "").lower() or None


def analyze_parlay(legs: list[dict]) -> dict:
    """Return a structured correlation report for the parlay slip.

    Output keys:
      - warnings:           list of human-readable strings to surface
                            in the UI ("Same player on 2 legs", etc.)
      - blocked_pairs:      list of (idx_a, idx_b) pairs that should
                            be PREVENTED from combining (same player)
      - downweight_factor:  multiplier (0..1) to apply to the combined
                            edge before display. 1.0 = no adjustment.
      - correlation_tier:   "high" | "medium" | "low" | "none"
    """
    n = len(legs or [])
    out = {
        "warnings": [],
        "blocked_pairs": [],
        "downweight_factor": 1.0,
        "correlation_tier": "none",
    }
    if n < 2:
        return out

    same_player_pairs:     list[tuple[int, int]] = []
    same_side_pairs:       list[tuple[int, int]] = []
    same_game_opp_pairs:   list[tuple[int, int]] = []

    for i in range(n):
        for j in range(i + 1, n):
            a, b = legs[i], legs[j]
            pa, pb = _player_name(a), _player_name(b)
            if pa and pb and pa == pb:
                same_player_pairs.append((i, j))
                continue
            ea, eb = _event_id(a), _event_id(b)
            if ea and eb and ea == eb:
                ta, tb = _picked_team(a), _picked_team(b)
                if ta and tb and ta == tb:
                    same_side_pairs.append((i, j))
                else:
                    same_game_opp_pairs.append((i, j))

    if same_player_pairs:
        out["blocked_pairs"] = same_player_pairs
        out["warnings"].append(
            f"{len(same_player_pairs)} same-player leg pair(s) — these are highly "
            "correlated and have been flagged. Combining them inflates apparent "
            "edge by ~30-50%."
        )
        out["correlation_tier"] = "high"

    if same_side_pairs:
        # 0.85^N edge attenuation per same-side same-game pair
        out["downweight_factor"] *= 0.85 ** len(same_side_pairs)
        out["warnings"].append(
            f"{len(same_side_pairs)} same-game same-side stack(s) detected — "
            "edge attenuated by 15% per stack."
        )
        if out["correlation_tier"] == "none":
            out["correlation_tier"] = "medium"

    if same_game_opp_pairs:
        # 0.93^N for mild opposite-side correlation
        out["downweight_factor"] *= 0.93 ** len(same_game_opp_pairs)
        if out["correlation_tier"] == "none":
            out["correlation_tier"] = "low"

    out["downweight_factor"] = round(out["downweight_factor"], 3)
    return out


__all__ = ["analyze_parlay"]
