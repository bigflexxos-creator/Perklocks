"""Phase 4B — Deterministic Simulation Seed.

Given a pick + a simulator identity, produce a **stable 64-bit seed**
so the same (pick, simulator_version) always produces the same MC
sample path across processes and refreshes.

Prior to Phase 4B every simulator used the unseeded global
``random`` module, meaning identical inputs produced different
outputs every refresh — poisoning regression tests and slate-to-
slate reproducibility.

Seed inputs (in order of preference) — the FIRST STABLE identifier
found is used; anything unstable (Python ``hash()``, display name
only, ``id(pick)``) is refused.

Stable identifiers
==================
  1. ``prediction_id``     — canonical pick id
  2. ``canonical_event_id``— event/game id (falls back to
                              ``event_id`` / ``game_id`` / ``espn_event_id``)
  3. ``market_contract_id``— market + line + side + book identity
                              (built from ``market``/``market_key`` +
                              ``line`` + ``point`` + ``side``)
  4. ``participant_id``    — player id (falls back to canonical name)
  5. ``side``              — Over / Under / Home / Away
  6. ``line``              — exact float line
  7. ``simulator_version`` — simulator name + version

Rules
=====
  • Same pick + same simulator_version → same seed.
  • Different line (0.5 vs 1.5 vs 2.5) → different seed.
  • Different player/event → different seed.
  • Different simulator version → different seed.
  • No Python process-randomised ``hash()``.
  • No display-name-only seeding when a stable ID exists.

Determinism
===========
Uses BLAKE2b (128-bit) truncated to 63 bits so the seed fits in a
signed Python int.  BLAKE2b is a stable, cross-process hash (unlike
Python's PYTHONHASHSEED-randomised ``hash()``).
"""
from __future__ import annotations

from hashlib import blake2b
from typing import Any, Optional


class SeedError(ValueError):
    """Raised when a caller tries to seed without any stable identifier."""


def _first(*vals: Any) -> Optional[str]:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("none", "null", ""):
            return s
    return None


def _pick_field(pick: dict, *keys: str) -> Optional[str]:
    return _first(*(pick.get(k) for k in keys))


def build_seed(
    pick: dict,
    simulator_name: str,
    simulator_version: str,
    *,
    allow_name_only_fallback: bool = False,
) -> int:
    """Return a deterministic 63-bit seed for the (pick, simulator) pair.

    Raises
    ------
    SeedError
        If the pick lacks every candidate stable identifier and
        ``allow_name_only_fallback`` is False.  This forces callers to
        acknowledge (via the flag) that they are seeding on a
        display-name basis, which is discouraged.
    """
    prediction_id = _pick_field(pick, "prediction_id", "id", "pick_id")
    event_id = _pick_field(pick, "canonical_event_id", "event_id",
                            "game_id", "espn_event_id")
    market_key = _pick_field(pick, "market_key", "market",
                              "market_contract_id")
    participant = _pick_field(pick, "player_id", "player", "participant_id",
                                "selection")
    side = _pick_field(pick, "side", "direction", "over_under")
    line = _pick_field(pick, "line", "point", "threshold")

    stable_bits = [prediction_id, event_id, market_key, participant, side, line]
    have_stable = any(x is not None for x in
                        (prediction_id, event_id, market_key))

    if not have_stable:
        # Only a display-name / side / line — refuse unless caller opted-in.
        if not allow_name_only_fallback:
            raise SeedError(
                "cannot seed simulator without a stable identifier "
                "(prediction_id / event_id / market_key). Provided fields "
                f"were: pick.id={prediction_id!r}, event={event_id!r}, "
                f"market={market_key!r}. Pass allow_name_only_fallback=True "
                "to acknowledge the weaker seeding."
            )

    parts = [
        f"sim={simulator_name}",
        f"ver={simulator_version}",
        f"pred={prediction_id or ''}",
        f"evt={event_id or ''}",
        f"mkt={market_key or ''}",
        f"part={participant or ''}",
        f"side={side or ''}",
        f"line={line or ''}",
    ]
    payload = "|".join(parts).encode("utf-8")
    # BLAKE2b, 8-byte digest → 63-bit signed positive int.
    digest = blake2b(payload, digest_size=8).digest()
    seed = int.from_bytes(digest, "big", signed=False)
    # Clamp to positive int63 for Python's random.Random().
    return seed & ((1 << 63) - 1)


def describe_seed_inputs(
    pick: dict,
    simulator_name: str,
    simulator_version: str,
) -> dict:
    """Return an audit dict of the seed inputs (for pick metadata)."""
    return {
        "prediction_id":     _pick_field(pick, "prediction_id", "id", "pick_id"),
        "event_id":          _pick_field(pick, "canonical_event_id", "event_id",
                                         "game_id", "espn_event_id"),
        "market_key":        _pick_field(pick, "market_key", "market",
                                         "market_contract_id"),
        "participant":       _pick_field(pick, "player_id", "player",
                                         "participant_id", "selection"),
        "side":              _pick_field(pick, "side", "direction", "over_under"),
        "line":              _pick_field(pick, "line", "point", "threshold"),
        "simulator_name":    simulator_name,
        "simulator_version": simulator_version,
    }


__all__ = ["build_seed", "describe_seed_inputs", "SeedError"]
