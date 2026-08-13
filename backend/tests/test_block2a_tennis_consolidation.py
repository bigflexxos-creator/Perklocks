"""Block 2A — Tennis Runtime Consolidation + Cross-Sport Duplicate-Runtime Guard.

Certifies:
  (a) ONE authoritative Tennis runtime entry point.
  (b) Tennis simulator (`brain/sim_tennis.py`) is runtime-reachable
      via the shared sim_runner dispatch.
  (c) `services/tennis_feature_engine.py` remains an
      UNREACHABLE_MODERN_ENGINE (not silently re-wired).
  (d) `tennis_extra/*` remains a specialized separate product surface
      (TennisExplorer scraper for lower-tier tour) and cannot
      independently publish canonical Tennis candidates without going
      through the P0.2b canonical settlement path.
  (e) No duplicate primary Tennis runtime exists in the tree.

Block 2A is scope-limited to Tennis + cross-sport inventory.  Block 2B
will do NFL/NBA Magic wiring; Block 2C will complete NHL/CFB/UFC/Soccer.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ═════════════════════════════════════════════════════════════════════
# §A — Authoritative Tennis runtime is `tennis_engine.apply_tennis_engine`
# ═════════════════════════════════════════════════════════════════════

class TestAuthoritativeTennisRuntime:
    def test_tennis_engine_defines_apply(self):
        import tennis_engine
        assert hasattr(tennis_engine, "apply_tennis_engine")
        assert callable(tennis_engine.apply_tennis_engine)

    def test_pick_refresh_orchestrator_calls_apply_tennis_engine(self):
        """The canonical refresh orchestrator delegates to
        ``tennis_engine.apply_tennis_engine``.  If a future edit
        introduces a competing entry point this test fails."""
        src = Path("/app/backend/services/pick_refresh_orchestrator.py"
                   ).read_text()
        assert "from tennis_engine import apply_tennis_engine" in src
        assert "await apply_tennis_engine(db, picks)" in src

    def test_tennis_engine_authoritative_docstring(self):
        """The Block 2A canonical-owner marker MUST remain in the
        module docstring so future agents know this is authoritative."""
        src = Path("/app/backend/tennis_engine.py").read_text()
        assert "SINGLE AUTHORITATIVE TENNIS RUNTIME" in src
        assert "Block 2A" in src


# ═════════════════════════════════════════════════════════════════════
# §B — Tennis simulator reachability
# ═════════════════════════════════════════════════════════════════════

class TestTennisSimulatorReachable:
    def test_sim_runner_dispatches_tennis(self):
        src = Path("/app/backend/brain/sim_runner.py").read_text()
        assert 'sport == "Tennis"' in src
        assert "from brain.sim_tennis import simulate_tennis_pick" in src

    def test_sim_tennis_module_exposes_simulate_tennis_pick(self):
        from brain import sim_tennis
        assert hasattr(sim_tennis, "simulate_tennis_pick")
        assert callable(sim_tennis.simulate_tennis_pick)


# ═════════════════════════════════════════════════════════════════════
# §C — `services/tennis_feature_engine.py` remains UNREACHABLE
# ═════════════════════════════════════════════════════════════════════

class TestTennisFeatureEngineRemainsUnreachable:
    """Explicitly enforces the P0.2f finding that this modern-but-
    dead engine must NOT silently be wired into the production
    Tennis path.  If a future edit imports it from any production
    module outside the approved allow list, this test fails.
    """
    APPROVED_CALLERS = {
        # Read-only diagnostic + test surface — not production runtime.
        "services/pipeline_diagnostic.py",
    }

    def test_no_production_caller_imports_tennis_feature_engine(self):
        backend = Path("/app/backend")
        rogue = []
        pat = re.compile(
            r"""(?:from\s+services\.tennis_feature_engine|"""
            r"""from\s+tennis_feature_engine|"""
            r"""import\s+services\.tennis_feature_engine|"""
            r"""import\s+tennis_feature_engine)"""
        )
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in self.APPROVED_CALLERS:
                continue
            if pat.search(py.read_text(errors="ignore")):
                rogue.append(rel)
        assert not rogue, (
            "services/tennis_feature_engine.py is classified as "
            "UNREACHABLE_MODERN_ENGINE (Block 2A).  New production "
            "callers must be documented in the Block 2A report and "
            "added to APPROVED_CALLERS.  Found: " + str(rogue))

    def test_unreachable_marker_present(self):
        """The Block 2A classification must remain in the module
        docstring so future edits cannot silently promote it."""
        src = Path("/app/backend/services/tennis_feature_engine.py"
                   ).read_text()
        assert "UNREACHABLE_MODERN_ENGINE" in src


# ═════════════════════════════════════════════════════════════════════
# §D — tennis_extra/* remains a specialized separate product
# ═════════════════════════════════════════════════════════════════════

class TestTennisExtraSpecializedSeparate:
    """`tennis_extra/*` is the TennisExplorer scraper for lower-tier
    tour picks.  It is a specialized separate product — NOT a
    duplicate of `tennis_engine`.  P0.2b already routed its
    settlement writes through canonical `SettlementService`, so it
    cannot independently publish canonical settlement truth.  This
    test enforces that architectural boundary."""
    def test_tennis_extra_settle_routes_through_settlement_service(self):
        src = Path("/app/backend/tennis_extra/settle.py").read_text()
        assert "SettlementService" in src
        assert "settle_from_pick" in src

    def test_tennis_extra_does_not_publish_locks_board_directly(self):
        """`tennis_extra` picks land as a specialized separate
        product surface; they must not directly write to `db.picks`
        with a status that would bypass canonical settlement."""
        src = Path("/app/backend/tennis_extra/settle.py").read_text()
        # No literal `"status": "won|lost|push|void"` write remains
        # in the scraper — it all goes through SettlementService.
        assert re.search(
            r"""["']status["']\s*:\s*["'](won|lost|push|void)["']""",
            src) is None


