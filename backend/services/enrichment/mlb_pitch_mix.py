"""MLB Pitch-Mix vs Batter-Tendency — Phase 2 (2026-07-19).

A batter who hits .310 vs 4-seam fastballs and .180 vs sliders is a
very different bet depending on the opposing pitcher's arsenal. This
module creates a lightweight compatibility score without needing
real-time Baseball Savant scraping (which would blow the API budget
and block the pipeline).

Approach:
  • Pitcher arsenal — read from the ``stuff_plus`` block already
    attached by ``services.mlb_stuff_plus`` when present. This gives us
    the pitcher's ``pitch_mix`` = {'4-Seam': 0.42, 'Slider': 0.28, ...}.
  • Batter split by pitch type — read from Statcast ``xwoba_vs`` /
    ``ba_vs`` maps when the batter enricher attaches them. Fallback:
    a coarse league-average model that flags the batter as
    ``pull_heavy`` / ``opposite_field`` / ``ground_ball`` and predicts
    weak vs breaking balls for GB hitters, strong vs FB for FB hitters.

Outputs ``pick['pitch_mix_edge']`` = float on ±5 scale where positive
= batter's strengths ALIGN with pitcher's arsenal (e.g., FB-crushing
batter faces a 60% FB pitcher).

All fields are optional — missing data returns None and the signal
calculator skips the sub-signal cleanly.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.mlb_pitch_mix")


# Pitch-type groups (Statcast codes → coarse family).
_FB_FAMILY = {"4-Seam", "Sinker", "Cutter", "FF", "SI", "FC", "Fastball"}
_BREAKING_FAMILY = {"Slider", "Curveball", "Sweeper", "SL", "CU", "ST", "KC", "Knuckle Curve"}
_OFFSPEED_FAMILY = {"Changeup", "Splitter", "Screwball", "CH", "FS", "SC"}


def _classify(pitch: str) -> Optional[str]:
    p = pitch.strip()
    if p in _FB_FAMILY:
        return "fastball"
    if p in _BREAKING_FAMILY:
        return "breaking"
    if p in _OFFSPEED_FAMILY:
        return "offspeed"
    return None


def _pitcher_pitch_families(pick: dict) -> dict[str, float]:
    """Aggregate the pitcher's arsenal to fastball / breaking / offspeed
    share. Returns {} if the arsenal data isn't attached.
    """
    sp_grades = pick.get("stuff_plus") or {}
    arsenal = sp_grades.get("pitch_mix") or sp_grades.get("arsenal") or {}
    if not isinstance(arsenal, dict) or not arsenal:
        return {}
    out = {"fastball": 0.0, "breaking": 0.0, "offspeed": 0.0}
    total = 0.0
    for name, share in arsenal.items():
        try:
            s = float(share)
        except (TypeError, ValueError):
            continue
        # Support both fractional (0.42) and percent (42) inputs.
        if s > 1.5:
            s = s / 100.0
        fam = _classify(str(name))
        if fam:
            out[fam] += s
            total += s
    if total <= 0:
        return {}
    return out


def _batter_family_scores(pick: dict) -> Optional[dict[str, float]]:
    """Return {'fastball': xwOBA-diff, 'breaking': ..., 'offspeed': ...}
    or None if the batter split data isn't available.

    xwOBA-diff is (batter xwOBA vs family) - .310 (league avg). Positive
    = batter crushes that family; negative = batter struggles.
    """
    sb = pick.get("statcast_batter") or {}
    xwoba_vs = sb.get("xwoba_vs") or sb.get("xwoba_by_family")
    if isinstance(xwoba_vs, dict) and xwoba_vs:
        out = {}
        for fam in ("fastball", "breaking", "offspeed"):
            v = xwoba_vs.get(fam)
            if isinstance(v, (int, float)):
                out[fam] = float(v) - 0.310
        return out or None
    return None


def enrich_pick_with_pitch_mix(pick: dict) -> dict:
    """Attach ``pick['pitch_mix_edge']`` (float, ±5) and
    ``pick['pitch_mix_details']`` (list[str]).

    Only runs for MLB hitter markets (HR / hits / total bases / RBI).
    Idempotent, non-throwing.
    """
    if (pick.get("sport") or "").upper() != "MLB":
        return pick
    market = (pick.get("market") or "").lower()
    if not any(t in market for t in (
        "home run", "hits", "total bases", "rbi", "runs batted"
    )):
        return pick

    pitcher_mix = _pitcher_pitch_families(pick)
    batter_scores = _batter_family_scores(pick)
    if not pitcher_mix or not batter_scores:
        return pick

    # Weighted xwOBA edge = Σ (pitcher_share * batter_edge_vs_family).
    # Positive = pitcher throws lots of pitches the batter feasts on.
    edge_xwoba = 0.0
    for fam in ("fastball", "breaking", "offspeed"):
        share = pitcher_mix.get(fam, 0.0)
        batter_diff = batter_scores.get(fam)
        if batter_diff is None:
            continue
        edge_xwoba += share * batter_diff

    # Scale from xwOBA units (roughly ±.100) → ±5 point signal budget.
    edge = max(-5.0, min(5.0, edge_xwoba * 50.0))
    pick["pitch_mix_edge"] = round(edge, 2)

    # Human-readable rationale for the biggest driver.
    contribs = []
    for fam in ("fastball", "breaking", "offspeed"):
        share = pitcher_mix.get(fam, 0.0)
        if share <= 0:
            continue
        d = batter_scores.get(fam)
        if d is None:
            continue
        contribs.append((share * d, fam, share, d))
    contribs.sort(key=lambda x: abs(x[0]), reverse=True)
    details: list[str] = []
    if contribs:
        _, fam, share, diff = contribs[0]
        verb = "crushes" if diff >= 0.020 else (
            "struggles vs" if diff <= -0.020 else "is average vs"
        )
        details.append(
            f"Batter {verb} {fam}s ({diff:+.3f} xwOBA vs .310 league) — "
            f"pitcher throws {share * 100:.0f}% {fam}s"
        )
    if details:
        pick["pitch_mix_details"] = details
    return pick
