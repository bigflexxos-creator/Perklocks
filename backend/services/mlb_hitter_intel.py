"""MLB Hitter Prop Intelligence Engine.

Builds an *adjusted* hit-probability for a given batter vs starter matchup
using ONLY free MLB Stats API data. Replaces the naive season-AVG baseline
with a multi-layer model:

    final_hit_prob = base_form
                   × platoon_adjustment       (handedness L/R/S)
                   × pitcher_quality          (ERA, K%, BB%, H/9)
                   × ballpark_factor          (Coors→hitter, Petco→pitcher)
                   × recent_form_adjustment   (last-5 weighted 70%, season 30%)
                   × home_away_adjustment

The engine is intentionally CONTEXT-FIRST: raw season AVG is only the
seed value. Every prediction MUST include a handedness split, pitcher
quality, and recent-form blend per the user's product spec.

Public API
----------
    await build_matchup(db, batter_id, pitcher_id, *,
                        batter_team_id=None, ballpark=None,
                        batting_order=None, is_home=True,
                        season=None) → HitterMatchup
    HitterMatchup.summary  → 1-line plain English
    HitterMatchup.rationale → structured dict for pick_rationale

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.services.mlb_intel")

# ─────────────────────────── Config ───────────────────────────
MLB_BASE = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 12.0

# League-average baselines (2024-25 MLB league rates) — used as priors and
# to detect "elite vs weak" pitchers/hitters.
LEAGUE_BABIP = 0.296
LEAGUE_AVG = 0.244       # batting average
LEAGUE_K_PCT = 0.225     # batter strikeout rate
LEAGUE_BB_PCT = 0.083
LEAGUE_PITCHER_ERA = 4.10
LEAGUE_PITCHER_K9 = 8.7
LEAGUE_PITCHER_BB9 = 3.2
LEAGUE_PITCHER_H9 = 8.2

# Hit-prop baseline: P(≥1 hit) when batter posts league-avg AVG over ~4 PA.
# We use Poisson(λ = AB * AVG) → P(N≥1) = 1 - e^-λ. With ~3.7 AB/game and
# AVG=0.244, λ≈0.90 → P(hit)≈59.4%. We anchor base_form here and let the
# adjustments shift around it.
DEFAULT_AB_PER_GAME = 3.7
LEAGUE_HIT_PROB = 1.0 - math.exp(-DEFAULT_AB_PER_GAME * LEAGUE_AVG)  # ≈0.594


# Ballpark hit-factor table (multipliers on hit probability). Values from
# the Statcast 2023-25 three-year park factors averaged. Sourced from
# Baseball Savant's public park-factors page (free, no key).
BALLPARK_FACTORS: dict[str, float] = {
    # Hitter-friendly
    "coors field":          1.18,  # Rockies — altitude
    "great american ball park": 1.07,  # Reds
    "globe life field":     1.05,  # Rangers
    "yankee stadium":       1.05,  # short porch RF
    "fenway park":          1.05,  # Green Monster doubles
    "kauffman stadium":     1.03,
    "minute maid park":     1.03,  # short LF
    "wrigley field":        1.03,  # wind blowing out — average up
    "citizens bank park":   1.04,
    "rogers centre":        1.04,
    # Neutral (most parks fall 0.97–1.03)
    "guaranteed rate field": 1.02,
    "dodger stadium":       0.97,
    "citi field":           0.97,
    "truist park":          1.00,
    "busch stadium":        0.98,
    "target field":         1.00,
    "progressive field":    0.97,
    "comerica park":        0.96,
    # Pitcher-friendly
    "petco park":           0.93,  # SD — marine layer
    "oracle park":          0.92,  # SF — cold + huge RF
    "tropicana field":      0.94,  # Tampa — dome
    "loanDepot park":       0.93,  # Marlins
    "loandepot park":       0.93,  # alt cap
    "american family field": 0.98,
    "pnc park":             0.96,
    "oriole park at camden yards": 1.02,  # post-2022 LF wall pulled in
    "t-mobile park":        0.94,
}


# ─────────────────────────── Data classes ───────────────────────────
@dataclass
class BatterSplits:
    bat_side: str = ""      # 'L' / 'R' / 'S' (switch)
    avg_vs_l: Optional[float] = None
    ops_vs_l: Optional[float] = None
    avg_vs_r: Optional[float] = None
    ops_vs_r: Optional[float] = None
    pa_vs_l: int = 0
    pa_vs_r: int = 0
    season_avg: Optional[float] = None
    season_ops: Optional[float] = None
    last5_avg: Optional[float] = None
    last5_hits: int = 0
    last5_ab: int = 0


@dataclass
class PitcherProfile:
    throw_hand: str = ""    # 'L' / 'R'
    avg_against_l: Optional[float] = None
    avg_against_r: Optional[float] = None
    ops_against_l: Optional[float] = None
    ops_against_r: Optional[float] = None
    bf_l: int = 0
    bf_r: int = 0
    era: Optional[float] = None
    whip: Optional[float] = None
    k_per_9: Optional[float] = None
    bb_per_9: Optional[float] = None
    h_per_9: Optional[float] = None
    k_pct: Optional[float] = None
    bb_pct: Optional[float] = None
    ip: Optional[float] = None
    bf: Optional[int] = None


@dataclass
class HitterMatchup:
    batter_id: int
    pitcher_id: int
    batter_name: str = ""
    pitcher_name: str = ""
    batter_team: str = ""
    pitcher_team: str = ""
    batter: BatterSplits = field(default_factory=BatterSplits)
    pitcher: PitcherProfile = field(default_factory=PitcherProfile)
    ballpark: Optional[str] = None
    is_home: bool = True
    batting_order: Optional[int] = None
    # Model outputs
    base_form: float = LEAGUE_HIT_PROB
    platoon_mult: float = 1.0
    pitcher_quality_mult: float = 1.0
    park_mult: float = 1.0
    recent_form_mult: float = 1.0
    home_away_mult: float = 1.0
    final_hit_prob: float = LEAGUE_HIT_PROB
    # Explanation
    summary: str = ""
    advantages: list[str] = field(default_factory=list)
    disadvantages: list[str] = field(default_factory=list)

    def to_rationale(self) -> dict[str, Any]:
        """Convert to the universal `pick_rationale` shape so pick_enrichment
        can drop this straight into a pick payload."""
        return {
            "summary": self.summary,
            "data_source": "mlb_stats_api",
            "engine": "mlb_hitter_intel",
            "matchup": {
                "batter": self.batter_name,
                "batter_hand": self.batter.bat_side,
                "pitcher": self.pitcher_name,
                "pitcher_hand": self.pitcher.throw_hand,
                "ballpark": self.ballpark,
                "is_home": self.is_home,
                "batting_order": self.batting_order,
            },
            "splits": {
                "batter_vs_lhp_avg": self.batter.avg_vs_l,
                "batter_vs_rhp_avg": self.batter.avg_vs_r,
                "pitcher_vs_lhb_avg": self.pitcher.avg_against_l,
                "pitcher_vs_rhb_avg": self.pitcher.avg_against_r,
            },
            "pitcher_quality": {
                "era": self.pitcher.era,
                "whip": self.pitcher.whip,
                "k_per_9": self.pitcher.k_per_9,
                "bb_per_9": self.pitcher.bb_per_9,
                "h_per_9": self.pitcher.h_per_9,
            },
            "recent_form": {
                "last5_avg": self.batter.last5_avg,
                "last5_hits": self.batter.last5_hits,
                "last5_ab": self.batter.last5_ab,
            },
            "multipliers": {
                "platoon": round(self.platoon_mult, 3),
                "pitcher_quality": round(self.pitcher_quality_mult, 3),
                "park": round(self.park_mult, 3),
                "recent_form": round(self.recent_form_mult, 3),
                "home_away": round(self.home_away_mult, 3),
            },
            "base_form_pct": round(self.base_form * 100, 1),
            "final_hit_prob_pct": round(self.final_hit_prob * 100, 1),
            "confidence_score": _confidence_from_inputs(self),
            "evidence": self.advantages,
            "concerns": self.disadvantages,
        }


def _confidence_from_inputs(m: HitterMatchup) -> int:
    """0-100 score. Penalises small samples (low PA vs hand, low BF vs hand)
    and reduced data availability."""
    score = 60.0
    # PA sample reliability — both sides.
    pa_total = (m.batter.pa_vs_l or 0) + (m.batter.pa_vs_r or 0)
    if pa_total >= 350:
        score += 15
    elif pa_total >= 150:
        score += 8
    elif pa_total < 60:
        score -= 15
    bf_total = (m.pitcher.bf_l or 0) + (m.pitcher.bf_r or 0)
    if bf_total >= 350:
        score += 10
    elif bf_total < 100:
        score -= 10
    # Have recent form?
    if (m.batter.last5_ab or 0) >= 12:
        score += 8
    # Have park data?
    if m.ballpark and m.ballpark.lower() in BALLPARK_FACTORS:
        score += 5
    # Strong matchup signal (big platoon mult swing) → high confidence.
    if abs(m.platoon_mult - 1.0) >= 0.10:
        score += 6
    return int(max(0, min(100, round(score))))


# ─────────────────────────── MLB Stats API fetchers ───────────────────────────
async def _fetch_json(client: httpx.AsyncClient, path: str, **params) -> dict:
    try:
        r = await client.get(f"{MLB_BASE}/{path}", params=params)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"MLB API fail {path} {params}: {e}")
        return {}


async def _fetch_person(client: httpx.AsyncClient, pid: int) -> dict:
    return (await _fetch_json(client, f"people/{pid}")).get("people", [{}])[0]


async def fetch_batter_splits(client: httpx.AsyncClient, batter_id: int,
                              season: int) -> BatterSplits:
    """Pulls batter L/R splits + season AVG + last-5 game log."""
    bs = BatterSplits()
    person = await _fetch_person(client, batter_id)
    bs.bat_side = ((person.get("batSide") or {}).get("code") or "R").upper()

    # L/R splits
    data = await _fetch_json(
        client, f"people/{batter_id}/stats",
        stats="statSplits", season=season, group="hitting", sitCodes="vl,vr",
    )
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            desc = (sp.get("split") or {}).get("description", "").lower()
            st = sp.get("stat") or {}
            try:
                avg = float(st.get("avg")) if st.get("avg") is not None else None
            except Exception:
                avg = None
            try:
                ops = float(st.get("ops")) if st.get("ops") is not None else None
            except Exception:
                ops = None
            pa = int(st.get("plateAppearances", st.get("atBats") or 0) or 0)
            if "left" in desc:
                bs.avg_vs_l, bs.ops_vs_l, bs.pa_vs_l = avg, ops, pa
            elif "right" in desc:
                bs.avg_vs_r, bs.ops_vs_r, bs.pa_vs_r = avg, ops, pa

    # Season totals
    data = await _fetch_json(
        client, f"people/{batter_id}/stats",
        stats="season", season=season, group="hitting",
    )
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            st = sp.get("stat") or {}
            try:
                bs.season_avg = float(st.get("avg"))
            except Exception:
                pass
            try:
                bs.season_ops = float(st.get("ops"))
            except Exception:
                pass

    # Last 5 game logs.
    data = await _fetch_json(
        client, f"people/{batter_id}/stats",
        stats="gameLog", season=season, group="hitting",
    )
    games: list[dict] = []
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            games.append(sp.get("stat") or {})
    last5 = games[-5:]
    hits = sum(int(g.get("hits") or 0) for g in last5)
    ab = sum(int(g.get("atBats") or 0) for g in last5)
    bs.last5_hits = hits
    bs.last5_ab = ab
    if ab > 0:
        bs.last5_avg = round(hits / ab, 3)
    return bs


async def fetch_pitcher_profile(client: httpx.AsyncClient, pitcher_id: int,
                                season: int) -> PitcherProfile:
    """Pulls pitcher L/R splits + season ERA/K%/BB%/WHIP/H per 9."""
    pp = PitcherProfile()
    person = await _fetch_person(client, pitcher_id)
    pp.throw_hand = ((person.get("pitchHand") or {}).get("code") or "R").upper()

    # Splits
    data = await _fetch_json(
        client, f"people/{pitcher_id}/stats",
        stats="statSplits", season=season, group="pitching", sitCodes="vl,vr",
    )
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            desc = (sp.get("split") or {}).get("description", "").lower()
            st = sp.get("stat") or {}
            try:
                avg = float(st.get("avg")) if st.get("avg") is not None else None
            except Exception:
                avg = None
            try:
                ops = float(st.get("ops")) if st.get("ops") is not None else None
            except Exception:
                ops = None
            bf = int(st.get("battersFaced") or 0)
            if "left" in desc:
                pp.avg_against_l, pp.ops_against_l, pp.bf_l = avg, ops, bf
            elif "right" in desc:
                pp.avg_against_r, pp.ops_against_r, pp.bf_r = avg, ops, bf

    # Season totals
    data = await _fetch_json(
        client, f"people/{pitcher_id}/stats",
        stats="season", season=season, group="pitching",
    )
    for s in data.get("stats", []):
        for sp in s.get("splits", []):
            st = sp.get("stat") or {}
            try:
                pp.era = float(st.get("era"))
            except Exception:
                pass
            try:
                pp.whip = float(st.get("whip"))
            except Exception:
                pass
            try:
                ip = float(st.get("inningsPitched"))
                pp.ip = ip
                k = float(st.get("strikeOuts") or 0)
                bb = float(st.get("baseOnBalls") or 0)
                h = float(st.get("hits") or 0)
                if ip > 0:
                    pp.k_per_9 = round(k * 9 / ip, 2)
                    pp.bb_per_9 = round(bb * 9 / ip, 2)
                    pp.h_per_9 = round(h * 9 / ip, 2)
            except Exception:
                pass
            try:
                bf = int(st.get("battersFaced") or 0)
                pp.bf = bf
                if bf > 0:
                    pp.k_pct = round(float(st.get("strikeOuts") or 0) / bf, 3)
                    pp.bb_pct = round(float(st.get("baseOnBalls") or 0) / bf, 3)
            except Exception:
                pass
    return pp


# ─────────────────────────── Adjustment math ───────────────────────────
def _platoon_advantage(batter: BatterSplits, pitcher: PitcherProfile) -> tuple[float, str]:
    """Returns (multiplier, label).
    Switch hitters get a small +3% bonus (they always have platoon edge).
    Otherwise we compare same-handed (disadvantage) vs opposite-handed (advantage)
    and scale based on the magnitude of the batter's OPS gap between sides."""
    b, p = batter.bat_side, pitcher.throw_hand
    if not b or not p:
        return 1.0, "no handedness data"
    if b == "S":
        # Switch hitter — pick best-side OPS as if they always have the edge.
        return 1.03, "switch hitter — always has L/R advantage"
    same_side = (b == p)
    # OPS gap: how much better is batter on opposite side vs same side?
    if same_side:
        # same-side disadvantage
        batter_side_ops = batter.ops_vs_r if b == "R" else batter.ops_vs_l
        opp_side_ops = batter.ops_vs_l if b == "R" else batter.ops_vs_r
        pitcher_side_avg = pitcher.avg_against_r if b == "R" else pitcher.avg_against_l
    else:
        batter_side_ops = batter.ops_vs_l if b == "R" else batter.ops_vs_r
        opp_side_ops = batter.ops_vs_r if b == "R" else batter.ops_vs_l
        pitcher_side_avg = pitcher.avg_against_l if b == "R" else pitcher.avg_against_r

    # Default mult, then refine.
    if same_side:
        mult, label = 0.92, f"same-side ({b}-vs-{p}) — typical platoon disadvantage"
    else:
        mult, label = 1.10, f"opposite-side ({b}-vs-{p}) — typical platoon advantage"

    # Refine with actual OPS gap if we have both sides.
    if (batter_side_ops is not None) and (opp_side_ops is not None):
        gap = opp_side_ops - batter_side_ops  # positive = better on opposite
        # Use the actual sign + magnitude.
        # gap of +0.150 OPS → ~+12% hit-prob bump, gap of +0.050 → +4%.
        if same_side:
            # batter is on his weaker side → multiplier should be < 1.
            # negative offset of (gap * 0.5).
            mult = max(0.85, 1.0 - max(0, gap) * 0.5)
            label = f"same-side {b}-vs-{p}, gap={gap:+.3f} OPS → {((1-mult)*100):.0f}% downgrade"
        else:
            mult = min(1.15, 1.0 + max(0, gap) * 0.6)
            label = f"opposite-side {b}-vs-{p}, gap={gap:+.3f} OPS → +{((mult-1)*100):.0f}% boost"

    # Refine further with pitcher's allowed AVG on this side.
    if pitcher_side_avg is not None:
        # If pitcher gives up .280 vs RHB and league = .244 → +5% bump.
        diff = pitcher_side_avg - LEAGUE_AVG
        mult *= (1.0 + diff * 1.5)
        label += f" · pitcher allows {pitcher_side_avg:.3f} vs {b}HB"
    return round(mult, 4), label


