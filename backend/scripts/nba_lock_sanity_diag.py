"""NBA Lock Score sanity diagnostic — Phase C Scoring Validation.

For every NBA candidate pick produced by the live pipeline whose Lock Score
is >= 80, dump the exact scoring breakdown so we can detect structural
inflation defects before publication:

  * raw model_win_prob
  * sim_win_probability
  * every factor value fed into compute_lock_score (with normalization)
  * each of the v4 components (confidence / edge / align / dq / vol / roi / clv)
  * effective weights applied (EXPLICIT-PREGAME vs full-signal)
  * final composite + any authority overrides (NFL prop authority / BQ)
  * reasons for anomalies (wp vs lock mismatch, book_implied dominating,
    MODEL_CONDITIONED single-source evidence, etc.)
"""

from __future__ import annotations
import asyncio
import os
import sys
import json
from pprint import pformat

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

os.environ.setdefault("THEODDS_FILTER_SPORT", "NBA")

import sports_engine as se  # noqa: E402


def _mp(x):
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return x
    return round(v, 4)


async def main():
    picks = await se.generate_all_picks(sport_filter="NBA")
    print(f"\nTOTAL NBA candidates: {len(picks)}")
    # Show those >= 80 lock score (so we catch borderline inflation too)
    hi = [p for p in picks if (p.get("lock_score") or 0) >= 80]
    print(f"Picks with lock_score >= 80: {len(hi)}")

    # Market distribution
    from collections import Counter
    mkt_c = Counter()
    for p in picks:
        mkt_c[(p.get("market") or "")[:40]] += 1
    print("\nMarket distribution (top 20 by count):")
    for m, c in mkt_c.most_common(20):
        print(f"  {c:4d}  {m}")

    print("\n" + "=" * 80)
    print("STRUCTURAL BREAKDOWN — PICKS WITH LOCK >= 80")
    print("=" * 80)
    anomalies = []
    for p in sorted(hi, key=lambda x: -float(x.get("lock_score") or 0))[:30]:
        wp   = p.get("model_win_prob") or p.get("win_probability")
        sim  = p.get("sim_win_probability")
        lock = p.get("lock_score")
        ed   = p.get("edge_percent")
        imp  = p.get("implied_probability")
        print(f"\n--- {p.get('market')} · {p.get('pick')}  ---")
        print(f"  lock={lock}  wp={_mp(wp)}  sim_wp={_mp(sim)}  edge={ed}  implied={imp}")
        print(f"  book_odds={p.get('book_odds')}  grade={p.get('grade')}  is_alt={p.get('is_alt')}")
        prov = p.get("sim_provenance") or p.get("simulator_provenance")
        if prov:
            print(f"  sim_provenance={prov}")
        # v4 provenance (populated when lock_score_version is stamped)
        if p.get("lock_confidence_component") is not None:
            print(f"  v4 confidence_component={p.get('lock_confidence_component')}  "
                  f"effective_weights={p.get('lock_effective_weights')}")
        # Factor view (display-side)
        fac = p.get("factors") or {}
        print(f"  factors ({len(fac)} entries):")
        for k, v in list(fac.items())[:12]:
            print(f"     {k!r}: {v}")
        if len(fac) > 12:
            print(f"     ... (+{len(fac) - 12} more)")
        # Anomaly detectors
        reasons = []
        try:
            wp_f = float(wp or 0) / (100.0 if (wp or 0) > 1 else 1.0)
        except Exception:
            wp_f = 0.0
        if wp_f < 0.50 and (lock or 0) >= 85:
            reasons.append(f"LOCK_WP_INVERSION_HIGH ({(lock or 0):.1f} on {wp_f:.1%})")
        if wp_f < 0.20 and (lock or 0) >= 80:
            reasons.append(f"LOCK_WP_LONG_SHOT_INFLATION ({(lock or 0):.1f} on {wp_f:.1%})")
        if abs(float(sim or 0) - float(wp or 0)) < 0.1 and (sim is not None):
            reasons.append("SIM_EQUALS_WP (independence collapse?)")
        if reasons:
            anomalies.append((p, reasons))
            print(f"  !! ANOMALIES: {reasons}")

    print("\n" + "=" * 80)
    print(f"TOTAL ANOMALIES: {len(anomalies)}")
    print("=" * 80)
    if anomalies:
        print("\nSAMPLE anomaly payload (full):")
        p, reasons = anomalies[0]
        print(f"REASONS: {reasons}")
        # Dump everything scoring-related
        for k in sorted(p.keys()):
            if any(t in k.lower() for t in (
                "lock", "wp", "win_prob", "edge", "implied", "factor",
                "data_quality", "confidence", "vol", "align", "roi",
                "clv", "quality", "provenance", "evidence", "authority",
                "model", "simulator")):
                v = p[k]
                if isinstance(v, (dict, list)) and len(str(v)) > 400:
                    v = str(v)[:400] + "..."
                print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
