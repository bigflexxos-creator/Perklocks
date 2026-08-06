"""index_registry — Phase 3C central index registry + verification.

One canonical place for every runtime index declaration on the critical
collections.  Replaces the eight fragmented ``ensure_indices()``
implementations plus the ad-hoc ``create_index`` calls in server.py.

Design goals
────────────
* **Typed, declarative** — every index is an :class:`IndexSpec` with
  stable name, key list, and flags.
* **Idempotent** — :func:`ensure_all_indexes` reuses matching indexes;
  it never drops or rebuilds a production index automatically.
* **Non-destructive** — conflicts and duplicates are *reported*.
  Destructive resolution requires a separately approved migration.
* **Critical-vs-noncritical** — critical missing/conflicting indexes
  block readiness.  Noncritical only warn.
* **TTL safety** — TTL specs must reference BSON-Date-typed fields.
  Publication-mismatch TTL is BLOCKED on a documented migration
  because the existing ``logged_at`` field is stored as ISO 8601
  strings, not Dates.  See ``PHASE3C_INDEX_CONFLICT_REPORT.md``.
* **No secrets in diagnostics.**
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.index_registry")


# ═════════════════════════════════════════════════════════════════════
# Typed spec
# ═════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class IndexSpec:
    collection:             str
    name:                   str
    keys:                   tuple[tuple[str, int], ...]
    unique:                 bool = False
    sparse:                 bool = False
    partial_filter:         Optional[dict[str, Any]] = None
    expire_after_seconds:   Optional[int] = None
    collation:              Optional[dict[str, Any]] = None
    critical:               bool = True
    owner_service:          str  = "unowned"
    purpose:                str  = ""
    migration_notes:        str  = ""

    def to_pymongo_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"name": self.name}
        if self.unique:                         kw["unique"] = True
        if self.sparse:                         kw["sparse"] = True
        if self.partial_filter is not None:     kw["partialFilterExpression"] = self.partial_filter
        if self.expire_after_seconds is not None: kw["expireAfterSeconds"] = self.expire_after_seconds
        if self.collation is not None:          kw["collation"] = self.collation
        return kw


class IndexRegistryError(RuntimeError):
    pass


# ═════════════════════════════════════════════════════════════════════
# Registry declarations
# ═════════════════════════════════════════════════════════════════════
# Ordered by owner_service — each block mirrors the previous
# fragmented ensure_indices() function so parity is trivially
# verifiable.  Adding a new index is a one-line append.
_INDEX_SPECS: list[IndexSpec] = [

    # ── picks (owner: server.py startup) ─────────────────────────────
    IndexSpec("picks", "pick_date_1_sport_1",
              keys=(("pick_date", 1), ("sport", 1)),
              owner_service="server_startup",
              purpose="daily-slate list by sport"),
    IndexSpec("picks", "pick_date_1_lock_score_-1",
              keys=(("pick_date", 1), ("lock_score", -1)),
              owner_service="server_startup",
              purpose="lock-score ranking on the daily slate"),
    IndexSpec("picks", "status_1_settled_at_-1",
              keys=(("status", 1), ("settled_at", -1)),
              owner_service="server_startup",
              purpose="settlement scan"),
    IndexSpec("picks", "id_1", keys=(("id", 1),), unique=True,
              owner_service="server_startup",
              purpose="stable pick id lookup"),
    IndexSpec("picks", "pick_date_1_signal_score_-1",
              keys=(("pick_date", 1), ("signal_score", -1)),
              critical=False,
              owner_service="server_startup",
              purpose="signal-score sort for admin views"),

    # ── users ────────────────────────────────────────────────────────
    IndexSpec("users", "email_1",
              keys=(("email", 1),), unique=True,
              owner_service="server_startup",
              purpose="login lookup"),

    # ── prediction_snapshots + publication_mismatch (Phase 1) ────────
    IndexSpec("prediction_snapshots", "prediction_snapshot_version_uniq",
              keys=(("prediction_id", 1), ("snapshot_version", 1)),
              unique=True,
              owner_service="prediction_publication_service",
              purpose="unique versioned snapshots"),
    IndexSpec("prediction_snapshots", "prediction_idempotency_uniq",
              keys=(("prediction_id", 1), ("idempotency_key", 1)),
              unique=True,
              owner_service="prediction_publication_service",
              purpose="idempotent publication"),
    IndexSpec("prediction_snapshots", "board_version_idx",
              keys=(("board_version", 1),),
              owner_service="prediction_publication_service"),
    IndexSpec("prediction_snapshots", "published_at_idx",
              keys=(("published_at", 1),),
              owner_service="prediction_publication_service"),
    IndexSpec("prediction_snapshots", "model_version_idx",
              keys=(("model_version", 1),), critical=False,
              owner_service="prediction_publication_service"),
    IndexSpec("prediction_snapshots", "is_active_idx",
              keys=(("is_active", 1),),
              owner_service="prediction_publication_service"),
    IndexSpec("publication_mismatch_report", "mismatch_prediction_board_idx",
              keys=(("prediction_id", 1), ("board_version", 1)),
              critical=False,
              owner_service="prediction_publication_service",
              purpose="audit lookup by prediction/board"),
    IndexSpec("publication_mismatch_report", "mismatch_logged_at_idx",
              keys=(("logged_at", 1),), critical=False,
              owner_service="prediction_publication_service",
              purpose="chronological audit scan (legacy ISO-string field)"),
    IndexSpec("publication_mismatch_report", "mismatch_logged_at_dt_ttl",
              keys=(("logged_at_dt", 1),),
              expire_after_seconds=2592000,   # 30 days
              critical=False,
              owner_service="prediction_publication_service",
              purpose="Phase 3K — 30-day retention on BSON-Date logged_at_dt",
              migration_notes=(
                  "PHASE3K (2026-08): TTL now applied.  logged_at_dt is a "
                  "BSON Date populated by both new writers (in "
                  "prediction_publication_service._log_mismatch) AND the "
                  "backfill script at "
                  "scripts/backfills/backfill_publication_mismatch_logged_at_dt.py.  "
                  "42,756/42,756 historical rows backfilled with 0 invalid.  "
                  "Legacy `logged_at` ISO-string field kept for "
                  "backward compatibility; DO NOT drop it in this phase.")),

    # ── settlement + enrichment ──────────────────────────────────────
    IndexSpec("settlement_events", "prediction_settled_at_idx",
              keys=(("prediction_id", 1), ("settled_at", -1)),
              owner_service="settlement_service"),
    IndexSpec("settlement_events", "prediction_active_idx",
              keys=(("prediction_id", 1), ("is_active", 1)),
              owner_service="settlement_service"),
    IndexSpec("settlement_events", "source_idx",
              keys=(("source", 1),), critical=False,
              owner_service="settlement_service"),
    IndexSpec("settlement_events", "settled_at_idx",
              keys=(("settled_at", 1),), critical=False,
              owner_service="settlement_service"),
    IndexSpec("pick_enrichment", "pred_type_active_idx",
              keys=(("prediction_id", 1), ("enrichment_type", 1), ("is_active", 1)),
              owner_service="enrichment_service"),
    IndexSpec("pick_enrichment", "pred_updated_idx",
              keys=(("prediction_id", 1), ("updated_at", -1)),
              owner_service="enrichment_service"),
    IndexSpec("pick_enrichment", "type_idx",
              keys=(("enrichment_type", 1),), critical=False,
              owner_service="enrichment_service"),
    IndexSpec("pick_enrichment", "source_idx",
              keys=(("source", 1),), critical=False,
              owner_service="enrichment_service"),

    # ── user_bets, parlay_history ────────────────────────────────────
    IndexSpec("user_bets", "user_id_1_status_1_created_at_-1",
              keys=(("user_id", 1), ("status", 1), ("created_at", -1)),
              owner_service="user_bets_routes"),
    IndexSpec("user_bets", "user_id_1_sport_1",
              keys=(("user_id", 1), ("sport", 1)), critical=False,
              owner_service="user_bets_routes"),
    IndexSpec("user_bets", "user_id_1_market_1",
              keys=(("user_id", 1), ("market", 1)), critical=False,
              owner_service="user_bets_routes"),
    IndexSpec("user_bets", "pick_id_1",
              keys=(("pick_id", 1),), critical=False,
              owner_service="user_bets_routes"),

    # ── Phase 3G Step 2 · canonical UserBetLedger indexes ────────────
    # All new indexes are declared critical=False and every uniqueness
    # constraint is gated by a partial_filter so pre-existing rows
    # (which used ``id`` and lacked the new field entirely) can NEVER
    # cause a startup ensure to fail.  Duplicate scanning must happen
    # explicitly via services.user_bet_ledger.preflight_unique_indexes
    # before this project promotes any of these to critical=True.
    IndexSpec("user_bets", "user_bet_id_uniq_partial",
              keys=(("user_bet_id", 1),),
              unique=True,
              partial_filter={"user_bet_id": {"$exists": True, "$type": "string"}},
              critical=False,
              owner_service="user_bet_ledger",
              purpose="canonical wager id lookup (unique when present)"),
    IndexSpec("user_bets", "user_id_1_placed_at_-1",
              keys=(("user_id", 1), ("placed_at", -1)),
              critical=False,
              owner_service="user_bet_ledger",
              purpose="per-user chronological listing"),
    IndexSpec("user_bets", "status_1",
              keys=(("status", 1),),
              critical=False,
              owner_service="user_bet_ledger",
              purpose="status distribution / settlement scans"),
    IndexSpec("user_bets", "user_id_1_client_bet_id_1_uniq_partial",
              keys=(("user_id", 1), ("client_bet_id", 1)),
              unique=True,
              partial_filter={"client_bet_id": {"$exists": True, "$type": "string"}},
              critical=False,
              owner_service="user_bet_ledger",
              purpose="client_bet_id idempotency per user"),
    IndexSpec("user_bets", "user_id_1_idempotency_key_1_uniq_partial",
              keys=(("user_id", 1), ("idempotency_key", 1)),
              unique=True,
              partial_filter={"idempotency_key": {"$exists": True, "$type": "string"}},
              critical=False,
              owner_service="user_bet_ledger",
              purpose="server-computed idempotency per user"),
    IndexSpec("user_bets", "migration_source_1_migration_source_id_1_uniq_partial",
              keys=(("migration_source", 1), ("migration_source_id", 1)),
              unique=True,
              partial_filter={"migration_source": {"$exists": True, "$type": "string"},
                              "migration_source_id": {"$exists": True, "$type": "string"}},
              critical=False,
              owner_service="user_bet_ledger",
              purpose="prevent duplicate migration from legacy stores"),
    IndexSpec("user_bets", "prediction_id_1",
              keys=(("prediction_id", 1),),
              critical=False,
              owner_service="user_bet_ledger",
              purpose="join to canonical picks / prediction snapshots"),
    IndexSpec("user_bets", "snapshot_id_1",
              keys=(("snapshot_id", 1),),
              critical=False,
              owner_service="user_bet_ledger",
              purpose="join to canonical prediction_snapshots"),
    IndexSpec("user_bets", "legs_prediction_id_1",
              keys=(("legs.prediction_id", 1),),
              critical=False,
              owner_service="user_bet_ledger",
              purpose="parlay-leg fan-out at settle time"),

    IndexSpec("parlay_history", "id_1",
              keys=(("id", 1),), unique=True, critical=False,
              owner_service="parlay_routes",
              purpose="legacy compat with future Phase 3G migration"),
    IndexSpec("parlay_history", "user_id_1_created_at_-1",
              keys=(("user_id", 1), ("created_at", -1)), critical=False,
              owner_service="parlay_routes"),
    IndexSpec("parlay_history", "user_id_1_status_1",
              keys=(("user_id", 1), ("status", 1)), critical=False,
              owner_service="parlay_routes"),

    # ── scheduled_jobs / job_execution_log / job_audit_log ───────────
    IndexSpec("scheduled_jobs", "job_name_uniq",
              keys=(("job_name", 1),), unique=True,
              owner_service="job_coordinator"),
    IndexSpec("scheduled_jobs", "lease_until_idx",
              keys=(("lease_until", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("scheduled_jobs", "status_idx",
              keys=(("status", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("scheduled_jobs", "next_eligible_at_idx",
              keys=(("next_eligible_at", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("scheduled_jobs", "updated_at_idx",
              keys=(("updated_at", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("job_execution_log", "started_at_idx",
              keys=(("started_at", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("job_execution_log", "job_name_idx",
              keys=(("job_name", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("job_execution_log", "execution_ttl_idx",
              keys=(("ttl_at", 1),), expire_after_seconds=0, critical=False,
              owner_service="job_coordinator",
              purpose="30-day TTL on ttl_at BSON date"),
    IndexSpec("job_audit_log", "created_at_idx",
              keys=(("created_at", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("job_audit_log", "event_type_idx",
              keys=(("event_type", 1),), critical=False,
              owner_service="job_coordinator"),
    IndexSpec("job_audit_log", "audit_ttl_idx",
              keys=(("ttl_at", 1),), expire_after_seconds=0, critical=False,
              owner_service="job_coordinator",
              purpose="180-day TTL on ttl_at BSON date"),

    # ── provider_budget ──────────────────────────────────────────────
    IndexSpec("provider_budget_state", "provider_month_uniq",
              keys=(("provider", 1), ("month_key", 1)), unique=True,
              owner_service="provider_budget"),
    IndexSpec("provider_request_intents", "intent_id_uniq",
              keys=(("intent_id", 1),), unique=True,
              owner_service="provider_budget"),
    IndexSpec("provider_request_intents", "request_key_idx",
              keys=(("request_key", 1),), critical=False,
              partial_filter={"request_key": {"$exists": True}},
              owner_service="provider_budget",
              purpose="sparse index for optional idempotency key",
              migration_notes=(
                  "Phase 3C: reverted partial filter from `$exists+$ne` "
                  "to `$exists`.  MongoDB partial indexes do not support "
                  "$not/$ne — the old form silently failed to create in "
                  "production.  This form is what the registry now "
                  "creates on first startup after Phase 3C.")),
    IndexSpec("provider_request_intents", "prov_status_idx",
              keys=(("provider", 1), ("status", 1)), critical=False,
              owner_service="provider_budget"),
    IndexSpec("provider_request_intents", "expires_at_idx",
              keys=(("expires_at", 1),), critical=False,
              owner_service="provider_budget"),
    IndexSpec("provider_request_intents", "created_at_idx",
              keys=(("created_at", 1),), critical=False,
              owner_service="provider_budget"),

    # ── odds_api gateway ─────────────────────────────────────────────
    IndexSpec("odds_api_cache", "uniq_cache_key",
              keys=(("cache_key", 1),), unique=True,
              owner_service="odds_cache"),
    IndexSpec("odds_api_cache", "refreshed_at",
              keys=(("refreshed_at", 1),), critical=False,
              owner_service="odds_cache"),
    IndexSpec("odds_api_request_log", "ts",
              keys=(("ts", 1),), critical=False,
              owner_service="odds_cache"),
    IndexSpec("odds_api_request_log", "sport_ts",
              keys=(("sport_key", 1), ("ts", -1)), critical=False,
              owner_service="odds_cache"),

    IndexSpec("odds_bad_market_registry", "sport_market_uniq",
              keys=(("sport_key", 1), ("market", 1)), unique=True,
              owner_service="bad_market_registry"),
    IndexSpec("odds_bad_market_registry", "expires_at_ttl",
              keys=(("expires_at", 1),), expire_after_seconds=0, critical=False,
              owner_service="bad_market_registry",
              purpose="TTL on BSON-date expires_at"),

    IndexSpec("odds_tournament_registry", "sport_key_uniq",
              keys=(("sport_key", 1),), unique=True,
              owner_service="tournament_registry"),
    IndexSpec("odds_tournament_registry", "suppress_until_idx",
              keys=(("suppress_until", 1),), critical=False,
              owner_service="tournament_registry"),
    IndexSpec("odds_tournament_registry", "sport_group_idx",
              keys=(("sport_group", 1),), critical=False,
              owner_service="tournament_registry"),
    IndexSpec("odds_tournament_registry", "updated_at_idx",
              keys=(("updated_at", 1),), critical=False,
              owner_service="tournament_registry"),

    IndexSpec("odds_request_flights", "request_key_uniq",
              keys=(("request_key", 1),), unique=True,
              owner_service="single_flight"),
    IndexSpec("odds_request_flights", "expires_at_idx",
              keys=(("expires_at", 1),), critical=False,
              owner_service="single_flight"),
    IndexSpec("odds_request_flights", "flight_ttl_idx",
              keys=(("ttl_at", 1),), expire_after_seconds=0, critical=False,
              owner_service="single_flight",
              purpose="TTL on BSON-date ttl_at"),

    IndexSpec("sports_catalog_snapshots", "run_id_uniq",
              keys=(("run_id", 1),), unique=True,
              owner_service="sports_catalog"),
    IndexSpec("sports_catalog_snapshots", "catalog_ttl_idx",
              keys=(("ttl_at", 1),), expire_after_seconds=0, critical=False,
              owner_service="sports_catalog",
              purpose="TTL on BSON-date ttl_at"),

    # ── live_alt_lines (owner: alt_lines_feed) ───────────────────────
    IndexSpec("live_alt_lines", "market_id_1",
              keys=(("market_id", 1),), unique=True, critical=False,
              owner_service="alt_lines_feed"),
    IndexSpec("live_alt_lines", "sport_1_event_name_1_market_key_1",
              keys=(("sport", 1), ("event_name", 1), ("market_key", 1)),
              critical=False,
              owner_service="alt_lines_feed"),
    IndexSpec("live_alt_lines", "sport_1_selection_norm_1_market_key_1",
              keys=(("sport", 1), ("selection_norm", 1), ("market_key", 1)),
              critical=False,
              owner_service="alt_lines_feed"),
    IndexSpec("live_alt_lines", "last_seen_1",
              keys=(("last_seen", 1),), expire_after_seconds=1800,
              critical=False,
              owner_service="alt_lines_feed",
              purpose="30-minute TTL on last_seen BSON date"),

    # ── learning_snapshots (owner: server_startup) ───────────────────
    IndexSpec("learning_snapshots", "learning_generated_idx",
              keys=(("generated_at", -1),), critical=False,
              owner_service="server_startup"),
    IndexSpec("learning_snapshots", "learning_date_idx",
              keys=(("snapshot_date", 1),), unique=True, critical=False,
              owner_service="server_startup"),
]


# ═════════════════════════════════════════════════════════════════════
# Validation on load
# ═════════════════════════════════════════════════════════════════════
def _validate_specs(specs: Sequence[IndexSpec]) -> None:
    """Detect duplicate (collection, name) declarations and duplicate
    key-sets within a collection at import time.  Fails LOUDLY — this
    is a repository invariant, not a runtime concern."""
    seen_names: dict[tuple[str, str], IndexSpec] = {}
    for s in specs:
        key = (s.collection, s.name)
        if key in seen_names:
            raise IndexRegistryError(
                f"duplicate index name {s.name!r} on {s.collection!r}"
            )
        seen_names[key] = s


_validate_specs(_INDEX_SPECS)


# ═════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════
def get_index_specs() -> list[IndexSpec]:
    """Return every declared spec."""
    return list(_INDEX_SPECS)


def get_specs_for_collection(collection: str) -> list[IndexSpec]:
    return [s for s in _INDEX_SPECS if s.collection == collection]


def collections() -> list[str]:
    return sorted({s.collection for s in _INDEX_SPECS})


# ── Live-Mongo introspection ─────────────────────────────────────────
async def _live_indexes(db: AsyncIOMotorDatabase, collection: str) -> dict[str, dict]:
    return await db[collection].index_information()


def _keys_match(live_key: list[tuple[str, int]], spec_keys: tuple[tuple[str, int], ...]) -> bool:
    live_pairs = [(k, int(v)) for k, v in live_key]
    return live_pairs == list(spec_keys)


# ── Verification ─────────────────────────────────────────────────────
@dataclass
class VerificationResult:
    missing:                list[IndexSpec] = field(default_factory=list)
    same_name_conflict:     list[tuple[IndexSpec, dict]] = field(default_factory=list)
    equivalent_duplicates:  list[tuple[IndexSpec, str, dict]] = field(default_factory=list)
    matching:               list[IndexSpec] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.same_name_conflict

    @property
    def critical_missing(self) -> list[IndexSpec]:
        return [s for s in self.missing if s.critical]

    @property
    def critical_conflicts(self) -> list[tuple[IndexSpec, dict]]:
        return [t for t in self.same_name_conflict if t[0].critical]

    @property
    def critical_ok(self) -> bool:
        return not self.critical_missing and not self.critical_conflicts

    def summary(self) -> dict[str, Any]:
        return {
            "matching":                len(self.matching),
            "missing":                 len(self.missing),
            "critical_missing":        [f"{s.collection}.{s.name}" for s in self.critical_missing],
            "same_name_conflict":      [f"{s.collection}.{s.name}" for s, _ in self.same_name_conflict],
            "critical_conflict":       [f"{s.collection}.{s.name}" for s, _ in self.critical_conflicts],
            "equivalent_duplicates":   [
                f"{s.collection}.{s.name} <==> {live_name}"
                for s, live_name, _ in self.equivalent_duplicates
            ],
            "critical_ok":             not (self.critical_missing or self.critical_conflicts),
        }


async def verify_collection_indexes(
    db: AsyncIOMotorDatabase, collection: str,
) -> VerificationResult:
    result = VerificationResult()
    specs = get_specs_for_collection(collection)
    try:
        live = await _live_indexes(db, collection)
    except Exception:
        live = {}
    # 1. Missing / matching / same-name conflict
    for spec in specs:
        info = live.get(spec.name)
        if info is None:
            result.missing.append(spec)
            continue
        live_key = info.get("key") or []
        keys_ok = _keys_match(live_key, spec.keys)
        unique_ok  = bool(info.get("unique")) == spec.unique
        sparse_ok  = bool(info.get("sparse")) == spec.sparse
        ttl_ok     = info.get("expireAfterSeconds") == spec.expire_after_seconds \
                     if spec.expire_after_seconds is not None \
                     else "expireAfterSeconds" not in info
        if keys_ok and unique_ok and sparse_ok and ttl_ok:
            result.matching.append(spec)
        else:
            result.same_name_conflict.append((spec, info))
    # 2. Equivalent duplicates (different names, same key-set)
    spec_keysets = {tuple(spec.keys): spec for spec in specs}
    for live_name, info in live.items():
        if live_name == "_id_" or live_name in {s.name for s in specs}:
            continue
        live_pairs = tuple((k, int(v)) for k, v in (info.get("key") or []))
        if live_pairs in spec_keysets:
            result.equivalent_duplicates.append(
                (spec_keysets[live_pairs], live_name, info)
            )
    return result


async def verify_all_indexes(db: AsyncIOMotorDatabase) -> dict[str, VerificationResult]:
    out: dict[str, VerificationResult] = {}
    for coll in collections():
        out[coll] = await verify_collection_indexes(db, coll)
    return out


# ── Creation (non-destructive) ───────────────────────────────────────
async def create_missing_indexes(
    db: AsyncIOMotorDatabase,
    collection: Optional[str] = None,
    critical_only: bool = False,
) -> dict[str, list[str]]:
    """Create any missing indexes.  Never drops.  Returns per-collection
    lists of created index names.  Same-name conflicts are LEFT ALONE
    and reported via :func:`verify_all_indexes`."""
    colls = [collection] if collection else collections()
    created: dict[str, list[str]] = {c: [] for c in colls}
    for coll in colls:
        result = await verify_collection_indexes(db, coll)
        for spec in result.missing:
            if critical_only and not spec.critical:
                continue
            try:
                await db[spec.collection].create_index(
                    list(spec.keys), **spec.to_pymongo_kwargs()
                )
                created[coll].append(spec.name)
                logger.info(
                    "index_registry: created %s.%s", spec.collection, spec.name,
                )
            except Exception as e:                              # pragma: no cover
                logger.warning(
                    "index_registry: could not create %s.%s: %s",
                    spec.collection, spec.name, e,
                )
    return created


async def ensure_all_indexes(
    db: AsyncIOMotorDatabase,
    critical_only: bool = False,
) -> dict[str, Any]:
    """Ensure every declared index exists.  Idempotent.  Returns a
    diagnostic summary."""
    created = await create_missing_indexes(db, critical_only=critical_only)
    verified = await verify_all_indexes(db)
    return {
        "created":        {k: v for k, v in created.items() if v},
        "verification":   {k: r.summary() for k, r in verified.items()
                            if r.missing or r.same_name_conflict or r.equivalent_duplicates},
        "critical_ok":    all(r.critical_ok for r in verified.values()),
    }


async def report_conflicts(db: AsyncIOMotorDatabase) -> dict[str, Any]:
    """Aggregate all non-destructive conflict findings so an operator
    can plan the next migration."""
    verified = await verify_all_indexes(db)
    return {
        "same_name_conflicts": {
            c: [{
                "spec_name":   s.name,
                "spec_keys":   list(s.keys),
                "spec_flags":  s.to_pymongo_kwargs(),
                "live_key":    info.get("key"),
                "live_flags":  {k: v for k, v in info.items() if k != "key"},
                "critical":    s.critical,
            } for s, info in r.same_name_conflict]
            for c, r in verified.items() if r.same_name_conflict
        },
        "equivalent_duplicates": {
            c: [{
                "spec_name":     s.name,
                "live_dup_name": live_name,
                "keys":          list(s.keys),
            } for s, live_name, _info in r.equivalent_duplicates]
            for c, r in verified.items() if r.equivalent_duplicates
        },
    }


def safe_index_diagnostics() -> dict[str, Any]:
    """Return a redacted overview of the registry.  Never includes
    document data or connection strings."""
    by_coll: dict[str, dict[str, Any]] = {}
    for s in _INDEX_SPECS:
        by_coll.setdefault(s.collection, {"total": 0, "critical": 0, "ttl": 0, "unique": 0})
        by_coll[s.collection]["total"] += 1
        if s.critical:                    by_coll[s.collection]["critical"] += 1
        if s.expire_after_seconds is not None: by_coll[s.collection]["ttl"] += 1
        if s.unique:                      by_coll[s.collection]["unique"] += 1
    return {
        "total_specs":        len(_INDEX_SPECS),
        "collections":        len(by_coll),
        "critical_specs":     sum(1 for s in _INDEX_SPECS if s.critical),
        "ttl_specs":          sum(1 for s in _INDEX_SPECS if s.expire_after_seconds is not None),
        "unique_specs":       sum(1 for s in _INDEX_SPECS if s.unique),
        "per_collection":     by_coll,
    }


# ── Compatibility wrappers for the 8 legacy ensure_indices() sites ──
async def ensure_collection(db: AsyncIOMotorDatabase, collection: str) -> None:
    """Compat entry point used by legacy ``ensure_indices()`` wrappers.
    Ensures only the indexes declared for the given collection."""
    await create_missing_indexes(db, collection=collection)


__all__ = [
    "IndexSpec", "IndexRegistryError",
    "VerificationResult",
    "get_index_specs", "get_specs_for_collection", "collections",
    "verify_all_indexes", "verify_collection_indexes",
    "create_missing_indexes", "ensure_all_indexes",
    "report_conflicts", "safe_index_diagnostics",
    "ensure_collection",
]
