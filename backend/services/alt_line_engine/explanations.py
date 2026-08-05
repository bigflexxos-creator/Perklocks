"""Phase 8 — human-readable "why this alt line has value" strings."""
from __future__ import annotations

from typing import Optional


def compose_explanation(
    *,
    player: str,
    stat: str,
    line: float,
    p_over: float,
    projected: Optional[float],
    edge: Optional[float],
    source: str,                # "market" | "model_projection"
    bucket_roi: Optional[float] = None,
    stability: Optional[float] = None,
) -> str:
    """Produce a compact one-line rationale for why the alt has value.

    Examples:
      "Model projects Burrow ~285 yds vs 250.5 line — 68% Over, +12%
       edge (bucket ROI +7.2% over 214 similar props). Market."
      "Alcaraz aces model projects ~7.4 vs 4.5 — 82% Over, model
       projection (no book line)."
    """
    parts: list[str] = []
    proj_str = f"{projected:.1f}" if projected is not None else "n/a"
    parts.append(f"Model projects {player} ≈ {proj_str} {stat.replace('_',' ')}")
    side = "Over" if p_over >= 0.5 else "Under"
    p_show = p_over if side == "Over" else (1.0 - p_over)
    parts.append(f"vs {line} line — {int(round(p_show * 100))}% {side}")
    if edge is not None:
        parts.append(f"({'+' if edge > 0 else ''}{int(round(edge*100))}% edge)")
    if bucket_roi is not None:
        parts.append(f"[bucket ROI {'+' if bucket_roi>0 else ''}"
                      f"{bucket_roi*100:.1f}%]")
    if stability is not None:
        parts.append(f"stability {stability:.2f}")
    parts.append(f"[{source}]")
    return " ".join(parts) + "."


__all__ = ["compose_explanation"]
