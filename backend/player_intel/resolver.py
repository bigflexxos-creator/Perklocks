"""Player Identity Resolver.

Resolves a free-form player string (from a market label like "Lionel Messi
Anytime Goal Scorer" or "Aaron Judge Over 1.5 Hits") into a canonical player
name + enriched profile.

Resolution order:
  1. In-memory canonical/alias index (seeded at process start)
  2. Mongo `player_profiles_v2` collection (for learned profiles)
  3. Fuzzy match on case-folded normalised name
  4. Fallback: return an empty profile so callers never crash

Calls should always go through `resolve_player(raw, sport)`. The function is
sync-safe and memoised — cheap to call thousands of times per pick refresh.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from functools import lru_cache
from typing import Any

from .schema import empty_profile
from .seeds import seed_rows

logger = logging.getLogger("lockscore.player_intel.resolver")

_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    """Lower-case, strip diacritics + punctuation, collapse whitespace.

    Used for both alias index keys AND market substring matching so we never
    miss "Mbappé" vs "Mbappe" etc.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split())


# In-memory alias index — { sport: { norm_alias: canonical_dict } }
_INDEX: dict[str, dict[str, dict]] = {}


def _build_index() -> None:
    """Construct the alias → canonical lookup from seed rows."""
    _INDEX.clear()
    for row in seed_rows():
        sport = row["sport"]
        bucket = _INDEX.setdefault(sport, {})
        for alias in row["aliases"]:
            n = _norm(alias)
            if n and n not in bucket:
                bucket[n] = row
        bucket[_norm(row["canonical_name"])] = row


_build_index()


def _augment_with_db_profiles(db_profiles: list[dict]) -> None:
    """Merge persisted learned profiles into the in-memory index."""
    for row in db_profiles:
        sport = row.get("sport") or "Soccer"
        bucket = _INDEX.setdefault(sport, {})
        for alias in (row.get("aliases") or []) + [row.get("canonical_name")]:
            n = _norm(alias or "")
            if n and n not in bucket:
                bucket[n] = row


# Player extraction from a market string
_MARKET_PLAYER_RE = re.compile(
    r"^(?P<name>[A-Z][A-Za-zÀ-ÿ.\-' ]+?)\s+"
    r"(?:Anytime|First|Last|To Score|Over|Under|Total\s+\d|Alt|Player|Score)",
    re.IGNORECASE,
)


def extract_player_from_market(market: str) -> str | None:
    if not market:
        return None
    m = _MARKET_PLAYER_RE.match(market.strip())
    if not m:
        return None
    name = m.group("name").strip()
    if " " not in name or len(name) < 5:
        return None
    return name


@lru_cache(maxsize=4096)
def _resolve_cached(raw_norm: str, sport: str) -> dict | None:
    bucket = _INDEX.get(sport) or {}
    if not bucket:
        return None
    hit = bucket.get(raw_norm)
    if hit:
        return hit
    # Substring fallback — walk longest aliases first
    for alias_norm in sorted(bucket.keys(), key=len, reverse=True):
        if len(alias_norm) < 5:
            continue
        if alias_norm in raw_norm:
            return bucket[alias_norm]
    return None


def resolve_player(
    raw: str,
    sport: str,
    market: str | None = None,
) -> dict[str, Any]:
    """Return an enriched player profile dict for the given raw string.

    Never returns None — always a dict matching `schema.empty_profile`.
    """
    if not raw:
        return empty_profile("", sport)
    candidate = raw.strip()
    src = market or candidate
    extracted = extract_player_from_market(src)
    name_for_lookup = extracted or candidate
    norm = _norm(name_for_lookup)
    found = _resolve_cached(norm, sport)
    if found:
        return dict(found)
    return empty_profile(name_for_lookup, sport)


def enrich_picks_with_player_intel(picks: list[dict]) -> int:
    """Attach a `player_intel` dict to every pick whose market references a
    recognised athlete. Mutates picks in-place; returns count enriched."""
    enriched = 0
    for p in picks:
        market = p.get("market") or ""
        sport = p.get("sport") or ""
        name = extract_player_from_market(market)
        if not name:
            continue
        profile = resolve_player(name, sport)
        if profile.get("archetype") or profile.get("canonical_name") != name:
            p["player_intel"] = {
                "canonical_name": profile.get("canonical_name") or name,
                "archetype":      profile.get("archetype"),
                "sport":          sport,
                "team":           profile.get("team"),
                "position":       profile.get("position"),
                "usage_intensity":profile.get("usage_intensity"),
                "volatility":     profile.get("volatility"),
                "source":         profile.get("archetype_source") or profile.get("source"),
            }
            enriched += 1
    return enriched


def rebuild_index_from_db_profiles(profiles: list[dict]) -> None:
    """Public hook used by the refresh job after Mongo is populated."""
    _resolve_cached.cache_clear()
    _augment_with_db_profiles(profiles)
