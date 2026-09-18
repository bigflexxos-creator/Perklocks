"""P4 — ONE NFL ATD slate truth.

`build_atd_universe` constructs the ATD candidate universe ONCE per request
from (a) canonical published ATD picks and (b) legitimate on-demand ATD
engine candidates for provider players not yet published.  Candidate state
is explicit; canonical published rows always win a dedupe collision.

`build_atd_slate` derives BOTH Top 5 and By-Game from that same universe,
so the two views can never disagree.  Ranking is deterministic:
(td_probability desc, confidence desc, canonical_player_id asc) — no hash,
no name-based signal.  Zero provider network calls: reads Mongo only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ATD_MARKET_REGEX = r"Anytime\s*TD"


def _rank_key(r: dict) -> tuple:
    return (
        -float(r.get("td_probability") or 0.0),
        -float(r.get("confidence") or 0.0),
        str(r.get("player_id") or ""),
    )


def _candidate_from_pick(p: dict, min_probability: float) -> Optional[dict]:
    ev = p.get("atd_evidence") or {}
    td_prob = float(ev.get("td_probability") or 0.0)
    if td_prob <= 0.0:
        wp = p.get("win_probability")
        if isinstance(wp, (int, float)) and wp > 0:
            td_prob = float(wp) / 100.0
    if td_prob < min_probability:
        return None
    team = (p.get("player_team") or p.get("canonical_team_id")
            or p.get("player_team_name") or "")
    if not team:
        return None
    sel = p.get("selection") or p.get("pick") or ""
    event = p.get("event") or ""
    opponent = (ev.get("opponent")
                or (p.get("home_team") if p.get("away_team") == team else p.get("away_team"))
                or "")
    return {
        "canonical_pick_id":  p.get("id"),
        "pick_id":            p.get("id"),
        "canonical_event_id": p.get("canonical_event_id") or p.get("event_id") or event,
        "event":              event,
        "event_time":         p.get("event_time"),
        "home_team":          p.get("home_team"),
        "away_team":          p.get("away_team"),
        "canonical_player_id": p.get("canonical_player_id") or p.get("player_id") or "",
        "player_id":          p.get("canonical_player_id") or p.get("player_id") or "",
        "player_name":        sel or p.get("player_name") or "",
        "player":             sel or p.get("player_name") or "",
        "team":               team,
        "opponent":           opponent,
        "position":           p.get("position") or ev.get("position"),
        "market":             p.get("market"),
        "line":               p.get("line"),
        "td_probability":     round(td_prob, 4),
        "model_probability":  round(td_prob, 4),
        "confidence":         float(ev.get("confidence") or 0.0),
        "opportunity_rating": ev.get("opportunity_rating") or "med",
        "is_rb_archetype":    bool(ev.get("is_rb_archetype")),
        "sample_games":       int(ev.get("sample_games") or 0),
        "reasons":            list(ev.get("reasons") or []),
        "book_odds":          p.get("book_odds"),
        "odds":               p.get("book_odds"),
        "book":               p.get("book") or p.get("bookmaker"),
        "implied_probability": p.get("implied_probability"),
        "edge_percent":       p.get("edge_percent"),
        "lock_score":         p.get("published_lock_score", p.get("lock_score")),
        "grade":              p.get("published_grade", p.get("grade")),
        "model_version":      p.get("model_version"),
        "publication_version": p.get("snapshot_version"),
        "publication_state":  p.get("publication_state"),
        "evidence_authority": ev.get("authority") or ev.get("evidence_authority"),
        "candidate_state":    "PUBLISHED",
        "provenance":         "canonical_publication",
        "updated_at":         p.get("updated_at"),
        "published_at":       p.get("published_at"),
    }


_UNIVERSE_CACHE: dict[str, dict] = {}
_UNIVERSE_TTL_S = 600.0
_UNIVERSE_INFLIGHT: dict[str, "asyncio.Task"] = {}


async def build_atd_universe(db, *, min_probability: float = 0.10) -> dict:
    """Universe is built ONCE per generation window (10 min) and served to
    every ATD consumer from the same in-process snapshot.  A stale snapshot
    is served immediately while ONE background rebuild runs (single-flight).
    Reads Mongo only — no provider calls."""
    import asyncio, time
    key = f"{float(min_probability):.4f}"
    now = time.monotonic()
    cached = _UNIVERSE_CACHE.get(key)
    if cached and (now - cached["_built_mono"]) < _UNIVERSE_TTL_S:
        return cached
    if cached:
        # stale-while-revalidate: kick ONE rebuild, serve last-good now.
        if key not in _UNIVERSE_INFLIGHT or _UNIVERSE_INFLIGHT[key].done():
            _UNIVERSE_INFLIGHT[key] = asyncio.create_task(_rebuild_universe(db, key, min_probability))
        return cached
    # cold: single-flight build
    if key not in _UNIVERSE_INFLIGHT or _UNIVERSE_INFLIGHT[key].done():
        _UNIVERSE_INFLIGHT[key] = asyncio.create_task(_rebuild_universe(db, key, min_probability))
    return await _UNIVERSE_INFLIGHT[key]


async def _rebuild_universe(db, key: str, min_probability: float) -> dict:
    import time
    uni = await _build_atd_universe_uncached(db, min_probability=min_probability)
    uni["_built_mono"] = time.monotonic()
    uni["built_at"] = datetime.now(timezone.utc).isoformat()
    _UNIVERSE_CACHE[key] = uni
    return uni


async def _build_atd_universe_uncached(db, *, min_probability: float = 0.10) -> dict:
    """Return {"candidates": [...ranked...], "canonical_count", "on_demand_count",
    "min_event_time"}.  Reads Mongo only — no provider calls."""
    now = datetime.now(timezone.utc)
    min_event_time = (now - timedelta(minutes=15)).isoformat()
    canonical: list[dict] = []
    cursor = db.picks.find(
        {
            "sport": "NFL",
            "market": {"$regex": ATD_MARKET_REGEX, "$options": "i"},
            "atd_evidence.td_probability": {"$gt": 0},
            "event_time": {"$gte": min_event_time},
        },
        {"_id": 0},
    )
    async for p in cursor:
        c = _candidate_from_pick(p, min_probability)
        if c:
            canonical.append(c)

    on_demand_count = 0
    candidates = canonical
    try:
        from services.nfl_atd_universe import (
            expand_atd_universe_from_live_alt_lines, dedupe_atd_candidates,
        )
        cov: dict[str, dict[str, set]] = {}
        for c in canonical:
            for k in {c.get("event") or "", c.get("canonical_event_id") or ""}:
                if not k:
                    continue
                b = cov.setdefault(k, {"player_ids": set(), "player_names": set()})
                if (c.get("player_id") or "").strip():
                    b["player_ids"].add(c["player_id"].strip())
                if (c.get("player_name") or "").strip():
                    b["player_names"].add(c["player_name"].strip().lower())
        on_demand = await expand_atd_universe_from_live_alt_lines(
            db, canonical_by_event=cov, min_probability=float(min_probability or 0.0),
        )
        if on_demand:
            for r in on_demand:
                r.setdefault("candidate_state", "ON_DEMAND")
                r.setdefault("canonical_player_id", r.get("player_id") or "")
                r.setdefault("model_probability", r.get("td_probability"))
                r.setdefault("player", r.get("player_name"))
                r.setdefault("odds", r.get("book_odds"))
            on_demand_count = len(on_demand)
            candidates = dedupe_atd_candidates(canonical, on_demand)
    except Exception:
        candidates = canonical  # fail-open: canonical universe stays authoritative

    # Dedupe on (canonical_event_id, canonical_player_id, market) — published wins.
    seen: dict[tuple, dict] = {}
    for c in candidates:
        key = (str(c.get("canonical_event_id") or c.get("event") or ""),
               str(c.get("canonical_player_id") or c.get("player_id") or c.get("player_name") or "").lower(),
               "anytime_td")
        prev = seen.get(key)
        if prev is None or (prev.get("candidate_state") != "PUBLISHED" and c.get("candidate_state") == "PUBLISHED"):
            seen[key] = c
    ranked = sorted(seen.values(), key=_rank_key)
    for i, c in enumerate(ranked, 1):
        c["global_rank"] = i
    return {
        "candidates": ranked,
        "canonical_count": len(canonical),
        "on_demand_count": on_demand_count,
        "min_event_time": min_event_time,
    }


def _slate_version(cands: list[dict]) -> str:
    from services.board_snapshot_cache import compute_board_version
    rows = [{"id": c.get("canonical_pick_id") or f"{c.get('canonical_event_id')}::{c.get('player_id')}",
             "updated_at": c.get("updated_at") or ""} for c in cands]
    return compute_board_version(rows)


async def build_atd_slate(db, *, min_probability: float = 0.10, top_n: int = 5,
                          top_n_per_game: int = 3) -> dict:
    uni = await build_atd_universe(db, min_probability=min_probability)
    cands = uni["candidates"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    by_game: dict[str, list[dict]] = {}
    for c in cands:
        k = (c.get("event") or c.get("canonical_event_id") or "unknown").strip()
        by_game.setdefault(k, []).append(c)
    games = []
    for k, rows in by_game.items():
        rows.sort(key=_rank_key)
        for i, r in enumerate(rows, 1):
            r["game_rank"] = i
        head = rows[0]
        kickoff = head.get("event_time") or ""
        state = "SCHEDULED"
        try:
            if kickoff and datetime.fromisoformat(str(kickoff).replace("Z", "+00:00")) <= now:
                state = "STARTED"
        except Exception:
            pass
        games.append({
            # Group key == event name (same key /atd/by-game uses) so
            # Top 5 / By Game / by-game endpoint reconcile exactly.
            "canonical_event_id": k,
            "provider_event_id": head.get("canonical_event_id"),
            "event": head.get("event") or k,
            "away_team": head.get("away_team"),
            "home_team": head.get("home_team"),
            "commence_time": kickoff,
            "event_time": kickoff,
            "state": state,
            "candidates_in_game": len(rows),
            "candidates": rows,
            "top": rows[:top_n_per_game],
        })
    games.sort(key=lambda g: (g["state"] == "STARTED", g.get("commence_time") or "", g.get("event") or ""))

    data_as_of = None
    for c in cands:
        for key in ("published_at", "updated_at"):
            v = c.get(key)
            if isinstance(v, datetime):
                v = v.isoformat()
            if isinstance(v, str) and (data_as_of is None or v > data_as_of):
                data_as_of = v
    top5 = cands[:top_n]
    return {
        "mode": "canonical_publication" if cands else "no_current_bettable_atd",
        "board_version": _slate_version(cands),
        "publication_version": max([int(c.get("publication_version") or 0) for c in cands] or [0]) or None,
        "generated_at": now_iso,
        "universe_built_at": uni.get("built_at"),
        "data_as_of": data_as_of,
        "universe_count": len(cands),
        "canonical_count": uni["canonical_count"],
        "on_demand_count": uni["on_demand_count"],
        "rules": {
            "min_probability": min_probability,
            "min_event_time": uni["min_event_time"],
            "sort_key": "(td_probability desc, confidence desc, canonical_player_id asc)",
            "dedupe": "canonical_event_id + canonical_player_id + market; PUBLISHED wins",
        },
        "top5": top5,
        "games": games,
    }


__all__ = ["build_atd_universe", "build_atd_slate"]
