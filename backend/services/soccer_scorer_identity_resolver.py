"""SOCCER_UNIVERSAL_PLAYER_IDENTITY (2026-09) — shared identity resolver.

The single canonical identity resolver used by the shared Soccer
scorer ingester across every enabled Soccer league.  Wires together
existing production identity infrastructure:

  • ``db.player_identities`` (27k+ canonical Soccer players with
    aliases, current_team, historical_teams, provider_ids)
  • ``services.soccer_team_identity.canonical_team_key`` (team alias
    normalisation used across the runtime)
  • ``services.player_identity._norm`` (canonical name normalisation
    already used elsewhere)

Contract:

    resolve_soccer_scorer_identity(
        db, provider_player, provider_event_id,
        home_team, away_team, league,
    ) -> ResolvedIdentity

The resolver is EVENT-ANCHORED — a scorer must map to a player on
one of the two participating teams.  Global same-name matches
without team validation are refused (per §2 of the directive).

Missing history / form is NOT an identity failure.  This module
resolves identity only.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from services.player_identity import _norm, IDENTITY_COLLECTION
from services.soccer_team_identity import canonical_team_key, teams_equal


# ── Canonical identity status codes (per directive §5) ─────────────
STATUS_RESOLVED                 = "IDENTITY_RESOLVED"
STATUS_UNRESOLVED               = "PLAYER_IDENTITY_UNRESOLVED"
STATUS_AMBIGUOUS                = "PLAYER_IDENTITY_AMBIGUOUS"
STATUS_TEAM_MISMATCH            = "PLAYER_TEAM_MISMATCH"
STATUS_STALE_ROSTER             = "STALE_ROSTER"
STATUS_SOURCE_ID_UNMAPPED       = "PLAYER_SOURCE_ID_UNMAPPED"
STATUS_EVENT_IDENTITY_FAILURE   = "EVENT_IDENTITY_FAILURE"
STATUS_TEAM_IDENTITY_FAILURE    = "TEAM_IDENTITY_FAILURE"

ALL_IDENTITY_STATUSES = frozenset({
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    STATUS_AMBIGUOUS,
    STATUS_TEAM_MISMATCH,
    STATUS_STALE_ROSTER,
    STATUS_SOURCE_ID_UNMAPPED,
    STATUS_EVENT_IDENTITY_FAILURE,
    STATUS_TEAM_IDENTITY_FAILURE,
})


@dataclass
class ResolvedIdentity:
    status:               str
    provider_player:      str
    normalized_player:    str = ""
    canonical_player_id:  Optional[str] = None
    canonical_name:       Optional[str] = None
    canonical_team_id:    Optional[str] = None
    canonical_team_name:  Optional[str] = None
    canonical_event_id:   Optional[str] = None
    canonical_home:       Optional[str] = None
    canonical_away:       Optional[str] = None
    resolution_method:    str = ""
    aliases_used:         list[str] = field(default_factory=list)
    ambiguous_candidates: list[dict] = field(default_factory=list)


def _strip_ascii(name: str) -> str:
    """Diacritic-strip + normalise apostrophes/hyphens/spaces."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("'", "").replace("-", " ").replace(".", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _generate_name_variants(name: str) -> list[str]:
    """Produce a small set of variants (normalized name only, aliases
    on identity docs are handled separately) so lookup tolerates
    accents / initials / hyphens / apostrophes / suffixes."""
    if not name:
        return []
    variants: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        v = (v or "").strip()
        if not v:
            return
        n = _norm(v)
        if n and n not in seen:
            variants.append(n)
            seen.add(n)

    _add(name)
    _add(_strip_ascii(name))

    # Suffix strip (Jr, Sr, II, III).
    stripped = re.sub(r"\s+(jr\.?|sr\.?|ii+)\.?$", "", name, flags=re.I)
    if stripped and stripped != name:
        _add(stripped)

    # First-initial + last name (e.g. "L. Messi" ⇄ "Lionel Messi").
    parts = re.split(r"\s+", name.strip())
    if len(parts) >= 2:
        # Try "F. Last" variant.
        _add(f"{parts[0][0]}. {parts[-1]}")
        # Try "Last, First" reversal.
        _add(f"{parts[-1]} {parts[0]}")
        # Middle-name dropped.
        _add(f"{parts[0]} {parts[-1]}")

    return variants


