"""Team History — shared loader + window builder.

Reads normalized team-event rows from ``db.team_game_actuals`` first,
falling back to ``db.team_game_logs`` if the normalized collection is
empty for the requested team.  NEVER fabricates rows — an empty
result yields ``UNAVAILABLE`` with no side effects.

Missing scores stay ``None`` — never zero.  A REAL 0 (0-run shutout,
0-0 draw) is preserved as ``0``.
"""
from __future__ import annotations

from statistics import median as _stats_median
from typing import Optional

from .models import (
    TeamHistoryEvidence,
    TeamHistoryWindow,
    TeamHistoryQuality,
    TeamHistoryStatus,
)


# ═══════════════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════════════
async def load_team_rows(
    db,
    *,
    sport: str,
    canonical_team_id: Optional[str],
    team_name: Optional[str],
    as_of: str,
    limit: int = 200,
) -> tuple[list[dict], str]:
    """Load per-event team rows, newest-first, strictly before
    ``as_of``.  Returns (rows, source_label)."""
    sport_l = (sport or "").lower()

    # Primary collection.
    try:
        coll = db.team_game_actuals
        q: dict = {"sport": sport_l, "event_time": {"$lt": as_of}}
        if canonical_team_id:
            q["canonical_team_id"] = canonical_team_id
        elif team_name:
            # Identity confidence downgrades to LOW when we fall back
            # to display-name equality (§3) — the caller is warned via
            # ``identity_confidence`` on the resulting evidence.
            q["team_name"] = team_name
        else:
            return [], "UNAVAILABLE"
        cursor = coll.find(q, {"_id": 0}).sort("event_time", -1).limit(limit)
        rows = [d async for d in cursor]
        if rows:
            return rows, "NORMALIZED"
    except Exception:
        pass

    # Fallback to legacy collection (if any).
    try:
        legacy = db.team_game_logs
        q2: dict = {"sport": sport_l, "date": {"$lt": as_of[:10]}}
        if canonical_team_id:
            q2["canonical_team_id"] = canonical_team_id
        elif team_name:
            q2["team_name"] = team_name
        else:
            return [], "UNAVAILABLE"
        cursor = legacy.find(q2, {"_id": 0}).sort("date", -1).limit(limit)
        rows = [d async for d in cursor]
        if rows:
            return rows, "LEGACY_TEAM_LOGS"
    except Exception:
        pass

    return [], "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# Numeric helpers
# ═══════════════════════════════════════════════════════════════════
QUANTILE_MIN_SAMPLE = 3


def _linear_quantile(sorted_vals: list[float], q: float) -> Optional[float]:
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _variance(vals: list[float]) -> Optional[float]:
    n = len(vals)
    if n < 2:
        return None
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / (n - 1)


