"""Provider scaffolding for external data services (2026-08-25).

New in P3 (Perklocks final surgical repair):
  * pitchapi           — Soccer completed-match PRIMARY provider
  * bigballs           — Cross-sport completed-match FALLBACK provider
  * settlement_bridge  — Cascade helper (PitchAPI → Big Balls) used by
                         the existing canonical settler when a market
                         has been proven wire-ready.

Nothing here is wired into settlement yet.  The existing canonical
settler + The Odds API acquisition path are UNCHANGED.
"""
