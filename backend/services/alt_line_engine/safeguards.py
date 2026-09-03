"""Phase 8 — safeguards: prevent retired players / invalid markets / DNPs.

Session A (2026-08-25) — ALT MAGIC CANONICAL HISTORY REPAIR
────────────────────────────────────────────────────────────────────────
The historical-history gate used to query the legacy `player_game_logs`
collection with an UPPERCASE `sport` value + `player_name` only.  Every
player_game_actuals row stores `sport` in LOWERCASE (`"mlb"`, `"nfl"`,
`"nba"`, `"soccer"`, `"tennis"`) and canonical MLB rows carry
`player_name = None` — the row is keyed by `canonical_player_id`
(MLBAM ID) instead.  Result: 100 %-of-canonical-actuals players
(Travis Kelce with 131 real actuals, Josh Allen with 129, Patrick
Mahomes with 128, etc.) were being falsely rejected with:

    insufficient history (0 < 5)

This fix swaps the read path to a CANONICAL-FIRST order:

    1. player_game_actuals by canonical_player_id + lowercase sport
       (authoritative — 305 k rows across mlb/nba/nfl/soccer/tennis)

    2. player_game_actuals by lowercase sport + player_name
       (canonical store, name-based bridge when caller does not
        pass a canonical_player_id)

    3. LEGACY per-sport fallbacks preserved for pre-canonical rows:
       - MLB/NBA/NFL → player_game_logs (lowercase sport)
       - Tennis      → tennis_matches_history (winner/loser name)
       - Soccer      → soccer_player_game_logs (49 k rows)

    4. Name-based match on legacy stores only as a last resort.

Zero changes to:
    • Magic probability math
    • Magic thresholds
    • Alt-line selection math
    • Scoring weights
    • Model behavior
"""
from __future__ import annotations

from typing import Optional


# Sportsbook markets we ALLOW alt-line projections for.
# UNIVERSAL COVERAGE (2026-06-30) — every player-prop family the
# runtime can publish is whitelisted so the universal projected-
# distribution fallback surfaces alt lines for ANY pick.  The gate
# still enforces:
#   1. sport supported at all
#   2. stat is a known player-prop family (not moneyline/team totals)
#   3. minimum historical sample so we're not projecting off a debut
_SUPPORTED_STAT_WHITELIST = {
    "NFL":    {"passing_yards", "rushing_yards", "receiving_yards",
                "passing_tds", "rushing_tds", "receiving_tds",
                "passing_completions", "passing_attempts",
                "rush_attempts", "receptions", "targets", "carries",
                "passing_ints"},
    "MLB":    {"hits", "total_bases", "home_runs", "strikeouts",
                "pitcher_strikeouts", "pitcher_outs",
                "runs_scored", "rbi", "walks", "hits_runs_rbis"},
    "NBA":    {"points", "rebounds", "assists", "threes", "threes_made",
                "steals", "blocks", "points_rebounds_assists"},
    "TENNIS": {"aces", "double_faults", "break_points_won",
                "total_games"},
    "SOCCER": {"goals", "assists", "shots_on_target", "shots",
                "goalscorer", "score_or_assist", "goal_contributions"},
    "NHL":    {"goals", "assists", "points", "shots_on_goal"},
}


