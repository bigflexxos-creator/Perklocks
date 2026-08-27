"""Player → Event Identity Gate (Phase 9B/9C/9F, 2026-07).

The Phase 9 mandate:

    A production defect has been observed where a Soccer player prop was
    associated with a fixture that did not contain that player's team.
    This class of defect must become impossible.

This module is the ONE authoritative cross-sport validation contract that
answers the question:

    "Does the player named on this pick actually belong to this event?"

It runs BEFORE model scoring / simulator / Magic / Lock Score / Apex /
canonical publication.  It also runs INSIDE
``canonical_publication_boundary.evaluate_publication`` as defense-in-
depth so an upstream skip cannot slip an invalid identity onto the board.

Design rules (per Phase 9, refined by Phase 10A + post-Phase-10 audit)
─────────────────────────────────────────────────────────────────────
* Fail-closed for provable mismatches — no silent attach to the most
  likely event, no confidence downgrade.
* Fail-closed for provably UNRESOLVABLE player identities (Phase 10A):
  if we cannot prove membership, we do not publish the player prop.
* Team-side markets (moneyline / spread / total) are NOT_APPLICABLE
  because the "player" concept does not apply.
* Player-market detection is CANONICAL-KEY AWARE
  (post-Phase-10 audit fix): recognises both human-readable display
  labels ("Anytime Goal Scorer", "Passing Yards", …) AND provider /
  canonical market keys ("player_goal_scorer_anytime",
  "player_pass_yds", …).  Reads ``market``, ``market_key``, AND
  ``provider_market_key`` so a canonical-key producer cannot bypass
  identity validation by choosing a non-display label.
* Reuse `services.pick_identity_authority` normalization helpers where
  practical.  DO NOT construct a competing identity table.

Terminal reasons emitted
────────────────────────
    VALID                          — player belongs to event.
    NOT_APPLICABLE                 — market has no player concept
                                     (team-side / totals).
    PLAYER_TEAM_UNRESOLVED         — pick lacks enough enrichment to
                                     prove membership.  FAIL-CLOSED
                                     for publication (Phase 10A).
    PLAYER_EVENT_IDENTITY_MISMATCH — proven mismatch.  Fail-closed.

This module is intentionally pure/synchronous — no DB I/O.  All inputs
are read directly from the pick dict.  Callers wanting to enrich a pick
with authoritative team names should do so BEFORE calling the gate.
"""
from __future__ import annotations

import enum
import re
import unicodedata
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════════
# Verdict types
# ═══════════════════════════════════════════════════════════════════════
class IdentityVerdict(str, enum.Enum):
    VALID                          = "VALID"
    NOT_APPLICABLE                 = "NOT_APPLICABLE"
    PLAYER_TEAM_UNRESOLVED         = "PLAYER_TEAM_UNRESOLVED"
    PLAYER_EVENT_IDENTITY_MISMATCH = "PLAYER_EVENT_IDENTITY_MISMATCH"


# Markets where a "player" is the participant of interest.  Anything not
# matching one of these prefixes is treated as team-side and skipped.
_PLAYER_MARKET_TOKENS: tuple[str, ...] = (
    # Soccer
    "anytime goal scorer",
    "first goal scorer",
    "to score or assist",
    "score or assist",
    "score & assist",
    # MLB
    "hits", "total bases", "home runs", "rbi",
    "strikeouts", "pitcher outs", "walks allowed", "earned runs",
    # NFL
    "passing yards", "passing tds", "pass yards",
    "rushing yards", "rushing tds", "rush yards",
    "receiving yards", "receiving tds", "rec yards",
    "receptions",
    "anytime touchdown", "atd", "first td",
    # NBA
    "points", "rebounds", "assists", "pra",
    "threes made", "player points",
    # Tennis: player is always the selection itself; handled separately.
)