def _pitcher_quality(pitcher: PitcherProfile) -> tuple[float, list[str], list[str]]:
    """Returns (multiplier, advantages, disadvantages).
    Elite pitchers (low ERA, high K, low H/9) reduce hit prob. Weak pitchers boost it."""
    advs: list[str] = []
    cons: list[str] = []
    mult = 1.0
    if pitcher.era is not None:
        if pitcher.era <= 2.80:
            mult *= 0.85
            cons.append(f"🥶 Elite pitcher (ERA {pitcher.era:.2f}) — reduces hit prob")
        elif pitcher.era <= 3.40:
            mult *= 0.92
            cons.append(f"💪 Strong pitcher (ERA {pitcher.era:.2f})")
        elif pitcher.era >= 5.50:
            mult *= 1.12
            advs.append(f"🔥 Weak pitcher (ERA {pitcher.era:.2f}) — boost hit prob")
        elif pitcher.era >= 4.60:
            mult *= 1.06
            advs.append(f"📈 Below-avg pitcher (ERA {pitcher.era:.2f})")
    if pitcher.k_per_9 is not None:
        if pitcher.k_per_9 >= 11.0:
            mult *= 0.94
            cons.append(f"⚡ High-K pitcher ({pitcher.k_per_9:.1f} K/9) — fewer balls in play")
        elif pitcher.k_per_9 <= 6.5:
            mult *= 1.04
            advs.append(f"🎯 Low-K pitcher ({pitcher.k_per_9:.1f} K/9) — contact friendly")
    if pitcher.h_per_9 is not None:
        if pitcher.h_per_9 >= 9.5:
            mult *= 1.05
            advs.append(f"🏏 Hittable ({pitcher.h_per_9:.1f} H/9)")
        elif pitcher.h_per_9 <= 6.5:
            mult *= 0.92
            cons.append(f"🛡️ Suppresses contact ({pitcher.h_per_9:.1f} H/9)")
    return round(mult, 4), advs, cons


