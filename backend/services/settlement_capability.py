"""Phase A — Settlement Capability Registry (2026-06).

Small, read-only capability oracle answering ONE question:

    "Given (sport, market, league) — do we have an authoritative
     grading path?  If not, why?"

USED BY
-------
- Settlement engine (main + soccer batch):
    Terminates picks whose markets we CANNOT grade via any current
    settler, so they exit the queue with a canonical VOID + source
    ``settler_unsupported`` instead of starving the loop forever.

- (Future — Phase B/C):
    Generation surface may consult ``is_supported`` to prevent
    ``SETTLEMENT_UNSUPPORTED`` markets from ever becoming actionable
    wagers.  Not wired in Phase A per budget.

DESIGN NOTES
------------
* Read-only — no DB access, no I/O, no state.  Pure classification.
* Reflects the settlers that actually live in the codebase today:
    - settlement_engine.py         (moneyline / spread / totals / team-total)
    - soccer_espn_settle.py        (soccer moneyline / totals / btts /
                                    anytime goal scorer / to score or assist /
                                    win-or-draw / double-chance)
    - prop_settlement.py           (player props via MLB Stats / ESPN)
    - espn_settlement.py           (tennis / ufc / player props via ESPN)
* Conservative "unsupported" set — anything we CANNOT confidently grade
  is marked ``SETTLEMENT_UNSUPPORTED``.  Anything ambiguous stays
  ``UNKNOWN`` and is NOT terminated (defensive — keeps existing
  supported picks flowing).

TERMINAL REASONS (canonical strings — stable across releases)
    settler_unsupported:soccer_shots
    settler_unsupported:soccer_shots_on_target
    settler_unsupported:soccer_cards
    settler_unsupported:soccer_corners
    settler_unsupported:soccer_first_goalscorer
    settler_unsupported:soccer_last_goalscorer
    settler_unsupported:soccer_score_at_ht
    settler_unsupported:soccer_htft
    settler_unsupported:soccer_penalty_taken
    settler_unsupported:soccer_asian_handicap
    settler_unsupported:soccer_corner_range
    settler_unsupported:generic_unknown

The registry is intentionally VERBOSE and terminal-reason keyed so
telemetry and audit reports can bucket ungraded picks by reason.
"""
from __future__ import annotations

from typing import Optional

# ── Canonical status vocabulary ───────────────────────────────────────
SUPPORTED = "SUPPORTED"
UNSUPPORTED = "SETTLEMENT_UNSUPPORTED"
UNKNOWN = "UNKNOWN"

# ── Soccer capability table ───────────────────────────────────────────
# Substring match, lowercase.  First match wins.  Order matters — more
# specific patterns should sit above generic ones.
_SOCCER_UNSUPPORTED_PATTERNS: list[tuple[str, str]] = [
    # First / last goalscorer — the ESPN summary emits only ONE scoring
    # order (goal minute), but the pipeline has no first-scorer settler
    # that respects the minute-ordered evidence.  Anytime IS supported.
    ("first goalscorer",              "settler_unsupported:soccer_first_goalscorer"),
    ("first goal scorer",             "settler_unsupported:soccer_first_goalscorer"),
    ("last goalscorer",               "settler_unsupported:soccer_last_goalscorer"),
    ("last goal scorer",              "settler_unsupported:soccer_last_goalscorer"),
    # Shots / SoT / cards / corners / offsides — no authoritative stat
    # source wired into any current settler.  Free-tier ESPN /summary
    # exposes some of these but the pipeline has no adapter for them.
    ("shots on target",               "settler_unsupported:soccer_shots_on_target"),
    ("total shots",                   "settler_unsupported:soccer_shots"),
    ("player shots",                  "settler_unsupported:soccer_shots"),
    ("shots ",                        "settler_unsupported:soccer_shots"),
    ("cards ",                        "settler_unsupported:soccer_cards"),
    ("total cards",                   "settler_unsupported:soccer_cards"),
    ("booking points",                "settler_unsupported:soccer_cards"),
    ("corners ",                      "settler_unsupported:soccer_corners"),
    ("total corners",                 "settler_unsupported:soccer_corners"),
    ("corner range",                  "settler_unsupported:soccer_corner_range"),
    ("offsides",                      "settler_unsupported:soccer_offsides"),
    ("fouls ",                        "settler_unsupported:soccer_fouls"),
    ("free kicks",                    "settler_unsupported:soccer_fouls"),
    # Complex derivative markets — HT/FT, half-time score, penalty taken
    ("half time / full time",         "settler_unsupported:soccer_htft"),
    ("ht/ft",                         "settler_unsupported:soccer_htft"),
    ("half-time score",               "settler_unsupported:soccer_score_at_ht"),
    ("half time score",               "settler_unsupported:soccer_score_at_ht"),
    ("correct score",                 "settler_unsupported:soccer_correct_score"),
    ("penalty taken",                 "settler_unsupported:soccer_penalty_taken"),
    ("penalty scored",                "settler_unsupported:soccer_penalty_taken"),
    ("asian handicap",                "settler_unsupported:soccer_asian_handicap"),
]

