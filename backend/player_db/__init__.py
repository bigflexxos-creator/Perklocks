"""Player Database — free-source replacement for SportsDataIO (Phase 1: MLB).

Builds & maintains a local MongoDB-backed player intelligence database
using exclusively FREE, no-key sources. Phase 1 covers MLB via the
official MLB Stats API (statsapi.mlb.com). NBA / NFL / Tennis arrive
in later phases.

Cost: $0/month. Quota: unlimited. Latency: local Mongo (≪ HTTPS).

Collections:
  • players       — one doc per player (sport, canonical_name, ids, team,
                    position, height/weight, status, photo_url)
  • player_stats  — season stat aggregates (batting + pitching for MLB)
  • injuries     — current injury list (IL10 / IL15 / IL60 etc.)

The `client.enrich_profile()` function is shape-compatible with the
SportsDataIO client it replaces — callers don't change.
"""
from __future__ import annotations
