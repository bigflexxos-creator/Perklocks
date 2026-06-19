"""PerksLocks soccer data module.

Isolated layer that integrates API-Football (api-sports.io) as a richer
data source for Soccer predictions, running ALONGSIDE the existing
soccer pick generator in sports_engine.py. The two systems coexist:

  • sports_engine.py  ── existing Odds API-driven picks (200+/day)
  • soccer/*          ── new API-Football-driven predictions w/ lineups,
                         injuries, top-scorer stats, standings

New predictions are written to BOTH:
  • soccer_predictions  ── canonical store for this module's outputs
  • picks               ── merged so they show in existing Locks/Killer/
                            Rollover tabs (user choice 2B)

No other backend module imports anything from here directly except via
the FastAPI router mounted in server.py — keeps the integration
fully isolated and easy to roll back if needed.
"""