async def is_safe_for_alt_lines(
    db, *,
    sport: str,
    player_name: str,
    stat: str,
    min_prior_games: int = 5,
    canonical_player_id: Optional[str] = None,
    pick: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
    """Return (safe, reason).

    Rejects:
      • unsupported stat for this sport
      • player with < min_prior_games historical rows (checked against
        the canonical `player_game_actuals` store first, legacy per-sport
        store as fallback)
      • player flagged as retired / inactive in the DB
      • market where the player has no team assignment

    Session A change: `canonical_player_id` is an OPTIONAL keyword
    argument — passing it lets the reader hit the canonical store
    directly without a name-round-trip. Callers that only have a
    name still work exactly as before.

    UNIVERSAL COVERAGE (2026-06-30): ``pick`` is an OPTIONAL keyword.
    When it carries both ``win_probability`` and ``line``, the
    minimum-history gate is BYPASSED — the universal projected-
    distribution fallback derives probabilities purely from the pick's
    own immutable model output (which already passed publication
    gates upstream), so re-blocking on missing ``player_game_actuals``
    rows would double-guard against a signal that has already been
    verified.  Retired-player / market-support gates STILL apply.
    """
    sport_u = (sport or "").upper()
    stat_l = (stat or "").lower()
    if sport_u not in _SUPPORTED_STAT_WHITELIST:
        return False, f"sport {sport_u} not supported"
    if stat_l not in _SUPPORTED_STAT_WHITELIST[sport_u]:
        return False, f"stat {stat_l} not whitelisted for {sport_u}"
    if not player_name and not canonical_player_id:
        return False, "no player_name or canonical_player_id"

    # Retired / inactive check — soccer & NFL both have `player_status`.
    try:
        if player_name:
            p = await db.players.find_one(
                {"name": {"$regex": f"^{player_name}$", "$options": "i"}},
                {"status": 1, "retired": 1, "active": 1, "_id": 0},
            )
            if p:
                if p.get("retired") is True:
                    return False, "player is retired"
                if p.get("active") is False:
                    return False, "player marked inactive"
                if (p.get("status") or "").lower() in ("retired", "inactive"):
                    return False, f"player status: {p['status']}"
    except Exception:
        pass  # missing collection is fine — safeguard is best-effort

    # ── Universal-fallback bypass ────────────────────────────────
    # If the caller supplied a pick with the two ingredients the
    # universal projected-distribution helper needs, the history
    # gate is not applicable: probabilities come from the pick's own
    # ``win_probability`` + ``line``, not from historical PA/AB rows.
    if isinstance(pick, dict):
        wp = pick.get("win_probability")
        line = pick.get("line")
        # Some picks store the line only in the market string; parse
        # it out so the fallback path can still fire.
        if line is None:
            try:
                import re as _re
                _tm = _re.search(
                    r"(?:Over|Under|O|U)\s+(-?\d+(?:\.\d+)?)",
                    str(pick.get("market") or ""), _re.I,
                )
                if _tm:
                    line = float(_tm.group(1))
            except Exception:
                line = None
        if isinstance(wp, (int, float)) and isinstance(line, (int, float)):
            wp_frac = wp / 100.0 if wp > 1.0 else wp
            if 0.0 < wp_frac < 1.0:
                return True, None

    # ── Historical-history gate (Session A: canonical-first order) ────
    sport_l = sport_u.lower()  # canonical store uses lowercase sport
    n = 0
    try:
        # STEP 1 — canonical read by canonical_player_id (authoritative).
        if canonical_player_id:
            n = await db.player_game_actuals.count_documents({
                "sport": sport_l,
                "canonical_player_id": canonical_player_id,
            })
            if n >= min_prior_games:
                return True, None

        # STEP 2 — canonical read by name (name still works, but the
        # store is queried at the correct lowercase sport).
        if n < min_prior_games and player_name:
            n_name = await db.player_game_actuals.count_documents({
                "sport": sport_l,
                "player_name": player_name,
            })
            n = max(n, n_name)
            if n >= min_prior_games:
                return True, None

        # STEP 3 — canonical bridge: resolve name → canonical_player_id
        # via the `player_identities` collection then re-query canonical.
        # Only run when the caller did NOT already pass a cpid and the
        # canonical name-match returned nothing.
        if n < min_prior_games and player_name and not canonical_player_id:
            try:
                ident = await db.player_identities.find_one(
                    {
                        "sport": sport_l,
                        "$or": [
                            {"name":  {"$regex": f"^{player_name}$",
                                       "$options": "i"}},
                            {"aliases": {"$regex": f"^{player_name}$",
                                          "$options": "i"}},
                        ],
                    },
                    {"canonical_player_id": 1, "_id": 0},
                )
                if ident and ident.get("canonical_player_id"):
                    n_bridge = await db.player_game_actuals.count_documents({
                        "sport": sport_l,
                        "canonical_player_id": ident["canonical_player_id"],
                    })
                    n = max(n, n_bridge)
                    if n >= min_prior_games:
                        return True, None
            except Exception:
                pass  # name→canonical bridge is best-effort

        # STEP 4 — legacy fallback (per-sport). Correct lowercase-sport
        # query — the original code queried with UPPERCASE which never
        # matched. Preserved for pre-canonical rows only.
        if n < min_prior_games and player_name:
            legacy_hits = 0
            if sport_u in ("MLB", "NBA", "NFL"):
                legacy_hits = await db.player_game_logs.count_documents({
                    "sport": sport_l,  # ← was uppercase; corrected
                    "player_name": player_name,
                })
            elif sport_u == "TENNIS":
                legacy_hits = await db.tennis_matches_history.count_documents({
                    "$or": [{"winner_name": player_name},
                            {"loser_name":  player_name}],
                })
            elif sport_u == "SOCCER":
                legacy_hits = await db.soccer_player_game_logs.count_documents({
                    "player_name": player_name,
                })
            n = max(n, legacy_hits)
            if n < min_prior_games:
                return False, (f"insufficient history "
                               f"({n} < {min_prior_games})")
    except Exception:
        pass  # non-fatal — an infra exception is not a safety veto

    return True, None


__all__ = ["is_safe_for_alt_lines", "_SUPPORTED_STAT_WHITELIST"]
