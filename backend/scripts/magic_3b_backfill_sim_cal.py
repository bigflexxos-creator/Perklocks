"""MAGIC 3B — Backfill db.simulator_outputs and db.calibrated_probabilities.

Reads existing sim + calibration output ALREADY PERSISTED on
db.picks (root-level `sim_*` fields and `brain.confidence_calibrated`)
and mirrors them into the dedicated collections with input
fingerprints.  Never fabricates values.  Never modifies db.picks.

Idempotent (upsert on (pick_id, version, fingerprint)).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.magic.sim_cal_store import (
    persist_simulator_output, persist_calibration,
    SIMULATOR_OUTPUTS_COLLECTION,
    CALIBRATED_PROBABILITIES_COLLECTION,
)


async def run(*, write: bool) -> dict:
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]

    counts = {
        "picks_scanned":               0,
        "sim_eligible":                0,
        "sim_persisted":               0,
        "sim_rejected_low_runs":       0,
        "sim_rejected_no_provenance":  0,
        "cal_eligible":                0,
        "cal_persisted":               0,
        "cal_rejected_no_method":      0,
    }

    per_sport_sim: dict[str, dict] = {}
    per_sport_cal: dict[str, dict] = {}

    async for p in db.picks.find(
        {},
        {
            "_id": 0,
            "id": 1, "sport": 1, "league": 1, "market": 1,
            "selection": 1, "line": 1, "side": 1,
            "canonical_event_id": 1, "event": 1, "event_time": 1,
            "canonical_player_id": 1, "player_name": 1,
            "canonical_team_id": 1, "team_name": 1,
            "opponent_team": 1, "opposing_pitcher": 1,
            "model_version": 1, "model_probability": 1, "win_probability": 1,
            "sim_win_probability": 1, "sim_runs": 1, "sim_signal": 1,
            "simulator_name": 1, "simulator_version": 1,
            "simulator_type": 1, "seed": 1,
            "independent_evidence": 1, "valid": 1,
            "sim_ci_lower": 1, "sim_ci_upper": 1,
            "sim_mean": 1, "sim_median": 1,
            "sim_q10": 1, "sim_q25": 1, "sim_q75": 1, "sim_q90": 1,
            "sim_std": 1,
            "brain": 1,
        },
    ):
        counts["picks_scanned"] += 1
        sport = p.get("sport") or "UNKNOWN"

        # ── Simulator ───────────────────────────────────────────
        if p.get("sim_win_probability") is not None:
            counts["sim_eligible"] += 1
            per_sport_sim.setdefault(sport, {"eligible": 0, "persisted": 0})
            per_sport_sim[sport]["eligible"] += 1

            sim_dict = {
                "sim_win_probability":  p.get("sim_win_probability"),
                "sim_runs":             p.get("sim_runs"),
                "sim_signal":           p.get("sim_signal"),
                "simulator_name":       p.get("simulator_name"),
                "simulator_version":    p.get("simulator_version"),
                "simulator_type":       p.get("simulator_type"),
                "seed":                 p.get("seed"),
                "independent_evidence": p.get("independent_evidence"),
                "valid":                p.get("valid"),
                "sim_ci_lower":         p.get("sim_ci_lower"),
                "sim_ci_upper":         p.get("sim_ci_upper"),
                "sim_mean":             p.get("sim_mean"),
                "sim_median":           p.get("sim_median"),
                "sim_q10":              p.get("sim_q10"),
                "sim_q25":              p.get("sim_q25"),
                "sim_q75":              p.get("sim_q75"),
                "sim_q90":              p.get("sim_q90"),
                "sim_std":              p.get("sim_std"),
            }
            # Under-run or missing provenance → count rejection.
            # NOTE: rejection here MUST NOT skip the calibration branch
            # below — a pick can be sim-rejected but cal-eligible.
            try:
                runs = int(sim_dict.get("sim_runs") or 0)
            except (TypeError, ValueError):
                runs = 0
            _sim_write_ok = True
            if runs < 1000:
                counts["sim_rejected_low_runs"] += 1
                _sim_write_ok = False
            elif not sim_dict.get("simulator_name"):
                counts["sim_rejected_no_provenance"] += 1
                _sim_write_ok = False

            if write and _sim_write_ok:
                fp = await persist_simulator_output(db, p, sim_dict)
                if fp:
                    counts["sim_persisted"] += 1
                    per_sport_sim[sport]["persisted"] += 1

        # ── Calibration ─────────────────────────────────────────
        brain_block = p.get("brain") or {}
        if brain_block.get("confidence_calibrated") is not None:
            counts["cal_eligible"] += 1
            per_sport_cal.setdefault(sport, {"eligible": 0, "persisted": 0})
            per_sport_cal[sport]["eligible"] += 1

            if write:
                fp = await persist_calibration(db, p)
                if fp:
                    counts["cal_persisted"] += 1
                    per_sport_cal[sport]["persisted"] += 1
                else:
                    counts["cal_rejected_no_method"] += 1

    return {
        "mode":          "WRITE" if write else "DRY_RUN",
        "counts":        counts,
        "per_sport_sim": per_sport_sim,
        "per_sport_cal": per_sport_cal,
    }


def _print(rep: dict) -> None:
    print("=" * 72)
    print(f"MAGIC 3B BACKFILL — {rep['mode']}")
    print("=" * 72)
    for k, v in rep["counts"].items():
        print(f"  {k:<35}  {v:>7}")
    print()
    print("Per-sport simulator coverage:")
    for sp, s in sorted(rep["per_sport_sim"].items(),
                          key=lambda kv: -kv[1]["eligible"]):
        pct = 100.0 * s["persisted"] / s["eligible"] if s["eligible"] else 0.0
        print(f"  {sp:<20} eligible={s['eligible']:>5}  "
              f"persisted={s['persisted']:>5} ({pct:5.1f}%)")
    print()
    print("Per-sport calibration coverage:")
    for sp, s in sorted(rep["per_sport_cal"].items(),
                          key=lambda kv: -kv[1]["eligible"]):
        pct = 100.0 * s["persisted"] / s["eligible"] if s["eligible"] else 0.0
        print(f"  {sp:<20} eligible={s['eligible']:>5}  "
              f"persisted={s['persisted']:>5} ({pct:5.1f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    write = args.write and not args.dry_run
    rep = asyncio.run(run(write=write))
    _print(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
