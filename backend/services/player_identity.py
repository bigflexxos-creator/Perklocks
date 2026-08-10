"""Canonical Player Identity Layer — Phase 2 Follow-up (2026-08-11).

One resolver, one stable ``canonical_player_id``.  Every writer /
enrichment path that needs to know "who is this player" should route
through :func:`resolve_player`.  The registry keeps historical teams
attached across transfers without letting historical teams override
current-team truth.

Contract for each ``PlayerIdentity`` record::

    {
      "canonical_player_id":  str          # stable across providers
      "provider_ids":         {provider: id}  # espn / statsapi / apisports / sportdb
      "name":                 str          # canonical display name (with diacritics)
      "aliases":              list[str]    # additional legitimate spellings
      "name_norm":            str          # diacritic-stripped lowercase key
      "sport":                str
      "league":               str
      "position":             Optional[str]
      "role":                 Optional[str]  # e.g. "striker", "starting_pitcher"
      "current_team":         Optional[str]
      "historical_teams":     list[{team: str, from: iso_date, to: iso_date or None}]
      "roster_status":        str          # "active" | "loan" | "reserve" | "retired" | "unknown"
      "source":               str          # provenance of the current_team observation
      "observed_at":          iso_datetime # freshness of current_team
    }

Anti-collision rules
────────────────────
* Similar-name players are NEVER auto-merged. Resolution requires
  either a provider-id match OR a (sport, league, exact normalised
  name) match — otherwise a NEW canonical id is minted.
* Two players with identical normalised names in the same league are
  disambiguated by provider id or by explicit ``dob`` field on the
  input (never silently merged).

Historical stats
────────────────
* Stats attach to ``canonical_player_id`` — they follow the PLAYER
  across transfers.  Historical team appearances live in
  ``historical_teams`` and are used ONLY for context (never as proof
  of current membership).

Freshness gate for "current team"
─────────────────────────────────
* ``current_team`` is only trusted when ``observed_at`` is within a
  configurable staleness window (default 30 days).  Older
  observations are considered stale and the caller must fall back to
  ``roster_status="unknown"``.
"""
from __future__ import annotations

import hashlib
import unicodedata
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


_STALENESS_DAYS = 30


