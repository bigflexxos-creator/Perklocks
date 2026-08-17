"""Focused tests for the player-meta headshot decorator.

Certifies:
 T1  REAL PLAYER RESOLUTION       — canonical (sport, name, team)
                                    resolves the correct verified
                                    headshot from db.players.
 T2  WRONG-PLAYER PREVENTION      — ambiguous / unknown identity
                                    returns NO headshot instead of
                                    fuzzy-matching.
 T3  PLAYER PROP                  — player-prop pick receives
                                    ``player_meta.headshot_url``.
 T4  GAME MARKET                  — Moneyline / Spread / Total picks
                                    receive NO player_meta stamp.
 T5  IMAGE-URL SAFETY             — non-HTTPS / non-image URLs are
                                    refused; card falls back.
 T7  CACHE                        — a second decoration for the same
                                    canonical player DOES NOT trigger
                                    another db.players lookup.
 T8  CANONICAL SAFETY             — stamping player_meta must NOT
                                    mutate lock_score / published_lock_score
                                    / win_probability / edge / odds /
                                    line / market / selection / pick id.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────
# Test scaffolding — stub db.players via monkeypatched find_player
# ─────────────────────────────────────────────────────────────────────
class _StubDB:
    """Placeholder — the decorator's DB argument is not read directly;
    the actual query goes through player_db.client.find_player which
    we monkeypatch."""


ROSTER = {
    # (sport_key_lower, canonical_name_lower, team_upper)
    ("mlb",  "aaron judge",     "NYY"): {
        "sport": "mlb", "canonical_name": "aaron judge",
        "team": "NYY", "display_name": "Aaron Judge",
        "player_id": 592450, "mlb_id": 592450,
        "photo_url": "https://midfield.mlbstatic.com/v1/people/592450/spots/120",
        "source": "mlb_stats_api",
    },
    ("mlb",  "gerrit cole",     "NYY"): {
        "sport": "mlb", "canonical_name": "gerrit cole",
        "team": "NYY", "display_name": "Gerrit Cole",
        "player_id": 543037, "mlb_id": 543037,
        "photo_url": "https://midfield.mlbstatic.com/v1/people/543037/spots/120",
        "source": "mlb_stats_api",
    },
    ("nfl",  "patrick mahomes", "KC"): {
        "sport": "nfl", "canonical_name": "patrick mahomes",
        "team": "KC", "display_name": "Patrick Mahomes",
        "player_id": 3139477, "espn_id": 3139477,
        "photo_url": "https://a.espncdn.com/i/headshots/nfl/players/full/3139477.png",
        "source": "espn",
    },
}
# Counts calls into find_player so we can verify caching.
CALL_COUNT = {"n": 0}


async def _stub_find_player(sport: str, name: str, team=None):
    CALL_COUNT["n"] += 1
    key = ((sport or "").lower(), (name or "").strip().lower(),
           (team or "").upper() if team else None)
    # Exact match with team.
    if key[2] and (key[0], key[1], key[2]) in ROSTER:
        return ROSTER[(key[0], key[1], key[2])]
    # Match without team — return only if EXACTLY one candidate exists
    # (safety guard against wrong-photo attribution).
    candidates = [v for (s, n, _t), v in ROSTER.items()
                  if s == key[0] and n == key[1]]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _install_stub(monkeypatch):
    CALL_COUNT["n"] = 0
    # Reset the decorator's module cache between tests.
    from services import player_meta_decorator as pmd
    pmd._cache.clear()
    monkeypatch.setattr(
        "player_db.client.find_player", _stub_find_player, raising=True,
    )


# ─────────────────────────────────────────────────────────────────────
# Helper builders
# ─────────────────────────────────────────────────────────────────────
def _prop_pick(*, pid, sport, player, team, market="Hits", extras=None):
    p = {
        "id": pid,
        "sport": sport,
        "market": market,
        "selection": f"{player} Over 0.5",
        "selection_v2": {"selection": {"player": player, "team": team}},
        "home_meta": {"abbrev": team, "logo": f"https://a.espncdn.com/logos/{team}.png"},
        "away_meta": {"abbrev": "OPP", "logo": "https://a.espncdn.com/logos/OPP.png"},
        "win_probability": 62.5, "edge_percent": 3.2, "book_odds": -120,
        "lock_score": 91.0, "published_lock_score": 91.0, "line": 0.5,
    }
    if extras:
        p.update(extras)
    return p


def _game_pick(*, pid, sport="MLB", market="Run Line"):
    return {
        "id": pid,
        "sport": sport,
        "market": market,
        "selection": "Diamondbacks +1.5",
        "selection_v2": {"selection": {"player": None, "team": "ARI"}},
        "home_meta": {"abbrev": "ARI", "logo": "https://a.espncdn.com/logos/ARI.png"},
        "away_meta": {"abbrev": "BOS", "logo": "https://a.espncdn.com/logos/BOS.png"},
        "win_probability": 67.4, "edge_percent": 4.9, "book_odds": -188,
        "lock_score": 93.0, "published_lock_score": 93.0,
    }


# ─────────────────────────────────────────────────────────────────────
# T1 — REAL PLAYER RESOLUTION
# ─────────────────────────────────────────────────────────────────────
def test_t1_real_player_resolution(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    picks = [_prop_pick(pid="P1", sport="MLB", player="Aaron Judge", team="NYY")]
    asyncio.run(decorate_with_player_meta(_StubDB(), picks))
    pm = picks[0].get("player_meta")
    assert isinstance(pm, dict), "player_meta must be attached"
    assert pm["headshot_url"] == \
        "https://midfield.mlbstatic.com/v1/people/592450/spots/120"
    assert pm["display_name"] == "Aaron Judge"
    assert pm["team"] == "NYY"
    assert pm["headshot_verified"] is True
    assert pm.get("mlb_id") == 592450


# ─────────────────────────────────────────────────────────────────────
# T2 — WRONG PLAYER PREVENTION  (ambiguity guard)
# ─────────────────────────────────────────────────────────────────────
def test_t2_wrong_player_prevention_no_match(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    # A player we do NOT have in the roster + no team hint.
    picks = [_prop_pick(pid="X1", sport="MLB",
                        player="Some Nobody", team="TBD")]
    asyncio.run(decorate_with_player_meta(_StubDB(), picks))
    # NO stamp — decorator MUST refuse rather than guess.
    assert picks[0].get("player_meta") is None or \
        "headshot_url" not in (picks[0].get("player_meta") or {}), (
            "Decorator must NOT stamp a headshot for an unmatched player"
        )


# ─────────────────────────────────────────────────────────────────────
# T3 — PLAYER PROP path (canonical selection_v2.selection.player)
# ─────────────────────────────────────────────────────────────────────
def test_t3_player_prop_receives_meta(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    # NFL passing yards prop.
    picks = [_prop_pick(pid="P3", sport="NFL",
                        player="Patrick Mahomes", team="KC",
                        market="Passing Yards")]
    asyncio.run(decorate_with_player_meta(_StubDB(), picks))
    pm = picks[0]["player_meta"]
    assert pm["headshot_url"].endswith("3139477.png")
    assert pm["headshot_source"] == "espn"
    assert pm["external_id"] == 3139477


# ─────────────────────────────────────────────────────────────────────
# T4 — GAME MARKET path — NO player_meta stamp
# ─────────────────────────────────────────────────────────────────────
def test_t4_game_market_no_player_meta(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    picks = [_game_pick(pid="G1", sport="MLB", market="Run Line")]
    asyncio.run(decorate_with_player_meta(_StubDB(), picks))
    # Game market → no player prop → no stamp.
    assert picks[0].get("player_meta") is None
    # And find_player was never called for a game-market pick.
    assert CALL_COUNT["n"] == 0


# ─────────────────────────────────────────────────────────────────────
# T5 — IMAGE URL SAFETY
# ─────────────────────────────────────────────────────────────────────
def test_t5_refuses_non_authoritative_photo_url(monkeypatch):
    _install_stub(monkeypatch)
    # Override find_player to return a bogus URL.
    async def _bogus(sport, name, team=None):
        return {
            "canonical_name": (name or "").lower(),
            "team": team, "display_name": name,
            "photo_url": "http://untrusted.example.com/bad.exe",
            "source": "hacker",
        }
    monkeypatch.setattr("player_db.client.find_player", _bogus, raising=True)
    from services.player_meta_decorator import decorate_with_player_meta
    picks = [_prop_pick(pid="S1", sport="MLB",
                        player="Aaron Judge", team="NYY")]
    asyncio.run(decorate_with_player_meta(_StubDB(), picks))
    # Untrusted-scheme URL rejected → no stamp.
    assert picks[0].get("player_meta") is None


# ─────────────────────────────────────────────────────────────────────
# T7 — CACHE  (no repeat lookup for same canonical player)
# ─────────────────────────────────────────────────────────────────────
def test_t7_cache_avoids_repeat_lookup(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    # First board load — resolves and caches.
    b1 = [_prop_pick(pid=f"C{i}", sport="MLB",
                     player="Aaron Judge", team="NYY") for i in range(3)]
    asyncio.run(decorate_with_player_meta(_StubDB(), b1))
    calls_after_first = CALL_COUNT["n"]
    assert calls_after_first == 1, (
        f"Expected exactly 1 lookup for the same canonical player across "
        f"3 picks; got {calls_after_first}"
    )
    # Second board load — cache hit; no additional lookup.
    b2 = [_prop_pick(pid="C4", sport="MLB",
                     player="Aaron Judge", team="NYY")]
    asyncio.run(decorate_with_player_meta(_StubDB(), b2))
    assert CALL_COUNT["n"] == calls_after_first, (
        "Second board load must not trigger another lookup"
    )
    # And the stamp is still correct.
    assert b2[0]["player_meta"]["headshot_url"].endswith("/592450/spots/120")


# ─────────────────────────────────────────────────────────────────────
# T8 — CANONICAL SAFETY  (never mutates betting truth)
# ─────────────────────────────────────────────────────────────────────
def test_t8_canonical_betting_truth_unchanged(monkeypatch):
    _install_stub(monkeypatch)
    from services.player_meta_decorator import decorate_with_player_meta
    original = _prop_pick(pid="Z1", sport="MLB",
                          player="Aaron Judge", team="NYY",
                          extras={
                              "line": 1.5, "market": "Total Bases",
                              "grade": "Strong Lock", "confidence": "STRONG",
                          })
    baseline = {
        k: original[k] for k in (
            "id", "sport", "market", "selection", "lock_score",
            "published_lock_score", "win_probability", "edge_percent",
            "book_odds", "line", "grade", "confidence",
        )
    }
    asyncio.run(decorate_with_player_meta(_StubDB(), [original]))
    for k, v in baseline.items():
        assert original[k] == v, (
            f"Decorator modified frozen betting field {k!r}: "
            f"{baseline[k]!r} → {original[k]!r}"
        )
    # And player_meta WAS attached as an additive object.
    assert "headshot_url" in original["player_meta"]
