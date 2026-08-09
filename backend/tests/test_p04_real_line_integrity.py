"""P0-4 (2026-08-11) — Real-Line Integrity.

Ensures NO writer manufactures sportsbook data (book_odds /
implied_probability / edge_percent) when there is no verified US
sportsbook line available.  Real signal (win_probability + lock_score
+ grade) is PRESERVED so a strong model-only pick can still appear
on Locks with the odds/edge shown as unavailable.

Covered writers audited:
  * `soccer/predictor.py`             (soccer_v1)
  * `soccer_hot_scorers.py`           (hot-scorer AGS)
  * `sportdb_player_scorer.py`        (sportdb_scorer_v1)
  * `thesportsdb_scorer.py`           (thesportsdb synth scorer)
  * `ufc_espn_ingest.py`              (UFC ESPN pregame)
  * `uefa_espn_ingest.py`             (UEFA/CFB ESPN pregame + form)
  * `tennis_extra/picks.py`           (P0-3 fixed already, regression check)
  * `sports_engine.py`                (CSL ESPN leaderboard AGS)
  * `pick_validator.py`               (model-only edge normalisation)

Contract:
  * book_odds:            None
  * implied_probability:  None
  * edge_percent:         None
  * lock_score:           preserved
  * grade / confidence:   preserved
  * no_real_book_line:    True
  * model_only:           True (or is_model_only for legacy variants)
  * model_fair_odds:      Optional[int]  (model's own fair line, stored
                                          separately, NEVER named as book)
"""
from __future__ import annotations

import pathlib


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_BACKEND_ROOT / rel).read_text()


# ── 1. Writer audit — no manufactured book data ─────────────────────
def test_soccer_predictor_no_manufactured_book_data():
    src = _read("soccer/predictor.py")
    # Fair-odds branch must NOT stamp book_odds=int(fair_odds) OR
    # edge_percent=0.0 (both were the P0-4 loci).
    else_idx = src.find("no_real_book_line = True")
    assert else_idx > 0
    window = src[max(0, else_idx - 400):else_idx + 400]
    assert "book_odds         = None" in window
    assert "edge_pct          = None" in window
    assert "book_odds         = int(fair_odds)" not in window
    # Over-1.5 synth branch — same requirement.
    o15_idx = src.find("Total Goals Over 1.5")
    assert o15_idx > 0
    # Search from the pick doc that FOLLOWS the model math.
    o15_doc_idx = src.find('"market":            "Total Goals Over 1.5"', o15_idx)
    assert o15_doc_idx > 0
    o15_win = src[o15_doc_idx:o15_doc_idx + 1500]
    assert '"book_odds":         None' in o15_win
    assert '"edge_percent":      None' in o15_win
    assert '"no_real_book_line": True' in o15_win


def test_soccer_hot_scorers_no_manufactured_book_data():
    src = _read("soccer_hot_scorers.py")
    assert '"book_odds":       None' in src
    assert '"edge_percent":    None' in src
    assert '"no_real_book_line": True' in src
    # Real signal preserved.
    assert '"lock_score":      lock' in src
    # Old manufactured values must be gone.
    assert '"book_odds":       fair_odds' not in src
    assert '"edge_percent":    0.0' not in src


def test_sportdb_player_scorer_no_manufactured_book_data():
    src = _read("sportdb_player_scorer.py")
    assert '"book_odds": None' in src
    assert '"edge_percent": None' in src
    assert '"no_real_book_line": True' in src
    assert '"model_only": True' in src
    # Fair odds retained separately for reference.
    assert '"model_fair_odds": implied_odds' in src


def test_thesportsdb_scorer_no_manufactured_book_data():
    src = _read("thesportsdb_scorer.py")
    assert '"edge_percent": None' in src
    # Book odds no longer set to the model's fair number as if it were
    # a bookmaker line.
    assert '"book_odds": None' in src
    assert '"no_real_book_line": True' in src
    assert '"model_only": True' in src


def test_ufc_espn_no_manufactured_book_data():
    src = _read("ufc_espn_ingest.py")
    idx = src.find('"no_real_book_line": True')
    assert idx > 0
    win = src[max(0, idx - 400):idx + 400]
    assert '"book_odds":        None' in win
    assert '"edge_percent":     None' in win
    assert '"model_fair_odds":  fair_odds' in win


def test_uefa_espn_no_manufactured_book_data():
    """Both UEFA ESPN branches (double-chance + form-derived ML)."""
    src = _read("uefa_espn_ingest.py")
    # Count the P0-4 markers — must appear in both branches.
    n = src.count('"no_real_book_line": True')
    assert n >= 2, f"expected ≥2 P0-4 markers, found {n}"
    # Book odds nulled in both branches.
    assert src.count('"book_odds":        None') >= 2
    assert src.count('"edge_percent":     None') >= 2


