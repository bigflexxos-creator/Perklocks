"""Phase 4B — Simulator Result Contract.

Every simulator (independent MC, posterior sampler, event sim, stress
test, etc.) MUST return a :class:`SimulatorResult` that truthfully
labels itself. This is the guardrail against future mislabelling of
posterior samplers as independent Monte-Carlo evidence.

The contract is intentionally serialisable as a plain ``dict`` so the
brain / feed / analytics layers can consume it without importing this
module (keeps circular dependencies out).

Types
=====
``simulator_type`` — one of:
  • ``event_simulation``          — true point-by-point / play-by-play
                                     (tennis serve-level, chess-tree).
  • ``distribution_monte_carlo``  — draws from a fitted distribution
                                     (Poisson lambda, Bernoulli per AB).
  • ``scenario_stress_test``      — replays historical scenarios.
  • ``heuristic_adjustment``      — deterministic rule-based nudge.
  • ``deterministic_projection``  — closed-form projection (no MC).
  • ``posterior_uncertainty``     — Beta/Dirichlet posterior around
                                     the CALLING model's probability.
                                     **Never independent evidence.**

Independence flag
=================
``independent_evidence`` is True IFF the simulator's output is
computed from inputs NOT already summarised by the caller's model
probability.  A posterior sampler seeded from ``mu = model_prob``
CANNOT set this flag to True.

Consumers that treat simulator output as a second model vote MUST
check ``independent_evidence`` before doing so.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Literal


SimulatorType = Literal[
    "event_simulation",
    "distribution_monte_carlo",
    "scenario_stress_test",
    "heuristic_adjustment",
    "deterministic_projection",
    "posterior_uncertainty",
]

ALLOWED_SIMULATOR_TYPES: frozenset[str] = frozenset({
    "event_simulation",
    "distribution_monte_carlo",
    "scenario_stress_test",
    "heuristic_adjustment",
    "deterministic_projection",
    "posterior_uncertainty",
})


@dataclass
class SimulatorResult:
    """Typed contract every simulator MUST return.

    ``valid=False`` means the caller MUST NOT use the probability or
    anchor the pick — the simulator ran but its result is unusable
    (bad line, missing input, degenerate parameters, etc.).
    """
    simulator_name: str
    simulator_version: str
    simulator_type: SimulatorType
    seed: int
    iterations: int
    input_line: Optional[float]
    input_side: Optional[str]
    raw_probability: Optional[float]              # p from the sim
    stabilized_probability: Optional[float]       # smoothed / calibrated p
    standard_error: Optional[float]
    lower_bound: Optional[float]
    upper_bound: Optional[float]
    push_probability: Optional[float]
    valid: bool
    invalid_reason: Optional[str]
    independent_evidence: bool
    duration_ms: Optional[float] = None
    method: Optional[str] = None                  # human-readable method label
    extras: dict = field(default_factory=dict)    # sim-specific auxiliary data

    def __post_init__(self) -> None:
        if self.simulator_type not in ALLOWED_SIMULATOR_TYPES:
            raise ValueError(
                f"simulator_type {self.simulator_type!r} not in "
                f"{sorted(ALLOWED_SIMULATOR_TYPES)}"
            )
        # Guard-rail: posterior_uncertainty MUST NOT claim independence.
        if (self.simulator_type == "posterior_uncertainty"
                and self.independent_evidence):
            raise ValueError(
                "posterior_uncertainty simulator_type cannot set "
                "independent_evidence=True"
            )

    def to_dict(self) -> dict:
        """Serialise to a plain dict.  Filters trivially-null fields."""
        d = asdict(self)
        return d


__all__ = [
    "SimulatorResult",
    "SimulatorType",
    "ALLOWED_SIMULATOR_TYPES",
]