# Post-Phase-10 audit fix — CANONICAL PROVIDER MARKET KEYS.
# Producers routinely stamp picks with normalized keys such as
# ``player_goal_scorer_anytime`` instead of the human-readable label
# ``Anytime Goal Scorer``.  Prior to this registry, the substring match
# above missed the canonical form (e.g. no "anytime goal scorer" spaces)
# and the gate returned NOT_APPLICABLE, silently bypassing player-event
# validation.  This registry closes the bypass by matching normalized
# market keys AND display labels against BOTH `market` and
# `market_key`/`provider_market_key` fields.
_CANONICAL_PLAYER_MARKET_KEYS: frozenset[str] = frozenset({
    # ── Soccer ───────────────────────────────────────────────────────
    "player_goal_scorer_anytime",
    "player_first_goal_scorer",
    "player_last_goal_scorer",
    "player_to_score_or_assist",
    "player_shots_on_target",
    "player_shots",
    "player_assists_soccer",
    # ── MLB ──────────────────────────────────────────────────────────
    "batter_hits", "batter_total_bases",
    "batter_home_runs", "batter_rbis", "batter_runs_scored",
    "batter_stolen_bases", "batter_walks",
    "pitcher_strikeouts", "pitcher_outs",
    "pitcher_hits_allowed", "pitcher_walks_allowed",
    "pitcher_earned_runs", "pitcher_record_a_win",
    # Real alt-line MLB equivalents
    "batter_hits_alternate", "batter_total_bases_alternate",
    "batter_home_runs_alternate",
    "pitcher_strikeouts_alternate", "pitcher_outs_alternate",
    "pitcher_hits_allowed_alternate", "pitcher_earned_runs_alternate",
    # ── NFL ──────────────────────────────────────────────────────────
    "player_pass_yds", "player_pass_tds", "player_pass_completions",
    "player_pass_attempts", "player_pass_interceptions",
    "player_rush_yds", "player_rush_tds", "player_rush_attempts",
    "player_reception_yds", "player_reception_tds",
    "player_receptions",
    "player_anytime_td", "player_first_td", "player_last_td",
    "player_1st_td",
    "player_pats", "player_field_goals",
    # Real alt-line NFL equivalents
    "player_pass_yds_alternate", "player_pass_tds_alternate",
    "player_rush_yds_alternate", "player_rush_tds_alternate",
    "player_reception_yds_alternate", "player_reception_tds_alternate",
    "player_receptions_alternate", "player_anytime_td_alternate",
    # ── NBA ──────────────────────────────────────────────────────────
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_blocks", "player_steals",
    "player_turnovers", "player_double_double", "player_triple_double",
    # Fused NBA markets — PRA / PR / PA / RA
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
    # Real alt-line NBA equivalents
    "player_points_alternate", "player_rebounds_alternate",
    "player_assists_alternate", "player_threes_alternate",
    "player_points_rebounds_assists_alternate",
})


# ═══════════════════════════════════════════════════════════════════════
# Normalization helpers
# ═══════════════════════════════════════════════════════════════════════
def _norm(s: Optional[str]) -> str:
    """Lowercase + strip diacritics + collapse whitespace."""
    if not s:
        return ""
    s = str(s)
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split()).strip()


def _canonical_market_key(s: Optional[str]) -> str:
    """Return the lowercase, whitespace-collapsed canonical form of a
    provider market key.  Producers may write ``player_goal_scorer_anytime``
    or ``Player Goal Scorer - Anytime``; we normalise both to
    ``player_goal_scorer_anytime`` for registry lookup.
    """
    if not s:
        return ""
    s = str(s).strip().lower()
    # unify separators — spaces, hyphens, dots → underscore
    for ch in (" ", "-", ".", "/"):
        s = s.replace(ch, "_")
    # collapse consecutive underscores
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def _is_player_market(pick: dict) -> bool:
    """Return True iff the pick names a player as the participant.

    Post-Phase-10 audit fix — reads BOTH the display-label ``market``
    string AND the canonical-key fields ``market_key`` /
    ``provider_market_key`` / ``market_id`` / ``bookmaker_market_key``.
    A pick that carries only a canonical provider key (e.g.
    ``player_goal_scorer_anytime``) is now correctly classified as a
    player market instead of leaking through as NOT_APPLICABLE.
    """
    # ── (a) Canonical-key match ─────────────────────────────────────
    for k in ("market_key", "provider_market_key",
              "market_id", "bookmaker_market_key"):
        raw = pick.get(k)
        if raw:
            key_norm = _canonical_market_key(raw)
            if key_norm in _CANONICAL_PLAYER_MARKET_KEYS:
                return True
    # The `market` string itself may be a canonical key on some
    # producers (soccer feeds routinely write market="player_goal_scorer_anytime").
    market_raw = pick.get("market")
    if market_raw:
        key_norm = _canonical_market_key(market_raw)
        if key_norm in _CANONICAL_PLAYER_MARKET_KEYS:
            return True
    # ── (b) Display-label substring match ───────────────────────────
    market = _norm(market_raw)
    if not market:
        return False
    # ── 2026-08-27 GAME-TOTAL / SPREAD SHORT-CIRCUIT ────────────────
    # Game-level totals and spreads are emitted with canonical
    # prefixes ("Total <Unit> Over/Under X.Y" and "<Team> ±X.Y
    # Spread").  Prior to this guard the substring scan below
    # matched "points"/"goals"/"runs" inside "Total Points" /
    # "Total Goals" / "Total Runs" and mis-classified team-side
    # game markets as player props (silent PLAYER_TEAM_UNRESOLVED
    # rejections on CFB/NFL/NBA/NHL/MLB totals).  Team markets
    # NEVER carry a player_name / player field, so short-circuit
    # here to preserve the existing player-prop detection path.
    if market.startswith("total ") or market.endswith(" spread"):
        return False
    for token in _PLAYER_MARKET_TOKENS:
        if token in market:
            return True
    return False


