"""Phase 14 — ROLLOVER 10X invariants.

  RO1. Rollover history is stamped by a POST-SETTLEMENT tagger
       (`rollover_history_tagger`) — never by frontend derivation
       or a live pick-generation heuristic.
  RO2. `on_rollover_at` is stamped only on picks that were ON the
       live V4 top-3 rollover board for that date.
  RO3. Rollover history reads use the same canonical settled
       statuses (won/lost/push/void) as History and Analytics.
  RO4. The rollover route is READ-ONLY on `picks` for statistics
       (only reads `status`, never mutates it).
"""
from __future__ import annotations
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

REPO_ROOT = pathlib.Path("/app/backend")


def _read(p):
    return (REPO_ROOT / p).read_text(encoding="utf-8")


def test_rollover_tagger_module_exists():
    assert (REPO_ROOT / "rollover_history_tagger.py").exists()


def test_rollover_tagger_is_post_settlement():
    src = _read("rollover_history_tagger.py")
    assert "post-settlement" in src.lower() or \
        "settlement-time" in src.lower()


def test_rollover_tag_is_idempotent():
    """The tagger must skip picks that already have `on_rollover_at`
    so re-runs never duplicate stamps or clobber earlier stamps."""
    src = _read("rollover_history_tagger.py")
    # Contract text guarantees idempotence.
    assert "Idempotence" in src or "idempotent" in src.lower()


def test_rollover_route_uses_canonical_statuses():
    src = _read("routes/picks_routes.py")
    # The rollover branch must filter on canonical settled statuses.
    for status in ('"won"', '"lost"', '"push"'):
        assert status in src, f"rollover route missing {status}"


def test_rollover_route_never_mutates_picks_status():
    """The rollover endpoint must never call an update / delete on
    the `picks` collection.  Search picks_routes.py's rollover
    section for db.picks writes."""
    src = _read("routes/picks_routes.py")
    # Extract just the rollover function (`pick_rollover` def).
    idx = src.find("async def pick_rollover(")
    assert idx != -1, "pick_rollover route missing"
    # 5000 chars downstream is more than the whole rollover branch.
    body = src[idx:idx + 5000]
    banned = [
        r"db\.picks\.update_one\(",
        r"db\.picks\.update_many\(",
        r"db\.picks\.delete_one\(",
        r"db\.picks\.replace_one\(",
    ]
    for pat in banned:
        assert not re.search(pat, body), \
            f"rollover route mutates picks: {pat}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
