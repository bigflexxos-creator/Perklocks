"""NFL Anytime TD — Full Canonical Universe Expander.

Root defect (2026-09-14):
    ``/api/nfl/atd/leaderboard`` and ``/api/nfl/atd/by-game`` both
    scan ``db.picks`` for rows with ``atd_evidence.td_probability > 0``.
    That set only reflects games the canonical publication cycle has
    already processed.  When a provider ATD market lands mid-cycle
    (as with Denver @ Kansas City this evening: 61 real
    ``player_anytime_td`` rows in ``live_alt_lines``, 33 candidates,
    ATD engine returns valid probabilities of 0.20 – 0.48), those
    games silently disappear from BOTH the whole-slate leaderboard
    AND the by-game grouping.  The by-game grouping is not the
    defect; the *universe* it groups is.

Fix (surgical):
    Read ``live_alt_lines`` for NFL ``player_anytime_td`` rows in
    the current fresh window, group provider outcomes by
    ``event_id``, and for every event NOT already represented in
    the canonical universe, run the SAME authoritative
    ``build_nfl_game_context`` pre-loader.  The pre-loader returns
    ``nfl_atd_precomputed`` — the same engine output the canonical
    publication would have computed on its next tick — with real
    ``td_probability`` + ``confidence`` values.  Convert those into
    the ATD leaderboard record shape and merge into the universe.

Guarantees:
    * No ATD model change.  The pre-loader IS the ATD engine.
    * No fake scores.  Only players the engine returns with
      ``td_probability > 0`` are surfaced.  Rejects are dropped.
    * NFL alt props are untouched — this operates strictly on the
      ``player_anytime_td`` provider key.
    * UEA / other-sports / MLB / soccer are untouched.
    * Idempotent.  Records already present in the canonical
      universe (matched by ``canonical_player_id`` + event) are
      never duplicated — the on-demand branch is skipped entirely
      for those events.
    * Provenance is recorded: ``provenance="on_demand_atd_engine"``
      so downstream consumers can distinguish canonical publication
      rows from live-engine rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("lockscore.nfl_atd_universe")

# Provider row freshness window — the alt-lines fetcher's TTL is
# 30 min; we honour a slightly wider window (60 min) so a mid-cycle
# refresh does not blank the board for a full TTL cycle.
_LAL_FRESHNESS_MIN = 60


def _grade_from_td_prob(p: float) -> str:
    """Mirror of the leaderboard's grade banding for consistency."""
    if p >= 0.70:
        return "A+"
    if p >= 0.60:
        return "A"
    if p >= 0.52:
        return "B+"
    if p >= 0.45:
        return "B"
    if p >= 0.38:
        return "C+"
    return "C"


