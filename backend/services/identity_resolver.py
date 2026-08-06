"""identity_resolver — Phase 3D canonical identity resolution.

Non-destructive helpers that MAP live records to their canonical
identity contracts.  Dry-run first: every resolver returns an
:class:`IdentityContract` **without** touching Mongo documents.

Canonical ID rules
──────────────────
* Provider ID present → ``{provider}:{provider_id}``. This is the
  strongest identity.
* No provider ID → ``fallback:{sport}:{sha1(context)}`` with
  ``identity_quality="fallback"``.  Never trust fallback as
  "verified".
* Never merge two records because normalized display names match.
* Never merge two teams because abbreviations match.
* Never resolve players by first-token / first-name only.

Alias resolution priority
─────────────────────────
1. Exact provider ID match (best).
2. Exact canonical ID match.
3. Normalized full-name match ONLY when sport + team + event context
   agree.
4. Otherwise: ``identity_quality="ambiguous"`` or ``"unresolved"``.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

from services.identity_contracts import (
    EventIdentity, TeamIdentity, PlayerIdentity,
    MarketContractIdentity, PredictionIdentity, BetLegIdentity,
)


# ═════════════════════════════════════════════════════════════════════
# Normalisation helpers
# ═════════════════════════════════════════════════════════════════════
_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Lowercase alphanumeric-only, whitespace collapsed.  NEVER
    reduces to first token — full-name normalisation only."""
    if not name:
        return ""
    return _NORMALISE_RE.sub("_", name.strip().lower()).strip("_")


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:20]


# ═════════════════════════════════════════════════════════════════════
# Team / Player / Event resolvers
# ═════════════════════════════════════════════════════════════════════
def resolve_team(
    *,
    provider:         Optional[str] = None,
    provider_team_id: Optional[str] = None,
    display_name:     Optional[str] = None,
    sport:            Optional[str] = None,
    aliases:          Optional[Iterable[str]] = None,
) -> TeamIdentity:
    if provider and provider_team_id:
        return TeamIdentity(
            canonical_team_id=f"{provider}:{provider_team_id}",
            provider=provider,
            provider_team_id=str(provider_team_id),
            display_name=display_name,
            aliases=tuple(sorted(set(aliases or ()))),
            sport=sport,
            identity_quality="provider",
        )
    if not (sport and display_name):
        return TeamIdentity(
            canonical_team_id=f"unresolved:{_sha1(display_name or '')}",
            display_name=display_name,
            sport=sport,
            identity_quality="unresolved",
            notes="missing sport or display_name for team fallback",
        )
    key = f"team:{sport.lower()}:{normalize_name(display_name)}"
    return TeamIdentity(
        canonical_team_id=f"fallback:{_sha1(key)}",
        display_name=display_name,
        sport=sport,
        aliases=tuple(sorted(set(aliases or ()))),
        identity_quality="fallback",
        notes="no provider_team_id — name-normalised fallback",
    )


def resolve_player(
    *,
    provider:            Optional[str] = None,
    provider_player_id:  Optional[str] = None,
    display_name:        Optional[str] = None,
    sport:               Optional[str] = None,
    team_id:             Optional[str] = None,
    position:            Optional[str] = None,
) -> PlayerIdentity:
    if provider and provider_player_id:
        return PlayerIdentity(
            canonical_player_id=f"{provider}:{provider_player_id}",
            provider=provider,
            provider_player_id=str(provider_player_id),
            display_name=display_name,
            team_id=team_id,
            sport=sport,
            position=position,
            identity_quality="provider",
        )
    # Player fallback REQUIRES sport + team_id + display_name so
    # "Aaron Judge NYY" is never confused with "Aaron Judge (other)".
    if not (sport and display_name and team_id):
        return PlayerIdentity(
            canonical_player_id=f"unresolved:{_sha1(display_name or '')}",
            display_name=display_name,
            sport=sport,
            team_id=team_id,
            position=position,
            identity_quality="unresolved",
            notes="player fallback requires sport + team_id + display_name",
        )
    key = f"player:{sport.lower()}:{team_id}:{normalize_name(display_name)}"
    return PlayerIdentity(
        canonical_player_id=f"fallback:{_sha1(key)}",
        display_name=display_name,
        sport=sport,
        team_id=team_id,
        position=position,
        identity_quality="fallback",
        notes="no provider_player_id — name+team+sport fallback",
    )


