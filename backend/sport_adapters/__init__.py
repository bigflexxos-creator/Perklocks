"""Sport Adapter framework — Phase 2 of the Universal Evidence System.

Every sport plugs into the same 5-method contract:
  1. collect_features(pick) → list[EvidenceFeature]
  2. generate_probability(pick) → float (p_sport, 0..1)
  3. run_simulation(pick) → dict (sim metrics)
  4. calibrate(p_final) → float (post-isotonic)
  5. generate_explanation(pick, features) → list[str]

The pipeline stays exactly:
  raw_features → sport_probability → simulation_probability →
  probability_fusion → calibration_layer → final_probability →
  lock_score

Lock score remains a DOWNSTREAM display layer — it never receives raw
stats. Every adapter only emits probabilities and features; the
Universal Evidence System governs how those become a lock score.

Phase 2 ships:
  • MLB / Soccer / Tennis  → LIVE adapters with deeper feature
                              extraction than the sport-agnostic
                              fallback we shipped in Phase 1.
  • NBA / NFL / CFB        → adapter shells only. No live ingestion
                              until those seasons start serving
                              data; interface is already wired so
                              the plug-in is one PR.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from evidence_engine import EvidenceFeature


class SportAdapter(ABC):
    """The 5-method contract every sport must implement.

    `pick` is the live mongo dict — we read whatever provenance
    upstream (sports_engine, Understat scraper, tennis_extra, etc.)
    has populated.

    Adapters MUST be tolerant of partial data: if a feature can't
    be sourced for this particular pick (e.g. weather missing for
    an indoor stadium), skip it. The Evidence Governor will
    naturally down-weight a pick whose features are sparse — that's
    the design.
    """

    SPORT: str = ""

    # ── 1) Feature extraction ─────────────────────────────────────
    @abstractmethod
    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        """Return the EvidenceFeature envelope list for `pick`.

        Each feature must carry full provenance: name, source,
        sample_size, lookback_days, freshness, importance.
        Reliability + tier are filled in by the Evidence Engine
        downstream — don't set them here.
        """

    # ── 2) Sport-native probability ───────────────────────────────
    def generate_probability(self, pick: dict) -> Optional[float]:
        """Compute the sport-native probability estimate (`p_sport`)
        for this pick. Returns None if the adapter can't compute one
        — the pipeline will fall back to the blended engine output.

        Default implementation reads `pick.win_probability` (which
        upstream sports_engine already populated). Per-sport
        adapters override this when they have a richer model.
        """
        wp = pick.get("win_probability")
        try:
            return float(wp) / 100.0 if wp is not None else None
        except Exception:
            return None

    # ── 3) Simulation hook ────────────────────────────────────────
    def run_simulation(self, pick: dict) -> dict:
        """Pass-through to the existing sport-specific Monte Carlo
        simulator (brain/sim_*.py). Each adapter overrides to wire
        in its specific simulator. Default returns the cached
        sim_* fields already on the pick."""
        return {
            "sim_win_probability": pick.get("sim_win_probability"),
            "sim_ci_lower":        pick.get("sim_ci_lower"),
            "sim_ci_upper":        pick.get("sim_ci_upper"),
            "sim_runs":            pick.get("sim_runs"),
        }

    # ── 4) Calibration shim ───────────────────────────────────────
    def calibrate(self, p_final: float) -> float:
        """Apply isotonic (or per-sport beta) calibration to the
        blended probability. Default uses the global isotonic curve
        from lock_calibration.py — sports with rich enough data can
        override with their own."""
        try:
            from lock_calibration import calibrate_probability
            return calibrate_probability(p_final)
        except Exception:
            return p_final

    # ── 5) Explanation generator ──────────────────────────────────
    def generate_explanation(
        self, pick: dict, features: list[EvidenceFeature],
    ) -> list[str]:
        """Build the per-feature explanation bullets shown in the UI.

        Default: emits one bullet per HIGH/MEDIUM-tier feature using
        its `reason` text. The Explanation Governor in
        evidence_engine.apply_explanation_governor() handles
        hype-word filtering and the SIGNAL_LIMITED_FALLBACK.
        """
        out: list[str] = []
        for f in features:
            if f.tier in ("HIGH", "MEDIUM") and (f.explanation_text or f.reason):
                out.append(f.explanation_text or f.reason)
        return out


# ── Sport dispatch ───────────────────────────────────────────────
def get_adapter(sport: str) -> SportAdapter:
    """Return the singleton adapter for a sport. Returns the
    sport-agnostic fallback adapter if no sport-specific one exists
    yet (e.g. UFC, NHL until those get dedicated adapters)."""
    key = (sport or "").upper().strip()
    if key in _REGISTRY:
        return _REGISTRY[key]
    # Fallback: use the base sport-agnostic feature extraction so
    # picks without a dedicated adapter still get the universal
    # evidence system applied.
    return _FALLBACK


class _FallbackAdapter(SportAdapter):
    """Sport-agnostic fallback. Mirrors the Phase 1 generic feature
    extractor — used for sports without a dedicated adapter."""
    SPORT = "*"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        from evidence_engine import build_features_from_pick
        return build_features_from_pick(pick)


_FALLBACK = _FallbackAdapter()
_REGISTRY: dict[str, SportAdapter] = {}


def register(adapter: SportAdapter) -> None:
    """Register a sport adapter into the dispatch table. Called by
    each adapter module at import time."""
    if adapter.SPORT:
        _REGISTRY[adapter.SPORT.upper()] = adapter