async def expand_atd_universe_from_live_alt_lines(
    db,
    *,
    canonical_event_ids: set[str],
    min_probability: float,
) -> list[dict]:
    """Return on-demand ATD leaderboard records for events not yet
    canonically published.

    Args:
        db: motor database.
        canonical_event_ids: set of event ids/names already covered
            by ``db.picks`` with real ``atd_evidence`` rows.  Any
            event whose id OR name matches one of these is skipped
            (no duplication).
        min_probability: floor on ``td_probability`` for inclusion —
            same semantics as the endpoint parameters.

    Returns:
        List of dicts shaped like the ``canonical`` records the ATD
        leaderboard already emits, so callers can concatenate + sort.
        Empty list on any failure (fail-open: the canonical path
        remains authoritative on its own).
    """
    try:
        from services.nfl_feature_engine import build_nfl_game_context
    except Exception as _imp_err:                                # pragma: no cover
        logger.warning("ATD universe: pre-loader import failed: %s", _imp_err)
        return []

    now = datetime.now(timezone.utc)
    fresh_after = now - timedelta(minutes=_LAL_FRESHNESS_MIN)

    # Group provider ATD rows by event.
    try:
        rows = await db.live_alt_lines.find(
            {
                "sport": "nfl",
                "market_key": "player_anytime_td",
                "$or": [
                    {"last_seen": {"$gte": fresh_after}},
                    {"last_seen": {"$gte": fresh_after.isoformat()}},
                ],
                # Event must be current/upcoming — anything already
                # started or in the past is ignored (matches the
                # freshness gate on both endpoints).
                "commence_time": {"$gte": (now - timedelta(minutes=15)).isoformat()},
            },
            {
                "_id": 0,
                "event_id": 1,
                "event_name": 1,
                "home_team": 1,
                "away_team": 1,
                "commence_time": 1,
                "selection": 1,
                "price": 1,
            },
        ).to_list(length=5000)
    except Exception as _q_err:                                  # pragma: no cover
        logger.warning("ATD universe: live_alt_lines read failed: %s", _q_err)
        return []

    if not rows:
        return []

    # Bucket by event.
    events: dict[str, dict] = {}
    for r in rows:
        eid = r.get("event_id") or r.get("event_name") or ""
        if not eid:
            continue
        e = events.setdefault(eid, {
            "event_id": eid,
            "event_name": r.get("event_name")
                          or f"{r.get('away_team')} @ {r.get('home_team')}",
            "home_team": r.get("home_team") or "",
            "away_team": r.get("away_team") or "",
            "commence_time": r.get("commence_time") or "",
            "outcomes": {},
        })
        sel = (r.get("selection") or "").strip()
        if not sel:
            continue
        # Keep the best (most negative — steepest chalk) price per
        # player so the leaderboard shows the true bettable price.
        prev = e["outcomes"].get(sel)
        try:
            price = int(r.get("price")) if r.get("price") is not None else None
        except Exception:
            price = None
        if price is None:
            e["outcomes"].setdefault(sel, None)
        elif prev is None or (isinstance(prev, int) and price < prev):
            e["outcomes"][sel] = price

    out: list[dict] = []
    for eid, e in events.items():
        # Skip events already in the canonical universe (dedupe).
        _event_name = e["event_name"]
        if eid in canonical_event_ids or _event_name in canonical_event_ids:
            continue

        outcomes = e["outcomes"] or {}
        if not outcomes:
            continue

        # Build prop_candidates payload the pre-loader expects.
        prop_candidates = [
            {
                "player": name,
                "market": "player_anytime_td",
                "line":   None,
                "side":   "Yes",
                "team":   None,          # pre-loader resolves via roster
                "position": None,
                "book_implied": None,
            }
            for name in outcomes.keys()
            # Skip synthetic D/ST or Defense entries — those are not
            # real player ATD candidates on the model.
            if "D/ST" not in name.upper() and "DEFENSE" not in name.upper()
        ]
        if not prop_candidates:
            continue

        try:
            ctx = await build_nfl_game_context(
                db,
                game={
                    "home_team": e["home_team"],
                    "away_team": e["away_team"],
                },
                prop_candidates=prop_candidates,
                season=now.year,
                # The ATD engine only uses week for surface metadata,
                # not for the probability computation — a rough
                # week-from-date is safe.  Falls back to 1 on Sep dates.
                week=max(1, ((now.month - 9) * 4 + (now.day - 1) // 7 + 1)
                         if now.month >= 9 else 1),
            )
        except Exception as _pl_err:                              # pragma: no cover
            logger.debug("ATD universe: pre-loader failed for %s: %s",
                         _event_name, _pl_err)
            continue

        atd = (ctx or {}).get("nfl_atd_precomputed") or {}
        if not atd:
            continue

        for player_lower, ev in atd.items():
            if not isinstance(ev, dict):
                continue
            if ev.get("reject"):
                continue
            try:
                td_prob = float(ev.get("td_probability") or 0.0)
            except Exception:
                td_prob = 0.0
            if td_prob < float(min_probability or 0.0):
                continue
            # Recover the canonical-case player name (outcomes keys
            # preserve the sportsbook rendering — e.g. "J.K. Dobbins").
            _canonical_name = ev.get("player_name") or player_lower
            for _n in outcomes.keys():
                if _n.strip().lower() == player_lower:
                    _canonical_name = _n
                    break
            _team = ev.get("team") or ""
            _opp = ev.get("opponent") or ""
            _book_odds = outcomes.get(_canonical_name)
            _implied = None
            try:
                if isinstance(_book_odds, int):
                    if _book_odds < 0:
                        _implied = round(
                            (-_book_odds) / ((-_book_odds) + 100.0), 4)
                    else:
                        _implied = round(100.0 / (_book_odds + 100.0), 4)
            except Exception:
                _implied = None
            out.append({
                "player_id":        ev.get("canonical_player_id") or "",
                "player_name":      _canonical_name,
                "team":             _team,
                "opponent":         _opp,
                "td_probability":   round(td_prob, 4),
                "confidence":       float(ev.get("confidence") or 0.0),
                "opportunity_rating": ev.get("opportunity_rating") or "med",
                "weighted_touches_recent": float(ev.get("weighted_touches_recent") or 0.0),
                "weighted_tds_recent":     float(ev.get("weighted_tds_recent") or 0.0),
                "team_td_rate":            float(ev.get("team_td_rate") or 0.0),
                "matchup_factor":          float(ev.get("matchup_factor") or 1.0),
                "game_script_factor":      float(ev.get("game_script_factor") or 1.0),
                "is_rb_archetype":         bool(ev.get("is_rb_archetype")),
                "sample_games":            int(ev.get("sample_games") or 0),
                "reasons":                 list(ev.get("reasons") or []),
                # Betting provenance — on-demand rows do not have a
                # persisted ``pick_id``; downstream consumers should
                # treat missing ``pick_id`` as a live-engine row.
                "pick_id":           None,
                "book_odds":         _book_odds,
                "implied_probability": _implied,
                "edge_percent":      None,
                "lock_score":        None,
                "event":             _event_name,
                "event_time":        e["commence_time"],
                "market":            f"{_canonical_name} Anytime TD",
                "publication_state": "LIVE_ENGINE",
                "provenance":        "on_demand_atd_engine",
                # Extra fields for by-game grouping — the canonical
                # path emits these too so shapes reconcile.
                "canonical_event_id": eid,
                "home_team":         e["home_team"],
                "away_team":         e["away_team"],
            })

    if out:
        logger.info(
            "ATD universe expander: emitted %d on-demand candidates across "
            "%d events not yet canonically published",
            len(out),
            len({r["event"] for r in out}),
        )
    return out