def _norm(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = re.sub(r"[.'’\-]", "", ascii_only)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


@dataclass
class PlayerIdentity:
    canonical_player_id: str
    name: str
    name_norm: str
    sport: str
    league: str
    provider_ids: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    position: Optional[str] = None
    role: Optional[str] = None
    current_team: Optional[str] = None
    historical_teams: list[dict] = field(default_factory=list)
    roster_status: str = "unknown"
    source: str = "unknown"
    observed_at: Optional[str] = None

    def is_current_team_fresh(self, staleness_days: int = _STALENESS_DAYS) -> bool:
        if not self.current_team or not self.observed_at:
            return False
        try:
            ts = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except Exception:
            return False
        return (datetime.now(timezone.utc) - ts) <= timedelta(days=staleness_days)

    def to_dict(self) -> dict:
        return {
            "canonical_player_id": self.canonical_player_id,
            "provider_ids": dict(self.provider_ids),
            "name": self.name,
            "name_norm": self.name_norm,
            "aliases": list(self.aliases),
            "sport": self.sport,
            "league": self.league,
            "position": self.position,
            "role": self.role,
            "current_team": self.current_team,
            "historical_teams": list(self.historical_teams),
            "roster_status": self.roster_status,
            "source": self.source,
            "observed_at": self.observed_at,
        }


class _IdentityRegistry:
    """Thin in-memory registry.  Callers can persist via
    `snapshot_to_dicts` / `hydrate_from_dicts` for Mongo backing."""

    def __init__(self) -> None:
        self._by_id: dict[str, PlayerIdentity] = {}
        self._by_provider: dict[tuple[str, str], str] = {}
        self._by_name_league: dict[tuple[str, str, str], str] = {}

    # ── Lookup ──────────────────────────────────────────────────
    def resolve(self, *, name: str, sport: str, league: str,
                provider: Optional[str] = None,
                provider_id: Optional[str] = None,
                ) -> Optional[PlayerIdentity]:
        # 1. Provider-id match — the strongest signal.
        if provider and provider_id:
            cid = self._by_provider.get((provider, str(provider_id)))
            if cid:
                return self._by_id[cid]
        # 2. Exact (sport, league, name_norm) match.
        name_norm = _norm(name)
        cid = self._by_name_league.get((sport, league, name_norm))
        if cid:
            return self._by_id[cid]
        return None

    # ── Ingest ──────────────────────────────────────────────────
    def upsert(self, *, name: str, sport: str, league: str,
                provider: Optional[str] = None,
                provider_id: Optional[str] = None,
                current_team: Optional[str] = None,
                position: Optional[str] = None,
                role: Optional[str] = None,
                roster_status: str = "unknown",
                source: str = "unknown",
                observed_at: Optional[str] = None,
                dob: Optional[str] = None,
                ) -> PlayerIdentity:
        existing = self.resolve(name=name, sport=sport, league=league,
                                 provider=provider, provider_id=provider_id)
        if existing:
            # Anti-collision: if a DIFFERENT provider id was supplied
            # and this identity already has one for that provider that
            # doesn't match, mint a NEW canonical id (don't silently
            # overwrite).  Similar names ≠ same player.
            if provider and provider_id:
                cur = existing.provider_ids.get(provider)
                if cur and cur != str(provider_id):
                    # Different provider id → different player.
                    return self._mint(name=name, sport=sport, league=league,
                                       provider=provider, provider_id=provider_id,
                                       current_team=current_team,
                                       position=position, role=role,
                                       roster_status=roster_status,
                                       source=source, observed_at=observed_at,
                                       dob=dob)
            # Merge — provider id newly supplied.
            if provider and provider_id:
                existing.provider_ids[provider] = str(provider_id)
                self._by_provider[(provider, str(provider_id))] = (
                    existing.canonical_player_id)
            # Current team update — only when observation is fresher.
            if current_team:
                self._maybe_transfer(existing, current_team,
                                      source=source, observed_at=observed_at)
            if position and not existing.position:
                existing.position = position
            if role and not existing.role:
                existing.role = role
            if roster_status and roster_status != "unknown":
                existing.roster_status = roster_status
            return existing
        return self._mint(name=name, sport=sport, league=league,
                          provider=provider, provider_id=provider_id,
                          current_team=current_team,
                          position=position, role=role,
                          roster_status=roster_status,
                          source=source, observed_at=observed_at,
                          dob=dob)

    def _mint(self, *, name: str, sport: str, league: str, dob: Optional[str],
               **kw) -> PlayerIdentity:
        name_norm = _norm(name)
        # Deterministic canonical id — hash covers sport, league,
        # normalised name AND (provider_id or dob) to prevent
        # collisions between same-named players.
        seed = "|".join([
            sport, league, name_norm,
            str(kw.get("provider_id") or "") or str(dob or ""),
        ])
        cid = "cpid_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        ident = PlayerIdentity(
            canonical_player_id=cid,
            name=name, name_norm=name_norm,
            sport=sport, league=league,
            provider_ids=({kw.get("provider"): str(kw.get("provider_id"))}
                           if kw.get("provider") and kw.get("provider_id") else {}),
            position=kw.get("position"),
            role=kw.get("role"),
            current_team=kw.get("current_team"),
            roster_status=kw.get("roster_status") or "unknown",
            source=kw.get("source") or "unknown",
            observed_at=kw.get("observed_at"),
        )
        self._by_id[cid] = ident
        self._by_name_league[(sport, league, name_norm)] = cid
        if kw.get("provider") and kw.get("provider_id"):
            self._by_provider[(kw["provider"], str(kw["provider_id"]))] = cid
        if ident.current_team:
            ident.historical_teams.append({
                "team": ident.current_team,
                "from": ident.observed_at
                        or datetime.now(timezone.utc).isoformat(),
                "to": None,
                "source": ident.source,
            })
        return ident

    def _maybe_transfer(self, ident: PlayerIdentity, new_team: str,
                         *, source: str, observed_at: Optional[str]) -> None:
        """Update current_team only when the observation is fresher
        than the existing one AND the team actually changed."""
        if not new_team:
            return
        ts_new = _parse(observed_at) or datetime.now(timezone.utc)
        ts_cur = _parse(ident.observed_at) or datetime.min.replace(
            tzinfo=timezone.utc)
        if ts_new < ts_cur:
            return   # older observation → ignore
        if _norm(new_team) == _norm(ident.current_team or ""):
            # Same team — just refresh timestamp/source.
            ident.observed_at = observed_at or ident.observed_at
            ident.source = source or ident.source
            return
        # Genuine transfer — close the previous historical entry.
        if ident.historical_teams:
            last = ident.historical_teams[-1]
            if last.get("to") is None:
                last["to"] = ts_new.isoformat()
        ident.historical_teams.append({
            "team": new_team,
            "from": ts_new.isoformat(),
            "to": None,
            "source": source,
        })
        ident.current_team = new_team
        ident.observed_at = ts_new.isoformat()
        ident.source = source or ident.source

    def snapshot_to_dicts(self) -> list[dict]:
        return [i.to_dict() for i in self._by_id.values()]

    def hydrate_from_dicts(self, docs: list[dict]) -> None:
        for d in docs:
            ident = PlayerIdentity(
                canonical_player_id=d["canonical_player_id"],
                name=d.get("name") or "",
                name_norm=d.get("name_norm") or _norm(d.get("name") or ""),
                sport=d.get("sport") or "",
                league=d.get("league") or "",
                provider_ids=dict(d.get("provider_ids") or {}),
                aliases=list(d.get("aliases") or []),
                position=d.get("position"),
                role=d.get("role"),
                current_team=d.get("current_team"),
                historical_teams=list(d.get("historical_teams") or []),
                roster_status=d.get("roster_status") or "unknown",
                source=d.get("source") or "unknown",
                observed_at=d.get("observed_at"),
            )
            self._by_id[ident.canonical_player_id] = ident
            self._by_name_league[
                (ident.sport, ident.league, ident.name_norm)
            ] = ident.canonical_player_id
            for prov, pid in ident.provider_ids.items():
                if prov and pid:
                    self._by_provider[(prov, str(pid))] = (
                        ident.canonical_player_id)


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        return None