# Soccer markets we DO grade — allow-list for defensive checking.
_SOCCER_SUPPORTED_PATTERNS: tuple[str, ...] = (
    "moneyline",
    "win or draw",
    "double chance",
    "draw no bet",
    "total goals",
    "both teams to score",
    "btts",
    "anytime goal scorer",
    "to score or assist",
    "score & assist",
    "score or assist",
    # Underscored variants used by Odds API keys directly.
    "goal scorer anytime",
    "player goal scorer anytime",
    "player to score or assist",
    "player first goal scorer",   # explicit — still classified separately below
)


def classify(sport: Optional[str], market: Optional[str],
             league: Optional[str] = None) -> tuple[str, Optional[str]]:
    """Return ``(status, terminal_reason)`` for a (sport, market) pair.

    Status ∈ {SUPPORTED, SETTLEMENT_UNSUPPORTED, UNKNOWN}.
    Terminal reason is a stable dot-namespaced string used by
    telemetry / analytics buckets.  ``None`` when status == SUPPORTED
    or UNKNOWN (nothing to bucket).

    Rules:
    * SUPPORTED  — matches an explicit allow-list for the sport.
    * SETTLEMENT_UNSUPPORTED — matches the explicit deny-list for the sport.
    * UNKNOWN    — neither list matches; caller MUST leave pending.
                   We refuse to terminate on speculation.
    """
    sp = (sport or "").strip().lower()
    mk = (market or "").strip().lower()
    if not sp or not mk:
        return (UNKNOWN, None)
    # Normalize underscored market_keys (e.g. "player_shots_on_target")
    # to the human-readable form ("player shots on target") the deny/
    # allow patterns are authored against.  Both forms are checked so
    # the classifier is robust to either producer convention.
    mk_norm = mk.replace("_", " ")
    mk_forms = (mk, mk_norm) if mk != mk_norm else (mk,)

    # ── Soccer ────────────────────────────────────────────────────
    if sp == "soccer":
        # Deny-list first (more specific).
        for pat, reason in _SOCCER_UNSUPPORTED_PATTERNS:
            if any(pat in form for form in mk_forms):
                return (UNSUPPORTED, reason)
        # Allow-list.
        for pat in _SOCCER_SUPPORTED_PATTERNS:
            if any(pat in form for form in mk_forms):
                return (SUPPORTED, None)
        return (UNKNOWN, None)

    # ── Non-soccer sports — supported by settlement_engine.py ─────
    # moneyline / spread / run-line / puck-line / handicap / total /
    # team total.  Any market containing one of these tokens is a
    # game-line and gradable.
    game_tokens = (
        "moneyline", "spread", "run line", "puck line", "handicap",
        "total ", "total goals", "total runs", "total points",
        "total games", "team total", "win or draw",
    )
    if any(t in form for t in game_tokens for form in mk_forms):
        return (SUPPORTED, None)

    # Player-prop markets across MLB / NBA / WNBA / Tennis / UFC —
    # handled by prop_settlement.py / espn_settlement.py.  The
    # is_player_prop() heuristic in settlement_engine treats leagues
    # containing "props" as player-props; we mirror that here.
    if (league or "").strip().lower().endswith("props"):
        return (SUPPORTED, None)

    return (UNKNOWN, None)


def is_supported(sport: Optional[str], market: Optional[str],
                 league: Optional[str] = None) -> bool:
    """Convenience — True iff status == SUPPORTED."""
    return classify(sport, market, league)[0] == SUPPORTED


def is_unsupported(sport: Optional[str], market: Optional[str],
                   league: Optional[str] = None) -> bool:
    """Convenience — True iff status == SETTLEMENT_UNSUPPORTED."""
    return classify(sport, market, league)[0] == UNSUPPORTED


__all__ = [
    "SUPPORTED", "UNSUPPORTED", "UNKNOWN",
    "classify", "is_supported", "is_unsupported",
]
