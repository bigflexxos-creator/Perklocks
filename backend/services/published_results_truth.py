"""P0.5 (2026-08) — Published Results Truth Service.

Single canonical source of "what was actually published to users AND
what the verified result became". Every History / Analytics / Lab
consumer that needs the published-record population MUST use this
service so we cannot have two systems reconstructing different truths.

Design rules (from P0.5 spec):

  §1  Once a pick was actually published to users, that publication
      record is permanent. History cannot lose it because the pick
      later lost, became off_board, or the current Lock threshold
      changed.
  §3  Deduplication NEVER uses the result. If two db.picks rows
      represent the same canonical publication (same pick_id in
      prediction_snapshots), the WON/LOST outcome must not decide
      which survives.
  §4  publication-time frozen values (`published_lock_score`,
      `published_line`, `published_odds`, `publication_source`,
      `published_grade`, `published_at`, `board_version`,
      `model_version`, ...) are the historical authority.
  §5  Current `off_board=true` MUST NOT erase historical publication.
  §6  Legacy `lock_score >= 89` requalification MUST NOT be used to
      decide historical visibility.
  §7  History and Analytics MUST consume the same population and the
      same canonical query.
  §8  WON / LOST / PUSH / VOID / UNRESOLVED are all first-class
      visible states.
  §9  A "board sweep" is valid only when the entire canonical
      published day has been verified with wins > 0 and
      losses == unresolved == 0.
  §13 Missing CLV MUST remain None — never fabricated as 0.
  §17 Original publication snapshot is NEVER rewritten; corrections
      attach settlement/reconciliation truth.

Public surface:

    class PublishedResultsTruthService:
        canonical_query(days=30, ...) → CanonicalPublishedQuery
        load_published(days=30, ...) → list[PublishedRecord]
        summarise(records)           → PublishedSummary
        classify_publication(pick)   → str  # PROVEN_PUBLISHED |
                                            # PROVEN_NOT_PUBLISHED |
                                            # AMBIGUOUS_LEGACY

Helpers:

    stable_publication_dedupe(records) → list[PublishedRecord]
    verify_sweep(records)              → dict
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


CANONICAL_STATES = ("won", "lost", "push", "void", "unresolved")


# ── Publication classification ─────────────────────────────────
def classify_publication(pick: dict) -> str:
    """Classify a db.picks row as one of:

      PROVEN_PUBLISHED       — has a canonical prediction_snapshots
                               entry OR a board-stamp
                               (`on_*_board_at` / `on_rollover_at` /
                               `on_parlay_at`) OR
                               `publication_source` is set.
      PROVEN_NOT_PUBLISHED   — `no_bet=True`, `hide_from_main_board=True`,
                               `excluded_from_history=True`.
      AMBIGUOUS_LEGACY       — everything else (typically pre-fence
                               rows with no board-stamp yet a plausible
                               lock/grade).  MUST NOT silently enter
                               the official published performance
                               dataset.
    """
    if pick.get("no_bet") is True:
        return "PROVEN_NOT_PUBLISHED"
    if pick.get("hide_from_main_board") is True:
        return "PROVEN_NOT_PUBLISHED"
    if pick.get("excluded_from_history") is True:
        return "PROVEN_NOT_PUBLISHED"
    # Any board-stamp is proof the pick was surfaced.
    for k in ("on_main_board_at", "on_rollover_at", "on_hr_board_at",
              "on_under_at", "on_atd_board_at", "on_parlay_at"):
        if pick.get(k):
            return "PROVEN_PUBLISHED"
    if pick.get("publication_source") or pick.get("published_at"):
        return "PROVEN_PUBLISHED"
    if pick.get("elite_pitcher_override") is True:
        return "PROVEN_PUBLISHED"
    if pick.get("_has_prediction_snapshot") is True:
        return "PROVEN_PUBLISHED"
    return "AMBIGUOUS_LEGACY"


# ── Stable, outcome-neutral dedupe (spec §3) ───────────────────
def _identity_key(pick: dict) -> tuple:
    """Universal semantic wager identity — 2026-08-23 PASS 1 closure.

    Prioritises CANONICAL participant/event/market identity over
    producer/pick IDs so Tjen-style display-name aliases never split
    one semantic wager into multiple published truth rows.
    """
    try:
        from services.pick_identity_enricher import canonical_wager_identity
        canon = canonical_wager_identity(pick)
        if canon[0] and canon[1] and canon[2]:
            return canon
    except Exception:
        pass
    # Legacy fallback (producer-ID precedence) — kept ONLY for rows
    # lacking canonical enrichment.  Do NOT rely on this for new code.
    pub_id = (pick.get("published_pick_id")
               or pick.get("canonical_publication_id"))
    if pub_id:
        return ("pub", pub_id)
    pid = pick.get("id") or pick.get("pick_id")
    if pid:
        return ("pid", pid)
    return (
        "identity",
        pick.get("provider_event_id"),
        pick.get("canonical_player_id"),
        (pick.get("market") or "").lower(),
        pick.get("line") or (pick.get("settlement_detail") or {}).get("line"),
        pick.get("side"),
    )


def stable_publication_dedupe(records: list[dict]) -> list[dict]:
    """Deduplicate technical duplicates by canonical identity.

    IMPORTANT: outcome is NEVER used to choose which duplicate
    survives.  When two rows share an identity, we prefer the row
    with the most-recently populated ``published_at`` (i.e. the
    freshest publication snapshot), then the row that has a
    canonical prediction_snapshot backing (indicated by the
    injected ``_has_prediction_snapshot`` flag), then the first.
    """
    by_key: dict[tuple, dict] = {}
    for r in records:
        k = _identity_key(r)
        ex = by_key.get(k)
        if ex is None:
            by_key[k] = r
            continue
        # Outcome-neutral tie-break
        new_pub_at = r.get("published_at") or ""
        old_pub_at = ex.get("published_at") or ""
        if new_pub_at > old_pub_at:
            by_key[k] = r
            continue
        if new_pub_at < old_pub_at:
            continue
        new_snap = bool(r.get("_has_prediction_snapshot"))
        old_snap = bool(ex.get("_has_prediction_snapshot"))
        if new_snap and not old_snap:
            by_key[k] = r
    return list(by_key.values())


# ── Canonical query ─────────────────────────────────────────────
def canonical_query(*, days: int, exclude_ambiguous_legacy: bool = True,
                     include_pending: bool = False) -> dict:
    """Return the Mongo query for the canonical published-picks
    population.

    This query is **outcome-agnostic** — it does NOT filter by
    status.  It filters ONLY on publication provenance (spec §5
    and §7).  Callers filter/bucket the results by status client
    side using ``summarise``.

    A pick is included when it has ANY of:
      * a board-visibility stamp (on_*_at)
      * `publication_source` populated
      * `elite_pitcher_override` = True
      * a canonical prediction_snapshot (checked in the loader —
        cannot be expressed with a single-collection Mongo query)

    Explicitly excluded (via `PROVEN_NOT_PUBLISHED` rules):
      * `no_bet=True`
      * `hide_from_main_board=True`
      * `excluded_from_history=True`
    """
    cutoff = (datetime.now(timezone.utc)
               - timedelta(days=days)).isoformat()
    time_field_gate = {"$or": [
        {"settled_at": {"$gte": cutoff}},
        {"event_time": {"$gte": cutoff}},
    ]}
    provenance_gate = {"$or": [
        {"on_main_board_at":    {"$exists": True}},
        {"on_rollover_at":      {"$exists": True}},
        {"on_hr_board_at":      {"$exists": True}},
        {"on_under_at":         {"$exists": True}},
        {"on_atd_board_at":     {"$exists": True}},
        {"on_parlay_at":        {"$exists": True}},
        {"publication_source":  {"$exists": True, "$ne": None}},
        {"published_at":        {"$exists": True, "$ne": None}},
        {"elite_pitcher_override": True},
    ]}
    q: dict = {"$and": [
        time_field_gate,
        provenance_gate,
        {"no_bet":                {"$ne": True}},
        {"hide_from_main_board":  {"$ne": True}},
        {"excluded_from_history": {"$ne": True}},
    ]}
    return q


# ── Loader ──────────────────────────────────────────────────────
async def load_published(db, *, days: int = 30,
                          exclude_ambiguous_legacy: bool = True,
                          include_pending: bool = True) -> list[dict]:
    """Load canonical published records.  Returns a list of dicts
    with publication-time values preserved plus a
    ``_has_prediction_snapshot`` flag injected for downstream dedupe
    tie-breaking and a ``_classification`` field.

    ── History Zero μ-fix (2026-06) — starvation-proof reads ─────
    Prior implementation issued ONE bounded read sorted by
    ``event_time DESC LIMIT 5000``.  In production with thousands
    of newer pending/future canonical picks queued for grading,
    OLDER settled WIN/LOSS/PUSH/VOID rows dropped off the tail of
    the slice and History returned ZERO.

    We now split the read into THREE bounded, mutually-exclusive
    slices — every one starvation-proof against the other:

      1. SETTLED slice (starvation-proof anchor)
         status ∈ {won,lost,push,void,unresolved}
         sort settled_at DESC (fallback event_time), limit 3000

      2. PENDING slice (only if ``include_pending=True``)
         status ∈ {pending, None}
         sort event_time DESC, limit 2000

      3. SNAPSHOT-ADMISSION slice (fixes root cause #2)
         Any prediction_snapshots row inside the window whose
         ``pick_id`` is NOT already present in slices 1-2 and whose
         underlying pick still exists in ``db.picks`` inside the
         time window and is not explicitly excluded is admitted
         with ``_has_prediction_snapshot=True`` so it classifies
         as PROVEN_PUBLISHED even if it lacks the newer
         ``publication_source`` / ``on_*_at`` fields.  Bounded 500.

    Total ceiling: 5500 rows.  No unbounded read.  Canonical
    provenance preserved (all three slices intersect the same
    ``canonical_query`` time + rejection gates).
    """
    q = canonical_query(days=days,
                          exclude_ambiguous_legacy=exclude_ambiguous_legacy,
                          include_pending=include_pending)

    # ── Slice 1: SETTLED (starvation-proof anchor) ────────────────
    settled_q = {"$and": q["$and"] + [
        {"status": {"$in": ["won", "lost", "push", "void", "unresolved"]}},
    ]}
    # Prefer settled_at ordering when present (canonical) so the
    # newest graded rows lead; if a legacy row lacks settled_at
    # Mongo treats missing as smallest and it falls to the tail —
    # still inside the 3000 ceiling, still not starved by pending.
    settled = await db.picks.find(settled_q, {"_id": 0}).sort(
        [("settled_at", -1), ("event_time", -1)]).limit(3000).to_list(
        length=3000)
    settled_ids = {p.get("id") for p in settled if p.get("id")}

    # ── Slice 2: PENDING (only if requested) ──────────────────────
    pending: list[dict] = []
    if include_pending:
        pending_q = {"$and": q["$and"] + [
            {"$or": [
                {"status": {"$in": [None, "pending"]}},
                {"status": {"$exists": False}},
            ]},
        ]}
        pending = await db.picks.find(pending_q, {"_id": 0}).sort(
            "event_time", -1).limit(2000).to_list(length=2000)
        pending = [p for p in pending if p.get("id") not in settled_ids]

    picks = settled + pending

    # ── Slice 3: SNAPSHOT-ADMISSION (fixes root cause #2) ─────────
    # A legacy pick with a canonical prediction_snapshot inside the
    # window MUST be considered PROVEN_PUBLISHED, even if it lacks
    # the newer publication_source / on_*_at / published_at fields.
    # We admit ONLY snapshot-backed pick_ids that (a) live in
    # db.picks inside the same 30-day window and (b) are not
    # explicitly excluded via no_bet / hide_from_main_board /
    # excluded_from_history.  Canonical provenance preserved.
    cutoff = (datetime.now(timezone.utc)
               - timedelta(days=days)).isoformat()
    existing_ids: set = set()
    for p in picks:
        pid = p.get("id")
        if pid: existing_ids.add(pid)

    snap_admit_ids: set = set()
    try:
        # Sorted DESC by recency so we discover snapshot-backed picks
        # closest to the current window first.  We iterate up to
        # 20000 snapshot rows (bounded scan) but only KEEP pick_ids
        # not already surfaced by slices 1-2, and stop as soon as
        # we've collected 500 admissions (matches the admit_q ceiling).
        snap_cursor = db.prediction_snapshots.find(
            {"$or": [
                {"snapshot_created_at": {"$gte": cutoff}},
                {"created_at":          {"$gte": cutoff}},
                {"published_at":        {"$gte": cutoff}},
            ]},
            {"_id": 0, "pick_id": 1, "prediction_id": 1, "id": 1},
        ).sort([("snapshot_created_at", -1),
                 ("created_at",         -1)]).limit(20000)
        async for r in snap_cursor:
            for k in ("pick_id", "prediction_id", "id"):
                v = r.get(k)
                if v and v not in existing_ids:
                    snap_admit_ids.add(v)
                    if len(snap_admit_ids) >= 500:
                        break
            if len(snap_admit_ids) >= 500:
                break
    except Exception:
        # Snapshot collection missing / errored — degrade to slices 1-2.
        snap_admit_ids = set()

    if snap_admit_ids:
        # Bounded admission — preserve time window + rejection gates.
        admit_q = {
            "id": {"$in": list(snap_admit_ids)[:500]},
            "$or": [
                {"settled_at": {"$gte": cutoff}},
                {"event_time": {"$gte": cutoff}},
            ],
            "no_bet":                {"$ne": True},
            "hide_from_main_board":  {"$ne": True},
            "excluded_from_history": {"$ne": True},
        }
        admitted = await db.picks.find(
            admit_q, {"_id": 0}).limit(500).to_list(length=500)
        # Pre-flag as snapshot-backed so classify_publication returns
        # PROVEN_PUBLISHED (line 92: _has_prediction_snapshot=True).
        for a in admitted:
            a["_has_prediction_snapshot"] = True
        picks.extend(admitted)

    # Hydrate prediction-snapshot presence for the WHOLE population.
    # A single lookup query keeps this O(1) per pick.
    ids = [p["id"] for p in picks if p.get("id")]
    have_snap: set[str] = set(a.get("id") for a in picks
                                if a.get("_has_prediction_snapshot"))
    if ids:
        async for r in db.prediction_snapshots.find(
                {"$or": [{"pick_id":       {"$in": ids}},
                          {"prediction_id": {"$in": ids}},
                          {"id":            {"$in": ids}}]},
                {"_id": 0, "pick_id": 1, "prediction_id": 1, "id": 1}):
            for k in ("pick_id", "prediction_id", "id"):
                v = r.get(k)
                if v: have_snap.add(v)
    for p in picks:
        pid = p.get("id")
        p["_has_prediction_snapshot"] = pid in have_snap
        p["_classification"] = classify_publication(p)

    if exclude_ambiguous_legacy:
        picks = [p for p in picks if p["_classification"] != "AMBIGUOUS_LEGACY"]

    # Stable, outcome-neutral dedupe.
    picks = stable_publication_dedupe(picks)
    return picks


# ── Summary + sweep validator ───────────────────────────────────
def summarise(records: Iterable[dict]) -> dict:
    """Return the canonical performance summary.  Explicitly exposes
    every settlement state (§8).  Never divides by (won+lost) unless
    that's what the caller wants — instead exposes both hit_rate
    (won / decisions) and coverage (decisions / published)."""
    won = lost = push = void = unresolved = pending = 0
    units_risked = 0.0
    units_profit = 0.0
    total = 0
    for r in records:
        total += 1
        s = (r.get("status") or "pending").lower()
        if s == "won": won += 1
        elif s == "lost": lost += 1
        elif s == "push": push += 1
        elif s == "void": void += 1
        elif s == "unresolved": unresolved += 1
        else: pending += 1
        try:
            units_risked += float(r.get("units_risked") or 0)
            units_profit += float(r.get("units_profit") or 0)
        except (TypeError, ValueError):
            pass
    verified_decisions = won + lost + push
    hit_rate_pct = (round(won / (won + lost) * 100, 1)
                     if (won + lost) else None)
    roi_pct = (round(units_profit / units_risked * 100, 2)
                if units_risked else None)
    return {
        "published_total":   total,
        "won":               won,
        "lost":              lost,
        "push":              push,
        "void":              void,
        "unresolved":        unresolved,
        "pending":           pending,
        "verified_decisions": verified_decisions,
        "hit_rate_pct":      hit_rate_pct,
        "units_risked":      round(units_risked, 2),
        "units_profit":      round(units_profit, 2),
        "roi_pct":           roi_pct,
    }


def verify_sweep(records: Iterable[dict]) -> dict:
    """§9 — a sweep is ONLY valid when every qualifying published
    pick is verified, won > 0, and losses == unresolved == 0."""
    rows = list(records)
    s = summarise(rows)
    valid = (
        s["published_total"] > 0
        and s["won"] > 0
        and s["lost"] == 0
        and s["unresolved"] == 0
        and s["pending"] == 0
    )
    reasons: list[str] = []
    if s["published_total"] == 0:
        reasons.append("no_published_picks")
    if s["won"] == 0:
        reasons.append("no_wins")
    if s["lost"] > 0:
        reasons.append(f"has_{s['lost']}_losses")
    if s["unresolved"] > 0:
        reasons.append(f"has_{s['unresolved']}_unresolved")
    if s["pending"] > 0:
        reasons.append(f"has_{s['pending']}_pending")
    return {"is_valid_sweep": valid, "summary": s, "reasons": reasons}


# ── Publication-time projection ────────────────────────────────
def project_publication_time_view(pick: dict) -> dict:
    """Return only the publication-time frozen values (§4/§17).
    Callers must display THESE values in history — NOT current
    mutable fields.  Missing CLV stays None (§13)."""
    return {
        "pick_id":              pick.get("id") or pick.get("pick_id"),
        "published_at":         pick.get("published_at"),
        "sport":                pick.get("sport"),
        "league":               pick.get("league"),
        "event":                pick.get("event"),
        "market":               pick.get("market"),
        "selection":            pick.get("selection")
                                 or pick.get("pick"),
        "published_line":       pick.get("published_line")
                                 or pick.get("line"),
        "published_odds":       pick.get("published_odds")
                                 or pick.get("book_odds")
                                 or pick.get("odds_at_pick"),
        "published_lock_score": pick.get("published_lock_score"),
        "published_grade":      pick.get("published_grade"),
        "publication_source":   pick.get("publication_source"),
        "board_version":        pick.get("board_version"),
        "model_version":        pick.get("model_version"),
        "simulator_version":    pick.get("simulator_version"),
        # settlement state (may be current)
        "status":                    pick.get("status"),
        "settlement_verified":       pick.get("settlement_verified"),
        "settlement_source":         pick.get("settlement_source"),
        "settlement_detail":         pick.get("settlement_detail"),
        "reconciliation_trail":      pick.get("reconciliation_trail"),
        "verification_trail":        pick.get("verification_trail"),
        # CLV: never fabricate zero (§13)
        "closing_odds":     pick.get("closing_odds"),
        "clv_value":        pick.get("clv_value"),
        "clv_verified":     bool(pick.get("closing_odds") is not None),
    }


class PublishedResultsTruthService:
    """Convenience wrapper — a single object History/Analytics can
    depend on."""
    def __init__(self, db):
        self.db = db

    async def load(self, *, days: int = 30,
                    exclude_ambiguous_legacy: bool = True,
                    include_pending: bool = True) -> list[dict]:
        return await load_published(
            self.db, days=days,
            exclude_ambiguous_legacy=exclude_ambiguous_legacy,
            include_pending=include_pending)

    def summarise(self, records) -> dict:
        return summarise(records)

    def verify_sweep(self, records) -> dict:
        return verify_sweep(records)

    def project_publication_time_view(self, pick: dict) -> dict:
        return project_publication_time_view(pick)

    def classify_publication(self, pick: dict) -> str:
        return classify_publication(pick)


__all__ = [
    "CANONICAL_STATES",
    "canonical_query",
    "classify_publication",
    "load_published",
    "project_publication_time_view",
    "PublishedResultsTruthService",
    "stable_publication_dedupe",
    "summarise",
    "verify_sweep",
]
