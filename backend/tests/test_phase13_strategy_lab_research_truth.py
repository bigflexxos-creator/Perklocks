"""Phase 13 — STRATEGY LAB 10X RESEARCH TRUTH invariants.

Lab is a RESEARCH surface — it must NEVER:
  * mutate `picks`, `settlement_events`, or `prediction_snapshots`
  * grade or re-grade a pick
  * fabricate outcomes
  * expose non-settled rows as if they were settled

  L1. Every Lab endpoint is READ-ONLY.  Source scan proves no writer
      calls (`db.*.insert`, `db.*.update_one`, `db.*.replace_one`,
      `db.*.delete_one`) in `lab_routes.py`.
  L2. Lab filters rows to canonical settled statuses only
      (`status ∈ {won, lost, push, void}`).  Live/pending/None rows
      are excluded from research aggregates.
  L3. Lab route module MUST expose `sample_size` on every metric
      response so the UI can badge low-N patterns.
  L4. Sport/market/side normalisers used by Lab match the canonical
      registry — no divergent identity keys.
"""
from __future__ import annotations
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

REPO_ROOT = pathlib.Path("/app/backend")
LAB_ROUTES = REPO_ROOT / "lab_routes.py"


def _lab_src() -> str:
    return LAB_ROUTES.read_text(encoding="utf-8")


def test_lab_routes_module_exists():
    assert LAB_ROUTES.exists()


# ── L1 ── read-only guarantee (source scan) ────────────────────
def test_lab_routes_no_direct_writers():
    """Lab must never call an insert / update / replace / delete on
    the DB.  Search the module source for those verbs on `db.` calls."""
    src = _lab_src()
    banned = [
        r"\bdb\.\w+\.insert_one\(",
        r"\bdb\.\w+\.insert_many\(",
        r"\bdb\.\w+\.update_one\(",
        r"\bdb\.\w+\.update_many\(",
        r"\bdb\.\w+\.replace_one\(",
        r"\bdb\.\w+\.delete_one\(",
        r"\bdb\.\w+\.delete_many\(",
        r"\bdb\.\w+\.find_one_and_update\(",
        r"\bdb\.\w+\.find_one_and_delete\(",
        r"\bdb\.\w+\.find_one_and_replace\(",
        r"\bdb\.\w+\.bulk_write\(",
    ]
    offenders: list[str] = []
    for pat in banned:
        for m in re.finditer(pat, src):
            # Grab a small context window for the failure message.
            start = max(0, m.start() - 20)
            offenders.append(src[start:m.end()])
    assert not offenders, (
        f"Lab is not read-only — found {len(offenders)} DB writers: "
        f"{offenders[:3]}"
    )


# ── L2 ── canonical settled status filter ──────────────────────
def test_lab_filters_to_canonical_settled_statuses():
    """Lab pipeline filters must reference the canonical settled
    status set.  A source-scan finds at least ONE occurrence of the
    `won/lost/push/void` filter (backtest/patterns/matchup-dna all
    aggregate over settled picks only)."""
    src = _lab_src()
    # Accept ONE of these equivalent filter shapes:
    canonical_filters = [
        '"won", "lost", "push", "void"',
        "'won', 'lost', 'push', 'void'",
        '"won", "lost"',   # some endpoints only tally W/L
        '{"$in": ["won", "lost", "push", "void"]}',
    ]
    assert any(f in src for f in canonical_filters), (
        "no canonical settled-status filter found in lab_routes.py"
    )


def test_lab_never_treats_pending_as_settled():
    """Lab must never include `pending` in a settled-status filter."""
    src = _lab_src()
    # Would be a red flag: a filter that mixes pending with settled.
    bad_patterns = [
        '"won", "lost", "pending"',
        '"pending", "won"',
        "'pending', 'won'",
    ]
    for b in bad_patterns:
        assert b not in src, f"Lab treats pending as settled: {b!r}"


# ── L3 ── sample_size on every research response ───────────────
def test_lab_exposes_sample_size():
    """Every research endpoint must include `sample_size` on its
    row-level or top-level response.  Grep for the key literal."""
    src = _lab_src()
    assert '"sample_size"' in src or "'sample_size'" in src or \
        'sample_size":' in src


# ── L4 ── canonical identity alignment with sport registry ────
def test_lab_sport_labels_align_with_authority_registry():
    """Sports appearing in Lab must exist in the sport model
    authority registry (or be legitimate research-only aliases —
    but the primary sport labels must match)."""
    from services.sport_model_authority import AUTHORITY
    src = _lab_src()
    # These sport strings appear in the endpoint dispatchers and
    # must all resolve.
    primary_sports = ("MLB", "NFL", "NBA", "CFB", "Soccer", "Tennis")
    for sport in primary_sports:
        # Must appear somewhere in Lab source (as sport routing).
        if sport in src:
            assert sport in AUTHORITY, (
                f"Lab references {sport!r} but sport authority "
                f"registry does not declare it — divergent identity")


# ── L5 ── read-only class in projection service is still read-only
def test_history_projection_service_still_read_only():
    """Lab often reuses the projection service.  Guard against
    accidental write-method additions."""
    from services.history_projection_service import HistoryProjectionService
    write_indicators = ("update_one", "insert_one", "delete_one",
                        "replace_one", "grade_pick", "settle")
    class_src = pathlib.Path(
        HistoryProjectionService.__module__.replace(".", "/") + ".py"
    )
    src = (REPO_ROOT / class_src).read_text(encoding="utf-8")
    for w in write_indicators:
        # Some appear in docstrings as forbidden-writer references —
        # ensure they aren't invoked (need `self.db["x"].w(` shape).
        pat = rf'self\.db\[[\'"][^\'"]+[\'"]\]\.{w}\('
        assert not re.search(pat, src), \
            f"HistoryProjectionService calls {w}()"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