async def _fetch_event_participant_candidates(
    db, home_norm: str, away_norm: str,
    *, home_raw: Optional[str] = None, away_raw: Optional[str] = None,
) -> tuple[dict[str, str], list[dict]]:
    """Query ``player_identities`` for every player whose current_team
    (or historical_teams) matches one of the event participants.

    Server-side prefilter: builds a regex on the most-distinctive
    token of each team name so we only page through the ~30-100
    candidate rows instead of the full 27k identity registry.
    """
    def _distinctive_regex(team: str) -> Optional[str]:
        if not team:
            return None
        # Strip generic soccer-club suffixes so the significant token
        # survives (e.g. "Inter Miami CF" → "Miami"; "FC Cincinnati"
        # → "Cincinnati"; "New York City FC" → "City").
        _generic = {"fc", "cf", "sc", "ac", "afc", "club", "united",
                    "city", "the", "de", "do", "da"}
        tokens = [
            _norm(t) for t in re.split(r"\s+", team.strip())
            if _norm(t) and _norm(t) not in _generic
        ]
        if not tokens:
            tokens = [_norm(team)]
        # Pick the longest distinctive token — least likely to
        # be shared with unrelated teams.
        tokens.sort(key=len, reverse=True)
        return re.escape(tokens[0])

    home_re = _distinctive_regex(home_norm)
    away_re = _distinctive_regex(away_norm)
    regex_or: list[dict] = []
    raw_pool = list({home_norm, away_norm, home_raw or "", away_raw or ""})
    raw_pool = [t for t in raw_pool if t]
    for r in (home_re, away_re):
        if r:
            regex_or.append({"current_team": {"$regex": r, "$options": "i"}})
            regex_or.append({"historical_teams.team":
                              {"$regex": r, "$options": "i"}})
    for team_raw in raw_pool:
        escaped = re.escape(team_raw)
        regex_or.append({"current_team": {"$regex": escaped, "$options": "i"}})
        regex_or.append({"historical_teams.team":
                          {"$regex": escaped, "$options": "i"}})

    query: dict[str, Any] = {"sport": "Soccer"}
    if regex_or:
        query["$or"] = regex_or

    projection = {
        "_id": 0, "canonical_player_id": 1, "name": 1, "name_norm": 1,
        "aliases": 1, "current_team": 1, "historical_teams": 1,
        "league": 1, "observed_at": 1, "provider_ids": 1,
        "position": 1, "roster_status": 1,
    }

    home_docs: list[dict] = []
    away_docs: list[dict] = []

    try:
        cursor = db.player_identities.find(query, projection)
    except Exception:
        # Test doubles / stub DBs without the collection.  Treat as
        # empty participant pool — resolver will emit UNRESOLVED and
        # the ingester falls through the pre-resolver path unchanged.
        return {}, []

    try:
        async for d in cursor:
            ct = d.get("current_team")
            if ct and teams_equal(ct, home_norm):
                home_docs.append(d); continue
            if ct and teams_equal(ct, away_norm):
                away_docs.append(d); continue
            for h in (d.get("historical_teams") or []):
                t = h.get("team") if isinstance(h, dict) else None
                if t and teams_equal(t, home_norm):
                    home_docs.append(d); break
                if t and teams_equal(t, away_norm):
                    away_docs.append(d); break
    except Exception:
        return {}, []

    team_of: dict[str, str] = {}
    for d in home_docs:
        team_of[d.get("canonical_player_id") or d.get("name_norm") or ""] = "home"
    for d in away_docs:
        team_of[d.get("canonical_player_id") or d.get("name_norm") or ""] = "away"
    return team_of, home_docs + away_docs


