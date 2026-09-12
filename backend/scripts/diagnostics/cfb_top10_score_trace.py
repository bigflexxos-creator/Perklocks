"""CFB TOP-10 SCORE COMPONENT TRACE — DIAGNOSTIC ONLY (2026-06-12).

Read-only. Does NOT mutate DB. Does NOT change scoring. Traces the
persisted score components for the top 10 current-slate CFB picks so
we can answer: why does the live slate top out at LS 92?

Per pick, we report:
  * raw model probability      (win_probability)
  * simulator probability      (sim_win_probability / cfb_game_sim.sim_probability)
  * calibrated probability     (calibrated_probability)
  * authority ceiling          (magic_tier / provided_cap / probability_provenance)
  * evidence count             (magic_categories_available / factor_sources / factors)
  * evidence quality/tier      (data_quality / probability_provenance)
  * raw Lock Score before caps (lock_score_raw / lock_score_v3_base)
  * every cap/ceiling applied  (magic_tier caps_applied / capped_from
                                / probability_provenance cap / value-floor)
  * final Lock Score           (lock_score / published_lock_score)
  * Magic / V2 score if applicable (magic_score / lock_score_v3_delta)
  * Apex eligibility           (apex_lock / apex_status)
  * Apex gates passed / total  (from apex_reasons length)
  * Apex blockers              (apex_block_reason)

Run:
    cd /app/backend && python -m scripts.diagnostics.cfb_top10_score_trace
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from deps import db


def _ls_of(p: dict) -> float:
    for k in ("published_lock_score", "lock_score_v2", "lock_score"):
        v = p.get(k)
        try:
            f = float(v) if v is not None else None
        except (TypeError, ValueError):
            f = None
        if f is not None and f > 0:
            return f
    return 0.0


async def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.picks.find(
        {"sport": "CFB",
         "event_time": {"$gte": now_iso},
         "off_board": {"$ne": True}},
        {"_id": 0},
    )
    rows = await cursor.to_list(length=1000)
    rows.sort(key=_ls_of, reverse=True)
    top = rows[:10]

    for i, p in enumerate(top, start=1):
        # Extract every cap-related field the code may have stamped.
        magic_tier_dec = p.get("magic_tier") or {}
        magic_signals = (magic_tier_dec.get("signals") if isinstance(magic_tier_dec, dict) else {}) or {}
        cfb_sim = p.get("cfb_game_sim") or {}
        factors = p.get("factors") or {}

        # Evidence-count views (multiple accepted by Magic Tier Policy).
        factor_sources = p.get("factor_sources") or p.get("real_factors_sources") or []
        magic_cats_avail = p.get("magic_categories_available") or []
        magic_cats_pos = p.get("magic_categories_positive") or []
        magic_cats_contra = p.get("magic_categories_contradictory") or []

        # Apex gates evaluated by evaluate_apex() (11 total; sport-specific
        # extra-strict counts a 12th but only for soccer_ATG / NFL_ATD).
        apex_reasons = p.get("apex_reasons") or []
        apex_gates_passed = len(apex_reasons) if isinstance(apex_reasons, list) else 0
        apex_gates_total = 11
        apex_lock = bool(p.get("apex_lock"))

        trace = {
            "rank": i,
            "id": p.get("id"),
            "event": p.get("event"),
            "market": p.get("market"),
            "selection": p.get("selection") or p.get("side"),
            "book_odds": p.get("book_odds"),

            # 1. Probabilities (raw / sim / calibrated / market)
            "raw_model_probability": p.get("win_probability"),
            "simulator_probability_top": p.get("simulator_probability")
                                          or p.get("sim_win_probability"),
            "simulator_probability_cfb_game_sim":
                cfb_sim.get("sim_probability"),
            "calibrated_probability": p.get("calibrated_probability"),
            "implied_probability":    p.get("implied_probability"),

            # 2. Authority ceiling
            "authority": {
                "magic_tier_evaluated":
                    magic_tier_dec.get("magic_tier") if isinstance(magic_tier_dec, dict) else None,
                "original_magic_tier":
                    magic_tier_dec.get("original_tier") if isinstance(magic_tier_dec, dict) else None,
                "provided_cap": magic_signals.get("provided_cap"),
                "probability_provenance": p.get("probability_provenance"),
                "data_quality":  p.get("data_quality") or cfb_sim.get("data_quality"),
                "identity_source": magic_signals.get("identity_source"),
                "stable_identity": magic_signals.get("stable_identity"),
                "lineup_certainty": magic_signals.get("lineup_certainty"),
            },

            # 3. Evidence count / quality
            "evidence": {
                "magic_categories_available_count": len(magic_cats_avail),
                "magic_categories_available":       magic_cats_avail,
                "magic_categories_positive_count":  len(magic_cats_pos),
                "magic_categories_positive":        magic_cats_pos,
                "magic_categories_contradictory":   magic_cats_contra,
                "factor_sources_count": (len(factor_sources)
                                          if isinstance(factor_sources, list) else None),
                "factor_sources":       factor_sources if isinstance(factor_sources, list) else None,
                "factor_keys":          list(factors.keys()),
                "signals_present":      magic_signals.get("signals_present"),
                "sample_size":          magic_signals.get("sample_size"),
                "calibration_gap":      magic_signals.get("calibration_gap"),
                "cfb_sim_sources":      cfb_sim.get("sources"),
            },

            # 4. Raw lock score before caps + delta
            "score_stack": {
                "lock_score_raw":       p.get("lock_score_raw"),
                "lock_score_v3_base":   p.get("lock_score_v3_base"),
                "lock_score_v3_delta":  p.get("lock_score_v3_delta"),
                "lock_score_v3_positive_cap": p.get("lock_score_v3_positive_cap"),
                "lock_score_v3_negative_cap": p.get("lock_score_v3_negative_cap"),
                "lock_score_peak":      p.get("lock_score_peak"),
                "lock_score_v2":        p.get("lock_score_v2"),
                "lock_score":           p.get("lock_score"),
                "published_lock_score": p.get("published_lock_score"),
                "magic_score":          p.get("magic_score"),
                "magic_delta_reasons":  p.get("magic_delta_reasons"),
            },

            # 5. Every cap / down-cap applied
            "caps_applied": {
                "magic_tier_caps_applied":
                    magic_tier_dec.get("caps_applied") if isinstance(magic_tier_dec, dict) else None,
                "magic_tier_reasons":
                    magic_tier_dec.get("reasons") if isinstance(magic_tier_dec, dict) else None,
                "magic_tier_capped_from_to": (
                    f"{magic_tier_dec.get('original_tier')} → {magic_tier_dec.get('magic_tier')}"
                    if isinstance(magic_tier_dec, dict) and magic_tier_dec.get("capped")
                    else None
                ),
                "weak_evidence_market_shrink":
                    factors.get("Weak-Evidence Market Shrink"),
                "chalk_trap":              p.get("chalk_trap"),
                "chalk_trap_meta":         p.get("chalk_trap_meta"),
                "apex_defensive_downgrade":p.get("apex_defensive_downgrade"),
            },

            # 6. Final Lock Score + tier
            "final": {
                "lock_score":  _ls_of(p),
                "grade":       p.get("grade"),
                "tier":        p.get("tier"),
                "confidence":  p.get("confidence"),
                "engine_version":     p.get("block8_integrator_version"),
                "apex_gate_version":  p.get("apex_gate_version"),
            },

            # 7. Apex
            "apex": {
                "apex_lock":         apex_lock,
                "apex_status":       p.get("apex_status"),
                "apex_score":        p.get("apex_score"),
                "gates_passed":      apex_gates_passed,
                "gates_total":       apex_gates_total,
                "apex_block_reason": p.get("apex_block_reason"),
                "apex_reasons":      apex_reasons,
            },
        }
        print(json.dumps(trace, indent=2, default=str))
        print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
