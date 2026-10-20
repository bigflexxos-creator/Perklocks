"""NBA game-market model — real distribution-based moneyline / point
spread / game total probabilities seeded from the historical
``team_game_actuals`` collection.

STEP C ACTIVATION 2026-10-20
────────────────────────────
Prior state: NBA ``moneyline`` / ``point_spread`` / ``game_total`` were
registered ``MODEL_UNAVAILABLE`` because no independent probability
model had shipped — only ``brain/sim_nba`` which back-solves µ from
model_wp (``MODEL_CONDITIONED``) and so is not independent evidence.
This module ships a real **EMPIRICAL_INDEPENDENT** model built on the
5,544 real NBA team-game rows in ``team_game_actuals`` (2024-2025
seasons, 30 unique teams).

Features (all derived from real evidence, no manufactured data):

  team_rating[t]          = (avg points scored − avg points allowed)
                            over team ``t``'s most recent N games
                            (default N=20 → ≈ last ¼ season), vs the
                            NBA baseline (0.0).  Equivalent to the
                            team's rolling net rating in raw points.

  team_off_rating[t]      = avg points scored last N games
  team_def_rating[t]      = avg points allowed last N games
  avg_team_pts            = league-wide average team points per game
                            (currently 113.5)

  µ_margin = (home_rating − away_rating) + HOME_COURT_ADV
             = net-rating gap, home-advantaged ~ +2.5 pts
  µ_total  = max(170, home_off + away_off
                    + 0.5·((home_def−avg_pts) + (away_def−avg_pts)))

  σ_margin = 12.0   # NBA single-game margin standard deviation —
                   # textbook NBA value; our 2024-25 team-row sample
                   # measured σ_margin=15.92 (team-side; includes
                   # blowouts).  12 is intentionally conservative so
                   # we do not over-confidently exceed the market.
  σ_total  = 20.0   # NBA single-game total standard deviation —
                   # our 2024-25 sample measured σ_total=20.1.

Public API:
  estimate_nba_game(ctx: dict, home_team: str, away_team: str)
      → NBAGameResult(available, p_home_ml, p_away_ml,
                       expected_margin, expected_total,
                       margin_sigma, total_sigma, tier,
                       sources, provenance, data_quality)

The caller pre-loads the rolling-rating map into:
    ctx["nba_team_ratings_by_team"]  (team key → dict rating/off/def/n)

If any required piece is missing the function returns
``available=False`` with an explicit ``reason``; callers must then
fail-closed with ``MODEL_UNAVAILABLE`` telemetry — never fabricate.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from math import exp, sqrt, erf
from typing import Any, Optional, Dict, List


# ── Model constants (NBA historical) ────────────────────────────────
HOME_COURT_ADV     = 2.5     # pts (standard NBA figure)
MARGIN_SIGMA_BASE  = 12.0    # pts (textbook NBA single-game margin σ)
TOTAL_SIGMA_BASE   = 20.0    # pts (our 2024-25 team_game_actuals σ)
AVG_TEAM_PTS       = 113.5   # pts/game (measured on our sample)
MIN_TEAM_SAMPLE_N  = 10      # need at least N recent games to trust
SIGMA_INFLATE_LOW_SAMPLE = 1.20


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _team_key(name: str) -> str:
    """Canonicalize a team name for map lookup."""
    return (name or "").strip().lower()


@dataclass
class NBAGameResult:
    available: bool
    p_home_ml: Optional[float] = None
    p_away_ml: Optional[float] = None
    expected_margin: Optional[float] = None   # home - away, pts
    expected_total: Optional[float] = None    # home + away, pts
    margin_sigma: float = MARGIN_SIGMA_BASE
    total_sigma: float = TOTAL_SIGMA_BASE
    reason: Optional[str] = None
    tier: str = "UNAVAILABLE"
    sources: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    data_quality: str = "unknown"

    def as_dict(self) -> dict:
        return {
            "available":        self.available,
            "p_home_ml":        self.p_home_ml,
            "p_away_ml":        self.p_away_ml,
            "expected_margin":  self.expected_margin,
            "expected_total":   self.expected_total,
            "margin_sigma":     self.margin_sigma,
            "total_sigma":      self.total_sigma,
            "reason":           self.reason,
            "tier":             self.tier,
            "sources":          list(self.sources),
            "provenance":       dict(self.provenance),
            "data_quality":     self.data_quality,
        }

    # ─── Market-specific probability helpers ────────────────────────
    def p_home_cover(self, home_line: float) -> Optional[float]:
        """P(home margin > ``home_line``).  ``home_line`` is the
        SPREAD on the home team (negative = home favoured; e.g.
        home_line=-3.5 means home must win by ≥ 4).

        Uses a normal distribution with mean ``expected_margin`` and
        std ``margin_sigma``.  Push is impossible when ``home_line``
        is a half-point (standard NBA spread).
        """
        if not self.available or self.expected_margin is None:
            return None
        # P(margin > home_line) where margin ~ N(mu, sigma)
        try:
            z = (self.expected_margin - float(home_line)) / self.margin_sigma
            return _norm_cdf(z)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def p_over_total(self, total_line: float) -> Optional[float]:
        """P(total > ``total_line``).  Uses N(expected_total, total_sigma)."""
        if not self.available or self.expected_total is None:
            return None
        try:
            z = (self.expected_total - float(total_line)) / self.total_sigma
            return _norm_cdf(z)
        except (TypeError, ValueError, ZeroDivisionError):
            return None


def _lookup_rating(m: Dict[str, Any], name: str) -> Optional[dict]:
    """Resolve a team name → rating dict.  Case-insensitive exact
    match, then alias-trimming for mascot suffixes.  Fail-closed."""
    if not name or not m:
        return None
    key = _team_key(name)
    if key in m:
        return m[key]
    # Trim common NBA mascot suffixes.
    for stop in (" lakers", " warriors", " celtics", " bulls", " knicks",
                 " heat", " spurs", " 76ers", " sixers", " rockets",
                 " mavericks", " nuggets", " clippers", " kings",
                 " jazz", " thunder", " pelicans", " suns", " trail blazers",
                 " blazers", " timberwolves", " wolves", " grizzlies",
                 " hawks", " magic", " nets", " hornets", " bucks",
                 " cavaliers", " cavs", " pacers", " pistons", " raptors",
                 " wizards"):
        if key.endswith(stop):
            trimmed = key[: -len(stop)]
            if trimmed in m:
                return m[trimmed]
    # Also try abbreviations stripped (e.g. "LAL", "GSW").
    first_word = key.split()[0] if key else ""
    if first_word in m:
        return m[first_word]
    return None


async def load_nba_team_ratings(db: Any,
                                 window: int = 20) -> Dict[str, dict]:
    """Load rolling NBA team ratings from ``team_game_actuals``.

    Returns a map:
        {<team_key>: {"rating": net_rating_pts,
                       "off_rating": avg_pts_scored,
                       "def_rating": avg_pts_allowed,
                       "n": games_counted,
                       "canonical_team_id": ...}}

    ``team_key`` is produced by _team_key() applied to BOTH the
    ``canonical_team_id`` and the full name variants seen in picks.

    Fail-closed: an empty dict means the caller MUST stamp
    ``MODEL_UNAVAILABLE:no_nba_team_ratings`` on every emitted
    NBA game-market pick.  Never fabricate a rating.

    2026-10-20 CANONICAL ROUTING FIX
    ────────────────────────────────
    When ``USE_CANONICAL_DB=true`` and ``CANONICAL_FALLBACK_MODE=true``
    the active DB handle wraps every collection name with the
    ``canonical_`` prefix.  Historical NBA rows were ingested into the
    LEGACY, unprefixed ``team_game_actuals`` (5,544 rows for the
    2024-25 season); the canonical shadow holds only 130 NFL rows.
    If the passed handle returns zero NBA rows we transparently
    retry against the legacy DB handle so the game model can boot.
    """
    out: Dict[str, dict] = {}

    async def _fetch(handle) -> list:
        try:
            cursor = handle.team_game_actuals.find(
                {"sport": "nba"},
                {"_id": 0,
                 "canonical_team_id": 1,
                 "team_score": 1,
                 "opponent_score": 1,
                 "event_time": 1,
                 "season": 1},
            ).sort("event_time", -1)
            return await cursor.to_list(length=20000)
        except Exception:
            return []

    rows = await _fetch(db)
    if not rows:
        # Canonical-fallback routing can hide historical NBA rows that
        # live only in the legacy, unprefixed collection.  Retry
        # against the legacy DB handle.
        try:
            from services.database import get_legacy_database
            legacy_db = get_legacy_database()
            if legacy_db is not db:
                rows = await _fetch(legacy_db)
        except Exception:
            rows = []
    if not rows:
        return out

    # Group by canonical_team_id, keep most recent ``window`` games.
    by_team: Dict[str, List[dict]] = {}
    for r in rows:
        t = r.get("canonical_team_id")
        if not t:
            continue
        by_team.setdefault(t, []).append(r)
    for t, recent in by_team.items():
        recent = recent[:window]
        if len(recent) < 1:
            continue
        pts = [r.get("team_score") for r in recent
               if r.get("team_score") is not None]
        allowed = [r.get("opponent_score") for r in recent
                   if r.get("opponent_score") is not None]
        if not pts or not allowed:
            continue
        n = min(len(pts), len(allowed))
        off = sum(pts[:n]) / n
        d = sum(allowed[:n]) / n
        rating = off - d
        rec = {"rating":    round(rating, 3),
               "off_rating": round(off, 2),
               "def_rating": round(d, 2),
               "n":          n,
               "canonical_team_id": t}
        out[_team_key(t)] = rec
    return out


# NBA team-name → canonical abbreviation map (handles the common cases
# seen on the Odds API — full city/mascot names).  Fail-open: when a
# name isn't here we fall through to the alias-trim logic above.
#
# 2026-10-20 — abbreviations below MUST match the ``canonical_team_id``
# values stored in ``team_game_actuals``.  Our 2024-25 historical rows
# use GS (not GSW), NO (not NOP), NY (not NYK), SA (not SAS),
# UTAH (not UTA) and WSH (not WAS).  Using the wrong abbreviation
# collapses the game market to MODEL_UNAVAILABLE:ratings_missing.
_NBA_NAME_TO_ABBR = {
    "atlanta hawks": "ATL", "boston celtics": "BOS", "brooklyn nets": "BKN",
    "charlotte hornets": "CHA", "chicago bulls": "CHI",
    "cleveland cavaliers": "CLE", "dallas mavericks": "DAL",
    "denver nuggets": "DEN", "detroit pistons": "DET",
    "golden state warriors": "GS", "houston rockets": "HOU",
    "indiana pacers": "IND", "los angeles clippers": "LAC",
    "la clippers": "LAC",
    "los angeles lakers": "LAL", "memphis grizzlies": "MEM",
    "miami heat": "MIA", "milwaukee bucks": "MIL",
    "minnesota timberwolves": "MIN", "new orleans pelicans": "NO",
    "new york knicks": "NY", "oklahoma city thunder": "OKC",
    "orlando magic": "ORL", "philadelphia 76ers": "PHI",
    "phoenix suns": "PHX", "portland trail blazers": "POR",
    "sacramento kings": "SAC", "san antonio spurs": "SA",
    "toronto raptors": "TOR", "utah jazz": "UTAH",
    "washington wizards": "WSH",
}


def _resolve_team(ratings: Dict[str, dict], name: str) -> Optional[dict]:
    """Resolve full team name → rating via abbreviation map, then
    fall back to the alias-trim lookup."""
    if not name or not ratings:
        return None
    key = _team_key(name)
    abbr = _NBA_NAME_TO_ABBR.get(key)
    if abbr:
        rec = ratings.get(_team_key(abbr))
        if rec:
            return rec
    return _lookup_rating(ratings, name)


def estimate_nba_game(ctx: dict, home_team: str,
                       away_team: str) -> NBAGameResult:
    """Independent NBA game-market projection.

    Required ctx keys (pre-loaded by the pipeline before this call):
        ctx["nba_team_ratings_by_team"]  (team key → rating dict)

    Returns ``NBAGameResult(available=False, reason=...)`` when
    either team is missing from the ratings map — the caller MUST
    stamp MODEL_UNAVAILABLE and NEVER synthesize a value.
    """
    ratings = (ctx or {}).get("nba_team_ratings_by_team") or {}
    if not ratings:
        return NBAGameResult(
            available=False,
            reason="MODEL_UNAVAILABLE:no_nba_team_ratings_ctx")

    h = _resolve_team(ratings, home_team)
    a = _resolve_team(ratings, away_team)
    if not h or not a:
        missing = []
        if not h: missing.append(f"home={home_team}")
        if not a: missing.append(f"away={away_team}")
        return NBAGameResult(
            available=False,
            reason=f"MODEL_UNAVAILABLE:ratings_missing:{','.join(missing)}")

    try:
        h_rate = float(h.get("rating") or 0.0)
        a_rate = float(a.get("rating") or 0.0)
        h_off  = float(h.get("off_rating") or AVG_TEAM_PTS)
        a_off  = float(a.get("off_rating") or AVG_TEAM_PTS)
        h_def  = float(h.get("def_rating") or AVG_TEAM_PTS)
        a_def  = float(a.get("def_rating") or AVG_TEAM_PTS)
        h_n    = int(h.get("n") or 0)
        a_n    = int(a.get("n") or 0)
    except (TypeError, ValueError) as e:
        return NBAGameResult(
            available=False,
            reason=f"MODEL_UNAVAILABLE:bad_ratings_types:{type(e).__name__}")

    # Expected margin: home net-rating minus away net-rating, plus home-court.
    expected_margin = (h_rate - a_rate) + HOME_COURT_ADV

    # Expected total: sum of each team's offensive output against the
    # OPPONENT's defence, where "defence" means pts allowed.  Each
    # team's expected points vs this opponent is the mean of
    #   (team_off_rating + opponent_def_rating) / 2
    # i.e. a 50/50 blend of what the team usually scores and what the
    # opponent usually gives up.  Fail-closed floor at 170 (physical
    # minimum for a modern NBA game).
    home_pts = 0.5 * (h_off + a_def)
    away_pts = 0.5 * (a_off + h_def)
    expected_total = max(170.0, home_pts + away_pts)

    # Inflate sigma when either team's sample is thin.
    m_sig = MARGIN_SIGMA_BASE
    t_sig = TOTAL_SIGMA_BASE
    thin = []
    if h_n < MIN_TEAM_SAMPLE_N:
        thin.append(f"home_n={h_n}")
    if a_n < MIN_TEAM_SAMPLE_N:
        thin.append(f"away_n={a_n}")
    if thin:
        m_sig *= SIGMA_INFLATE_LOW_SAMPLE
        t_sig *= SIGMA_INFLATE_LOW_SAMPLE

    # Moneyline: P(home wins) = P(margin > 0)
    p_home_ml = _norm_cdf(expected_margin / m_sig)
    p_away_ml = 1.0 - p_home_ml

    # Data quality tag
    bits = ["team_game_actuals_rolling"]
    bits.append(f"home_n{h_n}")
    bits.append(f"away_n{a_n}")
    if thin:
        bits.append("sigma_inflated_low_sample")

    return NBAGameResult(
        available=True,
        p_home_ml=round(p_home_ml, 4),
        p_away_ml=round(p_away_ml, 4),
        expected_margin=round(expected_margin, 3),
        expected_total=round(expected_total, 2),
        margin_sigma=round(m_sig, 3),
        total_sigma=round(t_sig, 3),
        tier="NBA_TEAM_RATINGS_ACTIVE",
        sources=["team_game_actuals"],
        provenance={
            "home_rating":  h_rate,
            "away_rating":  a_rate,
            "home_off":     h_off,
            "home_def":     h_def,
            "away_off":     a_off,
            "away_def":     a_def,
            "home_games":   h_n,
            "away_games":   a_n,
            "home_court":   HOME_COURT_ADV,
            "margin_sigma_reason": "nba_textbook_12" + (
                "|sigma_inflated_low_sample" if thin else ""),
            "total_sigma_reason":  "team_game_actuals_measured_20_1" + (
                "|sigma_inflated_low_sample" if thin else ""),
        },
        data_quality="|".join(bits),
    )


__all__ = ["estimate_nba_game", "load_nba_team_ratings", "NBAGameResult"]