# ── Per-event cache — one participants-fetch per (event_id, home, away)
#    inside a single ingester run, keyed by process-lifetime.
_EVENT_PARTICIPANTS_CACHE: dict[str, tuple[dict[str, str], list[dict]]] = {}


async def _cached_event_participants(
    db, event_id: str, home_norm: str, away_norm: str,
    *, home_raw: Optional[str] = None, away_raw: Optional[str] = None,
) -> tuple[dict[str, str], list[dict]]:
    key = f"{event_id}|{home_norm}|{away_norm}"
    hit = _EVENT_PARTICIPANTS_CACHE.get(key)
    if hit is not None:
        return hit
    result = await _fetch_event_participant_candidates(
        db, home_norm, away_norm,
        home_raw=home_raw, away_raw=away_raw,
    )
    if len(_EVENT_PARTICIPANTS_CACHE) > 512:
        _EVENT_PARTICIPANTS_CACHE.clear()
    _EVENT_PARTICIPANTS_CACHE[key] = result
    return result


def _match_player_against_docs(
    provider_player: str, docs: list[dict],
) -> list[dict]:
    """Return every identity doc whose (name_norm | aliases) matches
    any variant of ``provider_player``."""
    if not provider_player or not docs:
        return []
    provider_variants = set(_generate_name_variants(provider_player))
    matches: list[dict] = []
    for d in docs:
        keys: set[str] = set()
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        if nn:
            keys.add(nn)
            keys.add(_strip_ascii(d.get("name") or ""))
        for al in (d.get("aliases") or []):
            keys.add(_norm(al))
            keys.add(_strip_ascii(al))
        # Test variant-vs-variant.
        if provider_variants & keys:
            matches.append(d)
    return matches


