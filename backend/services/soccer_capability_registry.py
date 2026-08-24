"""Central Soccer Capability Registry — Session B (2026-06).

ONE shared registry that tracks per-league × per-market real-provider
capability with the granularity the P0 SESSION B directive demands.

Do NOT collapse everything into a supported/unsupported boolean.  Each
capability dimension is tracked separately so operators can see
exactly which market a league covers and which is UNAVAILABLE /
CURRENT_PROVIDER_UNAVAILABLE / UNVERIFIED / NO_CURRENT_EVENTS.

Design
──────
* Immutable module-level ``LEAGUE_CAPABILITIES`` dict + query helpers.
* Capability values are enum strings — never booleans — so we can
  distinguish "no current event today" from "the provider doesn't
  support this market for this league at all".
* Capability verification timestamp is recorded per league (the
  measurement date the entries were last confirmed).  Sessions that
  do NOT re-audit MUST NOT edit the verification timestamp.
* The registry is CONSUMED by the Session-A canonical publication
  boundary (see ``services.soccer_market_gate.classify_market``).  If
  a producer emits a market for a league where the capability is
  UNAVAILABLE / CURRENT_PROVIDER_UNAVAILABLE / UNVERIFIED, the
  producer MUST also set ``no_real_book_line=True`` and
  ``odds_source="MODEL_ONLY"`` — otherwise the canonical boundary
  rejects the pick as SYNTHETIC_BOOK_ODDS.

Capability values
─────────────────
    REAL_VERIFIED             — live provider proof exists, real
                                sportsbook lines returned.
    NO_CURRENT_EVENTS         — provider supports it in principle,
                                but no matching event on the current
                                slate.  Different from BROKEN.
    UNAVAILABLE               — provider explicitly does not carry
                                this market for this league at all
                                (measured).
    CURRENT_PROVIDER_UNAVAILABLE
                              — currently configured provider does
                                NOT return this league.  A different
                                provider could activate it later.
    UNVERIFIED                — never been probed / not measured yet.
                                Treated as UNAVAILABLE at runtime
                                until proven.

Market keys
───────────
Game markets:      h2h / spreads / totals / btts / double_chance
Player markets:    anytime_goalscorer / first_goalscorer / assist /
                   score_or_assist / shots / shots_on_target
Non-market:        fixture_support / player_identity / roster_source /
                   scorer_form_source / player_history / team_history /
                   sportsbook_provider

Measurement source (verification_at)
────────────────────────────────────
Session A's live provider capability probe on 2026-06 (see
/tmp/soccer_provider_capability_report.md).  Any later probe MUST
update ``verification_at`` on the leagues it touched.
"""
from __future__ import annotations

import enum
from typing import Any


class Capability(str, enum.Enum):
    REAL_VERIFIED                 = "REAL_VERIFIED"
    NO_CURRENT_EVENTS             = "NO_CURRENT_EVENTS"
    UNAVAILABLE                   = "UNAVAILABLE"
    CURRENT_PROVIDER_UNAVAILABLE  = "CURRENT_PROVIDER_UNAVAILABLE"
    UNVERIFIED                    = "UNVERIFIED"


# Canonical market keys (aligned with pipeline_diagnostic + player_history/soccer).
MARKET_KEYS: tuple[str, ...] = (
    # Game markets
    "h2h", "spreads", "totals", "btts", "double_chance",
    # Player markets
    "anytime_goalscorer", "first_goalscorer",
    "assist", "score_or_assist",
    "shots", "shots_on_target",
)

# Non-market capability dimensions (per user Rule 1).
NON_MARKET_DIMS: tuple[str, ...] = (
    "fixture_support",
    "player_identity",
    "roster_source",
    "scorer_form_source",
    "player_history",
    "team_history",
    "sportsbook_provider",
)


