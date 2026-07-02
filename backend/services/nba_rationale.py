"""NBA sport-rationale evidence builder.

Uses the season-average `player_stats` collection populated nightly from
ESPN's public JSON API. NBA game-log persistence is TBD, so this builder
focuses on season signals: PPG, RPG, APG, MPG, shooting %, GP, plus
composite markets like PRA (Points+Rebounds+Assists).

Public API
----------
    await build_nba_rationale(db, pick, player_name) → {evidence, concerns}

Author: PerkLocks AI · 2026-07-01 (Season prep)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.nba_rationale")

# ─────────────────────────── Market taxonomy ───────────────────────────
_PTS   = ("points", " pts")
_REB   = ("rebounds", " reb", "boards")
_AST   = ("assists", " ast", " asts")
_PRA   = ("pts + reb + ast", "points + rebounds + assists",
         "p+r+a", "pra", "points rebounds assists")
_PR    = ("pts + reb", "points + rebounds", "p+r")
_PA    = ("pts + ast", "points + assists", "p+a")
_RA    = ("reb + ast", "rebounds + assists", "r+a")
_THREES = ("three pointers made", "3-pt made", "three-pointers", "3pm",
          "3-pointers made", "made threes")
_STL   = ("steals", " stl")
_BLK   = ("blocks", " blk")
_TO    = ("turnovers", " to ")

_ALL_NBA_PROP_MARKETS = (
    _PTS + _REB + _AST + _PRA + _PR + _PA + _RA
    + _THREES + _STL + _BLK + _TO
)


def is_nba_player_prop(market: str) -> bool:
    m = (market or "").lower()
    return any(needle in m for needle in _ALL_NBA_PROP_MARKETS)


def _match_any(market: str, needles: tuple[str, ...]) -> bool:
    m = (market or "").lower()
    return any(n in m for n in needles)


def _extract_line(market: str) -> Optional[float]:
    m = re.search(r"\b(?:Over|Under)\s+(\d+(?:\.\d+)?)", market or "", re.I)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)", market or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s or s == "--":
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


async def _fetch_season(db, player_name: str) -> Optional[dict]:
    cn = player_name.strip().lower()
    if not cn:
        return None
    try:
        doc = await db.player_stats.find_one(
            {"sport": "nba", "canonical_name": cn},
            sort=[("season", -1)],
        )
    except Exception as e:
        logger.debug("NBA season lookup failed for %s: %s", player_name, e)
        return None
    if not doc:
        return None
    return doc.get("stats") or None


# ─────────────────────────── Per-market builders ───────────────────────
def _compare_to_line(stat_name: str, value: float, line: Optional[float],
                     player: str, emoji: str, gp: Optional[float],
                     min_cushion: float) -> dict[str, list[str]]:
    """Generic pattern: 'Booker averaging 27.4 PPG — line 24.5 (+2.9 cushion)'"""
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    last = player.split()[-1] if player else "Player"
    gp_tag = f" ({int(gp)} GP)" if gp else ""
    if line is None:
        out["evidence"].append(f"{emoji} {last}: {value:.1f} {stat_name}/game season{gp_tag}")
        return out
    delta = value - line
    if delta >= min_cushion:
        out["evidence"].append(
            f"{emoji} {last}: {value:.1f} {stat_name}/game season{gp_tag}"
            f" — line {line:g} (+{delta:.1f} cushion)"
        )
    elif delta <= -min_cushion:
        out["concerns"].append(
            f"📉 Season avg only {value:.1f} {stat_name}/game{gp_tag}"
            f" — line of {line:g} requires above-trend output"
        )
    else:
        out["evidence"].append(
            f"{emoji} {last}: {value:.1f} {stat_name}/game season{gp_tag}"
            f" — sitting near the {line:g} line"
        )
    return out


def _minutes_context(stats: dict) -> Optional[str]:
    mpg = _to_float(stats.get("MIN"))
    if mpg is None:
        return None
    if mpg >= 34:
        return f"⏱️ {mpg:.1f} MPG — high-usage starter"
    if mpg >= 28:
        return f"⏱️ {mpg:.1f} MPG — solid starter minutes"
    if mpg <= 20:
        return f"⚠️ Only {mpg:.1f} MPG — rotation piece"
    return None


async def build_nba_rationale(
    db, pick: dict, player_name: Optional[str],
) -> dict[str, list[str]]:
    """Season-average NBA rationale. Composite markets (PRA / PR / PA)
    sum the relevant averages before comparing to the line."""
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    if not player_name:
        return out
    market = pick.get("market") or ""
    line = _extract_line(market)

    stats = await _fetch_season(db, player_name)
    if not stats:
        return out

    gp = _to_float(stats.get("GP"))
    ppg = _to_float(stats.get("PTS"))
    rpg = _to_float(stats.get("REB"))
    apg = _to_float(stats.get("AST"))
    fg = _to_float(stats.get("FG%"))
    tpp = _to_float(stats.get("3P%"))
    ft = _to_float(stats.get("FT%"))
    stl = _to_float(stats.get("STL"))
    blk = _to_float(stats.get("BLK"))
    to = _to_float(stats.get("TO"))

    # Composite markets first (most specific)
    if _match_any(market, _PRA):
        if ppg is not None and rpg is not None and apg is not None:
            pra = ppg + rpg + apg
            r = _compare_to_line("PRA", pra, line, player_name, "🏀", gp, 2.5)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
            out["evidence"].append(
                f"📊 Splits: {ppg:.1f} PPG · {rpg:.1f} RPG · {apg:.1f} APG"
            )
    elif _match_any(market, _PR):
        if ppg is not None and rpg is not None:
            pr = ppg + rpg
            r = _compare_to_line("P+R", pr, line, player_name, "🏀", gp, 2.0)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _PA):
        if ppg is not None and apg is not None:
            pa = ppg + apg
            r = _compare_to_line("P+A", pa, line, player_name, "🏀", gp, 2.0)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _RA):
        if rpg is not None and apg is not None:
            ra = rpg + apg
            r = _compare_to_line("R+A", ra, line, player_name, "🏀", gp, 1.5)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _PTS):
        if ppg is not None:
            r = _compare_to_line("PTS", ppg, line, player_name, "🏀", gp, 1.5)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
            if fg is not None and fg >= 55:
                out["evidence"].append(f"🎯 {fg:.1f}% FG — efficient scorer")
            elif fg is not None and fg <= 40:
                out["concerns"].append(f"🚧 Only {fg:.1f}% FG — inefficient")
    elif _match_any(market, _REB):
        if rpg is not None:
            r = _compare_to_line("REB", rpg, line, player_name, "🏀", gp, 1.0)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _AST):
        if apg is not None:
            r = _compare_to_line("AST", apg, line, player_name, "🎯", gp, 1.0)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _THREES):
        # No dedicated 3PM in season aggregate → approximate from 3P%
        # + typical 3PA rate (heuristic only).
        if tpp is not None and gp is not None and gp >= 10:
            out["evidence"].append(
                f"🎯 {tpp:.1f}% from 3 ({int(gp)} GP) — shooting benchmark"
            )
        if line is not None and line >= 3.5 and tpp is not None and tpp < 33:
            out["concerns"].append(f"❄️ Only {tpp:.1f}% from 3 — high line risk")
    elif _match_any(market, _STL):
        if stl is not None:
            r = _compare_to_line("STL", stl, line, player_name, "🥷", gp, 0.4)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _BLK):
        if blk is not None:
            r = _compare_to_line("BLK", blk, line, player_name, "🧱", gp, 0.4)
            out["evidence"].extend(r["evidence"])
            out["concerns"].extend(r["concerns"])
    elif _match_any(market, _TO):
        if to is not None:
            # For turnovers, LOW is good — invert semantics
            if line is not None:
                delta = line - to
                if delta >= 0.7:
                    out["evidence"].append(
                        f"🛡️ Only {to:.1f} TO/game — under-line cushion +{delta:.1f}"
                    )
                elif delta <= -0.7:
                    out["concerns"].append(
                        f"⚠️ {to:.1f} TO/game — line {line:g} risky"
                    )

    # Common context — minutes + games-played reliability
    mtx = _minutes_context(stats)
    if mtx:
        (out["evidence"] if not mtx.startswith("⚠️") else out["concerns"]).append(mtx)

    if gp is not None:
        if gp < 15:
            out["concerns"].append(f"📊 Small sample: only {int(gp)} games played")
        elif gp >= 60:
            out["evidence"].append(f"📊 Reliable sample: {int(gp)} games this season")

    return out
