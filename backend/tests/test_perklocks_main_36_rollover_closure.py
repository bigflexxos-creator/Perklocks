"""PERKLOCKS MAIN 36 · P1 ROLLOVER CLOSURE — regression tests.

Locks in every mandate from the final P1 pass:

  P1.1  Only LIVE_FROZEN_SELECTION establishes Rollover membership.
  P1.2  Live + replay use the SAME pure selector (shared function).
  P1.3  Canonical event uniqueness (not display-string dedupe).
  P1.4  Stale H+R+RBI ban + -140/-110 odds dead-zone REMOVED.
  P1.5  Fail-closed on required Rollover authority checks.
  P1.6  Selector may legitimately return 3, 2, 1, or 0 picks.
  P1.7  Frozen selections carry selector_version + canonical metadata.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────
# P1.1 — LIVE_FROZEN vs candidate-only
# ─────────────────────────────────────────────────────────────────
def test_candidate_only_not_live_frozen_selection():
    from services.rollover_selector import is_live_frozen_selection
    # Generation-time candidate — passes filter but was never
    # stamped on the live route.  MUST NOT count as membership.
    candidate = {
        "id": "abc-1", "lock_score": 92, "book_odds": -140,
        "edge_percent": 4.5, "win_probability": 68.0,
        "market": "Total Runs Over 8.5",
        # No on_rollover_at, no rollover_frozen_source.
    }
    assert is_live_frozen_selection(candidate) is False


def test_live_frozen_selection_is_membership():
    from services.rollover_selector import is_live_frozen_selection
    frozen = {
        "id": "def-1", "on_rollover_at": "2026-06-30T12:00:00Z",
        "rollover_frozen_source": "picks_route_live",
        "rollover_selection_rank": 2,
    }
    assert is_live_frozen_selection(frozen) is True


def test_reconstructed_history_tag_is_not_live_membership():
    """A pick stamped by the settlement-time reconstruction pass
    (frozen_source='rollover_history_tagger') MUST NOT retroactively
    count as a live selection."""
    from services.rollover_selector import is_live_frozen_selection
    reconstructed = {
        "id": "ghi-1", "on_rollover_at": "2026-06-30T12:00:00Z",
        "rollover_frozen_source": "rollover_history_tagger",
        "rollover_selection_rank": 1,
    }
    assert is_live_frozen_selection(reconstructed) is False


# ─────────────────────────────────────────────────────────────────
# P1.2 — Shared selector for live + replay
# ─────────────────────────────────────────────────────────────────
def test_live_route_imports_shared_selector():
    """The live route MUST import & call the shared selector — no
    duplicated formula."""
    import inspect
    from routes import picks_routes
    src = inspect.getsource(picks_routes.pick_rollover)
    assert "from services.rollover_selector" in src
    assert "select_rollover_top(" in src
    # And the duplicate local _passes_v4 / _ev_score are removed.
    assert "def _passes_v4" not in src
    # A local `_ev_score` reference remains only as a lookup — no
    # local definition.
    assert "def _ev_score" not in src


def test_selector_is_pure_and_deterministic():
    """The shared selector is a pure function of its inputs — calling
    it twice with the same list returns the same picks."""
    from services.rollover_selector import select_rollover_top
    cands = [
        {"id": "a", "canonical_event_id": "e1", "market": "Strikeouts",
         "lock_score": 92, "book_odds": -145, "edge_percent": 3.0,
         "win_probability": 66.0, "sim_win_probability": 65.0},
        {"id": "b", "canonical_event_id": "e2", "market": "Total Runs",
         "lock_score": 90, "book_odds": -115, "edge_percent": 4.0,
         "win_probability": 62.0, "sim_win_probability": 60.0},
        {"id": "c", "canonical_event_id": "e3", "market": "Moneyline",
         "lock_score": 91, "book_odds": -160, "edge_percent": 2.5,
         "win_probability": 63.5, "sim_win_probability": 62.0},
    ]
    top1, _ = select_rollover_top(list(cands))
    top2, _ = select_rollover_top(list(cands))
    assert [p["id"] for p in top1] == [p["id"] for p in top2]


# ─────────────────────────────────────────────────────────────────
# P1.3 — Canonical event uniqueness
# ─────────────────────────────────────────────────────────────────
def test_canonical_event_id_dedupe_over_display_string():
    """Two candidates with different display 'event' strings but the
    SAME canonical_event_id must produce only ONE selected leg."""
    from services.rollover_selector import select_rollover_top
    cands = [
        {"id": "leg1", "canonical_event_id": "gm-42",
         "event": "Yankees @ Red Sox",
         "market": "Strikeouts", "lock_score": 94,
         "book_odds": -145, "edge_percent": 4.0,
         "win_probability": 70.0},
        {"id": "leg2", "canonical_event_id": "gm-42",
         # Same game, alternative display string
         "event": "NYY @ BOS",
         "market": "Total Runs Over 8.5", "lock_score": 92,
         "book_odds": -120, "edge_percent": 3.0,
         "win_probability": 65.0},
        {"id": "leg3", "canonical_event_id": "gm-51",
         "event": "Cubs @ Mets", "market": "Run Line",
         "lock_score": 91, "book_odds": -155, "edge_percent": 2.5,
         "win_probability": 63.0},
    ]
    top, _ = select_rollover_top(cands)
    # gm-42 appears once, gm-51 appears once → 2 legs total.
    ids = [p["id"] for p in top]
    assert len(ids) == 2
    events = {p.get("canonical_event_id") for p in top}
    assert events == {"gm-42", "gm-51"}


def test_missing_canonical_event_id_falls_back_gracefully():
    from services.rollover_selector import canonical_event_key
    # Prefer canonical_event_id
    assert canonical_event_key({"canonical_event_id": "x", "event": "y"}) == "x"
    # Fall through to event_id
    assert canonical_event_key({"event_id": "e", "event": "y"}) == "e"
    # Last resort: event display string (still stable within a session)
    assert canonical_event_key({"event": "Yankees @ Red Sox"}) == "Yankees @ Red Sox"


# ─────────────────────────────────────────────────────────────────
# P1.4 — Stale bans REMOVED
# ─────────────────────────────────────────────────────────────────
def test_stale_hrr_blacklist_removed():
    """A Hits + Runs + RBIs pick that passes every other rule must
    NOT be rejected by an H+R+RBI blacklist anymore."""
    from services.rollover_selector import passes_v5
    hrr = {"market": "Otto Lopez (MIA) Over 0.5 Hits + Runs + RBIs",
            "lock_score": 92, "book_odds": -145,
            "edge_percent": 3.5, "win_probability": 68.0}
    ok, reason = passes_v5(hrr)
    assert ok is True, f"H+R+RBI still banned: {reason!r}"


def test_stale_odds_dead_zone_removed():
    """Odds in the retired -140/-110 dead-zone are now accepted."""
    from services.rollover_selector import passes_v5
    for odds in (-139, -125, -111):
        p = {"market": "Total Runs Over 8.5",
              "lock_score": 91, "book_odds": odds,
              "edge_percent": 3.0, "win_probability": 66.0}
        ok, reason = passes_v5(p)
        assert ok is True, (
            f"odds={odds} still rejected — dead-zone must be retired "
            f"(reason={reason!r})"
        )


def test_non_settleable_markets_still_banned():
    """Goalscorer / assist family remains banned (settlement gap)."""
    from services.rollover_selector import passes_v5
    p = {"market": "Erling Haaland Anytime Goal Scorer",
          "lock_score": 92, "book_odds": +160,
          "edge_percent": 5.0, "win_probability": 68.0}
    ok, reason = passes_v5(p)
    assert ok is False
    assert reason == "non_settleable_market"


# ─────────────────────────────────────────────────────────────────
# P1.5 — Fail closed on authority checks
# ─────────────────────────────────────────────────────────────────
def test_no_bare_except_pass_on_canonical_eligibility():
    """The route MUST NOT wrap the canonical Locks eligibility gate
    in ``except Exception: pass``.  Fail-closed means bubble errors."""
    import inspect
    from routes import picks_routes
    src = inspect.getsource(picks_routes.pick_rollover)
    # Locate the eligibility gate segment and verify no swallow.
    idx = src.find("apply_canonical_locks_eligibility_gate")
    assert idx > 0
    segment = src[idx: idx + 500]
    assert "except Exception" not in segment, (
        "canonical eligibility gate is still wrapped in a swallowed "
        "exception — must fail closed per P1.5"
    )


# ─────────────────────────────────────────────────────────────────
# P1.6 — Legitimate abstention (0 picks)
# ─────────────────────────────────────────────────────────────────
def test_selector_returns_zero_when_no_candidate_qualifies():
    from services.rollover_selector import select_rollover_top
    weak = [
        {"id": "x", "market": "Strikeouts", "lock_score": 70,
         "book_odds": -110, "edge_percent": -1, "win_probability": 55}
    ]
    top, reasons = select_rollover_top(weak)
    assert top == [], "NO PICK > FORCED PICK — must return empty"
    assert reasons, "reject reasons should be surfaced"


def test_selector_can_return_1_2_or_3():
    from services.rollover_selector import select_rollover_top
    # Only 1 qualifier.
    one = [{"id": "1", "canonical_event_id": "e1",
             "market": "Strikeouts", "lock_score": 93,
             "book_odds": -145, "edge_percent": 3.0,
             "win_probability": 68.0}]
    assert len(select_rollover_top(one)[0]) == 1
    # 2 qualifiers.
    two = one + [{"id": "2", "canonical_event_id": "e2",
                   "market": "Total Runs", "lock_score": 91,
                   "book_odds": -125, "edge_percent": 2.5,
                   "win_probability": 63.0}]
    assert len(select_rollover_top(two)[0]) == 2
    # 3+ qualifiers cap at 3.
    three_plus = two + [
        {"id": f"{i+3}", "canonical_event_id": f"e{i+3}",
         "market": "Run Line", "lock_score": 90,
         "book_odds": -140, "edge_percent": 2.0,
         "win_probability": 62.0}
        for i in range(3)
    ]
    assert len(select_rollover_top(three_plus)[0]) == 3


# ─────────────────────────────────────────────────────────────────
# P1.7 — Prospective truth metadata
# ─────────────────────────────────────────────────────────────────
def test_freeze_metadata_includes_selector_version_and_canonical_event():
    from services.rollover_selector import freeze_metadata, SELECTOR_VERSION
    meta = freeze_metadata(
        rank=2, stamped_at="2026-06-30T12:00:00Z",
        canonical_event_id="gm-42",
    )
    assert meta["on_rollover_at"] == "2026-06-30T12:00:00Z"
    assert meta["rollover_frozen_source"] == "picks_route_live"
    assert meta["rollover_selection_rank"] == 2
    assert meta["rollover_selector_version"] == SELECTOR_VERSION
    assert meta["rollover_canonical_event_id"] == "gm-42"


def test_live_route_stamps_canonical_event_id_and_selector_version():
    import inspect
    from routes import picks_routes
    src = inspect.getsource(picks_routes.pick_rollover)
    assert "canonical_event_key" in src
    assert "freeze_metadata" in src
    assert "SELECTOR_VERSION" in src or "_SELECTOR_VERSION" in src


def test_only_live_frozen_selections_count_toward_prospective_record():
    """A prospective-record helper (or any consumer) MUST filter via
    ``is_live_frozen_selection`` so candidate-only rows are excluded."""
    from services.rollover_selector import is_live_frozen_selection
    rows = [
        # Real live selection.
        {"id": "live", "on_rollover_at": "2026-06-30T12:00:00Z",
         "rollover_frozen_source": "picks_route_live",
         "rollover_selection_rank": 1},
        # Historical reconstruction — MUST be excluded.
        {"id": "reconstruction", "on_rollover_at": "2026-06-30T12:00:00Z",
         "rollover_frozen_source": "rollover_history_tagger",
         "rollover_selection_rank": 1},
        # Candidate only — MUST be excluded.
        {"id": "cand", "lock_score": 92, "book_odds": -145,
         "edge_percent": 3.0, "win_probability": 68.0},
    ]
    live_only = [r for r in rows if is_live_frozen_selection(r)]
    assert [r["id"] for r in live_only] == ["live"]
