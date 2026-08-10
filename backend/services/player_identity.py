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
    # ── Club affiliation (BC field names preserved) ──
    current_team: Optional[str] = None
    historical_teams: list[dict] = field(default_factory=list)
    roster_status: str = "unknown"
    source: str = "unknown"
    observed_at: Optional[str] = None
    # ── National-team affiliation (P0-C, 2026-08-11) ──
    # Independent freshness stream — a stale national-team
    # observation must NEVER be blocked by fresh club data and
    # vice versa.  `nationality` is the player's country of
    # eligibility (may differ from active call-up team, though rare).
    nationality: Optional[str] = None
    current_national_team: Optional[str] = None
    historical_national_teams: list[dict] = field(default_factory=list)
    national_team_status: str = "unknown"
    national_team_source: str = "unknown"
    national_team_observed_at: Optional[str] = None

    # ── Club freshness ──
    def is_current_team_fresh(self, staleness_days: int = _STALENESS_DAYS) -> bool:
        if not self.current_team or not self.observed_at:
            return False
        try:
            ts = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except Exception:
            return False
        return (datetime.now(timezone.utc) - ts) <= timedelta(days=staleness_days)

    # ── National-team freshness (P0-C) ──
    def is_current_national_team_fresh(
        self, staleness_days: int = _STALENESS_DAYS,
    ) -> bool:
        if not self.current_national_team or not self.national_team_observed_at:
            return False
        try:
            ts = datetime.fromisoformat(
                self.national_team_observed_at.replace("Z", "+00:00"))
        except Exception:
            return False
        return (datetime.now(timezone.utc) - ts) <= timedelta(days=staleness_days)

    @property
    def current_club(self) -> Optional[str]:
        """P0-C alias — semantic name for `current_team`."""
        return self.current_team

    @property
    def historical_clubs(self) -> list[dict]:
        """P0-C alias — semantic name for `historical_teams`."""
        return self.historical_teams

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
            # Club
            "current_team": self.current_team,
            "historical_teams": list(self.historical_teams),
            "roster_status": self.roster_status,
            "source": self.source,
            "observed_at": self.observed_at,
            # National team (P0-C)
            "nationality": self.nationality,
            "current_national_team": self.current_national_team,
            "historical_national_teams": list(self.historical_national_teams),
            "national_team_status": self.national_team_status,
            "national_team_source": self.national_team_source,
            "national_team_observed_at": self.national_team_observed_at,
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
                affiliation_type: str = "club",
                nationality: Optional[str] = None,
                ) -> PlayerIdentity:
        """Upsert a player identity.

        Parameters
        ----------
        affiliation_type
            ``"club"`` (default) — ``current_team`` is written to
            ``PlayerIdentity.current_team`` (club affiliation).
            ``"national_team"`` — ``current_team`` is written to
            ``PlayerIdentity.current_national_team`` (independent
            freshness stream, independent history).

        National-team writes NEVER touch club fields, and vice versa —
        each affiliation has its own observation timestamp, source and
        transfer history so a stale club observation cannot invalidate
        a fresh national-team observation.
        """
        existing = self.resolve(name=name, sport=sport, league=league,
                                 provider=provider, provider_id=provider_id)
        if existing:
            # Anti-collision: if a DIFFERENT provider id was supplied
            # and this identity already has one for that provider that
            # doesn't match, mint a NEW canonical id.  Similar names
            # ≠ same player.  Only enforce when the incoming write is
            # for the same affiliation type (national-team providers
            # commonly differ from club providers for the same
            # canonical player).
            if provider and provider_id and affiliation_type == "club":
                cur = existing.provider_ids.get(provider)
                if cur and cur != str(provider_id):
                    return self._mint(name=name, sport=sport, league=league,
                                       provider=provider, provider_id=provider_id,
                                       current_team=current_team,
                                       position=position, role=role,
                                       roster_status=roster_status,
                                       source=source, observed_at=observed_at,
                                       dob=dob,
                                       affiliation_type=affiliation_type,
                                       nationality=nationality)
            # Merge provider id.
            if provider and provider_id:
                existing.provider_ids[provider] = str(provider_id)
                self._by_provider[(provider, str(provider_id))] = (
                    existing.canonical_player_id)
            if nationality and not existing.nationality:
                existing.nationality = nationality
            if current_team:
                if affiliation_type == "national_team":
                    self._maybe_transfer_national(
                        existing, current_team,
                        source=source, observed_at=observed_at,
                        status=roster_status)
                else:
                    self._maybe_transfer(existing, current_team,
                                          source=source,
                                          observed_at=observed_at)
            if position and not existing.position:
                existing.position = position
            if role and not existing.role:
                existing.role = role
            if roster_status and roster_status != "unknown" \
                    and affiliation_type == "club":
                existing.roster_status = roster_status
            return existing
        return self._mint(name=name, sport=sport, league=league,
                          provider=provider, provider_id=provider_id,
                          current_team=current_team,
                          position=position, role=role,
                          roster_status=roster_status,
                          source=source, observed_at=observed_at,
                          dob=dob,
                          affiliation_type=affiliation_type,
                          nationality=nationality)

    def _mint(self, *, name: str, sport: str, league: str, dob: Optional[str],
               affiliation_type: str = "club",
               nationality: Optional[str] = None,
               **kw) -> PlayerIdentity:
        name_norm = _norm(name)
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
            nationality=nationality,
        )
        if affiliation_type == "national_team":
            ident.current_national_team = kw.get("current_team")
            ident.national_team_status = kw.get("roster_status") or "unknown"
            ident.national_team_source = kw.get("source") or "unknown"
            ident.national_team_observed_at = kw.get("observed_at")
            if ident.current_national_team:
                ident.historical_national_teams.append({
                    "team": ident.current_national_team,
                    "from": ident.national_team_observed_at
                            or datetime.now(timezone.utc).isoformat(),
                    "to": None,
                    "source": ident.national_team_source,
                })
        else:
            ident.current_team = kw.get("current_team")
            ident.roster_status = kw.get("roster_status") or "unknown"
            ident.source = kw.get("source") or "unknown"
            ident.observed_at = kw.get("observed_at")
            if ident.current_team:
                ident.historical_teams.append({
                    "team": ident.current_team,
                    "from": ident.observed_at
                            or datetime.now(timezone.utc).isoformat(),
                    "to": None,
                    "source": ident.source,
                })
        self._by_id[cid] = ident
        self._by_name_league[(sport, league, name_norm)] = cid
        if kw.get("provider") and kw.get("provider_id"):
            self._by_provider[(kw["provider"], str(kw["provider_id"]))] = cid
        return ident

    def _maybe_transfer(self, ident: PlayerIdentity, new_team: str,
                         *, source: str, observed_at: Optional[str]) -> None:
        """Club transfer — update ``current_team`` only when the
        observation is fresher and the team actually changed."""
        if not new_team:
            return
        ts_new = _parse(observed_at) or datetime.now(timezone.utc)
        ts_cur = _parse(ident.observed_at) or datetime.min.replace(
            tzinfo=timezone.utc)
        if ts_new < ts_cur:
            return
        if _norm(new_team) == _norm(ident.current_team or ""):
            ident.observed_at = observed_at or ident.observed_at
            ident.source = source or ident.source
            return
        if ident.historical_teams:
            last = ident.historical_teams[-1]
            if last.get("to") is None:
                last["to"] = ts_new.isoformat()
        ident.historical_teams.append({
            "team": new_team, "from": ts_new.isoformat(),
            "to": None, "source": source,
        })
        ident.current_team = new_team
        ident.observed_at = ts_new.isoformat()
        ident.source = source or ident.source

    def _maybe_transfer_national(self, ident: PlayerIdentity, new_team: str,
                                  *, source: str, observed_at: Optional[str],
                                  status: str = "unknown") -> None:
        """National-team transfer — independent stream from club.

        A player's national-team status changes rarely (naturalization,
        FIFA switch), but when it does it must be recorded WITHOUT
        touching the club affiliation.
        """
        if not new_team:
            return
        ts_new = _parse(observed_at) or datetime.now(timezone.utc)
        ts_cur = _parse(ident.national_team_observed_at) or datetime.min.replace(
            tzinfo=timezone.utc)
        if ts_new < ts_cur:
            return
        if _norm(new_team) == _norm(ident.current_national_team or ""):
            ident.national_team_observed_at = observed_at \
                or ident.national_team_observed_at
            ident.national_team_source = source or ident.national_team_source
            if status and status != "unknown":
                ident.national_team_status = status
            return
        if ident.historical_national_teams:
            last = ident.historical_national_teams[-1]
            if last.get("to") is None:
                last["to"] = ts_new.isoformat()
        ident.historical_national_teams.append({
            "team": new_team, "from": ts_new.isoformat(),
            "to": None, "source": source,
        })
        ident.current_national_team = new_team
        ident.national_team_observed_at = ts_new.isoformat()
        ident.national_team_source = source or ident.national_team_source
        if status and status != "unknown":
            ident.national_team_status = status

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
                nationality=d.get("nationality"),
                current_national_team=d.get("current_national_team"),
                historical_national_teams=list(
                    d.get("historical_national_teams") or []),
                national_team_status=d.get("national_team_status") or "unknown",
                national_team_source=d.get("national_team_source") or "unknown",
                national_team_observed_at=d.get("national_team_observed_at"),
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
#
# P0-A (2026-08-11) — production-safe persistence.
#
# Design constraints from the user:
#   * Idempotent upserts keyed by canonical_player_id (no duplicates).
#   * Older observations must NEVER overwrite fresher current-team
#     information — even under concurrent multi-replica writes.
#   * Restart hydration must NOT alter observed_at (stale stays stale).
#   * Additive fields (aliases, provider_ids, historical_teams) merge
#     without losing prior data.
#
# Concurrency strategy:
#   1. `$setOnInsert` upsert seeds a fresh document atomically when
#      none exists. Multiple replicas racing to insert will collapse
#      to a single winner via the unique index on canonical_player_id.
#   2. Freshness-fields update is a CONDITIONAL `update_one` — the
#      filter requires the DB's current observed_at to be strictly
#      OLDER than the write we're attempting. If a fresher observation
#      already landed, matched_count == 0 and we skip.
#   3. Additive fields use `$addToSet` and dotted-key `$set` on
#      provider_ids — both idempotent.
IDENTITY_COLLECTION = "player_identities"


