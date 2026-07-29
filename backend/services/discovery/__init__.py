"""Discovery layer public API (2026-07-28).

    from services.discovery import (
        analyse_thresholds,       # threshold ladder analysis
        discover_patterns,        # NFL factor-split pattern miner
        find_similar_situations,  # k-means opponent clustering
        recommend_alt_lines,      # safest / strongest / best-value line
        magic_find,               # unified Magic Finder API
        # confidence primitives
        wilson_lower_bound, confidence_grade, confidence_label,
        passes_sample_gate, consistency_score,
    )
"""
from .threshold_discovery import analyse_thresholds
from .pattern_discovery import discover_patterns
from .situation_clustering import find_similar_situations
from .alt_line_intelligence import recommend_alt_lines
from .magic_finder import magic_find
from .confidence_system import (
    wilson_lower_bound, wilson_upper_bound,
    confidence_grade, confidence_label,
    passes_sample_gate, consistency_score, variance_score,
)

__all__ = [
    "analyse_thresholds",
    "discover_patterns",
    "find_similar_situations",
    "recommend_alt_lines",
    "magic_find",
    "wilson_lower_bound", "wilson_upper_bound",
    "confidence_grade", "confidence_label",
    "passes_sample_gate", "consistency_score", "variance_score",
]
