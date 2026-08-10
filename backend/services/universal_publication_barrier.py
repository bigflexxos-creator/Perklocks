"""Phase 5 (2026-08-11) — cross-sport publication barrier.

Extends the Soccer-specific ``player_team_fixture_validator`` concept
to a sport-generic contract Magic Layer 2.0 can invoke uniformly.

Design constraint: MUST NOT regress the existing Soccer P0-A..P0-E
behaviour.  For Soccer this module DELEGATES to
:func:`services.player_team_fixture_validator.validate_player_fixture_pick`.

For other TEAM_SPORTS (NFL / NBA / MLB / NHL / CFB) — the sport
adapter tells us whether the market is player-based, and we validate
the player's fresh authoritative current-team against the fixture
parties.

For INDIVIDUAL_SPORTS (Tennis / UFC) — we validate the participant
name against the event participants; there's no team to check.

Status semantics (P5 contract):

    "verified"           — evidence positively confirms the pick
    "unresolved"         — no fresh evidence to decide either way
    "source_conflict"    — two trusted sources disagree
    "confirmed_mismatch" — trusted evidence positively refutes the pick

Only ``confirmed_mismatch`` should hard-reject at the publication
gate.  ``unresolved`` and ``source_conflict`` degrade to quarantine,
never to a fake "team_mismatch" reject.
"""
from __future__ import annotations

from typing import Any, Optional

import re

from services.player_team_fixture_validator import (
    validate_player_fixture_pick as _soccer_validate,
    _extract_player_name, _extract_fixture_teams,
    _teams_match, _norm, _lookup_with_alias,
    REASON_PLAYER_TEAM_MISMATCH, REASON_ROSTER_UNVERIFIED,
    REASON_FIXTURE_TEAMS_UNKNOWN, REASON_PLAYER_NAME_MISSING,
    REASON_MARKET_NOT_PLAYER, REASON_ROSTER_CONFLICT,
)
from services.sport_adapters import get_adapter


# ── Sport-agnostic player-name extraction (extends Soccer's) ────
#
# Soccer's ``_extract_player_name`` handles the "<Name> - <Market>"
# pattern and Soccer market suffixes.  For NFL / NBA / MLB / NHL /
# CFB the market string commonly reads "<Name> <StatMarket> Over N" —
# we strip the trailing stat + line to recover the name.
_STAT_TRAILING_RE = re.compile(
    r"\s+(?:"
    r"passing\s+yards?|pass\s+yards?"
    r"|passing\s+tds?|pass\s+tds?"
    r"|passing\s+completions?|passing\s+attempts?"
    r"|rushing\s+yards?|rush\s+yards?"
    r"|rushing\s+tds?|rush\s+tds?"
    r"|carries"
    r"|receiving\s+yards?|rec(?:eiving)?\s+tds?|rec\s+yards?"
    r"|receptions|targets"
    r"|anytime\s+touchdown|anytime\s+td"
    r"|first\s+touchdown|last\s+touchdown"
    r"|points\s*\+\s*rebounds|points\s*\+\s*assists|pts\s*\+\s*reb"
    r"|pts\s*\+\s*ast|pra"
    r"|three\s*pointers?\s+made|3pt\s+made|made\s+threes"
    r"|double\s+double|triple\s+double"
    r"|points|rebounds|assists|steals|blocks|turnovers"
    r"|hits|home\s+run|hr|total\s+bases|rbi|runs\s+scored"
    r"|singles|doubles|triples|stolen\s+bases"
    r"|strikeouts|walks|pitches\s+thrown"
    r"|innings\s+pitched|earned\s+runs"
    r"|goals\s+scored|goal\s+scorer|goalscorer"
    r"|shots\s+on\s+goal|sog"
    r"|first\s+goal|last\s+goal"
    r"|saves|power\s+play\s+points"
    r"|kicking\s+points|field\s+goals\s+made"
    r"|sacks|interceptions\s+thrown"
    r"|longest\s+reception|longest\s+completion|longest\s+rush"
    r")\s*(?:over|under)?\s*[\d.]*.*$",
    re.IGNORECASE,
)


