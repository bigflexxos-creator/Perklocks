"""ESPN Signal Engine — analysis layer, NOT a display layer.

Rationale (per user directive 2026-07-09): ESPN data is more valuable
folded into the *model* than shown as chips. This module converts
ESPN context (injury reports, recent form, record deltas) into a signed
adjustment on `win_probability`, then recomputes `lock_score` and
records the reasoning in `factors` so \"Why This Pick?\" can explain
*why* the pick moved.

Design principles:
  1. **Bounded**  — total adjustment capped at ±6 pts. ESPN is a
     minority signal alongside odds/matchup/simulation.
  2. **Directional** — each signal has a fixed sign convention keyed to
     the *pick side*, not the home/away side. Missing key player on
     pick side ⇒ negative; missing key player on opponent ⇒ positive.
  3. **Auditable** — every applied delta is stored under
     `espn_signals.items` so we can debug drift or run backtests.
  4. **Idempotent** — safe to call twice on the same pick; uses the
     stored `pre_espn_win_probability` as the base.

Signal families (v1):
  A. INJURY  – count and severity of pick-side vs opponent injuries.
  B. FORM    – 5-game weighted W/D/L delta between the two teams.
  C. RECORD  – (UFC/MMA) career win-rate delta — already applied at
                creation time; we skip re-applying here to avoid
                double counting.

Future (v2, TODO):
  • Player-prop suppression — if the subject player is on the injury
    report as OUT, mark the pick `no_bet=True` regardless of odds.
  • Weather signal — combine with existing weather module for totals.
  • Rest-day signal — days-since-last-game asymmetry.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .espn_common import form_win_share
from .espn_injury_notes import get_team_injuries
from .espn_team_meta import normalize_name

logger = logging.getLogger("lockscore.services.espn_signal_engine")

# ── tunable weights ─────────────────────────────────────────────────
# Kept conservative on purpose — ESPN is one of many signals. Total
# possible swing capped at ±6.0 pts, chosen so a single injury can't
# flip a 60% pick to a 66% pick on its own.

_INJURY_WEIGHTS = {
    "out":          3.0,   # each side's OUT starter
    "doubtful":     1.5,
    "questionable": 0.6,
}
_OPPONENT_MULT = 0.7   # opponent injuries help our pick a bit less than
                       # our own injuries hurt us (books already price
                       # the pick-side more efficiently)

_FORM_MAX_SWING = 3.0  # ±3 pts from form delta

_MAX_TOTAL_SWING = 6.0

# Sports that carry the ESPN injury feed (from espn_injury_notes)
_INJURY_SPORTS = {"NFL", "NBA", "CFB", "MLB", "NHL", "WNBA"}

# Sports whose events are two-team match-ups where our "pick side"
# maps cleanly to home/away. Combat sports handled separately.
_TEAM_SPORTS = {"NFL", "NBA", "CFB", "MLB", "NHL", "WNBA", "NCAAB", "Soccer"}


def _pick_side(pick: dict) -> Optional[str]:
    """Return 'home' | 'away' | None based on the selection text vs event.

    Robust to markets like 'X Moneyline', 'X -1.5', 'X Team Total Over 4.5',
    and short abbreviations ('KC ML').
    """
    event = pick.get("event") or ""
    sel = (pick.get("selection") or "").strip()
    if not event or not sel:
        return None
    away = home = None
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)
    if not (home and away):
        return None
    sel_norm = normalize_name(sel)
    home_norm = normalize_name(home)
    away_norm = normalize_name(away)
    if not sel_norm:
        return None
    # Substring match (either direction) so 'KC ML' matches 'Kansas City Chiefs'
    # after normalization drops the ' ml'.
    sel_stem = sel_norm.replace("ml", "").replace("moneyline", "")
    if home_norm and (sel_stem in home_norm or home_norm in sel_stem):
        return "home"
    if away_norm and (sel_stem in away_norm or away_norm in sel_stem):
        return "away"
    # Draw / no-side markets (soccer draw picks, totals, spreads)
    return None


def _injury_bucket(injuries: list[dict]) -> dict[str, int]:
    out = {"out": 0, "doubtful": 0, "questionable": 0}
    for i in injuries:
        s = (i.get("status") or "").lower()
        if "out" in s and "probab" not in s:
            out["out"] += 1
        elif "doubt" in s:
            out["doubtful"] += 1
        elif "question" in s or "day" in s:
            out["questionable"] += 1
    return out


def _injury_signal(pick: dict, side: str,
                    home_inj: list[dict],
                    away_inj: list[dict]) -> tuple[float, list[dict]]:
    """Compute injury-driven pt swing on the pick.
    Returns (delta_pct, list_of_signal_items).
    """
    own = home_inj if side == "home" else away_inj
    opp = away_inj if side == "home" else home_inj
    own_b = _injury_bucket(own)
    opp_b = _injury_bucket(opp)

    delta = 0.0
    items: list[dict] = []
    # Own-side hits (negative)
    for tier, w in _INJURY_WEIGHTS.items():
        cnt = own_b[tier]
        if cnt > 0:
            d = -w * min(cnt, 3)   # diminishing return past 3
            delta += d
            items.append({
                "kind":  "injury",
                "side":  "pick",
                "tier":  tier,
                "count": cnt,
                "delta": round(d, 2),
            })
    # Opponent hits (positive)
    for tier, w in _INJURY_WEIGHTS.items():
        cnt = opp_b[tier]
        if cnt > 0:
            d = _OPPONENT_MULT * w * min(cnt, 3)
            delta += d
            items.append({
                "kind":  "injury",
                "side":  "opponent",
                "tier":  tier,
                "count": cnt,
                "delta": round(d, 2),
            })
    return delta, items


def _form_signal(pick: dict, side: str) -> tuple[float, list[dict]]:
    """Delta driven by ESPN recent-form strings on home/away team_meta.
    Only fires when both sides have a form string on the pick doc.
    """
    home_meta = pick.get("home_meta") or {}
    away_meta = pick.get("away_meta") or {}
    home_form = (home_meta.get("form") or "").upper()
    away_form = (away_meta.get("form") or "").upper()
    if not home_form or not away_form:
        return (0.0, [])
    home_share = form_win_share(home_form)
    away_share = form_win_share(away_form)
    # Positive when the pick-side is on the better form streak.
    diff = (home_share - away_share) if side == "home" else (away_share - home_share)
    delta = max(-_FORM_MAX_SWING, min(_FORM_MAX_SWING, diff * (2 * _FORM_MAX_SWING)))
    items = [{
        "kind":       "form",
        "pick_form":  home_form if side == "home" else away_form,
        "opp_form":   away_form if side == "home" else home_form,
        "diff":       round(diff, 3),
        "delta":      round(delta, 2),
    }]
    return delta, items


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _prob_to_lock(prob: float) -> float:
    """Keep in sync with sports_engine._grade. lock_score has always
    been ~=win_probability in this codebase, so we treat them 1:1
    and let the grader tier at read-time."""
    return round(_clamp(prob, 1.0, 99.5), 1)


async def _fetch_team_injuries(db, sport: str, event: str) -> tuple[list[dict], list[dict]]:
    """Return (home_injuries, away_injuries) for the pick's event."""
    if not event or sport not in _INJURY_SPORTS:
        return ([], [])
    home = away = ""
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)
    if not (home and away):
        return ([], [])
    home_inj = await get_team_injuries(db, sport, home.strip())
    away_inj = await get_team_injuries(db, sport, away.strip())
    return home_inj, away_inj


