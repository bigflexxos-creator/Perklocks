"""Universal ESPN-backed pick enrichment.

Runs immediately before `db.picks.insert_many(...)` so EVERY pick across
every sport gets the same treatment:

  1. **Active-roster validation** — calls `services.active_registry.is_active`
     to drop picks whose player has been retired / traded / cut.
     Currently armed for NBA + NFL (where the registry has live data).
     Soccer (CSL) keeps its own legacy `csl_espn_live` filter (already
     applied upstream in `sportdb_player_scorer`).
  2. **`pick_rationale` block** — structured "show your work" data for
     every player-prop pick: ESPN rank where available, raw stats, source
     of truth, the math (λ, prob), evidence + concerns tags. The UI's
     LockPickCard expands this into an audit panel so users understand
     WHY each pick made the board, not just the win-prob number.

The user request driving this module (2026-06-28):

    "ESPN data should be in pipeline for all sports"
    "I want education behind goalscorer not just random picks"

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.pick_enrichment")


# ─── Sport detection ──────────────────────────────────────────────
def _detect_sport(pick: dict) -> Optional[str]:
    """Maps the pick's sport_key / league / sport field to the
    `active_registry` sport key. Returns None for picks that don't map
    cleanly (those get passed through untouched)."""
    sport = (pick.get("sport") or "").lower()
    sport_key = (pick.get("sport_key") or "").lower()
    league = (pick.get("league") or "").lower()
    if "basketball" in sport_key or sport == "basketball" or "nba" in league:
        return "nba"
    if "football" in sport_key and "americanfootball" in sport_key.replace("_", "") + sport.replace(" ", ""):
        return "nfl"
    if sport_key.startswith("americanfootball_nfl") or "nfl" in league:
        return "nfl"
    if "americanfootball_ncaaf" in sport_key or "cfb" in league or "college football" in league:
        return "cfb"
    if sport_key.startswith("baseball_mlb") or "mlb" in league or sport == "baseball":
        return "mlb"
    if "soccer" in sport_key or sport == "soccer" or "football" in sport_key:
        return "soccer"
    return None


def _extract_player_name(pick: dict) -> Optional[str]:
    """Returns the player name attached to this pick, or None for
    team-level picks (moneyline, spread, total)."""
    name = pick.get("player_name") or pick.get("player") or ""
    if name and isinstance(name, str):
        return name.strip()
    # Try to parse from market string ("LeBron James Points Over 25.5")
    market = (pick.get("market") or "")
    for sep in (" - ", " Over ", " Under ", " Anytime ", " To Score", " To Record"):
        if sep in market:
            return market.split(sep, 1)[0].strip()
    return None


# ─── Rationale builder ──────────────────────────────────────────────
def _build_rationale(pick: dict, sport: str, name: str) -> dict[str, Any]:
    """Build a structured rationale for a player-prop pick using whatever
    sources we have for the given sport. Always returns a dict so the
    UI can render *something* — empty evidence/concerns lists are fine."""
    rationale: dict[str, Any] = {
        "summary": "",
        "data_source": pick.get("source") or "model",
        "evidence": [],
        "concerns": [],
        "espn_rank": None,
        "stats_this_season": None,
        "model_win_prob_pct": pick.get("win_probability"),
        "edge_percent": pick.get("edge_percent"),
        "lock_score": pick.get("lock_score"),
    }

    # Pull the active_registry record for this player (NBA / NFL / future sports).
    try:
        from services import active_registry
        rec = active_registry.get_record(sport, name) if sport in ("nba", "nfl") else None
    except Exception:
        rec = None

    if rec:
        # NFL nfl.com's first stat column is *not* games-played for passing
        # leaders (it's passing yards); cap any value > 100 to None for
        # display purposes so the UI doesn't claim "3587 games this season".
        gp = rec.get("games_played")
        if isinstance(gp, (int, float)) and gp > 200:
            gp = None
        rationale["stats_this_season"] = {
            "team": rec.get("team"),
            "minutes": rec.get("minutes"),
            "games_played": gp,
            "sources": list((rec.get("sources") or {}).keys()),
        }
        srcs = list((rec.get("sources") or {}).keys())
        if srcs:
            rationale["evidence"].append(
                f"✅ Active per {len(srcs)} ESPN-backed sources: {', '.join(srcs)}"
            )
        if isinstance(gp, (int, float)) and gp >= 30:
            rationale["evidence"].append(
                f"📊 {gp:.0f} games this season — large sample"
            )
        elif isinstance(gp, (int, float)) and gp <= 5:
            rationale["concerns"].append(
                f"⚠️ Only {gp:.0f} games played — small sample"
            )

    # CSL: piggy-back the live ESPN scorer board (rank + form).
    if sport == "soccer":
        try:
            import csl_espn_live as _csl
            live = _csl.get_live_form(name)
            if live:
                # Compute rank from in-memory leaderboard.
                rows = sorted(
                    (v for v in _csl._scorer_index.values() if (v.get("goals") or 0) > 0),
                    key=lambda r: (r.get("goals") or 0),
                    reverse=True,
                )
                rank = None
                key = _csl._norm(name)
                for i, r in enumerate(rows, 1):
                    if _csl._norm(r.get("name") or "") == key:
                        rank = i
                        break
                rationale["espn_rank"] = rank
                rationale["stats_this_season"] = {
                    "team": live.get("team"),
                    "goals": live.get("goals"),
                    "matches": live.get("matches"),
                    "assists": live.get("assists"),
                    "rate_per_match": live.get("rate_per_match"),
                }
                if rank and rank <= 10:
                    rationale["evidence"].append(
                        f"🥇 ESPN #{rank} scorer in his league — top-tier threat"
                    )
                elif rank and rank <= 25:
                    rationale["evidence"].append(
                        f"📈 ESPN #{rank} scorer — consistent contributor"
                    )
                elif rank:
                    rationale["concerns"].append(
                        f"⚠️ ESPN #{rank} scorer — outside top contributors"
                    )
                if (live.get("goals") or 0) > 0:
                    rationale["evidence"].append(
                        f"⚽ {live['goals']} goals in {live['matches']} matches"
                        f" ({live['rate_per_match']:.2f}/match)"
                    )
        except Exception:
            pass

    # Win-prob and edge framing — universally useful.
    wp = pick.get("win_probability")
    edge = pick.get("edge_percent")
    if isinstance(wp, (int, float)):
        if wp >= 65:
            rationale["evidence"].append(f"💯 Model gives {wp:.1f}% win prob — high confidence")
        elif wp <= 35:
            rationale["concerns"].append(f"📉 Model gives only {wp:.1f}% win prob — longshot")
    if isinstance(edge, (int, float)) and edge >= 5.0:
        rationale["evidence"].append(f"📊 Edge vs market: +{edge:.1f}%")
    elif isinstance(edge, (int, float)) and edge <= -2.0:
        rationale["concerns"].append(f"📊 Negative edge vs market: {edge:.1f}%")

    # Existing pick_rationale (e.g. CSL synth picks already built one in
    # sportdb_player_scorer) wins — don't overwrite a richer source.
    existing = pick.get("pick_rationale")
    if isinstance(existing, dict) and existing.get("evidence"):
        # Merge our additions onto the existing block.
        existing.setdefault("evidence", []).extend(rationale["evidence"])
        existing.setdefault("concerns", []).extend(rationale["concerns"])
        return existing

    # Compose a one-line summary fallback.
    rank_part = f"ESPN #{rationale['espn_rank']} " if rationale["espn_rank"] else ""
    rationale["summary"] = (
        f"{name}: {rank_part}model {wp:.0f}% to hit"
        if isinstance(wp, (int, float))
        else f"{name}: pick rationale (auto)"
    )
    return rationale


# ─── Public entry point ──────────────────────────────────────────────
def enrich_picks_with_active_registry(picks: list[dict]) -> dict[str, int]:
    """Mutates each pick in `picks`:

      * Adds `pick_rationale` to every player pick (sport-specific).
      * Marks `validation_block` reasons on picks whose player is
        confirmed INACTIVE — caller may drop these before persistence.

    Returns counts: {enriched, blocked_inactive, skipped_team_pick}.
    """
    counts = {"enriched": 0, "blocked_inactive": 0, "skipped_team_pick": 0}
    try:
        from services import active_registry
    except Exception:
        active_registry = None  # type: ignore

    for pick in picks:
        sport = _detect_sport(pick)
        name = _extract_player_name(pick)
        if not name or not sport:
            counts["skipped_team_pick"] += 1
            continue

        # ── Active-roster gate (NBA / NFL only for now) ──
        if active_registry is not None and sport in ("nba", "nfl"):
            verdict = active_registry.is_active(sport, name)
            if verdict is False:
                # Mark for downstream dropping but never silently delete —
                # caller decides whether to skip insertion or keep with a
                # `validation_block` flag for analytics.
                pick["validation_block"] = "inactive_player"
                pick["validation_block_reason"] = (
                    f"{name} not found in ESPN active {sport.upper()} roster + season leaders"
                )
                counts["blocked_inactive"] += 1
                continue

        # ── Pick rationale (every player pick) ──
        try:
            pick["pick_rationale"] = _build_rationale(pick, sport, name)
            counts["enriched"] += 1
        except Exception as e:
            logger.debug(f"rationale build failed for {name} ({sport}): {e}")

    return counts
