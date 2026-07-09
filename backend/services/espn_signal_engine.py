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
# possible swing capped at ±8.0 pts (up from ±6 to accommodate the new
# season-record signal). No single signal can exceed its own sub-cap.

_INJURY_WEIGHTS = {
    "out":          3.0,   # each side's OUT starter
    "doubtful":     1.5,
    "questionable": 0.6,
}
_OPPONENT_MULT = 0.7   # opponent injuries help our pick a bit less than
                       # our own injuries hurt us (books already price
                       # the pick-side more efficiently)

_FORM_MAX_SWING = 3.0    # ±3 pts from ESPN last-5 form delta
_RECORD_MAX_SWING = 4.0  # ±4 pts from Wikipedia season-record delta

_MAX_TOTAL_SWING = 8.0

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


def _is_recent(iso_date: str, max_age_days: int = 30) -> bool:
    """Only count injuries with a note updated within the last N days.
    ESPN often carries season-ending IL entries from months ago that
    shouldn't move today's line."""
    if not iso_date:
        return True   # unknown → keep (better to include than drop signal)
    try:
        from datetime import datetime, timezone
        # Support both '2026-06-12T21:21Z' and full ISO strings.
        s = iso_date.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        age = (datetime.now(timezone.utc) - d).days
        return age <= max_age_days
    except Exception:
        return True


def _filter_active(injuries: list[dict]) -> list[dict]:
    """Drop stale or long-term IL entries so we only count injuries
    actually affecting *today's* lineup.

    Rules:
      • Recent (within 30 days) OR unknown date.
      • Status is one of: Out, Doubtful, Questionable, Day-to-Day,
        or a short-term IL stint (7/10/15-Day-IL).
      • Excluded: 60-Day-IL, season-ending, suspensions, retired.
    """
    active: list[dict] = []
    for inj in injuries or []:
        status = (inj.get("status") or "").strip().lower()
        # Long-term IL entries — the player was already presumed out; the
        # book already priced this in and it shouldn't move the line.
        if "60-day" in status or "60 day" in status:
            continue
        if not any(k in status for k in
                   ("out", "doubt", "question", "day-to-day", "day to day",
                    "-day-il", " day il", "10-day", "15-day", "7-day", "il")):
            continue
        desc = (inj.get("description") or "").lower()
        if any(bad in desc for bad in
               ("season-ending", "season ending",
                "suspended", "suspension", "retirement", "retired")):
            continue
        if not _is_recent(inj.get("date") or "", max_age_days=30):
            continue
        active.append(inj)
    return active


def _short_injury_line(inj: dict) -> str:
    """Single-line label for an evidence bullet."""
    name = inj.get("athlete") or "Player"
    pos = inj.get("position")
    status = (inj.get("status") or "").strip()
    pos_str = f" ({pos})" if pos else ""
    return f"{name}{pos_str} — {status}"


def _injury_tier(status: str) -> str | None:
    """Map an ESPN status string to one of our three tiers.
    Returns None when the entry shouldn't count."""
    s = (status or "").lower()
    if not s or "probab" in s:
        return None
    # Any IL stint = the player isn't in the lineup today. Treat as OUT.
    if "il" in s or "injured list" in s:
        return "out"
    if "out" in s:
        return "out"
    if "doubt" in s:
        return "doubtful"
    if "question" in s:
        return "questionable"
    if "day" in s:   # "day-to-day"
        return "questionable"
    return None


def _injury_bucket(injuries: list[dict]) -> dict[str, int]:
    """Bucket ACTIVE injuries by tier. Excludes long-term IL entries."""
    active = _filter_active(injuries)
    out = {"out": 0, "doubtful": 0, "questionable": 0}
    for i in active:
        tier = _injury_tier(i.get("status") or "")
        if tier and tier in out:
            out[tier] += 1
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