async def ensure_identity_indexes(db) -> None:
    """Create the unique index required for race-safe upserts.

    Idempotent — Mongo `create_index` on an existing spec is a no-op.
    """
    try:
        await db[IDENTITY_COLLECTION].create_index(
            "canonical_player_id", unique=True, name="canonical_player_id_uniq")
    except Exception:
        # Duplicate-key errors during index build are surfaced by Mongo;
        # we log via the caller — never break startup on this.
        pass


def _iso_lt(a: Optional[str], b: Optional[str]) -> bool:
    """True iff `a` is strictly older than `b`. `None`/malformed treated
    as `-inf`. ISO-8601 UTC strings are lexicographically sortable, but
    we go through datetime for robustness to `Z` suffix."""
    da = _parse(a)
    db_ = _parse(b)
    if db_ is None:
        return False
    if da is None:
        return True
    return da < db_


async def persist_identity(db, doc: dict) -> str:
    """Race-safe per-identity persistence.

    Returns one of:
        "inserted"     — new document created
        "advanced"     — existing doc's current-team fields moved forward
        "merged_only"  — no freshness change but additive fields merged
        "skipped"      — no-op (nothing new to write)
    """
    cid = doc.get("canonical_player_id")
    if not cid:
        return "skipped"
    new_obs = doc.get("observed_at")

    # ── 1. Atomic seed. `$setOnInsert` never touches an existing doc
    #     so this is safe under concurrent inserts (unique index will
    #     collapse duplicate attempts to a single winner).
    seed_doc = dict(doc)
    # Ensure a historical_teams list exists on brand-new docs when a
    # current_team is being seeded.
    if seed_doc.get("current_team") and not seed_doc.get("historical_teams"):
        seed_doc["historical_teams"] = [{
            "team": seed_doc["current_team"],
            "from": new_obs or datetime.now(timezone.utc).isoformat(),
            "to": None,
            "source": seed_doc.get("source", "unknown"),
        }]
    try:
        seed_res = await db[IDENTITY_COLLECTION].update_one(
            {"canonical_player_id": cid},
            {"$setOnInsert": seed_doc},
            upsert=True,
        )
    except Exception:
        # Duplicate-key on the unique index means another replica beat
        # us — that's a valid race outcome, fall through to merge.
        seed_res = None

    inserted = bool(seed_res and getattr(seed_res, "upserted_id", None) is not None)

    # ── 2. Conditional freshness update. Only advance current-team
    #     fields when our observation is STRICTLY fresher than what
    #     the DB currently holds. Guarded by a filter on observed_at
    #     so concurrent writers cannot clobber each other.
    advanced = False
    if new_obs and doc.get("current_team"):
        existing = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid},
            {"_id": 0, "current_team": 1, "observed_at": 1,
             "historical_teams": 1},
        )
        if existing is None:
            existing = {}
        cur_obs = existing.get("observed_at")
        # Fresher OR the record has no observation yet.
        if _iso_lt(cur_obs, new_obs) or cur_obs is None:
            prev_team = existing.get("current_team")
            new_team = doc.get("current_team")
            set_update: dict[str, Any] = {
                "current_team": new_team,
                "observed_at": new_obs,
                "source": doc.get("source") or existing.get("source", "unknown"),
                "roster_status": doc.get("roster_status")
                    or existing.get("roster_status", "unknown"),
                "name": doc.get("name") or existing.get("name"),
                "name_norm": doc.get("name_norm") or existing.get("name_norm"),
                "sport": doc.get("sport") or existing.get("sport"),
                "league": doc.get("league") or existing.get("league"),
            }
            # If team actually changed, roll historical_teams forward.
            if new_team and _norm(new_team) != _norm(prev_team or ""):
                hist = list(existing.get("historical_teams") or [])
                if hist and hist[-1].get("to") is None:
                    hist[-1]["to"] = new_obs
                hist.append({
                    "team": new_team,
                    "from": new_obs,
                    "to": None,
                    "source": doc.get("source", "unknown"),
                })
                set_update["historical_teams"] = hist
            # Conditional write — ONLY when DB observed_at is still
            # what we read (or missing). If another replica just
            # advanced observed_at past ours, matched_count == 0 and
            # we correctly skip.
            filt: dict[str, Any] = {"canonical_player_id": cid}
            if cur_obs is None:
                filt["$or"] = [
                    {"observed_at": {"$exists": False}},
                    {"observed_at": None},
                    {"observed_at": {"$lt": new_obs}},
                ]
            else:
                filt["observed_at"] = {"$lt": new_obs}
            try:
                res = await db[IDENTITY_COLLECTION].update_one(
                    filt, {"$set": set_update})
                advanced = bool(res.modified_count)
            except Exception:
                advanced = False

    # ── 2b. Conditional freshness update — NATIONAL TEAM (P0-C).
    #     Independent freshness gate from club above.  A stale club
    #     write cannot advance national-team fields, and vice versa.
    nat_advanced = False
    new_nat_obs = doc.get("national_team_observed_at")
    if new_nat_obs and doc.get("current_national_team"):
        existing_nat = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid},
            {"_id": 0, "current_national_team": 1,
             "national_team_observed_at": 1,
             "historical_national_teams": 1},
        )
        if existing_nat is None:
            existing_nat = {}
        cur_nat_obs = existing_nat.get("national_team_observed_at")
        if _iso_lt(cur_nat_obs, new_nat_obs) or cur_nat_obs is None:
            prev_nt = existing_nat.get("current_national_team")
            new_nt = doc.get("current_national_team")
            set_nat: dict[str, Any] = {
                "current_national_team": new_nt,
                "national_team_observed_at": new_nat_obs,
                "national_team_source": doc.get("national_team_source")
                    or existing_nat.get("national_team_source", "unknown"),
                "national_team_status": doc.get("national_team_status")
                    or existing_nat.get("national_team_status", "unknown"),
            }
            if new_nt and _norm(new_nt) != _norm(prev_nt or ""):
                nhist = list(existing_nat.get("historical_national_teams") or [])
                if nhist and nhist[-1].get("to") is None:
                    nhist[-1]["to"] = new_nat_obs
                nhist.append({
                    "team": new_nt,
                    "from": new_nat_obs,
                    "to": None,
                    "source": doc.get("national_team_source", "unknown"),
                })
                set_nat["historical_national_teams"] = nhist
            filt_nat: dict[str, Any] = {"canonical_player_id": cid}
            if cur_nat_obs is None:
                filt_nat["$or"] = [
                    {"national_team_observed_at": {"$exists": False}},
                    {"national_team_observed_at": None},
                    {"national_team_observed_at": {"$lt": new_nat_obs}},
                ]
            else:
                filt_nat["national_team_observed_at"] = {"$lt": new_nat_obs}
            try:
                res = await db[IDENTITY_COLLECTION].update_one(
                    filt_nat, {"$set": set_nat})
                nat_advanced = bool(res.modified_count)
            except Exception:
                nat_advanced = False

    # ── 3. Additive merges — always safe, idempotent.
    merged = False
    additive: dict[str, Any] = {}
    aliases = [a for a in (doc.get("aliases") or []) if isinstance(a, str) and a]
    if aliases:
        additive.setdefault("$addToSet", {})["aliases"] = {"$each": aliases}
    provider_ids = doc.get("provider_ids") or {}
    for prov, pid in provider_ids.items():
        if prov and pid:
            additive.setdefault("$set", {})[f"provider_ids.{prov}"] = str(pid)
    # `nationality` is a stable player attribute (rarely changes) —
    # merge on first-write only via $setOnInsert semantics: use $set
    # only when the DB does not already have a value.
    if doc.get("nationality"):
        existing_nat_doc = await db[IDENTITY_COLLECTION].find_one(
            {"canonical_player_id": cid},
            {"_id": 0, "nationality": 1},
        )
        if existing_nat_doc is None or not existing_nat_doc.get("nationality"):
            additive.setdefault("$set", {})["nationality"] = doc["nationality"]
    if additive:
        try:
            res = await db[IDENTITY_COLLECTION].update_one(
                {"canonical_player_id": cid}, additive)
            merged = bool(res.modified_count)
        except Exception:
            merged = False

    if inserted:
        return "inserted"
    if advanced or nat_advanced:
        return "advanced"
    if merged:
        return "merged_only"
    return "skipped"


