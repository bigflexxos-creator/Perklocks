"""Phase 8 — Alt-Line Magic Tier public exports."""
from .ranker import (
    generate_alt_lines,
    AltLine,
    AltLineBundle,
)
from .safeguards import is_safe_for_alt_lines
from .distribution import build_outcome_distribution
from .explanations import compose_explanation

__all__ = [
    "generate_alt_lines",
    "AltLine",
    "AltLineBundle",
    "is_safe_for_alt_lines",
    "build_outcome_distribution",
    "compose_explanation",
]