async def resolve_soccer_scorer_identity(
    db, *, provider_player: str, provider_event_id: str,
    home_team: str, away_team: str, league: Optional[str] = None,
) -> ResolvedIdentity:
    """Resolve canonical player identity for a Soccer scorer row.

    Event-anchored: the player must appear on one of the two
    participating teams' current or historical roster.  Global
    same-name matches without team validation are refused.

    Never raises — returns a ResolvedIdentity with a status code.
    """
    normalized_player = _norm(provider_player)

    # ── Team identity resolution ────────────────────────────────
    home_norm = canonical_team_key(home_team) or home_team
    away_norm = canonical_team_key(away_team) or away_team
    if not (home_norm and away_norm):
        return ResolvedIdentity(
            status=STATUS_TEAM_IDENTITY_FAILURE,
            provider_player=provider_player,
            normalized_player=normalized_player,
            canonical_event_id=provider_event_id,
        )

    if not provider_event_id:
        return ResolvedIdentity(
            status=STATUS_EVENT_IDENTITY_FAILURE,
            provider_player=provider_player,
            normalized_player=normalized_player,
        )

    # ── Fetch event participant candidates (cached per event) ────
    team_of, participant_docs = await _cached_event_participants(
        db, provider_event_id, home_norm, away_norm,
        home_raw=home_team, away_raw=away_team,
    )

    # ── Match provider_player against those participants ────────
    matches = _match_player_against_docs(provider_player, participant_docs)
    aliases_used: list[str] = []
    for d in matches:
        aliases_used.extend(d.get("aliases") or [])

    # Collapse duplicate DB entries for the same canonical player
    # (same name_norm + current_team pair).  Registry hygiene
    # sometimes yields multiple rows with DIFFERENT canonical_player_
    # ids for what is clearly one player — the resolver must not
    # treat that as ambiguity.  We prefer name_norm + current_team
    # as the collapse key; canonical_player_id is only a fallback.
    seen_keys: set[str] = set()
    deduped_matches: list[dict] = []
    for d in matches:
        nn = (d.get("name_norm") or _norm(d.get("name") or "")).lower()
        ct = (d.get("current_team") or "").strip().lower()
        key = f"{nn}|{ct}" if nn else (
            d.get("canonical_player_id") or id(d)
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_matches.append(d)
    matches = deduped_matches

    if len(matches) == 1:
        d = matches[0]
        cpid = d.get("canonical_player_id") or d.get("name_norm")
        team = team_of.get(cpid, "unknown")
        team_name = home_norm if team == "home" else (away_norm if team == "away" else None)
        return ResolvedIdentity(
            status=STATUS_RESOLVED,
            provider_player=provider_player,
            normalized_player=normalized_player,
            canonical_player_id=cpid,
            canonical_name=d.get("name"),
            canonical_team_id=team_name,
            canonical_team_name=team_name,
            canonical_event_id=provider_event_id,
            canonical_home=home_norm,
            canonical_away=away_norm,
            resolution_method="event_anchored_identity_registry",
            aliases_used=aliases_used,
        )

    if len(matches) > 1:
        # Pick the one whose CURRENT team matches an event participant
        # (i.e. active roster) — historical-only matches are broken
        # ties by prefer-current-team.
        active = [
            d for d in matches
            if teams_equal(d.get("current_team") or "", home_norm)
            or teams_equal(d.get("current_team") or "", away_norm)
        ]
        if len(active) == 1:
            d = active[0]
            cpid = d.get("canonical_player_id") or d.get("name_norm")
            team = team_of.get(cpid, "unknown")
            team_name = home_norm if team == "home" else (away_norm if team == "away" else None)
            return ResolvedIdentity(
                status=STATUS_RESOLVED,
                provider_player=provider_player,
                normalized_player=normalized_player,
                canonical_player_id=cpid,
                canonical_name=d.get("name"),
                canonical_team_id=team_name,
                canonical_team_name=team_name,
                canonical_event_id=provider_event_id,
                canonical_home=home_norm,
                canonical_away=away_norm,
                resolution_method="event_anchored_active_roster_tie_break",
                aliases_used=aliases_used,
            )
        return ResolvedIdentity(
            status=STATUS_AMBIGUOUS,
            provider_player=provider_player,
            normalized_player=normalized_player,
            canonical_event_id=provider_event_id,
            canonical_home=home_norm,
            canonical_away=away_norm,
            resolution_method="event_anchored_identity_registry",
            aliases_used=aliases_used,
            ambiguous_candidates=[
                {"canonical_player_id": d.get("canonical_player_id"),
                 "name": d.get("name"),
                 "current_team": d.get("current_team"),
                 "league": d.get("league")}
                for d in matches
            ],
        )

    # ── No event-anchored match — mark unresolved ───────────────
    # We deliberately do NOT fall back to a global same-name pick per
    # directive §2.  A same-name global player on a different team is
    # PLAYER_IDENTITY_UNRESOLVED, not PLAYER_TEAM_MISMATCH, because we
    # cannot prove the identity is the "same person".
    return ResolvedIdentity(
        status=STATUS_UNRESOLVED,
        provider_player=provider_player,
        normalized_player=normalized_player,
        canonical_event_id=provider_event_id,
        canonical_home=home_norm,
        canonical_away=away_norm,
        resolution_method="event_anchored_identity_registry",
    )


__all__ = [
    "ResolvedIdentity",
    "resolve_soccer_scorer_identity",
    "ALL_IDENTITY_STATUSES",
    "STATUS_RESOLVED",
    "STATUS_UNRESOLVED",
    "STATUS_AMBIGUOUS",
    "STATUS_TEAM_MISMATCH",
    "STATUS_STALE_ROSTER",
    "STATUS_SOURCE_ID_UNMAPPED",
    "STATUS_EVENT_IDENTITY_FAILURE",
    "STATUS_TEAM_IDENTITY_FAILURE",
]
