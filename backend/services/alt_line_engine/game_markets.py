"""GAME-MARKET ALT-LINE MAGIC (2026-06-30).

Extends the universal alt-line engine to **game-level** markets
(Spread + Total) across every sport.  Zero fabrication: probabilities
are derived from the pick's own ``win_probability`` + anchor line via
a back-solved Normal distribution on the underlying game random
variable (margin of victory for Spread, total points for Total).

Handles the following market families (auto-detected from the pick's
``market`` string):

    Spread:   "<Team> +2.5 Spread"
              "<Team> -3.0 Spread"
              MLB Run Line, NHL Puck Line
    Total:    "Total Points Over 216.5"
              "Total Goals Under 2.5"
              "Total Rounds Over 1.5"

Moneyline is intentionally excluded — it is a single-outcome market
with no alt-line grid.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────
# Market string classification
# ─────────────────────────────────────────────────────────────────
_SPREAD_RE = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*(?:Spread|Run\s*Line|Puck\s*Line|Handicap)",
    re.I,
)
_TOTAL_RE = re.compile(
    r"Total\s+(?:Points?|Goals?|Runs?|Rounds?|Games?|Sets?)?\s*"
    r"(Over|Under)\s+(-?\d+(?:\.\d+)?)", re.I,
)
_ML_RE = re.compile(r"\bMoneyline\b|\bML\b", re.I)


@dataclass
class GameMarketParse:
    market_type: str          # "spread" | "total"
    line:        float        # signed for spread (dog +, fav -)
    side:        str          # "team"|"opp" for spread, "Over"|"Under" for total
    label:       str          # human-friendly label (e.g. "Miami Marlins +1.5")
    win_prob:    float        # normalised 0-1


def parse_game_market_pick(pick: dict) -> Optional[GameMarketParse]:
    """Return a GameMarketParse if the pick is a game-market pick
    (Spread or Total), else ``None``."""
    if not isinstance(pick, dict):
        return None
    market = str(pick.get("market") or "")
    if not market or _ML_RE.search(market) and not _SPREAD_RE.search(market):
        # Moneyline / player prop — not game market.
        return None
    wp = pick.get("win_probability")
    if not isinstance(wp, (int, float)):
        return None
    wp_frac = wp / 100.0 if wp > 1.0 else wp
    if not (0.0 < wp_frac < 1.0):
        return None

    # ── Total ───────────────────────────────────────────────────
    tm = _TOTAL_RE.search(market)
    if tm:
        side = tm.group(1).capitalize()   # "Over" | "Under"
        line = float(tm.group(2))
        return GameMarketParse(
            market_type="total", line=line, side=side,
            label=f"Total {side} {line}", win_prob=wp_frac,
        )

    # ── Spread / Run Line / Puck Line ────────────────────────────
    sm = _SPREAD_RE.search(market)
    if sm:
        # Selection is the team we're picking — the sign of the line
        # indicates whether we are receiving points (+) or laying (-).
        selection = str(pick.get("selection") or "").strip()
        raw_line = float(sm.group(1))
        # Some picks store the line as always positive with the side
        # encoded in the selection.  Normalise: ``line`` is the number
        # of points the picked team is spotted (dog:+, favorite:-).
        stored_line = pick.get("line")
        if isinstance(stored_line, (int, float)):
            # Prefer the explicit stored line when it disambiguates
            # sign (e.g. Fritz -3.0 stored as -3.0).
            line = float(stored_line)
        else:
            line = raw_line
        team_label = selection or market.split(str(raw_line))[0].strip()
        sign_prefix = f"+{line}" if line >= 0 else f"{line}"
        return GameMarketParse(
            market_type="spread", line=line, side="team",
            label=f"{team_label} {sign_prefix}", win_prob=wp_frac,
        )

    return None


# ─────────────────────────────────────────────────────────────────
# Universal Normal distribution on game random variable
# ─────────────────────────────────────────────────────────────────
def _normal_sf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if mu > x else 0.0
    z = (x - mu) / sigma
    return 0.5 * math.erfc(z / math.sqrt(2.0))


# Sport-tuned CoV / standard-deviation defaults for the Normal
# distribution's σ.  Values are empirical (published sportsbook
# implied distributions):
#     NFL margin  σ ≈ 13.5     (Vegas standard)
#     NFL total   σ ≈ 10       (~20 % of a 50-point total)
#     NBA margin  σ ≈ 12
#     NBA total   σ ≈ 13.5     (~6 % of a 220-point total)
#     MLB margin  σ ≈ 3.5
#     MLB total   σ ≈ 2.9
#     NHL margin  σ ≈ 2.0
#     NHL total   σ ≈ 1.85
#     TENNIS games σ ≈ 4
#     SOCCER total σ ≈ 1.35
_SIGMA_TABLE = {
    ("NFL",    "spread"): 13.5,
    ("NFL",    "total"):  10.0,
    ("CFB",    "spread"): 14.0,
    ("CFB",    "total"):  11.0,
    ("NBA",    "spread"): 12.0,
    ("NBA",    "total"):  13.5,
    ("NCAAB",  "spread"): 11.0,
    ("NCAAB",  "total"):  12.0,
    ("MLB",    "spread"): 3.5,
    ("MLB",    "total"):  2.9,
    ("NHL",    "spread"): 2.0,
    ("NHL",    "total"):  1.85,
    ("TENNIS", "spread"): 3.5,
    ("TENNIS", "total"):  4.0,
    ("SOCCER", "spread"): 1.35,
    ("SOCCER", "total"):  1.35,
    ("UFC",    "spread"): 0.7,   # rounds
    ("UFC",    "total"):  0.9,
}
_DEFAULT_SIGMA = 8.0


def _sigma_for(sport: str, market_type: str) -> float:
    return _SIGMA_TABLE.get(
        ((sport or "").upper(), market_type), _DEFAULT_SIGMA,
    )


def _grid_for_game_market(anchor_line: float, market_type: str,
                           sport: str) -> list[float]:
    """Symmetric alt-line grid centered on ``anchor_line``.

    Grid spacing depends on sport granularity:
      • NBA / NFL / CFB spread:  1-point buckets
      • MLB / NHL spread:        0.5-point buckets (they are usually
                                   locked at 1.5, but books offer
                                   alt run-lines at 2.5 / 3.5).
      • Totals: 3-point buckets around the anchor for NBA/NFL,
        0.5 for MLB/NHL, 0.5 for tennis games.
    """
    sport_u = (sport or "").upper()
    if market_type == "spread":
        if sport_u in ("MLB", "NHL"):
            steps = [-3.5, -2.5, -1.5, -0.5, 0, 0.5, 1.5, 2.5, 3.5]
        elif sport_u in ("NBA", "NCAAB"):
            steps = [-6, -3, -1, 0, 1, 3, 6, 9]
        elif sport_u in ("NFL", "CFB"):
            steps = [-6, -3, -1, 0, 1, 3, 6, 9]
        else:
            steps = [-3, -2, -1, 0, 1, 2, 3]
    else:  # total
        if sport_u in ("MLB", "NHL"):
            steps = [-2.5, -1.5, -0.5, 0, 0.5, 1.5, 2.5]
        elif sport_u == "SOCCER":
            steps = [-1.5, -0.5, 0, 0.5, 1.5, 2.5]
        elif sport_u == "UFC":
            steps = [-0.5, 0, 0.5, 1.0, 1.5]
        elif sport_u in ("NBA", "NCAAB"):
            steps = [-9, -6, -3, 0, 3, 6, 9]
        elif sport_u == "TENNIS":
            steps = [-3.5, -2.5, -1.5, 0, 1.5, 2.5, 3.5]
        else:  # NFL / CFB / default
            steps = [-7, -4, -1, 0, 1, 4, 7]
    grid = [anchor_line + s for s in steps]
    # Ensure the anchor is in the grid and grid is monotone unique.
    grid = sorted({round(v, 2) for v in grid})
    return grid


def _solve_normal_mean_for_anchor(
    anchor: float, p_over: float, sigma: float,
) -> Optional[float]:
    """Given a Normal with known σ, solve μ such that
    ``P(X > anchor) = p_over``."""
    if not (0.0 < p_over < 1.0):
        return None
    lo, hi = anchor - 200.0, anchor + 200.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _normal_sf(anchor, mid, sigma) < p_over:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ─────────────────────────────────────────────────────────────────
# Bundle builder
# ─────────────────────────────────────────────────────────────────
def build_game_market_alt_lines(
    *,
    sport: str,
    pick: dict,
    parsed: GameMarketParse,
    top_n: int = 8,
    market_alt_lines: Optional[list[dict]] = None,
) -> dict:
    """Return an AltLineBundle-shaped dict for a game-market pick.

    ``market_alt_lines`` — optional list of book-price rows from the
    Odds API alternate feed (``alternate_spreads`` /
    ``alternate_totals``), used to hydrate real sportsbook prices and
    computed edge percentages on chips where a matching (line, side)
    is quoted.  When absent, chips stay ``source: model_projection``.
    """
    sigma = _sigma_for(sport, parsed.market_type)
    grid = _grid_for_game_market(parsed.line, parsed.market_type, sport)

    # ── Solve the underlying distribution ───────────────────────
    if parsed.market_type == "spread":
        anchor_reference = -parsed.line
        p_over = parsed.win_prob
    else:  # total
        anchor_reference = parsed.line
        p_over = parsed.win_prob if parsed.side.lower() == "over" else 1 - parsed.win_prob

    mu = _solve_normal_mean_for_anchor(anchor_reference, p_over, sigma)
    if mu is None:
        return _empty_bundle(sport, pick, parsed,
                              "degenerate win_probability")

    # ── Index real book prices by (line, side) ───────────────────
    book_index: dict[tuple[float, str], dict] = {}
    for m in (market_alt_lines or []):
        try:
            line = float(m.get("line") or m.get("point"))
            side = (m.get("side") or m.get("name") or "").strip()
            if not side:
                continue
            # For spreads the side is a team name; normalize to a
            # canonical "team" / "opp" tag against the pick's own
            # selection so lookups from either perspective hit.
            if parsed.market_type == "spread":
                if side.lower() == (pick.get("selection") or "").lower():
                    key = (line, "team")
                else:
                    key = (-line, "opp")
            else:
                key = (line, side.capitalize())
            book_index[key] = m
        except (TypeError, ValueError):
            continue

    alt_lines: list[dict] = []
    for th in grid:
        if parsed.market_type == "spread":
            # TWO-WAY: emit BOTH sides at the SAME grid threshold so
            # the ranker's group-by-line pairs them together.
            # ``team_covers @ +th`` is the picked team getting +th
            # points; ``opp_covers @ +th`` is the opposing team laying
            # -th (semantically the same underlying spread, just from
            # the opposing side — this is the "flip" the user taps).
            p_team = _normal_sf(-th, mu, sigma)
            alt_lines.append(_row(
                side="team_covers", line=th, p_model=p_team,
                anchor_line=parsed.line, anchor_side="team",
                pick=pick, market=book_index.get((th, "team")),
            ))
            alt_lines.append(_row(
                side="opp_covers", line=th, p_model=1 - p_team,
                anchor_line=parsed.line, anchor_side="team",
                pick=pick, market=book_index.get((-th, "opp")),
            ))
        else:  # total — emit Over + Under at each threshold
            p_over_th = _normal_sf(th, mu, sigma)
            alt_lines.append(_row(
                side="Over", line=th, p_model=p_over_th,
                anchor_line=parsed.line, anchor_side=parsed.side,
                pick=pick, market=book_index.get((th, "Over")),
            ))
            alt_lines.append(_row(
                side="Under", line=th, p_model=1 - p_over_th,
                anchor_line=parsed.line, anchor_side=parsed.side,
                pick=pick, market=book_index.get((th, "Under")),
            ))

    # ── Rank by threshold-LINE (keep pairs together) ────────────
    from collections import defaultdict
    by_line: dict[float, list[dict]] = defaultdict(list)
    for chip in alt_lines:
        by_line[chip["line"]].append(chip)
    ranked_lines = sorted(
        by_line.items(),
        key=lambda kv: max(c["composite_score"] for c in kv[1]),
        reverse=True,
    )[: max(1, int(top_n) // 2)]
    # Preserve ascending line order within the trimmed set.
    ranked_lines.sort(key=lambda kv: kv[0])
    output: list[dict] = []
    for _, chips in ranked_lines:
        # Prefer picked-side first, opposing second for spread; Over
        # first, Under second for total.
        chips.sort(key=lambda c: (
            0 if c["side"] in ("team_covers", "Over") else 1))
        output.extend(chips)

    book_count = sum(1 for c in output if c["source"] == "market")
    notes = [f"universal_game_market ({parsed.market_type}, σ={sigma})"]
    if book_count:
        notes.append(f"{book_count}/{len(output)} chips hydrated with real book prices")

    return {
        "sport":      sport,
        "player":     None,
        "stat":       parsed.market_type,
        "opponent":   pick.get("away_team") if pick.get("home_team") == pick.get("selection")
                        else pick.get("home_team"),
        "projected":  round(mu, 2),
        "alt_lines":  output,
        "notes":      notes,
    }


def _row(*, side: str, line: float, p_model: float,
          anchor_line: float, anchor_side: str, pick: dict,
          market: Optional[dict] = None) -> dict:
    """Compose one alt-line row.  When ``market`` (a book row from
    the Odds API alternate feed) is provided, real book prices are
    attached and edge is computed against the model.
    """
    p_model = max(0.001, min(0.999, p_model))
    # American odds from the MODEL (fair, no vig).
    if p_model >= 0.5:
        model_american = int(round(-p_model / (1 - p_model) * 100))
    else:
        model_american = int(round((1 - p_model) / p_model * 100))
    # If a real book price exists, use it for the chip; compute edge.
    if market is not None and market.get("american") is not None:
        american = int(market["american"])
        p_implied = _american_to_implied_prob(american)
        edge_pct = (p_model - p_implied) if p_implied is not None else None
        bookmaker = market.get("bookmaker") or market.get("bookmaker_key")
        source = "market"
    else:
        american = model_american
        p_implied = None
        edge_pct = None
        bookmaker = None
        source = "model_projection"
    # Composite score: gives real-book chips a small boost since
    # they are actually tradeable; adds an edge boost when edge > 0.
    base_edge = max(p_model - 0.5, 0.5 - p_model)
    edge_boost = 0.0
    if edge_pct is not None and edge_pct > 0:
        edge_boost = min(0.15, edge_pct * 1.5)
    market_bonus = 0.02 if source == "market" else 0.0
    composite = round(0.5 + base_edge * 0.9 + edge_boost + market_bonus, 3)
    composite = min(0.999, composite)
    return {
        "side":            side,
        "line":            line,
        "p_model":         round(p_model, 3),
        "p_implied":       round(p_implied, 3) if p_implied is not None else None,
        "american":        american,
        "bookmaker":       bookmaker,
        "edge_pct":        round(edge_pct * 100, 2) if edge_pct is not None else None,
        "roi_bucket":      None,
        "simulation_std":  None,
        "composite_score": composite,
        "source":          source,
        "explanation":     _explain(anchor_line, anchor_side, side, line, p_model,
                                      edge_pct=edge_pct, bookmaker=bookmaker),
    }


def _american_to_implied_prob(american: Optional[int]) -> Optional[float]:
    if american is None:
        return None
    try:
        a = int(american)
    except (TypeError, ValueError):
        return None
    if a > 0:
        return 100.0 / (a + 100.0)
    else:
        return (-a) / ((-a) + 100.0)


def _explain(anchor_line: float, anchor_side: str, side: str,
              line: float, p_model: float,
              edge_pct: Optional[float] = None,
              bookmaker: Optional[str] = None) -> str:
    if edge_pct is not None and edge_pct > 0.02:
        edge_str = f" · +{edge_pct * 100:.1f}% edge"
    elif edge_pct is not None and edge_pct < -0.02:
        edge_str = f" · {edge_pct * 100:.1f}% edge"
    else:
        edge_str = ""
    src_str = f" ({bookmaker})" if bookmaker else ""
    if side.lower() in ("over", "under"):
        return (f"Model implies P({side} {line}) = {p_model:.0%}"
                f"{edge_str}{src_str}.")
    if side == "opp_covers":
        sign_prefix = f"+{line}" if line >= 0 else f"{line}"
        return (f"Opposing team covers at {sign_prefix} with "
                f"{p_model:.0%} probability{edge_str}{src_str}.")
    sign_prefix = f"+{line}" if line >= 0 else f"{line}"
    return (f"Picked team covers at {sign_prefix} with {p_model:.0%} "
            f"probability{edge_str}{src_str}.")


def _empty_bundle(sport: str, pick: dict,
                   parsed: GameMarketParse, reason: str) -> dict:
    return {
        "sport":      sport,
        "player":     None,
        "stat":       parsed.market_type,
        "opponent":   None,
        "projected":  None,
        "alt_lines":  [],
        "notes":      [f"blocked: {reason}"],
    }


__all__ = [
    "parse_game_market_pick",
    "build_game_market_alt_lines",
    "GameMarketParse",
]
