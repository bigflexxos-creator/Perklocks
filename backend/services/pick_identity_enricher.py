"""Canonical Pick Identity Enricher (Pre-Magic Blocker A).

Parses `event` / `selection` / `market` / `sport` on a pick dict at
canonical publication time and returns a **deterministic** enrichment
of structured canonical identity fields:

    canonical_event_id, canonical_team_id, canonical_opponent_id,
    canonical_player_id, home_team_name, away_team_name,
    team, opponent_team, player_name, identity_quality,
    identity_resolution

**Hard rules (per remediation §2 – §7):**

* Uses the EXISTING canonical identity system
  (:mod:`services.identity_resolver`).  Never invents a competing ID.
* Every resolver call is a pure function — no DB access, no
  guesswork, no fuzzy fallback beyond ``resolve_*`` fallback rules.
* If identity cannot be resolved confidently the enricher records
  ``canonical_player_id = None`` (or team) with an explicit reason.
  UNKNOWN stays UNKNOWN — no fabricated IDs.
* The enricher NEVER modifies :

  * ``market``  (kept as human display),
  * ``selection`` (kept as human display),
  * ``win_probability`` / ``lock_score`` / any scoring field,
  * publication eligibility / off_board flags.

* Idempotent.  Re-running on an already-enriched pick reproduces the
  same enrichment.

The enricher is intentionally **produce-source agnostic** — every
producer that flows through ``publish_upserted_picks`` gets identity
enrichment for free, so we never repeat the previous defect of an
untraced producer bypassing the fix.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from services.identity_resolver import (
    normalize_name,
    resolve_team,
    resolve_player,
    resolve_event,
)

logger = logging.getLogger("lockscore.pick_identity_enricher")


# ═══════════════════════════════════════════════════════════════════
# Sport classification
# ═══════════════════════════════════════════════════════════════════
_INDIVIDUAL_SPORTS = {"tennis", "ufc", "mma", "boxing", "golf"}
_TEAM_SPORTS = {"mlb", "nba", "nfl", "nhl", "soccer", "cfb", "cbb", "wnba"}

_TOTAL_TOKENS = ("Over", "Under", "over", "under")

_TEAM_MARKET_TOKENS = (
    "moneyline", "spread", "spread_line", "runline", "puckline",
    "handicap", "double chance", "btts", "team total",
    "asian handicap", "draw no bet",
)

_TOTAL_MARKET_RE = re.compile(
    r"\b(total\s+(goals|runs|points|rounds|games|corners|cards)|"
    r"over[/\s]under|o/u|team\s+total)\b",
    re.IGNORECASE,
)

# Player-prop patterns like "Player Name Over 24.5 Points" or
# "Player Name Anytime TD".  Player name appears BEFORE
# "Over"/"Under"/"Anytime".
_PLAYER_PROP_RE = re.compile(
    r"^(?!\s*Total\b)(?P<player>.+?)\s+"
    r"(over|under|anytime|to\s+score|to\s+record|"
    r"first\s+td|last\s+td)"
    r"(?:\s+\d+(?:\.\d+)?)?"
    r"(?:\s+.*)?$",
    re.IGNORECASE,
)


def _sport_l(sport: Optional[str]) -> str:
    return (sport or "").strip().lower()


def _is_individual_sport(sport: Optional[str]) -> bool:
    return _sport_l(sport) in _INDIVIDUAL_SPORTS


def _is_team_sport(sport: Optional[str]) -> bool:
    return _sport_l(sport) in _TEAM_SPORTS


# ═══════════════════════════════════════════════════════════════════
# Event parsing — "AWAY @ HOME" is the canonical convention across
# every current producer.  Some soccer producers use " vs " or " v ".
# ═══════════════════════════════════════════════════════════════════
_EVENT_SEPARATORS = (" @ ", " vs. ", " vs ", " v. ", " v ", " - ")


def parse_event_participants(event: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(away, home)`` tuple.

    * ``AWAY @ HOME`` is the canonical convention — used by every
      current producer (MLB / NBA / Soccer / Tennis / UFC / NFL).
    * Other separators degrade to (participant_a, participant_b) with
      no home/away guarantee.  We still populate both fields but the
      caller must treat ``home_team_name`` as best-effort.

    Returns ``(None, None)`` if the string cannot be split
    unambiguously.
    """
    if not event or not isinstance(event, str):
        return (None, None)
    for sep in _EVENT_SEPARATORS:
        if sep in event:
            left, _, right = event.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return (left, right)
    return (None, None)