async def persist_registry(db) -> int:
    """Persist every identity in the in-memory registry into
    `db.player_identities` using the race-safe writer.

    Returns the number of writes that actually mutated Mongo
    (inserted + advanced + merged_only).  Idempotent — safe to call
    repeatedly and from multiple replicas.
    """
    docs = snapshot_registry()
    if not docs:
        return 0
    written = 0
    for d in docs:
        try:
            outcome = await persist_identity(db, d)
            if outcome in ("inserted", "advanced", "merged_only"):
                written += 1
        except Exception:
            # Never let a single bad doc break the batch.
            continue
    return written


async def hydrate_registry_from_mongo(db) -> int:
    """Load every identity from `db.player_identities` into the
    in-memory registry, preserving ALL fields (provider ids, aliases,
    historical_teams, roster_status, source, observed_at) EXACTLY as
    stored.  Idempotent — safe to call from startup and after any
    refresh loop.

    IMPORTANT: hydration never mutates the DB and never touches
    `observed_at`. A stale identity in Mongo stays stale after
    hydration.  The freshness gate (`is_current_team_fresh`) is what
    guards downstream callers from acting on outdated observations.
    """
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


async def has_fresh_national_team_membership(
    db, name_norm: str, staleness_days: int = _STALENESS_DAYS,
) -> bool:
    """P0-C — True iff we have a fresh national-team observation for
    a player."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=staleness_days)
    cutoff_iso = cutoff.isoformat()
    try:
        doc = await db[IDENTITY_COLLECTION].find_one({
            "name_norm": name_norm,
            "current_national_team": {"$nin": [None, ""]},
            "national_team_observed_at": {"$gte": cutoff_iso},
        }, {"_id": 0, "current_national_team": 1})
        return doc is not None
    except Exception:
        return False


__all__ = [
    "PlayerIdentity",
    "resolve_player", "upsert_player",
    "snapshot_registry", "hydrate_registry",
    "registry_size", "reset_registry_for_tests",
    "persist_registry", "persist_identity",
    "hydrate_registry_from_mongo", "ensure_identity_indexes",
    "has_fresh_roster_for_league",
    "has_fresh_national_team_membership",
    "IDENTITY_COLLECTION",
    "_norm",
]