def _entry(
    *,
    odds_api_sport_key: str | None,
    verification_at: str,
    game_markets: dict[str, Capability],
    player_markets: dict[str, Capability],
    fixture_support: Capability = Capability.UNVERIFIED,
    player_identity: str = "UNVERIFIED",
    roster_source: str = "UNVERIFIED",
    scorer_form_source: str = "UNVERIFIED",
    player_history: str = "UNVERIFIED",
    team_history: str = "UNVERIFIED",
    sportsbook_provider: str = "UNVERIFIED",
    notes: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "odds_api_sport_key":  odds_api_sport_key,
        "verification_at":   verification_at,
        "fixture_support":   fixture_support.value,
        "player_identity":   player_identity,
        "roster_source":     roster_source,
        "scorer_form_source": scorer_form_source,
        "player_history":    player_history,
        "team_history":      team_history,
        "sportsbook_provider": sportsbook_provider,
        "notes":             notes,
    }
    for m in MARKET_KEYS:
        if m in ("anytime_goalscorer", "first_goalscorer", "assist",
                  "score_or_assist", "shots", "shots_on_target"):
            out[m] = player_markets.get(
                m, Capability.UNVERIFIED,
            ).value
        else:
            out[m] = game_markets.get(m, Capability.UNVERIFIED).value
    return out


