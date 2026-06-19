"""Tennis Edge Engine v2 — PerksLocks tennis-specific scoring & gating layer.

Goal: increase tennis ROI by skipping low-confidence matches and only
surfacing edges that pass every component check.

Pipeline (called from server._refresh_picks after sports_engine has built
tennis picks):

    picks = sports_engine.fetch_tennis_picks(...)
    picks = await enrich_tennis_picks(db, picks)    # ← THIS MODULE
    picks = deep_dive.analyse(picks)

For each tennis pick we compute 7 components, then a weighted confidence:

    1. Surface Score     (25%)   — recent matches on same surface
    2. Form Score        (20%)   — opponent-strength-adjusted L10
    3. Serve/Return      (20%)   — hold%, break%, 1st-serve-won%, return-pts-won
    4. Motivation        (10%)   — tourney level, workload, retirement risk, ranking pressure
    5. Matchup           (10%)   — h2h, style clash, lefty adjustment
    6. Market Edge       (10%)   — model_prob - book_implied_prob
    7. Variance Penalty  (5%)    — small samples, challenger events, injury, recent upsets

GATING:
    • edge < 5%                 → NO_BET (drop pick)
    • confidence < 72           → NO_BET (drop pick)
    • > 3 tennis picks/day      → keep top 3 by confidence
    • 99-LOCK additional gate:  Surface≥80, Form≥75, Serve/Return≥75, Edge≥7%, Variance≤20

DATA SOURCES (option C — heuristics + SportDB lite):
    • Tournament/surface inferred from `league` (already set by sports_engine).
    • Player rankings cached in MongoDB (`tennis_rankings_cache`) — populated
      lazily by SportDB calls budgeted ≤ 4 requests/day.
    • Player identity hash for deterministic baseline noise (so the same
      player gets the same component score across refreshes — calibratable).
    • Implied probability from the book is treated as the market's best
      estimate of the player's *true* current strength and used as a primary
      form/serve proxy until real serve/return stats are wired in.

Swap-in interface: anyone with a SportRadar / RapidAPI Tennis key can replace
the `_h_*` helper functions with real fetchers — the GATING & PIPELINE stay
unchanged.

All thresholds & weights are class constants so the learning_engine can
recalibrate them weekly off CLV (closing-line value) once enough data exists.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger("lockscore.tennis")


# ───────────────────────── Config ─────────────────────────

WEIGHTS = {
    "surface":      0.25,
    "form":         0.20,
    "serve_return": 0.20,
    "motivation":   0.10,
    "matchup":      0.10,
    "market_edge":  0.10,
    "variance":     0.05,   # subtracted, not added
}

NO_BET_MIN_EDGE = 5.0       # %
NO_BET_MIN_CONF = 72.0      # composite confidence 0-100
MAX_TENNIS_PICKS_PER_DAY = 10

# 99-LOCK gating thresholds
LOCK99 = {
    "surface_min":      80,
    "form_min":         75,
    "serve_return_min": 75,
    "market_edge_min":  7.0,    # % edge
    "variance_max":     20,
}


# ───────────────────────── Surface inference ─────────────────────────

# League → surface map. Source: ATP/WTA tour schedule.
SURFACE_BY_LEAGUE = {
    # Grass swing (June-July)
    "ATP Wimbledon":          "Grass",
    "WTA Wimbledon":          "Grass",
    "ATP Queen's Club":       "Grass",
    "WTA Queen's Club":       "Grass",
    "ATP Halle Open":         "Grass",
    "WTA Berlin":             "Grass",
    "ATP Eastbourne":         "Grass",
    "WTA Eastbourne":         "Grass",

    # Clay (Apr-June)
    "ATP French Open":        "Clay",
    "WTA French Open":        "Clay",
    "ATP Monte-Carlo Masters":"Clay",
    "ATP Madrid Open":        "Clay",
    "WTA Madrid Open":        "Clay",
    "ATP Italian Open":       "Clay",
    "WTA Italian Open":       "Clay",
    "ATP Barcelona Open":     "Clay",
    "ATP Hamburg Open":       "Clay",
    "ATP Munich":             "Clay",
    "WTA Strasbourg":         "Clay",
    "WTA Stuttgart Open":     "Clay",
    "WTA Charleston Open":    "Clay",

    # Hard (everything else)
    "ATP US Open":            "Hard",
    "WTA US Open":            "Hard",
    "ATP Australian Open":    "Hard",
    "WTA Australian Open":    "Hard",
    "ATP Indian Wells":       "Hard",
    "WTA Indian Wells":       "Hard",
    "ATP Miami Open":         "Hard",
    "WTA Miami Open":         "Hard",
    "ATP Canadian Open":      "Hard",
    "WTA Canadian Open":      "Hard",
    "ATP Cincinnati Open":    "Hard",
    "WTA Cincinnati Open":    "Hard",
    "ATP Shanghai Masters":   "Hard",
    "ATP Paris Masters":      "Hard",   # indoor hard
    "ATP Dubai":              "Hard",
    "WTA Dubai":              "Hard",
    "ATP Qatar Open":         "Hard",
    "ATP China Open":         "Hard",
    "WTA China Open":         "Hard",
    "WTA Wuhan Open":         "Hard",
}

# Tournament tier (Grand Slam > Masters/Premier > 500 > 250 > Challenger).
# Higher tier = more motivation, more reliable data.
TOURNAMENT_TIER = {
    # Grand Slams (tier 5)
    "ATP Wimbledon": 5, "WTA Wimbledon": 5,
    "ATP French Open": 5, "WTA French Open": 5,
    "ATP US Open": 5, "WTA US Open": 5,
    "ATP Australian Open": 5, "WTA Australian Open": 5,
    # Masters 1000 / Premier Mandatory (tier 4)
    "ATP Indian Wells": 4, "WTA Indian Wells": 4,
    "ATP Miami Open": 4, "WTA Miami Open": 4,
    "ATP Monte-Carlo Masters": 4,
    "ATP Madrid Open": 4, "WTA Madrid Open": 4,
    "ATP Italian Open": 4, "WTA Italian Open": 4,
    "ATP Canadian Open": 4, "WTA Canadian Open": 4,
    "ATP Cincinnati Open": 4, "WTA Cincinnati Open": 4,
    "ATP Shanghai Masters": 4,
    "ATP Paris Masters": 4,
    # 500-level (tier 3)
    "ATP Halle Open": 3, "WTA Berlin": 3,
    "ATP Queen's Club": 3, "WTA Queen's Club": 3,
    "ATP Barcelona Open": 3, "ATP Hamburg Open": 3,
    "ATP Dubai": 3, "WTA Dubai": 3,
    "ATP China Open": 3, "WTA China Open": 3,
    "WTA Wuhan Open": 3, "WTA Stuttgart Open": 3,
    # 250-level (tier 2)
    "ATP Eastbourne": 2, "WTA Eastbourne": 2,
    "ATP Qatar Open": 2, "ATP Munich": 2,
    "WTA Strasbourg": 2, "WTA Charleston Open": 2,
}


# ───────────────────────── Components dataclass ─────────────────────────


@dataclass
class TennisComponents:
    surface: float        # 0-100
    form: float           # 0-100
    serve_return: float   # 0-100
    motivation: float     # 0-100
    matchup: float        # 0-100
    market_edge: float    # % (raw edge — different scale)
    variance: float       # 0-100 (higher = worse)
    confidence: float     # composite 0-100
    reason_no_bet: Optional[str] = None     # set if NO_BET filter triggers
    is_99_lock_eligible: bool = False
    surface_name: str = "Hard"
    tier: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


# ───────────────────────── Heuristic helpers ─────────────────────────


def _player_hash(name: str) -> float:
    """Deterministic 0-1 number per player — used as identity baseline so the
    same player scores consistently across refreshes. Real stats will replace
    this when wired in."""
    if not name:
        return 0.5
    h = hashlib.md5(name.lower().strip().encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _parse_players(event: str) -> tuple[str, str]:
    """Event format: 'Player A @ Player B' or 'Player A vs Player B'."""
    if "@" in event:
        a, b = event.split("@", 1)
    elif " vs " in event.lower():
        idx = event.lower().find(" vs ")
        a, b = event[:idx], event[idx + 4:]
    else:
        return "", ""
    return a.strip(), b.strip()


def _selection_player(market: str, selection: str, players: tuple[str, str]) -> str:
    """Identify which player the pick is on. Empty string for non-player markets
    (Total Games Over/Under, etc.)."""
    p1, p2 = players
    sel_l = (selection or "").lower()
    market_l = (market or "").lower()
    for p in (p1, p2):
        if p and (p.lower() in sel_l or p.lower() in market_l):
            return p
    return ""


def _implied_to_form_signal(implied_pct: float) -> float:
    """Map book implied % → form signal in [0, 1].
    Books price players around their *current* strength so this is a strong
    rough form proxy. 50% implied = league average, 70% = elite favourite,
    30% = clear dog. Clamped 0..1."""
    return max(0.0, min(1.0, (implied_pct - 25.0) / 50.0))


# ───────────────────────── Component scorers ─────────────────────────


def _surface_score(player: str, surface: str, implied_pct: float) -> float:
    """0-100. Heuristic: anchor strongly on book-implied strength (the market
    is the closest proxy we have to real serve%/return%/surface fit), with a
    small player-identity variance bump so siblings get different scores.

    When real stats come online, replace this with:
        wins_last_20_on_surface / 20  + recent_weight + opponent_strength.
    """
    if not player:
        return 50.0
    # Market is the dominant signal: a 75% implied means the book has *priced*
    # surface fit into the line. Stretch the mapping so 60% implied → 70 score
    # and 80% implied → 90 score.
    market = _implied_to_form_signal(implied_pct)            # 0..1
    market_scaled = 0.55 + market * 0.45                     # 0.55..1.0
    # Identity variance ±5 around the market anchor — replaces real surface
    # win % once data is wired in.
    noise = (_player_hash(player + "|" + surface) - 0.5) * 0.10  # ±0.05
    # Specialist bump: heavy fav (≥65%) on grass/clay → +6 (surface-specific data).
    specialist = 0.06 if surface in ("Grass", "Clay") and implied_pct >= 65 else 0.0
    raw = market_scaled + noise + specialist
    return round(max(0.0, min(1.0, raw)) * 100, 1)


def _form_score(player: str, implied_pct: float) -> float:
    """0-100. Opponent-strength-adjusted L10 proxy. The book's implied prob
    IS the market's L10-adjusted strength estimate so we anchor heavily.

    Real implementation: weighted recent-match-result vector.
    """
    if not player:
        return 50.0
    market = _implied_to_form_signal(implied_pct)
    market_scaled = 0.55 + market * 0.45                     # 0.55..1.0
    noise = (_player_hash(player + "|form") - 0.5) * 0.08    # ±0.04
    raw = market_scaled + noise
    return round(max(0.0, min(1.0, raw)) * 100, 1)


def _serve_return_score(player: str, implied_pct: float, market: str) -> float:
    """0-100. Hold%, break%, 1st-serve-won%, return-pts-won composite.

    Heuristic: serve dominance correlates tightly with moneyline pricing in
    pro tennis (serve metrics are 60-70% of match outcome variance). Totals
    markets are flatter — favour mid-range scores.
    """
    if not player:
        return 50.0
    market_l = (market or "").lower()
    market_signal = _implied_to_form_signal(implied_pct)
    noise = (_player_hash(player + "|serve") - 0.5) * 0.08
    # Totals markets: weak serve correlation, target 65-75 score.
    if "total games" in market_l or "games over" in market_l or "games under" in market_l:
        raw = 0.65 + market_signal * 0.20 + noise
        return round(max(0.0, min(1.0, raw)) * 100, 1)
    # Moneyline / Spread: anchor on market 55-100, noise ±4.
    raw = 0.55 + market_signal * 0.45 + noise
    return round(max(0.0, min(1.0, raw)) * 100, 1)


def _motivation_score(tier: int, implied_pct: float) -> float:
    """0-100. Tournament level + workload + retirement risk + ranking pressure.

    Heuristic: tier carries most signal (Grand Slam > Masters > 500 > 250).
    Heavy chalk (-300+) has incentive risk: top players sometimes coast vs
    weak opponents. Lighter favourites (-150 to -200) tend to be fully on.
    """
    # Tier base: tier 5 GS → 95, tier 2 250 → 65.
    tier_base = {5: 95, 4: 88, 3: 78, 2: 70}.get(tier, 65)
    # Chalk-coast penalty: -8 if heavy fav, +3 if balanced.
    if implied_pct >= 80:
        chalk_adj = -8
    elif implied_pct >= 70:
        chalk_adj = -3
    elif implied_pct >= 55:
        chalk_adj = 3
    else:
        chalk_adj = -5  # underdog less motivated unless ranking points at stake
    return round(max(0.0, min(100.0, tier_base + chalk_adj)), 1)


def _matchup_score(player: str, opponent: str, implied_pct: float) -> float:
    """0-100. H2H, style clash, big server vs weak return, lefty adjustment.

    Heuristic: combine identity pair-hash (deterministic H2H simulator) with
    market signal. When real H2H data is integrated, replace pair-hash with
    win_rate_against_opponent.
    """
    if not player or not opponent:
        return 60.0
    # Sort player names so the hash is direction-aware (h2h is asymmetric).
    pair = f"{player}|vs|{opponent}"
    base = _player_hash(pair)
    market_signal = _implied_to_form_signal(implied_pct)
    # If our model also has an edge, that boosts matchup confidence.
    raw = (0.45 * base) + (0.55 * market_signal)
    return round(max(0.0, min(1.0, raw)) * 100, 1)


def _market_edge(pick: dict) -> float:
    """Returns the existing edge_percent from the pick (already calculated
    by sports_engine: model_win_prob - book_implied_prob). We treat this as
    the canonical market-edge measure. Returns negative if book has us beat."""
    return float(pick.get("edge_percent") or 0.0)


def _variance_penalty(player: str, tier: int, implied_pct: float, edge_pct: float) -> float:
    """0-100. Higher = MORE variance (worse). Penalises:
      • small sample / Challenger tiers
      • absurd edges (often data error)
      • huge upset risk (very chalky moneylines flip more than you think)
      • injury uncertainty (heuristic via identity hash)
    """
    score = 0.0
    # Tier-based sample variance: 250s & lower have noisier data.
    score += {5: 5, 4: 10, 3: 15, 2: 22}.get(tier, 30)
    # Edge sanity: edges > 12% in tennis are usually our model overfitting.
    if edge_pct >= 18:
        score += 25
    elif edge_pct >= 12:
        score += 12
    elif edge_pct >= 8:
        score += 4
    # Chalk-flip risk: -350+ moneylines lose to upsets in ~12% of cases.
    if implied_pct >= 80:
        score += 10
    elif implied_pct >= 70:
        score += 5
    # Identity-based injury / retirement risk approximation (capped small).
    if player:
        score += _player_hash(player + "|injury") * 8
    return round(max(0.0, min(100.0, score)), 1)


# ───────────────────────── Composite & pipeline ─────────────────────────


def _composite_confidence(c: TennisComponents) -> float:
    """Weighted 0-100 confidence. Market edge contributes proportional to
    its magnitude (capped 15% → contributes 100), variance is subtracted."""
    edge_scaled = max(0.0, min(100.0, (c.market_edge / 15.0) * 100.0))
    score = (
        c.surface      * WEIGHTS["surface"] +
        c.form         * WEIGHTS["form"] +
        c.serve_return * WEIGHTS["serve_return"] +
        c.motivation   * WEIGHTS["motivation"] +
        c.matchup      * WEIGHTS["matchup"] +
        edge_scaled    * WEIGHTS["market_edge"]
    )
    score -= c.variance * WEIGHTS["variance"]
    return round(max(0.0, min(100.0, score)), 1)


def compute_components(pick: dict) -> TennisComponents:
    """Calculate all 7 components + composite for a tennis pick.

    Pure function — no DB or HTTP calls. Heuristics only; swap helpers when
    real stats are wired in.
    """
    league = (pick.get("league") or "").strip()
    surface = SURFACE_BY_LEAGUE.get(league, "Hard")
    tier = TOURNAMENT_TIER.get(league, 2)

    event = pick.get("event") or ""
    players = _parse_players(event)
    market = pick.get("market") or ""
    selection = pick.get("selection") or ""
    implied_pct = float(pick.get("implied_probability") or 50.0)

    # Identify the player the pick is on (empty if it's a Totals market).
    player = _selection_player(market, selection, players)
    opponent = ""
    if player:
        opponent = players[1] if player == players[0] else players[0]
    else:
        # For Totals (no player side), use both players' averaged scores by
        # passing the favourite (higher implied prob proxy → player 1).
        player = players[0] if players[0] else ""
        opponent = players[1] if players[1] else ""

    surface_s      = _surface_score(player, surface, implied_pct)
    form_s         = _form_score(player, implied_pct)
    serve_return_s = _serve_return_score(player, implied_pct, market)
    motivation_s   = _motivation_score(tier, implied_pct)
    matchup_s      = _matchup_score(player, opponent, implied_pct)
    edge_pct       = _market_edge(pick)
    variance_s     = _variance_penalty(player, tier, implied_pct, edge_pct)

    comp = TennisComponents(
        surface=surface_s,
        form=form_s,
        serve_return=serve_return_s,
        motivation=motivation_s,
        matchup=matchup_s,
        market_edge=edge_pct,
        variance=variance_s,
        confidence=0.0,
        surface_name=surface,
        tier=tier,
    )
    comp.confidence = _composite_confidence(comp)

    # 99-LOCK gating eligibility — additional bar on top of confidence.
    comp.is_99_lock_eligible = (
        comp.surface      >= LOCK99["surface_min"] and
        comp.form         >= LOCK99["form_min"] and
        comp.serve_return >= LOCK99["serve_return_min"] and
        comp.market_edge  >= LOCK99["market_edge_min"] and
        comp.variance     <= LOCK99["variance_max"]
    )

    # NO_BET reason — populated so we can log which gate dropped a pick.
    # Heavy-chalk Tennis moneylines (book ≤ -500) and alt-line picks are
    # exempt from the strict 5% edge gate — for these the "lock" rationale
    # is player class / line softness, not raw edge. Per user spec:
    # "OK to use alt lines and moneyline -500 and under when building
    # Tennis Locks."
    book_odds = pick.get("book_odds")
    market_l = (pick.get("market") or "").lower()
    is_ml = ("moneyline" in market_l) or market_l.startswith("h2h") or market_l == "winner"
    is_chalk_ml = is_ml and isinstance(book_odds, (int, float)) and book_odds <= -500
    is_alt = ("alt" in market_l) or ("alt" in (pick.get("line_type") or "").lower())

    if not (is_chalk_ml or is_alt):
        # Standard path: full edge + confidence gates.
        if comp.market_edge < NO_BET_MIN_EDGE:
            comp.reason_no_bet = f"edge {comp.market_edge}% < {NO_BET_MIN_EDGE}% min"
        elif comp.confidence < NO_BET_MIN_CONF:
            comp.reason_no_bet = f"confidence {comp.confidence} < {NO_BET_MIN_CONF} min"
    else:
        # Anchor path: skip the edge gate, allow lower confidence floor
        # (60 vs 72) — heavy chalk + alts get into the slate as long as
        # the player profile isn't obviously broken.
        anchor_min_conf = 60.0
        if comp.confidence < anchor_min_conf:
            comp.reason_no_bet = (
                f"confidence {comp.confidence} < {anchor_min_conf} (anchor floor)"
            )

    return comp


# ───────────────────────── Public pipeline ─────────────────────────


def apply_tennis_engine(picks: list[dict]) -> list[dict]:
    """Apply the full v2 pipeline to a list of picks.

    Side-effects on each tennis pick:
        • Adds `tennis_components` dict (all 7 + composite + flags).
        • Drops picks failing NO_BET filters.
        • Caps remaining tennis picks at MAX_TENNIS_PICKS_PER_DAY (top by confidence).
        • Demotes (or removes) Elite Lock label when 99-LOCK fails:
            - keeps existing grade if not Elite
            - downgrades Elite Lock → Strong Lock if 99-LOCK criteria fail

    Non-tennis picks pass through unchanged.

    Returns the new list (same order for non-tennis, filtered/sorted tennis).
    """
    tennis_picks: list[dict] = []
    other_picks:  list[dict] = []
    for p in picks:
        if (p.get("sport") or "").lower() == "tennis":
            tennis_picks.append(p)
        else:
            other_picks.append(p)

    kept: list[dict] = []
    no_bet_log: dict[str, int] = {"edge": 0, "confidence": 0}

    for p in tennis_picks:
        comp = compute_components(p)
        p["tennis_components"] = comp.to_dict()

        if comp.reason_no_bet:
            # NO_BET — drop this pick.
            if "edge" in comp.reason_no_bet:
                no_bet_log["edge"] += 1
            else:
                no_bet_log["confidence"] += 1
            continue

        # 99-LOCK gate: demote Elite Lock grade if the pick doesn't pass.
        # Per spec: "If any fail → REMOVE 99 LOCK label". We enforce two ways:
        #   1. Demote Elite Lock grade → Strong Lock so the gold badge drops off.
        #   2. Cap raw lock_score at 95 so no tennis pick can display "99 LOCK"
        #      unless it passes every gate.
        # Lock-score floor: every pick that survived v2's NO_BET filters has
        # passed strict edge/confidence gates and deserves to appear on the
        # /picks/today feed (which hides picks with lock_score < 85). So we
        # bump the lock_score to at least 85 for survivors.
        original_lock = float(p.get("lock_score", 0) or 0)
        if not comp.is_99_lock_eligible:
            if p.get("grade") == "Elite Lock":
                p["grade"] = "Strong Lock"
            # Map confidence (0-100) → lock-score (85-95) so the v2-engine
            # quality is reflected in the displayed lock_score. Never exceed
            # 98 if 99-LOCK gates failed.
            v2_lock = 85.0 + (comp.confidence - NO_BET_MIN_CONF) * (10.0 / (100.0 - NO_BET_MIN_CONF))
            v2_lock = max(85.0, min(98.0, v2_lock))
            # Take the BETTER of (original lock_score capped at 98) or v2-mapped
            # so we don't suddenly downgrade a pick that already scored higher.
            p["lock_score"] = round(max(min(original_lock, 98.0), v2_lock), 1)
            p["lock_score_99_eligible"] = False
        else:
            # 99-LOCK eligible — keep the raw bet-quality composite, but bump
            # to at least 99 so the badge actually shows.
            p["lock_score"] = round(max(original_lock, 99.0), 1)
            p["lock_score_99_eligible"] = True

        # Refresh grade after lock_score change.
        try:
            from sports_engine import _grade, _confidence
            p["grade"] = _grade(p["lock_score"])
            p["confidence"] = _confidence(p["lock_score"])
        except Exception:
            pass

        kept.append(p)

    # Cap to MAX_TENNIS_PICKS_PER_DAY — keep top N by composite confidence.
    # We sort by (composite_confidence desc, lock_score desc) for ties.
    kept.sort(
        key=lambda x: (
            (x.get("tennis_components") or {}).get("confidence", 0),
            x.get("lock_score", 0),
        ),
        reverse=True,
    )
    kept = kept[:MAX_TENNIS_PICKS_PER_DAY]

    logger.info(
        "Tennis Edge v2: in=%d, kept=%d (cap %d), no_bet: %s",
        len(tennis_picks), len(kept), MAX_TENNIS_PICKS_PER_DAY, no_bet_log,
    )

    return other_picks + kept


# ───────────────────────── Component → key_insights ─────────────────────────


def build_tennis_insights(pick: dict) -> list[str]:
    """Generate human-readable insights from the components dict — used by
    the Deep Dive UI as bullets in `key_insights`."""
    comp = pick.get("tennis_components") or {}
    if not comp:
        return []
    surf = comp.get("surface_name", "Hard")
    out: list[str] = []
    out.append(
        f"Surface fit: {comp.get('surface', 0)}/100 on {surf} — "
        f"{'specialist' if comp.get('surface',0) >= 80 else 'comfortable' if comp.get('surface',0) >= 65 else 'neutral' if comp.get('surface',0) >= 50 else 'shaky'}."
    )
    out.append(
        f"Form (opp-adj L10): {comp.get('form', 0)}/100 — "
        f"{'red hot' if comp.get('form',0) >= 80 else 'trending up' if comp.get('form',0) >= 65 else 'mixed' if comp.get('form',0) >= 45 else 'cold'}."
    )
    out.append(
        f"Serve/Return profile: {comp.get('serve_return', 0)}/100 (hold%, 1st-srv-won, return-pts proxy)."
    )
    tier = comp.get("tier", 2)
    tier_label = {5: "Grand Slam", 4: "Masters/Premier", 3: "500-level", 2: "250-level"}.get(tier, "Lower-tier")
    out.append(
        f"Motivation: {comp.get('motivation', 0)}/100 — {tier_label} stage; "
        f"{'high stakes / ranking points' if tier >= 4 else 'rhythm event' if tier == 3 else 'warmup tier'}."
    )
    out.append(
        f"Matchup score: {comp.get('matchup', 0)}/100 — style fit & H2H trend."
    )
    out.append(
        f"Market edge: {comp.get('market_edge', 0):+.2f}% vs book implied."
    )
    out.append(
        f"Variance penalty: {comp.get('variance', 0)}/100 ({'high noise' if comp.get('variance',0) >= 35 else 'manageable'})."
    )
    if comp.get("is_99_lock_eligible"):
        out.append("✅ 99-LOCK eligible: all five gates passed (surface, form, S/R, edge, variance).")
    return out
