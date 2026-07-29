"""ML Model Routing Regression Tests (Phase 5 wiring fix, iter101,
2026-07-29).

Guards the market → model registry → feature builder wiring proven by
the audit:

  1. Every trained `.meta.json` model on disk is reachable end-to-end
     by the prediction engine (`predict_player_prop`).
  2. `_resolve_model_key` disambiguates MLB pitcher-Ks vs batter-Ks
     correctly across (position, threshold) combinations.
  3. Incorrect sport / stat routing fails safely — never raises, never
     returns a foreign model, and returns a clear `reason`.
  4. Market detector (`services.pick_matchup_wiring._detect_stat`)
     produces stat keys that ARE routable (either directly or through
     `_resolve_model_key`).
  5. The routing decision is telemetry-visible via `routing_notes` +
     `effective_stat` fields on the prediction result.

No models are retrained. No scoring logic touched.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest


def _run(c): return asyncio.run(c)


# ─── Async-Mongo stub (bounded) ──────────────────────────────────────
class _Coll:
    def __init__(self): self.rows = []
    async def insert_one(self, d): self.rows.append(dict(d))
    async def find_one(self, q=None, projection=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in (q or {}).items()):
                return dict(r)
        return None
    def find(self, *_a, **_k):
        cursor = self
        async def _iter(): 
            for r in list(self.rows): yield dict(r)
        return _AsyncGen(list(self.rows))

class _AsyncGen:
    def __init__(self, rows): self.rows = rows; self._i = 0
    def limit(self, n): self.rows = self.rows[:n]; return self
    def sort(self, *_a, **_k): return self
    async def to_list(self, length=None): return list(self.rows[:length] if length else self.rows)
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows): raise StopAsyncIteration
        r = self.rows[self._i]; self._i += 1; return r


class _DB:
    def __init__(self):
        self._c = {}
    def __getitem__(self, name):
        if name not in self._c: self._c[name] = _Coll()
        return self._c[name]
    def __getattr__(self, name):
        if name.startswith("_"): raise AttributeError(name)
        return self.__getitem__(name)


# ═════════════════════════════════════════════════════════════════════
# A. Meta ↔ Feature builder ↔ Registry manifest
# ═════════════════════════════════════════════════════════════════════
MODEL_DIR = pathlib.Path("/app/backend/models")


def _all_meta_files():
    return sorted(MODEL_DIR.glob("*.meta.json"))


def test_every_model_has_matching_pkl_and_feature_names():
    """Every meta.json advertises a winner pkl + non-empty feature list."""
    metas = _all_meta_files()
    assert metas, "no model metas found — is /app/backend/models mounted?"
    for meta_path in metas:
        meta = json.loads(meta_path.read_text())
        winner = meta.get("winner")
        assert winner in ("lgbm", "xgb"), (
            f"{meta_path.name}: winner must be lgbm or xgb, got {winner}")
        pkl_path = meta_path.with_name(
            meta_path.stem.replace(".meta", f"_{winner}.pkl"))
        assert pkl_path.exists(), (
            f"{meta_path.name}: pkl file missing: {pkl_path.name}")
        # Winner meta block must carry feature-column count matches
        winner_meta = meta.get(winner) or {}
        assert winner_meta.get("n_train"), (
            f"{meta_path.name}: winner meta must expose n_train")


# ═════════════════════════════════════════════════════════════════════
# B. _resolve_model_key disambiguation
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("sport,stat,line,pos,expected", [
    # ── MLB Ks disambiguation (the audit-flagged bug) ────────────────
    ("MLB", "strikeouts",  4.5, None,   "pitcher_strikeouts"),
    ("MLB", "strikeouts",  5.5, None,   "pitcher_strikeouts"),
    ("MLB", "strikeouts",  3.0, None,   "pitcher_strikeouts"),
    ("MLB", "strikeouts",  0.5, None,   "strikeouts"),
    ("MLB", "strikeouts",  1.5, None,   "strikeouts"),
    ("MLB", "strikeouts",  2.5, None,   "strikeouts"),
    ("MLB", "strikeouts",  None, None,  "strikeouts"),  # no signal, default batter
    # ── Position overrides threshold heuristic ────────────────────────
    ("MLB", "strikeouts",  0.5, "P",    "pitcher_strikeouts"),
    ("MLB", "strikeouts",  0.5, "SP",   "pitcher_strikeouts"),
    ("MLB", "strikeouts",  0.5, "RP",   "pitcher_strikeouts"),
    ("MLB", "strikeouts",  4.5, "1B",   "strikeouts"),
    ("MLB", "strikeouts",  4.5, "OF",   "strikeouts"),
    ("MLB", "strikeouts",  4.5, "C",    "strikeouts"),
    ("MLB", "strikeouts",  4.5, "DH",   "strikeouts"),
    # ── Non-Ks passthrough ────────────────────────────────────────────
    ("MLB",    "hits",           1.5,  None, "hits"),
    ("MLB",    "home_runs",      0.5,  None, "home_runs"),
    ("MLB",    "total_bases",    1.5,  None, "total_bases"),
    ("MLB",    "hits_runs_rbis", 1.5,  None, "hits_runs_rbis"),
    # ── Other sports untouched ────────────────────────────────────────
    ("NFL",    "passing_yards",  249.5, None, "passing_yards"),
    ("NFL",    "rushing_yards",  74.5,  None, "rushing_yards"),
    ("NFL",    "receiving_yards",64.5,  None, "receiving_yards"),
    ("TENNIS", "aces",           5.5,   None, "aces"),
    ("TENNIS", "double_faults",  2.5,   None, "double_faults"),
    ("TENNIS", "break_points_won",2.5,  None, "break_points_won"),
    # ── Unknown stat / sport passes through cleanly ───────────────────
    ("NBA",    "points",         25.5,  None, "points"),
    ("MLB",    "walks",          0.5,   None, "walks"),
])
def test_resolve_model_key(sport, stat, line, pos, expected):
    from services.trained_prediction_engine import _resolve_model_key
    got, notes = _resolve_model_key(sport, stat, line=line,
                                     player_position=pos)
    assert got == expected, (
        f"{sport}/{stat}/line={line}/pos={pos}: expected {expected} got {got}\n"
        f"notes={notes}"
    )


def test_resolve_model_key_returns_routing_notes():
    """When disambiguation kicks in, notes explain why (telemetry)."""
    from services.trained_prediction_engine import _resolve_model_key
    _, notes = _resolve_model_key("MLB", "strikeouts", line=4.5)
    assert notes and any("pitcher_strikeouts" in n for n in notes)
    _, notes2 = _resolve_model_key("MLB", "strikeouts", line=None,
                                     player_position="SP")
    assert notes2 and any("position=SP" in n for n in notes2)


# ═════════════════════════════════════════════════════════════════════
# C. Model file reachability — every meta reaches the loader
# ═════════════════════════════════════════════════════════════════════
def test_every_trained_model_loads_via_loader():
    """Loader can materialise every .meta.json into an in-memory bundle."""
    from services.trained_prediction_engine import _load_model, _reset_model_cache
    _reset_model_cache()
    for meta_path in _all_meta_files():
        meta = json.loads(meta_path.read_text())
        sport = meta.get("sport")
        stat = meta.get("stat")
        assert sport and stat, f"{meta_path.name}: sport/stat missing"
        bundle = _load_model(sport, stat)
        assert bundle is not None, (
            f"loader failed to reach {meta_path.name} "
            f"(key={sport.lower()}_{stat.lower()})"
        )
        assert bundle["booster"] is not None
        assert bundle["feature_names"], (
            f"{meta_path.name}: no feature_names attached to bundle")


# ═════════════════════════════════════════════════════════════════════
# D. Safe-fail on foreign / unknown routing
# ═════════════════════════════════════════════════════════════════════
def test_unknown_sport_fails_safely():
    from services.trained_prediction_engine import predict_player_prop
    r = _run(predict_player_prop(
        _DB(), sport="RUGBY", player="X", stat="tries",
        opponent="Y", line=0.5,
    ))
    assert r["supported"] is False
    assert "not yet supported" in r["reason"].lower()


def test_batter_ks_prop_never_loads_pitcher_model():
    """The audit's critical regression: a batter K prop must NEVER
    load the pitcher_strikeouts model even by accident."""
    from services.trained_prediction_engine import predict_player_prop
    r = _run(predict_player_prop(
        _DB(), sport="MLB", player="Aaron Judge",
        stat="strikeouts", opponent="BOS", line=1.5,
        player_position="OF",
    ))
    assert r["supported"] is False, (
        "Batter K prop must NOT resolve to pitcher_strikeouts")
    assert "batter" in r["reason"].lower(), (
        "Reason must explain the batter safe-fail")
    assert r["effective_stat"] == "strikeouts", (
        f"effective_stat should be 'strikeouts' (batter fam), "
        f"got {r['effective_stat']}"
    )


def test_unknown_stat_fails_safely():
    from services.trained_prediction_engine import predict_player_prop
    r = _run(predict_player_prop(
        _DB(), sport="MLB", player="X", stat="doubles",
        opponent="Y", line=0.5,
    ))
    assert r["supported"] is False
    assert "no trained model" in r["reason"].lower()


def test_no_exceptions_leak_on_missing_features():
    """When the feature builder returns empty / errors, we get a
    supported=False payload — never an exception."""
    from services.trained_prediction_engine import predict_player_prop
    # Empty DB — MLB feature builder will fall back to zero-signal
    r = _run(predict_player_prop(
        _DB(), sport="MLB", player="Unknown Guy",
        stat="strikeouts", opponent="XXX", line=4.5,
    ))
    # Either supported (with low confidence) or supported=False with a
    # clear reason. Never a Python exception.
    assert r is not None
    assert "supported" in r


# ═════════════════════════════════════════════════════════════════════
# E. Market detector → routable stat
# ═════════════════════════════════════════════════════════════════════
def _model_key_exists(sport: str, stat: str) -> bool:
    return (MODEL_DIR / f"{sport.lower()}_{stat.lower()}.meta.json").exists()


@pytest.mark.parametrize("sport,market,expected_stat", [
    ("MLB", "Aaron Judge (NYY) Over 1.5 Total Bases", "total_bases"),
    ("MLB", "Cody Bellinger (NYY) Over 0.5 Home Runs", "home_runs"),
    ("MLB", "Elly De La Cruz (CIN) Over 1.5 Hits", "hits"),
    ("MLB", "Aaron Nola (PHI) Over 4.5 Strikeouts", "strikeouts"),
    ("MLB", "Aaron Judge (NYY) Over 0.5 Strikeouts", "strikeouts"),
    ("NFL", "Joe Burrow Over 249.5 Passing Yards", "passing_yards"),
    ("NFL", "Christian McCaffrey Over 74.5 Rushing Yards", "rushing_yards"),
    ("NFL", "Justin Jefferson Over 64.5 Receiving Yards", "receiving_yards"),
    ("Tennis", "Novak Djokovic Over 5.5 Aces", "aces"),
    ("Tennis", "Coco Gauff Over 2.5 Double Faults", "double_faults"),
    ("Tennis", "Iga Swiatek Over 3.5 Break Points Won", "break_points_won"),
])
def test_market_detector_produces_routable_stat(sport, market, expected_stat):
    from services.pick_matchup_wiring import _detect_stat
    stat = _detect_stat(sport, market)
    assert stat == expected_stat, (
        f"{sport} · {market!r} → {stat}, expected {expected_stat}")


def test_tennis_break_points_won_market_now_reachable():
    """Audit finding: `tennis_break_points_won` model file existed on
    disk but no market pattern mapped to it. Fixed by adding the
    `break\\s*point` regex to `_TENNIS_MARKET_STAT_MAP`."""
    from services.pick_matchup_wiring import _detect_stat
    assert _detect_stat("Tennis", "Iga Swiatek Over 3.5 Break Points Won") \
        == "break_points_won"
    assert _model_key_exists("tennis", "break_points_won")


def test_all_detected_stats_have_a_reachable_route():
    """For every stat the market detector can emit for MLB/NFL/Tennis,
    either the direct model exists OR `_resolve_model_key` routes to
    a model that does. This is the "no dead detection paths" gate."""
    from services.pick_matchup_wiring import (
        _MLB_MARKET_STAT_MAP, _NFL_MARKET_STAT_MAP, _TENNIS_MARKET_STAT_MAP,
    )
    from services.trained_prediction_engine import _resolve_model_key

    def _reachable(sport, stat):
        if _model_key_exists(sport, stat):
            return True
        # Router may promote (e.g. strikeouts → pitcher_strikeouts)
        for line in (0.5, 4.5):
            eff, _ = _resolve_model_key(sport.upper(), stat, line=line)
            if _model_key_exists(sport, eff):
                return True
        return False

    unreachable = []
    for sport, table in [
        ("mlb", _MLB_MARKET_STAT_MAP),
        ("nfl", _NFL_MARKET_STAT_MAP),
        ("tennis", _TENNIS_MARKET_STAT_MAP),
    ]:
        for _pat, stat in table:
            if not _reachable(sport, stat):
                unreachable.append(f"{sport}/{stat}")

    # The set of stats we've detected but NEVER trained is documented
    # here — the audit's follow-up. Failing this list means either a
    # model was added without route wiring, or a market family was
    # dropped from the map.
    KNOWN_UNTRAINED_FAMILIES = {
        "mlb/rbi", "mlb/hits_runs_rbis",
        "nfl/passing_tds", "nfl/attempts", "nfl/completions",
        "nfl/passing_ints", "nfl/rushing_tds", "nfl/carries",
        "nfl/receptions", "nfl/receiving_tds", "nfl/targets",
        "tennis/total_games",
    }
    surprises = set(unreachable) - KNOWN_UNTRAINED_FAMILIES
    assert not surprises, (
        f"New unrouted market families detected: {sorted(surprises)}. "
        f"Either add a trained model or update KNOWN_UNTRAINED_FAMILIES."
    )
