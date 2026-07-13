"""Universal signal calculators — Phase A.

Six independent signals, each returning a component dict:

    {
      "key":     "form" | "matchup" | "volume" | "injury" | "market" | "value",
      "label":   human label,
      "points":  signed contribution (float),
      "max":     absolute cap for this component,
      "details": [str, ...]   — real numbers, never generic text,
      "found":   bool         — whether ANY underlying data existed,
    }

Budgets sum to ±50 so `score = 50 + Σpoints` spans 0-100:
    form ±12 · matchup ±8 · volume ±7 · injury ±8 · market ±7 · value ±8

Every calculator is defensive — picks vary wildly across sports/markets
and a missing field must yield a neutral (0-point, found=False) block,
never an exception.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("lockscore.services.signal_engine")

FORM_MAX = 12.0
MATCHUP_MAX = 8.0
VOLUME_MAX = 7.0
INJURY_MAX = 8.0
MARKET_MAX = 7.0
VALUE_MAX = 8.0
# Phase B.1 — MLB deep signal (park factors + market-family fit).
# Neutral 0-point return for non-MLB picks, so score remains centered
# at 50 across sports. Small budget (±5) keeps existing calibration
# intact while adding a real evidence layer for MLB hitters/pitchers.
MLB_DEEP_MAX = 5.0
# Phase B.4 — Soccer deep signal (xG regression + xG differential +
# home advantage + league tier). Same ±5 budget so per-sport signals
# scale evenly across the score.
SOCCER_DEEP_MAX = 5.0
# Phase B.5 — Tennis deep signal (surface fit + serve/return dominance +
# motivation + variance risk + surface-Elo delta + recent match load).
# Same ±5 budget for cross-sport consistency.
TENNIS_DEEP_MAX = 5.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_LINE_RE = re.compile(r"\b(over|under)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _extract_line(market: str) -> tuple[Optional[float], Optional[str]]:
    """'Walker Buehler Over 2.5 Strikeouts' → (2.5, 'over')."""
    m = _LINE_RE.search(market or "")
    if not m:
        return None, None
    return float(m.group(2)), m.group(1).lower()


def _player_name(pick: dict) -> Optional[str]:
    # 1) explicit field
    n = pick.get("player_name")
    if n:
        return n
    # 2) selection when it's a person, not one of the event's teams
    sel = str(pick.get("selection") or "").strip()
    home = str(pick.get("home_team") or "").strip()
    away = str(pick.get("away_team") or "").strip()
    if sel and sel != home and sel != away:
        base = re.sub(r"\s*\([^)]*\)\s*", " ", sel).strip()
        words = base.split()
        if 2 <= len(words) <= 4 and not _LINE_RE.search(base) \
                and not any(base in t for t in (home, away) if t):
            return base
    # 3) market prefix — "Mookie Betts (LAD) Over 0.5 Hits"
    m = re.match(
        r"^([A-Z][\w.'\u00C0-\u017F-]*(?:\s+[A-Z][\w.'\u00C0-\u017F-]*){1,3})\s*(?:\(|Over\b|Under\b)",
        pick.get("market") or "")
    if m:
        cand = m.group(1).strip()
        if cand != home and cand != away:
            return cand
    # 4) shared resolver (fuzzy market parser)
    try:
        from player_intel.resolver import extract_player_from_market
        return extract_player_from_market(pick.get("market") or "")
    except Exception:
        return None


_STAT_LABELS = {
    "pitcher_strikeouts": "strikeouts",
    "home_runs": "home runs",
    "total_bases": "total bases",
    "threes_made": "threes",
    "nfl_yds": "yards",
    "nfl_td": "TDs",
    "nfl_rec": "receptions",
}


def _stat_label(stat: str) -> str:
    return _STAT_LABELS.get(stat, (stat or "").replace("_", " "))


def _american_to_prob(odds: float) -> Optional[float]:
    """American odds → implied probability (0-1)."""
    if not odds:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _american_payout(odds: float) -> Optional[float]:
    """Profit per $1 staked."""
    if not odds:
        return None
    if odds > 0:
        return odds / 100.0
    return 100.0 / abs(odds)


# ─────────────────────────────────────────────────────────────────────
# 1. FORM — L5/L10/season averages, trend direction, consistency
# ─────────────────────────────────────────────────────────────────────
async def form_signal(db, pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    # 1a. Real game-log form (historical store — MLB/NBA/NFL/NHL/Soccer)
    name = _player_name(pick)
    if name:
        try:
            from historical.lookup import get_player_form
            pf = await get_player_form(
                pick.get("sport") or "", name, market_hint=pick.get("market"))
        except Exception:
            pf = None
        if pf and int(pf.get("games_logged") or 0) >= 3:
            stat = pf.get("headline_stat") or ""
            # Headline-stat correction — the lookup maps "Strikeouts" to
            # BATTER strikeouts unless the market says "pitcher". Use the
            # player's position / log profile to pick the pitcher stat.
            mkt_l = (pick.get("market") or "").lower()
            l10_map = pf.get("last10_avg") or {}
            if "strikeout" in mkt_l and _f(l10_map.get("pitcher_strikeouts")) > 0:
                stat = "pitcher_strikeouts"
            l5 = _f((pf.get("last5_avg") or {}).get(stat))
            l10 = _f(l10_map.get(stat))
            # Guard: when the mapped stat has NO production in the entire
            # sample (e.g. "Outs Recorded" markets have no direct log
            # stat), skip the game-log block instead of issuing a bogus
            # cold-consistency penalty against the wrong stat.
            if l5 > 0 or l10 > 0:
                found = True
                line, direction = _extract_line(pick.get("market") or "")
                if line is not None and l5 > 0:
                    ratio = (l5 - line) / max(line, 0.5)
                    if direction == "under":
                        ratio = -ratio
                    d = _clamp(ratio * 8.0, -6.0, 6.0)
                    pts += d
                    rel = "above" if (l5 - line) >= 0 else "below"
                    details.append(
                        f"L5 avg {l5:g} {_stat_label(stat)} — "
                        f"{abs(l5 - line) / max(line, 0.5) * 100:.0f}% {rel} the {line:g} line")
                # Trend + consistency come from the lookup's OWN headline
                # stat — only trustworthy when we didn't override it.
                if stat == (pf.get("headline_stat") or ""):
                    trend = pf.get("trend")
                    if trend == "hot":
                        pts += 2.0
                        details.append(
                            f"Trend rising — L5 {l5:g} vs L10 {l10:g} {_stat_label(stat)}")
                    elif trend == "cold":
                        pts -= 2.0
                        details.append(
                            f"Trend falling — L5 {l5:g} vs L10 {l10:g} {_stat_label(stat)}")
                    cons = _f(pf.get("consistency"))
                    if cons > 0:
                        pts += (cons - 0.5) * 6.0
                        details.append(
                            f"Produced {_stat_label(stat)} in {round(cons * 10)}/10 recent games")

    # 1b. ESPN team-form delta (already computed by espn_signal_engine)
    esp = pick.get("espn_signals") or {}
    for item in esp.get("items") or []:
        if item.get("kind") == "form":
            found = True
            pts += _clamp(_f(item.get("delta")) * 1.3, -4.0, 4.0)
            pf_str = item.get("pick_form") or "n/a"
            of_str = item.get("opp_form") or "n/a"
            details.append(f"Last-5 team form {pf_str} vs opponent {of_str}")

    # 1c. Understat xG form (soccer goalscorers)
    uf = pick.get("understat_form") or {}
    if uf.get("label") in ("HOT", "COLD"):
        found = True
        hot = uf["label"] == "HOT"
        pts += 2.0 if hot else -2.0
        g, gm, xg = uf.get("goals"), uf.get("games"), uf.get("xg")
        if g is not None and gm:
            details.append(
                f"{'Hot' if hot else 'Cold'} xG form — {g} goals in {gm} matches"
                + (f" ({_f(xg):.1f} xG)" if xg is not None else ""))

    # 1d. Pick-history hit rate (player_profiles_v2 decorator)
    pfm = pick.get("player_form") or {}
    l10h = pfm.get("last10_hit")
    if isinstance(l10h, (int, float)) and int(pfm.get("n_picks") or 0) >= 5:
        found = True
        rate = _f(l10h)
        rate = rate / 100.0 if rate > 1 else rate
        if rate >= 0.7:
            pts += 2.0
            details.append(f"{round(rate * 10)}/10 hit rate on recent graded picks")
        elif rate <= 0.3:
            pts -= 2.0
            details.append(f"Only {round(rate * 10)}/10 recent graded picks hit")

    # 1e. Historical hot/cold signal (nudge layer)
    hs = pick.get("historical_signal") or {}
    if hs.get("label") == "hot":
        found = True
        pts += 1.5
    elif hs.get("label") == "cold":
        found = True
        pts -= 1.5

    return {
        "key": "form", "label": "Form",
        "points": round(_clamp(pts, -FORM_MAX, FORM_MAX), 1),
        "max": FORM_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 2. MATCHUP — opponent strength, positional matchup, H2H history
# ─────────────────────────────────────────────────────────────────────
async def matchup_signal(db, pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    # 2a. Season record delta (Wikipedia via espn_signal_engine)
    esp = pick.get("espn_signals") or {}
    for item in esp.get("items") or []:
        if item.get("kind") == "record":
            found = True
            pts += _clamp(_f(item.get("delta")) * 1.2, -5.0, 5.0)
            pw, ow = item.get("pick_wdl"), item.get("opp_wdl")
            if pw and ow:
                details.append(
                    f"Season record {pw} ({item.get('pick_wr')}%) vs "
                    f"opponent {ow} ({item.get('opp_wr')}%)")

    # 2b. Goalscorer matchup engine (soccer)
    ms = pick.get("matchup_score")
    if isinstance(ms, (int, float)):
        found = True
        pts += _clamp((_f(ms) - 50.0) / 50.0 * 5.0, -5.0, 5.0)
        grade = pick.get("matchup_grade")
        details.append(
            f"Matchup engine score {round(_f(ms))}/100"
            + (f" (grade {grade})" if grade else ""))

    # 2c. Batter-vs-Pitcher career history (MLB)
    bvp = pick.get("bvp_history") or {}
    ab = int(_f(bvp.get("ab")))
    if ab >= 8:
        found = True
        avg = _f(bvp.get("avg"))
        h = int(_f(bvp.get("h")))
        if avg >= 0.300:
            pts += 2.0
        elif avg <= 0.150:
            pts -= 2.0
        details.append(
            f"{h}-for-{ab} ({avg:.3f}) career vs {bvp.get('pitcher_name') or 'this pitcher'}")

    return {
        "key": "matchup", "label": "Matchup",
        "points": round(_clamp(pts, -MATCHUP_MAX, MATCHUP_MAX), 1),
        "max": MATCHUP_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 3. VOLUME — usage, minutes, role, opportunities
# ─────────────────────────────────────────────────────────────────────
def volume_signal(pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    sp = pick.get("starter_probability")
    if isinstance(sp, (int, float)):
        found = True
        spf = _f(sp)
        spf = spf / 100.0 if spf > 1 else spf
        if spf >= 0.85:
            pts += 2.0
            details.append(f"Projected starter ({spf * 100:.0f}% start probability)")
        elif spf < 0.5:
            pts -= 3.0
            details.append(f"Bench risk — only {spf * 100:.0f}% start probability")

    em = pick.get("expected_minutes")
    if isinstance(em, (int, float)):
        found = True
        if _f(em) >= 80:
            pts += 1.5
            details.append(f"Expected minutes {round(_f(em))} — full workload")
        elif _f(em) < 60:
            pts -= 1.5
            details.append(f"Expected minutes only {round(_f(em))} — reduced workload")

    if pick.get("penalty_taker"):
        found = True
        pts += 1.5
        details.append("Primary penalty taker — extra scoring path")

    role = str(pick.get("role") or "").lower()
    if role in ("primary", "talisman", "focal point"):
        found = True
        pts += 1.0
        details.append(f"Role: {role} attacking option")

    xg = pick.get("sim_player_xg")
    if isinstance(xg, (int, float)) and _f(xg) > 0:
        found = True
        if _f(xg) >= 0.5:
            pts += 1.0
        details.append(f"Simulated expected goals {_f(xg):.2f} per 90")

    return {
        "key": "volume", "label": "Volume",
        "points": round(_clamp(pts, -VOLUME_MAX, VOLUME_MAX), 1),
        "max": VOLUME_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 4. INJURY — missing teammates, subject-player status, lineup shifts
# ─────────────────────────────────────────────────────────────────────
def injury_signal(pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    esp = pick.get("espn_signals") or {}
    own = {"out": 0, "doubtful": 0, "questionable": 0}
    opp = {"out": 0, "doubtful": 0, "questionable": 0}
    inj_delta = 0.0
    for item in esp.get("items") or []:
        if item.get("kind") != "injury":
            continue
        found = True
        inj_delta += _f(item.get("delta"))
        bucket = own if item.get("side") == "pick" else opp
        tier = item.get("tier")
        if tier in bucket:
            bucket[tier] += int(_f(item.get("count")))
    if found:
        pts += _clamp(inj_delta * 1.2, -6.0, 6.0)
        own_total = sum(own.values())
        opp_total = sum(opp.values())
        if own_total:
            details.append(
                f"Pick side missing {own_total} player{'s' if own_total > 1 else ''} "
                f"({own['out']} out, {own['doubtful']} doubtful, {own['questionable']} questionable)")
        if opp_total:
            details.append(
                f"Opponent depleted — {opp_total} player{'s' if opp_total > 1 else ''} on injury report")

    hurt = pick.get("subject_player_hurt") or {}
    status = str(hurt.get("status") or "").lower()
    if status:
        found = True
        if status in ("out", "doubtful"):
            pts -= 5.0
        elif status == "questionable":
            pts -= 2.0
        details.append(f"{hurt.get('athlete') or 'Subject player'} listed {status.title()}")

    return {
        "key": "injury", "label": "Injury",
        "points": round(_clamp(pts, -INJURY_MAX, INJURY_MAX), 1),
        "max": INJURY_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 5. MARKET — line movement, implied probability zone, CLV
# ─────────────────────────────────────────────────────────────────────
def market_signal(pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    open_odds = _f(pick.get("odds_at_pick"))
    now_odds = _f(pick.get("book_odds"))
    if open_odds and now_odds and open_odds != now_odds:
        imp_open = _american_to_prob(open_odds)
        imp_now = _american_to_prob(now_odds)
        if imp_open is not None and imp_now is not None:
            shift_pp = (imp_now - imp_open) * 100.0
            if abs(shift_pp) >= 0.5:
                found = True
                pts += _clamp(shift_pp * 0.6, -4.0, 4.0)
                def _fmt(o):
                    return f"+{o:.0f}" if o > 0 else f"{o:.0f}"
                direction = "toward" if shift_pp > 0 else "away from"
                details.append(
                    f"Line moved {_fmt(open_odds)} → {_fmt(now_odds)} "
                    f"(implied {imp_open * 100:.1f}% → {imp_now * 100:.1f}%) — "
                    f"market steaming {direction} this pick")

    imp = _f(pick.get("implied_probability"))
    if imp > 0:
        found = True
        if 30.0 <= imp <= 65.0:
            pts += 1.0
            details.append(f"Implied {imp:.1f}% sits in the healthy payout zone")
        elif imp >= 85.0:
            pts -= 2.0
            details.append(f"Heavy chalk — implied {imp:.1f}% limits payout upside")
        elif imp <= 12.0:
            pts -= 1.5
            details.append(f"Longshot territory — implied only {imp:.1f}%")

    lc = pick.get("lock_components")
    if isinstance(lc, dict):
        clv = _f(lc.get("clv"), 50.0)
        if abs(clv - 50.0) >= 5:
            found = True
            pts += _clamp((clv - 50.0) / 50.0 * 1.5, -1.5, 1.5)
            details.append(f"Closing-line value component {round(clv)}/100")

    return {
        "key": "market", "label": "Market",
        "points": round(_clamp(pts, -MARKET_MAX, MARKET_MAX), 1),
        "max": MARKET_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 6. VALUE — model vs book probability, EV, sim agreement
# ─────────────────────────────────────────────────────────────────────
def value_signal(pick: dict) -> dict:
    pts = 0.0
    details: list[str] = []
    found = False

    wp = _f(pick.get("win_probability"))
    imp = _f(pick.get("implied_probability"))
    edge = _f(pick.get("edge_percent"))
    if wp > 0 and imp > 0:
        found = True
        if edge > 12.0:
            # >12% edge is historically an inverted signal (V4 calibration)
            pts += 3.0
            details.append(
                f"Model {wp:.1f}% vs book {imp:.1f}% — edge +{edge:.1f}% "
                f"exceeds the calibration band, treated cautiously")
        else:
            pts += _clamp(edge / 12.0 * 6.0, -6.0, 6.0)
            details.append(
                f"Model {wp:.1f}% vs book implied {imp:.1f}% → "
                f"{'+' if edge >= 0 else ''}{edge:.1f}% edge")

    odds = _f(pick.get("book_odds"))
    payout = _american_payout(odds)
    if payout is not None and wp > 0:
        ev = (wp / 100.0) * payout - (1.0 - wp / 100.0)
        details.append(f"Expected value {'+' if ev >= 0 else ''}${ev:.2f} per $1 staked")

    sim = _f(pick.get("sim_win_probability"))
    if sim > 0 and wp > 0:
        found = True
        gap = sim - wp
        if abs(gap) <= 5.0:
            pts += 1.5
            details.append(f"Monte Carlo sim ({sim:.0f}%) confirms the model")
        elif gap >= 8.0:
            pts += 1.0
            details.append(f"Sim even stronger than model ({sim:.0f}% vs {wp:.0f}%)")
        elif gap <= -8.0:
            pts -= 1.5
            details.append(f"Sim disagrees — {sim:.0f}% vs model {wp:.0f}%")

    return {
        "key": "value", "label": "Value",
        "points": round(_clamp(pts, -VALUE_MAX, VALUE_MAX), 1),
        "max": VALUE_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 7. MLB DEEP — park factor + market-family fit (Phase B.1)
# ─────────────────────────────────────────────────────────────────────
def mlb_deep_signal(pick: dict) -> dict:
    """MLB-only enrichment layer. Reads `pick['mlb_deep']` (populated by
    `services.signal_engine.mlb_deep.enrich_mlb_pick`) and awards
    directional points based on:
      • Park factor vs the pick's market family (HR park + Over HR pick
        → +points, pitcher-friendly park + Over K pick → +points).
      • Whether the park lean AGREES with the pick's Over/Under side.

    Returns a neutral 0-point block for non-MLB picks or when
    `mlb_deep` isn't attached — identical to the Phase A calculators'
    fallback behaviour so scoring stays stable when the data isn't
    available.
    """
    pts = 0.0
    details: list[str] = []
    found = False

    if (pick.get("sport") or "").upper() != "MLB":
        return {
            "key": "mlb_deep", "label": "MLB Context",
            "points": 0.0, "max": MLB_DEEP_MAX,
            "details": [], "found": False,
        }
    deep = pick.get("mlb_deep") or {}
    if not deep:
        return {
            "key": "mlb_deep", "label": "MLB Context",
            "points": 0.0, "max": MLB_DEEP_MAX,
            "details": [], "found": False,
        }

    hr_pf   = int(deep.get("park_hr_factor") or 100)
    hits_pf = int(deep.get("park_hits_factor") or 100)
    run_pf  = int(deep.get("park_run_factor") or 100)
    park    = deep.get("park_name") or "the park"
    family  = deep.get("market_family") or ""

    # Direction alignment — over-side benefits from hitter-friendly
    # parks; under-side benefits from pitcher-friendly parks.
    _, direction = _extract_line(pick.get("market") or "")
    is_over = direction == "over"
    is_under = direction == "under"

    # HR-family: park HR factor
    if family in ("hr", "total_bases"):
        dev = hr_pf - 100
        found = True
        if is_over:
            pts += _clamp(dev * 0.10, -3.5, 3.5)
        elif is_under:
            pts += _clamp(-dev * 0.10, -3.5, 3.5)
        else:
            pts += _clamp(dev * 0.06, -2.0, 2.0)
        details.append(
            f"{park} park HR factor {hr_pf} "
            f"({'boost' if dev > 3 else 'suppress' if dev < -3 else 'neutral'} "
            f"vs league avg)")

    # Hits / RBI / HRR — hits factor is the primary driver
    elif family in ("hits", "rbi", "hrr"):
        dev = hits_pf - 100
        found = True
        if is_over:
            pts += _clamp(dev * 0.10, -3.0, 3.0)
        elif is_under:
            pts += _clamp(-dev * 0.10, -3.0, 3.0)
        details.append(
            f"{park} park hits factor {hits_pf}")

    # Pitcher K / IP / ER — pitcher-friendly park HELPS Over K, HURTS Over ER
    elif family in ("pitcher_k", "pitcher_ip"):
        # Pitcher-friendly park (low run_pf) → more Ks, longer outings
        dev = 100 - run_pf   # inverted: lower run env is better for pitchers
        found = True
        if is_over:
            pts += _clamp(dev * 0.10, -3.0, 3.0)
        elif is_under:
            pts += _clamp(-dev * 0.10, -3.0, 3.0)
        details.append(
            f"{park} run environment {run_pf} — "
            f"{'pitcher-friendly' if run_pf < 97 else 'hitter-friendly' if run_pf > 103 else 'neutral'}")
    elif family == "pitcher_er":
        # Higher run environment → more earned runs
        dev = run_pf - 100
        found = True
        if is_over:
            pts += _clamp(dev * 0.08, -2.5, 2.5)
        elif is_under:
            pts += _clamp(-dev * 0.08, -2.5, 2.5)
        details.append(
            f"{park} run environment {run_pf} (earned-run context)")

    # Game total / NRFI — pure run environment
    elif family in ("game_total", "nrfi"):
        dev = run_pf - 100
        found = True
        # NRFI = "no run first inning" = Under-style bet
        if family == "nrfi":
            pts += _clamp(-dev * 0.08, -2.0, 2.0)
        elif is_over:
            pts += _clamp(dev * 0.08, -2.5, 2.5)
        elif is_under:
            pts += _clamp(-dev * 0.08, -2.5, 2.5)
        details.append(
            f"{park} run factor {run_pf} — "
            f"{'high-scoring' if run_pf > 103 else 'low-scoring' if run_pf < 97 else 'neutral'} venue")

    return {
        "key": "mlb_deep", "label": "MLB Context",
        "points": round(_clamp(pts, -MLB_DEEP_MAX, MLB_DEEP_MAX), 1),
        "max": MLB_DEEP_MAX, "details": details, "found": found,
    }



# ─────────────────────────────────────────────────────────────────────
# 8. SOCCER DEEP — xG regression, xG differential, home advantage,
#                  league tier (Phase B.4)
# ─────────────────────────────────────────────────────────────────────
def soccer_deep_signal(pick: dict) -> dict:
    """Soccer-only evidence layer. Reads `pick['soccer_deep']` populated
    by `services.signal_engine.soccer_deep.enrich_soccer_pick`.

    Awards points from FOUR independent sub-signals:
      • xG regression      (goalscorer picks) — G/xG outliers due to revert
      • xG differential    (team picks) — durable underlying quality edge
      • Home advantage     (team picks in high-home-adv leagues)
      • League tier        (data quality confidence bump / penalty)

    Returns a neutral 0-point block for non-soccer picks or when the
    soccer_deep block hasn't been attached — identical fallback to
    every Phase A calculator so scoring stays stable.
    """
    pts = 0.0
    details: list[str] = []
    found = False

    if (pick.get("sport") or "").lower() != "soccer":
        return {
            "key": "soccer_deep", "label": "Soccer Context",
            "points": 0.0, "max": SOCCER_DEEP_MAX,
            "details": [], "found": False,
        }
    deep = pick.get("soccer_deep") or {}
    if not deep:
        return {
            "key": "soccer_deep", "label": "Soccer Context",
            "points": 0.0, "max": SOCCER_DEEP_MAX,
            "details": [], "found": False,
        }

    market_l = (pick.get("market") or "").lower()
    is_scorer = ("goal scorer" in market_l
                 or "score or assist" in market_l
                 or "to score" in market_l)
    is_team_market = any(t in market_l for t in (
        "moneyline", "win or draw", "double chance", "1x2",
        "total goals", "btts", "both teams", "asian handicap",
    ))

    # 1. xG regression (goalscorer markets only)
    xgr = deep.get("xg_regression")
    gox = deep.get("goals_over_xg")
    if is_scorer and xgr is not None and gox is not None:
        found = True
        if xgr == "due_hot":
            pts += 2.5
            details.append(
                f"xG regression buy — scoring only {gox:g}× xG (underperforming, "
                f"positive regression due)")
        elif xgr == "due_cold":
            pts -= 2.0
            details.append(
                f"xG regression risk — scoring {gox:g}× xG (unsustainable pace, "
                f"negative regression risk)")

    # 2. xG differential (team markets only). Factor score is 0-100 where
    #    50 = neutral. Anything ≥ 70 is a strong pro signal for the pick,
    #    ≤ 30 is a strong against signal.
    xg_diff = deep.get("xg_diff")
    xga_diff = deep.get("xga_diff")
    if is_team_market and (xg_diff is not None or xga_diff is not None):
        found = True
        # Combine offensive (xG) and defensive (xGA) differentials.
        combined = 0.0
        n = 0
        if xg_diff is not None:
            combined += xg_diff; n += 1
        if xga_diff is not None:
            combined += xga_diff; n += 1
        avg = combined / n if n else 50.0
        # Scale (avg-50) from ±50 → ±3 points, then clamp to protect budget.
        pts += _clamp((avg - 50.0) / 50.0 * 3.0, -3.0, 3.0)
        if xg_diff is not None and xg_diff >= 70:
            details.append(
                f"xG creation edge {round(xg_diff)}/100 — durable underlying-quality advantage")
        elif xg_diff is not None and xg_diff <= 30:
            details.append(
                f"xG creation deficit {round(xg_diff)}/100 — book chalk on weaker underlying")
        if xga_diff is not None and xga_diff >= 70:
            details.append(
                f"xGA defensive edge {round(xga_diff)}/100 — pick side concedes less")
        elif xga_diff is not None and xga_diff <= 30:
            details.append(
                f"xGA defensive gap {round(xga_diff)}/100 — pick side leaks chances")

    # 3. Home advantage (only when the league is one where home cooking
    #    materially affects outcomes — Turkish Super Lig, Liga MX, MLS,
    #    Brasileirão, etc.)
    home_adv = deep.get("home_adv")
    if (is_team_market and home_adv is not None
            and deep.get("high_home_adv") and home_adv >= 70):
        found = True
        pts += _clamp((home_adv - 60.0) / 40.0 * 1.5, 0.0, 1.5)
        details.append(
            f"Strong home-side profile ({round(home_adv)}/100) in a "
            f"high-home-advantage league")

    # 4. League tier — small confidence bump for well-covered leagues.
    tier = deep.get("league_tier")
    if tier == 1:
        # Only bump if we already have SOMETHING going on — don't stack
        # a free +1 onto every Top-5 pick.
        if found:
            pts += 0.5
            details.append("Top-tier league — Understat/Opta calibration is strong")
    elif tier == 3 and found:
        # Long-tail leagues get a tiny confidence penalty because model
        # variance is higher there.
        pts -= 0.5

    return {
        "key": "soccer_deep", "label": "Soccer Context",
        "points": round(_clamp(pts, -SOCCER_DEEP_MAX, SOCCER_DEEP_MAX), 1),
        "max": SOCCER_DEEP_MAX, "details": details, "found": found,
    }


# ─────────────────────────────────────────────────────────────────────
# 9. TENNIS DEEP — surface fit, serve/return, motivation, variance,
#                  surface-Elo delta, match load (Phase B.5)
# ─────────────────────────────────────────────────────────────────────
def tennis_deep_signal(pick: dict) -> dict:
    """Tennis-only evidence layer. Reads `pick['tennis_deep']` populated
    by `services.signal_engine.tennis_deep.enrich_tennis_pick`.

    Aggregates six sub-signals into a single ±5 component. Returns
    neutral 0-point block for non-tennis picks or missing data — same
    fallback pattern as every Phase A calculator.
    """
    pts = 0.0
    details: list[str] = []
    found = False

    if (pick.get("sport") or "").lower() != "tennis":
        return {
            "key": "tennis_deep", "label": "Tennis Context",
            "points": 0.0, "max": TENNIS_DEEP_MAX,
            "details": [], "found": False,
        }
    deep = pick.get("tennis_deep") or {}
    if not deep:
        return {
            "key": "tennis_deep", "label": "Tennis Context",
            "points": 0.0, "max": TENNIS_DEEP_MAX,
            "details": [], "found": False,
        }

    surface = str(deep.get("surface") or "Hard").title()

    # 1. Surface fit (from tennis_components)
    sf = deep.get("surface_fit")
    if isinstance(sf, (int, float)):
        found = True
        v = float(sf)
        if v >= 80:
            pts += 1.5
            details.append(f"Surface specialist ({round(v)}/100 on {surface})")
        elif v >= 65:
            pts += 0.6
        elif v <= 35:
            pts -= 1.2
            details.append(f"Poor surface fit ({round(v)}/100 on {surface})")

    # 2. Serve/return dominance
    sr = deep.get("serve_return")
    if isinstance(sr, (int, float)):
        found = True
        v = float(sr)
        if v >= 80:
            pts += 1.5
            details.append(f"Elite hold/break profile ({round(v)}/100)")
        elif v <= 40:
            pts -= 1.0
            details.append(f"Weak serve+return ({round(v)}/100)")

    # 3. Motivation
    mo = deep.get("motivation")
    if isinstance(mo, (int, float)):
        v = float(mo)
        if v <= 50:
            found = True
            pts -= 1.0
            details.append(f"Low motivation flag ({round(v)}/100)")
        elif v >= 85:
            found = True
            pts += 0.5

    # 4. Variance risk
    var = deep.get("variance")
    if isinstance(var, (int, float)):
        v = float(var)
        if v >= 60:
            found = True
            pts -= 1.0
            details.append(f"High variance profile ({round(v)}/100) — upset risk")

    # 5. Surface-Elo delta (from tennis_players DB)
    edge = deep.get("elo_edge")
    if isinstance(edge, (int, float)):
        found = True
        e = float(edge)
        # +100 Elo ≈ +64% win prob head-to-head. Scale to ±2 points at ±150.
        pts += _clamp(e / 150.0 * 2.0, -2.0, 2.0)
        if e >= 100:
            details.append(f"Surface Elo edge +{round(e)} on {surface}")
        elif e <= -100:
            details.append(f"Surface Elo deficit {round(e)} on {surface}")

    # 6. Recent match load (fatigue)
    p_load = deep.get("pick_matches_7d")
    o_load = deep.get("opp_matches_7d")
    if isinstance(p_load, int) and p_load >= 5:
        found = True
        pts -= 0.8
        details.append(f"Heavy match load ({p_load} matches in 7 days) — fatigue risk")
    elif isinstance(p_load, int) and isinstance(o_load, int) and (o_load - p_load) >= 3:
        found = True
        pts += 0.6
        details.append(f"Rest edge — opponent has {o_load} recent matches vs pick's {p_load}")

    # 7. 99-lock eligibility from tennis_engine (bonus signal)
    if deep.get("is_99_lock_elig"):
        found = True
        pts += 0.4

    return {
        "key": "tennis_deep", "label": "Tennis Context",
        "points": round(_clamp(pts, -TENNIS_DEEP_MAX, TENNIS_DEEP_MAX), 1),
        "max": TENNIS_DEEP_MAX, "details": details, "found": found,
    }

