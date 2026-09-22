"""CFB HIGH-LOCK FULL TRACE (2026-06-22)

For every current-slate CFB candidate with **lock_score_raw >= 90**,
produce the full reduction chain that answers the user's question:

    Why can the CFB model produce a legitimate 90–98 base score
    while the visible board currently tops out around 88?

Trace fields per pick:
  canonical_pick_id
  event → market → selection
  sportsbook line / odds
  raw_model_probability (win_probability)
  independent_sim_probability (if cfb_independent_sim present)
  lock_score_raw          (compute_lock_score output)
  evidence_score          (evidence_engine.evidence_score)
  evidence_multiplier     (evidence_engine.evidence_multiplier)
  governed_lock           (raw × multiplier)
  magic_tier_grade_cap    (magic_tier_policy)
  magic_tier_signals      (signals_present)
  magic_categories_available
  magic_categories_positive
  magic_tier              (Block 8)
  magic_delta             (Block 8)
  defensive_downgrade
  chalk_trap
  final lock_score
  published_lock_score
  85+ eligibility
  publication_state (off_board vs on_board)
  reduction_chain: list of (stage, before, after, reason)

Emits BASE-model distribution, FINAL-lock distribution, and PUBLISHED-lock
distribution.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, "/app/backend")
from deps import db


def _band(score: float) -> str:
    if score is None:
        return "N/A"
    s = float(score)
    if s >= 100: return "100"
    if s >= 99:  return "99"
    if s >= 98:  return "98"
    if s >= 96:  return "96-97"
    if s >= 93:  return "93-95"
    if s >= 90:  return "90-92"
    if s >= 85:  return "85-89"
    return "<85"


async def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.picks.find(
        {"sport": "CFB", "event_time": {"$gte": now_iso}},
        {"_id": 0},
    )
    rows = await cursor.to_list(length=2000)
    print(f"Total upcoming CFB picks: {len(rows)}")

    dist_base = Counter()
    dist_final = Counter()
    dist_published = Counter()

    high_base_traces = []

    for p in rows:
        raw = p.get("lock_score_raw")
        if raw is None:
            raw = p.get("lock_score")
        try:
            raw_f = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            raw_f = None
        final = p.get("lock_score")
        try:
            final_f = float(final) if final is not None else None
        except (TypeError, ValueError):
            final_f = None
        pub = p.get("published_lock_score")
        try:
            pub_f = float(pub) if pub is not None else None
        except (TypeError, ValueError):
            pub_f = None

        if raw_f is not None:
            dist_base[_band(raw_f)] += 1
        if final_f is not None and not p.get("off_board"):
            dist_final[_band(final_f)] += 1
        if pub_f is not None and not p.get("off_board"):
            dist_published[_band(pub_f)] += 1

        # Full reduction trace ONLY for high-base candidates.
        if raw_f is not None and raw_f >= 90.0:
            mag_tier = p.get("magic_tier") or {}
            sig = (mag_tier.get("signals") if isinstance(mag_tier, dict) else {}) or {}
            trace = {
                "canonical_pick_id": p.get("id"),
                "event": p.get("event"),
                "market": p.get("market"),
                "selection": p.get("selection"),
                "book_odds": p.get("book_odds"),
                "line": p.get("line"),
                "win_probability": p.get("win_probability"),
                "cfb_indep_sim_prob": (
                    (p.get("cfb_independent_sim") or {}).get("home_ml_prob")
                    if p.get("cfb_independent_sim") else None
                ),
                "cfb_indep_sim_available": bool(p.get("cfb_independent_sim")),
                "factor_sources": p.get("factor_sources"),
                "data_quality": p.get("data_quality"),
                "lock_score_raw": raw_f,
                "lock_score_v2": p.get("lock_score_v2"),
                "lock_score_v3_base": p.get("lock_score_v3_base"),
                "lock_score_v3_delta": p.get("lock_score_v3_delta"),
                "lock_score": final_f,
                "published_lock_score": pub_f,
                "grade": p.get("grade"),
                "magic_tier_grade_cap": {
                    "original": mag_tier.get("original_tier") if isinstance(mag_tier, dict) else None,
                    "capped_to": mag_tier.get("magic_tier") if isinstance(mag_tier, dict) else None,
                    "reasons": mag_tier.get("reasons") if isinstance(mag_tier, dict) else None,
                    "signals_present": sig.get("signals_present"),
                },
                "magic_block8": {
                    "categories_available": p.get("magic_categories_available") or [],
                    "categories_positive": p.get("magic_categories_positive") or [],
                    "delta_reasons": p.get("magic_delta_reasons") or [],
                    "positive_cap": p.get("lock_score_v3_positive_cap"),
                    "negative_cap": p.get("lock_score_v3_negative_cap"),
                },
                "apex": {
                    "status": p.get("apex_status"),
                    "lock": p.get("apex_lock"),
                    "block_reason": p.get("apex_block_reason"),
                },
                "gates": {
                    "chalk_trap": p.get("chalk_trap"),
                    "chalk_trap_meta": p.get("chalk_trap_meta"),
                    "off_board": p.get("off_board"),
                    "off_board_reasons": p.get("off_board_reasons"),
                    "no_bet": p.get("no_bet"),
                    "defensive_downgrade": p.get("apex_defensive_downgrade"),
                },
                "reduction_chain": [],
                "final_ge_85_eligible": (final_f is not None and final_f >= 85.0),
            }
            # Build the chain step-by-step.
            steps = []
            if raw_f is not None:
                steps.append({"stage": "compute_lock_score", "value": round(raw_f, 2), "note": "v3-signfix raw"})
            v2 = p.get("lock_score_v2")
            if v2 is not None:
                try:
                    v2f = float(v2)
                    if abs(v2f - raw_f) > 0.05:
                        steps.append({
                            "stage": "lock_score_v2_writer",
                            "value": round(v2f, 2),
                            "note": f"delta={round(v2f - raw_f, 2)}",
                        })
                except (TypeError, ValueError):
                    pass
            v3b = p.get("lock_score_v3_base")
            if v3b is not None:
                try:
                    v3bf = float(v3b)
                    prev = steps[-1]["value"] if steps else raw_f
                    steps.append({
                        "stage": "evidence_governor_multiplier",
                        "value": round(v3bf, 2),
                        "note": f"raw {round(prev, 2)} × multiplier ≈ {round(v3bf / prev, 3) if prev else 0} "
                                f"→ {round(v3bf - prev, 2) if prev else 0}",
                    })
                except (TypeError, ValueError):
                    pass
            if final_f is not None:
                delta = p.get("lock_score_v3_delta")
                steps.append({
                    "stage": "magic_block8_delta",
                    "value": round(final_f, 2),
                    "note": f"delta={delta} reasons={p.get('magic_delta_reasons')}",
                })
            grade_cap = trace["magic_tier_grade_cap"]
            if grade_cap.get("capped_to") and grade_cap.get("original") and grade_cap["capped_to"] != grade_cap["original"]:
                steps.append({
                    "stage": "magic_tier_policy_grade_cap",
                    "value": grade_cap["capped_to"],
                    "note": f"cosmetic (grade only). from={grade_cap['original']} "
                            f"reasons={grade_cap.get('reasons')}",
                })
            if p.get("chalk_trap"):
                steps.append({
                    "stage": "chalk_trap_gate",
                    "value": "OFF_BOARD",
                    "note": f"meta={p.get('chalk_trap_meta')}",
                })
            if p.get("off_board"):
                steps.append({
                    "stage": "off_board_gate",
                    "value": "OFF_BOARD",
                    "note": f"reasons={p.get('off_board_reasons')}",
                })
            trace["reduction_chain"] = steps
            high_base_traces.append(trace)

    print("\n=== CFB DIAGNOSTIC — CURRENT SLATE ===")
    print(f"\nBASE model distribution (lock_score_raw):")
    for band in ["100", "99", "98", "96-97", "93-95", "90-92", "85-89", "<85", "N/A"]:
        if dist_base.get(band):
            print(f"  {band}: {dist_base[band]}")

    print(f"\nFINAL Lock Score distribution (on-board):")
    for band in ["100", "99", "98", "96-97", "93-95", "90-92", "85-89", "<85", "N/A"]:
        if dist_final.get(band):
            print(f"  {band}: {dist_final[band]}")

    print(f"\nPUBLISHED locks distribution (published_lock_score):")
    for band in ["100", "99", "98", "96-97", "93-95", "90-92", "85-89", "<85", "N/A"]:
        if dist_published.get(band):
            print(f"  {band}: {dist_published[band]}")

    print(f"\n=== HIGH-BASE (raw >= 90) TRACES: {len(high_base_traces)} candidates ===\n")
    # Sort by raw score descending
    high_base_traces.sort(key=lambda t: t.get("lock_score_raw") or 0, reverse=True)
    for t in high_base_traces[:20]:
        print(json.dumps(t, indent=2, default=str))
        print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