def _recent_form(batter: BatterSplits) -> tuple[float, str]:
    """70% weight on last-5 AVG, 30% on season AVG. Returns (mult, label)."""
    if batter.last5_avg is None or batter.season_avg is None:
        return 1.0, ""
    blended = 0.7 * batter.last5_avg + 0.3 * batter.season_avg
    season = batter.season_avg
    if season <= 0:
        return 1.0, ""
    ratio = blended / season
    mult = max(0.85, min(1.18, ratio))
    delta = (mult - 1.0) * 100
    if abs(delta) < 2:
        return 1.0, ""
    if delta > 0:
        return round(mult, 3), f"🔥 Hot streak: {batter.last5_avg:.3f} L5 vs {season:.3f} season ({delta:+.0f}%)"
    return round(mult, 3), f"🧊 Cold streak: {batter.last5_avg:.3f} L5 vs {season:.3f} season ({delta:+.0f}%)"


def _park_factor(ballpark: Optional[str]) -> tuple[float, str]:
    if not ballpark:
        return 1.0, ""
    key = ballpark.strip().lower()
    pf = BALLPARK_FACTORS.get(key)
    if pf is None:
        return 1.0, ""
    if pf >= 1.05:
        return pf, f"🏟️ Hitter-friendly park ({ballpark}, PF={pf:.2f})"
    if pf <= 0.95:
        return pf, f"🏟️ Pitcher-friendly park ({ballpark}, PF={pf:.2f})"
    return pf, ""