def _extract_event_participants(pick: dict) -> tuple[str, str]:
    """Return (home_team_norm, away_team_norm). Empty string when
    unresolvable (do not guess)."""
    home = pick.get("home_team") or ""
    away = pick.get("away_team") or ""
    if not home or not away:
        # Fall back to parsing "Away @ Home" (canonical event string).
        event = pick.get("event") or ""
        if isinstance(event, str) and " @ " in event:
            parts = event.split(" @ ", 1)
            if len(parts) == 2:
                if not away:
                    away = parts[0]
                if not home:
                    home = parts[1]
    return _norm(home), _norm(away)


def _extract_player_team(pick: dict) -> str:
    """Return the player's team normalized.  Empty string when the pick
    lacks any enriched team hint (fail-open — see module docstring).

    Post-Cert Defect 3B — recognises the Soccer scorer ingestion's
    ``canonical_team_name`` field as an authoritative player-team
    source (matches other authoritative fields like ``player_team`` /
    ``elite_player_team``).  Never invents a team — only reads existing
    enriched fields.
    """
    for k in ("player_team", "elite_player_team",
              "player_team_name", "canonical_team_name",
              "team", "team_abbrev"):
        v = pick.get(k)
        if isinstance(v, str) and v.strip():
            return _norm(v)
    return ""


def _teams_match(candidate: str, home: str, away: str) -> bool:
    """Return True iff candidate corresponds to home OR away.

    Uses two-way containment so short abbreviations (KC) match full names
    (Kansas City Chiefs) and vice versa.  Empty candidate returns False —
    the caller is expected to route that to PLAYER_TEAM_UNRESOLVED.
    """
    if not candidate:
        return False
    if candidate == home or candidate == away:
        return True
    # containment guard — but only if the shorter token is at least 3
    # chars to avoid degenerate matches like "st" hitting every team
    for peer in (home, away):
        if not peer:
            continue
        short, long = sorted([candidate, peer], key=len)
        if len(short) >= 3 and short in long:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════
def evaluate_identity(pick: dict) -> IdentityVerdict:
    """Return the canonical identity verdict for ``pick``.

    Never raises — caller may unconditionally consult the verdict.
    """
    if not isinstance(pick, dict):
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED

    sport = _norm(pick.get("sport"))
    market = _norm(pick.get("market"))

    # Tennis — the "player" IS the selection.  Verify the selection is
    # one of the two competitors named in the event.
    #
    # PERKLOCKS TENNIS REACHABILITY (2026-06):
    #   Tennis GAME-TOTAL / SPREAD / (Alt) markets carry
    #   ``selection ∈ {"Over", "Under"}`` (or a numeric spread) and
    #   are NOT player selections. They must be classified
    #   ``NOT_APPLICABLE`` — otherwise the player-name-match rule
    #   below incorrectly rejects every game-total pick as
    #   ``PLAYER_EVENT_IDENTITY_MISMATCH``. The Tennis moneyline
    #   path is preserved (selection = player name).
    if sport == "tennis":
        if not _is_tennis_player_selection(pick):
            return IdentityVerdict.NOT_APPLICABLE
        return _evaluate_tennis(pick)

    # Team-side markets have no player concept — NOT_APPLICABLE.
    if not _is_player_market(pick):
        return IdentityVerdict.NOT_APPLICABLE

    home_n, away_n = _extract_event_participants(pick)
    if not home_n or not away_n:
        # Cannot prove membership OR mismatch — fail-open.
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED

    player_team_n = _extract_player_team(pick)
    if not player_team_n:
        # PERKLOCKS MLB REACHABILITY (2026-06): try to derive the
        # player's team from the market label itself before failing.
        # MLB hitter markets are frequently written as
        # ``"Henry Bolte (OAK) Henry Bolte 2.5 Hits + Runs + RBIs"``
        # — the (TEAM_ABBREV) parenthetical carries the identity but
        # was not being consumed anywhere. Read it here as a last-
        # chance signal so legitimate hitter rows with real book
        # odds + positive edge reach publication.
        derived = _derive_player_team_from_market(pick)
        if derived:
            player_team_n = _norm(derived)
    if not player_team_n:
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED

    if _teams_match(player_team_n, home_n, away_n):
        return IdentityVerdict.VALID
    return IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