def _extract_player_name_universal(pick: dict) -> Optional[str]:
    """Sport-agnostic player-name extractor.

    Order:
      1. structured fields (player_name / player / selection / pick_side)
      2. Soccer / Odds API canonical "<Name> - <Market>"
      3. strip a trailing team-sport stat market ("Passing Yards Over 250.5")
      4. Soccer market suffixes ("To Score", "Anytime Goal Scorer")
    """
    n = _extract_player_name(pick)
    if n and " Over " not in n and " Under " not in n \
            and not _STAT_TRAILING_RE.search(n):
        # Soccer extractor already produced a clean name.
        return n
    market = pick.get("market") or ""
    if isinstance(market, str) and market.strip():
        stripped = _STAT_TRAILING_RE.sub("", market.strip()).strip()
        if stripped and stripped != market.strip():
            return stripped
    return n

# ── Status enum ──────────────────────────────────────────────────
STATUS_VERIFIED           = "verified"
STATUS_UNRESOLVED         = "unresolved"
STATUS_SOURCE_CONFLICT    = "source_conflict"
STATUS_CONFIRMED_MISMATCH = "confirmed_mismatch"

# Sports the barrier can meaningfully validate.  Every other sport
# returns a non-blocking pass-through (never a false mismatch).
_TEAM_SPORTS = frozenset(("NFL", "NBA", "MLB", "NHL", "CFB", "Soccer"))
_INDIVIDUAL_SPORTS = frozenset(("Tennis", "UFC"))


def _map_reason_to_status(reason: Optional[str]) -> str:
    """Translate the underlying Soccer validator's reason enum into
    the Phase 5 status enum."""
    if reason is None:
        return STATUS_VERIFIED
    if reason == REASON_PLAYER_TEAM_MISMATCH:
        return STATUS_CONFIRMED_MISMATCH
    if reason == REASON_ROSTER_CONFLICT:
        return STATUS_SOURCE_CONFLICT
    # Everything else (roster_unverified, fixture_teams_unknown,
    # player_name_missing, market_not_player_based) — no confirmed
    # contradiction — falls under unresolved.
    return STATUS_UNRESOLVED


def _envelope(*, verified: bool, reason: Optional[str],
               status: str, player: Optional[str],
               player_team: Optional[str],
               fixture_teams: Optional[tuple[str, str]],
               sport_class: str,
               evidence: Optional[str] = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "verified": verified,
        "reason": reason,
        "status": status,
        "player": player,
        "player_team": player_team,
        "fixture_teams": fixture_teams,
        "sport_class": sport_class,
    }
    if evidence:
        r["evidence"] = evidence
    return r


