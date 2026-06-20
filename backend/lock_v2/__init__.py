"""Lock Engine V2 — Shadow Mode.

Deep-thinking scoring layer that computes a parallel `lock_score_v2` for
every pick WITHOUT touching the production `lock_score` field. The current
engine stays visible to users; the v2 engine writes hidden shadow fields:

  * evidence_score   — positive - negative evidence (raw confidence)
  * conviction_score — agreement × survivability × sim_pass
  * counter_score    — opposing case strength (0-100, higher = more pushback)
  * survival_score   — edge-removal resilience (0-100)
  * simulation_pass  — % of removal scenarios that still clear
  * agreement_score  — model agreement
  * lock_score_v2    — mapped to 90-99 branding, gated for 99 "Apex"
  * tier_v2          — Elite | Strong Lock | Rare Lock | Apex Lock
  * is_apex          — bool (all 7 gates pass)
  * v2_reasons       — list of (sign, kind, detail) for the breakdown UI

Gated by env `ENABLE_COUNTER_ENGINE=true`.
"""
from .engine import compute_v2_shadow, V2_ENABLED  # noqa: F401
