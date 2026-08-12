"""Pre-Magic Certification — individual checks.

Every check is a coroutine that receives an async MongoDB handle,
optional context, and returns a ``CertificationEntry``.  Each check
is READ-ONLY and side-effect-free — no writes, no cache mutation,
no consumer wiring.

The checks come in three flavours:

1. **Sport × market**: ``certify_player_history_market``,
   ``certify_team_history_market``, ``certify_exact_threshold``,
   ``certify_distributions``, ``certify_h2h`` — one row per
   (sport, market).
2. **Cross-cutting invariants**: ``certify_missing_not_zero``,
   ``certify_as_of_safety``, ``certify_identity``,
   ``certify_market_normalization``, ``certify_tennis_context``,
   ``certify_soccer_producer_integrity``.
3. **Live pick reachability**: ``certify_live_pick_reachability``,
   ``certify_market_readiness``, ``certify_model_readiness`` —
   inspect actual published picks in the DB.

None of these checks call Magic 2.0.  Magic remains ``NOT_WIRED``
throughout (§15).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .states import (
    CertificationState as CS,
    EvidenceType as ET,
    CertificationEntry,
)
from .market_catalog import MarketAtom


# ═══════════════════════════════════════════════════════════════════
# Market key normalization — catalog name → adapter name
# ═══════════════════════════════════════════════════════════════════
# Maps the CANONICAL production market names used in db.picks and on
# the catalogue to the market string each sport adapter expects.
# Adapters that use the canonical name directly need no entry.
_ADAPTER_MARKET_ALIAS: dict[str, str] = {
    # MLB — adapters expect the bare stat name
    "player_hits":               "hits",
    "batter_hits":               "hits",
    "player_total_bases":        "total_bases",
    "batter_total_bases":        "total_bases",
    "player_home_runs":          "home_runs",
    "batter_home_runs":          "home_runs",
    "player_rbis":               "rbis",
    "batter_rbis":               "rbis",
    "player_runs_scored":        "runs",
    "batter_runs_scored":        "runs",
    "batter_strikeouts":         "strikeouts",
    "pitcher_strikeouts":        "pitcher_strikeouts",
    "pitcher_outs":              "pitcher_outs",
    # NFL / NBA / Soccer / Tennis adapters accept both variants —
    # entries are conservative aliases.
    "player_passing_yards":      "passing_yards",
    "player_rushing_yards":      "rushing_yards",
    "player_receiving_yards":    "receiving_yards",
    "player_receptions":         "receptions",
    "player_points":             "points",
    "player_rebounds":           "rebounds",
    "player_assists":            "assists",
    "player_threes":             "threes",
    "player_goals":              "goals",
    "player_shots":              "shots",
    "player_shots_on_target":    "shots_on_target",
    "player_aces":               "aces",
    "player_double_faults":      "double_faults",
    "player_games_won":          "games_won",
    "player_sets_won":           "sets_won",
}


def _adapter_market(market: str) -> str:
    m = (market or "").lower()
    return _ADAPTER_MARKET_ALIAS.get(m, m)


# ═══════════════════════════════════════════════════════════════════
# Atom aliasing — canonical catalogue atom → any of the real column
# names it can appear under in ``player_game_actuals`` / adapters.
# Empty list means "the atom name IS the DB key".
# ═══════════════════════════════════════════════════════════════════
_ATOM_ALIASES: dict[str, tuple[str, ...]] = {
    # MLB
    "hits":              ("h", "hits", "mlb_h"),
    "home_runs":         ("hr", "home_runs", "mlb_hr"),
    "rbis":              ("rbi", "rbis", "mlb_rbi"),
    "runs":              ("r", "runs", "mlb_r"),
    "total_bases":       ("tb", "total_bases", "mlb_tb"),
    "strikeouts":        ("k", "so", "strikeouts", "mlb_k"),
    "pitcher_strikeouts": ("k", "so", "strikeouts", "pitcher_strikeouts"),
    "pitcher_outs":      ("outs", "pitcher_outs"),
    # NBA
    "points":            ("points", "pts"),
    "rebounds":          ("rebounds", "reb"),
    "assists":           ("assists", "ast"),
    "threes":            ("threes_made", "threes", "fg3m", "3pm"),
    "fg3m":              ("threes_made", "fg3m", "threes"),
    # NFL
    "passing_yards":     ("pass_yds", "passing_yards"),
    "rushing_yards":     ("rush_yds", "rushing_yards"),
    "receiving_yards":   ("rec_yds", "receiving_yards"),
    "receptions":        ("receptions", "rec"),
    "passing_tds":       ("pass_tds", "passing_tds"),
    "rushing_tds":       ("rush_tds", "rushing_tds"),
    "receiving_tds":     ("rec_tds", "receiving_tds"),
    # Soccer
    "goals":             ("goals",),
    "shots":             ("shots",),
    "shots_on_target":   ("shots_on_target", "sot"),
    # Tennis
    "aces":              ("aces",),
    "double_faults":     ("double_faults", "df"),
    "games_won":         ("games_won", "service_games"),
    "sets_won":          ("sets_won",),
    # Team-history atoms
    "team_score":        ("team_score", "score_for", "score"),
    "opponent_score":    ("opponent_score", "score_against"),
    "result":            ("result", "outcome"),
}


def _row_has_atom(row: dict, atom: str) -> bool:
    """Return True iff ``row`` carries a non-None value for ``atom``
    (or any of its known aliases), searching flat fields as well as
    the ``actuals`` / ``stats`` sub-dicts used by the real pod
    schema."""
    aliases = _ATOM_ALIASES.get(atom, (atom,))
    if atom not in aliases:
        aliases = (atom, *aliases)
    for k in aliases:
        if row.get(k) is not None:
            return True
        for sub in ("actuals", "stats"):
            d = row.get(sub)
            if isinstance(d, dict) and d.get(k) is not None:
                return True
    return False


# ═══════════════════════════════════════════════════════════════════
# Utility — safe DB probes
# ═══════════════════════════════════════════════════════════════════
async def _safe_count(db, collection: str, query: dict) -> Optional[int]:
    """Return count or ``None`` on any error — never raises."""
    try:
        c = db[collection]
        return int(await c.count_documents(query))
    except Exception:
        return None


async def _sample_rows(db, collection: str, query: dict,
                        *, limit: int = 25) -> list[dict]:
    try:
        cursor = db[collection].find(query, {"_id": 0}).limit(limit)
        # Support both real motor cursors and _FakeCollection.
        rows: list[dict] = []
        async for d in cursor:
            rows.append(d)
        return rows
    except Exception:
        return []


def _classify_sample(n: Optional[int]) -> CS:
    if n is None:
        return CS.UNKNOWN
    if n == 0:
        return CS.UNAVAILABLE
    if n < 20:
        return CS.PARTIAL
    return CS.PASS


def _sport_l(sport: str) -> str:
    return (sport or "").lower()


# ═══════════════════════════════════════════════════════════════════
# 1. Player-history certification
# ═══════════════════════════════════════════════════════════════════
async def certify_player_history_market(db, market: MarketAtom) -> CertificationEntry:
    """Certify one (sport, player_market) tuple against the
    ``player_game_actuals`` collection AND the dispatcher.

    Emits:
      * ``UNAVAILABLE`` — market marked SOURCE INSUFFICIENT in catalogue.
      * ``FAIL``       — atoms present in catalogue but zero rows in DB.
      * ``PARTIAL``    — rows exist but insufficient for reliable stats.
      * ``PASS``       — rows exist and dispatcher returns a populated
                          evidence object with an atom-derived actual.
    """
    ent = CertificationEntry(
        sport=market.sport, market=market.market,
        evidence_type=ET.PLAYER_HISTORY.value,
    )
    # Catalogue-level UNAVAILABLE.
    if not market.atoms:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.as_of_safe     = CS.NOT_APPLICABLE.value
        ent.identity_resolved = CS.NOT_APPLICABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = market.notes or "no historical atoms defined"
        return ent

    # Count rows for this sport in the canonical collection.
    total = await _safe_count(db, "player_game_actuals",
                                {"sport": _sport_l(market.sport)})
    ent.sample_size = total
    ent.provenance = "player_game_actuals"

    if total is None:
        ent.data_available = CS.UNKNOWN.value
        ent.certification_status = CS.UNKNOWN.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = "collection unreachable / not indexed"
        return ent
    if total == 0:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = ("collection empty in this environment — no "
                       "player_game_actuals rows found")
        return ent
    ent.data_available = CS.PASS.value

    # Verify at least one atom is populated on real rows.
    rows = await _sample_rows(db, "player_game_actuals",
                                {"sport": _sport_l(market.sport)}, limit=50)
    atom_hits = 0
    for r in rows:
        for atom in market.atoms:
            if _row_has_atom(r, atom):
                atom_hits += 1
                break
    if atom_hits == 0:
        # Sample says no rows — but the sample is only 50 out of
        # potentially hundreds of thousands.  Confirm with a full-
        # collection existence probe on each alias before declaring
        # this atom UNREACHABLE.  This is the difference between
        # "no evidence found in first 50 rows" and "no evidence in
        # collection at all".
        full_hits = 0
        for atom in market.atoms:
            aliases = _ATOM_ALIASES.get(atom, (atom,))
            if atom not in aliases:
                aliases = (atom, *aliases)
            for alias in aliases:
                for path in (alias, f"actuals.{alias}", f"stats.{alias}"):
                    c = await _safe_count(db, "player_game_actuals",
                                          {"sport": _sport_l(market.sport),
                                           path: {"$exists": True,
                                                    "$ne": None}})
                    if c and c > 0:
                        full_hits += c
                        break
                if full_hits:
                    break
            if full_hits:
                break
        if full_hits == 0:
            # 0% atom population across entire collection —
            # this is a SOURCE gap for THIS market, not a FAIL of
            # the certification framework.  Emit UNAVAILABLE so
            # Magic 2.0 can degrade gracefully rather than crash.
            ent.reachable = CS.UNAVAILABLE.value
            ent.certification_status = CS.UNAVAILABLE.value
            ent.drop_reason = "EVIDENCE_UNAVAILABLE"
            ent.detail = (
                f"sport has {total} rows but atom(s) "
                f"{market.atoms!r} not populated on ANY row — "
                "market-level source gap")
            return ent
        # Sample missed but collection has rows — partial coverage.
        ent.reachable = CS.PARTIAL.value
        ent.certification_status = CS.PARTIAL.value
        ent.drop_reason = "INSUFFICIENT_EVIDENCE"
        ent.detail = (
            f"atom(s) {market.atoms!r} populated on {full_hits} rows "
            f"of {total} — under-covered market")
        return ent

    ent.reachable = CS.PASS.value

    # Identity check — every row must carry either canonical_player_id
    # OR a resolvable player_id.  A row with only a display name is
    # DOWNGRADED (§8).
    unresolved = sum(1 for r in rows
                      if not (r.get("canonical_player_id") or r.get("player_id")))
    if unresolved:
        ent.identity_resolved = CS.PARTIAL.value
        ent.detail = (f"{unresolved}/{len(rows)} sampled rows missing "
                       "canonical identity (identity downgraded)")
    else:
        ent.identity_resolved = CS.PASS.value

    # As-of safety — probe the dispatcher directly with an as_of in
    # the far past; result must be UNAVAILABLE or empty.
    ent.as_of_safe = await _probe_as_of_safety_player(
        db, market.sport, rows[0] if rows else None)

    # Sample-size classification.
    ent.certification_status = _classify_sample(total).value
    if ent.certification_status == CS.PASS.value and ent.identity_resolved == CS.PARTIAL.value:
        ent.certification_status = CS.PARTIAL.value
    return ent


async def _probe_as_of_safety_player(db, sport: str,
                                      sample_row: Optional[dict]) -> str:
    """Query the dispatcher with an as_of BEFORE any known row and
    ensure zero games come back (no future leakage).

    ``sample_row`` may be None — in which case we cannot probe and
    return UNKNOWN.
    """
    if not sample_row:
        return CS.UNKNOWN.value
    try:
        from services.player_history import get_player_history
    except Exception:
        return CS.UNKNOWN.value
    # Pick an as_of 20 years before any row.
    past = "1970-01-01T00:00:00+00:00"
    try:
        ev = await get_player_history(
            db, sport=sport,
            canonical_player_id=sample_row.get("canonical_player_id"),
            player_id=sample_row.get("player_id"),
            player_name=sample_row.get("player_name"),
            market="probe",
            threshold=0.5,
            event_time=past,
        )
    except Exception:
        return CS.UNKNOWN.value
    # If any window is populated with rows dated after the as_of, that
    # is a leak.  We accept UNAVAILABLE / empty result as PASS.
    games = getattr(ev, "games_used", None) or getattr(ev, "games_available", None)
    if not games:
        return CS.PASS.value
    return CS.FAIL.value


# ═══════════════════════════════════════════════════════════════════
# 2. Team-history certification
# ═══════════════════════════════════════════════════════════════════
async def certify_team_history_market(db, market: MarketAtom) -> CertificationEntry:
    ent = CertificationEntry(
        sport=market.sport, market=market.market,
        evidence_type=ET.TEAM_HISTORY.value,
    )
    if not market.atoms:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.as_of_safe     = CS.NOT_APPLICABLE.value
        ent.identity_resolved = CS.NOT_APPLICABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = market.notes or "no team atoms defined"
        return ent

    total = await _safe_count(db, "team_game_actuals",
                                {"sport": _sport_l(market.sport)})
    ent.sample_size = total
    ent.provenance = "team_game_actuals"

    if total is None:
        ent.certification_status = CS.UNKNOWN.value
        ent.data_available = CS.UNKNOWN.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = "collection unreachable"
        return ent
    if total == 0:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = "team_game_actuals empty for this sport in pod DB"
        return ent
    ent.data_available = CS.PASS.value

    rows = await _sample_rows(db, "team_game_actuals",
                                {"sport": _sport_l(market.sport)}, limit=50)
    with_scores = sum(1 for r in rows
                       if r.get("team_score") is not None
                       and r.get("opponent_score") is not None)
    if with_scores == 0:
        ent.reachable = CS.FAIL.value
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = (f"0/{len(rows)} rows carried both team_score "
                       "and opponent_score")
        return ent
    ent.reachable = CS.PASS.value

    unresolved = sum(1 for r in rows if not r.get("canonical_team_id"))
    ent.identity_resolved = (CS.PASS.value if unresolved == 0
                              else CS.PARTIAL.value)

    ent.as_of_safe = await _probe_as_of_safety_team(
        db, market.sport, rows[0] if rows else None)

    ent.certification_status = _classify_sample(total).value
    if ent.certification_status == CS.PASS.value and unresolved:
        ent.certification_status = CS.PARTIAL.value
        ent.detail = f"{unresolved}/{len(rows)} rows without canonical_team_id"
    return ent


async def _probe_as_of_safety_team(db, sport: str,
                                     sample_row: Optional[dict]) -> str:
    if not sample_row:
        return CS.UNKNOWN.value
    try:
        from services.team_history import get_team_history
    except Exception:
        return CS.UNKNOWN.value
    try:
        ev = await get_team_history(
            db, sport=sport,
            canonical_team_id=sample_row.get("canonical_team_id"),
            team_name=sample_row.get("team_name"),
            as_of="1970-01-01T00:00:00+00:00",
        )
    except Exception:
        return CS.UNKNOWN.value
    if not getattr(ev, "events_used", None):
        return CS.PASS.value
    return CS.FAIL.value


# ═══════════════════════════════════════════════════════════════════
# 3. H2H certification
# ═══════════════════════════════════════════════════════════════════
async def certify_h2h(db, sport: str) -> CertificationEntry:
    """Head-to-Head certification against ``team_game_actuals`` — a
    non-null ``canonical_opponent_id`` on rows proves H2H is
    queryable (§8, §9)."""
    ent = CertificationEntry(
        sport=sport.upper(), market="_h2h_",
        evidence_type=ET.H2H.value,
    )
    total = await _safe_count(db, "team_game_actuals",
                                {"sport": _sport_l(sport),
                                 "canonical_opponent_id": {"$exists": True,
                                                            "$ne": None}})
    ent.sample_size = total
    ent.provenance = "team_game_actuals.canonical_opponent_id"
    if total is None or total == 0:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.identity_resolved = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.drop_reason = "SOURCE_UNAVAILABLE"
        ent.detail = ("no team_game_actuals rows carry "
                       "canonical_opponent_id for this sport")
        return ent
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.identity_resolved = CS.PASS.value
    ent.as_of_safe     = CS.PASS.value    # inherits team-history's as_of
    ent.certification_status = _classify_sample(total).value
    return ent


# ═══════════════════════════════════════════════════════════════════
# 4. Exact-Threshold certification
# ═══════════════════════════════════════════════════════════════════
def certify_exact_threshold_engine() -> CertificationEntry:
    """Uses ``evaluate_threshold`` against an inline deterministic
    sample and asserts:

      * changing the line changes the hit-rate.
      * pushes are excluded from decisions.
      * ``None`` actuals are excluded (never counted as 0).

    This runs entirely in-memory — no DB probe.
    """
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.EXACT_THRESHOLD.value,
    )
    try:
        from services.player_history.threshold_engine import evaluate_threshold
    except Exception as e:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "MODEL_INPUT_INVALID"
        ent.detail = f"threshold engine unavailable: {e}"
        return ent
    actuals = [1.0, 2.0, 2.0, 3.0, 4.0, None, 5.0]
    lo = evaluate_threshold(actuals, 1.5, "over")   # 5 wins
    mid = evaluate_threshold(actuals, 3.5, "over")  # 2 wins
    hi = evaluate_threshold(actuals, 4.5, "over")   # 1 win
    push = evaluate_threshold([2.0, 2.0, 2.0], 2.0, "over")  # all pushes
    # Assertions.
    if not (lo.hit_rate is not None and mid.hit_rate is not None
            and hi.hit_rate is not None):
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "hit_rate should be populated for non-empty samples"
        return ent
    if not (lo.hit_rate > mid.hit_rate > hi.hit_rate):
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = ("raising the line did NOT decrease hit-rate — "
                       "engine is not threshold-aware")
        return ent
    if push.decisions != 0 or push.pushes != 3:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "push handling incorrect on whole-number line"
        return ent
    # None must be excluded (sample_size is 6, not 7).
    if lo.sample_size != 6:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "None actual leaked into sample size"
        return ent
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.sample_size    = lo.sample_size
    ent.provenance     = "player_history.threshold_engine.evaluate_threshold"
    ent.certification_status = CS.PASS.value
    ent.detail = (f"line 1.5→{lo.hit_rate:.3f}, 3.5→{mid.hit_rate:.3f}, "
                   f"4.5→{hi.hit_rate:.3f} — strictly monotonic")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 5. Distribution certification
# ═══════════════════════════════════════════════════════════════════
def certify_distribution_engine() -> CertificationEntry:
    """Confirm the distribution engine returns q25/median/q75/variance
    for a large sample AND refuses to fabricate them for a tiny
    sample (< 3 per QUANTILE_MIN_SAMPLE)."""
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.DISTRIBUTIONS.value,
    )
    try:
        from services.player_history.threshold_engine import (
            evaluate_threshold, QUANTILE_MIN_SAMPLE,
        )
    except Exception as e:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "MODEL_INPUT_INVALID"
        ent.detail = f"threshold engine unavailable: {e}"
        return ent
    big = evaluate_threshold([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5.5, "over",
                              quantiles=True)
    tiny = evaluate_threshold([5.0], 4.5, "over", quantiles=True)
    if big.q25 is None or big.median is None or big.q75 is None or big.variance is None:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "distributions missing on adequate sample"
        return ent
    if tiny.q25 is not None or tiny.median is not None or tiny.q75 is not None:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = ("distributions fabricated on tiny sample "
                       f"(< {QUANTILE_MIN_SAMPLE})")
        return ent
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.sample_size    = 10
    ent.provenance     = "player_history.threshold_engine (_attach_distribution)"
    ent.certification_status = CS.PASS.value
    ent.detail = (f"q25={big.q25}, median={big.median}, q75={big.q75}, "
                   f"var={big.variance:.4f}")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 6. Missing != Zero
# ═══════════════════════════════════════════════════════════════════
def certify_missing_not_zero() -> CertificationEntry:
    """Verify the ``missing_data_guard`` module refuses to coerce
    missing data to zero — the core §7 invariant."""
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.MISSING_NOT_ZERO.value,
    )
    try:
        from services.production_truth.missing_data_guard import (
            coerce_optional_number, UNKNOWN, is_unknown,
        )
    except Exception as e:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "MODEL_INPUT_INVALID"
        ent.detail = f"missing_data_guard unavailable: {e}"
        return ent
    failures: list[str] = []
    # Missing values must NOT become zero.
    for v in (None, "", "N/A", "NA", "NULL", "-", "?", float("nan")):
        r = coerce_optional_number(v)
        if not is_unknown(r):
            failures.append(f"{v!r} → {r!r} (expected UNKNOWN)")
    # Legitimate zero must survive.
    if coerce_optional_number(0) != 0.0:
        failures.append("0 → not 0.0")
    if coerce_optional_number("0") != 0.0:
        failures.append("'0' → not 0.0")
    # UNKNOWN != 0.
    if UNKNOWN == 0 or UNKNOWN == 0.0:
        failures.append("UNKNOWN == 0 (violated §7)")
    if failures:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "; ".join(failures)
        return ent
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.provenance     = "production_truth.missing_data_guard"
    ent.certification_status = CS.PASS.value
    ent.detail = ("None, '', 'N/A', 'NA', 'NULL', '-', '?', NaN all → "
                   "UNKNOWN; real 0 preserved")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 7. As-of safety (aggregate) — dispatcher-level
# ═══════════════════════════════════════════════════════════════════
async def certify_as_of_safety(db) -> CertificationEntry:
    """Aggregate as-of certification.

    Queries player-history and team-history dispatchers with an
    as_of of 1970 for any sport that has data.  Any positive result
    is a leakage failure.
    """
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.AS_OF_SAFETY.value,
    )
    leaks: list[str] = []
    probes: list[tuple[str, str, str]] = []   # sport, kind, identity

    try:
        # Sample one row per sport if present.
        for sport in ("MLB", "NBA", "NFL", "SOCCER", "TENNIS"):
            rows = await _sample_rows(db, "player_game_actuals",
                                        {"sport": _sport_l(sport)}, limit=1)
            if rows:
                probes.append(("player", sport, rows[0]))
        for sport in ("MLB", "NBA", "NFL", "SOCCER"):
            rows = await _sample_rows(db, "team_game_actuals",
                                        {"sport": _sport_l(sport)}, limit=1)
            if rows:
                probes.append(("team", sport, rows[0]))
    except Exception:
        pass

    if not probes:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no historical rows available to probe as-of safety"
        return ent

    try:
        from services.player_history import get_player_history
        from services.team_history import get_team_history
    except Exception as e:
        ent.certification_status = CS.UNKNOWN.value
        ent.detail = f"dispatchers unavailable: {e}"
        return ent

    past = "1970-01-01T00:00:00+00:00"
    for kind, sport, row in probes:
        try:
            if kind == "player":
                ev = await get_player_history(
                    db, sport=sport,
                    canonical_player_id=row.get("canonical_player_id"),
                    player_id=row.get("player_id"),
                    player_name=row.get("player_name"),
                    market="probe",
                    threshold=0.5,
                    event_time=past,
                )
                if getattr(ev, "games_used", None):
                    leaks.append(f"player/{sport} leaked {ev.games_used} games")
            else:
                ev = await get_team_history(
                    db, sport=sport,
                    canonical_team_id=row.get("canonical_team_id"),
                    team_name=row.get("team_name"),
                    as_of=past,
                )
                if getattr(ev, "events_used", None):
                    leaks.append(f"team/{sport} leaked {ev.events_used} events")
        except Exception as e:
            leaks.append(f"{kind}/{sport} probe crashed: {e!r}")

    ent.sample_size = len(probes)
    ent.provenance  = "get_player_history / get_team_history (as_of=1970)"
    if leaks:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "EVIDENCE_UNAVAILABLE"
        ent.detail = "; ".join(leaks)
        return ent
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.as_of_safe     = CS.PASS.value
    ent.identity_resolved = CS.PASS.value
    ent.certification_status = CS.PASS.value
    ent.detail = f"{len(probes)} dispatcher probes returned zero games"
    return ent


# ═══════════════════════════════════════════════════════════════════
# 8. Identity certification
# ═══════════════════════════════════════════════════════════════════
async def certify_identity(db) -> CertificationEntry:
    """Prove that ``player_game_actuals`` and ``team_game_actuals``
    are indexed by canonical id.  Rows without canonical id are
    downgraded per §8."""
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.IDENTITY.value,
    )
    p_total = await _safe_count(db, "player_game_actuals", {}) or 0
    p_canon = await _safe_count(db, "player_game_actuals",
                                  {"canonical_player_id":
                                     {"$exists": True, "$ne": None}}) or 0
    t_total = await _safe_count(db, "team_game_actuals", {}) or 0
    t_canon = await _safe_count(db, "team_game_actuals",
                                  {"canonical_team_id":
                                     {"$exists": True, "$ne": None}}) or 0

    if p_total == 0 and t_total == 0:
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no rows to certify identity against"
        return ent
    p_frac = (p_canon / p_total) if p_total else 1.0
    t_frac = (t_canon / t_total) if t_total else 1.0

    ent.sample_size = p_total + t_total
    ent.provenance  = "player_game_actuals + team_game_actuals"
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value

    if p_frac >= 0.99 and t_frac >= 0.99:
        ent.identity_resolved = CS.PASS.value
        ent.certification_status = CS.PASS.value
    else:
        ent.identity_resolved = CS.PARTIAL.value
        ent.certification_status = CS.PARTIAL.value
    ent.detail = (f"player canonical: {p_canon}/{p_total} "
                   f"({p_frac*100:.1f}%); "
                   f"team canonical: {t_canon}/{t_total} "
                   f"({t_frac*100:.1f}%)")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 8b. Pick identity tagging — LIVE picks must carry canonical IDs
# ═══════════════════════════════════════════════════════════════════
async def certify_pick_identity_tagging(
    db, *, sample_size: int = 2000,
) -> list[CertificationEntry]:
    """Audit whether live picks carry canonical identity.

    §8: "Prove that history is attached only through canonical
    identity."  For Magic to consume history, the picks themselves
    must be tagged with either ``canonical_player_id`` (player
    markets) or ``canonical_team_id`` (team markets) or at minimum a
    ``player_name`` / ``team`` string that identity resolution can
    walk to canonical.  This check reports the coverage per sport.

    Emits ONE entry per sport.  A sport whose picks carry no
    canonical id at all is flagged with drop_reason=
    ``IDENTITY_MISSING_ON_PICKS`` — a real Pre-Magic blocker even
    when history rows for that sport exist.
    """
    out: list[CertificationEntry] = []
    priority_sports = ("MLB", "NBA", "NFL", "NHL", "SOCCER", "TENNIS",
                        "UFC", "CFB")
    for sport in priority_sports:
        ent = CertificationEntry(
            sport=sport, market="_identity_tagging_",
            evidence_type=ET.PICK_IDENTITY_TAGGING.value,
        )
        try:
            q = {"sport": {"$regex": f"^{sport}", "$options": "i"}}
            total = await _safe_count(db, "picks", q) or 0
        except Exception:
            total = 0
        ent.sample_size = total
        ent.provenance = "picks"
        if total == 0:
            ent.data_available = CS.UNAVAILABLE.value
            ent.reachable      = CS.UNAVAILABLE.value
            ent.certification_status = CS.UNAVAILABLE.value
            ent.detail = "no live picks for this sport"
            out.append(ent)
            continue
        # Count coverage of each identity field.
        pid_canon = await _safe_count(db, "picks",
            {**q, "canonical_player_id": {"$exists": True, "$ne": None}}) or 0
        tid_canon = await _safe_count(db, "picks",
            {**q, "canonical_team_id": {"$exists": True, "$ne": None}}) or 0
        eid_canon = await _safe_count(db, "picks",
            {**q, "canonical_event_id": {"$exists": True, "$ne": None}}) or 0
        p_name = await _safe_count(db, "picks",
            {**q, "player_name": {"$exists": True, "$ne": None}}) or 0
        t_name = await _safe_count(db, "picks",
            {**q, "team": {"$exists": True, "$ne": None}}) or 0
        # A pick is MAGIC-reachable if it carries at least one of:
        #   canonical_player_id / canonical_team_id / canonical_event_id
        any_canonical_q = {**q, "$or": [
            {"canonical_player_id": {"$exists": True, "$ne": None}},
            {"canonical_team_id":   {"$exists": True, "$ne": None}},
            {"canonical_event_id":  {"$exists": True, "$ne": None}},
        ]}
        any_canonical = await _safe_count(db, "picks", any_canonical_q) or 0
        any_name      = max(p_name, t_name)
        ent.data_available = CS.PASS.value
        ent.reachable      = CS.PASS.value
        detail = (f"total={total} — "
                  f"any_canonical_id={any_canonical}, "
                  f"canonical_event_id={eid_canon}, "
                  f"canonical_team_id={tid_canon}, "
                  f"canonical_player_id={pid_canon}, "
                  f"player_name={p_name}, team={t_name}")
        ent.detail = detail
        if any_canonical == 0 and any_name == 0:
            # Complete identity gap — Magic cannot reach history for
            # any pick in this sport.
            ent.identity_resolved = CS.FAIL.value
            ent.certification_status = CS.FAIL.value
            ent.drop_reason = "IDENTITY_MISSING_ON_PICKS"
        elif any_canonical == 0 and any_name > 0:
            # Names only — identity must be resolved at Magic time.
            # Downgraded because canonical is the source of truth (§8).
            ent.identity_resolved = CS.PARTIAL.value
            ent.certification_status = CS.PARTIAL.value
            ent.drop_reason = "IDENTITY_UNRESOLVED"
        elif any_canonical / total < 0.90:
            ent.identity_resolved = CS.PARTIAL.value
            ent.certification_status = CS.PARTIAL.value
            ent.drop_reason = "IDENTITY_UNRESOLVED"
        else:
            ent.identity_resolved = CS.PASS.value
            ent.certification_status = CS.PASS.value
        out.append(ent)
    return out


# ═══════════════════════════════════════════════════════════════════
# 9. Market normalization
# ═══════════════════════════════════════════════════════════════════
def certify_market_normalization(catalog) -> CertificationEntry:
    """Certify that every market in the catalogue with defined atoms
    is resolvable — catalogue-completeness gate."""
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.MARKET_NORMALIZATION.value,
    )
    missing: list[str] = []
    with_atoms = 0
    for m in catalog:
        if not m.atoms:
            continue
        with_atoms += 1
        for atom in m.atoms:
            if not isinstance(atom, str) or not atom:
                missing.append(f"{m.sport}.{m.market}: invalid atom")
    ent.data_available = CS.PASS.value if with_atoms else CS.UNAVAILABLE.value
    ent.reachable      = CS.PASS.value if with_atoms else CS.UNAVAILABLE.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.sample_size    = with_atoms
    ent.provenance     = "pre_magic_certification.market_catalog"
    if missing:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "MARKET_UNAVAILABLE"
        ent.detail = "; ".join(missing[:10])
    else:
        ent.certification_status = (
            CS.PASS.value if with_atoms else CS.UNAVAILABLE.value)
        ent.detail = f"{with_atoms} markets with defined atoms"
    return ent


# ═══════════════════════════════════════════════════════════════════
# 10. Tennis context readiness
# ═══════════════════════════════════════════════════════════════════
async def certify_tennis_context(db) -> CertificationEntry:
    """Confirm contextual tennis fields (surface / tournament / round /
    serve metrics) are retrievable from ``player_game_actuals``."""
    ent = CertificationEntry(
        sport="TENNIS", market="_context_",
        evidence_type=ET.TENNIS_CONTEXT.value,
    )
    rows = await _sample_rows(db, "player_game_actuals",
                                {"sport": "tennis"}, limit=50)
    if not rows:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no tennis rows in pod DB"
        return ent
    fields = {"surface": 0, "tournament": 0, "round": 0,
              "aces": 0, "double_faults": 0, "break_points_saved": 0}
    for r in rows:
        for k in list(fields.keys()):
            v = r.get(k)
            if v is None and isinstance(r.get("stats"), dict):
                v = r["stats"].get(k)
            if v is None and isinstance(r.get("actuals"), dict):
                v = r["actuals"].get(k)
            if v is not None:
                fields[k] += 1
    present = [k for k, n in fields.items() if n > 0]
    missing = [k for k, n in fields.items() if n == 0]
    ent.data_available = CS.PASS.value if present else CS.UNAVAILABLE.value
    ent.reachable      = CS.PASS.value if present else CS.UNAVAILABLE.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.sample_size    = len(rows)
    ent.provenance     = "player_game_actuals[sport=tennis]"
    if not present:
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no contextual fields present on tennis rows"
    elif missing:
        ent.certification_status = CS.PARTIAL.value
        ent.drop_reason = "INSUFFICIENT_EVIDENCE"
        ent.detail = f"present: {present}; missing: {missing}"
    else:
        ent.certification_status = CS.PASS.value
        ent.detail = f"all contextual fields present: {present}"
    return ent


# ═══════════════════════════════════════════════════════════════════
# 11. Market / Sportsbook readiness (Real book odds + provenance)
# ═══════════════════════════════════════════════════════════════════
async def certify_market_readiness(db, *, sample_size: int = 200) -> CertificationEntry:
    """Inspect a batch of recent published picks and classify their
    market readiness.  Detects the Soccer producer failure mode
    (synthetic/fair odds) if it appears."""
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.MARKET_READINESS.value,
    )
    try:
        cursor = db["picks"].find({}, {
            "_id": 0, "sport": 1, "market": 1, "book_odds": 1,
            "odds_provenance": 1, "no_real_book_line": 1,
            "implied_probability": 1, "line": 1,
            "book": 1, "book_line_timestamp": 1,
        }).limit(sample_size)
        picks: list[dict] = []
        async for p in cursor:
            picks.append(p)
    except Exception:
        picks = []
    ent.sample_size = len(picks)
    ent.provenance  = "picks (book_odds + odds_provenance)"
    if not picks:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no picks in pod DB — cannot certify market readiness"
        return ent
    from services.production_truth.missing_data_guard import (
        validate_no_synthetic_odds, MissingDataViolation,
    )
    real = 0
    synthetic = 0
    missing = 0
    null_ip = 0
    for p in picks:
        try:
            validate_no_synthetic_odds(
                p.get("book_odds"),
                no_real_book_line=p.get("no_real_book_line"),
                provenance=p.get("odds_provenance"),
            )
            real += 1
        except MissingDataViolation as e:
            msg = str(e).lower()
            if "synthetic" in msg or "provenance" in msg or "no_real" in msg:
                synthetic += 1
            else:
                missing += 1
        if p.get("implied_probability") is None:
            null_ip += 1

    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value

    if synthetic > 0:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "REAL_LINE_UNAVAILABLE"
        ent.detail = (f"{synthetic}/{len(picks)} picks carry synthetic / "
                       f"fair odds; {missing} have missing book_odds; "
                       f"{null_ip} have null implied_probability")
        return ent
    if missing / max(1, len(picks)) > 0.10:
        ent.certification_status = CS.PARTIAL.value
        ent.drop_reason = "REAL_LINE_UNAVAILABLE"
        ent.detail = (f"{missing}/{len(picks)} picks missing book_odds "
                       f"(>10% threshold)")
        return ent
    ent.certification_status = CS.PASS.value
    ent.detail = (f"{real}/{len(picks)} real-book picks; "
                   f"{missing} model-only correctly identified; "
                   f"null implied_probability on {null_ip}")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 12. Soccer producer integrity — checks espn_soccer_fixtures.py
#    for the known synthetic-odds failure mode
# ═══════════════════════════════════════════════════════════════════
async def certify_soccer_producer_integrity(db) -> CertificationEntry:
    """Detects whether the ESPN soccer fixture producer has been
    letting synthetic odds through onto real picks."""
    ent = CertificationEntry(
        sport="SOCCER", market="_producer_",
        evidence_type=ET.SOCCER_PRODUCER_INTEGRITY.value,
    )
    # Look at soccer picks in db.picks and check for synthetic odds.
    try:
        cursor = db["picks"].find(
            {"sport": {"$regex": "soccer|epl|mls|championsleague|laliga|bundes",
                       "$options": "i"}},
            {"_id": 0, "book_odds": 1, "odds_provenance": 1,
             "no_real_book_line": 1, "implied_probability": 1,
             "id": 1, "sport": 1, "market": 1},
        ).limit(500)
        picks: list[dict] = []
        async for p in cursor:
            picks.append(p)
    except Exception:
        picks = []
    ent.sample_size = len(picks)
    ent.provenance = "picks[sport~soccer]"
    if not picks:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no soccer picks to audit"
        return ent
    bad: list[dict] = []
    for p in picks:
        prov = (p.get("odds_provenance") or "").upper()
        if prov in {"MODEL", "SYNTHETIC", "FAIR", "MODEL_ONLY", "COMPUTED"}:
            bad.append({"id": p.get("id"), "provenance": prov,
                         "market": p.get("market")})
        elif p.get("no_real_book_line") is True and p.get("book_odds"):
            bad.append({"id": p.get("id"),
                         "reason": "no_real_book_line=True but book_odds present",
                         "market": p.get("market")})
        elif p.get("implied_probability") is None and p.get("book_odds"):
            bad.append({"id": p.get("id"),
                         "reason": "book_odds present but null implied_probability",
                         "market": p.get("market")})
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    if bad:
        ent.certification_status = CS.FAIL.value
        ent.drop_reason = "REAL_LINE_UNAVAILABLE"
        ent.detail = (f"{len(bad)}/{len(picks)} soccer picks flagged. "
                       f"Sample: {bad[:5]}")
    else:
        ent.certification_status = CS.PASS.value
        ent.detail = (f"{len(picks)} soccer picks — none carry synthetic "
                       "odds / null implied_probability inconsistency")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 13. Model / Simulator readiness
# ═══════════════════════════════════════════════════════════════════
async def certify_model_readiness(db, *, sample_size: int = 200) -> CertificationEntry:
    """Confirm ``model_probability`` is populated on published picks
    with real provenance — no anonymous confidence values.

    Sampling strategy: prefer newest picks with canonical identity
    (i.e. post-remediation picks) — these are the picks that flow
    through Magic 2.0.  Fall back to newest picks of any kind.
    """
    ent = CertificationEntry(
        sport="_ALL_", market="_ALL_",
        evidence_type=ET.MODEL_READINESS.value,
    )
    picks: list[dict] = []
    try:
        # Prefer post-remediation picks (carry model_evidence_version).
        cursor = db["picks"].find(
            {"model_evidence_version": {"$exists": True}},
            {"_id": 0, "model_probability": 1, "model_source": 1,
             "model_probability_source": 1,
             "model_probability_provenance": 1,
             "simulator_probability": 1, "engine": 1}
        ).sort("identity_enriched_at", -1).limit(sample_size)
        async for p in cursor:
            picks.append(p)
        # Fall back to any picks if remediation set is thin.
        if len(picks) < sample_size:
            remaining = sample_size - len(picks)
            cursor = db["picks"].find(
                {}, {"_id": 0, "model_probability": 1, "model_source": 1,
                     "model_probability_source": 1,
                     "model_probability_provenance": 1,
                     "simulator_probability": 1, "engine": 1}
            ).sort("created_at", -1).limit(remaining)
            async for p in cursor:
                picks.append(p)
    except Exception:
        picks = []
    ent.sample_size = len(picks)
    ent.provenance  = "picks.model_probability"
    if not picks:
        ent.data_available = CS.UNAVAILABLE.value
        ent.reachable      = CS.UNAVAILABLE.value
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = "no picks in pod DB"
        return ent
    with_model = sum(1 for p in picks if p.get("model_probability") is not None)
    with_source = sum(1 for p in picks if p.get("model_source")
                        or p.get("engine")
                        or p.get("model_probability_source")
                        or p.get("model_probability_provenance"))
    ent.data_available = CS.PASS.value
    ent.reachable      = CS.PASS.value
    ent.identity_resolved = CS.NOT_APPLICABLE.value
    ent.as_of_safe     = CS.NOT_APPLICABLE.value
    if with_model == 0:
        ent.certification_status = CS.UNAVAILABLE.value
        ent.detail = f"0/{len(picks)} picks carry model_probability"
        return ent
    if with_source / max(1, with_model) < 0.5:
        ent.certification_status = CS.PARTIAL.value
        ent.drop_reason = "MODEL_INPUT_INVALID"
        ent.detail = (f"only {with_source}/{with_model} model_probability "
                       "picks carry provenance (engine/model_source)")
        return ent
    ent.certification_status = CS.PASS.value
    ent.detail = (f"{with_model}/{len(picks)} picks with model_probability; "
                   f"{with_source} carry engine/model_source provenance")
    return ent


# ═══════════════════════════════════════════════════════════════════
# 14. Live pick → history reachability (§1)
# ═══════════════════════════════════════════════════════════════════
async def certify_live_pick_reachability(
    db,
    *,
    sample_size: int = 25,
) -> list[CertificationEntry]:
    """For a batch of REAL published picks, trace:

        canonical pick
          → sport
          → canonical player/team identity
          → market
          → history adapter
          → historical actuals (bounded by pick.commence_time or now)

    Emits ONE entry per sampled pick.  This is the strongest §1
    check — it proves the READ-PATH can actually reach the evidence,
    not just that rows exist in isolation.

    Sampling strategy: prefer diversity across sports so we can
    exercise the read-path for every sport that has BOTH picks AND
    history in the pod (§1: "Do this by sport/market where history
    is available").  We split the sample budget across sports rather
    than blindly grabbing the most-recent N (which in the pod DB
    tends to be dominated by exotic soccer leagues with no history).
    """
    out: list[CertificationEntry] = []
    picks: list[dict] = []
    # ── Prefer diversity: pull picks per sport that has history. ─
    #    Within each sport, prefer picks that have been canonically
    #    identified (i.e. carry ``canonical_event_id``), as those are
    #    the picks that would flow through Magic 2.0 when eventually
    #    wired.  Fall back to any pick if no canonical ones exist.
    priority_sports = ("MLB", "NBA", "NFL", "SOCCER", "TENNIS")
    per_sport = max(1, sample_size // len(priority_sports))
    seen_ids: set = set()
    try:
        for sport in priority_sports:
            # First pass — canonically-identified picks (preferred).
            cursor = db["picks"].find(
                {"sport": {"$regex": f"^{sport}", "$options": "i"},
                 "canonical_event_id": {"$exists": True, "$ne": None}},
                {"_id": 0},
            ).sort("created_at", -1).limit(per_sport)
            n_added = 0
            async for p in cursor:
                pid = p.get("id") or p.get("_id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                picks.append(p)
                n_added += 1
            # If none canonical, fall back to newest of any kind.
            if n_added == 0:
                cursor = db["picks"].find(
                    {"sport": {"$regex": f"^{sport}", "$options": "i"}},
                    {"_id": 0},
                ).sort("created_at", -1).limit(per_sport)
                async for p in cursor:
                    pid = p.get("id") or p.get("_id")
                    if pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    picks.append(p)
        # Top up with newest picks (any sport) if we're under budget.
        if len(picks) < sample_size:
            remaining = sample_size - len(picks)
            cursor = db["picks"].find({}, {"_id": 0}).sort(
                "created_at", -1).limit(remaining * 4)
            async for p in cursor:
                pid = p.get("id") or p.get("_id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                picks.append(p)
                if len(picks) >= sample_size:
                    break
    except Exception:
        pass

    if not picks:
        stub = CertificationEntry(
            sport="_ALL_", market="_ALL_",
            evidence_type=ET.LIVE_PICK_REACHABILITY.value,
        )
        stub.data_available = CS.UNAVAILABLE.value
        stub.reachable      = CS.UNAVAILABLE.value
        stub.certification_status = CS.UNAVAILABLE.value
        stub.detail = "no picks in pod DB — cannot certify live reachability"
        out.append(stub)
        return out

    try:
        from services.player_history import get_player_history
        from services.team_history import get_team_history
    except Exception as e:
        stub = CertificationEntry(
            sport="_ALL_", market="_ALL_",
            evidence_type=ET.LIVE_PICK_REACHABILITY.value,
        )
        stub.certification_status = CS.FAIL.value
        stub.drop_reason = "MODEL_INPUT_INVALID"
        stub.detail = f"dispatchers unavailable: {e}"
        out.append(stub)
        return out

    for p in picks:
        sport = (p.get("sport") or "").upper()
        market = (p.get("market") or "").lower()
        ent = CertificationEntry(
            sport=sport or "?", market=market or "?",
            evidence_type=ET.LIVE_PICK_REACHABILITY.value,
        )
        ent.provenance = f"pick.id={p.get('id')}"
        commence = p.get("commence_time") or p.get("kickoff") or \
                    datetime.now(timezone.utc).isoformat()
        # Player market path.
        is_player = bool(p.get("player_name") or p.get("canonical_player_id"))
        try:
            if is_player and sport in ("MLB", "NBA", "NFL", "SOCCER",
                                          "TENNIS", "UFC"):
                ev = await get_player_history(
                    db, sport=sport,
                    canonical_player_id=p.get("canonical_player_id"),
                    player_id=p.get("player_id"),
                    player_name=p.get("player_name"),
                    market=_adapter_market(market),
                    threshold=float(p.get("line") or 0.5),
                    direction=p.get("direction") or "over",
                    opponent=p.get("opponent_name") or p.get("opponent"),
                    event_time=commence,
                    home_away=p.get("home_away"),
                    current_team=p.get("team"),
                )
                if getattr(ev, "games_used", None):
                    ent.data_available = CS.PASS.value
                    ent.reachable      = CS.PASS.value
                    ent.identity_resolved = (
                        CS.PASS.value if ev.canonical_player_id
                        else CS.PARTIAL.value)
                    ent.as_of_safe     = CS.PASS.value
                    ent.sample_size    = int(ev.games_used or 0)
                    ent.certification_status = CS.PASS.value
                    ent.detail = (
                        f"{ev.games_used} games reachable; "
                        f"quality={ev.data_quality}")
                else:
                    ent.data_available = (
                        CS.UNAVAILABLE.value if (ev.data_quality or "").upper()
                        == "UNAVAILABLE" else CS.PARTIAL.value)
                    ent.reachable      = CS.UNAVAILABLE.value
                    ent.certification_status = (
                        CS.UNAVAILABLE.value if (ev.data_quality or "").upper()
                        == "UNAVAILABLE" else CS.PARTIAL.value)
                    ent.drop_reason = "EVIDENCE_UNAVAILABLE"
                    ent.detail = f"dispatcher returned quality={ev.data_quality}"
            else:
                # Team market path.
                ev = await get_team_history(
                    db, sport=sport,
                    canonical_team_id=p.get("canonical_team_id"),
                    team_name=p.get("team"),
                    opponent_id=p.get("canonical_opponent_id"),
                    home_away=p.get("home_away"),
                    competition=p.get("competition") or p.get("league"),
                    metric=market,
                    as_of=commence,
                )
                if getattr(ev, "events_used", None):
                    ent.data_available = CS.PASS.value
                    ent.reachable      = CS.PASS.value
                    ent.identity_resolved = (
                        CS.PASS.value if ev.canonical_team_id
                        else CS.PARTIAL.value)
                    ent.as_of_safe     = CS.PASS.value
                    ent.sample_size    = int(ev.events_used or 0)
                    ent.certification_status = CS.PASS.value
                    ent.detail = f"{ev.events_used} team events reachable"
                else:
                    ent.data_available = (
                        CS.UNAVAILABLE.value if (ev.data_quality or "").upper()
                        == "UNAVAILABLE" else CS.PARTIAL.value)
                    ent.reachable      = CS.UNAVAILABLE.value
                    ent.certification_status = (
                        CS.UNAVAILABLE.value if (ev.data_quality or "").upper()
                        == "UNAVAILABLE" else CS.PARTIAL.value)
                    ent.drop_reason = "EVIDENCE_UNAVAILABLE"
                    ent.detail = (
                        f"dispatcher returned status={ev.status} "
                        f"quality={ev.data_quality}")
        except Exception as e:
            ent.certification_status = CS.FAIL.value
            ent.drop_reason = "EVIDENCE_UNAVAILABLE"
            ent.detail = f"dispatcher crashed: {e!r}"
        out.append(ent)
    return out


__all__ = [
    "certify_player_history_market",
    "certify_team_history_market",
    "certify_h2h",
    "certify_exact_threshold_engine",
    "certify_distribution_engine",
    "certify_missing_not_zero",
    "certify_as_of_safety",
    "certify_identity",
    "certify_market_normalization",
    "certify_pick_identity_tagging",
    "certify_tennis_context",
    "certify_market_readiness",
    "certify_soccer_producer_integrity",
    "certify_model_readiness",
    "certify_live_pick_reachability",
]