def _home_away(is_home: bool) -> tuple[float, str]:
    """Modest +2% home-field hitter edge on average."""
    if is_home:
        return 1.02, "🏠 Home batter"
    return 0.99, "✈️ Away batter"


def _base_form(batter: BatterSplits) -> float:
    """Anchor hit probability to the batter's *season* AVG over ~3.7 AB/game.
    P(≥1 hit) = 1 - (1-AVG)^AB. Falls back to league avg if unavailable."""
    avg = batter.season_avg if batter.season_avg is not None else LEAGUE_AVG
    return 1.0 - (1.0 - avg) ** DEFAULT_AB_PER_GAME


# ─────────────────────────── Public builder ───────────────────────────
async def build_matchup(
    db,
    batter_id: int,
    pitcher_id: int,
    *,
    batter_name: str = "",
    pitcher_name: str = "",
    batter_team: str = "",
    pitcher_team: str = "",
    ballpark: Optional[str] = None,
    batting_order: Optional[int] = None,
    is_home: bool = True,
    season: Optional[int] = None,
) -> HitterMatchup:
    """End-to-end builder. Caches the resulting matchup in Mongo for 6 h to
    avoid hammering the MLB API during the daily prop sweep."""
    if season is None:
        # Use 2025 if we're past March 2026; MLB API starts populating new
        # season AVG/splits around mid-April.
        now = datetime.now(timezone.utc)
        season = now.year if now.month >= 4 else now.year - 1

    cache_key = f"mlb_hitter_intel:{batter_id}:{pitcher_id}:{season}"
    try:
        cached = await db.mlb_hitter_intel_cache.find_one({"_id": cache_key})
        if cached and (time.time() - (cached.get("ts") or 0) < 6 * 3600):
            return _matchup_from_dict(cached["matchup"])
    except Exception:
        cached = None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        batter, pitcher = await asyncio.gather(
            fetch_batter_splits(client, batter_id, season),
            fetch_pitcher_profile(client, pitcher_id, season),
        )

    m = HitterMatchup(
        batter_id=batter_id, pitcher_id=pitcher_id,
        batter_name=batter_name, pitcher_name=pitcher_name,
        batter_team=batter_team, pitcher_team=pitcher_team,
        batter=batter, pitcher=pitcher,
        ballpark=ballpark, is_home=is_home, batting_order=batting_order,
    )
    m.base_form = _base_form(batter)
    m.platoon_mult, platoon_label = _platoon_advantage(batter, pitcher)
    m.pitcher_quality_mult, pq_adv, pq_con = _pitcher_quality(pitcher)
    m.park_mult, park_label = _park_factor(ballpark)
    m.recent_form_mult, recent_label = _recent_form(batter)
    m.home_away_mult, ha_label = _home_away(is_home)

    final = (
        m.base_form
        * m.platoon_mult
        * m.pitcher_quality_mult
        * m.park_mult
        * m.recent_form_mult
        * m.home_away_mult
    )
    m.final_hit_prob = round(max(0.05, min(0.97, final)), 4)

    # Build adv / dis tags
    if "advantage" in platoon_label or "boost" in platoon_label:
        m.advantages.append(f"⚔️ Platoon edge: {platoon_label}")
    elif "downgrade" in platoon_label or "disadvantage" in platoon_label:
        m.disadvantages.append(f"⚔️ Platoon: {platoon_label}")
    m.advantages.extend(pq_adv)
    m.disadvantages.extend(pq_con)
    if park_label:
        (m.advantages if m.park_mult > 1.0 else m.disadvantages).append(park_label)
    if recent_label:
        (m.advantages if m.recent_form_mult > 1.0 else m.disadvantages).append(recent_label)
    if batting_order is not None:
        if batting_order <= 3:
            m.advantages.append(f"🔝 Bats #{batting_order} — top-of-order PAs")
        elif batting_order >= 8:
            m.disadvantages.append(f"⬇️ Bats #{batting_order} — fewer PAs")

    # One-line summary
    delta_pct = (m.final_hit_prob - m.base_form) * 100
    sign = "+" if delta_pct >= 0 else ""
    m.summary = (
        f"{batter_name or 'Batter'} vs {pitcher_name or 'Pitcher'} "
        f"({batter.bat_side}-vs-{pitcher.throw_hand}): {m.final_hit_prob*100:.0f}% to hit "
        f"({sign}{delta_pct:.0f}% vs baseline)"
    )

    try:
        await db.mlb_hitter_intel_cache.update_one(
            {"_id": cache_key},
            {"$set": {"ts": time.time(), "matchup": _matchup_to_dict(m)}},
            upsert=True,
        )
    except Exception:
        pass

    return m


