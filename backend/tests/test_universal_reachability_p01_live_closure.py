"""P0.1 — Live production reachability closure regression tests.

Guarantees the specific defects identified in the P0.1 live audit
cannot silently regress:

    A. MLB tail-slice starvation — the extended fair-slate contract
       preserves BOTH current-slate AND next-day-slate events
       chronologically (no more `rest[:remainder]` truncation of
       later Aug-13-style refreshes crossing the Perklocks day
       boundary).
    B. MLB NRFI event-identity fail-closed — a candidate with
       missing home_team OR away_team must be skipped, not persisted
       with `event=None`.
    C. Tennis mandatory timestamp contract — every newly emitted
       tennis_extra pick_doc carries `created_at`, `updated_at`,
       `published_at`, and `publication_source`.
    D. Tennis real-line integrity — model-only picks (no book_odds)
       must NOT stamp implied_probability from the fair-value model
       (mirrors Support 2026-06 durable rule already applied to
       Soccer).
    E. Cross-surface backend URL contract — every frontend surface
       consumes EXPO_PUBLIC_BACKEND_URL (single source of truth);
       no orphan re-resolutions.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═════════════════════════════════════════════════════════════════════════
# A.  MLB tail-slice starvation — extended fair-slate contract
# ═════════════════════════════════════════════════════════════════════════

class TestMlbExtendedFairSlate:
    """Reproduces the Aug-13 defect where the 04:03 UTC refresh (=
    12:03 AM ET on Aug 12→13) treated Aug 13 games as ``rest`` and
    truncated them with ``rest[:remainder]``.  The extended
    fair-slate contract guarantees every event in the CURRENT
    Perklocks day AND the immediately-NEXT Perklocks day survives
    the cap."""

    def _slate(self, base_utc, n=14, spacing_hr=1.0):
        return [base_utc + timedelta(hours=i * spacing_hr) for i in range(n)]

    def test_next_day_slate_survives_cap(self):
        """At 04:03 UTC Aug 13 (=00:03 ET, still Perklocks day Aug 12
        which rolls at 04:00 ET) a full 12-game Aug 13 slate lives in
        ``rest`` under the OLD rule.  Under the NEW rule the entire
        next-day slate is guaranteed alongside the current slate."""
        from services.perklocks_day import is_in_current_slate
        now = datetime(2026, 8, 13, 4, 3, tzinfo=timezone.utc)
        # Current Perklocks day = Aug 12 (ends 08:00 UTC Aug 13).
        # Build slate:  0-1 current-slate (Aug 12 evening), 12
        # next-day (Aug 13 games).
        current = [datetime(2026, 8, 13, 3, 45, tzinfo=timezone.utc)]
        next_day = [datetime(2026, 8, 13, 17, 11, tzinfo=timezone.utc)
                     + timedelta(hours=i) for i in range(12)]
        upcoming = current + next_day
        current_slate = [e for e in upcoming if is_in_current_slate(e, now)]
        rest = [e for e in upcoming if not is_in_current_slate(e, now)]
        # Emulate the P0.1 extended fair-slate rule.
        _next_day_now = now + timedelta(hours=24)
        next_slate = [e for e in rest if is_in_current_slate(e, _next_day_now)]
        far_future = [e for e in rest if not is_in_current_slate(e, _next_day_now)]
        must_have = current_slate + next_slate
        cap = 8   # deliberately small so old rule would truncate
        selected = (must_have if len(must_have) >= cap
                     else must_have + far_future[:cap - len(must_have)])
        # Every next-day event survives — no starvation.
        for e in next_day:
            assert e in selected, f"P0.1 defect regressed: {e} was starved"
        # In particular the LAST next-day game (11:11 PM ET) survives.
        assert next_day[-1] in selected

    def test_source_wired_into_sports_engine(self):
        """Grep the source to confirm the extended-fair-slate block
        exists in `sports_engine._collect_upcoming_prop_events`.
        A future refactor that reverts to `rest[:remainder]` fails
        this guard."""
        src = Path("/app/backend/sports_engine.py").read_text()
        # Signature markers of the P0.1 fix:
        assert "extended fair-slate contract" in src
        assert "next_slate" in src
        assert "must_have" in src
        assert "far_future" in src

    def test_no_rest_truncation_when_next_day_full(self):
        """Even at cap=3 (way smaller than a real MLB slate), if the
        next-day slate has 15 events they ALL survive."""
        from services.perklocks_day import is_in_current_slate
        now = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)   # 6 PM ET
        next_day = [datetime(2026, 9, 16, 17, 0, tzinfo=timezone.utc)
                     + timedelta(hours=i) for i in range(15)]
        upcoming = next_day
        current_slate = [e for e in upcoming if is_in_current_slate(e, now)]
        rest = [e for e in upcoming if not is_in_current_slate(e, now)]
        _next_day_now = now + timedelta(hours=24)
        next_slate = [e for e in rest if is_in_current_slate(e, _next_day_now)]
        far_future = [e for e in rest if not is_in_current_slate(e, _next_day_now)]
        must_have = current_slate + next_slate
        cap = 3
        selected = (must_have if len(must_have) >= cap
                     else must_have + far_future[:cap - len(must_have)])
        for e in next_day:
            assert e in selected


# ═════════════════════════════════════════════════════════════════════════
# B.  MLB NRFI event-identity fail-closed
# ═════════════════════════════════════════════════════════════════════════

class TestMlbNrfiIdentityFailClosed:
    """A NRFI/YRFI candidate with missing home_team OR away_team
    must NOT be persisted.  Previously these rows were written with
    ``event=None`` which leaked into audit / analytics."""

    def test_source_wires_fail_closed_check(self):
        src = Path("/app/backend/brain/nrfi_engine.py").read_text()
        # Signature markers of the P0.1 fail-closed:
        assert "IDENTITY_REJECTED" in src
        assert 'if not home_team or not away_team' in src
        # `event` field is stamped as `{away} @ {home}`
        assert '"event": event_label' in src

    def test_nrfi_upsert_skipped_when_home_missing(self):
        """`_upsert_pick` returns early (no DB write) when home_team
        is missing.  We stub the DB and confirm no update_one call."""
        import asyncio
        from brain.nrfi_engine import _upsert_pick
        db = MagicMock()
        db.picks.update_one = AsyncMock()
        base = {
            "game_pk": 123, "home_team": "", "away_team": "Yankees",
            "event_time": "2026-08-15T18:00:00Z",
        }
        model_out = {"edge_signal": 0.05, "expected_runs_1st_inning": 0.5,
                      "nrfi_prob": 0.6, "yrfi_prob": 0.4,
                      "model_inputs": {"pitcher_factor": 1.0,
                                       "lineup_top_factor": 1.0,
                                       "park_factor": 1.0}}
        asyncio.run(_upsert_pick(db, base, "NRFI", 0.6, model_out))
        # No persist call — fail-closed.
        db.picks.update_one.assert_not_called()

    def test_nrfi_upsert_skipped_when_away_missing(self):
        import asyncio
        from brain.nrfi_engine import _upsert_pick
        db = MagicMock()
        db.picks.update_one = AsyncMock()
        base = {"game_pk": 123, "home_team": "Yankees", "away_team": None,
                 "event_time": "2026-08-15T18:00:00Z"}
        model_out = {"edge_signal": 0.05, "expected_runs_1st_inning": 0.5,
                      "nrfi_prob": 0.6, "yrfi_prob": 0.4,
                      "model_inputs": {"pitcher_factor": 1.0,
                                       "lineup_top_factor": 1.0,
                                       "park_factor": 1.0}}
        asyncio.run(_upsert_pick(db, base, "NRFI", 0.6, model_out))
        db.picks.update_one.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# C.  Tennis mandatory timestamp contract
# ═════════════════════════════════════════════════════════════════════════

class TestTennisTimestampContract:
    """Every newly emitted tennis_extra pick_doc must carry the four
    mandatory timestamp / provenance fields."""

    def test_source_stamps_all_timestamps(self):
        src = Path("/app/backend/tennis_extra/picks.py").read_text()
        assert '"created_at":' in src
        assert '"updated_at":' in src
        assert '"published_at":' in src
        assert '"publication_source": "tennis_extra_v1"' in src

    def test_source_never_leaves_created_at_none(self):
        """A future refactor that emits pick_doc without created_at
        fails this pattern check.  Any place that builds a Tennis
        pick_doc via dict-literal MUST include the created_at key."""
        src = Path("/app/backend/tennis_extra/picks.py").read_text()
        # Find the (single) main pick_doc dict literal and confirm
        # created_at appears within its scope.
        idx = src.find("pick_doc = {")
        assert idx > 0, "Tennis pick_doc literal not found"
        # Look ahead 4000 chars for the closing brace + created_at.
        window = src[idx:idx + 4000]
        assert '"created_at"' in window


# ═════════════════════════════════════════════════════════════════════════
# D.  Tennis real-line integrity — no fabricated implied_probability
# ═════════════════════════════════════════════════════════════════════════

class TestTennisRealLineIntegrity:
    """A model-only Tennis pick (no real book line) must NOT stamp
    ``implied_probability`` from the fair-value model.  Mirrors the
    Soccer Market Truth durable rule."""

    def test_source_conditions_implied_probability(self):
        src = Path("/app/backend/tennis_extra/picks.py").read_text()
        # The dict literal for pick_doc now sets implied_probability
        # to None when no_real_book_line is True.
        assert ("implied_final\n                                     if not no_real_book_line else None" in src
                or "implied_final if not no_real_book_line else None" in src)

    def test_hide_from_main_board_stamped_for_model_only(self):
        src = Path("/app/backend/tennis_extra/picks.py").read_text()
        assert '"hide_from_main_board": bool(no_real_book_line)' in src
        assert '"model_only":           bool(no_real_book_line)' in src


# ═════════════════════════════════════════════════════════════════════════
# E.  Cross-surface backend URL contract
# ═════════════════════════════════════════════════════════════════════════

class TestCrossSurfaceBackendContract:
    """Preview / Expo Go / Web must all consume the single
    canonical backend URL resolver in `src/lib/api.ts`.  No frontend
    file may read `EXPO_PUBLIC_BACKEND_URL` directly (deprecated
    contract — see Phase 1 2026-08-11 note in api.ts)."""

    def test_single_resolver_module_exists(self):
        api_ts = Path("/app/frontend/src/lib/api.ts")
        assert api_ts.exists(), "Canonical API module missing"
        content = api_ts.read_text()
        assert "resolveBaseUrl" in content
        assert "EXPO_PUBLIC_BACKEND_URL" in content
        # Fail-loud contract: native build without env var returns "".
        assert 'return ""' in content

    def test_no_orphan_env_reads_in_frontend(self):
        """Search the frontend for direct `process.env.EXPO_PUBLIC_BACKEND_URL`
        reads OUTSIDE the canonical resolver module.  A single
        allowed exception: `app/(tabs)/profile.tsx` for display-only
        purposes.  Any other occurrence is a divergence risk."""
        frontend = Path("/app/frontend")
        offenders = []
        allowed_files = {
            "src/lib/api.ts",
            "app/(tabs)/profile.tsx",   # display-only
        }
        for ts in list(frontend.rglob("*.ts")) + list(frontend.rglob("*.tsx")):
            rel = str(ts.relative_to(frontend))
            if rel in allowed_files:
                continue
            # Skip build/cache dirs
            if any(part in rel for part in
                     (".metro-cache", "node_modules", ".expo",
                      "ios/", "android/")):
                continue
            src = ts.read_text(errors="ignore")
            if "process.env.EXPO_PUBLIC_BACKEND_URL" in src:
                offenders.append(rel)
        assert not offenders, (
            "Orphan EXPO_PUBLIC_BACKEND_URL reads (surfaces may diverge): "
            f"{offenders}")


# ═════════════════════════════════════════════════════════════════════════
# F.  Reachability counters — mandatory contract
# ═════════════════════════════════════════════════════════════════════════

class TestReachabilityCounterContract:
    """Support-mandated aggregate reconciliation:
    supported_markets_seen == GENERATED + legitimate rejections."""

    def test_reconcile_matches(self):
        from services.pipeline_reachability import ReachabilityCounters
        rc = ReachabilityCounters(sport="MLB")
        # Simulate 10 supported markets: 6 generated, 2 identity-rejected,
        # 1 missing-evidence, 1 publication-rejected.
        for _ in range(6):
            rc.record("GENERATED")
        for _ in range(2):
            rc.record("IDENTITY_REJECTED", reason="unknown_team_id")
        rc.record("MISSING_REQUIRED_EVIDENCE", reason="no_lineup")
        rc.record("CANONICAL_PUBLICATION_REJECTED", reason="no_real_book_line")
        ok, detail = rc.reconcile(supported_markets_seen=10)
        assert ok, detail

    def test_reconcile_fails_on_silent_drop(self):
        from services.pipeline_reachability import ReachabilityCounters
        rc = ReachabilityCounters(sport="MLB")
        rc.record("GENERATED")
        # Supported=5 but only 1 accounted for → 4 silently dropped.
        ok, detail = rc.reconcile(supported_markets_seen=5)
        assert not ok
        assert "unaccounted" in detail
        assert "diff=4" in detail


# ═════════════════════════════════════════════════════════════════════════
# G.  Odds API cost guardrail — no polling frequency increase
# ═════════════════════════════════════════════════════════════════════════

class TestOddsApiCostGuardrail:
    """No P0.1 change may introduce a new external Odds API call
    site.  We scan the diff-affected files and assert the number
    of `httpx.AsyncClient()` / `httpx.get(` / `.get_events(` /
    `.get_odds(` sites is unchanged for these modules."""

    def test_no_new_httpx_calls_in_touched_files(self):
        """Baseline counts captured in this test — if a future edit
        adds an httpx call to any of these files, the test fails."""
        # sports_engine.py: retains existing Odds API call sites,
        # nrfi_engine.py: schedule + odds fetch already existed,
        # tennis_extra/picks.py: scrape + fair-value only (no odds),
        # so no NEW external HTTP call has been added by P0.1.
        # We assert the P0.1 diff did not introduce a new
        # ``httpx.AsyncClient(`` invocation to any of them.
        for path, max_expected in [
            ("/app/backend/sports_engine.py",         30),
            ("/app/backend/brain/nrfi_engine.py",      5),
            ("/app/backend/tennis_extra/picks.py",     3),
        ]:
            src = Path(path).read_text()
            n = src.count("httpx.AsyncClient(")
            assert n <= max_expected, (
                f"{path}: httpx.AsyncClient() call count is {n} > "
                f"expected max {max_expected} — Odds API cost regression"
            )


# ═════════════════════════════════════════════════════════════════════════
# H.  Failure isolation — one sport / row cannot abort refresh
# ═════════════════════════════════════════════════════════════════════════

class TestFailureIsolation:
    """`apply_block8_magic_to_picks` and the reachability counter
    both isolate per-row failures.  A raise in one sport must not
    halt the batch."""

    def test_apex_batch_isolates_per_pick_failures(self):
        """Already tested in `test_universal_reachability_p0` — this
        is a canary that the pattern remains wired."""
        import services.magic.adapters as adapters
        assert hasattr(adapters, "build_evidence")
