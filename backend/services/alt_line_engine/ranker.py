"""Phase 8 — Alt-Line Ranker.

Takes the outcome distribution + real market alt lines and produces a
ranked list of alt-line opportunities, each scored on:
  1. Win probability
  2. Model confidence (from residual_std + top-factor coherence)
  3. Historical bucket performance (win rate + ROI in this bucket)
  4. Expected value / edge (P_model − P_market)
  5. Simulation stability (how much P(over) changes across nearby lines)

Every alt line is tagged with `source` = "market" (real book line) OR
"model_projection" (round-number thresholds we generate when the book
posts none).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .distribution import build_outcome_distribution, _grid_for
from .safeguards import is_safe_for_alt_lines
from .explanations import compose_explanation


@dataclass
class AltLine:
    line:            float
    side:            str            # "Over" | "Under"
    source:          str            # "market" | "model_projection"
    p_model:         float          # P(hit side) per model
    p_implied:       Optional[float]   # from market odds if source=market
    edge:            Optional[float]   # p_model - p_implied
    confidence:      float          # 0-1
    bucket_roi:      Optional[float]
    stability:       float
    composite_score: float
    market_odds:     Optional[dict]     # {bookmaker, american, decimal}
    explanation:     str


@dataclass
class AltLineBundle:
    sport:     str
    player:    str
    stat:      str
    opponent:  Optional[str]
    projected: Optional[float]
    alt_lines: list[AltLine]
    notes:     list[str]

    def to_dict(self) -> dict:
        return {**{k: v for k, v in asdict(self).items()
                    if k != "alt_lines"},
                "alt_lines": [asdict(a) for a in self.alt_lines]}


# ─── Scoring weights (composite = weighted sum, all normalized 0-1)
_W_PROB       = 0.30
_W_CONFIDENCE = 0.20
_W_BUCKET     = 0.15
_W_EDGE       = 0.25
_W_STABILITY  = 0.10


def _american_to_implied_prob(odds: Optional[int]) -> Optional[float]:
    if odds is None:
        return None
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return None
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _stability_score(rows: list[tuple[float, float, dict]],
                      target_line: float) -> float:
    """Return 0-1 stability — how much P(over) shifts across nearby
    thresholds. Very unstable curves (huge jumps between adjacent
    thresholds) score lower."""
    if len(rows) < 2:
        return 0.5
    sorted_rows = sorted(rows, key=lambda r: r[0])
    diffs = [abs(sorted_rows[i+1][1] - sorted_rows[i][1])
              for i in range(len(sorted_rows) - 1)]
    avg_diff = sum(diffs) / len(diffs) if diffs else 0
    # 0.03 diff between neighbors = ideal (score 1)
    # 0.15+ diff = unstable (score 0)
    if avg_diff <= 0.03: return 1.0
    if avg_diff >= 0.15: return 0.0
    return 1.0 - (avg_diff - 0.03) / 0.12


def _confidence_score(residual_std: Optional[float],
                       top_factors_n: int) -> float:
    """Blend residual_std (lower = more confident) with signal
    coverage (more top_factors = more grounded)."""
    if residual_std is None:
        return 0.5
    # residual_std of 0 = perfect; 3+ = noisy
    conf_std = max(0.0, min(1.0, 1.0 - residual_std / 3.0))
    conf_signals = min(1.0, top_factors_n / 5.0)
    return 0.6 * conf_std + 0.4 * conf_signals


async def _fetch_bucket_roi(db, sport: str, stat: str,
                              p_bucket: str) -> Optional[float]:
    """Look up the recent ROI of picks in this probability bucket.

    Uses the existing `learning_snapshots` collection (Phase 5 output).
    Returns None if no bucket record exists.
    """
    try:
        doc = await db.learning_snapshots.find_one(
            {"sport": sport.upper(), "stat": stat.lower(),
              "bucket": p_bucket},
            {"_id": 0, "roi": 1},
            sort=[("date", -1)],
        )
        if doc and doc.get("roi") is not None:
            return float(doc["roi"])
    except Exception:
        pass
    return None


def _bucket_from_prob(p: float) -> str:
    if p >= 0.75: return "very_high"
    if p >= 0.60: return "high"
    if p >= 0.45: return "medium"
    if p >= 0.30: return "low"
    return "very_low"


async def generate_alt_lines(
    db, *,
    sport: str,
    player: str,
    stat: str,
    opponent: Optional[str] = None,
    market_alt_lines: Optional[list[dict]] = None,   # from Odds API alt feed
    top_n: int = 8,
    canonical_player_id: Optional[str] = None,       # Session A additive
    pick: Optional[dict] = None,                     # UNIVERSAL fallback source
) -> AltLineBundle:
    """Produce a ranked bundle of alt lines.

    `market_alt_lines` — optional list of `{line, side, american, bookmaker}`
    dicts from the Odds API alternate-line feed. When provided, real
    book prices are used to compute the edge. When absent, thresholds
    are model-projection only.

    `canonical_player_id` — Session A additive. Lets the safeguard
    hit `player_game_actuals` by canonical id + lowercase sport
    directly. Backwards-compatible: callers may still omit it.

    `pick` — UNIVERSAL COVERAGE (2026-06-30) additive.  When the
    trained-model distribution path returns nothing (family has no
    trained model yet), the pick's ``win_probability`` + ``line`` are
    used to synthesize a Poisson/Normal distribution over the
    threshold grid so EVERY sport / EVERY player-prop market gets
    alt-line chips.  Backwards-compatible: legacy callers may omit
    it and the fallback simply won't trigger.
    """
    # ── Safeguards ────────────────────────────────────────────────
    safe, reason = await is_safe_for_alt_lines(
        db, sport=sport, player_name=player, stat=stat,
        canonical_player_id=canonical_player_id,
        pick=pick,
    )
    if not safe:
        return AltLineBundle(sport=sport, player=player, stat=stat,
                              opponent=opponent, projected=None,
                              alt_lines=[], notes=[f"blocked: {reason}"])

    # ── Distribution ──────────────────────────────────────────────
    dist = await build_outcome_distribution(
        db, sport=sport, player=player, stat=stat, opponent=opponent,
        pick=pick,
    )
    if not dist.get("supported"):
        return AltLineBundle(sport=sport, player=player, stat=stat,
                              opponent=opponent, projected=None,
                              alt_lines=[], notes=dist.get("notes", []) +
                              [dist.get("reason") or "no dist"])
    thresholds = dist["thresholds"]     # [(line, p_over, meta), ...]
    projected  = dist.get("projected")
    residual_std = dist.get("residual_std")

    # ── Market alt-line lookup by line ────────────────────────────
    market_index: dict[tuple[float, str], dict] = {}
    for m in (market_alt_lines or []):
        try:
            line = float(m.get("line") or m.get("point"))
            side = (m.get("side") or m.get("name") or "").capitalize()
            if side in ("Over", "Under"):
                market_index[(line, side)] = m
        except (TypeError, ValueError):
            continue

    # ── Score each threshold on BOTH sides ────────────────────────
    stability = _stability_score(
        [(l, p, _) for l, p, _ in thresholds], target_line=0)
    scored: list[AltLine] = []
    for line, p_over, meta in thresholds:
        for side, p_side in (("Over", p_over), ("Under", 1.0 - p_over)):
            market = market_index.get((line, side))
            source = "market" if market else "model_projection"
            p_implied = _american_to_implied_prob(
                (market or {}).get("american") if market else None)
            edge = (p_side - p_implied) if p_implied is not None else None
            bucket = _bucket_from_prob(p_side)
            bucket_roi = await _fetch_bucket_roi(
                db, sport=sport, stat=stat, p_bucket=bucket)
            conf = _confidence_score(
                residual_std,
                len(meta.get("top_factors") or []),
            )
            # Composite score — all components normalized 0-1.
            comp_prob   = min(1.0, max(0.0, (p_side - 0.30) / 0.70))
            comp_conf   = conf
            comp_bucket = 0.5 if bucket_roi is None else \
                          min(1.0, max(0.0, (bucket_roi + 0.10) / 0.30))
            comp_edge   = 0.5 if edge is None else \
                          min(1.0, max(0.0, (edge + 0.05) / 0.15))
            comp_stab   = stability
            score = (_W_PROB * comp_prob
                      + _W_CONFIDENCE * comp_conf
                      + _W_BUCKET * comp_bucket
                      + _W_EDGE * comp_edge
                      + _W_STABILITY * comp_stab)
            explanation = compose_explanation(
                player=player, stat=stat, line=line, p_over=p_side,
                projected=projected, edge=edge, source=source,
                bucket_roi=bucket_roi, stability=stability,
            )
            scored.append(AltLine(
                line=line, side=side, source=source,
                p_model=round(p_side, 4),
                p_implied=(round(p_implied, 4) if p_implied is not None
                            else None),
                edge=(round(edge, 4) if edge is not None else None),
                confidence=round(conf, 3),
                bucket_roi=(round(bucket_roi, 4)
                             if bucket_roi is not None else None),
                stability=round(stability, 3),
                composite_score=round(score, 4),
                market_odds=({"american": market.get("american"),
                                "bookmaker": market.get("bookmaker")}
                              if market else None),
                explanation=explanation,
            ))
    # ── Two-way pairing ─────────────────────────────────────────
    # Emit BOTH Over AND Under chips per threshold so users can flip
    # the pick straight from the chip row.  We rank threshold-LINES
    # (not individual chips) by the max composite score of the pair,
    # then keep top_n lines with both sides intact.
    from collections import defaultdict
    by_line: dict[float, list[AltLine]] = defaultdict(list)
    for a in scored:
        by_line[a.line].append(a)
    # Sort lines by best composite in the pair.
    ranked_lines = sorted(
        by_line.items(),
        key=lambda kv: max(a.composite_score for a in kv[1]),
        reverse=True,
    )[: max(1, int(top_n) // 2)]
    # Rebuild the flat list preserving threshold ascending order
    # inside the trimmed set so the chip row reads left-to-right by
    # line, with Over/Under paired.
    ranked_lines.sort(key=lambda kv: kv[0])
    output: list[AltLine] = []
    for line, chips in ranked_lines:
        # Prefer Over first, then Under, for consistent chip order.
        chips.sort(key=lambda a: 0 if a.side == "Over" else 1)
        output.extend(chips)
    return AltLineBundle(
        sport=sport, player=player, stat=stat, opponent=opponent,
        projected=projected, alt_lines=output,
        notes=dist.get("notes", []),
    )


__all__ = ["generate_alt_lines", "AltLine", "AltLineBundle"]