async def _season_record_signal(db, pick: dict, side: str) -> tuple[float, list[dict]]:
    """Season-long W/D/L record delta pulled from Wikipedia.

    This is the deeper-history signal that ESPN's 5-game form can't
    provide for niche leagues. A 55% win rate over 36 games is a *much*
    stronger indicator than the last 5 which might have been a rough
    stretch.

    We compute the pick-side vs opponent-side win-rate delta, scale it
    by sample size, and cap at ±_RECORD_MAX_SWING (±4pp).
    """
    sport = pick.get("sport")
    if sport != "Soccer":
        return (0.0, [])   # Wikipedia scraper only runs on soccer today

    event = pick.get("event") or ""
    home = away = ""
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)
    if not (home and away):
        return (0.0, [])

    try:
        from services.wikipedia_team_record import get_team_record
    except Exception:
        return (0.0, [])

    home_rec = await get_team_record(db, sport, home.strip())
    away_rec = await get_team_record(db, sport, away.strip())
    if not home_rec or not away_rec:
        return (0.0, [])

    home_wr = home_rec["wins"] / max(1, home_rec["played"])
    away_wr = away_rec["wins"] / max(1, away_rec["played"])

    # Sample-size confidence — a 5-game season is worth less than a
    # full 36. Scale linearly up to 30 games each, saturating there.
    n_conf = min(1.0, min(home_rec["played"], away_rec["played"]) / 30.0)

    pick_wr = home_wr if side == "home" else away_wr
    opp_wr  = away_wr if side == "home" else home_wr
    diff = pick_wr - opp_wr   # positive = pick side has better record
    delta = _clamp(diff * (2 * _RECORD_MAX_SWING) * n_conf,
                   -_RECORD_MAX_SWING, _RECORD_MAX_SWING)

    items = [{
        "kind":     "season_record",
        "pick_wdl": f"{home_rec['wins']}-{home_rec['draws']}-{home_rec['losses']}"
                    if side == "home" else
                    f"{away_rec['wins']}-{away_rec['draws']}-{away_rec['losses']}",
        "opp_wdl":  f"{away_rec['wins']}-{away_rec['draws']}-{away_rec['losses']}"
                    if side == "home" else
                    f"{home_rec['wins']}-{home_rec['draws']}-{home_rec['losses']}",
        "pick_wr":  round(pick_wr * 100, 1),
        "opp_wr":   round(opp_wr * 100, 1),
        "pick_source": (home_rec.get("source_page") if side == "home"
                        else away_rec.get("source_page")),
        "opp_source":  (away_rec.get("source_page") if side == "home"
                        else home_rec.get("source_page")),
        "delta":    round(delta, 2),
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
    adjustments AND injecting bullets into `pick_rationale.evidence`
    so \"Why This Pick?\" renders the reasoning natively.

    - No-op when the pick lacks a clear home/away side, when the sport
      isn't ESPN-covered, or when the pick already has an
      `espn_signals` block written by a previous run.
    - Adjusts `win_probability` and `lock_score` bounded to ±6 pts.
    - Filters ESPN injury data to *active* injuries only (recent date +
      Out/Doubtful/Questionable). Long-term IL entries are ignored so
      we don't over-adjust for players who weren't going to play anyway.
    - When zero meaningful signals fire, `injury_chip` is stripped from
      the pick so the frontend doesn't render a hollow chip.
    """
    if not pick or pick.get("espn_signals"):
        return pick   # idempotent

    sport = pick.get("sport")
    if sport not in _TEAM_SPORTS and sport != "UFC":
        return pick   # nothing to adjust yet

    side = _pick_side(pick)
    if not side:
        return pick

    base = float(pick.get("win_probability") or 0.0)
    if base <= 0:
        return pick

    home_inj, away_inj = await _fetch_team_injuries(db, sport, pick.get("event") or "")

    signals: list[dict] = []
    delta = 0.0

    inj_d, inj_items = _injury_signal(pick, side, home_inj, away_inj)
    delta += inj_d
    signals.extend(inj_items)

    form_d, form_items = _form_signal(pick, side)
    delta += form_d
    signals.extend(form_items)

    rec_d, rec_items = await _season_record_signal(db, pick, side)
    delta += rec_d
    signals.extend(rec_items)

    delta = _clamp(delta, -_MAX_TOTAL_SWING, _MAX_TOTAL_SWING)

    # When no meaningful signal fired, strip hollow chip data and bail.
    if abs(delta) < 0.25:
        if isinstance(pick.get("injury_chip"), dict):
            c = pick["injury_chip"]
            total = (c.get("home", {}).get("out", 0)
                     + c.get("home", {}).get("doubtful", 0)
                     + c.get("home", {}).get("questionable", 0)
                     + c.get("away", {}).get("out", 0)
                     + c.get("away", {}).get("doubtful", 0)
                     + c.get("away", {}).get("questionable", 0))
            if total == 0:
                # Nothing to show — remove entirely.
                pick.pop("injury_chip", None)
        pick["espn_signals"] = {
            "applied":   False,
            "delta":     0.0,
            "base_prob": base,
            "items":     signals,
        }
        return pick

    new_prob = _clamp(base + delta, 1.0, 99.5)
    new_lock = _prob_to_lock(new_prob)

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

    # ── Inject bullets into pick_rationale.evidence ────────────────
    # This is what makes ESPN data actually "flow through" the "Why This
    # Pick?" panel instead of hiding in a side section. Each signal
    # becomes one evidence bullet, styled with an emoji tag so the
    # rationale renderer can group them visually.
    rationale = pick.setdefault("pick_rationale", {})
    if not isinstance(rationale, dict):
        rationale = pick["pick_rationale"] = {}
    evidence = rationale.setdefault("evidence", [])
    if not isinstance(evidence, list):
        evidence = rationale["evidence"] = []

    # Headline bullet — always emitted when we adjust.
    evidence.append(
        f"🧠 ESPN Signal moved the model {'+' if delta > 0 else ''}{round(delta, 1)}pp "
        f"({round(base, 1)}% → {pick['win_probability']}%)."
    )

    # Injury bullets — one per side with counts + key names.
    own_inj = home_inj if side == "home" else away_inj
    opp_inj = away_inj if side == "home" else home_inj
    own_active = _filter_active(own_inj)
    opp_active = _filter_active(opp_inj)
    if own_active:
        top = ", ".join(_short_injury_line(i) for i in own_active[:3])
        more = "" if len(own_active) <= 3 else f" (+{len(own_active) - 3} more)"
        evidence.append(
            f"🚑 Pick side has {len(own_active)} active injur"
            f"{'y' if len(own_active) == 1 else 'ies'}: {top}{more}."
        )
    if opp_active:
        top = ", ".join(_short_injury_line(i) for i in opp_active[:3])
        more = "" if len(opp_active) <= 3 else f" (+{len(opp_active) - 3} more)"
        evidence.append(
            f"💪 Opponent depleted — {len(opp_active)} active injur"
            f"{'y' if len(opp_active) == 1 else 'ies'}: {top}{more}."
        )

    # Form bullets
    for f in form_items:
        pick_form = f.get("pick_form") or "n/a"
        opp_form = f.get("opp_form") or "n/a"
        arrow = "📈" if f.get("delta", 0) > 0 else "📉"
        evidence.append(
            f"{arrow} Recent form — pick side {pick_form} vs opponent {opp_form} "
            f"({'+' if f['delta'] > 0 else ''}{f['delta']}pp)."
        )

    # Season-record bullets (Wikipedia-sourced deep history)
    for r in rec_items:
        arrow = "🏆" if r.get("delta", 0) > 0 else "🧊"
        evidence.append(
            f"{arrow} Full-season record — pick side {r['pick_wdl']} "
            f"({r['pick_wr']}%) vs opponent {r['opp_wdl']} ({r['opp_wr']}%) "
            f"({'+' if r['delta'] > 0 else ''}{r['delta']}pp, "
            f"via {r.get('pick_source', 'Wikipedia')[:40]})."
        )

    # Prune any duplicate evidence lines that already existed
    seen = set()
    deduped = []
    for line in evidence:
        key = str(line).lower()
        if key not in seen:
            seen.add(key)
            deduped.append(line)
    rationale["evidence"] = deduped
    # Keep the summary factor bullet too (used by legacy consumers).
    factors = pick.setdefault("factors", {})
    if isinstance(factors, dict):
        factors["ESPN Signal Adjustment"] = (
            f"{'+' if delta > 0 else ''}{round(delta, 2)}pp on win probability "
            f"({base}% → {pick['win_probability']}%)."
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
