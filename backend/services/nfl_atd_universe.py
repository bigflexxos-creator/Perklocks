"""NFL Anytime TD — Full Canonical Universe Expander (per-player merge).

Root defect closure (2026-09-14 · rev 2):
    First revision gated on-demand ATD scoring at the *event* level:
    if any canonical row existed for an event, the whole event was
    treated as "covered" and provider players missing a canonical
    row were silently dropped.  That reproduced the truncation for
    events like DET @ BUF (only Gibbs canonically published; other
    provider players like Amon-Ra St. Brown, David Montgomery,
    James Cook were still filtered out).

Rev 2 fix — per-player merge:
    For EVERY current-slate event with fresh provider
    ``player_anytime_td`` rows:

      1. read the set of canonical player_ids already published for
         that event (from the pre-loaded canonical roster the caller
         passes in);
      2. compute the provider players *missing* from that set;
      3. run the SAME authoritative ATD pre-loader
         (``build_nfl_game_context``) for the missing set only;
      4. emit those engine rows tagged
         ``provenance="on_demand_atd_engine"``.

    Dedupe keys downstream:
       (canonical_event_id, canonical_player_id) → canonical wins.
       Fall-back: (canonical_event_id, player_name_lower).

Guarantees:
    * ATD model unchanged.
    * NFL alt props / UEA / other sports untouched.
    * No duplicate player cards (dedupe helper below).
    * No fake scores.  Engine ``reject`` rows are dropped.
    * Provider rows below ``min_probability`` after modelling are
      dropped honestly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger("lockscore.nfl_atd_universe")

# Provider-row freshness window.  ``alt_lines_feed`` TTL is 30 min;
# we honour 60 min so a mid-cycle refresh does not blank an event
# for a full TTL window.
_LAL_FRESHNESS_MIN = 60


def _norm_name(s: Any) -> str:
    return (str(s or "")).strip().lower()


async def expand_atd_universe_from_live_alt_lines(
    db,
    *,
    canonical_by_event: dict[str, dict[str, Any]],
    min_probability: float,
) -> list[dict]:
    """Return on-demand ATD leaderboard rows for provider players
    missing from the canonical universe (per-player merge).

    Args:
        db: motor database.
        canonical_by_event: map of event key → coverage payload:

            {
              "<event_name or event_id>": {
                 "player_ids":    set[str]  # canonical player IDs
                 "player_names":  set[str]  # lower-cased canonical names
              },
              ...
            }

            The caller populates this from its canonical `db.picks`
            scan so we never double-emit a player that is already
            published under a canonical row.
        min_probability: floor on ``td_probability`` for inclusion —
            same semantics as the endpoint parameters.

    Returns:
        List of leaderboard-shaped dicts for players NOT already
        covered canonically for their event.  Empty on any failure
        (fail-open — canonical path stays authoritative).
    """
    try:
        from services.nfl_feature_engine import build_nfl_game_context
    except Exception as _imp_err:                                # pragma: no cover
        logger.warning("ATD universe: pre-loader import failed: %s", _imp_err)
        return []

    now = datetime.now(timezone.utc)
    fresh_after = now - timedelta(minutes=_LAL_FRESHNESS_MIN)

    # Read fresh provider rows.
    try:
        rows = await db.live_alt_lines.find(
            {
                "sport": "nfl",
                "market_key": "player_anytime_td",
                "$or": [
                    {"last_seen": {"$gte": fresh_after}},
                    {"last_seen": {"$gte": fresh_after.isoformat()}},
                ],
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
            "event_id":       eid,
            "event_name":     r.get("event_name")
                              or f"{r.get('away_team')} @ {r.get('home_team')}",
            "home_team":      r.get("home_team") or "",
            "away_team":      r.get("away_team") or "",
            "commence_time":  r.get("commence_time") or "",
            "outcomes":       {},   # {display_name: best_price}
        })
        sel = (r.get("selection") or "").strip()
        if not sel:
            continue
        try:
            price = int(r.get("price")) if r.get("price") is not None else None
        except Exception:
            price = None
        prev = e["outcomes"].get(sel)
        if price is None:
            e["outcomes"].setdefault(sel, None)
        elif prev is None or (isinstance(prev, int) and price < prev):
            e["outcomes"][sel] = price

    # Compact lookup for canonical coverage that accepts either
    # event_id, event_name, or either home@away shape.
    def _canonical_cov_for(e: dict) -> dict:
        for key in (
            e["event_id"],
            e["event_name"],
            f"{e['away_team']} @ {e['home_team']}",
            f"{e['home_team']} @ {e['away_team']}",
        ):
            if key and key in canonical_by_event:
                return canonical_by_event[key]
        return {"player_ids": set(), "player_names": set()}

    out: list[dict] = []
    for eid, e in events.items():
        outcomes = e["outcomes"] or {}
        if not outcomes:
            continue

        cov = _canonical_cov_for(e)
        covered_names: set[str] = {_norm_name(n) for n in cov.get("player_names", set())}

        # PER-PLAYER filter: keep only outcomes whose lower-cased name
        # is NOT already canonically covered.  Also drop synthetic
        # defense entries — ATD engine is not designed to score them.
        missing_outcomes: dict[str, Any] = {}
        for name, price in outcomes.items():
            if "D/ST" in name.upper() or "DEFENSE" in name.upper():
                continue
            if _norm_name(name) in covered_names:
                continue
            missing_outcomes[name] = price

        if not missing_outcomes:
            continue

        prop_candidates = [
            {
                "player":       name,
                "market":       "player_anytime_td",
                "line":         None,
                "side":         "Yes",
                "team":         None,
                "position":     None,
                "book_implied": None,
            }
            for name in missing_outcomes.keys()
        ]

        try:
            ctx = await build_nfl_game_context(
                db,
                game={
                    "home_team": e["home_team"],
                    "away_team": e["away_team"],
                },
                prop_candidates=prop_candidates,
                season=now.year,
                week=max(
                    1,
                    ((now.month - 9) * 4 + (now.day - 1) // 7 + 1)
                    if now.month >= 9 else 1,
                ),
            )
        except Exception as _pl_err:                              # pragma: no cover
            logger.debug(
                "ATD universe: pre-loader failed for %s: %s",
                e["event_name"], _pl_err,
            )
            continue

        atd = (ctx or {}).get("nfl_atd_precomputed") or {}
        if not atd:
            continue

        _cased: dict[str, str] = {_norm_name(n): n for n in missing_outcomes.keys()}

        for player_lower, ev in atd.items():
            if not isinstance(ev, dict) or ev.get("reject"):
                continue
            try:
                td_prob = float(ev.get("td_probability") or 0.0)
            except Exception:
                td_prob = 0.0
            if td_prob < float(min_probability or 0.0):
                continue
            _display = _cased.get(player_lower) or ev.get("player_name") or player_lower
            _book_odds = missing_outcomes.get(_display)
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
                "player_id":              ev.get("canonical_player_id") or "",
                "player_name":            _display,
                "team":                   ev.get("team") or "",
                "opponent":               ev.get("opponent") or "",
                "td_probability":         round(td_prob, 4),
                "confidence":             float(ev.get("confidence") or 0.0),
                "opportunity_rating":     ev.get("opportunity_rating") or "med",
                "weighted_touches_recent": float(ev.get("weighted_touches_recent") or 0.0),
                "weighted_tds_recent":     float(ev.get("weighted_tds_recent") or 0.0),
                "team_td_rate":            float(ev.get("team_td_rate") or 0.0),
                "matchup_factor":          float(ev.get("matchup_factor") or 1.0),
                "game_script_factor":      float(ev.get("game_script_factor") or 1.0),
                "is_rb_archetype":         bool(ev.get("is_rb_archetype")),
                "sample_games":            int(ev.get("sample_games") or 0),
                "reasons":                 list(ev.get("reasons") or []),
                "pick_id":                 None,
                "book_odds":               _book_odds,
                "implied_probability":     _implied,
                "edge_percent":            None,
                "lock_score":              None,
                "event":                   e["event_name"],
                "event_time":              e["commence_time"],
                "market":                  f"{_display} Anytime TD",
                "publication_state":       "LIVE_ENGINE",
                "provenance":              "on_demand_atd_engine",
                "canonical_event_id":      eid,
                "home_team":               e["home_team"],
                "away_team":               e["away_team"],
            })

    if out:
        _events_touched = {r["event"] for r in out}
        logger.info(
            "ATD universe expander (rev 2 per-player): emitted %d on-demand "
            "candidates across %d events",
            len(out), len(_events_touched),
        )
    return out


def dedupe_atd_candidates(
    canonical: Iterable[dict],
    on_demand: Iterable[dict],
) -> list[dict]:
    """Merge canonical + on-demand rows deduping by
    ``(canonical_event_id, canonical_player_id)`` and falling back to
    ``(event, player_name_lower)``.  Canonical rows always win a
    collision.
    """
    def _key(r: dict) -> tuple[str, str]:
        pid = (r.get("player_id") or r.get("canonical_player_id") or "").strip()
        event = (r.get("event") or r.get("canonical_event_id") or "").strip()
        if pid:
            return ("id:" + pid, event)
        return ("name:" + _norm_name(r.get("player_name")), event)

    seen: dict[tuple[str, str], dict] = {}
    for c in canonical or []:
        seen[_key(c)] = c
    for c in on_demand or []:
        k = _key(c)
        if k in seen:
            continue
        seen[k] = c
    return list(seen.values())