# ── PERKLOCKS TENNIS/MLB REACHABILITY (2026-06) ──────────────────────
# Helper predicates & derivers used by ``evaluate_identity`` above.
# Kept adjacent to the caller for readability; each function is small,
# side-effect-free, and never mutates the pick.

_TENNIS_NON_PLAYER_SELECTION_TOKENS = (
    "over", "under", "yes", "no", "draw",
)

def _is_tennis_player_selection(pick: dict) -> bool:
    """Return True iff the Tennis pick's ``selection`` is meant to be
    a player name (Moneyline / To Win / retirement).  Game-total,
    spread, BTTS, and (Alt) selections are ``NOT_APPLICABLE``.
    """
    sel = pick.get("selection")
    if not isinstance(sel, str):
        return False
    sel_n = _norm(sel)
    if not sel_n:
        return False
    # Explicit non-player selections short-circuit.
    if sel_n in _TENNIS_NON_PLAYER_SELECTION_TOKENS:
        return False
    # Anything with a leading over/under keyword is a total.
    for tok in _TENNIS_NON_PLAYER_SELECTION_TOKENS:
        if sel_n.startswith(tok + " ") or sel_n.endswith(" " + tok):
            return False
    # Numeric-only selections (spreads / totals lines) are also not players.
    stripped = sel_n.replace("+", "").replace("-", "").replace(".", "").replace(" ", "")
    if stripped.isdigit():
        return False
    # Market-label based inference — game totals / spreads are never
    # player-identity gated.
    market = _norm(pick.get("market"))
    if any(t in market for t in ("games (alt)", "total games", "games over",
                                   "games under", "spread", "handicap",
                                   " (alt)", "\bset spread\b")):
        return False
    return True


_MLB_TEAM_PAREN_RE = re.compile(r"\(([A-Z]{2,4})\)")

# ── MLB team-abbrev canonical map ────────────────────────────────────
# ONLY used when the derived player-team candidate is a 2-4 letter
# uppercase abbreviation (from an MLB market label parenthetical).
# Maps each abbrev to a distinctive city/team token that reliably
# appears in the ``event`` / ``home_team`` / ``away_team`` strings.
# Multiple tokens per abbrev handles franchise rebrands (e.g. Oakland
# Athletics → Athletics) and city vs mascot ambiguity.
_MLB_ABBREV_TO_TEAM_TOKENS = {
    "ARI": ["arizona", "diamondbacks"], "ATL": ["atlanta", "braves"],
    "BAL": ["baltimore", "orioles"], "BOS": ["boston", "red sox"],
    "CHC": ["cubs", "chicago cubs"], "CHW": ["white sox"],
    "CIN": ["cincinnati", "reds"], "CLE": ["cleveland", "guardians"],
    "COL": ["colorado", "rockies"], "DET": ["detroit", "tigers"],
    "HOU": ["houston", "astros"], "KC": ["kansas city", "royals"],
    "KCR": ["kansas city", "royals"], "LAA": ["angels", "los angeles angels"],
    "LAD": ["dodgers", "los angeles dodgers"], "MIA": ["miami", "marlins"],
    "MIL": ["milwaukee", "brewers"], "MIN": ["minnesota", "twins"],
    "NYM": ["mets", "new york mets"], "NYY": ["yankees", "new york yankees"],
    "OAK": ["athletics", "oakland"], "ATH": ["athletics", "oakland"],
    "PHI": ["philadelphia", "phillies"], "PIT": ["pittsburgh", "pirates"],
    "SD": ["san diego", "padres"], "SDP": ["san diego", "padres"],
    "SEA": ["seattle", "mariners"], "SF": ["san francisco", "giants"],
    "SFG": ["san francisco", "giants"], "STL": ["st. louis", "cardinals"],
    "TB": ["tampa bay", "rays"], "TBR": ["tampa bay", "rays"],
    "TEX": ["texas", "rangers"], "TOR": ["toronto", "blue jays"],
    "WSH": ["washington", "nationals"], "WAS": ["washington", "nationals"],
}