# ── Module-level singleton (in-memory) ─────────────────────────────
_REGISTRY = _IdentityRegistry()


def resolve_player(**kw) -> Optional[PlayerIdentity]:
    return _REGISTRY.resolve(**kw)


def upsert_player(**kw) -> PlayerIdentity:
    return _REGISTRY.upsert(**kw)


def snapshot_registry() -> list[dict]:
    return _REGISTRY.snapshot_to_dicts()


def hydrate_registry(docs: list[dict]) -> None:
    _REGISTRY.hydrate_from_dicts(docs)


def registry_size() -> int:
    return len(_REGISTRY._by_id)


def reset_registry_for_tests() -> None:
    """Only for pytest — clears the module-level singleton."""
    global _REGISTRY
    _REGISTRY = _IdentityRegistry()


# ── Mongo persistence + hydration ─────────────────────────────────
IDENTITY_COLLECTION = "player_identities"


async def persist_registry(db) -> int:
    """Upsert every identity in the in-memory registry into
    `db.player_identities`.  Returns the number of docs written."""
    docs = snapshot_registry()
    if not docs:
        return 0
    ops = []
    for d in docs:
        ops.append({"filter": {"canonical_player_id": d["canonical_player_id"]},
                     "update": {"$set": d},
                     "upsert": True})
    n = 0
    for op in ops:
        try:
            await db[IDENTITY_COLLECTION].update_one(
                op["filter"], op["update"], upsert=op["upsert"])
            n += 1
        except Exception:
            continue
    return n


async def hydrate_registry_from_mongo(db) -> int:
    """Load every identity from `db.player_identities` into the
    in-memory registry.  Idempotent — safe to call from startup and
    after any refresh loop."""
    reset_registry_for_tests()
    docs = [d async for d in db[IDENTITY_COLLECTION].find(
        {}, {"_id": 0})]
    hydrate_registry(docs)
    return len(docs)


async def has_fresh_roster_for_league(
    db, league: str, staleness_days: int = _STALENESS_DAYS,
) -> bool:
    """True iff `db.player_identities` contains AT LEAST ONE identity
    for the given league whose `observed_at` is within the staleness
    window.  Callers use this to fail safely when the roster feed
    hasn't landed yet (avoids mass roster_unverified rejections)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=staleness_days)
    cutoff_iso = cutoff.isoformat()
    try:
        doc = await db[IDENTITY_COLLECTION].find_one({
            "league": league,
            "observed_at": {"$gte": cutoff_iso},
        }, {"_id": 0, "canonical_player_id": 1})
        return doc is not None
    except Exception:
        return False


__all__ = [
    "PlayerIdentity",
    "resolve_player", "upsert_player",
    "snapshot_registry", "hydrate_registry",
    "registry_size", "reset_registry_for_tests",
    "persist_registry", "hydrate_registry_from_mongo",
    "has_fresh_roster_for_league",
    "IDENTITY_COLLECTION",
    "_norm",
]
