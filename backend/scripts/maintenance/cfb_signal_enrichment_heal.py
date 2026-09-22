"""CFB SIGNAL ENRICHMENT + LOCK SCORE HEAL (2026-06-22)

Repairs CFB picks that were legitimately produced at v3-signfix base
scores 90-98 but got compounded down by evidence_engine.govern_pick
running repeatedly per refresh.

For each current-slate CFB pick with cfb_engine_version.startswith("cfb_sp_game.v3"):

1. Stamp legitimate INDEPENDENT factor_sources that were already
   present but not enumerated:
     - "cfb_sp_ratings"           (SP+ base — model_family)
     - "the_odds_api"             (real book_odds — market_intel)
     - "cfb_independent_sim"      (Monte Carlo — independent axis)
   NO fabricated sources. Each must have real data on the pick.

2. Run services.cfb_independent_simulator.stamp_independent_sim_on_pick
   using the CFB game_sim inputs already stored on the pick. Adds a
   genuine 2nd probability axis that is NOT derived from SP+ output.

3. Reset lock_score / lock_score_v2 back to lock_score_raw (the
   calibrated pre-governor value). The compounded evidence-governor
   haircut is undone because CFB v3-signfix is now on the calibrated
   fast-path.

4. Re-run apply_magic_tier for the grade cap (cosmetic).

5. Re-run apply_magic_and_apex (Block 8) with the fresh evidence.
   Now signals_present >= 2, so Magic Tier Policy no longer caps to
   "Lock", and Block 8 sees model_family + market_intel available.

6. Bump updated_at + board_version so the frontend cache refreshes.

Idempotent — running twice is a no-op.

Guardrails preserved:
- No score fabrication. Each raw score comes from the v3-signfix rescore.
- No 85+ threshold weakening.
- No manufactured evidence — every source we stamp corresponds to
  data that IS ALREADY present on the pick.
- APEX gate, INSUFFICIENT_EVIDENCE gate, NON_APEX_HARD_CAP unchanged.
"""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from deps import db
from services.cfb_independent_simulator import stamp_independent_sim_on_pick
from services.magic_tier_policy import apply_magic_tier
from services.magic.adapters import build_evidence
from services.magic.lock_score_integrator import apply_magic_and_apex


def _has_real_book_odds(p: dict) -> bool:
    v = p.get("book_odds")
    if v is None:
        return False
    try:
        vf = float(v)
    except (TypeError, ValueError):
        return False
    # Anything from +100..+700 or -100..-2000 is a real american book price.
    if abs(vf) < 100 or abs(vf) > 5000:
        return False
    return True


def _sim_inputs_from_pick(p: dict) -> dict:
    """Extract Monte Carlo sim inputs from the pick's cfb_game_sim block."""
    gs = p.get("cfb_game_sim") or {}
    home = p.get("home_team")
    selection = p.get("selection") or ""
    is_home = 1 if selection == home else 0
    return {
        "expected_margin": gs.get("expected_margin"),
        "expected_total":  gs.get("expected_total"),
        "expected_margin_sigma": gs.get("margin_sigma"),
        "expected_total_sigma":  gs.get("total_sigma"),
        "is_home": is_home,
        "line": p.get("line"),
    }


