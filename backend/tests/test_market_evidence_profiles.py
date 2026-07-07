"""
Tests for the Market-Specific Evidence Engine.

Verifies that `market_evidence_profiles.select_top_evidence` surfaces
DIFFERENT top-ranked bullets when the same synthetic evidence pool is
passed in for DIFFERENT market families. This is the core promise of
the "Why This Pick" upgrade: an HR pick shouldn't show pitcher K/9,
an Outs pick shouldn't show strikeout %, etc.

Run:
  python -m pytest tests/test_market_evidence_profiles.py -v
"""
from market_evidence_profiles import (
    MarketFamily,
    PROFILES,
    classify_market,
    select_top_evidence,
    explain_selection,
)


# A rich, cross-market bullet pool. Each string mentions a distinctive
# stat so the profile can match it. Real pipeline emits bullets that
# look like these (with emojis + numbers).
_MLB_POOL = [
    "🏟 Park factor 1.18 — HR-friendly venue",
    "⚡ Barrel% 14.2 over last 30 games — top-tier power",
    "🌡 Weather: 88°F with 12mph wind out to LF",
    "🥎 Pitcher HR/9 = 1.9 (bottom quartile)",
    "💪 ISO .258 — elite isolated power",
    "🥊 Batter vs RHP AVG .312",
    "📊 Pitcher rolling K/9 = 11.3 — dominant strikeout stuff",
    "🎯 Opp K% 27.4 — high-K lineup",
    "⏱ Averages 6.2 innings per start (ip/gs)",
    "🔥 Pitch count avg 96 per outing",
    "🏆 Quality start rate 68%",
    "🥴 Manager pull tendency: quick hook when past 90 pitches",
    "🚶 BB/9 = 3.2",
    "🎪 Swinging strike rate (swstr) 14.8",
    "🎣 Chase rate 32% — batters expand the zone",
    "🎳 Pitch arsenal: 4-pitch mix with plus slider",
    "🧢 Batting order: leadoff",
    "🏃 Team implied runs 5.4 — high run projection",
    "👥 Runners on avg 1.4 PA/game — RBI opportunities",
    "📈 Recent form: 8-of-10 games with 1+ H (l10 form)",
    "💡 +6.5% edge vs market consensus (closing line)",
]


def _pick(sport: str, market: str) -> dict:
    return {"sport": sport, "market": market, "selection": "Over 0.5",
            "player_name": "Test Player", "event": "Team A @ Team B"}


# ── MLB market family classifier smoke tests ─────────────────────────
def test_classify_mlb_home_run():
    assert classify_market(_pick("MLB", "Home Run - Over 0.5")) == MarketFamily.MLB_HR


def test_classify_mlb_hits():
    assert classify_market(_pick("MLB", "Hits Over 0.5")) == MarketFamily.MLB_HITS


def test_classify_mlb_rbi():
    assert classify_market(_pick("MLB", "RBI Over 0.5")) == MarketFamily.MLB_RBI


def test_classify_mlb_outs_recorded():
    assert classify_market(_pick("MLB", "Outs Recorded Over 17.5")) == MarketFamily.MLB_OUTS


def test_classify_mlb_strikeouts():
    assert classify_market(_pick("MLB", "Strikeouts Over 6.5")) == MarketFamily.MLB_KS


def test_classify_mlb_total():
    assert classify_market(_pick("MLB", "Total Runs Over 8.5")) == MarketFamily.MLB_TOTAL


def test_classify_mlb_moneyline():
    assert classify_market(_pick("MLB", "Yankees Moneyline")) == MarketFamily.MLB_ML


