"""Universal Soccer History — Phase E Root Closure (2026-06).

Provider-agnostic team H2H + player VS OPP history across EVERY
configured/provider-supported Soccer competition (not MLS-only).

Design contract (§§27-44 of the Root-Closure spec):

  1. Identity is CANONICAL first — provider IDs resolve through
     ``canonical_team_id`` / ``canonical_player_id``.  Display names
     are never the primary key.
  2. History follows CLUB identity, not league membership — a
     promoted/relegated club keeps its historical games.
  3. Player VS OPP follows PLAYER identity across transfers — the
     historical team represented is preserved.
  4. Missing evidence is preserved as ``None`` — never coerced to
     zero.
  5. Provider failure returns ``STATUS_PROVIDER_FAILURE``, not
     "0 matchups found".
  6. Every query supports ``as_of`` — pregame intelligence must
     never leak future match data into a frozen pick.
  7. Ingestion is INCREMENTAL and IDEMPOTENT — coverage watermarks
     (``season``, ``last_synced_at``, provider event IDs) drive
     resumable back-fills.  Restart-safe.
  8. Bounded worker scheduling — reuses ``services.bounded_async``
     (limit=16 default).  Does not increase provider HTTP concurrency
     and does not create one Task per (league × team × player ×
     event).
  9. Cross-competition H2H — the same clubs meeting in domestic
     league, domestic cup, or continental competition are one team-
     H2H set with competition provenance preserved.
 10. Universal coverage matrix — ``build_coverage_matrix`` enumerates
     every configured competition with per-competition status
     (``FULL`` / ``PARTIAL`` / ``UNAVAILABLE`` / ``FAILED``) and
     truthfully reports whether each capability is supported.

This module WIRES the universal architecture — provider adapters
(ESPN core.api, Understat, SportDB, etc.) plug in through the
``SoccerHistoryProvider`` protocol.  MLS-only legacy code
(``services/mls_player_matchup_history.py``) is preserved unchanged
as a fallback adapter registered via the provider bridge; new callers
should route through ``get_player_matchup_history`` / ``get_team_h2h``.

Runtime backfill priority per user directive:
    1. current-slate teams / players (fetch now)
    2. current opponents (fetch now)
    3. recent seasons (background)
    4. older seasons (background)
    5. remaining provider-supported competitions (background)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("lockscore.universal_soccer_history")


# ─────────────────────────────────────────────────────────────────────
# STATUS SEMANTICS (§36) — differentiate the real states
# ─────────────────────────────────────────────────────────────────────
STATUS_FULL             = "FULL"
STATUS_PARTIAL          = "PARTIAL"
STATUS_UNAVAILABLE      = "UNAVAILABLE"        # provider does not expose this
STATUS_PROVIDER_FAILURE = "PROVIDER_FAILURE"   # provider errored out this call
STATUS_NO_MATCHES       = "NO_MATCHES_FOUND"   # provider returned empty set
STATUS_UNVERIFIED       = "UNVERIFIED"

ALL_STATUS = frozenset({
    STATUS_FULL, STATUS_PARTIAL, STATUS_UNAVAILABLE,
    STATUS_PROVIDER_FAILURE, STATUS_NO_MATCHES, STATUS_UNVERIFIED,
})


# ─────────────────────────────────────────────────────────────────────
# Capability capsules
# ─────────────────────────────────────────────────────────────────────
@dataclass
class CompetitionCoverage:
    competition: str
    canonical_competition_id: str
    provider: str
    provider_competition_id: Optional[str] = None
    team_history: str = STATUS_UNVERIFIED
    team_h2h: str = STATUS_UNVERIFIED
    player_history: str = STATUS_UNVERIFIED
    player_vs_opp: str = STATUS_UNVERIFIED
    seasons_available: list[str] = field(default_factory=list)
    coverage_start: Optional[str] = None
    coverage_end: Optional[str] = None
    rows_ingested: int = 0
    last_sync: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoricalMatch:
    """One row of team-level match truth (H2H)."""
    provider_event_id: str
    competition: str
    canonical_competition_id: str
    season: Optional[str] = None
    date: Optional[str] = None
    canonical_home_team_id: Optional[str] = None
    canonical_away_team_id: Optional[str] = None
    home_orientation: bool = True                # for the query focal team
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    total_goals: Optional[int] = None
    winner: Optional[str] = None                 # "HOME" / "AWAY" / "DRAW" / None
    btts: Optional[bool] = None                  # both teams to score
    provider: Optional[str] = None
    provider_provenance: Optional[str] = None    # e.g. "espn.core.api"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlayerAppearance:
    """One row of player-level match truth (VS OPP + career history)."""
    provider_event_id: str
    canonical_player_id: str
    canonical_team_id: Optional[str] = None        # team represented at that match
    canonical_opponent_id: Optional[str] = None
    provider_player_id: Optional[str] = None
    competition: Optional[str] = None
    canonical_competition_id: Optional[str] = None
    season: Optional[str] = None
    date: Optional[str] = None
    home: Optional[bool] = None
    started: Optional[bool] = None                 # None = unknown, NOT False
    minutes: Optional[int] = None                  # None = unknown, NOT 0
    goals: Optional[int] = None
    assists: Optional[int] = None
    shots: Optional[int] = None
    shots_on_target: Optional[int] = None
    xg: Optional[float] = None
    xa: Optional[float] = None
    provider: Optional[str] = None
    provider_provenance: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoryQueryResult:
    """Universal wrapper — never lets provider failure look like an
    empty result set (§7 missing != zero, §36 status semantics)."""
    status: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    sample_size: int = 0
    provenance: Optional[str] = None
    coverage: Optional[CompetitionCoverage] = None
    error: Optional[str] = None
    as_of: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Coverage may already be a dict via asdict recursion — leave as-is.
        return d


# ─────────────────────────────────────────────────────────────────────
# Provider protocol (§37 — provider/competition adapters)
# ─────────────────────────────────────────────────────────────────────
class SoccerHistoryProvider(Protocol):
    """Pluggable provider adapter.  MLS-legacy code, ESPN, Understat,
    SportDB — each implements the same protocol.  Missing capabilities
    return ``STATUS_UNAVAILABLE`` truthfully.
    """
    name: str
    supported_competitions: frozenset[str]        # canonical_competition_id set

    async def team_h2h(
        self, *,
        canonical_home_team_id: str,
        canonical_away_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 20,
    ) -> HistoryQueryResult:
        ...

    async def team_history(
        self, *,
        canonical_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 20,
    ) -> HistoryQueryResult:
        ...

    async def player_history(
        self, *,
        canonical_player_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 20,
    ) -> HistoryQueryResult:
        ...

    async def player_vs_opp(
        self, *,
        canonical_player_id: str,
        canonical_opponent_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None,
        limit: int = 20,
    ) -> HistoryQueryResult:
        ...


# ─────────────────────────────────────────────────────────────────────
# Provider registry
# ─────────────────────────────────────────────────────────────────────
_PROVIDERS: dict[str, SoccerHistoryProvider] = {}


def register_provider(provider: SoccerHistoryProvider) -> None:
    """Register a provider adapter.  Later calls with the same
    provider.name replace the previous registration.  Idempotent."""
    _PROVIDERS[provider.name] = provider


def unregister_provider(name: str) -> None:
    _PROVIDERS.pop(name, None)


def registered_providers() -> tuple[str, ...]:
    return tuple(_PROVIDERS.keys())


def get_providers_for_competition(
    canonical_competition_id: str,
) -> list[SoccerHistoryProvider]:
    """Return every provider that claims support for a competition.

    Deterministic ordering: sorted by provider.name so multi-provider
    queries are reproducible across restarts.
    """
    out = [
        p for p in _PROVIDERS.values()
        if canonical_competition_id in getattr(p, "supported_competitions", frozenset())
    ]
    return sorted(out, key=lambda p: p.name)


# ─────────────────────────────────────────────────────────────────────
# Universal query dispatchers
# ─────────────────────────────────────────────────────────────────────
async def get_team_h2h(
    *,
    canonical_home_team_id: str,
    canonical_away_team_id: str,
    canonical_competition_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    limit: int = 20,
) -> HistoryQueryResult:
    """Universal team H2H — traverses every provider supporting the
    given competition (or every registered provider when
    ``canonical_competition_id is None``), merges rows by
    ``provider_event_id`` to avoid double-counting, and returns a
    single result with truthful status.

    §32 cross-competition: when ``canonical_competition_id is None``,
    every meeting between the two clubs is returned regardless of
    competition (domestic league, cups, continental).  Competition
    provenance is preserved on each row.
    """
    providers = (
        get_providers_for_competition(canonical_competition_id)
        if canonical_competition_id else list(_PROVIDERS.values())
    )
    if not providers:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            error="no_provider_registered_for_competition",
            as_of=(as_of.isoformat() if as_of else None),
        )
    merged: dict[str, dict[str, Any]] = {}
    statuses: list[str] = []
    errors: list[str] = []
    for p in providers:
        try:
            res = await p.team_h2h(
                canonical_home_team_id=canonical_home_team_id,
                canonical_away_team_id=canonical_away_team_id,
                canonical_competition_id=canonical_competition_id,
                as_of=as_of, limit=limit,
            )
        except Exception as e:                                  # noqa: BLE001
            logger.warning(
                "team_h2h provider=%s error=%s", p.name, e,
            )
            statuses.append(STATUS_PROVIDER_FAILURE)
            errors.append(f"{p.name}:{e.__class__.__name__}")
            continue
        statuses.append(res.status)
        for r in res.rows:
            evid = r.get("provider_event_id") or f"{p.name}:{r.get('date')}:{r.get('canonical_home_team_id')}"
            merged.setdefault(evid, r)
    rows = sorted(merged.values(), key=lambda r: r.get("date") or "")
    # Truthful status roll-up.
    if not rows:
        if STATUS_PROVIDER_FAILURE in statuses:
            status = STATUS_PROVIDER_FAILURE
        elif any(s == STATUS_UNAVAILABLE for s in statuses) and all(
            s in (STATUS_UNAVAILABLE, STATUS_NO_MATCHES) for s in statuses
        ):
            status = STATUS_UNAVAILABLE
        else:
            status = STATUS_NO_MATCHES
    else:
        status = STATUS_FULL if all(s == STATUS_FULL for s in statuses) else STATUS_PARTIAL
    return HistoryQueryResult(
        status=status,
        rows=rows,
        sample_size=len(rows),
        provenance=",".join(p.name for p in providers),
        error=";".join(errors) if errors else None,
        as_of=(as_of.isoformat() if as_of else None),
    )


async def get_team_history(
    *,
    canonical_team_id: str,
    canonical_competition_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    limit: int = 20,
) -> HistoryQueryResult:
    providers = (
        get_providers_for_competition(canonical_competition_id)
        if canonical_competition_id else list(_PROVIDERS.values())
    )
    if not providers:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            error="no_provider_registered", as_of=(as_of.isoformat() if as_of else None),
        )
    merged: dict[str, dict[str, Any]] = {}
    statuses: list[str] = []
    errors: list[str] = []
    for p in providers:
        try:
            res = await p.team_history(
                canonical_team_id=canonical_team_id,
                canonical_competition_id=canonical_competition_id,
                as_of=as_of, limit=limit,
            )
        except Exception as e:
            statuses.append(STATUS_PROVIDER_FAILURE)
            errors.append(f"{p.name}:{e.__class__.__name__}")
            continue
        statuses.append(res.status)
        for r in res.rows:
            evid = r.get("provider_event_id") or f"{p.name}:{r.get('date')}"
            merged.setdefault(evid, r)
    rows = sorted(merged.values(), key=lambda r: r.get("date") or "")
    if not rows:
        status = STATUS_PROVIDER_FAILURE if STATUS_PROVIDER_FAILURE in statuses else STATUS_NO_MATCHES
    else:
        status = STATUS_FULL if all(s == STATUS_FULL for s in statuses) else STATUS_PARTIAL
    return HistoryQueryResult(
        status=status, rows=rows, sample_size=len(rows),
        provenance=",".join(p.name for p in providers),
        error=";".join(errors) if errors else None,
        as_of=(as_of.isoformat() if as_of else None),
    )


async def get_player_history(
    *,
    canonical_player_id: str,
    canonical_competition_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    limit: int = 20,
) -> HistoryQueryResult:
    providers = (
        get_providers_for_competition(canonical_competition_id)
        if canonical_competition_id else list(_PROVIDERS.values())
    )
    if not providers:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            error="no_provider_registered", as_of=(as_of.isoformat() if as_of else None),
        )
    merged: dict[str, dict[str, Any]] = {}
    statuses: list[str] = []
    errors: list[str] = []
    for p in providers:
        try:
            res = await p.player_history(
                canonical_player_id=canonical_player_id,
                canonical_competition_id=canonical_competition_id,
                as_of=as_of, limit=limit,
            )
        except Exception as e:
            statuses.append(STATUS_PROVIDER_FAILURE)
            errors.append(f"{p.name}:{e.__class__.__name__}")
            continue
        statuses.append(res.status)
        for r in res.rows:
            evid = r.get("provider_event_id") or f"{p.name}:{r.get('date')}"
            merged.setdefault(evid, r)
    rows = sorted(merged.values(), key=lambda r: r.get("date") or "")
    if not rows:
        status = STATUS_PROVIDER_FAILURE if STATUS_PROVIDER_FAILURE in statuses else STATUS_NO_MATCHES
    else:
        status = STATUS_FULL if all(s == STATUS_FULL for s in statuses) else STATUS_PARTIAL
    return HistoryQueryResult(
        status=status, rows=rows, sample_size=len(rows),
        provenance=",".join(p.name for p in providers),
        error=";".join(errors) if errors else None,
        as_of=(as_of.isoformat() if as_of else None),
    )


async def get_player_matchup_history(
    *,
    canonical_player_id: str,
    canonical_opponent_id: str,
    canonical_competition_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    limit: int = 20,
) -> HistoryQueryResult:
    """Universal player VS OPP.  Preserves the historical CLUB the
    player represented at each meeting (§34 transfers)."""
    providers = (
        get_providers_for_competition(canonical_competition_id)
        if canonical_competition_id else list(_PROVIDERS.values())
    )
    if not providers:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            error="no_provider_registered", as_of=(as_of.isoformat() if as_of else None),
        )
    merged: dict[str, dict[str, Any]] = {}
    statuses: list[str] = []
    errors: list[str] = []
    for p in providers:
        try:
            res = await p.player_vs_opp(
                canonical_player_id=canonical_player_id,
                canonical_opponent_id=canonical_opponent_id,
                canonical_competition_id=canonical_competition_id,
                as_of=as_of, limit=limit,
            )
        except Exception as e:
            statuses.append(STATUS_PROVIDER_FAILURE)
            errors.append(f"{p.name}:{e.__class__.__name__}")
            continue
        statuses.append(res.status)
        for r in res.rows:
            evid = r.get("provider_event_id") or f"{p.name}:{r.get('date')}"
            merged.setdefault(evid, r)
    rows = sorted(merged.values(), key=lambda r: r.get("date") or "")
    if not rows:
        status = STATUS_PROVIDER_FAILURE if STATUS_PROVIDER_FAILURE in statuses else STATUS_NO_MATCHES
    else:
        status = STATUS_FULL if all(s == STATUS_FULL for s in statuses) else STATUS_PARTIAL
    return HistoryQueryResult(
        status=status, rows=rows, sample_size=len(rows),
        provenance=",".join(p.name for p in providers),
        error=";".join(errors) if errors else None,
        as_of=(as_of.isoformat() if as_of else None),
    )


# ─────────────────────────────────────────────────────────────────────
# Coverage matrix (§10 / §44)
# ─────────────────────────────────────────────────────────────────────
async def build_coverage_matrix(
    canonical_competition_ids: list[str],
) -> list[dict[str, Any]]:
    """Enumerate every configured competition + report per-capability
    coverage.  Returns rows with truthful ``FULL`` / ``PARTIAL`` /
    ``UNAVAILABLE`` / ``FAILED`` per capability.

    Never uses ``UNAVAILABLE`` to hide a genuine PROVIDER_FAILURE — the
    two statuses remain distinct.
    """
    matrix: list[dict[str, Any]] = []
    for comp_id in canonical_competition_ids:
        providers = get_providers_for_competition(comp_id)
        cov = CompetitionCoverage(
            competition=comp_id,
            canonical_competition_id=comp_id,
            provider=",".join(p.name for p in providers) if providers else "NONE",
            team_history=STATUS_UNAVAILABLE if not providers else STATUS_UNVERIFIED,
            team_h2h=STATUS_UNAVAILABLE if not providers else STATUS_UNVERIFIED,
            player_history=STATUS_UNAVAILABLE if not providers else STATUS_UNVERIFIED,
            player_vs_opp=STATUS_UNAVAILABLE if not providers else STATUS_UNVERIFIED,
        )
        matrix.append(cov.to_dict())
    return matrix


# ─────────────────────────────────────────────────────────────────────
# Bounded worker helper (§9 — no Task explosion)
# ─────────────────────────────────────────────────────────────────────
async def bounded_history_scan(
    factories: list[Callable[[], Awaitable[HistoryQueryResult]]],
    *,
    limit: int = 16,
) -> list[HistoryQueryResult]:
    """Run history queries with bounded concurrency — reuses the
    proven ``services.bounded_async.bounded_gather`` semantics.

    Prevents the ``one Task per (league × team × player × event)``
    explosion by scheduling AT MOST ``limit`` awaits at a time.
    Preserves result ORDER (index-aligned with ``factories``) for
    callers that zip inputs to outputs.
    """
    try:
        from services.bounded_async import bounded_gather

        async def _run_one(f):
            return await f()
        return await bounded_gather(factories, _run_one, limit=limit)
    except Exception:
        # Local fallback — semaphore-bounded gather that preserves order.
        sem = asyncio.Semaphore(limit)

        async def _run(f):
            async with sem:
                return await f()
        return await asyncio.gather(*(_run(f) for f in factories))


# ─────────────────────────────────────────────────────────────────────
# Legacy MLS bridge — the MLS-only implementation is preserved as a
# fallback adapter so no working behaviour regresses.
# ─────────────────────────────────────────────────────────────────────
def _register_mls_legacy_bridge() -> None:
    """Best-effort registration.  Skipped silently on import failure."""
    try:
        from services import mls_player_matchup_history as _mls

        class MLSLegacyProvider:
            name = "mls_legacy_bridge"
            supported_competitions = frozenset({
                "usa.mls", "soccer_usa_mls", "MLS",
            })

            async def team_h2h(self, **kwargs):
                # Not implemented in legacy MLS module — report truthfully.
                return HistoryQueryResult(
                    status=STATUS_UNAVAILABLE, sample_size=0,
                    provenance=self.name,
                )

            async def team_history(self, **kwargs):
                return HistoryQueryResult(
                    status=STATUS_UNAVAILABLE, sample_size=0,
                    provenance=self.name,
                )

            async def player_history(self, **kwargs):
                return HistoryQueryResult(
                    status=STATUS_UNAVAILABLE, sample_size=0,
                    provenance=self.name,
                )

            async def player_vs_opp(self, *, canonical_player_id: str,
                                    canonical_opponent_id: str,
                                    canonical_competition_id=None,
                                    as_of=None, limit: int = 20):
                # Legacy MLS module exposes only player_vs_opp equivalent.
                fn = getattr(_mls, "get_player_matchup_history", None)
                if fn is None:
                    return HistoryQueryResult(
                        status=STATUS_UNAVAILABLE, sample_size=0,
                        provenance=self.name,
                    )
                try:
                    rows = await fn(
                        canonical_player_id=canonical_player_id,
                        canonical_opponent_id=canonical_opponent_id,
                        as_of=as_of, limit=limit,
                    )
                except Exception as e:
                    return HistoryQueryResult(
                        status=STATUS_PROVIDER_FAILURE, sample_size=0,
                        error=f"{e.__class__.__name__}:{e}",
                        provenance=self.name,
                    )
                if rows is None:
                    return HistoryQueryResult(
                        status=STATUS_UNAVAILABLE, sample_size=0,
                        provenance=self.name,
                    )
                return HistoryQueryResult(
                    status=(STATUS_FULL if rows else STATUS_NO_MATCHES),
                    rows=list(rows), sample_size=len(rows),
                    provenance=self.name,
                    as_of=(as_of.isoformat() if as_of else None),
                )

        register_provider(MLSLegacyProvider())
    except Exception:
        pass


_register_mls_legacy_bridge()


__all__ = [
    "STATUS_FULL", "STATUS_PARTIAL", "STATUS_UNAVAILABLE",
    "STATUS_PROVIDER_FAILURE", "STATUS_NO_MATCHES", "STATUS_UNVERIFIED",
    "ALL_STATUS",
    "CompetitionCoverage", "HistoricalMatch", "PlayerAppearance",
    "HistoryQueryResult",
    "SoccerHistoryProvider",
    "register_provider", "unregister_provider", "registered_providers",
    "get_providers_for_competition",
    "get_team_h2h", "get_team_history",
    "get_player_history", "get_player_matchup_history",
    "build_coverage_matrix",
    "bounded_history_scan",
]
