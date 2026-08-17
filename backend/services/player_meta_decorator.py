"""Player-meta headshot decorator (PRESENTATION ONLY).

Attaches ``player_meta.headshot_url`` to a pick when its canonical
player identity resolves cleanly against the existing ``db.players``
collection (populated by ``player_db/ingestors/espn_public.py`` for
NFL/NBA and by the MLB Stats API for MLB — 9,849 players with
photos as of this fix).

CRITICAL SAFETY:
    • PRESENTATION ONLY — never touches ``lock_score``,
      ``published_lock_score``, ``win_probability``, ``edge``,
      ``line``, ``odds``, ``market``, ``selection``, publication
      eligibility, or the canonical pick identity.
    • Player-props ONLY — game markets (Moneyline / Spread /
      Total / RL / BTTS / DC / 1X2) get NO player_meta stamp so
      the frontend continues to use team logos.
    • AMBIGUITY-GUARDED — resolution requires sport + canonical
      name + (team when available).  Missing / ambiguous inputs
      → NO stamp (fallback chain in the frontend takes over).
      Fuzzy name matching is explicitly disallowed.
    • Additive — never replaces existing pick fields.
    • Fail-open — a lookup exception logs at DEBUG and the loop
      continues.  A missing / broken photo NEVER prevents a card
      from rendering.

In-process cache (bounded, TTL 6h — athlete photos rarely change)
prevents N+1 lookups across sequential Board loads.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.player_meta_decorator")

# ── Bounded in-process cache ────────────────────────────────────────
# Key: (sport_lower, canonical_name, team_upper_or_empty)
# Val: (expiry_epoch, payload_or_None) — None = negative cache (no match)
_TTL_SECS = 6 * 3600   # 6 hours — headshots almost never change
_MAX_ENTRIES = 4096
_cache: dict[tuple[str, str, str], tuple[float, Optional[dict]]] = {}


def _canonical(name: str) -> str:
    return (name or "").strip().lower()


def _cache_get(key: tuple[str, str, str]) -> tuple[bool, Optional[dict]]:
    ent = _cache.get(key)
    if not ent:
        return (False, None)
    expiry, payload = ent
    if expiry < time.time():
        _cache.pop(key, None)
        return (False, None)
    return (True, payload)


def _cache_put(key: tuple[str, str, str], payload: Optional[dict]) -> None:
    if len(_cache) >= _MAX_ENTRIES:
        # Trim oldest 25% — cheap FIFO-ish eviction.
        for k in list(_cache.keys())[: _MAX_ENTRIES // 4]:
            _cache.pop(k, None)
    _cache[key] = (time.time() + _TTL_SECS, payload)


def _pick_player_name(pick: dict) -> Optional[str]:
    """Extract the canonical player name from the pick when it is a
    PLAYER PROP.  Return None for game markets.

    Resolution order (canonical → weakest):
      1. selection_v2.selection.player   (canonical parsed selection)
      2. elite_player_name / player_name (server-attached)
      3. None (game market)
    """
    sv2 = pick.get("selection_v2") or {}
    sel = (sv2.get("selection") or {}) if isinstance(sv2, dict) else {}
    nm = sel.get("player")
    if isinstance(nm, str) and nm.strip():
        return nm.strip()
    for k in ("elite_player_name", "player_name"):
        v = pick.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _pick_team_abbrev(pick: dict) -> Optional[str]:
    """Best-available team abbreviation the player is on.

    Prefers the canonical selection team, then falls back to home/away
    meta abbrev + selection-string matching (same rule the frontend uses).
    """
    sv2 = pick.get("selection_v2") or {}
    sel_team = (sv2.get("selection") or {}).get("team") if isinstance(sv2, dict) else None
    home = pick.get("home_meta") or {}
    away = pick.get("away_meta") or {}
    home_ab = home.get("abbrev")
    away_ab = away.get("abbrev")
    if isinstance(sel_team, str) and sel_team.strip():
        # Team may be full name, resolve to abbrev via home/away if possible.
        sel_up = sel_team.strip().upper()
        if home_ab and home.get("abbrev", "").upper() == sel_up:
            return sel_up
        if away_ab and away.get("abbrev", "").upper() == sel_up:
            return sel_up
        # Not an abbreviation — return whatever we have (find_player will
        # normalize to uppercase; a full team name may still match on
        # ``team`` field if the ingestor stored it that way).
        return sel_up
    # Fallback: match selection substring against home/away abbrev.
    selection = (pick.get("selection") or "").upper()
    if home_ab and str(home_ab).upper() in selection:
        return str(home_ab).upper()
    if away_ab and str(away_ab).upper() in selection:
        return str(away_ab).upper()
    return None


# Sport-string normalization used by db.players (all lowercase — see
# player_db/client.py).  Matches the ingestors' league slugs.
_SPORT_TO_KEY = {
    "MLB": "mlb", "NFL": "nfl", "NBA": "nba", "NHL": "nhl",
    "WNBA": "wnba", "CFB": "college-football",
    "Soccer": "soccer", "Tennis": "tennis", "UFC": "ufc", "KBO": "mlb",
}


async def _resolve_from_player_db(
    db, sport: str, canonical_name: str, team: Optional[str]
) -> Optional[dict]:
    """Query db.players via player_db.find_player.  Returns the raw row
    or None.  Never raises — swallowed at the caller."""
    try:
        from player_db.client import find_player
    except Exception as e:  # pragma: no cover
        logger.debug("player_db.find_player import failed: %s", e)
        return None
    key = _SPORT_TO_KEY.get(sport, sport.lower())
    try:
        # ``find_player`` accepts DISPLAY name; performs canonical
        # matching internally.  It respects the (sport, canonical_name,
        # team) uniqueness that guards against wrong-photo attribution.
        row = await find_player(key, canonical_name, team)
        return row
    except Exception as e:  # pragma: no cover
        logger.debug("find_player raised for %s/%s: %s", sport, canonical_name, e)
        return None


def _looks_like_photo(url: Optional[str]) -> bool:
    if not isinstance(url, str):
        return False
    u = url.strip().lower()
    return u.startswith("https://") and (
        u.endswith(".png") or u.endswith(".jpg") or u.endswith(".jpeg")
        or "espncdn.com" in u or "mlbstatic.com" in u
        or "images/players" in u or "headshots" in u
    )


async def decorate_with_player_meta(db, picks: list[dict]) -> list[dict]:
    """Attach ``player_meta.headshot_url`` to player-prop picks.

    Additive: never modifies existing fields.  Fail-open: any lookup
    exception is swallowed.  Idempotent: a pick already carrying a
    ``player_meta.headshot_url`` is skipped.
    """
    if not picks:
        return picks
    for p in picks:
        try:
            # Idempotency — skip if already stamped.
            existing = p.get("player_meta")
            if isinstance(existing, dict) and existing.get("headshot_url"):
                continue
            # Player-prop detection (frontend contract).
            name = _pick_player_name(p)
            if not name:
                continue    # Game market — no player_meta stamp.
            sport = p.get("sport") or ""
            if not sport:
                continue
            team = _pick_team_abbrev(p)
            key = (sport.lower(), _canonical(name), (team or ""))
            hit, cached = _cache_get(key)
            if hit:
                row = cached
            else:
                row = await _resolve_from_player_db(db, sport, name, team)
                _cache_put(key, row if isinstance(row, dict) else None)
            if not isinstance(row, dict):
                continue    # Ambiguity guard — NO stamp when no match.
            photo = row.get("photo_url")
            if not _looks_like_photo(photo):
                continue    # Refuse suspect / non-authoritative URLs.
            # Build the presentation-only object.  Only include fields
            # that are actually available.
            meta: dict[str, Any] = {
                "display_name":       row.get("display_name") or name,
                "team":               row.get("team") or team,
                "headshot_url":       photo,
                "headshot_source":    row.get("source") or "espn/mlb_stats",
                "headshot_verified":  True,
            }
            # Add authoritative external identifiers WHEN AVAILABLE
            # (not required — kept for downstream audit / future dedupe).
            for src_k, dst_k in (
                ("espn_id", "external_id"),
                ("mlb_id",  "mlb_id"),
                ("player_id", "player_id"),
            ):
                v = row.get(src_k)
                if v not in (None, ""):
                    meta[dst_k] = v
            p["player_meta"] = meta
        except Exception as e:
            logger.debug("player_meta enrich skipped for pick %s: %s",
                          p.get("id"), e)
    return picks


__all__ = ["decorate_with_player_meta"]
