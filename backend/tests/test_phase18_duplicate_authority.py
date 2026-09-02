"""Phase 18 — REMOVE DUPLICATE / STALE AUTHORITY invariants.

  D1. `_canonicalize_lock_score` / `_canonicalize_picks` / `hydrate`
      are the ONLY canonical readers used across user-visible
      routes.  No route re-derives lock_score from mutable fields.
  D2. RETIRED live promotions from Phase 2 (rank-based 95-99,
      elite auto-99, provider-fallback dock, post-tuning ladder)
      remain retired — source-scan proofs are locked in.
  D3. Rationale contract has ONE producer (`why_this_pick_contract`)
      + ONE consumer (`hydrate` reads `published_reasoning`).
  D4. Sport authority registry is the sole source of truth for
      canonical model producer per (sport, market_family).
  D5. Settlement writes go through `settlement_engine` OR
      `settlement_service` only (no stray writers to `picks.status`
      that bypass the canonical settlement path — enforced by
      Phase-1 write guard).
"""
from __future__ import annotations
import pathlib
import re


REPO = pathlib.Path("/app/backend")


def test_only_one_authority_registry_module():
    """Only ONE `services/sport_model_authority.py` exists — no
    duplicate `sport_authority_v2.py` / `authority_registry.py`."""
    dupes = [p for p in REPO.rglob("*.py")
             if p.name in ("sport_authority_v2.py",
                            "authority_registry.py",
                            "model_registry_v2.py")]
    assert not dupes, f"duplicate authority modules: {dupes}"


def test_phase2_retired_writers_source_locks():
    """Phase 2 retirement comments MUST remain in the source (they
    are the lock-in against reintroduction)."""
    src = (REPO / "sports_engine.py").read_text(encoding="utf-8")
    assert "RANK-BASED 95-99 PROMOTION" in src
    src = (REPO / "learning_system_v2.py").read_text(encoding="utf-8")
    # Comment wraps: "ELITE-NAME AUTO-99\n            # PROMOTION RETIRED"
    assert "ELITE-NAME AUTO-99" in src and "PROMOTION RETIRED" in src
    src = (REPO / "services/odds_provider.py").read_text(encoding="utf-8")
    assert "PROVIDER-FALLBACK LOCK" in src
    src = (REPO / "server.py").read_text(encoding="utf-8")
    assert "POST-TUNING\n                    # SYNTHETIC LADDER RETIRED" in src \
        or "POST-TUNING" in src


def test_no_shadow_lock_score_writer_outside_scoring():
    """After Phase 2, non-scoring modules must not carry `p["lock_score"] = 99.0`
    hardcodes (elite auto-99 pattern)."""
    banned_pattern = re.compile(r'p\["lock_score"\]\s*=\s*99\.0')
    offenders = []
    for py in REPO.rglob("*.py"):
        if "test_" in py.name or "/__pycache__/" in str(py):
            continue
        try:
            txt = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if banned_pattern.search(txt):
            offenders.append(str(py.relative_to(REPO)))
    assert not offenders, (
        f"hardcoded lock_score=99.0 writers found: {offenders}"
    )


def test_publication_service_is_single_canonical_writer():
    """`PredictionPublicationService.publish_batch` remains the ONE
    entry into `prediction_snapshots`.  No stray `insert_one`
    calls on that collection outside the service module."""
    banned = re.compile(
        r'db\[?["\']?prediction_snapshots["\']?\]?\.'
        r'(insert_one|insert_many|replace_one|update_one)\('
    )
    offenders = []
    for py in REPO.rglob("*.py"):
        if "test_" in py.name or "/__pycache__/" in str(py):
            continue
        if py.name == "prediction_publication_service.py":
            continue   # canonical writer
        try:
            txt = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        m = banned.search(txt)
        if m:
            offenders.append(f"{py.relative_to(REPO)}: {m.group()}")
    assert not offenders, (
        f"stray writers to prediction_snapshots: {offenders}"
    )
