"""Phase 4D — NBA Prop Feature Engine.

Uses the existing NBA gamelog ingest (``services.nba_gamelog_ingest``)
which persists per-player game logs under
``db.player_game_logs``  with ``sport="nba"``.  Rows carry:

  player_id, player_name, season, date, opp, is_home, minutes,
  points, rebounds, assists, steals, blocks, threes_made, threes_att,
  fgm, fga, ftm, fta, plus_minus, usage, pace, rest_days, ...

The engine returns a factors dict (0-1 scaled) + a list of source
tags so `_props_picks_from_event` can drop through to
`has_enough_real_data_nba`.

**Design invariants** (matches NFL feature engine style):
  • Deterministic.  No RNG.  Cache-friendly.
  • Min 3 real factors required (`has_enough_real_data_nba`).
  • Missing data → None (never a fake value); caller drops the pick.
  • Every factor is 0-1 scaled where 0.5 = neutral.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.nba_feature_engine")

MIN_FACTORS_NBA_PROP = 3


# ═══════════════════════════════════════════════════════════════════
# Factor helpers
# ═══════════════════════════════════════════════════════════════════
def _scale(value: float, low: float, high: float,
            *, invert: bool = False) -> float:
    if value is None:
        return 0.5
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.5
    if high == low:
        return 0.5
    x = (v - low) / (high - low)
    x = max(0.0, min(1.0, x))
    return 1.0 - x if invert else x


def has_enough_real_data_nba(factors: dict) -> bool:
    """Gate — must have at least ``MIN_FACTORS_NBA_PROP`` non-None factors."""
    return sum(1 for v in factors.values() if v is not None) >= MIN_FACTORS_NBA_PROP


# ═══════════════════════════════════════════════════════════════════
# Per-market stat mapping
# ═══════════════════════════════════════════════════════════════════
# Maps Odds API market_key → gamelog stat column + display name.
# ── 2026-10-20 · STEP B · FULL NBA MARKET COVERAGE EXPANSION ────────
# Any Odds API market key listed here is auto-processed by the
# precompute → factor-attach → sim → publish pipeline as long as
# ``canonical_player_game_actuals`` (via
# ``canonical_history_resolver.read_player_recent``) carries the
# referenced stat column(s).  Markets whose underlying stat column
# is NOT yet surfaced by the actuals (e.g. ``threes_made``) remain
# wired but will skip at the evidence gate (``has_enough_real_data_nba``)
# until the ingest adds those columns — zero code change needed then.
# Composite families (``pra``/``pr``/``pa``/``ra``/``bs``) are summed
# from their atomic components in ``_stat_value`` below.
_MARKET_STAT_MAP = {
    # ── Core singles ─────────────────────────────────────────────
    "player_points":                     ("points",      "PTS"),
    "player_rebounds":                   ("rebounds",    "REB"),
    "player_assists":                    ("assists",     "AST"),
    "player_threes":                     ("threes_made", "3PM"),
    "player_steals":                     ("steals",      "STL"),
    "player_blocks":                     ("blocks",      "BLK"),
    "player_turnovers":                  ("turnovers",   "TOV"),
    # ── Combos / composites ──────────────────────────────────────
    "player_points_rebounds_assists":    ("pra",         "PRA"),
    "player_points_rebounds":            ("pr",          "PTS+REB"),
    "player_points_assists":             ("pa",          "PTS+AST"),
    "player_rebounds_assists":           ("ra",          "REB+AST"),
    "player_blocks_steals":              ("bs",          "BLK+STL"),
    # ── Alternate-line variants (same stat column) ───────────────
    "player_points_alternate":           ("points",      "PTS"),
    "player_rebounds_alternate":         ("rebounds",    "REB"),
    "player_assists_alternate":          ("assists",     "AST"),
    "player_threes_alternate":           ("threes_made", "3PM"),
    "player_steals_alternate":           ("steals",      "STL"),
    "player_blocks_alternate":           ("blocks",      "BLK"),
    "player_turnovers_alternate":        ("turnovers",   "TOV"),
    "player_points_rebounds_assists_alternate": ("pra", "PRA"),
    "player_points_rebounds_alternate":  ("pr",          "PTS+REB"),
    "player_points_assists_alternate":   ("pa",          "PTS+AST"),
    "player_rebounds_assists_alternate": ("ra",          "REB+AST"),
    "player_blocks_steals_alternate":    ("bs",          "BLK+STL"),
    # ── Milestone (binary) markets ───────────────────────────────
    # ``dd_count``/``td_count`` are COMPUTED pseudo-stats representing
    # whether the row hit DD / TD respectively (0.0 or 1.0).  The
    # L10 hit-rate factor therefore turns into a direct empirical
    # probability of hitting that milestone, which is the correct
    # model for binary / event markets.  ``line=0.5`` is injected
    # by the extractor so the factor framework scores "Yes" (>0.5)
    # vs "No" (<0.5) consistently.
    "player_double_double":              ("dd_count",    "DD"),
    "player_triple_double":              ("td_count",    "TD"),
}


def _stat_value(row: dict, stat_key: str) -> Optional[float]:
    """Compute the stat value for a row.  Handles composite stats
    (PRA / PR / PA / RA) by summing components."""
    if stat_key == "pra":
        p = row.get("points"); r = row.get("rebounds"); a = row.get("assists")
        if p is None or r is None or a is None:
            return None
        return float(p) + float(r) + float(a)
    if stat_key == "pr":
        p = row.get("points"); r = row.get("rebounds")
        if p is None or r is None:
            return None
        return float(p) + float(r)
    if stat_key == "pa":
        p = row.get("points"); a = row.get("assists")
        if p is None or a is None:
            return None
        return float(p) + float(a)
    if stat_key == "ra":
        r = row.get("rebounds"); a = row.get("assists")
        if r is None or a is None:
            return None
        return float(r) + float(a)
    if stat_key == "bs":
        b = row.get("blocks"); s = row.get("steals")
        if b is None or s is None:
            return None
        return float(b) + float(s)
    if stat_key in ("dd_count", "td_count"):
        # Double-double / triple-double: count the standard five
        # categories (PTS, REB, AST, STL, BLK) that reach the 10+
        # threshold in this row.  dd_count returns 1.0 iff >=2
        # categories hit 10+; td_count iff >=3.  Returns None when
        # ANY source stat is missing so the row isn't scored on
        # partial evidence.
        _p = row.get("points");   _r = row.get("rebounds")
        _a = row.get("assists");  _s = row.get("steals")
        _b = row.get("blocks")
        if any(x is None for x in (_p, _r, _a, _s, _b)):
            return None
        _cnt = sum(1 for x in (_p, _r, _a, _s, _b) if float(x) >= 10.0)
        if stat_key == "dd_count":
            return 1.0 if _cnt >= 2 else 0.0
        return 1.0 if _cnt >= 3 else 0.0
    v = row.get(stat_key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════
# DB accessors
# ═══════════════════════════════════════════════════════════════════
async def _player_recent_logs(db, player_name: str, *,
                                 limit: int = 15) -> list[dict]:
    """Recent gamelog rows for a player (most recent first).

    2026-06 Universal canonical history wiring: canonical
    ``player_game_actuals`` is consulted first; legacy
    ``player_game_logs`` remains the fail-closed fallback.

    2026-10-20 · STEP B threes_made BACKFILL
    ────────────────────────────────────────
    Canonical ``player_game_actuals`` for NBA currently stores only
    ``{points, rebounds, assists, steals, blocks, turnovers}`` inside
    its ``actuals`` sub-dict — ``threes_made`` is NOT ingested yet.
    That starved every ``player_threes[_alternate]`` prop at the
    evidence gate (``has_enough_real_data_nba``) because every factor
    returned None.  Fail-closed fallback: when the canonical rows are
    missing ``threes_made``, enrich them from the legacy
    ``player_game_logs`` collection (which DOES carry ``threes_made``
    per the NBA gamelog ingest) by date-matching on the same player.
    """
    if not player_name:
        return []
    # Canonical first via the shared resolver (zero formula change).
    resolved: list[dict] = []
    try:
        from services.canonical_history_resolver import read_player_recent
        resolved = await read_player_recent(
            db, sport="nba", player_name=player_name, limit=limit) or []
    except Exception:
        resolved = []
    if resolved:
        # ── threes_made backfill (missing-column safe fallback) ─────
        try:
            # Only run backfill when at least one canonical row is
            # missing ``threes_made`` — the common case right now.
            if any(r.get("threes_made") is None for r in resolved):
                q = {"sport": "nba",
                     "player_name": {"$regex": f"^{player_name}$",
                                     "$options": "i"}}
                legacy_cursor = db.player_game_logs.find(
                    q, {"_id": 0, "date": 1, "threes_made": 1,
                        "threes_attempted": 1}
                ).sort("date", -1).limit(max(limit * 2, 30))
                legacy_map: dict[str, dict] = {}
                async for lrow in legacy_cursor:
                    _d = lrow.get("date")
                    if _d is not None:
                        legacy_map[str(_d)[:10]] = lrow
                for row in resolved:
                    if row.get("threes_made") is not None:
                        continue
                    _date = (row.get("game_date") or row.get("date")
                             or row.get("event_time"))
                    if _date is None:
                        continue
                    _key = str(_date)[:10]
                    lrow = legacy_map.get(_key)
                    if lrow is None:
                        continue
                    _tm = lrow.get("threes_made")
                    if _tm is not None:
                        row["threes_made"] = _tm
                    _ta = lrow.get("threes_attempted")
                    if _ta is not None and row.get("threes_attempted") is None:
                        row["threes_attempted"] = _ta
        except Exception:
            # Backfill failures must NEVER break the feature path —
            # just leave threes_made=None (pick drops at evidence gate).
            pass
        return resolved
    # Legacy fallback (no canonical rows at all).
    q = {"sport": "nba", "player_name": {"$regex": f"^{player_name}$",
                                            "$options": "i"}}
    cursor = db.player_game_logs.find(q, {"_id": 0}).sort("date", -1).limit(limit)
    return await cursor.to_list(limit)


# ═══════════════════════════════════════════════════════════════════
# Factor builders
# ═══════════════════════════════════════════════════════════════════
def _factor_rolling_avg_vs_line(rows: list[dict], stat: str,
                                  line: float, side: str) -> Optional[float]:
    """L10 average vs the line — over side scores high when avg > line."""
    if not rows:
        return None
    vals = [_stat_value(r, stat) for r in rows[:10]]
    vals = [v for v in vals if v is not None]
    if len(vals) < 3:
        return None
    avg = sum(vals) / len(vals)
    # Sensitivity band: ±30 % around the line.
    band = max(0.5, line * 0.30)
    x = (avg - line) / band                # positive means over, negative under
    x = max(-1.5, min(1.5, x))
    x01 = (x + 1.5) / 3.0                  # to 0..1
    return x01 if side.lower() == "over" else 1.0 - x01


def _factor_hit_rate(rows: list[dict], stat: str,
                       line: float, side: str) -> Optional[float]:
    """Hit rate on the exact line over the last 10 games."""
    if not rows:
        return None
    hits = 0
    total = 0
    for r in rows[:10]:
        v = _stat_value(r, stat)
        if v is None:
            continue
        total += 1
        if side.lower() == "over":
            if v > line: hits += 1
        else:
            if v < line: hits += 1
    if total < 3:
        return None
    return hits / total


def _factor_minutes_stability(rows: list[dict]) -> Optional[float]:
    """High score when minutes are consistent (low variance)."""
    if not rows:
        return None
    mins = [r.get("minutes") for r in rows[:10]]
    mins = [float(m) for m in mins if isinstance(m, (int, float))]
    if len(mins) < 3:
        return None
    avg = sum(mins) / len(mins)
    if avg < 15:
        return 0.15         # bench player — low confidence
    var = sum((m - avg) ** 2 for m in mins) / len(mins)
    sd = var ** 0.5
    # High minutes + low sd → high stability.
    stability = _scale(avg, 20, 40) * (1.0 - min(1.0, sd / 12.0))
    return max(0.0, min(1.0, stability))


def _factor_usage(rows: list[dict]) -> Optional[float]:
    """Average usage rate over recent games (0-1 scaled)."""
    if not rows:
        return None
    usage = [r.get("usage") for r in rows[:10]]
    usage = [float(u) for u in usage if isinstance(u, (int, float)) and u > 0]
    if len(usage) < 2:
        return None
    avg = sum(usage) / len(usage)
    # Usage 15 % = low; 25 % = average; 35 % = star.
    return _scale(avg, 15, 35)


def _factor_pace(rows: list[dict]) -> Optional[float]:
    """Higher pace → more possessions → more counting stats."""
    if not rows:
        return None
    pace = [r.get("pace") for r in rows[:10]]
    pace = [float(p) for p in pace if isinstance(p, (int, float)) and p > 0]
    if len(pace) < 2:
        return None
    return _scale(sum(pace) / len(pace), 95, 105)


def _factor_rest(rows: list[dict]) -> Optional[float]:
    """Rest days ≥ 1 → 0.55 (positive).  Back-to-back → 0.30."""
    if not rows:
        return None
    latest = rows[0]
    rd = latest.get("rest_days")
    if rd is None:
        return None
    try:
        r = int(rd)
    except (TypeError, ValueError):
        return None
    if r == 0:  return 0.30        # back-to-back → fatigue
    if r == 1:  return 0.55        # normal rest
    if r == 2:  return 0.60        # good rest
    if r >= 3:  return 0.65        # extended rest
    return 0.5


def _factor_l3_trend(rows: list[dict], stat: str,
                       side: str) -> Optional[float]:
    """L3 average vs L10 average — trending up = good for Over."""
    if len(rows) < 6:
        return None
    l3 = [_stat_value(r, stat) for r in rows[:3]]
    l10 = [_stat_value(r, stat) for r in rows[:10]]
    l3 = [v for v in l3 if v is not None]
    l10 = [v for v in l10 if v is not None]
    if len(l3) < 3 or len(l10) < 6:
        return None
    trend = (sum(l3) / len(l3)) / max(1.0, sum(l10) / len(l10))
    x = _scale(trend, 0.75, 1.25)
    return x if side.lower() == "over" else 1.0 - x


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════
async def build_nba_prop_factors(
    db: Any,
    *,
    player: str,
    market_key: str,
    side: str,
    line: Optional[float],
) -> tuple[dict, list[str]]:
    """Build the NBA prop factor dict + source-tag list.

    Returns ({factor_name: 0-1 value or None}, [source_tag, ...]).
    Emission gate: :func:`has_enough_real_data_nba(factors)`.
    """
    stat_info = _MARKET_STAT_MAP.get(market_key)
    if not stat_info:
        return {}, []
    stat_col, disp = stat_info

    rows = await _player_recent_logs(db, player)
    if not rows:
        return {}, []

    factors: dict[str, Optional[float]] = {}
    sources: list[str] = []

    if line is not None:
        f_avg = _factor_rolling_avg_vs_line(rows, stat_col, line, side)
        if f_avg is not None:
            factors[f"{disp} L10 vs Line"] = f_avg
            sources.append("player_game_logs.l10_avg")

        f_hit = _factor_hit_rate(rows, stat_col, line, side)
        if f_hit is not None:
            factors[f"{disp} L10 Hit Rate"] = f_hit
            sources.append("player_game_logs.hit_rate")

    f_min = _factor_minutes_stability(rows)
    if f_min is not None:
        factors["Minutes Stability"] = f_min
        sources.append("player_game_logs.minutes")

    f_usage = _factor_usage(rows)
    if f_usage is not None:
        factors["Usage Rate"] = f_usage
        sources.append("player_game_logs.usage")

    f_pace = _factor_pace(rows)
    if f_pace is not None:
        factors["Team Pace"] = f_pace
        sources.append("player_game_logs.pace")

    f_rest = _factor_rest(rows)
    if f_rest is not None:
        factors["Rest Days"] = f_rest
        sources.append("player_game_logs.rest")

    f_trend = _factor_l3_trend(rows, stat_col, side)
    if f_trend is not None:
        factors["L3 Trend"] = f_trend
        sources.append("player_game_logs.l3_trend")

    return factors, sources


__all__ = [
    "MIN_FACTORS_NBA_PROP",
    "has_enough_real_data_nba",
    "build_nba_prop_factors",
    "precompute_nba_prop_factors",
]


# ═══════════════════════════════════════════════════════════════════
# Precompute — mirrors the NFL pattern.  Call once per slate before
# emission; populates ctx["nba_precomputed"][player_lower][market_key]
# ═══════════════════════════════════════════════════════════════════
async def _lookup_nba_player_team(db: Any, player_name: str) -> dict:
    """Resolve ``{team, team_name, team_id, canonical_player_id}`` from
    ``canonical_players`` for an NBA player.  Case-insensitive against
    both uppercase ``NBA`` and lowercase ``nba`` sport values so the
    historical mismatch between ingest paths (lowercase) and
    publication paths (uppercase) does not starve identity resolution.

    Never raises; returns an empty dict when nothing resolves.
    """
    if not player_name:
        return {}
    try:
        q = {
            "sport": {"$in": ["nba", "NBA"]},
            "$or": [
                {"name":           {"$regex": f"^{player_name}$", "$options": "i"}},
                {"canonical_name": {"$regex": f"^{player_name}$", "$options": "i"}},
                {"player_name":    {"$regex": f"^{player_name}$", "$options": "i"}},
            ],
        }
        row = await db.canonical_players.find_one(q)
        if not row:
            return {}
        # Prefer canonical fields; fall back to raw aliases.
        # Canonical team ID is stored as the TEAM NAME (full display),
        # not the numeric roster-feed id, because
        # ``services.player_event_identity_gate._extract_player_team``
        # returns ``canonical_team_id`` FIRST and compares it against
        # the event's home/away participant NAMES.  A numeric id would
        # never match a team name → every pick would fail the identity
        # gate as PLAYER_EVENT_IDENTITY_MISMATCH.
        _team_name = row.get("team_name") or ""
        _team_abbrev = row.get("team") or ""
        _player_id = row.get("player_id") or row.get("canonical_player_id") or ""
        return {
            "player_team":          _team_name or _team_abbrev,
            "team_abbrev":          _team_abbrev,
            "canonical_team_id":    _team_name or _team_abbrev,
            "team_id_numeric":      str(row.get("team_id") or "") or None,
            "canonical_player_id":  str(_player_id) if _player_id else "",
        }
    except Exception as e:
        logger.debug("nba player team lookup failed for %s: %s", player_name, e)
        return {}


async def precompute_nba_prop_factors(
    db: Any,
    *,
    players: list[str],
    market_keys: list[str],
    lines_by_player_market: Optional[dict[tuple[str, str], list[tuple[float, str]]]] = None,
) -> dict:
    """Precompute NBA factor dicts for a slate.

    Parameters
    ----------
    players :
        Canonicalised player names to fetch gamelogs for.
    market_keys :
        Odds API market keys (``player_points``, ``player_threes``, ...).
    lines_by_player_market :
        Optional mapping ``(player_lower, market_key)`` → ``[(line, side), ...]``.
        When provided, the factor set includes line-sensitive factors
        (rolling avg vs line + hit rate) for each specified line.

    Returns
    -------
    dict :
        ``{"nba_precomputed": {player_lower: {market_key: {"factors": {...},
        "sources": [...], "player_team": "...", ...}}}, "nba_team_lookup":
        {player_lower: {...}}}`` — ready to be merged into ``ctx``.

    STEP A FIX (2026-10-20) — alongside the factor bundle, resolve
    each player's canonical team from ``canonical_players`` so the
    sync emission branch can stamp ``player_team`` /
    ``canonical_team_id`` / ``canonical_player_id`` onto every NBA
    pick.  This closes the ``PLAYER_TEAM_UNRESOLVED`` identity-gate
    rejection without weakening any safety guard.
    """
    out: dict = {}
    team_lookup: dict = {}
    for player in players:
        if not player:
            continue
        pl = player.strip().lower()
        # STEP A — resolve team identity once per player.
        if pl not in team_lookup:
            team_info = await _lookup_nba_player_team(db, player)
            if team_info:
                team_lookup[pl] = team_info
        rows = await _player_recent_logs(db, player)
        if not rows:
            continue
        per_player: dict = {}
        for mk in market_keys:
            if mk not in _MARKET_STAT_MAP:
                continue
            stat_col, disp = _MARKET_STAT_MAP[mk]
            # Aggregate factor bundle across every (line, side) pair for
            # this (player, market) so the sync emission path can dip in.
            lines_sides = ((lines_by_player_market or {}).get((pl, mk))
                             or [(None, "Over"), (None, "Under")])
            # ── 2026-10-20 STEP C SCORING-DEFECT FIX #2 ───────────────
            # The previous implementation iterated ``lines_sides`` and
            # ``break``-ed after the first qualifying bundle, then
            # stored that single bundle under ``per_player[mk]`` and
            # reused it for EVERY (line, side) pick on that (player,
            # market).  Picks on different lines got the FIRST line's
            # factors — a textbook stale-evidence inflation defect
            # (Khaman Maluach 4.5 Rebounds showed REB L10 Hit Rate=0.0
            # because the 7.5-line bundle was reused).  New shape:
            # ``per_player[mk]`` is a mapping of ``(line_f, side_lc)``
            # -> bundle, plus a ``__first_bundle`` fallback for callers
            # that don't carry (line, side) context.  The sync
            # emission branch picks the exact bundle by (line, side).
            per_market_variants: dict = {}
            first_bundle = None
            for (line, side) in lines_sides:
                factors: dict = {}
                sources: list[str] = []
                if line is not None:
                    f_avg = _factor_rolling_avg_vs_line(rows, stat_col, line, side)
                    if f_avg is not None:
                        factors[f"{disp} L10 vs Line"] = f_avg
                        sources.append("player_game_logs.l10_avg")
                    f_hit = _factor_hit_rate(rows, stat_col, line, side)
                    if f_hit is not None:
                        factors[f"{disp} L10 Hit Rate"] = f_hit
                        sources.append("player_game_logs.hit_rate")
                for name, val, tag in (
                    ("Minutes Stability", _factor_minutes_stability(rows),
                     "player_game_logs.minutes"),
                    ("Usage Rate",        _factor_usage(rows),
                     "player_game_logs.usage"),
                    ("Team Pace",         _factor_pace(rows),
                     "player_game_logs.pace"),
                    ("Rest Days",         _factor_rest(rows),
                     "player_game_logs.rest"),
                    ("L3 Trend",          _factor_l3_trend(rows, stat_col, side),
                     "player_game_logs.l3_trend"),
                ):
                    if val is not None:
                        factors[name] = val
                        sources.append(tag)
                if has_enough_real_data_nba(factors):
                    bundle = {
                        "factors": factors,
                        "sources": sources,
                        "recent_rows": rows[:10],
                        "line": line,
                        "side": side,
                    }
                    try:
                        _k_line = (float(line)
                                    if isinstance(line, (int, float))
                                    else None)
                    except Exception:
                        _k_line = None
                    _k_side = (str(side).strip().lower()
                                if side is not None else "")
                    per_market_variants[(_k_line, _k_side)] = bundle
                    if first_bundle is None:
                        first_bundle = bundle
            if per_market_variants:
                # Back-compat shape: callers that still read
                # ``per_player[mk]`` as a single bundle get the first
                # one (identical to the old behavior); callers that
                # read ``per_player[mk]["__variants"]`` get the full
                # per-(line, side) map.
                _bundle_out = dict(first_bundle or {})
                _bundle_out["__variants"] = per_market_variants
                per_player[mk] = _bundle_out
        if per_player:
            out[pl] = per_player
    return {"nba_precomputed": out, "nba_team_lookup": team_lookup}