def test_tennis_extra_no_manufactured_book_data_regression():
    """Regression check that P0-3's fix is still in place."""
    src = _read("tennis_extra/picks.py")
    assert '"no_real_book_line": no_real_book_line,' in src
    # No `edge_pct = 0.0` in the fallback branch.
    assert "edge_pct            = 0.0" not in src


def test_csl_espn_leaderboard_no_manufactured_book_data():
    src = _read("sports_engine.py")
    idx = src.find('"synthetic_source": "csl_espn_leaderboard"')
    assert idx > 0
    window = src[max(0, idx - 1400):idx]
    assert '"book_odds": book_odds' in window
    assert 'book_odds = None' in window
    assert '"edge_percent": None' in window
    assert '"no_real_book_line": True' in window
    assert '"model_fair_odds": _synth_book' in window


def test_pick_validator_no_longer_pins_model_only_edge_to_zero():
    """pick_validator used to clamp model-only edges to 0.0 — that
    presented as "0% edge" on the frontend.  Now the honest None
    is surfaced instead."""
    src = _read("pick_validator.py")
    # New markers.
    assert 'edge_null_reason' in src
    assert 'p["edge_percent"] = None' in src
    # Old marker should be gone.
    assert 'edge_zeroed_reason' not in src
    assert 'p["edge_percent"] = 0.0' not in src or (
        # (One legitimate residual: the book-anchored branch keeps 0.0
        # because those picks REALLY have edge ≈ 0.  That block is
        # explicitly documented.)
        'is_book_anchored' in src
    )


# ── 2. End-to-end contract via central eligibility ──────────────────
def test_model_only_pick_over_85_reaches_locks_with_null_odds_and_edge():
    """A model-only pick with lock=90, edge=None, book_odds=None MUST
    still qualify for the main Locks board (>85 contract) — real-line
    integrity does NOT remove strong model picks."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {
        "lock_score": 90.0,
        "edge_percent": None,
        "book_odds": None,
        "implied_probability": None,
        "no_real_book_line": True,
        "model_only": True,
        "grade": "Strong Lock",
        "confidence": "Very High",
    }
    assert is_main_board_eligible(p) is True


def test_model_only_pick_at_85_still_off_board():
    """>85 contract still applies — model-only doesn't bypass it."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {
        "lock_score": 85.0,
        "edge_percent": None,
        "no_real_book_line": True,
        "model_only": True,
    }
    assert is_main_board_eligible(p) is False


def test_model_only_pick_publishes_with_null_edge_and_odds():
    """The publication contract (P0-1) already accepts None for edge
    and Optional[int] for book_odds.  Regression check that a
    model-only pick round-trips cleanly."""
    import asyncio, os, uuid
    from datetime import datetime, timezone, timedelta
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "lockscore_db")]
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        pid = "p04modelonly_" + uuid.uuid4().hex[:12]
        pick = {
            "id": pid,
            "sport": "Soccer",
            "league": "CSL",
            "event": "Shanghai Port vs Beijing Guoan",
            "event_time": (datetime.now(timezone.utc)
                           + timedelta(hours=6)).isoformat(),
            "market": "Wu Lei - Anytime Goal Scorer",
            "selection": "Wu Lei to Score",
            "win_probability": 62.5,
            "lock_score": 92.0,
            "grade": "Elite Lock",
            "confidence": "Very High",
            "book_odds": None,
            "implied_probability": None,
            "edge_percent": None,
            "no_real_book_line": True,
            "model_only": True,
            "is_model_only": True,
            "model_fair_odds": -178,
            "source": "csl_espn_leaderboard",
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.prediction_snapshots.delete_many({"prediction_id": pid})
        await db.picks.insert_one(pick)
        try:
            await pub.publish(pick, dual_write=True,
                              publication_source="canonical_pipeline")
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            # book_odds and edge_percent stay None after dual-write.
            assert after["book_odds"] is None
            assert after["edge_percent"] is None
            # Lock, grade, win_probability preserved.
            assert after["lock_score"] == 92.0
            assert after["grade"] == "Elite Lock"
            assert abs(float(after["win_probability"]) - 62.5) < 1e-6
            # Snapshot mirrors.
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pid}, {"_id": 0})
            assert snap["published_edge"] is None
            assert snap["published_odds"] is None
            assert snap["published_lock_score"] == 92.0
        finally:
            await db.picks.delete_many({"id": pid})
            await db.prediction_snapshots.delete_many({"prediction_id": pid})
    asyncio.run(go())


# ── 3. Contract guarantees — no scoring changes ────────────────────
def test_locks_contract_still_gt_85():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


def test_99_lock_concept_intact_for_model_only():
    """A model-only pick at lock=99 with null odds/edge must still
    read as an Elite Lock in the frontend."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {
        "lock_score": 99.0, "grade": "Elite Lock",
        "edge_percent": None, "book_odds": None,
        "no_real_book_line": True, "model_only": True,
    }
    assert is_main_board_eligible(p) is True