# ── Cross-market isolation: same pool → different top bullets ────────
def test_hr_pick_does_not_surface_pitcher_k_stat():
    """The pitcher K/9 bullet must NOT be a top-3 evidence line for an
    HR pick — it belongs to the strikeouts market."""
    pick = _pick("MLB", "Home Run - Over 0.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    joined = " || ".join(top).lower()
    # Sanity: HR-specific bullets ARE present
    assert "hr/9" in joined, f"HR pick should surface Pitcher HR/9: {joined}"
    assert "iso" in joined or "isolated power" in joined, \
        f"HR pick should surface ISO: {joined}"
    # Anti-check: the pitcher K/9 bullet should NOT be in the top-5
    assert "k/9 = 11.3" not in joined and "strikeout stuff" not in joined, (
        f"HR pick MUST NOT surface pitcher K/9: {joined}"
    )


def test_hits_pick_surfaces_hit_specific_features():
    pick = _pick("MLB", "Hits Over 0.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "vs rhp" in joined or "hp split" in joined, \
        f"Hits pick should surface handedness split: {joined}"
    assert "recent form" in joined or "l10 form" in joined, \
        f"Hits pick should surface recent form: {joined}"


def test_rbi_pick_prefers_lineup_and_team_runs():
    pick = _pick("MLB", "RBI Over 0.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "batting order" in joined or "leadoff" in joined, \
        f"RBI pick should surface batting order: {joined}"
    assert "run projection" in joined or "implied runs" in joined, \
        f"RBI pick should surface team run projection: {joined}"
    # RBI should also mention runners on
    assert "runners on" in joined or "rbi opp" in joined, \
        f"RBI pick should mention runners on / RBI opp: {joined}"


def test_outs_pick_does_not_surface_strikeout_stats():
    """User's canonical example: Outs-Recorded pick should show IP,
    pitch count, quality start rate — NOT K% or swstr."""
    pick = _pick("MLB", "Outs Recorded Over 17.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    joined = " || ".join(top).lower()
    # OUTS-specific bullets ARE present
    assert "ip/gs" in joined or "innings per start" in joined, \
        f"Outs pick MUST surface IP/GS: {joined}"
    assert "pitch count" in joined, \
        f"Outs pick MUST surface pitch count: {joined}"
    assert "quality start" in joined, \
        f"Outs pick MUST surface quality start rate: {joined}"
    # Anti-check: pure strikeout-specific bullets should NOT be top-5
    assert "chase rate" not in joined, (
        f"Outs pick MUST NOT surface chase rate: {joined}"
    )
    assert "swinging strike" not in joined and "swstr" not in joined, (
        f"Outs pick MUST NOT surface swinging strike rate: {joined}"
    )


def test_outs_pick_blocks_stale_strikeout_bullets():
    """Regression (2026-07-07): a Pitcher-Outs pick that was previously
    enriched as a Strikeouts pick used to have "avg 6.8 K's / start"
    stuck in its rationale. `_CROSS_MARKET_BLOCK["MLB_OUTS"]` must
    strip those leftovers before ranking.
    """
    pick = _pick("MLB", "Outs Recorded Over 17.5")
    stale_pool = [
        "⚾ Season avg 6.8 K's / start — sitting right at the 5.5 line",
        "📉 Only 4.1 K avg vs BOS in last 3 starts",
        "⚾ Season avg 17.9 outs / start (6.0 IP) — right on the 17.5 line",
        "🔥 pitch count avg 96 per outing",
    ]
    top = select_top_evidence(pick, stale_pool, max_n=5)
    joined = " || ".join(top).lower()
    # The stale K bullets MUST be gone
    assert "k's / start" not in joined, \
        f"MLB_OUTS blocklist should drop K's / start bullet: {joined}"
    assert "k avg" not in joined, \
        f"MLB_OUTS blocklist should drop K avg bullet: {joined}"
    # The correct outs bullet MUST survive
    assert "outs / start" in joined, \
        f"Outs bullet must survive: {joined}"


def test_hr_pick_blocks_stale_pitcher_outs_bullets():
    """Symmetric regression: a batter HR pick must never surface leftover
    pitcher-outs bullets from a prior mis-enrichment pass.
    """
    pick = _pick("MLB", "Home Run Over 0.5")
    stale_pool = [
        "⚾ Season avg 17.9 outs / start (6.0 IP)",
        "🎯 pitcher HR/9: 1.42 (elite)",
        "🔥 batter ISO .285",
    ]
    top = select_top_evidence(pick, stale_pool, max_n=5)
    joined = " || ".join(top).lower()
    assert "outs / start" not in joined, \
        f"MLB_HR blocklist should drop outs bullet: {joined}"
    assert "hr/9" in joined or "iso" in joined, \
        f"Real HR bullets must survive: {joined}"



def test_ks_pick_surfaces_k_specific_features():
    pick = _pick("MLB", "Strikeouts Over 6.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "k/9" in joined or "k rate" in joined or "opp k%" in joined, \
        f"Ks pick MUST surface K rate stats: {joined}"
    assert "swstr" in joined or "swinging strike" in joined, \
        f"Ks pick MUST surface swinging strike rate: {joined}"
    assert "chase rate" in joined, \
        f"Ks pick MUST surface chase rate: {joined}"


# ── Cap check ────────────────────────────────────────────────────────
def test_max_5_reasons_returned():
    pick = _pick("MLB", "Home Run - Over 0.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=5)
    assert len(top) <= 5, f"Too many bullets returned: {len(top)}"


def test_max_n_3_option():
    pick = _pick("MLB", "Home Run - Over 0.5")
    top = select_top_evidence(pick, _MLB_POOL, max_n=3)
    assert len(top) <= 3, f"max_n=3 not honored: {len(top)}"


def test_empty_input_returns_empty():
    pick = _pick("MLB", "Home Run - Over 0.5")
    assert select_top_evidence(pick, []) == []
    assert select_top_evidence(pick, None) == []


# ── NBA sanity ───────────────────────────────────────────────────────
_NBA_POOL = [
    "⏱ Minutes projection 34.2 — starter volume",
    "📈 Usage rate 27.4% — high usage",
    "🎯 Shot attempts 20.5 per game",
    "🛡 Opp def rating 118 — soft matchup",
    "🏥 Star teammate out — role change (starter out)",
    "🎯 Potential assists 12.4 per game",
    "🏃 Pace 102.8 possessions",
    "🎳 Rebound chances 14 per game",
    "🥅 Season 3P% 39.4",
]


def test_nba_points_surfaces_usage_and_minutes():
    pick = _pick("NBA", "LeBron James Points Over 25.5")
    top = select_top_evidence(pick, _NBA_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "minutes" in joined, f"NBA Points should surface minutes: {joined}"
    assert "usage" in joined, f"NBA Points should surface usage: {joined}"


def test_nba_assists_surfaces_potential_assists():
    pick = _pick("NBA", "Nikola Jokic Assists Over 8.5")
    top = select_top_evidence(pick, _NBA_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "potential assist" in joined or "pot ast" in joined, \
        f"NBA Assists should surface potential assists: {joined}"


def test_nba_rebounds_surfaces_rebound_chances():
    pick = _pick("NBA", "Anthony Davis Rebounds Over 10.5")
    top = select_top_evidence(pick, _NBA_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "rebound chances" in joined or "reb opp" in joined, \
        f"NBA Rebounds should surface rebound chances: {joined}"


# ── Tennis sanity ────────────────────────────────────────────────────
_TENNIS_POOL = [
    "🎯 Overall Elo gap +180",
    "🎾 Grass surface Elo gap +240 — surface specialist",
    "📉 30-day form: 8-2",
    "🎳 Hold % 87.3 — dominant serve",
    "🔨 Return break % 22.1",
    "📊 Tiebreak record 12-4 recent",
    "💡 +4.2% edge vs closing line",
]


def test_tennis_match_winner_surfaces_elo_and_surface():
    pick = _pick("Tennis", "Alcaraz to win vs Sinner")
    top = select_top_evidence(pick, _TENNIS_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "elo" in joined, f"Tennis match should surface Elo: {joined}"
    assert "surface" in joined or "grass" in joined, \
        f"Tennis match should surface surface record: {joined}"
    assert "form" in joined, f"Tennis match should surface recent form: {joined}"


def test_tennis_total_games_surfaces_hold_break():
    pick = _pick("Tennis", "Total Games Over 22.5")
    top = select_top_evidence(pick, _TENNIS_POOL, max_n=5)
    joined = " || ".join(top).lower()
    assert "hold" in joined, f"Tennis games should surface hold%: {joined}"
    assert "break" in joined, f"Tennis games should surface break%: {joined}"


# ── Debug explain trace ──────────────────────────────────────────────
def test_explain_selection_returns_diagnostic_trace():
    pick = _pick("MLB", "Home Run - Over 0.5")
    trace = explain_selection(pick, _MLB_POOL[:5])
    assert trace["sport"] == "MLB"
    assert trace["family"] == "MLB_HR"
    assert isinstance(trace["profile_keys"], list) and trace["profile_keys"]
    assert isinstance(trace["trace"], list) and len(trace["trace"]) == 5
    # Every trace entry should have matched_key + weight (or None each)
    for row in trace["trace"]:
        assert "matched_key" in row and "weight" in row


# ── Profile catalog integrity ────────────────────────────────────────
def test_every_market_family_has_profile():
    """Regression check — if we add a new MarketFamily we must add
    a PROFILES entry so classify_market never keys into a missing
    profile at runtime."""
    for fam in MarketFamily:
        assert fam in PROFILES, f"MarketFamily.{fam.name} missing from PROFILES"


def test_profile_weights_are_valid():
    for fam, keys in PROFILES.items():
        for k in keys:
            assert 0.0 <= k.weight <= 1.0, (
                f"{fam.name}.{k.label} weight out of range: {k.weight}"
            )


if __name__ == "__main__":
    # Standalone runner for quick smoke tests without pytest.
    import inspect
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
            except AssertionError as e:
                print(f"FAIL {name}: {e}")
                failed += 1
    print(f"passed={passed} failed={failed}")
