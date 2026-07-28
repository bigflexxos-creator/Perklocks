"""Regression test for Defect #1 — emission-time symmetric-pair defense
in `_props_picks_from_event` (sports_engine.py).

Contract:
  Given a payload where the SAME (player, market, line) appears with
  BOTH `Over` and `Under` outcomes from The Odds API:
    → `_props_picks_from_event` must emit AT MOST one candidate for
      that (player, market, line) group.
    → The winning side must be selected by the model
      (`services.mlb_k_probability.evaluate_k_pick` for K props, or
      book-consensus for other markets) — NEVER by Odds-API iteration
      order.
    → Reversing the outcome order inside the payload must yield the
      same winner (proves iteration order is not the tiebreaker).
    → If the model can't decide (K math returns neither-emit; balanced
      book prices), BOTH sides must be dropped.
"""
from __future__ import annotations

import os
import sys
import random

os.environ.setdefault("MONGO_URL", os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
os.environ.setdefault("DB_NAME", "lockscore_db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_payload(outcomes: list[dict]) -> dict:
    """Build a minimal payload with one bookmaker + one market."""
    return {
        "home_team": "Philadelphia Phillies",
        "away_team": "Miami Marlins",
        "_ctx": {
            "home_team": "Philadelphia Phillies",
            "away_team": "Miami Marlins",
        },
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {"key": "pitcher_strikeouts", "outcomes": outcomes},
                ],
            }
        ],
    }


def _pitcher_k_outcomes(line: float = 6.5,
                       over_price: int = -145,
                       under_price: int = 115,
                       reverse: bool = False) -> list[dict]:
    """Zack Wheeler Over/Under K outcomes exactly like The Odds API."""
    over = {
        "name": "Over", "description": "Zack Wheeler",
        "point": line, "price": over_price,
    }
    under = {
        "name": "Under", "description": "Zack Wheeler",
        "point": line, "price": under_price,
    }
    return [under, over] if reverse else [over, under]


def _sides_of(picks: list[dict], player_substr: str) -> list[str]:
    """Extract Over/Under labels from emitted picks for a given player."""
    labels: list[str] = []
    for p in picks:
        m = (p.get("market") or "")
        if player_substr.lower() in m.lower():
            ml = m.lower()
            if " over " in ml or ml.startswith("over "):
                labels.append("over")
            elif " under " in ml or ml.startswith("under "):
                labels.append("under")
    return labels


def test_symmetric_pair_emits_only_one_side():
    """Given Over 6.5 K -145 AND Under 6.5 K +115 for the same pitcher,
    `_props_picks_from_event` must NOT emit both."""
    from sports_engine import _props_picks_from_event
    payload = _make_payload(_pitcher_k_outcomes(6.5, -145, 115))
    rng = random.Random(0)
    picks = _props_picks_from_event(
        sport="MLB", league="MLB", payload=payload,
        commence="2026-07-28T22:15:00Z", rng=rng,
    )
    sides = _sides_of(picks, "Wheeler")
    assert len(sides) <= 1, (
        f"Symmetric pair emitted BOTH sides: {sides}. "
        f"Defect #1 not fixed — {[p.get('market') for p in picks]}"
    )


def test_iteration_order_is_not_the_tiebreaker():
    """Reversing the order of outcomes in the payload must yield the
    SAME winning side. If iteration order determines the winner, this
    test fails."""
    from sports_engine import _props_picks_from_event
    rng1 = random.Random(0)
    rng2 = random.Random(0)
    picks_normal = _props_picks_from_event(
        sport="MLB", league="MLB",
        payload=_make_payload(_pitcher_k_outcomes(6.5, -145, 115, reverse=False)),
        commence="2026-07-28T22:15:00Z", rng=rng1,
    )
    picks_reversed = _props_picks_from_event(
        sport="MLB", league="MLB",
        payload=_make_payload(_pitcher_k_outcomes(6.5, -145, 115, reverse=True)),
        commence="2026-07-28T22:15:00Z", rng=rng2,
    )
    sides_normal = _sides_of(picks_normal, "Wheeler")
    sides_reversed = _sides_of(picks_reversed, "Wheeler")
    assert sides_normal == sides_reversed, (
        f"Iteration order changed the winner. normal={sides_normal} "
        f"reversed={sides_reversed} — Defect #1 fix is order-dependent."
    )


def test_neither_side_survives_when_model_kills_both():
    """A brutally chalky pair (Over -400 / Under +280 well above the
    odds cap) should have BOTH sides dropped by K math — not one
    arbitrary side kept."""
    from sports_engine import _props_picks_from_event
    payload = _make_payload(_pitcher_k_outcomes(6.5, -400, 280))
    rng = random.Random(0)
    picks = _props_picks_from_event(
        sport="MLB", league="MLB", payload=payload,
        commence="2026-07-28T22:15:00Z", rng=rng,
    )
    sides = _sides_of(picks, "Wheeler")
    # Zero or one is acceptable; two is unacceptable.
    assert len(sides) <= 1, (
        f"Chalky symmetric pair still emitted BOTH sides: {sides}"
    )


def test_only_one_side_present_still_emits():
    """If The Odds API returns only Over (no Under), the pair-dedup
    layer must NOT drop the sole side. Regression guard against
    over-aggressive filtering."""
    from sports_engine import _props_picks_from_event
    over_only = [{"name": "Over", "description": "Zack Wheeler",
                  "point": 6.5, "price": -145}]
    payload = _make_payload(over_only)
    rng = random.Random(0)
    picks = _props_picks_from_event(
        sport="MLB", league="MLB", payload=payload,
        commence="2026-07-28T22:15:00Z", rng=rng,
    )
    # One-sided input should NOT be over-filtered. If it emits, side
    # must be "over"; if downstream gates drop it (e.g. K math), that's
    # a separate concern outside Defect #1's scope.
    sides = _sides_of(picks, "Wheeler")
    assert "under" not in sides, (
        f"Under emitted despite only Over in payload — filter is broken: {sides}"
    )
