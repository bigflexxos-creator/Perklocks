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
#
# Session B (2026-08-25) — PitchAPI/Big Balls settlement wiring:
# The following market families have been PROVEN gradeable via real
# authenticated PitchAPI responses against real completed Perklocks
# fixtures (see routes/board_health_routes.settlement_probe live
# proofs on 2026-08-25):
#   • anytime goal scorer         → OK  (Cole Palmer / Muniz / Dybala)
#   • to score or assist          → OK  (via goals + assists extraction)
#   • player shots / SoT          → OK  (total_shots / ShotsOnTarget)
#   • total corners               → OK  (per-team corners extraction)
#   • cards                       → OK  (yellowcard + redcard events)
# These families were ALREADY listed as SETTLEMENT_UNSUPPORTED here
# because the previous scaffold had no adapter.  The five previously-
# unsupported patterns (shots / shots on target / cards / corners /
# total corners) are now REMOVED from the deny-list.  The new
# `settlement_bridge.resolve_completed_actual` provides the adapter.
_SOCCER_UNSUPPORTED_PATTERNS: list[tuple[str, str]] = [
    # First / last goalscorer — remains unsupported (needs
    # goal-minute ordering; not extracted yet in Session B).
    ("first goalscorer",              "settler_unsupported:soccer_first_goalscorer"),
    ("first goal scorer",             "settler_unsupported:soccer_first_goalscorer"),
    ("last goalscorer",               "settler_unsupported:soccer_last_goalscorer"),
    ("last goal scorer",              "settler_unsupported:soccer_last_goalscorer"),
    # Session B: promoted shots / SoT / cards / corners → SUPPORTED.
    # Complex derivative markets remain unsupported.
    ("corner range",                  "settler_unsupported:soccer_corner_range"),
    ("offsides",                      "settler_unsupported:soccer_offsides"),
    ("fouls ",                        "settler_unsupported:soccer_fouls"),
    ("free kicks",                    "settler_unsupported:soccer_fouls"),
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
    # Session B additions — proven via PitchAPI /v1/matches/{id}/players
    # and /v1/matches/{id}/events on 2026-08-25.
    "shots on target",
    "total shots",
    "player shots",
    "total cards",
    "cards ",
    "booking points",
    "total corners",
    "corners ",
)


def classify(sport: Optional[str], market: Optional[str],
             league: Optional[str] = None,
             line: Optional[float] = None) -> tuple[str, Optional[str]]:
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

    SLICE 5 (2026-08-26) — Quarter Asian-handicap fail-closed.  Books
    publish quarter lines as plain "Team X +0.25" without the token
    "asian handicap", so they bypassed the deny-list and reached the
    ledger — which cannot represent half-win/half-loss / half-push
    outcomes correctly and would corrupt W/L/ROI. Fail closed when
    ``line`` is a quarter multiple (…, -0.75, -0.25, 0.25, 0.75, …).
    """
    sp = (sport or "").strip().lower()
    mk = (market or "").strip().lower()
    if not sp or not mk:
        return (UNKNOWN, None)
    # SLICE 5 quarter-line fail-closed (Soccer/spread markets).
    if sp == "soccer" and line is not None:
        try:
            L = float(line)
            # Quarter multiple iff 4L is integer but 2L is not (i.e.,
            # x.25 or x.75 fractional part). Whole numbers and .5
            # increments are handled by the standard spread settler.
            q4, q2 = L * 4, L * 2
            if abs(q4 - round(q4)) < 1e-6 and abs(q2 - round(q2)) > 1e-6:
                return (UNSUPPORTED,
                        "settler_unsupported:soccer_asian_quarter_handicap")
        except (TypeError, ValueError):
            pass
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
                 league: Optional[str] = None,
                 line: Optional[float] = None) -> bool:
    """Convenience — True iff status == SUPPORTED."""
    return classify(sport, market, league, line)[0] == SUPPORTED


def is_unsupported(sport: Optional[str], market: Optional[str],
                   league: Optional[str] = None,
                   line: Optional[float] = None) -> bool:
    """Convenience — True iff status == SETTLEMENT_UNSUPPORTED."""
    return classify(sport, market, league, line)[0] == UNSUPPORTED


__all__ = [
    "SUPPORTED", "UNSUPPORTED", "UNKNOWN",
    "classify", "is_supported", "is_unsupported",
]
