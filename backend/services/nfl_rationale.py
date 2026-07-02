"""NFL sport-rationale evidence builder.

Replaces the generic "💯 Model gives X% win prob" bullet on NFL props
with real player-level context pulled from the FREE ESPN public feed
that populates `player_stats` (season aggregates) and
`player_game_logs` (per-game stat blocks).

Public API
----------
    await build_nfl_rationale(db, pick, player_name) → {evidence, concerns}

Design goals
------------
1.  **Market-aware routing** — passing / rushing / receiving / TDs
    each pull the right season fields and compare to the prop line.
2.  **Season + recent-form blend** — season avg for volume signal;
    last-3 game logs for hot/cold streak flags.
3.  **Best-effort** — returns empty lists on any lookup failure so the
    dispatcher can fall back to the universal edge bullet.

All data is FREE (ESPN public JSON). No Odds credits consumed.

Author: PerkLocks AI · 2026-07-01 (Season prep)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.nfl_rationale")

# ─────────────────────────── Market taxonomy ───────────────────────────
# Substring match on the market string. Order matters: check more
# specific terms first (e.g. "pass tds" before "tds").
_PASS_YDS   = ("passing yards", "pass yards", "pass yds", "passing yds")
_PASS_TDS   = ("passing tds", "pass tds", "passing touchdowns")
_PASS_CMP   = ("completions", "pass completions", "passing completions")
_PASS_ATT   = ("pass attempts", "passing attempts")
_PASS_INT   = ("interceptions thrown", "passing interceptions", "pass ints")
_RUSH_YDS   = ("rushing yards", "rush yards", "rush yds", "rushing yds")
_RUSH_ATT   = ("rushing attempts", "rush attempts", "carries")
_RUSH_TDS   = ("rushing tds", "rush tds", "rushing touchdowns")
_REC_YDS    = ("receiving yards", "rec yards", "rec yds", "receiving yds")
_RECEPTIONS = ("receptions", "receiving receptions")
_REC_TDS    = ("receiving tds", "rec tds", "receiving touchdowns")
_ANY_TD     = ("anytime touchdown", "anytime td", "atd", "to score a td")

_ALL_NFL_PROP_MARKETS = (
    _PASS_YDS + _PASS_TDS + _PASS_CMP + _PASS_ATT + _PASS_INT
    + _RUSH_YDS + _RUSH_ATT + _RUSH_TDS
    + _REC_YDS + _RECEPTIONS + _REC_TDS
    + _ANY_TD
)


def is_nfl_player_prop(market: str) -> bool:
    m = (market or "").lower()
    return any(needle in m for needle in _ALL_NFL_PROP_MARKETS)


def _match_any(market: str, needles: tuple[str, ...]) -> bool:
    m = (market or "").lower()
    return any(n in m for n in needles)


def _extract_line(market: str) -> Optional[float]:
    """Parse the numeric line out of a market string like
    'Passing Yards Over 249.5' → 249.5."""
    m = re.search(r"\b(?:Over|Under)\s+(\d+(?:\.\d+)?)", market or "", re.I)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)", market or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


async def _fetch_season_stats(db, player_name: str, stat_block: str) -> Optional[dict]:
    """`stat_block` is one of: 'passing', 'rushing', 'receiving'.
    Uses `player_stats` collection (season aggregates)."""
    cn = player_name.strip().lower()
    if not cn:
        return None
    try:
        doc = await db.player_stats.find_one(
            {"sport": "nfl", "canonical_name": cn},
            sort=[("season", -1)],
        )
    except Exception as e:
        logger.debug("NFL season stats lookup failed for %s: %s", player_name, e)
        return None
    if not doc:
        return None
    stats = doc.get("stats") or {}
    # We rely on the stat-block signature (presence of certain keys) since
    # ESPN doesn't tag skill vs passer explicitly.
    if stat_block == "passing" and any(k in stats for k in ("CMP", "ATT", "RTG")):
        return stats
    if stat_block == "rushing" and "CAR" in stats:
        return stats
    if stat_block == "receiving" and "REC" in stats:
        return stats
    # Fallback — return whatever we have; caller decides.
    return stats


async def _fetch_last_games(db, player_name: str, stat_block: str,
                            limit: int = 5) -> list[dict]:
    """Pulls the most recent `limit` game-log rows for the player+block.

    ESPN game_id sorts lexicographically → not strictly time-ordered but
    close enough for a "recent form" signal. If we later persist a
    `played_at` datetime we should sort by that instead."""
    if not player_name:
        return []
    try:
        rows: list[dict] = []
        async for d in db.player_game_logs.find(
            {"sport": "nfl", "stat_block": stat_block,
             "name": {"$regex": f"^{re.escape(player_name)}$", "$options": "i"}}
        ).sort("game_id", -1).limit(limit):
            rows.append(d)
        return rows
    except Exception as e:
        logger.debug("NFL game logs lookup failed for %s: %s", player_name, e)
        return []


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s or s == "--":
            return None
        # Some ESPN cells are like "21/34" — pull the *first* number.
        m = re.match(r"-?\d+(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None
    return None


# ─────────────────────────── Per-market builders ───────────────────────
def _rationale_passing_yards(stats: dict, last: list[dict], line: Optional[float],
                             player: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    yds = _to_float(stats.get("YDS"))
    # ESPN 'passing' stats also expose ATT for volume context
    att = _to_float(stats.get("ATT"))
    # Season yards / (approx games — use ATT/33 if AVG missing since ATT
    # per game ≈ 33 for starters)
    # We rely on presence of _split_display == 'Regular Season' → those
    # are cumulative totals, not per-game averages.
    # A cleaner signal: pull YPG from last-5 games.
    ypg_last = None
    if last:
        vals = [_to_float(r.get("nfl_yds")) for r in last]
        vals = [v for v in vals if v is not None]
        if vals:
            ypg_last = sum(vals) / len(vals)
    if ypg_last is not None:
        ln = f", line {line:g}" if line is not None else ""
        if line is not None and ypg_last - line >= 25:
            out["evidence"].append(
                f"🏈 {player.split()[-1]}: {ypg_last:.0f} pass yds/game over last {len(last)}"
                f"{ln} (+{ypg_last - line:.0f} cushion)"
            )
        elif line is not None and line - ypg_last >= 25:
            out["concerns"].append(
                f"📉 Last {len(last)}: {ypg_last:.0f} pass yds/game — line {line:g}"
                f" requires above-trend output"
            )
        else:
            out["evidence"].append(
                f"🏈 {ypg_last:.0f} pass yds/game recent{ln}"
            )
    if yds and att and yds >= 500:
        # Only surface YPA efficiency after a meaningful sample. Below
        # ~500 season yards (≈2 games) the ratio is noise and misfires
        # during offseason data hydration.
        ypa = yds / att if att else 0
        if ypa >= 7.5:
            out["evidence"].append(f"🎯 {ypa:.1f} YPA on the season — efficient")
        elif ypa <= 6.0:
            out["concerns"].append(f"⚠️ Only {ypa:.1f} YPA on the season — inefficient")
    rtg = _to_float(stats.get("RTG"))
    if rtg is not None:
        if rtg >= 95:
            out["evidence"].append(f"⭐ {rtg:.0f} passer rating (elite)")
        elif rtg <= 80:
            out["concerns"].append(f"🚧 {rtg:.0f} passer rating — struggling")
    return out


def _rationale_rushing_yards(stats: dict, last: list[dict], line: Optional[float],
                             player: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    ypg_last = None
    car_last = None
    if last:
        yv = [_to_float(r.get("nfl_yds")) for r in last]
        yv = [v for v in yv if v is not None]
        cv = [_to_float(r.get("nfl_car")) for r in last]
        cv = [v for v in cv if v is not None]
        if yv:
            ypg_last = sum(yv) / len(yv)
        if cv:
            car_last = sum(cv) / len(cv)
    if ypg_last is not None:
        ln = f", line {line:g}" if line is not None else ""
        if line is not None and ypg_last - line >= 15:
            out["evidence"].append(
                f"🏈 {player.split()[-1]}: {ypg_last:.0f} rush yds/game over last {len(last)}"
                f"{ln} (+{ypg_last - line:.0f} cushion)"
            )
        elif line is not None and line - ypg_last >= 15:
            out["concerns"].append(
                f"📉 Last {len(last)}: {ypg_last:.0f} rush yds/game — line {line:g}"
                f" requires above-trend output"
            )
        else:
            out["evidence"].append(
                f"🏈 {ypg_last:.0f} rush yds/game recent{ln}"
            )
    if car_last is not None:
        if car_last >= 18:
            out["evidence"].append(f"🐘 Bell-cow workload: {car_last:.0f} carries/game")
        elif car_last <= 8:
            out["concerns"].append(f"⚠️ Only {car_last:.0f} carries/game — limited volume")
    avg = _to_float(stats.get("AVG"))
    if avg is not None:
        if avg >= 4.8:
            out["evidence"].append(f"🚀 {avg:.1f} YPC on the season — explosive")
        elif avg <= 3.5:
            out["concerns"].append(f"🐢 Only {avg:.1f} YPC on the season")
    return out


def _rationale_receiving_yards(stats: dict, last: list[dict], line: Optional[float],
                               player: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    ypg_last = None
    rec_last = None
    tgt_last = None
    if last:
        yv = [_to_float(r.get("nfl_yds")) for r in last]
        yv = [v for v in yv if v is not None]
        rv = [_to_float(r.get("nfl_rec")) for r in last]
        rv = [v for v in rv if v is not None]
        tv = [_to_float(r.get("nfl_tgts")) for r in last]
        tv = [v for v in tv if v is not None]
        if yv: ypg_last = sum(yv) / len(yv)
        if rv: rec_last = sum(rv) / len(rv)
        if tv: tgt_last = sum(tv) / len(tv)
    if ypg_last is not None:
        ln = f", line {line:g}" if line is not None else ""
        if line is not None and ypg_last - line >= 15:
            out["evidence"].append(
                f"🏈 {player.split()[-1]}: {ypg_last:.0f} rec yds/game over last {len(last)}"
                f"{ln} (+{ypg_last - line:.0f} cushion)"
            )
        elif line is not None and line - ypg_last >= 15:
            out["concerns"].append(
                f"📉 Last {len(last)}: {ypg_last:.0f} rec yds/game — line {line:g}"
                f" requires above-trend output"
            )
        else:
            out["evidence"].append(
                f"🏈 {ypg_last:.0f} rec yds/game recent{ln}"
            )
    if tgt_last is not None and tgt_last >= 8:
        out["evidence"].append(f"🎯 Heavy target share: {tgt_last:.0f} targets/game")
    elif tgt_last is not None and tgt_last <= 3:
        out["concerns"].append(f"⚠️ Only {tgt_last:.0f} targets/game — low volume")
    if rec_last is not None and rec_last >= 6:
        out["evidence"].append(f"📈 {rec_last:.0f} receptions/game recent form")
    return out


def _rationale_receptions(stats: dict, last: list[dict], line: Optional[float],
                          player: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    rec_last = None
    tgt_last = None
    if last:
        rv = [_to_float(r.get("nfl_rec")) for r in last]
        rv = [v for v in rv if v is not None]
        tv = [_to_float(r.get("nfl_tgts")) for r in last]
        tv = [v for v in tv if v is not None]
        if rv: rec_last = sum(rv) / len(rv)
        if tv: tgt_last = sum(tv) / len(tv)
    if rec_last is not None:
        ln = f", line {line:g}" if line is not None else ""
        if line is not None and rec_last - line >= 1.5:
            out["evidence"].append(
                f"📈 {player.split()[-1]}: {rec_last:.1f} rec/game recent"
                f"{ln} (+{rec_last - line:.1f} cushion)"
            )
        elif line is not None and line - rec_last >= 1.5:
            out["concerns"].append(
                f"📉 Only {rec_last:.1f} rec/game recent — line {line:g}"
                f" requires above-trend output"
            )
        else:
            out["evidence"].append(f"🎯 {rec_last:.1f} rec/game recent{ln}")
    if tgt_last is not None:
        catch_rate = (rec_last / tgt_last * 100) if (rec_last and tgt_last) else None
        if catch_rate is not None and catch_rate >= 75:
            out["evidence"].append(f"🥅 {catch_rate:.0f}% catch rate recent — reliable target")
        elif catch_rate is not None and catch_rate <= 55:
            out["concerns"].append(f"🕳️ Only {catch_rate:.0f}% catch rate recent")
    return out


def _rationale_anytime_td(stats: dict, last: list[dict], player: str,
                          role: str) -> dict[str, list[str]]:
    """ATD works for RBs (rushing block) and WRs/TEs (receiving block).
    We already know `role` from which block we pulled."""
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    season_td = _to_float(stats.get("TD"))
    tds_last = None
    if last:
        tv = [_to_float(r.get("nfl_td")) for r in last]
        tv = [v for v in tv if v is not None]
        if tv: tds_last = sum(tv)
    if season_td is not None and season_td >= 8:
        out["evidence"].append(f"🎯 {int(season_td)} TDs on the season — proven scorer")
    elif season_td is not None and season_td <= 2:
        out["concerns"].append(f"🚧 Only {int(season_td)} TDs on the season")
    if tds_last is not None and last:
        if tds_last >= 3:
            out["evidence"].append(f"🔥 {int(tds_last)} TDs in last {len(last)} games — hot")
        elif tds_last == 0:
            out["concerns"].append(f"❄️ 0 TDs in last {len(last)} games")
    role_str = {"rushing": "RB", "receiving": "WR/TE"}.get(role, role.upper())
    out["evidence"].append(f"🏈 Rated as {role_str} for ATD equity")
    return out


# ─────────────────────────── Main dispatcher ───────────────────────────
async def build_nfl_rationale(
    db, pick: dict, player_name: Optional[str],
) -> dict[str, list[str]]:
    """Returns {evidence, concerns} bullets for NFL props. Best-effort:
    empty on missing data so the caller can fall back to the universal
    model/edge framing."""
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    if not player_name:
        return out
    market = pick.get("market") or ""
    line = _extract_line(market)

    # Route by market family. Note that ATD is checked BEFORE the
    # TDs-per-block builders so we don't double-fire.
    if _match_any(market, _ANY_TD):
        # Try rushing first, fall back to receiving
        st = await _fetch_season_stats(db, player_name, "rushing")
        block_used = "rushing"
        if not st or _to_float(st.get("CAR") or 0) == 0:
            st = await _fetch_season_stats(db, player_name, "receiving") or st
            block_used = "receiving" if st else block_used
        if not st:
            return out
        last = await _fetch_last_games(db, player_name, block_used, limit=5)
        r = _rationale_anytime_td(st, last, player_name, block_used)
        out["evidence"].extend(r["evidence"])
        out["concerns"].extend(r["concerns"])
        return out

    # Passing family
    if _match_any(market, _PASS_YDS + _PASS_TDS + _PASS_CMP + _PASS_ATT + _PASS_INT):
        st = await _fetch_season_stats(db, player_name, "passing")
        if not st:
            return out
        last = await _fetch_last_games(db, player_name, "passing", limit=5)
        if _match_any(market, _PASS_YDS):
            r = _rationale_passing_yards(st, last, line, player_name)
        elif _match_any(market, _PASS_TDS):
            season_td = _to_float(st.get("TD"))
            r = {"evidence": [], "concerns": []}
            if season_td is not None:
                gp_est = len(last) if last else 1
                td_pg = sum(_to_float(l.get("nfl_td")) or 0 for l in last) / gp_est if last else None
                if td_pg is not None and line is not None:
                    if td_pg - line >= 0.5:
                        r["evidence"].append(
                            f"🎯 {player_name.split()[-1]}: {td_pg:.1f} pass TDs/game recent"
                            f" — line {line:g} (+{td_pg - line:.1f} cushion)"
                        )
                    elif line - td_pg >= 0.5:
                        r["concerns"].append(
                            f"📉 Only {td_pg:.1f} pass TDs/game recent — line {line:g}"
                        )
                    else:
                        r["evidence"].append(f"🎯 {td_pg:.1f} pass TDs/game recent")
                elif season_td >= 20:
                    r["evidence"].append(f"⭐ {int(season_td)} pass TDs on the season")
        elif _match_any(market, _PASS_CMP):
            cmp_last = [_to_float(l.get("nfl_c/att", "").split("/")[0] if l.get("nfl_c/att") else None) for l in last]
            cmp_last = [v for v in cmp_last if v is not None]
            r = {"evidence": [], "concerns": []}
            if cmp_last:
                avg_cmp = sum(cmp_last) / len(cmp_last)
                ln = f", line {line:g}" if line is not None else ""
                if line is not None and avg_cmp - line >= 2:
                    r["evidence"].append(f"🎯 {avg_cmp:.0f} completions/game recent{ln} — trending over")
                elif line is not None and line - avg_cmp >= 2:
                    r["concerns"].append(f"📉 {avg_cmp:.0f} completions/game recent — line {line:g}")
                else:
                    r["evidence"].append(f"🎯 {avg_cmp:.0f} completions/game recent{ln}")
        elif _match_any(market, _PASS_INT):
            int_last = [_to_float(l.get("nfl_int")) for l in last]
            int_last = [v for v in int_last if v is not None]
            r = {"evidence": [], "concerns": []}
            if int_last:
                avg = sum(int_last) / len(int_last)
                if avg <= 0.5:
                    r["evidence"].append(f"🛡️ Only {avg:.1f} INTs/game recent — clean form")
                elif avg >= 1.5:
                    r["concerns"].append(f"⚠️ {avg:.1f} INTs/game recent — turnover-prone")
        else:
            r = {"evidence": [], "concerns": []}
        out["evidence"].extend(r.get("evidence", []))
        out["concerns"].extend(r.get("concerns", []))
        return out

    # Rushing family
    if _match_any(market, _RUSH_YDS + _RUSH_ATT + _RUSH_TDS):
        st = await _fetch_season_stats(db, player_name, "rushing")
        if not st:
            return out
        last = await _fetch_last_games(db, player_name, "rushing", limit=5)
        if _match_any(market, _RUSH_YDS):
            r = _rationale_rushing_yards(st, last, line, player_name)
        elif _match_any(market, _RUSH_ATT):
            car_last = [_to_float(l.get("nfl_car")) for l in last]
            car_last = [v for v in car_last if v is not None]
            r = {"evidence": [], "concerns": []}
            if car_last:
                avg = sum(car_last) / len(car_last)
                ln = f", line {line:g}" if line is not None else ""
                if line is not None and avg - line >= 2:
                    r["evidence"].append(f"🏈 {avg:.0f} carries/game recent{ln} — heavy usage")
                elif line is not None and line - avg >= 2:
                    r["concerns"].append(f"📉 Only {avg:.0f} carries/game recent — line {line:g}")
                else:
                    r["evidence"].append(f"🏈 {avg:.0f} carries/game recent{ln}")
        elif _match_any(market, _RUSH_TDS):
            td_last = [_to_float(l.get("nfl_td")) for l in last]
            td_last = [v for v in td_last if v is not None]
            r = {"evidence": [], "concerns": []}
            if td_last and sum(td_last) >= 3:
                r["evidence"].append(f"🎯 {int(sum(td_last))} rush TDs in last {len(last)} — red-zone role")
            elif td_last and sum(td_last) == 0:
                r["concerns"].append(f"❄️ 0 rush TDs in last {len(last)} games")
        else:
            r = {"evidence": [], "concerns": []}
        out["evidence"].extend(r.get("evidence", []))
        out["concerns"].extend(r.get("concerns", []))
        return out

    # Receiving family
    if _match_any(market, _REC_YDS + _RECEPTIONS + _REC_TDS):
        st = await _fetch_season_stats(db, player_name, "receiving")
        if not st:
            return out
        last = await _fetch_last_games(db, player_name, "receiving", limit=5)
        if _match_any(market, _RECEPTIONS):
            r = _rationale_receptions(st, last, line, player_name)
        elif _match_any(market, _REC_YDS):
            r = _rationale_receiving_yards(st, last, line, player_name)
        elif _match_any(market, _REC_TDS):
            td_last = [_to_float(l.get("nfl_td")) for l in last]
            td_last = [v for v in td_last if v is not None]
            r = {"evidence": [], "concerns": []}
            if td_last and sum(td_last) >= 2:
                r["evidence"].append(f"🎯 {int(sum(td_last))} rec TDs in last {len(last)} — red-zone role")
            elif td_last and sum(td_last) == 0:
                r["concerns"].append(f"❄️ 0 rec TDs in last {len(last)} games")
        else:
            r = {"evidence": [], "concerns": []}
        out["evidence"].extend(r.get("evidence", []))
        out["concerns"].extend(r.get("concerns", []))
        return out

    return out
