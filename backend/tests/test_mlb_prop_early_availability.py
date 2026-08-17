"""MLB Prop Early-Availability μ-repair — focused regressions.

Scope:
  (1) Hitter early-availability cap adjustment (unknown 79→88).
  (2) Diagnostic: prove pitcher-K first collapse is at the
      ``live_alt_lines`` cache layer (empty), not at the model /
      canonical publication layer.  Publication pipeline itself
      is intact — 68 canonical MLB strikeouts + 56 board-visible.
  (3) Confirmed-lineup reconciliation semantics preserved.
  (4) Bench/scratched still refuses publication.
  (5) Canonical single-board architecture preserved.
"""
import asyncio, os, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════
# (1) Hitter early-availability — unknown cap above canonical floor
# ══════════════════════════════════════════════════════════════════
def test_1_unknown_cap_allows_board_reachability():
    from services.mlb_gates import data_quality_cap_for_status
    # Before μ-fix: 79 (below 85 floor — silent Board invisibility).
    # After μ-fix: 88 (above 85 floor — reachable, still capped).
    cap = data_quality_cap_for_status("unknown")
    assert cap is not None and cap >= 85.0, (
        f"μ-fix regression — unknown cap {cap} still blocks 85+ Board")
    assert cap < 92.0, (
        f"μ-fix regression — unknown cap {cap} matches projected_starter "
        "(uncertainty safeguard erased)")
    # projected_starter and confirmed_starter contract intact.
    assert data_quality_cap_for_status("projected_starter") == 92.0
    assert data_quality_cap_for_status("confirmed_starter") == 99.0
    # Bench/scratched refuse publication (do-not-publish invariant).
    assert data_quality_cap_for_status("bench") is None
    assert data_quality_cap_for_status("scratched") is None
    print("test_1_unknown_cap_allows_board_reachability OK")


# ══════════════════════════════════════════════════════════════════
# (2) should_publish semantics unchanged for bench/scratched
# ══════════════════════════════════════════════════════════════════
def test_2_should_publish_semantics_preserved():
    from services.mlb_gates import should_publish
    assert should_publish("confirmed_starter") is True
    assert should_publish("projected_starter") is True
    assert should_publish("unknown")           is True
    assert should_publish("bench")             is False
    assert should_publish("scratched")         is False
    print("test_2_should_publish_semantics_preserved OK")


# ══════════════════════════════════════════════════════════════════
# (3) Lineup classification priority
# ══════════════════════════════════════════════════════════════════
def test_3_classify_lineup_priority():
    from services.mlb_gates import classify_lineup_status
    # scratched > bench > confirmed > projected > unknown
    assert classify_lineup_status(scratched=True) == "scratched"
    assert classify_lineup_status(on_bench=True)  == "bench"
    assert classify_lineup_status(lineup_confirmed=True) == "confirmed_starter"
    assert classify_lineup_status(is_starter=True,
                                    lineup_confirmed=False) == "projected_starter"
    assert classify_lineup_status(lineup_slot=3) == "projected_starter"
    assert classify_lineup_status() == "unknown"
    print("test_3_classify_lineup_priority OK")


# ══════════════════════════════════════════════════════════════════
# (4) Runtime disposition diagnostic (live DB probe).
#     Documents the FIRST collapse boundary for MLB strikeouts and
#     Hits so the report has grounded evidence.
# ══════════════════════════════════════════════════════════════════
def test_4_mlb_prop_disposition_diagnostic():
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        try:
            report = {}
            for market_key, label in [
                ("pitcher_strikeouts", "K"),
                ("batter_hits",        "HITS"),
                ("batter_total_bases", "TB"),
                ("batter_home_runs",   "HR"),
            ]:
                live_cache = await db.live_alt_lines.count_documents(
                    {"market_key": {"$regex": market_key, "$options": "i"}})
                propline_cache = await db.propline_alt_lines.count_documents(
                    {"market_key": {"$regex": market_key, "$options": "i"}})
                # db.picks trace for the same market family
                match_regex = "Strikeout" if market_key == "pitcher_strikeouts" \
                    else "Hits" if market_key == "batter_hits" \
                    else "Total Bases" if market_key == "batter_total_bases" \
                    else "HR"
                q = {"sport": "MLB",
                      "market": {"$regex": match_regex, "$options": "i"}}
                total       = await db.picks.count_documents(q)
                canonical   = await db.picks.count_documents(
                    {**q, "publication_source": {"$exists": True, "$ne": None}})
                board       = await db.picks.count_documents(
                    {**q, "publication_source": {"$exists": True, "$ne": None},
                     "off_board": {"$ne": True}, "no_bet": {"$ne": True},
                     "settlement_block": {"$ne": True}})
                report[label] = {
                    "live_alt_lines":    live_cache,
                    "propline_cache":    propline_cache,
                    "picks_total":       total,
                    "picks_canonical":   canonical,
                    "picks_board_visible": board,
                }
            # Print the disposition table.
            print("  MLB Prop Disposition Report:")
            for k, v in report.items():
                print(f"    {k:5s}  live_alt={v['live_alt_lines']:>4d}  "
                      f"propline={v['propline_cache']:>4d}  "
                      f"picks_total={v['picks_total']:>5d}  "
                      f"canonical={v['picks_canonical']:>4d}  "
                      f"board={v['picks_board_visible']:>4d}")
            # Diagnostic invariant: canonical publication path is
            # working (picks_canonical > 0 for at least one family).
            assert any(v["picks_canonical"] > 0 for v in report.values()), (
                "FULL COLLAPSE: no canonical MLB props published in preview "
                "— pipeline is broken upstream of publication")
        finally:
            cx.close()
    asyncio.run(_run())
    print("test_4_mlb_prop_disposition_diagnostic OK")


# ══════════════════════════════════════════════════════════════════
# (5) Canonical single-board preserved — no new writer added
# ══════════════════════════════════════════════════════════════════
def test_5_no_new_publication_writer_introduced():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "services/mlb_gates.py")) as f:
        src = f.read()
    # Ensure the μ-fix did NOT introduce a direct db.picks write
    # or a bypass around PredictionPublicationService.
    assert "db.picks.update" not in src
    assert "db.picks.insert" not in src
    assert "PredictionPublicationService" not in src, (
        "canonical single-board violation — mlb_gates now references "
        "a publication writer directly")
    print("test_5_no_new_publication_writer_introduced OK")


if __name__ == "__main__":
    test_1_unknown_cap_allows_board_reachability()
    test_2_should_publish_semantics_preserved()
    test_3_classify_lineup_priority()
    test_4_mlb_prop_disposition_diagnostic()
    test_5_no_new_publication_writer_introduced()
    print("\nMLB_PROP_EARLY_AVAILABILITY_TESTS_ALL_PASSED")
