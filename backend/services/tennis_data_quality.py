"""Tennis data-quality assessor — Phase 4E.1.

Reports the *actual* coverage of tennis features on a pick so the
Magic Tier policy (Phase 4E.3) can cap confidence when data is thin.

Signals evaluated (only the ones we actually have — no invented
features):
  • identity_source          — stable provider ID vs. name fallback
  • surface Elo edge         — tennis_deep.elo_edge
  • overall Elo              — tennis_players.pick_elo(_overall)
  • H2H sample size          — tennis_h2h.matches
  • recent form              — tennis_deep.matches_7d / surface_fit
  • first-serve context      — tennis_first_set.edge_1st
  • serve stats present      — tennis_sackmann_stats presence
  • travel / fatigue         — tennis_deep.matches_7d as fatigue proxy
  • injury/retirement risk   — pick.get("player_status") flags

Quality tiers (returned as a plain string so the policy module can
compare without importing enums):

    "full"    — ≥ 4 real feature signals + stable identity.
    "partial" — 2-3 real signals OR stable identity but limited features.
    "sparse"  — 1 real signal, OR name-fallback identity.
    "empty"   — 0 real signals (only book-implied line).

Tier CAPS (advisory — the Magic Tier policy owns the final cap):

    full    → no cap.
    partial → Strong Lock max.
    sparse  → Lock max.
    empty   → Playable max.

The assessor NEVER invents missing features and NEVER upgrades a pick
based on model self-belief.  It only downgrades.
"""
from __future__ import annotations

from typing import Optional

# Real-feature keys — each callable takes the pick dict and returns
# True if the feature is present with a usable value.


def _has_surface_elo_edge(pick: dict) -> bool:
    deep = pick.get("tennis_deep") or {}
    return isinstance(deep.get("elo_edge"), (int, float))


def _has_overall_elo(pick: dict) -> bool:
    tp = pick.get("tennis_players") or {}
    return isinstance(
        tp.get("pick_elo_overall") or tp.get("pick_elo"), (int, float)
    )


def _has_h2h(pick: dict, min_matches: int = 3) -> bool:
    h2h = pick.get("tennis_h2h") or {}
    return int(h2h.get("matches", 0) or 0) >= min_matches


def _has_recent_form(pick: dict) -> bool:
    deep = pick.get("tennis_deep") or {}
    return isinstance(deep.get("matches_7d"), (int, float)) or isinstance(
        deep.get("surface_fit"), (int, float)
    )


def _has_first_set(pick: dict) -> bool:
    fs = pick.get("tennis_first_set") or {}
    return isinstance(fs.get("edge_1st"), (int, float))


def _has_serve_stats(pick: dict) -> bool:
    stats = pick.get("tennis_sackmann_stats") or {}
    # Any of the ≥6 serve/return metrics counts as "serve stats present".
    keys = (
        "svpt_won_pct", "first_serve_pct", "first_serve_won_pct",
        "second_serve_won_pct", "rpw_pct", "bp_save_pct", "bp_conv_pct",
    )
    return any(isinstance(stats.get(k), (int, float)) for k in keys)


def _has_injury_signal(pick: dict) -> bool:
    """`player_status` gets populated from ATP/WTA retirement/withdrawal
    feeds.  Presence is a *signal*, not a red flag by itself."""
    status = pick.get("player_status") or pick.get("tennis_player_status")
    return bool(status)


# ── Public entry point ───────────────────────────────────────────────


def assess_tennis_data_quality(
    pick: dict,
    identity: Optional[dict] = None,
) -> dict:
    """Return a data-quality report for a tennis pick.

    Parameters
    ----------
    pick : the pick dict (with enrichment attachments).
    identity : optional identity dict from ``resolve_tennis_identity``.
        If omitted, we still evaluate feature coverage but flag the
        identity as ``unknown`` — the caller should call the resolver.

    Returns
    -------
    dict with keys:
        signals            — dict[str, bool] of which features present
        signal_count       — int
        stable_identity    — bool
        identity_source    — str
        quality            — "full" | "partial" | "sparse" | "empty"
        max_tier           — advisory tier cap
                             ("Apex Lock" / "Elite Lock" / "Strong Lock"
                              / "Lock" / "Playable" / "Pass")
        reasons            — list[str] describing what was missing
    """
    signals = {
        "surface_elo_edge": _has_surface_elo_edge(pick),
        "overall_elo":      _has_overall_elo(pick),
        "h2h":              _has_h2h(pick),
        "recent_form":      _has_recent_form(pick),
        "first_set":        _has_first_set(pick),
        "serve_stats":      _has_serve_stats(pick),
        "injury_signal":    _has_injury_signal(pick),
    }
    signal_count = sum(1 for v in signals.values() if v)

    # Identity — default to unknown if not passed.
    if identity is not None:
        stable = bool(identity.get("stable_identity"))
        identity_source = identity.get("identity_source", "unknown")
    else:
        stable = False
        identity_source = "unknown"

    reasons: list[str] = []
    for k, v in signals.items():
        if not v:
            reasons.append(f"missing:{k}")
    if not stable:
        reasons.append(f"unstable_identity:{identity_source}")

    # Quality tier logic — conservative, additive.
    if signal_count >= 4 and stable:
        quality = "full"
        max_tier = "Apex Lock"
    elif signal_count >= 2 and stable:
        quality = "partial"
        max_tier = "Strong Lock"
    elif signal_count >= 2 and not stable:
        # Feature-rich but identity is a fallback → still Lock max.
        quality = "partial"
        max_tier = "Lock"
    elif signal_count >= 1:
        quality = "sparse"
        max_tier = "Lock" if stable else "Playable"
    else:
        quality = "empty"
        max_tier = "Playable"

    return {
        "signals":         signals,
        "signal_count":    signal_count,
        "stable_identity": stable,
        "identity_source": identity_source,
        "quality":         quality,
        "max_tier":        max_tier,
        "reasons":         reasons,
    }


# Ordered ranking used by the Magic Tier policy to compare caps.
_TIER_RANK = {
    "Pass":        0,
    "Playable":    1,
    "Lock":        2,
    "Strong Lock": 3,
    "Elite Lock":  4,
    "Apex Lock":   5,
}


def tier_rank(tier: str) -> int:
    """Numeric rank for tier comparison (higher = stronger).

    Unknown labels return 0 (safest — capped as Pass).
    """
    return _TIER_RANK.get(tier or "", 0)


def apply_tier_cap(current_tier: str, cap_tier: str) -> str:
    """Return whichever of ``current_tier`` / ``cap_tier`` is weaker.

    Never upgrades a tier — a cap can only demote or leave unchanged.
    """
    return current_tier if tier_rank(current_tier) <= tier_rank(cap_tier) else cap_tier


__all__ = [
    "assess_tennis_data_quality",
    "tier_rank",
    "apply_tier_cap",
]
