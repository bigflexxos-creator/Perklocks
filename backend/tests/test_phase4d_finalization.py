"""Phase 4D finalization — outer orchestrator wire-up guardrails.

Enforces:
  1. `_extract_nba_prop_candidates` is present and correctly parses
     bookmaker payloads.
  2. `_extract_cfb_prop_candidates` is present.
  3. Both precompute helpers are invoked from the outer per-event
     orchestrator (repository-level static assertion).
  4. Failure of NBA precompute records a marker in the ctx without
     raising to the caller.
  5. Failure of CFB precompute records a marker without raising.
  6. Precompute call sites are guarded by `sport == "NBA"` and
     `sport == "CFB"` so they run at most once per event, never once
     per prop.
  7. `nba_precompute_status` / `cfb_precompute_status` are recorded
     for observability.
"""
from __future__ import annotations
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_extract_nba_prop_candidates_shape():
    from sports_engine import _extract_nba_prop_candidates
    payload = {
        "bookmakers": [
            {"markets": [
                {"key": "player_points",
                 "outcomes": [
                    {"description": "LeBron James", "name": "Over",
                     "point": 24.5, "price": -110},
                    {"description": "LeBron James", "name": "Under",
                     "point": 24.5, "price": -110},
                    {"description": "Anthony Davis", "name": "Over",
                     "point": 22.5, "price": -115},
                 ]},
                {"key": "player_threes",
                 "outcomes": [
                    {"description": "Steph Curry", "name": "Over",
                     "point": 4.5, "price": -120},
                 ]},
            ]},
        ],
    }
    players, markets, lines_bp = _extract_nba_prop_candidates(payload)
    assert "LeBron James" in players
    assert "Anthony Davis" in players
    assert "Steph Curry" in players
    assert "player_points" in markets
    assert "player_threes" in markets
    assert ("lebron james", "player_points") in lines_bp
    # Over and Under for the same (player, market) both recorded.
    entries = lines_bp[("lebron james", "player_points")]
    assert (24.5, "Over") in entries
    assert (24.5, "Under") in entries


def test_extract_nba_prop_candidates_ignores_non_nba_markets():
    from sports_engine import _extract_nba_prop_candidates
    payload = {
        "bookmakers": [
            {"markets": [
                {"key": "h2h",     # game market, not a prop
                 "outcomes": [{"description": "Home", "name": "Home",
                                "price": -110}]},
            ]},
        ],
    }
    players, markets, lines_bp = _extract_nba_prop_candidates(payload)
    assert not players
    assert not markets


def test_extract_cfb_prop_candidates_shape():
    from sports_engine import _extract_cfb_prop_candidates
    payload = {
        "home_team": "Alabama Crimson Tide",
        "away_team": "Georgia Bulldogs",
        "bookmakers": [
            {"markets": [
                {"key": "player_pass_yds",
                 "outcomes": [
                    {"description": "Ty Simpson", "name": "Over",
                     "point": 250.5, "price": -115},
                 ]},
            ]},
        ],
    }
    cands = _extract_cfb_prop_candidates(payload)
    assert len(cands) == 1
    c = cands[0]
    assert c["player"] == "Ty Simpson"
    assert c["market"] == "player_pass_yds"
    assert c["line"] == 250.5
    assert c["player_team"] == "Alabama Crimson Tide"
    assert c["opponent"] == "Georgia Bulldogs"


def test_outer_orchestrator_wires_nba_and_cfb_precompute():
    """The per-event orchestrator MUST call both precompute helpers
    behind sport gates.  Static assertion at the source-code level so
    the wire-up cannot silently disappear."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    # NBA
    assert "precompute_nba_prop_factors as _nba_pre" in src
    assert "if sport == \"NBA\":" in src
    assert "nba_precompute_status" in src
    # CFB
    assert "precompute_cfb_factors as _cfb_pre" in src
    assert "if sport == \"CFB\":" in src
    assert "cfb_precompute_status" in src


def test_precompute_failure_never_raises_to_caller():
    """The wire-up wraps each precompute call in try/except and stashes
    an error marker.  One sport failing must not block the other."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    # NBA fallback markers.
    assert 'nba_precompute_status"] = f"error:' in src
    assert 'logger.warning("NBA props ctx build failed' in src
    # CFB fallback markers.
    assert 'cfb_precompute_status"] = f"error:' in src
    assert 'logger.warning("CFB props ctx build failed' in src
    # Book-follow drop-through markers exist in the emission path.
    assert "nba_engine_no_precompute" in src
    assert "cfb_engine_no_precompute" in src


def test_precompute_called_once_per_event_not_per_prop():
    """The precompute injection sits INSIDE the ``for ev in events``
    loop but OUTSIDE the ``for prop`` inner loop → one call per event.
    Static structural check: the precompute block appears BEFORE
    ``_props_picks_from_event`` is invoked.
    """
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    nba_pos = src.find("precompute_nba_prop_factors as _nba_pre")
    # Look for the actual INVOCATION of _props_picks_from_event, not its
    # definition.  The invocation lives inside `all_picks.extend(...)` in
    # the orchestrator loop.
    props_pos = src.find("all_picks.extend(_props_picks_from_event(")
    assert nba_pos > 0 and props_pos > 0
    assert nba_pos < props_pos, (
        "NBA precompute must be invoked BEFORE _props_picks_from_event "
        "is called in the per-event orchestrator loop")


def test_shared_mongo_client_used():
    """Both precompute call sites use `services.database.get_database()`
    → shared Mongo client (no new connection, no new client)."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    # Both blocks use get_database().
    assert src.count("from services.database import get_database") >= 2 or \
           src.count("services.database import get_database") >= 2 or \
           (src.count("get_database()") >= 3)


def test_no_new_collections_created():
    """The precompute call sites do NOT create new Mongo collections."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    # No `db.create_collection` or explicit new-collection markers in
    # the two precompute blocks.
    nba_pos = src.find("precompute_nba_prop_factors as _nba_pre")
    cfb_pos = src.find("precompute_cfb_factors as _cfb_pre")
    for start in (nba_pos, cfb_pos):
        window = src[start:start + 2000]
        assert "create_collection" not in window
        assert "db.picks.insert" not in window
        assert "db.picks.delete" not in window
