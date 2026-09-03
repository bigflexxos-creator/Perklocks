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
) -> dict:
    """Return an AltLineBundle-shaped dict for a game-market pick."""
    sigma = _sigma_for(sport, parsed.market_type)
    grid = _grid_for_game_market(parsed.line, parsed.market_type, sport)

    # ── Solve the underlying distribution ───────────────────────
    # SPREAD convention: model the *picked team's margin of victory*
    # ``M``.  Pick covers when ``M > -line`` (dog +2.5 wins when
    # M > -2.5, so ANY win + losses < 3 cover).  We solve μ_M from
    # ``P(M > -anchor) = win_prob``, then P(cover at alt line s) =
    # ``P(M > -s)``.
    #
    # TOTAL: model total points ``T``.  Over pick wins when
    # ``T > line``, Under when ``T < line``.  We solve μ_T.
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

    alt_lines: list[dict] = []
    for th in grid:
        if parsed.market_type == "spread":
            # Alt-spread s → team covers when M > -s.
            p_th = _normal_sf(-th, mu, sigma)
            side_lbl = "team_covers"
            sign_prefix = f"+{th}" if th >= 0 else f"{th}"
            label_line = f"{sign_prefix}"
        else:
            # Alt-total: two chips per threshold (Over / Under).  We
            # emit BOTH — the side with the higher composite score
            # rises in the ranker.
            p_over_th = _normal_sf(th, mu, sigma)
            alt_lines.append(_row(
                side="Over", line=th, p_model=p_over_th,
                anchor_line=parsed.line, anchor_side=parsed.side,
                pick=pick,
            ))
            alt_lines.append(_row(
                side="Under", line=th, p_model=1 - p_over_th,
                anchor_line=parsed.line, anchor_side=parsed.side,
                pick=pick,
            ))
            continue
        alt_lines.append(_row(
            side=side_lbl, line=th, p_model=p_th,
            anchor_line=parsed.line, anchor_side="team",
            pick=pick,
        ))

    # Rank + trim.
    alt_lines.sort(key=lambda r: r["composite_score"], reverse=True)
    alt_lines = alt_lines[:top_n]

    return {
        "sport":      sport,
        "player":     None,
        "stat":       parsed.market_type,
        "opponent":   pick.get("away_team") if pick.get("home_team") == pick.get("selection")
                        else pick.get("home_team"),
        "projected":  round(mu, 2),
        "alt_lines":  alt_lines,
        "notes":      [
            f"universal_game_market ({parsed.market_type}, σ={sigma})",
        ],
    }


def _row(*, side: str, line: float, p_model: float,
          anchor_line: float, anchor_side: str, pick: dict) -> dict:
    """Compose one alt-line row in the same shape as the player-prop
    engine so the frontend chip renderer is 100 % source-agnostic."""
    p_model = max(0.001, min(0.999, p_model))
    # Composite score: how confident we are relative to a coin flip.
    # Same weighting the ranker uses for model-projection player
    # props (score = 0.5 + max(p - 0.5, 0.5 - p) · 0.4).
    edge = max(p_model - 0.5, 0.5 - p_model)
    composite = round(0.5 + edge * 0.9, 3)
    # American odds implied from the model (fair line, no vig).
    if p_model >= 0.5:
        american = int(round(-p_model / (1 - p_model) * 100))
    else:
        american = int(round((1 - p_model) / p_model * 100))
    return {
        "side":            side,
        "line":            line,
        "p_model":         round(p_model, 3),
        "p_implied":       None,
        "american":        american,
        "bookmaker":       None,
        "edge_pct":        None,
        "roi_bucket":      None,
        "simulation_std":  None,
        "composite_score": composite,
        "source":          "model_projection",
        "explanation":     _explain(anchor_line, anchor_side, side, line, p_model),
    }


def _explain(anchor_line: float, anchor_side: str, side: str,
              line: float, p_model: float) -> str:
    if side.lower() in ("over", "under"):
        return (f"Model implies P({side} {line}) = {p_model:.0%} "
                f"(anchor {anchor_side} {anchor_line}).")
    sign_prefix = f"+{line}" if line >= 0 else f"{line}"
    return (f"Model implies {sign_prefix} covers with {p_model:.0%} "
            f"probability (anchor {anchor_line}).")


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