# ═══════════════════════════════════════════════════════════════════
# Market classification
# ═══════════════════════════════════════════════════════════════════
def classify_market(*, market: Optional[str], selection: Optional[str],
                     bet_type: Optional[str] = None) -> str:
    """Classify a pick market into one of:

      ``TEAM``      — team moneyline / spread / totals-team
      ``TOTAL``     — game total (Over/Under)
      ``PLAYER``    — player prop
      ``INDIVIDUAL`` — individual-sport participant (Tennis / UFC)
      ``UNKNOWN``

    Deterministic — never uses fuzzy matching or a name registry.
    """
    m = (market or "").strip()
    s = (selection or "").strip()
    if not m and not s:
        return "UNKNOWN"

    # Totals — selection is "Over"/"Under" or market says total.
    if s in _TOTAL_TOKENS or _TOTAL_MARKET_RE.search(m):
        return "TOTAL"

    m_l = m.lower()

    # Team markets.
    for tok in _TEAM_MARKET_TOKENS:
        if tok in m_l:
            # But — for individual sports (Tennis/UFC) a "moneyline"
            # is actually the PLAYER moneyline.  Caller must
            # decide with sport in mind.
            return "TEAM"

    # Player prop patterns.
    if _PLAYER_PROP_RE.match(m):
        return "PLAYER"

    return "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Name extraction from player-prop market strings
# ═══════════════════════════════════════════════════════════════════
def extract_player_name_from_market(market: Optional[str]) -> Optional[str]:
    """Return the player name at the start of a player-prop market
    string, or None if the pattern doesn't match."""
    if not market:
        return None
    m = _PLAYER_PROP_RE.match(market)
    if not m:
        return None
    return m.group("player").strip() or None


