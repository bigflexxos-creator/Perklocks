"""Player Intelligence — canonical identity + archetype + volatility for every
athlete across NBA / NFL / Soccer / Tennis.

Entrypoints:
  • resolve_player(name, sport)           → enriched profile dict
  • enrich_picks_with_player_intel(picks)  → mutates picks in-place
  • refresh_player_profiles(db, sport)     → nightly job (settled-pick learning
                                              + API-Sports enrichment)

Never treat player names as plain strings — always resolve them first.
"""
from .resolver import resolve_player, enrich_picks_with_player_intel  # noqa: F401
from .refresh_job import refresh_player_profiles                       # noqa: F401
from .routes import router                                             # noqa: F401
