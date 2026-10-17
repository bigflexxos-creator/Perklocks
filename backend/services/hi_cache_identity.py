"""Historical Intelligence cache identity + invalidation (B2 — item 31).

The HI cache key must advance INDEPENDENTLY of board generation.  A
newly-ingested completed event must invalidate HI even when the Locks
board has not been regenerated.

Cache-identity components (per the directive):
  * history_revision / freshness_epoch — bumps whenever
    ``player_game_actuals`` / ``team_game_actuals`` admit a new row
    (checked via ``consumer_latest_event_time``).
  * pick_id — the specific pick an HI payload is attached to.
  * sample_scope — e.g. L5 / L10 / L20 / full season.
  * venue — home / away / neutral.
  * perspective — opponent / vs-opp / neutral-form.

The resulting cache key is a stable SHA1 digest of the components.
Invalidation is TTL-free: the key changes the moment ``history_
revision`` moves, forcing a re-read.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional


def _canon(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip().lower()


def hi_cache_key(*,
                 pick_id: str,
                 history_revision: str | int,
                 sample_scope: str = "L10",
                 venue: Optional[str] = None,
                 perspective: Optional[str] = None,
                 opponent_canonical_id: Optional[str] = None,
                 ) -> str:
    """Return a stable HI cache key.

    * The key CHANGES when any of (history_revision, pick_id,
      sample_scope, venue, perspective, opponent_canonical_id)
      changes.
    * ``history_revision`` MUST be derived from an authoritative
      event-time-bump signal (see ``hi_event_time_freshness``).
    * No display strings participate in the key.
    """
    parts = (
        _canon(pick_id),
        _canon(history_revision),
        _canon(sample_scope),
        _canon(venue),
        _canon(perspective),
        _canon(opponent_canonical_id),
    )
    key = "|".join(parts)
    return "hi_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def should_invalidate(prev_key: Optional[str],
                      *,
                      pick_id: str,
                      history_revision: str | int,
                      sample_scope: str = "L10",
                      venue: Optional[str] = None,
                      perspective: Optional[str] = None,
                      opponent_canonical_id: Optional[str] = None,
                      ) -> bool:
    """Return True iff the previous key is NO LONGER valid for the
    current identity.  TTL is NOT consulted — a known identity move
    always invalidates regardless of age (item 31).
    """
    current = hi_cache_key(
        pick_id=pick_id,
        history_revision=history_revision,
        sample_scope=sample_scope,
        venue=venue,
        perspective=perspective,
        opponent_canonical_id=opponent_canonical_id,
    )
    if prev_key is None:
        return True
    return prev_key != current


__all__ = ["hi_cache_key", "should_invalidate"]