def _derive_player_team_from_market(pick: dict) -> Optional[str]:
    """Return an inferred player-team parsed from the ``market`` label
    (MLB hitter path).  Never guesses — only reads an explicit ``(ABC)``
    parenthetical the ingestion writer already produced.

    Returns the FIRST expanded team token that actually appears in the
    pick's event/home/away — this defends against franchise rebrands
    (Oakland Athletics → Athletics) and city vs mascot mismatches.
    Falls back to the raw abbrev when no expansion tokens are known.
    """
    market = pick.get("market")
    if not isinstance(market, str) or not market:
        return None
    m = _MLB_TEAM_PAREN_RE.search(market)
    if not m:
        return None
    abbrev = m.group(1)
    if not abbrev:
        return None
    sport = str(pick.get("sport") or "").strip().lower()
    if sport != "mlb":
        return abbrev
    tokens = _MLB_ABBREV_TO_TEAM_TOKENS.get(abbrev.upper())
    if not tokens:
        return abbrev
    # Prefer the token that actually appears in the event/home/away
    # strings so the downstream substring match resolves correctly.
    haystack_parts = [
        pick.get("event") or "",
        pick.get("home_team") or "",
        pick.get("away_team") or "",
    ]
    haystack = " | ".join(str(x) for x in haystack_parts).lower()
    for tok in tokens:
        if tok in haystack:
            return tok
    # None of the tokens appear — fall back to the first token so the
    # caller can still register a proven mismatch (rather than an
    # unresolved verdict).
    return tokens[0]


def _evaluate_tennis(pick: dict) -> IdentityVerdict:
    """Tennis-specific: the selection MUST be one of the two competitors
    in the event.  The event is stored as `Player A vs Player B` or
    `Player A @ Player B` depending on ingestion path.
    """
    event = pick.get("event") or ""
    selection = pick.get("selection") or ""
    if not isinstance(event, str) or not isinstance(selection, str) or \
            not event.strip() or not selection.strip():
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED

    ev_n = _norm(event)
    sel_n = _norm(selection)

    # Strip common non-name tokens from the selection ("Moneyline" etc.)
    for suffix in ("moneyline", "to win", " win", " (alt)"):
        if sel_n.endswith(suffix):
            sel_n = sel_n[: -len(suffix)].strip()

    # The selection player must appear as a substring of the event.
    if not sel_n:
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED
    # Split event by " vs " / " @ " and confirm at least one side
    # contains the selection.
    tokens: list[str] = []
    for sep in (" vs ", " @ ", " v ", " - "):
        if sep in ev_n:
            tokens = [t.strip() for t in ev_n.split(sep) if t.strip()]
            break
    if not tokens:
        # Unparseable event — fail-open.
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED
    # Sanity floor: selection must be 3+ chars to avoid trivial matches.
    if len(sel_n) < 3:
        return IdentityVerdict.PLAYER_TEAM_UNRESOLVED
    for side in tokens:
        # Two-way containment for last-name-only vs full-name.
        if sel_n == side:
            return IdentityVerdict.VALID
        if sel_n in side or side in sel_n:
            return IdentityVerdict.VALID
    return IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def is_identity_valid_for_publication(pick: dict) -> bool:
    """Convenience wrapper — returns True iff the pick may cross the
    canonical publication boundary WITHOUT tripping identity rejection.

    PHASE 10A (2026-07) — Fail-closed contract:
      * ``VALID``                              → clears publication.
      * ``NOT_APPLICABLE`` (team-side markets) → clears publication.
      * ``PLAYER_TEAM_UNRESOLVED``             → REJECTED (was previously
        fail-open; the user directive is: if Perklocks cannot prove that
        a player belongs to one of the exact event participants, it must
        NOT publish the player prop).
      * ``PLAYER_EVENT_IDENTITY_MISMATCH``     → REJECTED.

    Rationale: an UNRESOLVED verdict means we cannot prove membership.
    For production player props that is a rejection, not a soft pass.
    """
    v = evaluate_identity(pick)
    return v in (IdentityVerdict.VALID, IdentityVerdict.NOT_APPLICABLE)


__all__ = [
    "IdentityVerdict",
    "evaluate_identity",
    "is_identity_valid_for_publication",
]