# ═════════════════════════════════════════════════════════════════════
# §E — Cross-sport duplicate-runtime static guard
# ═════════════════════════════════════════════════════════════════════

class TestNoDuplicateSportRuntime:
    """No unapproved duplicate primary runtime paths for any sport.

    Approved authoritative runtimes (Block 2A inventory):
        MLB       → sports_engine.py + services/mlb_*_engine
        MLB NRFI  → brain/nrfi_engine.py
        NFL       → nfl_game_engine.py + nfl_atd_engine.py + nfl_safe_engine.py
        NBA       → sports_engine.py + services/nba_feature_engine.py + sport_adapters/nba.py
        NHL       → (no authoritative engine yet — Block 2C scope)
        CFB       → services/cfb_feature_engine.py + services/cfb_ingest.py + services/cfb_precompute.py
        Soccer    → soccer/pipeline.py + soccer/predictor.py
        Tennis    → tennis_engine.py (Block 2A canonical entry)
        UFC/MMA   → (no authoritative engine yet — Block 2C scope)

    Specialized separate engines that legitimately coexist:
        tennis_extra/*                       lower-tier tour scraper
        nfl_atd_engine.py                    ATD leaderboard product
        soccer_lab.py                        research surface
        brain/nrfi_yrfi_model.py             NRFI helper for nrfi_engine
    """
    def test_only_one_apply_tennis_engine_definition(self):
        backend = Path("/app/backend")
        defs = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        ".venv")):
                continue
            if "def apply_tennis_engine" in py.read_text(errors="ignore"):
                defs.append(rel)
        assert defs == ["tennis_engine.py"], (
            "Duplicate Tennis runtime: apply_tennis_engine defined in "
            + str(defs))

    def test_no_hidden_tennis_publication_bypass(self):
        """No other module besides the approved canonical owners may
        write Tennis picks with a canonical `status` field.  The
        P0.2f unified bypass guard already enforces this at the
        settlement layer; here we specifically double-check the
        Tennis surface."""
        backend = Path("/app/backend")
        approved = {
            "services/settlement_service.py",
            "tennis_engine.py",
            "tennis_extra/settle.py",
            "sports_engine.py",
        }
        rogue = []
        for py in backend.rglob("*.py"):
            rel = str(py.relative_to(backend))
            if any(s in rel for s in ("__pycache__", "tests/",
                                        "scripts/", ".venv")):
                continue
            if rel in approved:
                continue
            src = py.read_text(errors="ignore")
            if "Tennis" not in src:
                continue
            if "db.picks.insert_many" in src or \
               "db.picks.insert_one" in src:
                if 'sport": "Tennis"' in src or "'Tennis'" in src:
                    rogue.append(rel)
        assert not rogue, (
            "Rogue Tennis publication path outside approved owners: "
            + str(rogue))


# ═════════════════════════════════════════════════════════════════════
# §F — Cross-sport simulator reachability matrix
# ═════════════════════════════════════════════════════════════════════

class TestCrossSportSimulatorMatrix:
    """Codifies the current simulator reachability matrix so any
    Block 2B/2C change that alters reachability is visible."""

    def test_sim_runner_dispatch_map(self):
        src = Path("/app/backend/brain/sim_runner.py").read_text()
        # Reachable sports (P0.2f + Block 2A verification):
        assert 'sport == "MLB"' in src
        assert 'sport == "Soccer"' in src
        assert 'sport == "NBA"' in src
        assert 'sport == "Tennis"' in src
        # Sports whose simulator wiring is NOT yet in sim_runner
        # (documented as Block 2B/2C scope):
        assert 'sport == "NFL"' not in src
        assert 'sport == "NHL"' not in src
        assert 'sport == "CFB"' not in src
        assert 'sport == "UFC"' not in src

    def test_sim_version_map_matches_reachable_sports(self):
        from brain import sim_runner
        assert set(sim_runner._SIM_VERSIONS.keys()) == {
            "MLB", "NBA", "Soccer", "Tennis"
        }


# ═════════════════════════════════════════════════════════════════════
# §G — Tennis authoritative helper import graph
# ═════════════════════════════════════════════════════════════════════

class TestTennisHelperGraph:
    """Sanity-check that tennis_engine wires into the documented
    authoritative helpers rather than something arbitrary."""

    @pytest.mark.parametrize("helper", [
        "tennis_identity",
        "tennis_calibration",
        "tennis_data_quality",
    ])
    def test_tennis_engine_imports_helper(self, helper):
        src = Path("/app/backend/tennis_engine.py").read_text()
        # Either `from services.<helper>` or `from <helper>`.
        assert (f"from services.{helper}" in src
                or f"import services.{helper}" in src
                or f"from {helper}" in src)
