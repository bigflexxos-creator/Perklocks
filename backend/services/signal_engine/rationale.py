"""Signal-driven "Why This Pick" builder.

Turns the computed signal components into concrete, numbers-first
bullets. NEVER emits generic text — every bullet is backed by a real
detail string produced by a calculator, and components with no data
simply don't appear.
"""
from __future__ import annotations


def build_why(pick: dict, score: int, components: list[dict]) -> list[str]:
    """Return an ordered list of why-this-pick strings.

    Layout:
      1. Headline — market + model probability + Signal Score.
      2. Strongest positive signals (points >= 1), best detail each.
      3. Up to 2 honest negatives (points <= -1) so users see the risk.
    """
    why: list[str] = []

    wp = pick.get("win_probability")
    market = pick.get("market") or pick.get("selection") or "This pick"
    if isinstance(wp, (int, float)) and wp > 0:
        why.append(
            f"{market} carries a {wp:g}% model win probability — "
            f"Signal Score {score}/100.")

    ranked = sorted(components, key=lambda c: c.get("points", 0), reverse=True)

    positives = [c for c in ranked if c.get("points", 0) >= 1 and c.get("details")]
    for c in positives[:4]:
        why.append(f"{c['label']} +{c['points']:g}: {c['details'][0]}")
        # A second detail on the single strongest component adds depth
        # without flooding the panel.
        if c is positives[0] and len(c["details"]) > 1:
            why.append(f"{c['label']}: {c['details'][1]}")

    negatives = [c for c in ranked if c.get("points", 0) <= -1 and c.get("details")]
    for c in list(reversed(negatives))[:2]:
        why.append(f"⚠ {c['label']} {c['points']:g}: {c['details'][0]}")

    return why[:6]


def signal_breakdown_line(components: list[dict]) -> str:
    """Compact 'Volume +18 · Matchup +15 · Form +12' summary string."""
    parts = []
    for c in sorted(components, key=lambda c: abs(c.get("points", 0)), reverse=True):
        p = c.get("points", 0)
        if abs(p) >= 0.5:
            parts.append(f"{c['label']} {'+' if p > 0 else ''}{p:g}")
    return " · ".join(parts[:5])