def _validate_team_sport(
    pick: dict[str, Any],
    *,
    roster_lookup: Optional[dict[str, str]],
    fresh_roster_names: Optional[set[str]],
) -> dict[str, Any]:
    """Adapter-agnostic team-sport validator used for
    NFL / NBA / MLB / NHL / CFB (Soccer has its own path).

    Missing roster data → ``unresolved`` (NEVER ``confirmed_mismatch``).
    """
    sport = pick.get("sport") or ""
    adapter = get_adapter(sport)
    market = pick.get("market") or ""
    if adapter and not adapter.is_player_market(market):
        return _envelope(
            verified=True, reason=REASON_MARKET_NOT_PLAYER,
            status=STATUS_VERIFIED, player=None,
            player_team=None, fixture_teams=None,
            sport_class="team")

    player_raw = _extract_player_name_universal(pick)
    if not player_raw:
        return _envelope(
            verified=False, reason=REASON_PLAYER_NAME_MISSING,
            status=STATUS_UNRESOLVED, player=None,
            player_team=None, fixture_teams=None,
            sport_class="team")

    fixture = _extract_fixture_teams(pick)
    if fixture is None:
        return _envelope(
            verified=False, reason=REASON_FIXTURE_TEAMS_UNKNOWN,
            status=STATUS_UNRESOLVED, player=player_raw,
            player_team=None, fixture_teams=None,
            sport_class="team")

    player_norm = _norm(player_raw)
    team = _lookup_with_alias(player_norm, roster_lookup or {})
    if team is None:
        return _envelope(
            verified=False, reason=REASON_ROSTER_UNVERIFIED,
            status=STATUS_UNRESOLVED, player=player_raw,
            player_team=None, fixture_teams=fixture,
            sport_class="team")

    if fresh_roster_names is not None:
        if player_norm not in fresh_roster_names:
            parts = player_norm.split()
            last = parts[-1] if parts else ""
            if not any(n.endswith(last) for n in fresh_roster_names if last):
                return _envelope(
                    verified=False, reason=REASON_ROSTER_UNVERIFIED,
                    status=STATUS_UNRESOLVED, player=player_raw,
                    player_team=team, fixture_teams=fixture,
                    sport_class="team")

    if _teams_match(team, fixture):
        return _envelope(
            verified=True, reason=None,
            status=STATUS_VERIFIED, player=player_raw,
            player_team=team, fixture_teams=fixture,
            sport_class="team",
            evidence=(adapter.ROSTER_SOURCE if adapter else "roster_lookup"))

    return _envelope(
        verified=False, reason=REASON_PLAYER_TEAM_MISMATCH,
        status=STATUS_CONFIRMED_MISMATCH, player=player_raw,
        player_team=team, fixture_teams=fixture,
        sport_class="team",
        evidence=(adapter.ROSTER_SOURCE if adapter else "roster_lookup"))


def _validate_individual_sport(
    pick: dict[str, Any],
) -> dict[str, Any]:
    """Tennis / UFC — participant-name check against fixture parties.

    Individual sports have no team; a positive mismatch (participant
    is not in the two-side event) is a ``confirmed_mismatch``.  Missing
    fixture data is ``unresolved``.
    """
    sport = pick.get("sport") or ""
    adapter = get_adapter(sport)
    market = pick.get("market") or ""
    if adapter and not adapter.is_player_market(market):
        return _envelope(
            verified=True, reason=REASON_MARKET_NOT_PLAYER,
            status=STATUS_VERIFIED, player=None,
            player_team=None, fixture_teams=None,
            sport_class="individual")

    player_raw = _extract_player_name(pick)
    # For individual sports, the market often IS the participant name
    # (e.g. "Alcaraz Moneyline") — strip a trailing action-suffix.
    if not player_raw and market:
        # Fallback — strip common individual-sport suffixes.
        m = market.strip()
        for suf in (" Moneyline", " Money Line", " To Win",
                     " Method of Victory", " Method Of Victory",
                     " Set Betting"):
            if m.endswith(suf):
                player_raw = m[: -len(suf)].strip()
                break
    if not player_raw:
        return _envelope(
            verified=False, reason=REASON_PLAYER_NAME_MISSING,
            status=STATUS_UNRESOLVED, player=None,
            player_team=None, fixture_teams=None,
            sport_class="individual")

    fixture = _extract_fixture_teams(pick)
    if fixture is None:
        return _envelope(
            verified=False, reason=REASON_FIXTURE_TEAMS_UNKNOWN,
            status=STATUS_UNRESOLVED, player=player_raw,
            player_team=None, fixture_teams=None,
            sport_class="individual")

    pn = _norm(player_raw)
    for side in fixture:
        sn = _norm(side)
        if not sn:
            continue
        if pn == sn:
            return _envelope(
                verified=True, reason=None,
                status=STATUS_VERIFIED, player=player_raw,
                player_team=side, fixture_teams=fixture,
                sport_class="individual",
                evidence="fixture_participant")
        # Length-guarded containment: participant list often carries
        # last name only ("Alcaraz vs Sinner") while the pick may
        # carry "Carlos Alcaraz Moneyline".  Require the shorter side
        # to be ≥ 4 chars to avoid "de" / "van" collisions.
        if len(pn) >= 4 and len(sn) >= 4:
            if pn in sn or sn in pn:
                return _envelope(
                    verified=True, reason=None,
                    status=STATUS_VERIFIED, player=player_raw,
                    player_team=side, fixture_teams=fixture,
                    sport_class="individual",
                    evidence="fixture_participant_contains")
        # Last-name match — but ONLY when the fixture side is a single
        # token (last name only).  Refuse to match on last name alone
        # when the fixture side is a full "First Last" — that's the
        # Tennis surname-only bug we must avoid.
        pn_parts = pn.split()
        sn_parts = sn.split()
        if len(sn_parts) == 1 and pn_parts and pn_parts[-1] == sn_parts[0]:
            return _envelope(
                verified=True, reason=None,
                status=STATUS_VERIFIED, player=player_raw,
                player_team=side, fixture_teams=fixture,
                sport_class="individual",
                evidence="fixture_participant_surname_bare")
    return _envelope(
        verified=False, reason=REASON_PLAYER_TEAM_MISMATCH,
        status=STATUS_CONFIRMED_MISMATCH, player=player_raw,
        player_team=None, fixture_teams=fixture,
        sport_class="individual",
        evidence="fixture_participant_absent")