# ═══════════════════════════════════════════════════════════════════
# Core enricher
# ═══════════════════════════════════════════════════════════════════
def enrich_pick_identity(pick: dict) -> dict:
    """Return a dict of enrichment fields to ``$set`` on the pick.

    This function is pure — it does NOT mutate ``pick``.  The caller
    is expected to merge the returned dict into the pick document via
    a targeted update.

    Enrichment fields returned (only those we can resolve — missing
    keys stay absent so the update is minimal and idempotent):

        canonical_event_id,     event_identity_quality,
        canonical_team_id,      canonical_opponent_id,
        canonical_player_id,
        home_team_name,         away_team_name,
        team, opponent_team,    player_name,
        identity_quality,       identity_resolution,
        pick_identity_version,  identity_enriched_at,

    ``identity_quality`` is one of ``provider`` / ``fallback`` /
    ``unresolved`` — matches the vocabulary of
    :mod:`services.identity_resolver`.

    ``identity_resolution`` is a machine-readable JSON blob describing
    exactly which fields were used and why.  Used by tests / audit.
    """
    out: dict[str, Any] = {}
    resolution: dict[str, Any] = {
        "market_class": None,
        "event_parsed": None,
        "sources": [],
        "reasons": [],
    }
    sport = pick.get("sport")
    sport_l = _sport_l(sport)
    event = pick.get("event")
    selection = pick.get("selection")
    market = pick.get("market")
    bet_type = pick.get("bet_type")

    # ── 1. Parse event participants ─────────────────────────────
    away, home = parse_event_participants(event)
    if away and home:
        resolution["event_parsed"] = {"away": away, "home": home}
        out["home_team_name"] = home
        out["away_team_name"] = away
        resolution["sources"].append("event")
    else:
        resolution["reasons"].append("event_unparsable")

    # ── 2. Classify market ──────────────────────────────────────
    mcls = classify_market(market=market, selection=selection,
                              bet_type=bet_type)
    # Individual sports: "TEAM" market is actually the PLAYER
    # participant.  Reclassify.
    if _is_individual_sport(sport_l) and mcls == "TEAM":
        mcls = "INDIVIDUAL"
    resolution["market_class"] = mcls

    # ── 3. Resolve team / opponent identity ─────────────────────
    home_team_id = None
    away_team_id = None
    if home:
        home_id = resolve_team(display_name=home, sport=sport_l)
        home_team_id = home_id.canonical_team_id
    if away:
        away_id = resolve_team(display_name=away, sport=sport_l)
        away_team_id = away_id.canonical_team_id

    # ── 4. Resolve event identity via existing identity_resolver ─
    # (deterministic fallback ID from sport + commence + both team_ids)
    commence = (pick.get("event_time") or pick.get("commence_time") or
                 pick.get("kickoff") or pick.get("start_time"))
    if commence and home_team_id and away_team_id and sport_l:
        ev_id = resolve_event(
            sport_key=sport_l,
            commence_time=commence,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
        )
        out["canonical_event_id"] = ev_id.canonical_event_id
        out["event_identity_quality"] = ev_id.identity_quality
        resolution["sources"].append("resolve_event(sport,time,teams)")
    else:
        resolution["reasons"].append("event_id_missing_inputs")

    # ── 5. Resolve pick-specific identity based on market class ─
    identity_quality = "unresolved"

    if mcls == "TEAM":
        # `selection` is the team we picked.
        pick_team = (selection or "").strip()
        opponent  = None
        if pick_team and away and pick_team.lower() == away.lower():
            opponent = home
        elif pick_team and home and pick_team.lower() == home.lower():
            opponent = away
        if pick_team:
            t_id = resolve_team(display_name=pick_team, sport=sport_l)
            out["canonical_team_id"] = t_id.canonical_team_id
            out["team"] = pick_team
            identity_quality = t_id.identity_quality
            resolution["sources"].append("selection→team")
        if opponent:
            opp_id = resolve_team(display_name=opponent, sport=sport_l)
            out["canonical_opponent_id"] = opp_id.canonical_team_id
            out["opponent_team"] = opponent
            resolution["sources"].append("event↔selection→opponent")
        else:
            resolution["reasons"].append("opponent_unresolved")

    elif mcls == "TOTAL":
        # Total pick — no player, but BOTH teams are known.
        # We attach team+opponent as "either side" using the event.
        # `canonical_team_id` intentionally NOT set — a total belongs
        # to the event, not a team.
        resolution["sources"].append("total_market_no_participant")
        identity_quality = "provider" if home_team_id and away_team_id \
            else "unresolved"

    elif mcls == "PLAYER":
        # Player prop — extract player name from market string.
        pn = extract_player_name_from_market(market) or pick.get("player_name")
        if pn:
            # For MLB / NBA / NFL etc., the player belongs to ONE of
            # the two teams — but we usually don't know which without
            # extra context.  Try to find team context.
            player_team = pick.get("team") or pick.get("player_current_team")
            player_team_id = None
            if player_team:
                pt_id = resolve_team(display_name=player_team,
                                       sport=sport_l)
                player_team_id = pt_id.canonical_team_id
                out["team"] = player_team
                out["canonical_team_id"] = player_team_id
            # Resolve player.  If we lack team context, resolver
            # returns ``unresolved`` — that's correct per §4.
            p_id = resolve_player(
                display_name=pn,
                sport=sport_l,
                team_id=player_team_id,
            )
            if p_id.identity_quality == "unresolved":
                # Store name but explicitly mark canonical id absent.
                out["player_name"] = pn
                # canonical_player_id INTENTIONALLY not set — §4.
                resolution["reasons"].append(
                    "player_unresolved_missing_team_context")
                identity_quality = "unresolved"
            else:
                out["canonical_player_id"] = p_id.canonical_player_id
                out["player_name"] = pn
                identity_quality = p_id.identity_quality
                resolution["sources"].append("market→player")
        else:
            resolution["reasons"].append("player_name_unparsable")

    elif mcls == "INDIVIDUAL":
        # Tennis / UFC — `selection` is a player, event lists BOTH
        # players.  No team context needed.
        pn = (selection or "").strip()
        opponent_name = None
        if pn and away and pn.lower() == away.lower():
            opponent_name = home
        elif pn and home and pn.lower() == home.lower():
            opponent_name = away
        if pn:
            # For individual sports we DO have team context — team
            # is effectively the player themselves.  But that's not
            # how the history collections are keyed, so we treat
            # player_id as authoritative and skip team_id.
            # Use a synthetic team_id keyed to the sport+player so
            # ``resolve_player`` doesn't reject.  Same rule the
            # history layer already uses for Tennis players.
            synth_team = f"{sport_l}:individual"
            p_id = resolve_player(
                display_name=pn,
                sport=sport_l,
                team_id=synth_team,
            )
            out["canonical_player_id"] = p_id.canonical_player_id
            out["player_name"] = pn
            identity_quality = p_id.identity_quality
            resolution["sources"].append("selection→player(individual)")
        if opponent_name:
            opp_synth_team = f"{sport_l}:individual"
            opp_id = resolve_player(
                display_name=opponent_name,
                sport=sport_l,
                team_id=opp_synth_team,
            )
            # canonical_opponent_id is a PLAYER for individual sports.
            out["canonical_opponent_id"] = opp_id.canonical_player_id
            out["opponent_team"] = opponent_name  # human-readable
            resolution["sources"].append("event↔selection→opponent(individual)")
        else:
            resolution["reasons"].append("individual_opponent_unresolved")

    else:
        resolution["reasons"].append("market_class_unknown")

    # ── 6. Final metadata ───────────────────────────────────────
    out["identity_quality"] = identity_quality
    out["identity_resolution"] = resolution
    out["pick_identity_version"] = 1
    out["identity_enriched_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ═══════════════════════════════════════════════════════════════════
# Batch application over a picks list (idempotent).
# ═══════════════════════════════════════════════════════════════════
def apply_enrichment(pick: dict) -> dict:
    """Return a NEW dict with identity fields merged onto ``pick``.

    Existing canonical_* fields on the pick are PRESERVED — a producer
    that already resolved identity is authoritative (§3).  We only
    fill in the fields that were missing/None.
    """
    enriched = enrich_pick_identity(pick)
    out = dict(pick)
    for k, v in enriched.items():
        if v is None:
            continue
        if out.get(k) in (None, "", []):
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════════
# Async enricher — DB-aware, uses authoritative identity mappings
# from ``player_game_actuals`` / ``team_game_actuals``.
#
# §3: "Reuse the canonical player/team identity system already used
# by player_game_actuals, team_game_actuals, Player History, Team
# History."  Because those collections were backfilled with provider
# IDs (numeric) and human-readable team names, the enricher performs
# an authoritative lookup against those collections whenever the
# names on the pick match a known entity.  A miss falls back to the
# pure-function ``enrich_pick_identity`` result (deterministic
# fallback hash IDs marked ``identity_quality="fallback"``).
# ═══════════════════════════════════════════════════════════════════
async def enrich_pick_identity_async(db, pick: dict) -> dict:
    """DB-aware enrichment.  Returns the same shape as
    :func:`enrich_pick_identity`, but with canonical ids upgraded
    from ``fallback:*`` to the AUTHORITATIVE ids used by history
    collections whenever a match is found."""
    from services.pick_identity_authority import (
        resolve_team_authoritative,
        resolve_player_authoritative,
    )
    out = enrich_pick_identity(pick)
    sport = _sport_l(pick.get("sport"))

    # Team upgrade — for team markets AND totals (which carry
    # home/away names).
    if out.get("team"):
        auth = await resolve_team_authoritative(
            db, sport=sport, name=out["team"])
        if auth:
            out["canonical_team_id"] = auth
            out["identity_quality"] = "authoritative"
            out["identity_resolution"]["sources"].append(
                "authoritative_team_lookup")
    if out.get("opponent_team"):
        auth = await resolve_team_authoritative(
            db, sport=sport, name=out["opponent_team"])
        if auth:
            out["canonical_opponent_id"] = auth
            out["identity_resolution"]["sources"].append(
                "authoritative_opponent_lookup")
    if out.get("home_team_name") and sport in _TEAM_SPORTS:
        # Also upgrade event_id if home+away resolved authoritatively.
        home_auth = await resolve_team_authoritative(
            db, sport=sport, name=out["home_team_name"])
        away_auth = await resolve_team_authoritative(
            db, sport=sport, name=out.get("away_team_name"))
        if home_auth and away_auth and pick.get("event_time"):
            from services.identity_resolver import resolve_event
            ev = resolve_event(
                sport_key=sport,
                commence_time=pick.get("event_time"),
                home_team_id=home_auth,
                away_team_id=away_auth,
            )
            out["canonical_event_id"] = ev.canonical_event_id
            out["event_identity_quality"] = ev.identity_quality

    # Player upgrade — team markets never have a player; player and
    # individual markets do.
    if out.get("player_name"):
        team_hint = out.get("team") or pick.get("team")
        auth = await resolve_player_authoritative(
            db, sport=sport, name=out["player_name"],
            team_hint=team_hint,
        )
        if auth:
            out["canonical_player_id"] = auth
            out["identity_quality"] = "authoritative"
            out["identity_resolution"]["sources"].append(
                "authoritative_player_lookup")
        # Also upgrade individual-sport opponent (they are players).
        if sport in _INDIVIDUAL_SPORTS and out.get("opponent_team"):
            opp_auth = await resolve_player_authoritative(
                db, sport=sport, name=out["opponent_team"])
            if opp_auth:
                out["canonical_opponent_id"] = opp_auth
                out["identity_resolution"]["sources"].append(
                    "authoritative_opponent_player_lookup")

    return out


__all__ = [
    "enrich_pick_identity",
    "enrich_pick_identity_async",
    "apply_enrichment",
    "parse_event_participants",
    "classify_market",
    "extract_player_name_from_market",
]
