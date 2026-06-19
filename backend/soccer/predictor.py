"""Soccer prediction engine — confidence-scored picks built from
API-Football data (fixtures + standings + form).

MVP scope: produce a per-fixture Match Result prediction (HOME / DRAW /
AWAY) with a confidence score 0-100, using:

  • Recent form string ("WWDLW") — last 5 results, recency-weighted
  • Goal differential per game in the standings
  • Home advantage offset (small constant — leagues vary, MVP keeps it flat)

The output is shape-compatible with the existing `picks` collection so
the Locks/Killer/Rollover tabs render them with no UI changes.

This is intentionally simple — once we have data flowing and the user
sees value, we can plug in lineups + injuries + xG features.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .normalize import normalize_match, normalize_standing_row  # noqa: F401

logger = logging.getLogger("lockscore.soccer.predictor")

MODEL_VERSION = "soccer.v1.0.0-mvp"

# Each W/D/L position from oldest → newest, recency-weighted.
_FORM_WEIGHTS = [0.5, 0.7, 0.85, 1.0, 1.2]
_FORM_POINTS  = {"W": 3.0, "D": 1.0, "L": 0.0}
_HOME_ADV     = 0.30   # ~5% baseline edge for hosts on neutral fixtures


def _form_score(form: str | None) -> float:
    """Weighted form score in [0, 3]. Returns 1.5 (neutral) if unknown."""
    if not form:
        return 1.5
    chars = form.strip().upper()[-5:]      # last 5 results
    if not chars:
        return 1.5
    weights = _FORM_WEIGHTS[-len(chars):]
    total_w = sum(weights)
    s = sum(_FORM_POINTS.get(c, 1.0) * w for c, w in zip(chars, weights))
    return s / total_w


def _goal_diff_per_game(row: dict) -> float:
    """Average goal differential — strong proxy for team strength."""
    played = row.get("played") or 0
    gd = row.get("goal_diff") or 0
    if played <= 0:
        return 0.0
    return gd / played


def _confidence(home_strength: float, away_strength: float) -> tuple[str, float]:
    """Pick the strongest side; if scores are basically tied, return DRAW.

    Confidence is 50 + (signed strength gap × scaling). Capped at 96 so we
    never emit absurd certainty from a 4-feature model.
    """
    gap = home_strength - away_strength
    abs_gap = abs(gap)
    if abs_gap < 0.25:
        # Genuine coin-flip → recommend draw at modest confidence.
        conf = 55.0 + (0.25 - abs_gap) * 30.0
        return "DRAW", round(min(conf, 70.0), 1)
    side = "HOME" if gap > 0 else "AWAY"
    conf = 60.0 + abs_gap * 25.0     # +1.4 gap → ~95 conf
    return side, round(min(conf, 96.0), 1)


def build_prediction(fixture_raw: dict, standings_index: dict[int, dict]) -> dict | None:
    """Produce one prediction for a fixture.

    `standings_index` is { team_id → standing_row_dict } so we can look
    up each side's form + goal diff without re-querying the API.
    Returns None when both teams are missing from standings (e.g.
    friendlies / lower-division cup opponents not in the table) —
    those fixtures get skipped so we never emit a low-quality
    prediction.
    """
    fx = normalize_match(fixture_raw)
    home_id = fx.get("home_team_id")
    away_id = fx.get("away_team_id")
    if not home_id or not away_id:
        return None
    home_row = standings_index.get(home_id)
    away_row = standings_index.get(away_id)
    if not home_row and not away_row:
        return None

    home_form_pts = _form_score((home_row or {}).get("form"))
    away_form_pts = _form_score((away_row or {}).get("form"))
    home_gd = _goal_diff_per_game(home_row or {})
    away_gd = _goal_diff_per_game(away_row or {})

    # Strength = standardized form (0-3 → 0-1) + GD/game scale + home boost.
    home_strength = (home_form_pts / 3.0) + home_gd * 0.4 + _HOME_ADV
    away_strength = (away_form_pts / 3.0) + away_gd * 0.4

    pick_side, conf = _confidence(home_strength, away_strength)
    if pick_side == "HOME":
        selection = fx["home_team_name"]
    elif pick_side == "AWAY":
        selection = fx["away_team_name"]
    else:
        selection = "Draw"

    # Deterministic ID: same fixture + same model = same id so re-runs
    # upsert cleanly instead of duplicating.
    sig = f"{fx['fixture_id']}|{MODEL_VERSION}|moneyline"
    pred_id = str(uuid.UUID(hashlib.md5(sig.encode()).hexdigest()))

    return {
        "id":             pred_id,
        "fixture_id":     fx["fixture_id"],
        "market":         "Match Result (1X2)",
        "selection":      selection,
        "pick_side":      pick_side,     # HOME | DRAW | AWAY
        "confidence":     conf,
        "model_version":  MODEL_VERSION,
        "event":          f"{fx['away_team_name']} @ {fx['home_team_name']}",
        "event_time":     fx["date"],
        "league_id":      fx["league_id"],
        "league":         fx["league_name"],
        "sport":          "Soccer",
        "home_team_id":   home_id,
        "away_team_id":   away_id,
        "features": {
            "home_form":     home_form_pts,
            "away_form":     away_form_pts,
            "home_gd_per_g": home_gd,
            "away_gd_per_g": away_gd,
            "home_strength": home_strength,
            "away_strength": away_strength,
        },
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }


def to_picks_collection_doc(pred: dict) -> dict:
    """Convert a soccer prediction into the existing `picks` schema so it
    shows up in Locks/Killer/Rollover tabs (user choice 2B).

    Mapping:
      • confidence (0-100) → lock_score and win_probability
      • Soccer doesn't have odds in this MVP — set safe defaults so the
        existing UI doesn't crash. Once we add an Odds API cross-ref
        we'll populate book_odds / edge_percent properly.
    """
    conf = float(pred.get("confidence") or 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "id":               pred["id"],
        "sport":            "Soccer",
        "league":           pred.get("league") or "Soccer",
        "event":            pred["event"],
        "event_time":       pred.get("event_time") or "",
        "market":           pred["market"],
        "selection":        pred["selection"],
        "win_probability":  round(conf / 100.0, 4),
        "implied_probability": 0.5,  # placeholder until Odds cross-ref
        "book_odds":        100,     # placeholder — keeps the UI happy
        "edge_percent":     round((conf - 50.0) / 5.0, 2),  # synthetic
        "lock_score":       round(conf, 1),
        "grade":            _grade_from_conf(conf),
        "pick_date":        today,
        "is_under_lock":    False,
        "no_bet":           conf < 60.0,     # very low conf → don't show
        "elite_player":     False,
        "deep_dive":        False,
        "source":           "soccer_v1",
        "model_version":    pred["model_version"],
        "created_at":       pred["created_at"],
    }


def _grade_from_conf(c: float) -> str:
    if c >= 90: return "ELITE"
    if c >= 85: return "A+"
    if c >= 80: return "A"
    if c >= 75: return "B+"
    if c >= 70: return "B"
    return "C"