# ═══════════════════════════════════════════════════════════════════
# The registry — the single source of truth
# ═══════════════════════════════════════════════════════════════════
LEAGUE_CAPABILITIES: dict[str, dict[str, Any]] = {

    # ── Top-5 European leagues (game odds routinely covered by The
    # Odds API; player-prop coverage is event-by-event / not
    # exhaustively re-measured in Session A's probe).
    "EPL": _entry(
        odds_api_sport_key="soccer_epl",
        verification_at="2026-06",
        # ── EPL — Big 5 flagship (REAL_VERIFIED across the full
        # supported family set post 2026-06 FULL FINAL PRODUCTION FIX).
        # Provider returns live markets for game families + Anytime
        # Goalscorer + Score-or-Assist; First Goalscorer intentionally
        # removed from acquisition (settlement authority unavailable).
        game_markets={
            "h2h":            Capability.REAL_VERIFIED,
            "spreads":        Capability.REAL_VERIFIED,
            "totals":         Capability.REAL_VERIFIED,
            "btts":           Capability.REAL_VERIFIED,
            "double_chance":  Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.REAL_VERIFIED,
            "score_or_assist":    Capability.REAL_VERIFIED,
            # first_goalscorer intentionally omitted — no settlement
            # authority, dropped from acquisition.
            "assist":             Capability.UNVERIFIED,
            "shots":              Capability.UNVERIFIED,
            "shots_on_target":    Capability.UNVERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + Understat",
        roster_source="ESPN core.api",
        scorer_form_source="Understat",
        player_history="team_history+player_history collections",
        team_history="team_history collection",
        sportsbook_provider="the_odds_api",
        notes=("Big-5 flagship; full family reachability certified "
                "for game markets + scorer + score-or-assist."),
    ),
    "La Liga": _entry(
        odds_api_sport_key="soccer_spain_la_liga",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.NO_CURRENT_EVENTS,
            "double_chance":   Capability.NO_CURRENT_EVENTS,
        },
        player_markets={
            "anytime_goalscorer": Capability.NO_CURRENT_EVENTS,
            "first_goalscorer":   Capability.NO_CURRENT_EVENTS,
            "assist":             Capability.UNVERIFIED,
            "score_or_assist":    Capability.UNVERIFIED,
            "shots":              Capability.UNVERIFIED,
            "shots_on_target":    Capability.UNVERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + Understat",
        roster_source="ESPN core.api",
        scorer_form_source="Understat",
        player_history="team_history+player_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
    ),
    "Serie A": _entry(
        odds_api_sport_key="soccer_italy_serie_a",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.NO_CURRENT_EVENTS,
            "double_chance":   Capability.NO_CURRENT_EVENTS,
        },
        player_markets={
            "anytime_goalscorer": Capability.NO_CURRENT_EVENTS,
            "first_goalscorer":   Capability.NO_CURRENT_EVENTS,
            "assist":             Capability.UNVERIFIED,
            "score_or_assist":    Capability.UNVERIFIED,
            "shots":              Capability.UNVERIFIED,
            "shots_on_target":    Capability.UNVERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + Understat",
        roster_source="ESPN core.api",
        scorer_form_source="Understat",
        player_history="team_history+player_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
    ),
    "Bundesliga": _entry(
        odds_api_sport_key="soccer_germany_bundesliga",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.NO_CURRENT_EVENTS,
            "double_chance":   Capability.NO_CURRENT_EVENTS,
        },
        player_markets={
            "anytime_goalscorer": Capability.NO_CURRENT_EVENTS,
            "first_goalscorer":   Capability.NO_CURRENT_EVENTS,
            "assist":             Capability.UNVERIFIED,
            "score_or_assist":    Capability.UNVERIFIED,
            "shots":              Capability.UNVERIFIED,
            "shots_on_target":    Capability.UNVERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + Understat",
        roster_source="ESPN core.api",
        scorer_form_source="Understat",
        player_history="team_history+player_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
    ),
    "Ligue 1": _entry(
        odds_api_sport_key="soccer_france_ligue_one",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.NO_CURRENT_EVENTS,
            "double_chance":   Capability.NO_CURRENT_EVENTS,
        },
        player_markets={
            "anytime_goalscorer": Capability.NO_CURRENT_EVENTS,
            "first_goalscorer":   Capability.NO_CURRENT_EVENTS,
            "assist":             Capability.UNVERIFIED,
            "score_or_assist":    Capability.UNVERIFIED,
            "shots":              Capability.UNVERIFIED,
            "shots_on_target":    Capability.UNVERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + Understat",
        roster_source="ESPN core.api",
        scorer_form_source="Understat",
        player_history="team_history+player_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
    ),

    # ── Champions League (game-only per Rule 5)
    "Champions League": _entry(
        odds_api_sport_key="soccer_uefa_champs_league",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.NO_CURRENT_EVENTS,
            "double_chance":   Capability.NO_CURRENT_EVENTS,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api",
        roster_source="ESPN core.api",
        scorer_form_source="Understat (Big-5 clubs only)",
        player_history="team_history+player_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
        notes=("Game odds routinely available; player markets NOT wired "
                "for UCL as of Session-A probe (Rule 5)."),
    ),

    # ── MLS (the only requested league with proven player-prop coverage)
    "MLS": _entry(
        odds_api_sport_key="soccer_usa_mls",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.REAL_VERIFIED,
            "double_chance":   Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.REAL_VERIFIED,
            "first_goalscorer":   Capability.UNVERIFIED,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.REAL_VERIFIED,
            "shots_on_target":    Capability.REAL_VERIFIED,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN core.api + ESPN MLS stats",
        roster_source="ESPN MLS stats",
        scorer_form_source="ESPN MLS stats",
        player_history="mls_player_matchup_history",
        team_history="team_history",
        sportsbook_provider="the_odds_api",
        notes=("The Odds API measured 3 books for anytime goalscorer, "
                "2 for SOT, 1 for shots.  Assist/SOA UNAVAILABLE "
                "globally on TOA measurement."),
    ),

    # ── Small leagues — real game odds, NO real player markets (Rule 6)
    "Chinese Super League": _entry(
        odds_api_sport_key="soccer_china_superleague",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.REAL_VERIFIED,
            "double_chance":   Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN scoreboard (limited)",
        roster_source="ESPN CSL leaderboard",
        scorer_form_source="CSL ESPN leaderboard",
        player_history="UNAVAILABLE",
        team_history="team_history (partial)",
        sportsbook_provider="the_odds_api",
        notes="Real game odds only; NO real player markets available.",
    ),
    "Allsvenskan": _entry(
        odds_api_sport_key="soccer_sweden_allsvenskan",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.REAL_VERIFIED,
            "double_chance":   Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN scoreboard (limited)",
        roster_source="ESPN scoreboard",
        scorer_form_source="Wikipedia top-scorers (best-effort)",
        player_history="UNAVAILABLE",
        team_history="UNAVAILABLE",
        sportsbook_provider="the_odds_api",
    ),
    "Superettan": _entry(
        odds_api_sport_key="soccer_sweden_superettan",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.REAL_VERIFIED,
            "double_chance":   Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="UNVERIFIED",
        roster_source="UNVERIFIED",
        scorer_form_source="UNAVAILABLE",
        player_history="UNAVAILABLE",
        team_history="UNAVAILABLE",
        sportsbook_provider="the_odds_api",
    ),
    "Eliteserien": _entry(
        odds_api_sport_key="soccer_norway_eliteserien",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.REAL_VERIFIED,
            "double_chance":   Capability.REAL_VERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="ESPN scoreboard (limited)",
        roster_source="ESPN scoreboard",
        scorer_form_source="Wikipedia top-scorers (best-effort)",
        player_history="UNAVAILABLE",
        team_history="UNAVAILABLE",
        sportsbook_provider="the_odds_api",
    ),
    "Argentina Liga Profesional": _entry(
        odds_api_sport_key="soccer_argentina_primera_division",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.REAL_VERIFIED,
            "spreads": Capability.REAL_VERIFIED,
            "totals":  Capability.REAL_VERIFIED,
            "btts":            Capability.UNVERIFIED,
            "double_chance":   Capability.UNVERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.REAL_VERIFIED,
        player_identity="UNVERIFIED",
        roster_source="UNVERIFIED",
        scorer_form_source="UNAVAILABLE",
        player_history="UNAVAILABLE",
        team_history="UNAVAILABLE",
        sportsbook_provider="the_odds_api",
        notes=("Game odds proven; BTTS/DC not returned by TOA in "
                "Session-A probe (kept UNVERIFIED)."),
    ),

    # ── Currently provider-unavailable (Rule 7) — do not remove
    "OBOS-ligaen": _entry(
        odds_api_sport_key=None,
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.CURRENT_PROVIDER_UNAVAILABLE,
            "spreads": Capability.CURRENT_PROVIDER_UNAVAILABLE,
            "totals":  Capability.CURRENT_PROVIDER_UNAVAILABLE,
            "btts":            Capability.CURRENT_PROVIDER_UNAVAILABLE,
            "double_chance":   Capability.CURRENT_PROVIDER_UNAVAILABLE,
        },
        player_markets={
            m: Capability.CURRENT_PROVIDER_UNAVAILABLE
            for m in ("anytime_goalscorer", "first_goalscorer",
                       "assist", "score_or_assist",
                       "shots", "shots_on_target")
        },
        fixture_support=Capability.CURRENT_PROVIDER_UNAVAILABLE,
        player_identity="CURRENT_PROVIDER_UNAVAILABLE",
        roster_source="CURRENT_PROVIDER_UNAVAILABLE",
        scorer_form_source="CURRENT_PROVIDER_UNAVAILABLE",
        player_history="CURRENT_PROVIDER_UNAVAILABLE",
        team_history="CURRENT_PROVIDER_UNAVAILABLE",
        sportsbook_provider="none_configured",
        notes=("Not in The Odds API catalog.  Registry preserves the "
                "entry so a new provider can activate later."),
    ),
    "NWSL": _entry(
        odds_api_sport_key=None,
        verification_at="2026-06",
        game_markets={
            m: Capability.CURRENT_PROVIDER_UNAVAILABLE
            for m in ("h2h", "spreads", "totals", "btts", "double_chance")
        },
        player_markets={
            m: Capability.CURRENT_PROVIDER_UNAVAILABLE
            for m in ("anytime_goalscorer", "first_goalscorer",
                       "assist", "score_or_assist",
                       "shots", "shots_on_target")
        },
        fixture_support=Capability.CURRENT_PROVIDER_UNAVAILABLE,
        player_identity="CURRENT_PROVIDER_UNAVAILABLE",
        roster_source="CURRENT_PROVIDER_UNAVAILABLE",
        scorer_form_source="CURRENT_PROVIDER_UNAVAILABLE",
        player_history="CURRENT_PROVIDER_UNAVAILABLE",
        team_history="CURRENT_PROVIDER_UNAVAILABLE",
        sportsbook_provider="none_configured",
        notes="Not in The Odds API catalog.  Awaiting alternate provider.",
    ),
    "Argentina Primera Nacional": _entry(
        odds_api_sport_key=None,
        verification_at="2026-06",
        game_markets={
            m: Capability.CURRENT_PROVIDER_UNAVAILABLE
            for m in ("h2h", "spreads", "totals", "btts", "double_chance")
        },
        player_markets={
            m: Capability.CURRENT_PROVIDER_UNAVAILABLE
            for m in ("anytime_goalscorer", "first_goalscorer",
                       "assist", "score_or_assist",
                       "shots", "shots_on_target")
        },
        fixture_support=Capability.CURRENT_PROVIDER_UNAVAILABLE,
        player_identity="CURRENT_PROVIDER_UNAVAILABLE",
        roster_source="CURRENT_PROVIDER_UNAVAILABLE",
        scorer_form_source="CURRENT_PROVIDER_UNAVAILABLE",
        player_history="CURRENT_PROVIDER_UNAVAILABLE",
        team_history="CURRENT_PROVIDER_UNAVAILABLE",
        sportsbook_provider="none_configured",
        notes="Not in The Odds API catalog.  Awaiting alternate provider.",
    ),

    # ── Saudi Pro League (Rule 8)
    "Saudi Pro League": _entry(
        odds_api_sport_key="soccer_saudi_arabia_pro_league",
        verification_at="2026-06",
        game_markets={
            "h2h":     Capability.UNVERIFIED,
            "spreads": Capability.UNVERIFIED,
            "totals":  Capability.UNVERIFIED,
            "btts":            Capability.UNVERIFIED,
            "double_chance":   Capability.UNVERIFIED,
        },
        player_markets={
            "anytime_goalscorer": Capability.UNAVAILABLE,
            "first_goalscorer":   Capability.UNAVAILABLE,
            "assist":             Capability.UNAVAILABLE,
            "score_or_assist":    Capability.UNAVAILABLE,
            "shots":              Capability.UNAVAILABLE,
            "shots_on_target":    Capability.UNAVAILABLE,
        },
        fixture_support=Capability.UNVERIFIED,
        player_identity="ESPN scoreboard (ksa.1)",
        roster_source="ESPN scoreboard",
        scorer_form_source="UNAVAILABLE",
        player_history="UNAVAILABLE",
        team_history="UNAVAILABLE",
        sportsbook_provider="the_odds_api",
        notes=("Registered.  Player props NEVER guessed until an actual "
                "configured provider proves it (Rule 8)."),
    ),
}


