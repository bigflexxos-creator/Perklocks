"""CFB rationale builder — turns cached CFBD ingestion into a
`pick_rationale` dict the LockPickCard UI can render.

Inputs (resolved via `cfb_ingest.get_team_record` / `get_portal_entry`):
  * Team-level: returning production %, SP+ rank/rating, SoS, off/def rank.
  * Player-level: portal entry (transferred from X to Y, stars, rating).
  * (Optional) opponent: same team-level inputs for the opposing school.

Output shape mirrors `mlb_hitter_intel.to_rationale()` so the existing UI
component renders it with zero changes:

    {
      "summary": "<one-liner>",
      "data_source": "collegefootballdata",
      "engine": "cfb_rationale",
      "evidence": ["✅ ...", "📊 ..."],
      "concerns": ["⚠️ ..."],
      "team_quality": {...},          # SP+ snapshot
      "matchup": {"team":..., "opponent":..., "sp_gap":...},
      "returning_production": {...},
      "portal": {...},
    }

The rationale is intentionally STABLE in shape — keep it boring so the UI
stays simple.

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from services import cfb_ingest

logger = logging.getLogger("lockscore.cfb.rationale")


def _fmt_rank(r: Optional[int]) -> str:
    """Format a rank as '#42' or '—' if missing."""
    if r is None:
        return "—"
    try:
        return f"#{int(r)}"
    except Exception:
        return "—"


def _rank_band(r: Optional[int]) -> str:
    """Banded label for a national ranking (1–135 FBS schools)."""
    if r is None:
        return "unknown"
    try:
        r = int(r)
    except Exception:
        return "unknown"
    if r <= 10:
        return "elite"
    if r <= 30:
        return "strong"
    if r <= 65:
        return "average"
    if r <= 100:
        return "below average"
    return "weak"


def _pct_band(p: Optional[float]) -> str:
    """Banded label for a 0–1 % (returning production)."""
    if p is None:
        return "unknown"
    try:
        p = float(p)
    except Exception:
        return "unknown"
    if p >= 0.75:
        return "loaded"
    if p >= 0.6:
        return "experienced"
    if p >= 0.45:
        return "rebuilding"
    return "gutted"


async def build_cfb_rationale(
    db,
    team: str,
    opponent: Optional[str] = None,
    player_name: Optional[str] = None,
    year: Optional[int] = None,
) -> dict[str, Any]:
    """Build a CFB rationale dict for a pick on `team` (optionally vs
    `opponent`, optionally about player `player_name`).

    Always returns a dict — empty `evidence`/`concerns` are acceptable
    so the UI can render *something*."""
    rationale: dict[str, Any] = {
        "summary": "",
        "data_source": "collegefootballdata",
        "engine": "cfb_rationale",
        "evidence": [],
        "concerns": [],
        "team_quality": None,
        "matchup": None,
        "returning_production": None,
        "portal": None,
    }

    team_rec = await cfb_ingest.get_team_record(db, team, year=year)
    if not team_rec:
        rationale["summary"] = f"{team}: no CFBD profile cached yet"
        return rationale

    # ── SP+ ratings ────────────────────────────────────────────
    sp = team_rec.get("sp") or {}
    if sp:
        rationale["team_quality"] = {
            "overall_rank": sp.get("ranking"),
            "overall_rating": sp.get("rating"),
            "offense_rank": sp.get("offense_rank"),
            "defense_rank": sp.get("defense_rank"),
            "sos": sp.get("sos"),
            "conference": sp.get("conference"),
        }
        band = _rank_band(sp.get("ranking"))
        if band == "elite":
            rationale["evidence"].append(
                f"🏆 SP+ {_fmt_rank(sp.get('ranking'))} overall — elite tier"
            )
        elif band == "strong":
            rationale["evidence"].append(
                f"📈 SP+ {_fmt_rank(sp.get('ranking'))} overall — top-30"
            )
        elif band == "below average":
            rationale["concerns"].append(
                f"📉 SP+ {_fmt_rank(sp.get('ranking'))} overall — below average"
            )
        elif band == "weak":
            rationale["concerns"].append(
                f"⚠️ SP+ {_fmt_rank(sp.get('ranking'))} overall — bottom-35"
            )
        # Off / Def rank deltas
        off_band = _rank_band(sp.get("offense_rank"))
        def_band = _rank_band(sp.get("defense_rank"))
        if off_band == "elite":
            rationale["evidence"].append(
                f"⚡ Offense {_fmt_rank(sp.get('offense_rank'))} — explosive unit"
            )
        if def_band == "elite":
            rationale["evidence"].append(
                f"🛡️ Defense {_fmt_rank(sp.get('defense_rank'))} — shutdown unit"
            )

    # ── Returning production ───────────────────────────────────
    rp = team_rec.get("returning") or {}
    if rp:
        rationale["returning_production"] = {
            "percent_ppa": rp.get("percent_ppa"),
            "passing": rp.get("percent_passing_ppa"),
            "receiving": rp.get("percent_receiving_ppa"),
            "rushing": rp.get("percent_rushing_ppa"),
        }
        band = _pct_band(rp.get("percent_ppa"))
        pct = rp.get("percent_ppa")
        pct_str = f"{pct*100:.0f}%" if isinstance(pct, (int, float)) else "—"
        if band == "loaded":
            rationale["evidence"].append(
                f"📚 {pct_str} of last year's production returns — loaded roster"
            )
        elif band == "experienced":
            rationale["evidence"].append(
                f"📚 {pct_str} returning production — experienced lineup"
            )
        elif band == "rebuilding":
            rationale["concerns"].append(
                f"🛠️ Only {pct_str} returning — mid-rebuild"
            )
        elif band == "gutted":
            rationale["concerns"].append(
                f"🚨 Only {pct_str} returning — heavy turnover"
            )

    # ── Strength of schedule ───────────────────────────────────
    sos = sp.get("sos") if sp else None
    if isinstance(sos, (int, float)):
        # SP+ SoS is typically expressed in points-per-game added/removed.
        # Higher = tougher schedule.
        if sos >= 4:
            rationale["evidence"].append(
                f"🔥 SoS +{sos:.1f} — gauntlet schedule (proven vs top opponents)"
            )
        elif sos <= -3:
            rationale["concerns"].append(
                f"🥱 SoS {sos:.1f} — soft schedule (record looks better than reality)"
            )

    # ── Opponent comparison (head-to-head SP+ gap) ─────────────
    if opponent:
        opp_rec = await cfb_ingest.get_team_record(db, opponent, year=year)
        opp_sp = (opp_rec.get("sp") if opp_rec else None) or {}
        if opp_sp and sp:
            tr = sp.get("rating")
            orr = opp_sp.get("rating")
            if isinstance(tr, (int, float)) and isinstance(orr, (int, float)):
                gap = tr - orr
                rationale["matchup"] = {
                    "team": team,
                    "opponent": opponent,
                    "team_sp_rating": tr,
                    "opp_sp_rating": orr,
                    "sp_gap": round(gap, 2),
                    "opp_rank": opp_sp.get("ranking"),
                }
                if gap >= 10:
                    rationale["evidence"].append(
                        f"🥇 SP+ edge +{gap:.1f} vs {opponent} — massive talent gap"
                    )
                elif gap >= 4:
                    rationale["evidence"].append(
                        f"📈 SP+ edge +{gap:.1f} vs {opponent} — clear favourite"
                    )
                elif gap <= -10:
                    rationale["concerns"].append(
                        f"🚨 SP+ deficit {gap:.1f} vs {opponent} — massive talent gap against"
                    )
                elif gap <= -4:
                    rationale["concerns"].append(
                        f"📉 SP+ deficit {gap:.1f} vs {opponent} — clear underdog"
                    )

    # ── Portal lookup on the player (if any) ───────────────────
    if player_name:
        portal = await cfb_ingest.get_portal_entry(db, player_name, year=year)
        if portal:
            rationale["portal"] = {
                "origin": portal.get("origin"),
                "destination": portal.get("destination"),
                "stars": portal.get("stars"),
                "rating": portal.get("rating"),
                "position": portal.get("position"),
                "eligibility": portal.get("eligibility"),
            }
            star_str = f" ({portal.get('stars')}★)" if portal.get("stars") else ""
            rationale["evidence"].append(
                f"🔁 Portal transfer{star_str}: {portal.get('origin')} → {portal.get('destination')}"
            )
            if portal.get("eligibility") and portal["eligibility"].lower() != "immediate":
                rationale["concerns"].append(
                    f"⏳ Eligibility: {portal['eligibility']} — may not play right away"
                )

    # ── Compose summary ─────────────────────────────────────────
    rank_part = ""
    if sp and sp.get("ranking"):
        rank_part = f"SP+ {_fmt_rank(sp.get('ranking'))} · "
    rp_part = ""
    if rp and isinstance(rp.get("percent_ppa"), (int, float)):
        rp_part = f"{rp['percent_ppa']*100:.0f}% returning"
    if rank_part or rp_part:
        rationale["summary"] = f"{team}: {rank_part}{rp_part}".strip(" ·")
    else:
        rationale["summary"] = f"{team}: limited CFBD data"
    return rationale


# ─── Sync wrapper used inside pick_enrichment's _build_rationale ─────
def build_cfb_rationale_sync(
    db,
    team: str,
    opponent: Optional[str] = None,
    player_name: Optional[str] = None,
    year: Optional[int] = None,
) -> dict[str, Any]:
    """Synchronous helper for the sync enrichment loop. Runs the async
    builder inside a fresh event loop. Returns an empty rationale on
    any failure so the caller always has something to attach."""
    import asyncio
    # If we're already inside a running event loop (the normal case
    # when called from `pick_enrichment.enrich_picks_with_active_registry`
    # which itself runs inside an async refresh handler), we can't spin
    # up a nested loop. Return an empty rationale and let the caller's
    # universal model/edge framing fill the card. The async path
    # (`build_cfb_rationale`) is wired directly from the daily refresh
    # job and admin endpoints, so the deep CFB rationale still lands
    # when the pipeline is rearchitected to be async-first.
    try:
        asyncio.get_running_loop()
        return {
            "summary": "",
            "data_source": "collegefootballdata",
            "engine": "cfb_rationale",
            "evidence": [],
            "concerns": [],
            "_skipped_reason": "called from inside a running event loop — use async build_cfb_rationale() instead",
        }
    except RuntimeError:
        pass  # no running loop — safe to create one
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                build_cfb_rationale(db, team, opponent, player_name, year)
            )
        finally:
            loop.close()
    except Exception as e:
        logger.warning("CFB rationale sync wrap failed: %s", e)
        return {
            "summary": f"{team}: rationale unavailable",
            "data_source": "collegefootballdata",
            "engine": "cfb_rationale",
            "evidence": [],
            "concerns": [],
        }