def resolve_event(
    *,
    provider:          Optional[str] = None,
    provider_event_id: Optional[str] = None,
    sport_key:         Optional[str] = None,
    commence_time:     Optional[str] = None,
    home_team_id:      Optional[str] = None,
    away_team_id:      Optional[str] = None,
) -> EventIdentity:
    if provider and provider_event_id:
        return EventIdentity(
            canonical_event_id=f"{provider}:{provider_event_id}",
            provider=provider,
            provider_event_id=str(provider_event_id),
            sport_key=sport_key,
            commence_time=commence_time,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            identity_quality="provider",
        )
    # Event fallback requires sport + commence_time + BOTH team ids
    if not (sport_key and commence_time and home_team_id and away_team_id):
        return EventIdentity(
            canonical_event_id=f"unresolved:{_sha1((sport_key or '') + (commence_time or ''))}",
            sport_key=sport_key,
            commence_time=commence_time,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            identity_quality="unresolved",
            notes="event fallback requires sport + commence + both team_ids",
        )
    key = f"event:{sport_key}:{commence_time}:{home_team_id}:{away_team_id}"
    return EventIdentity(
        canonical_event_id=f"fallback:{_sha1(key)}",
        sport_key=sport_key,
        commence_time=commence_time,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        identity_quality="fallback",
        notes="no provider_event_id — sport+time+teams fallback",
    )


# ═════════════════════════════════════════════════════════════════════
# Market contract resolver
# ═════════════════════════════════════════════════════════════════════
def resolve_market_contract(
    *,
    canonical_event_id:  str,
    market_key:          str,
    side:                Optional[str] = None,
    line:                Optional[float] = None,
    participant_id:      Optional[str] = None,
    bookmaker:           Optional[str] = None,
    provider_market_key: Optional[str] = None,
    odds_timestamp:      Optional[str] = None,
) -> MarketContractIdentity:
    """A market contract identity ALWAYS includes the exact line +
    bookmaker (per spec).  Missing either degrades identity_quality
    but never merges rows."""
    parts = [
        canonical_event_id or "no_event",
        market_key or "no_market",
        (participant_id or "no_participant"),
        (side or "no_side"),
        f"line={line}" if line is not None else "line=none",
        (bookmaker or "no_book"),
    ]
    key = "|".join(parts)
    qual = "canonical"
    if line is None or bookmaker is None:
        qual = "fallback"
    if not canonical_event_id or not market_key:
        qual = "unresolved"
    return MarketContractIdentity(
        canonical_market_contract_id=f"mc:{_sha1(key)}",
        canonical_event_id=canonical_event_id,
        market_key=market_key,
        participant_id=participant_id,
        side=side,
        line=line,
        bookmaker=bookmaker,
        provider_market_key=provider_market_key,
        odds_timestamp=odds_timestamp,
        identity_quality=qual,
        notes=("degraded — missing line or bookmaker" if qual == "fallback" else ""),
    )


# ═════════════════════════════════════════════════════════════════════
# Prediction + Bet leg
# ═════════════════════════════════════════════════════════════════════
def resolve_prediction(
    *, prediction_id: str,
    snapshot_id: Optional[str] = None,
    canonical_event_id: Optional[str] = None,
    canonical_market_contract_id: Optional[str] = None,
    publication_version: Optional[int] = None,
) -> PredictionIdentity:
    if not prediction_id:
        return PredictionIdentity(
            prediction_id="",
            identity_quality="unresolved",
        )
    return PredictionIdentity(
        prediction_id=prediction_id,
        snapshot_id=snapshot_id,
        canonical_event_id=canonical_event_id,
        canonical_market_contract_id=canonical_market_contract_id,
        publication_version=publication_version,
        identity_quality="canonical",
    )


def resolve_bet_leg(
    *, user_bet_id: str, leg_id: str,
    prediction_id: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    canonical_market_contract_id: Optional[str] = None,
) -> BetLegIdentity:
    return BetLegIdentity(
        user_bet_id=user_bet_id,
        leg_id=leg_id,
        prediction_id=prediction_id,
        snapshot_id=snapshot_id,
        canonical_market_contract_id=canonical_market_contract_id,
        identity_quality="canonical" if user_bet_id and leg_id else "unresolved",
    )