def _safe_num(v) -> Optional[float]:
    """Distinguish real 0 from missing.  Never converts None/'' to 0."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None      # bool must never masquerade as a score
    try:
        f = float(v)
        if f != f:       # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _classify_quality(sample: int) -> str:
    if sample == 0:
        return TeamHistoryQuality.UNAVAILABLE.value
    if sample < 3:
        return TeamHistoryQuality.INSUFFICIENT.value
    if sample < 8:
        return TeamHistoryQuality.LOW.value
    if sample < 15:
        return TeamHistoryQuality.MEDIUM.value
    return TeamHistoryQuality.HIGH.value


# ═══════════════════════════════════════════════════════════════════
# Window builder
# ═══════════════════════════════════════════════════════════════════
def build_window(
    label: str,
    rows: list[dict],
    *,
    events_requested: Optional[int] = None,
) -> TeamHistoryWindow:
    """Aggregate a slice of team rows into a window.

    * Rows with a missing ``team_score`` OR ``opponent_score`` are
      excluded from numeric distributions (never coerced to 0).
    * ``result`` field (WIN/LOSS/DRAW/OTL) drives win/loss counts.
      When ``result`` is absent it is INFERRED from the numeric
      scores when both are present; if either score is missing we
      cannot infer and the row is excluded from win-loss tallies.
    """
    scored: list[float] = []
    conceded: list[float] = []
    wins = losses = draws = ot_losses = 0
    seasons: set = set()
    dates: list[str] = []

    for r in rows:
        ts = _safe_num(r.get("team_score"))
        os = _safe_num(r.get("opponent_score"))
        # Numeric distributions require BOTH scores present.
        if ts is not None and os is not None:
            scored.append(ts)
            conceded.append(os)
        # Win/loss tally.
        result = (r.get("result") or "").upper()
        if not result and ts is not None and os is not None:
            result = "WIN" if ts > os else ("LOSS" if ts < os else "DRAW")
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1
        elif result == "DRAW":
            draws += 1
        elif result in ("OTL", "OT_LOSS", "SO_LOSS"):
            ot_losses += 1
            losses += 1
        # Metadata.
        s = r.get("season")
        if s is not None:
            try:
                seasons.add(int(s))
            except (TypeError, ValueError):
                pass
        d = r.get("event_time") or r.get("date")
        if isinstance(d, str):
            dates.append(d)

    sample_size = max(len(scored), len(conceded))
    win = TeamHistoryWindow(
        label=label,
        sample_size=sample_size,
        wins=wins, losses=losses, draws=draws, ot_losses=ot_losses,
        events_requested=events_requested,
    )
    if scored:
        win.scored_avg = round(sum(scored) / len(scored), 4)
        if len(scored) >= QUANTILE_MIN_SAMPLE:
            sv = sorted(scored)
            win.scored_q25 = _linear_quantile(sv, 0.25)
            win.scored_median = _stats_median(sv)
            win.scored_q75 = _linear_quantile(sv, 0.75)
            win.scored_variance = _variance(sv)
        win.scored_values = scored
    if conceded:
        win.conceded_avg = round(sum(conceded) / len(conceded), 4)
        if len(conceded) >= QUANTILE_MIN_SAMPLE:
            sv = sorted(conceded)
            win.conceded_q25 = _linear_quantile(sv, 0.25)
            win.conceded_median = _stats_median(sv)
            win.conceded_q75 = _linear_quantile(sv, 0.75)
            win.conceded_variance = _variance(sv)
        win.conceded_values = conceded
    if scored and conceded:
        win.total_avg = round(
            (sum(scored) + sum(conceded)) / (len(scored) + len(conceded)) * 2,
            4,
        )
    if dates:
        win.date_range = (min(dates), max(dates))
    win.seasons = sorted(seasons)
    return win


def _filter_home_away(rows: list[dict], home_away: str) -> list[dict]:
    ha = home_away.lower()
    return [r for r in rows
             if (r.get("home_away") or "").lower() == ha]


def _filter_competition(rows: list[dict], comp: str) -> list[dict]:
    return [r for r in rows
             if (r.get("competition") or r.get("league")) == comp]


# ═══════════════════════════════════════════════════════════════════
# Standard populate
# ═══════════════════════════════════════════════════════════════════
async def populate_team_history(
    db,
    ev: TeamHistoryEvidence,
    *,
    sport: str,
    canonical_team_id: Optional[str],
    team_name: Optional[str],
    opponent_id: Optional[str],
    home_away: Optional[str],
    competition: Optional[str],
    row_context_fn=None,
) -> TeamHistoryEvidence:
    from datetime import datetime, timezone
    as_of = ev.as_of or datetime.now(timezone.utc).isoformat()

    rows, source = await load_team_rows(
        db,
        sport=sport,
        canonical_team_id=canonical_team_id,
        team_name=team_name,
        as_of=as_of,
    )
    ev.source = source
    ev.source_timestamp = datetime.now(timezone.utc).isoformat()
    ev.events_requested = 200
    ev.events_available = len(rows)
    if not rows:
        ev.status = TeamHistoryStatus.UNAVAILABLE.value
        ev.data_quality = TeamHistoryQuality.UNAVAILABLE.value
        ev.events_used = 0
        return ev

    # Optional context filter.
    filtered = rows
    if competition:
        filtered = _filter_competition(filtered, competition)
    if home_away in ("home", "away"):
        filtered = _filter_home_away(filtered, home_away)

    # Rows with usable score data (either score present).
    def _usable(r):
        return _safe_num(r.get("team_score")) is not None \
            or _safe_num(r.get("opponent_score")) is not None
    usable = [r for r in filtered if _usable(r)]
    ev.events_used = len(usable)
    ev.missing_events = ev.events_available - ev.events_used
    ev.data_quality = _classify_quality(len(usable))

    # Windows.
    ev.last_5  = build_window("last_5",  filtered[:5],  events_requested=5).to_dict()
    ev.last_10 = build_window("last_10", filtered[:10], events_requested=10).to_dict()
    ev.last_20 = build_window("last_20", filtered[:20], events_requested=20).to_dict()

    # Season splits.
    seasons_present = sorted({
        int(r.get("season")) for r in filtered if r.get("season") is not None
    }, reverse=True) if filtered else []
    if seasons_present:
        cur = seasons_present[0]
        season_rows = [r for r in filtered if r.get("season") == cur]
        ev.season = build_window("season", season_rows,
                                    events_requested=len(season_rows)).to_dict()
        prev_rows = [r for r in filtered if r.get("season") == cur - 1]
        if prev_rows:
            ev.previous_season = build_window(
                "previous_season", prev_rows,
                events_requested=len(prev_rows)).to_dict()
        multi_rows = [r for r in filtered
                        if r.get("season") in (cur, cur - 1, cur - 2)]
        if multi_rows:
            ev.multi_season = build_window(
                "multi_season", multi_rows,
                events_requested=len(multi_rows)).to_dict()

    # Home / away perspective — always computed from ALL rows (not the
    # user-supplied filter) so the caller gets both perspectives.
    home_rows = _filter_home_away(rows, "home")
    away_rows = _filter_home_away(rows, "away")
    if home_rows:
        ev.home = build_window("home", home_rows,
                                 events_requested=len(home_rows)).to_dict()
    if away_rows:
        ev.away = build_window("away", away_rows,
                                 events_requested=len(away_rows)).to_dict()

    # Head-to-Head.
    if opponent_id:
        h2h_rows = [r for r in rows
                     if (r.get("canonical_opponent_id") == opponent_id)]
        if h2h_rows:
            from .h2h import build_h2h_result
            ev.h2h = build_h2h_result(
                canonical_team_id=canonical_team_id,
                canonical_opponent_id=opponent_id,
                rows=h2h_rows,
            ).to_dict()

    # Identity confidence.
    ev.identity_confidence = "HIGH" if canonical_team_id else "LOW"

    if row_context_fn is not None:
        try:
            extras = row_context_fn(rows) or {}
            if extras:
                ev.extras.update(extras)
        except Exception:
            pass

    return ev


__all__ = [
    "load_team_rows",
    "build_window",
    "populate_team_history",
    "_safe_num",
    "_classify_quality",
    "QUANTILE_MIN_SAMPLE",
]