def lean_and_edge(matchup: HitterMatchup, market_implied_prob: float, line: float = 0.5) -> dict:
    """Convert the model hit-prob into an OVER/UNDER lean + edge vs market.

    line == 0.5 means "over 0.5 hits" (anytime hit). Pass line == 1.5 for
    "over 1.5 hits" — we approximate via Poisson with λ ≈ AB * AVG_adj.
    """
    # For anytime-hit market, our final_hit_prob IS the model prob.
    if line <= 0.5:
        model_prob = matchup.final_hit_prob
    else:
        # P(N >= k+1) where N ~ Poisson(λ)
        avg_adj = matchup.batter.season_avg or LEAGUE_AVG
        avg_adj *= (matchup.final_hit_prob / matchup.base_form) if matchup.base_form > 0 else 1.0
        lam = DEFAULT_AB_PER_GAME * avg_adj
        # ceil(line) for over X.5
        k = int(math.ceil(line))
        cdf = sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k))
        model_prob = max(0.02, min(0.97, 1.0 - cdf))

    lean = "OVER" if model_prob > market_implied_prob else "UNDER"
    edge_pp = (model_prob - market_implied_prob) * 100
    return {
        "model_prob": round(model_prob, 4),
        "market_implied_prob": round(market_implied_prob, 4),
        "lean": lean,
        "edge_pct_points": round(edge_pp, 2),
        "confidence": _confidence_from_inputs(matchup),
        "explanation": matchup.summary,
    }


# ─────────────────────────── Helpers ───────────────────────────
def _matchup_to_dict(m: HitterMatchup) -> dict:
    d = asdict(m)
    return d


def _matchup_from_dict(d: dict) -> HitterMatchup:
    bs = BatterSplits(**(d.pop("batter", {}) or {}))
    pp = PitcherProfile(**(d.pop("pitcher", {}) or {}))
    m = HitterMatchup(batter=bs, pitcher=pp, **{k: v for k, v in d.items()
                                                if k in HitterMatchup.__dataclass_fields__})
    return m