async def apply_signals(db, pick: dict) -> dict:
    """Mutate `pick` in-place, applying ESPN-derived probability
    adjustments and recording the reasoning under `espn_signals`.

    - No-op when the pick lacks a clear home/away side, when the sport
      isn't ESPN-covered, or when the pick already has an
      `espn_signals` block written by a previous run.

    - Adjusts `win_probability` and `lock_score` bounded to ±6 pts.

    - Adds new bullets to `factors` so \"Why This Pick?\" shows them.
    """
    if not pick or pick.get("espn_signals"):
        return pick   # idempotent

    sport = pick.get("sport")
    if sport not in _TEAM_SPORTS and sport != "UFC":
        return pick   # nothing to adjust yet

    side = _pick_side(pick)
    if not side:
        # Markets without a clear team side (spread totals, draw ML,
        # goalscorers) still benefit from *displaying* injury info via
        # the enrichment path, but we don't adjust win_probability
        # here to avoid mis-attribution.
        return pick

    base = float(pick.get("win_probability") or 0.0)
    if base <= 0:
        return pick

    # Fetch context in parallel where possible
    home_inj, away_inj = await _fetch_team_injuries(db, sport, pick.get("event") or "")

    signals: list[dict] = []
    delta = 0.0

    inj_d, inj_items = _injury_signal(pick, side, home_inj, away_inj)
    delta += inj_d
    signals.extend(inj_items)

    form_d, form_items = _form_signal(pick, side)
    delta += form_d
    signals.extend(form_items)

    # Clamp total swing
    delta = _clamp(delta, -_MAX_TOTAL_SWING, _MAX_TOTAL_SWING)

    if abs(delta) < 0.25:
        # Not enough signal to disturb the pick — still record the
        # empty analysis so the read-side knows we tried.
        pick["espn_signals"] = {
            "applied":   False,
            "delta":     0.0,
            "base_prob": base,
            "items":     signals,
        }
        return pick

    new_prob = _clamp(base + delta, 1.0, 99.5)
    new_lock = _prob_to_lock(new_prob)

    # Preserve base for auditability / idempotency
    pick["pre_espn_win_probability"] = base
    pick["win_probability"]          = round(new_prob, 2)
    pick["lock_score"]                = new_lock
    if pick.get("lock_score_v2") is not None:
        pick["lock_score_v2"] = new_lock
    pick["espn_signals"] = {
        "applied":   True,
        "delta":     round(delta, 2),
        "base_prob": base,
        "final_prob": pick["win_probability"],
        "items":     signals,
        "side":      side,
    }

    # Human-readable bullets for the rationale panel
    factors = pick.setdefault("factors", {})
    if isinstance(factors, dict):
        summary = []
        if inj_items:
            own_hits = sum(i["count"] for i in inj_items if i["side"] == "pick")
            opp_hits = sum(i["count"] for i in inj_items if i["side"] == "opponent")
            if own_hits or opp_hits:
                summary.append(
                    f"Injuries — pick side: {own_hits} issue{'s' if own_hits != 1 else ''}, "
                    f"opponent: {opp_hits}"
                )
        if form_items:
            f = form_items[0]
            summary.append(
                f"Form — pick side {f['pick_form'] or 'n/a'} vs opponent {f['opp_form'] or 'n/a'}"
            )
        factors["ESPN Signal Adjustment"] = (
            f"{'+' if delta > 0 else ''}{round(delta, 2)}pp on win probability "
            f"({base}% → {pick['win_probability']}%). "
            + " · ".join(summary)
        )

    return pick


async def apply_signals_bulk(db, picks: list[dict]) -> list[dict]:
    """Batch entry-point. Runs `apply_signals` on each pick sequentially
    since each call is dominated by two mongo reads — not worth
    parallelising for our slate sizes (<300 picks).
    """
    for p in picks:
        try:
            await apply_signals(db, p)
        except Exception as e:
            logger.warning("apply_signals failed for pick %s: %s", p.get("id"), e)
    return picks
