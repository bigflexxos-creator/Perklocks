"""Item P0-E · Round 4 — End-to-end canonical probability consumer proof.

For a live published pick with a raw-vs-published probability mismatch
(e.g. Notre Dame CFB Total pick showing win_probability=98.2 in the
raw DB doc but published_probability=0.7629), verify that:

  1. Every consumer that reads through ``_canonicalize_lock_score`` /
     ``hydrate()`` sees the canonical 0.7629 → 76.29% legacy alias.
  2. ``canonical_final_probability(pick)`` returns 0.7629 for both
     the RAW doc and the HYDRATED doc.
  3. ``published_edge`` (26.29) is preserved via ``edge_percent``
     alias — never the raw pre-shrinkage 48.2.
  4. Fusion promotion authority: the hydrate mirror is the ONLY
     legal probability writer downstream of publication.  No
     consumer recomputes a competing probability.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _fake_published_cfb_pick():
    """Mirror of the actual Notre Dame CFB pick observed in the DB
    (id 7856b13c-4767-5807-9393-f8b6b2d8c980) — models the exact
    raw-vs-published divergence that appears on real picks."""
    return {
        "id":                     "test-cfb-nd-1",
        "sport":                  "CFB",
        "market":                 "Total Points Over 46.5",
        "selection":              "Over",
        "line":                   46.5,
        "home_team":              "Notre Dame Fighting Irish",
        "away_team":              "Wisconsin Badgers",
        # Raw engine — stale on the raw doc, still 98.2%.
        "win_probability":        98.2,
        "edge_percent":           48.2,
        # Canonical publication authority — shrunk / calibrated.
        "published_probability":  0.7629,
        "published_edge":         26.29,
        "published_lock_score":   98.0,
        "published_odds":         -110,
        "published_line":         46.5,
        "published_side":         "Over",
        "published_grade":        "Elite Lock",
        "published_at":           "2026-06-06T20:00:00+00:00",
        "book_odds":              -110,
        "lock_score":             98.0,
        "model_probability":      None,
        "publication_source":     "canonical_pipeline",
    }


def test_hydrate_aliases_canonical_probability_to_legacy_wp():
    from services.published_prediction_reader import hydrate
    p = _fake_published_cfb_pick()
    h = hydrate(p)
    # Canonical 0.7629 → legacy alias 76.29%.
    assert h["win_probability"] == 76.29
    assert h["edge_percent"] == 26.29
    # Immutable canonical fields survive.
    assert h["published_probability"] == 0.7629
    assert h["published_edge"] == 26.29
    assert h["_prediction_source"] == "snapshot"


def test_canonical_accessor_agrees_on_raw_and_hydrated_pick():
    from services.canonical_probability import (
        canonical_final_probability,
        canonical_final_probability_source,
    )
    from services.published_prediction_reader import hydrate
    p = _fake_published_cfb_pick()
    # Raw doc: canonical helper reads published_probability=0.7629
    # (model_probability is None on this pick).
    assert canonical_final_probability(p) == 0.7629
    assert canonical_final_probability_source(p) == "published_probability"
    # Hydrated doc — still 0.7629.
    h = hydrate(p)
    assert canonical_final_probability(h) == 0.7629


def test_no_downstream_authority_uses_raw_wp_98_after_hydrate():
    """Verifies that the hydrate boundary DELETES the 98.2 stale value
    from the display layer.  After hydrate the ``win_probability``
    field alias is exactly the canonical published value scaled to
    percentage — never the raw uncalibrated 98.2."""
    from services.published_prediction_reader import hydrate
    p = _fake_published_cfb_pick()
    h = hydrate(p)
    assert h["win_probability"] != 98.2
    assert h["win_probability"] == 76.29
    # edge_percent alias must also NOT be the raw 48.2.
    assert h["edge_percent"] != 48.2
    assert h["edge_percent"] == 26.29


def test_fusion_promotion_policy_is_calibration_bounded():
    """The canonical accessor + published_probability contract is the
    ONLY probability authority.  Fusion/decorator outputs are consumed
    as CANDIDATES upstream of publication; once published, the value
    is frozen.

    We verify this by asserting the canonical accessor refuses to
    read from ``fusion_probability`` / ``sim_probability`` /
    ``implied_probability`` — those inputs are NOT authority."""
    from services.canonical_probability import canonical_final_probability
    p = {
        "fusion_probability":  0.95,
        "sim_probability":     0.90,
        "implied_probability": 0.50,
    }
    assert canonical_final_probability(p) is None


def test_consumer_parity_lock_score_edge_and_wp_all_coherent():
    """Consumer parity: after hydrate, lock_score, edge_percent, and
    win_probability are all self-consistent with the published
    snapshot.  A Lock Score decorator or downstream consumer that
    reads these three fields will see one coherent story."""
    from services.published_prediction_reader import hydrate
    from services.canonical_probability import canonical_final_probability
    p = _fake_published_cfb_pick()
    h = hydrate(p)
    canonical = canonical_final_probability(h)
    assert canonical is not None
    # Legacy alias must equal canonical scaled to percentage.
    assert abs(h["win_probability"] - canonical * 100.0) < 0.01
    # Edge alias must equal published_edge (not the pre-shrinkage 48.2).
    assert h["edge_percent"] == h["published_edge"]
    # Lock score preserved.
    assert h["lock_score"] == 98.0


if __name__ == "__main__":
    test_hydrate_aliases_canonical_probability_to_legacy_wp()
    test_canonical_accessor_agrees_on_raw_and_hydrated_pick()
    test_no_downstream_authority_uses_raw_wp_98_after_hydrate()
    test_fusion_promotion_policy_is_calibration_bounded()
    test_consumer_parity_lock_score_edge_and_wp_all_coherent()
    print("OK — end-to-end canonical probability consumer parity verified.")