# ═══════════════════════════════════════════════════════════════════
# Query helpers
# ═══════════════════════════════════════════════════════════════════

def get_league(name: str) -> dict[str, Any] | None:
    return LEAGUE_CAPABILITIES.get(name)


def market_status(league: str, market_key: str) -> str:
    """Return the capability status (enum value) for a league × market
    pair.  Unknown → ``UNVERIFIED``."""
    entry = LEAGUE_CAPABILITIES.get(league) or {}
    return str(entry.get(market_key) or Capability.UNVERIFIED.value)


def is_real_market(league: str, market_key: str) -> bool:
    """True only when the market is REAL_VERIFIED for the league."""
    return market_status(league, market_key) == Capability.REAL_VERIFIED.value


def leagues_with_real_market(market_key: str) -> list[str]:
    return [
        name for name, entry in LEAGUE_CAPABILITIES.items()
        if entry.get(market_key) == Capability.REAL_VERIFIED.value
    ]


def matrix() -> dict[str, dict[str, Any]]:
    """Shallow copy — for read-only observability endpoints."""
    return {k: dict(v) for k, v in LEAGUE_CAPABILITIES.items()}


def summary() -> dict[str, Any]:
    """Compact summary: counts per capability value."""
    counts: dict[str, int] = {}
    for entry in LEAGUE_CAPABILITIES.values():
        for m in MARKET_KEYS:
            v = str(entry.get(m) or "")
            counts[v] = counts.get(v, 0) + 1
    return {
        "total_leagues":  len(LEAGUE_CAPABILITIES),
        "leagues":        list(LEAGUE_CAPABILITIES.keys()),
        "market_keys":    list(MARKET_KEYS),
        "capability_counts": counts,
    }


__all__ = [
    "Capability",
    "MARKET_KEYS",
    "NON_MARKET_DIMS",
    "LEAGUE_CAPABILITIES",
    "get_league",
    "market_status",
    "is_real_market",
    "leagues_with_real_market",
    "matrix",
    "summary",
]