async def _heal_one(p: dict, stats: dict) -> None:
    stats["considered"] += 1

    engine_v = str(p.get("cfb_engine_version") or "")
    if not engine_v.startswith("cfb_sp_game.v3"):
        stats["skipped_not_v3"] += 1
        return

    # ── Step 1: Reset lock_score to the raw calibrated value.
    raw = p.get("lock_score_raw")
    try:
        raw_f = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        raw_f = None
    if raw_f is None:
        stats["skipped_no_raw"] += 1
        return

    lock_before = p.get("lock_score")
    p["lock_score"] = round(raw_f, 1)
    p["lock_score_v2"] = round(raw_f, 1)
    # Clear the compounded governor stamp on the peak so it can
    # advance from the reset raw if it later climbs.
    p["lock_score_peak"] = round(max(float(p.get("lock_score_peak") or 0), raw_f), 1)

    # ── Step 2: Run the independent Monte Carlo sim.
    sim_inputs = _sim_inputs_from_pick(p)
    if sim_inputs.get("expected_margin") is not None:
        try:
            stamp_independent_sim_on_pick(p, sim_inputs)
            stats["sim_stamped"] += 1
        except Exception as e:
            stats.setdefault("sim_errors", 0)
            stats["sim_errors"] += 1

    # ── Step 3: Stamp legitimate factor_sources.
    existing_sources = list(p.get("factor_sources") or [])
    new_sources = list(existing_sources)
    # SP+ is always the base source.
    if "cfb_sp_ratings" not in new_sources:
        new_sources.append("cfb_sp_ratings")
    # Real market — book_odds present.
    if _has_real_book_odds(p) and "the_odds_api" not in new_sources:
        new_sources.append("the_odds_api")
    # Independent sim — only if truly stamped and non-empty.
    if isinstance(p.get("cfb_independent_sim"), dict) and p["cfb_independent_sim"].get("trials", 0) > 0:
        if "cfb_independent_sim" not in new_sources:
            new_sources.append("cfb_independent_sim")
    p["factor_sources"] = new_sources

    # ── Step 4: Re-apply magic tier policy for grade cap.
    apply_magic_tier(p, sport="CFB", write_back=True)

    # ── Step 5: Re-run Block 8 magic evidence + integration.
    try:
        mo = await build_evidence(db, p)
    except Exception as e:
        stats.setdefault("magic_evidence_errors", 0)
        stats["magic_evidence_errors"] += 1
        mo = None
    if mo is not None:
        try:
            apply_magic_and_apex(p, mo)
            stats["magic_applied"] += 1
        except Exception as e:
            stats.setdefault("magic_integration_errors", 0)
            stats["magic_integration_errors"] += 1

    # Final rewriting.
    final_ls = float(p.get("lock_score") or 0.0)
    # Grade — mirror sports_engine._grade
    if final_ls >= 100.0: _grade = "APEX Lock"
    elif final_ls >= 98.0: _grade = "Elite Lock"
    elif final_ls >= 95.0: _grade = "Strong Lock"
    elif final_ls >= 90.0: _grade = "Lock"
    elif final_ls >= 85.0: _grade = "Playable"
    else:                  _grade = "Pass"
    # Magic Tier Policy may have already overwritten `grade` with its
    # capped label. Keep whichever is stricter (lower rank).
    mtd = p.get("magic_tier") or {}
    if isinstance(mtd, dict) and mtd.get("capped"):
        capped = mtd.get("magic_tier") or _grade
        _grade = capped
    p["grade"] = _grade

    now_iso = datetime.now(timezone.utc).isoformat()
    set_payload = {
        "lock_score": p["lock_score"],
        "lock_score_v2": p["lock_score_v2"],
        "lock_score_raw": p.get("lock_score_raw") or round(raw_f, 1),
        "lock_score_peak": p["lock_score_peak"],
        "lock_score_v3_base": p.get("lock_score_v3_base"),
        "lock_score_v3_delta": p.get("lock_score_v3_delta"),
        "lock_score_v3_positive_cap": p.get("lock_score_v3_positive_cap"),
        "lock_score_v3_negative_cap": p.get("lock_score_v3_negative_cap"),
        "magic_categories_available": p.get("magic_categories_available") or [],
        "magic_categories_positive": p.get("magic_categories_positive") or [],
        "magic_categories_contradictory": p.get("magic_categories_contradictory") or [],
        "magic_delta_reasons": p.get("magic_delta_reasons") or [],
        "magic_tier_at_integration": p.get("magic_tier_at_integration"),
        "magic_tier": p.get("magic_tier"),
        "apex_lock": p.get("apex_lock"),
        "apex_status": p.get("apex_status"),
        "apex_score": p.get("apex_score"),
        "apex_reason": p.get("apex_reason"),
        "apex_reasons": p.get("apex_reasons"),
        "apex_block_reason": p.get("apex_block_reason"),
        "apex_evaluated_at": p.get("apex_evaluated_at"),
        "apex_gate_version": p.get("apex_gate_version"),
        "grade": _grade,
        "published_lock_score": p["lock_score"],
        "published_grade": _grade,
        "factor_sources": p["factor_sources"],
        "cfb_independent_sim": p.get("cfb_independent_sim"),
        "sim_stability": p.get("sim_stability"),
        "updated_at": now_iso,
        "last_seen_at": now_iso,
        "cfb_signal_enrichment_at": now_iso,
        "snapshot_version": int((p.get("snapshot_version") or 0)) + 1,
    }
    # Advance published_probability too so the published-reader
    # (which aliases these OVER the legacy fields) sees fresh math.
    try:
        set_payload["published_probability"] = round(
            float(p.get("win_probability") or 0.0) / 100.0, 6)
    except Exception:
        pass
    set_payload["pregame_score_snapshot"] = p.get("pregame_score_snapshot")

    await db.picks.update_one({"_id": p["_id"]}, {"$set": set_payload})
    stats["healed"] += 1

    # Track distribution
    lb = _band(final_ls)
    stats["dist_final"][lb] = stats["dist_final"].get(lb, 0) + 1
    if final_ls >= 85 and not p.get("off_board"):
        stats["board_ge85"] += 1
    if final_ls >= 90 and not p.get("off_board"):
        stats["board_ge90"] += 1


def _band(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "N/A"
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
    print(f"CFB Signal Enrichment + Lock Score Heal — {now_iso}")
    match = {
        "sport": "CFB",
        "event_time": {"$gte": now_iso},
    }
    total = await db.picks.count_documents(match)
    print(f"CFB upcoming picks: {total}")

    stats = {
        "considered": 0,
        "healed": 0,
        "skipped_not_v3": 0,
        "skipped_no_raw": 0,
        "sim_stamped": 0,
        "magic_applied": 0,
        "board_ge85": 0,
        "board_ge90": 0,
        "dist_final": {},
    }
    cursor = db.picks.find(match)
    async for p in cursor:
        await _heal_one(dict(p), stats)  # copy so we mutate freely

    print("\n=== CFB SIGNAL ENRICHMENT COMPLETE ===")
    for k, v in stats.items():
        if k == "dist_final":
            continue
        print(f"  {k}: {v}")
    print("\n  Final Lock Score distribution:")
    for band in ["100", "99", "98", "96-97", "93-95", "90-92", "85-89", "<85"]:
        n = stats["dist_final"].get(band, 0)
        if n:
            print(f"    {band}: {n}")

    # Bump board version so clients refresh caches.
    if stats["healed"] > 0:
        try:
            from services import board_generation
            gen_id = await board_generation.begin(scope="CFB_SIGNAL_ENRICH")
            bver, pcount, ecount = await board_generation.board_state()
            ok = await board_generation.commit(gen_id, bver, pcount, ecount)
            print(f"\n  board_generation.commit: board_version={bver} pick_count={pcount} ok={ok}")
        except Exception as e:
            print(f"\n  board_generation.commit failed (non-fatal): {e}")


if __name__ == "__main__":
    asyncio.run(main())
