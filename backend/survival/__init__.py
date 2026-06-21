"""Survivability Engine — Conditional Hit Coverage.

For every MLB hit prop generated, compute which other hitters ON THE
PRIMARY'S OWN TEAM historically recorded a hit on days when this hitter
went 0-for. Same-team-only is intentional — teammates share the exact
same pitching matchup, weather, ballpark, and batting-order context,
which is the only environment where "coverage" math is meaningful.

The output is pure INSIGHT — it never replaces a pick, never adjusts
odds, never claims 100% certainty.

Data source: MLB Stats API (free, no key required) via the existing
`mlb_live` adapter. New module so it doesn't pollute the picks pipeline.

Public surface:
  GET /api/picks/{pick_id}/coverage   →  routes.coverage_for_pick
  Background scheduler:               →  pipeline.survival_loop

Collections owned:
  • survival_coverage  ─ { pick_id, primary, candidates[], computed_at }

This module is fully isolated and best-effort. If MLB Stats API is
down or rate-limited, the endpoint returns an empty coverage list
with a `note` — the original pick still loads exactly as before.
"""