def validate_universal(
    pick: dict[str, Any], *,
    roster_lookup: Optional[dict[str, str]] = None,
    fresh_roster_names: Optional[set[str]] = None,
    national_team_lookup: Optional[dict[str, str]] = None,
    fresh_national_team_names: Optional[set[str]] = None,
    nationality_lookup: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Cross-sport verdict.  Non-team markets bypass entirely.

    Return envelope:

        {
          "verified":     bool,
          "reason":       Optional[str],          # legacy reason enum
          "status":       str,                    # P5 enum
          "player":       Optional[str],
          "player_team":  Optional[str],
          "fixture_teams": Optional[tuple[str, str]],
          "sport_class":  "team" | "individual" | "unknown",
          "evidence":     Optional[str],
        }
    """
    sport = pick.get("sport") or ""

    # ── Soccer — DELEGATE to the P0-A..P0-E stack.  This is a
    # non-negotiable guarantee: Soccer verdicts must be identical to
    # the direct validator.
    if sport == "Soccer":
        r = _soccer_validate(
            pick, roster_lookup or {},
            fresh_roster_names=fresh_roster_names,
            national_team_lookup=national_team_lookup,
            fresh_national_team_names=fresh_national_team_names,
            nationality_lookup=nationality_lookup,
        )
        r["sport_class"] = "team"
        # Phase 5.2 (2026-08-11) — verified=True MUST map to
        # ``verified`` regardless of the reason enum.  The old mapper
        # was ambiguous for ``market_not_player_based`` where
        # ``verified=True`` yet ``reason != None``.
        if r.get("verified") is True:
            r["status"] = STATUS_VERIFIED
        else:
            r["status"] = _map_reason_to_status(r.get("reason"))
        return r

    if sport in _TEAM_SPORTS:
        return _validate_team_sport(
            pick,
            roster_lookup=roster_lookup,
            fresh_roster_names=fresh_roster_names,
        )

    if sport in _INDIVIDUAL_SPORTS:
        return _validate_individual_sport(pick)

    # Unknown sport — non-blocking pass-through.
    return _envelope(
        verified=True, reason=REASON_MARKET_NOT_PLAYER,
        status=STATUS_VERIFIED, player=None,
        player_team=None, fixture_teams=None,
        sport_class="unknown")


__all__ = [
    "validate_universal",
    "STATUS_VERIFIED", "STATUS_UNRESOLVED",
    "STATUS_SOURCE_CONFLICT", "STATUS_CONFIRMED_MISMATCH",
]