# ═════════════════════════════════════════════════════════════════════
# Dry-run scanner
# ═════════════════════════════════════════════════════════════════════
async def dry_run_scan_collection(
    db, collection: str, sample_size: int = 500,
) -> dict[str, Any]:
    """Sample up to ``sample_size`` documents from ``collection`` and
    propose canonical identities.  Reports counts by identity_quality
    and lists candidate collisions.  DOES NOT WRITE anything."""
    quality_counts: dict[str, int] = {}
    proposed: dict[str, list[str]] = {}
    ambiguous: list[dict[str, Any]] = []
    scanned = 0
    async for doc in db[collection].find({}, {"_id": 0}).limit(sample_size):
        scanned += 1
        provider   = (doc.get("provider")
                      or doc.get("source")
                      or doc.get("sportsbook")
                      or None)
        prov_pid   = (doc.get("provider_player_id")
                      or doc.get("player_id"))
        prov_teamid= (doc.get("provider_team_id")
                      or doc.get("team_id"))
        prov_evtid = (doc.get("provider_event_id")
                      or doc.get("event_id")
                      or doc.get("external_id"))
        sport      = doc.get("sport") or doc.get("sport_key")
        display    = (doc.get("selection")
                      or doc.get("display_name")
                      or doc.get("player_name")
                      or doc.get("team")
                      or doc.get("event"))
        # For picks-like docs: propose a MarketContractIdentity.
        if collection in ("picks", "prediction_snapshots", "settlement_events",
                          "pick_enrichment", "live_alt_lines"):
            ev = resolve_event(
                provider=provider, provider_event_id=prov_evtid,
                sport_key=sport, commence_time=doc.get("event_time"),
                home_team_id=doc.get("home_team"),
                away_team_id=doc.get("away_team"),
            )
            mc = resolve_market_contract(
                canonical_event_id=ev.canonical_event_id,
                market_key=doc.get("market") or "",
                side=doc.get("side") or doc.get("selection_norm"),
                line=doc.get("line"),
                participant_id=display,   # fallback name for participant
                bookmaker=doc.get("book") or doc.get("bookmaker"),
                provider_market_key=doc.get("market"),
                odds_timestamp=doc.get("first_seen") or doc.get("created_at"),
            )
            q = mc.identity_quality
            proposed.setdefault(mc.canonical_market_contract_id, []).append(
                str(doc.get("id") or doc.get("external_id") or scanned)
            )
        elif collection in ("user_bets", "parlay_history"):
            q = "provider" if doc.get("user_bet_id") else "fallback"
        elif "team" in collection.lower():
            t = resolve_team(
                provider=provider, provider_team_id=prov_teamid,
                display_name=display, sport=sport,
            )
            q = t.identity_quality
        elif "player" in collection.lower():
            p = resolve_player(
                provider=provider, provider_player_id=prov_pid,
                display_name=display, sport=sport,
                team_id=doc.get("team_id"),
            )
            q = p.identity_quality
        else:
            q = "unresolved"
        quality_counts[q] = quality_counts.get(q, 0) + 1
    # Collision detection: any proposed canonical id with >1 source row.
    collisions = {k: v for k, v in proposed.items() if len(v) > 1}
    return {
        "collection":       collection,
        "sampled":          scanned,
        "quality_counts":   quality_counts,
        "collisions":       {k: len(v) for k, v in collisions.items()},
        "collision_examples": {k: v[:5] for k, v in list(collisions.items())[:5]},
        "ambiguous_count":  len(ambiguous),
    }


DRY_RUN_CRITICAL_COLLECTIONS = (
    "picks", "prediction_snapshots", "settlement_events",
    "pick_enrichment", "user_bets", "parlay_history",
    "players", "tennis_players", "soccer_player_form",
)


async def dry_run_scan_all(db, collections: Optional[list[str]] = None) -> dict[str, Any]:
    default_collections = [
        "picks", "prediction_snapshots", "settlement_events",
        "pick_enrichment", "user_bets", "parlay_history",
        "live_alt_lines", "player_game_logs", "players",
        "soccer_player_form", "tennis_players",
    ]
    targets = collections or default_collections
    out: dict[str, Any] = {}
    for coll in targets:
        try:
            out[coll] = await dry_run_scan_collection(db, coll)
        except Exception as e:
            out[coll] = {"error": str(e)}
    return out


__all__ = [
    "normalize_name",
    "resolve_team", "resolve_player", "resolve_event",
    "resolve_market_contract",
    "resolve_prediction", "resolve_bet_leg",
    "dry_run_scan_collection", "dry_run_scan_all",
]
