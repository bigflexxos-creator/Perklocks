"""Deep-Dive Mode — internal prediction-quality engine.

Runs AFTER the base pick is built but BEFORE it's persisted. Adds three
internal scores and gates low-quality picks via NO-BET logic. No UI changes:
all signals are stored on the pick document for the model to use; the
existing card/pick-detail UI keeps its current look.

Scores (0-100, higher = better):

  edge_score        — how much model value vs the market price. Direct
                      function of `edge_percent` + sportsbook line-movement
                      agreement.
  confidence_score  — how confident we are this pick will hit. Combines
                      win_probability, factor-agreement (low variance across
                      factors = high confidence), and historical hit-rate of
                      the (sport, market) bucket from the learning engine.
  risk_score        — variance / blow-up risk. Higher win-prob LOWERS risk;
                      long-shot props, chalky moneylines, and low-sample
                      buckets RAISE risk.

NO-BET logic: if confidence_score < `NO_BET_THRESHOLD` the pick is tagged
`no_bet = True`. The feed endpoints (`/picks/today`, `/picks/rollover`,
`/picks/under-of-the-day`) automatically filter these out unless an admin
flag is passed.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.deepdive")

NO_BET_THRESHOLD = 38   # picks below this never reach the feed
MIN_BUCKET_SAMPLE = 5   # ROI signal only used after this many settled picks


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def compute_edge_score(pick: dict) -> float:
    """0-100. Combines model edge with whether the line has moved toward us
    since we made the pick (line moved against = sharps agree = good)."""
    try:
        edge = float(pick.get("edge_percent") or 0.0)
    except (TypeError, ValueError):
        edge = 0.0
    # 0% edge → 50, +10% → 95, -5% → 25, +15%+ → cap at 99
    base = 50 + edge * 4.5
    odds_at = pick.get("odds_at_pick") or pick.get("book_odds")
    closing = pick.get("closing_odds") or pick.get("book_odds")
    if odds_at and closing:
        # Convert both to implied prob; positive CLV = price got worse (line
        # moved into us) which is the sharps-agreed signal.
        try:
            from analytics import american_to_implied_pct
            ip_at = american_to_implied_pct(odds_at)
            ip_close = american_to_implied_pct(closing)
            clv = float(ip_close) - float(ip_at)
            base += clv * 1.2   # +1pp CLV = +1.2 edge points
        except (TypeError, ValueError):
            pass
    return round(_clamp(base), 1)


def compute_confidence_score(pick: dict, bucket_n: int = 0, bucket_hit_rate: float = 0.0) -> float:
    """0-100. Combines win_probability, factor agreement (low variance), and
    historical bucket hit-rate."""
    wp = pick.get("win_probability") or 0.0   # 0..100 already
    # Coerce in case upstream stored it as a string (some scraped sources do).
    try:
        wp = float(wp)
    except (TypeError, ValueError):
        wp = 0.0
    # Win-prob anchors the score: 50% → 50, 70% → 70, 90% → 90.
    base = wp
    # Factor agreement: lower variance = higher confidence.
    # Some sources (e.g. tennis_extra scraper, soccer pipeline) store
    # text descriptions in `factors.values()` instead of numeric scores.
    # Coerce + skip non-numerics to prevent "int + str" crashes that
    # silently wipe out the deep-dive enrichment for those picks.
    factors = pick.get("factors") or {}
    if factors:
        numeric_values: list[float] = []
        for v in factors.values():
            try:
                numeric_values.append(float(v))
            except (TypeError, ValueError):
                continue
        if len(numeric_values) > 1:
            mean = sum(numeric_values) / len(numeric_values)
            var = sum((v - mean) ** 2 for v in numeric_values) / len(numeric_values)
            stdev = var ** 0.5    # 0..50 range typically
            # Low stdev → +5; high stdev → -5
            base += max(-5, min(5, (15 - stdev) * 0.5))
    # Bucket calibration boost (only with enough sample)
    if bucket_n >= MIN_BUCKET_SAMPLE and bucket_hit_rate:
        # If bucket's actual hit rate is HIGHER than the pick's model WP,
        # nudge up; if lower, nudge down. Capped ±5.
        try:
            bias = float(bucket_hit_rate) - wp
            base += max(-5, min(5, bias * 0.2))
        except (TypeError, ValueError):
            pass
    return round(_clamp(base), 1)


def compute_risk_score(pick: dict, bucket_n: int = 0) -> float:
    """0-100. HIGHER = riskier. We INVERT this so 100 = safest in callers."""
    try:
        wp = float(pick.get("win_probability") or 0.0)
    except (TypeError, ValueError):
        wp = 0.0
    # Win-prob is the inverse: 90% → 20 risk, 50% → 60 risk, 30% → 80 risk.
    base = 100 - wp
    # Long shots get +10 risk (high variance).
    if pick.get("is_long_shot"):
        base += 10
    # Player props at +250 or worse (long shots) → +5.
    try:
        book = float(pick.get("book_odds") or 0)
    except (TypeError, ValueError):
        book = 0
    if book >= 250:
        base += 5
    # Heavy chalk moneylines (worse than -400) → +5 (small win for big loss).
    if book < 0 and book <= -400:
        base += 5
    # Low-sample bucket → +5 (insufficient learning data).
    if bucket_n < MIN_BUCKET_SAMPLE:
        base += 5
    return round(_clamp(base), 1)


def top_three_reasons(pick: dict) -> list[str]:
    """Pick the top market-relevant reasons for the UI's summary chip.

    Preferred source (2026-07-07): `pick_rationale.evidence` — already
    ranked + trimmed by `market_evidence_profiles` so a Home-Run pick
    sees HR-relevant bullets, an Outs-Recorded pick sees IP/pitch-count
    bullets, and so on. This replaces the previous keyword-heuristic
    ranker that surfaced K/9 lines on batter picks.

    Fallback: legacy `key_insights` list ranked by the old heuristics
    (numbers/percent/keywords) — kept for picks that haven't been
    enriched yet, so the deep_dive stays best-effort.
    """
    # ── Preferred source: market-filtered evidence ─────────────────
    rationale = pick.get("pick_rationale") or {}
    ev = rationale.get("evidence") or []
    # Coerce dict-shaped bullets (rare, from services/mlb_hitter_intel)
    # to their `text`/`reason` field so the UI gets plain strings.
    def _stringify(b):
        if isinstance(b, str):
            return b
        if isinstance(b, dict):
            for k in ("text", "reason", "explanation_text", "label"):
                v = b.get(k)
                if isinstance(v, str) and v:
                    return v
        return str(b)
    ev_str = [_stringify(b) for b in ev if b]
    # Dedupe by leading 40 chars, cap at 5 per user spec ("top 3-5").
    if ev_str:
        seen: set[str] = set()
        out: list[str] = []
        for ins in ev_str:
            key = ins[:40].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(ins)
            if len(out) == 5:
                break
        if out:
            return out

    # ── Fallback: legacy key_insights + generic heuristics ─────────
    insights = pick.get("key_insights") or []
    if not insights:
        return []
    scored: list[tuple[int, str]] = []
    for ins in insights:
        score = 0
        # Heuristics: prefer lines with numbers + percent signs + specific data
        if "%" in ins:
            score += 3
        if any(ch.isdigit() for ch in ins):
            score += 2
        if any(k in ins.lower() for k in ["form", "rank", "matchup", "concede", "average"]):
            score += 2
        if any(k in ins.lower() for k in ["xg", "shots", "minutes"]):
            score += 1
        score += min(len(ins) // 30, 3)
        scored.append((score, ins))
    scored.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, ins in scored:
        # Dedupe near-identical lines.
        key = ins[:40].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ins)
        if len(out) == 3:
            break
    return out


async def deep_dive(db, pick: dict) -> dict:
    """Attach edge/confidence/risk scores + 3-reason summary + no_bet flag.
    Mutates and returns the pick. Best-effort — never raises."""
    try:
        # Pull historical bucket stats (lazy load — single doc fetch).
        bucket_n = 0
        bucket_hit_rate = 0.0
        try:
            doc = await db.learned_weights.find_one({"_id": "current"}, {"_id": 0, "buckets": 1})
            if doc:
                from analytics import _market_label
                label = _market_label(pick.get("market"))
                for b in doc.get("buckets") or []:
                    if b.get("sport") == pick.get("sport") and b.get("market_label") == label:
                        bucket_n = b.get("n", 0)
                        bucket_hit_rate = b.get("hit_rate", 0.0)
                        break
        except Exception:
            pass

        pick["edge_score"]       = compute_edge_score(pick)
        pick["confidence_score"] = compute_confidence_score(pick, bucket_n, bucket_hit_rate)
        pick["risk_score"]       = compute_risk_score(pick, bucket_n)
        pick["top_reasons"]      = top_three_reasons(pick)
        # NO-BET gate. Two carve-outs:
        #  • Elite-player anchors: Mbappé/Haaland/Messi/Kane/Ronaldo etc. are
        #    locked at Elite tier regardless of edge math. Never NO-BET them.
        #  • Synthetic long-shot props (First Goal Scorer etc.): the raw
        #    win_probability is inherently ~12-18% by market structure, so
        #    falls below threshold. Lock-score already accounts for the
        #    high-variance nature, so keep these visible.
        is_elite_anchor = bool(pick.get("elite_player"))
        is_long_shot_synth = bool(
            pick.get("synthetic_fgs")
            or pick.get("synthetic_ags")
            or pick.get("synthetic_soa")
        )
        # ── V2 LIVE: deep-dive `no_bet` flagging is now advisory only ──
        # User feedback ("V2 is blocking a lot of picks let's just make
        # it live"): the confidence_score < threshold check was silently
        # nuking Strong Lock / Elite Lock picks with high win-prob + edge.
        # We still COMPUTE the deep-dive scores for visibility, but the
        # `no_bet` gate is forced OFF here. The bet-quality floor + V2
        # promotion in learning_system_v2 are the only gates that matter
        # now — and those PROMOTE picks rather than silently blocking.
        if is_elite_anchor or is_long_shot_synth:
            pick["no_bet"] = False
        else:
            pick["no_bet"] = False
            # Keep the threshold check as a soft warning the UI can read.
            if pick.get("confidence_score", 100) < NO_BET_THRESHOLD:
                pick["deep_dive_warning"] = (
                    f"Low confidence ({pick.get('confidence_score')}) — "
                    f"pick kept visible per LIVE mode."
                )
        pick["deep_dive"]        = True
    except Exception as e:
        logger.warning("deep_dive failed for pick %s: %s", pick.get("id"), e)
    return pick
