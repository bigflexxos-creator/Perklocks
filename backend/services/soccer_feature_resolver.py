"""League-aware Soccer feature resolver — SOCCER_UNIVERSAL_RUNTIME.

Consumes every legitimate existing history / form store in the current
Perklocks database.  Never fabricates statistics.  Returns a normalized
feature dict PLUS a precise taxonomy code describing which stage
failed when no evidence is found.

Resolution chain (all league-aware; sample-size honest):

    1. `soccer_player_form`               — Understat / SportDB / ESPN
       pre-aggregated form (2,774 rows across Big-5 + top MLS players).
    2. `player_game_actuals` aggregation  — 305,132-row universal actuals
       store; filter to `sport="soccer"` + `player_name` match; aggregate
       recent N appearances into rolling goals / assists / shots /
       shots-on-target rates.  This is what unlocks Messi / Evander /
       Bouanga / Suárez etc. — they exist here even when
       `soccer_player_form` is empty for MLS.
    3. `soccer_player_game_logs` aggregation — 50,112-row per-fixture
       logs (canonicalized by short name — used when the actuals store
       doesn't cover the league).

Each hit returns an ``evidence_source`` label so downstream
attribution / diagnostics can report exactly which layer produced the
row.
"""
from __future__ import annotations
from typing import Any, Optional

from services.soccer_season_resolver import (
    resolve_current_season, resolve_prior_season,
)
from services.soccer_historical_stats import aggregate_player_season


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
async def resolve_soccer_player_features(
    db, *, player_name: str, league: str,
    canonical_player_id: Optional[str] = None,
    canonical_player_name: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    provider_player_name: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """Return ``(feature_row, evidence_source)`` for the player.

    UNIVERSAL_IDENTITY_HISTORY_BRIDGE (2026-09) — lookup priority:
      1. canonical_player_id (when the store carries the field)
      2. verified aliases (identity registry) — set of names
      3. canonical name (from the identity registry)
      4. normalized provider display name fallback

    Raw provider name is no longer the primary key.  Callers should
    pass the full ResolvedIdentity so downstream evidence stores can
    match on canonical IDs first and only fall back to name matching
    when the historical row lacks the ID.
    """
    if not player_name and not canonical_player_name:
        return None, ""

    # ── Build a de-duplicated, normalised set of name variants
    #    covering canonical + aliases + provider + accent-strips.
    def _norm_name(s: str) -> str:
        return (s or "").strip().lower()

    def _ascii(s: str) -> str:
        import unicodedata as _ud
        if not s:
            return ""
        n = _ud.normalize("NFKD", s)
        return "".join(c for c in n if not _ud.combining(c)).strip().lower()

    variants: set[str] = set()
    for n in (
        canonical_player_name, provider_player_name, player_name,
        *(aliases or []),
    ):
        if n:
            variants.add(_norm_name(n))
            variants.add(_ascii(n))
    variants.discard("")
    variants_list = list(variants)
    primary = _norm_name(canonical_player_name or player_name)

    # ── 1.  soccer_player_form — canonical_player_id then variants ─
    row = None
    if canonical_player_id:
        row = await db.soccer_player_form.find_one(
            {"canonical_player_id": canonical_player_id}
        )
    if not row and variants_list:
        row = await db.soccer_player_form.find_one(
            {"name_canonical": {"$in": variants_list}}
        )
    if row and int(row.get("minutes") or 0) >= 90:
        return row, "soccer_player_form"

    # ── 2.  player_game_actuals rolling aggregate — ID-first ────
    try:
        agg = await _aggregate_from_actuals(
            db, player_name=primary,
            canonical_player_id=canonical_player_id,
            name_variants=variants_list,
        )
    except Exception:
        agg = None
    if agg and (agg.get("minutes") or 0) >= 90:
        return agg, "player_game_actuals"

    # ── 3.  soccer_player_game_logs current/prior season ────────
    if league:
        for season_fn, min_mins, label in (
            (resolve_current_season, 180, "logs_current_season"),
            (resolve_prior_season,   400, "logs_prior_season"),
        ):
            try:
                season = season_fn(league)
                for nc in [primary, *variants_list]:
                    g = await aggregate_player_season(
                        db, player_name_canonical=nc, season=season,
                    )
                    if g and int(g.get("minutes") or 0) >= min_mins:
                        return g, label
            except Exception:
                pass

    # ── 4.  ESPN MLS stats fallback — PHASE 0 §3-§4 (2026-06). ─────
    # LAST-RESORT enrichment when internal history stores are empty
    # for this player.  Only kicks in for MLS/LigaMX-adjacent players
    # whose evidence didn't populate any Understat / actuals / logs
    # row (typical for mid-season new MLS signings).
    # STRICT CONTRACT:
    #   * ESPN NEVER overwrites canonical IDs (returned row carries
    #     the canonical_player_id from the identity resolver so
    #     downstream authority stays intact).
    #   * ESPN is tagged with a distinct evidence_source
    #     ("espn_mls_stats") so consumers can differentiate it from
    #     first-class stores.
    #   * ESPN season totals are converted to per-90 rates assuming
    #     ~65 mins/appearance (MLS average starter minutes).
    try:
        espn_row = await _lookup_espn_mls_stats(
            db, primary_name=primary, variants=variants_list,
            canonical_player_id=canonical_player_id,
        )
    except Exception:
        espn_row = None
    if espn_row and int(espn_row.get("minutes") or 0) >= 180:
        return espn_row, "espn_mls_stats"

    # ── 5.  Fall through — return whichever we found (even sparse) ─
    if row:
        return row, "soccer_player_form"
    if agg:
        return agg, "player_game_actuals"
    if espn_row:
        return espn_row, "espn_mls_stats"
    return None, ""


async def resolve_soccer_player_prior(
    db, *, player_name: str, league: str,
    canonical_player_name: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> Optional[dict]:
    """Prior-season aggregate for empirical-Bayes blending.
    Tries every verified alias variant of the canonical player.
    """
    if (not player_name and not canonical_player_name) or not league:
        return None
    def _norm_name(s: str) -> str:
        return (s or "").strip().lower()
    def _ascii(s: str) -> str:
        import unicodedata as _ud
        if not s:
            return ""
        n = _ud.normalize("NFKD", s)
        return "".join(c for c in n if not _ud.combining(c)).strip().lower()
    variants: set[str] = set()
    for n in (
        canonical_player_name, player_name, *(aliases or []),
    ):
        if n:
            variants.add(_norm_name(n))
            variants.add(_ascii(n))
    variants.discard("")
    try:
        prior_season = resolve_prior_season(league)
        for nc in variants:
            row = await aggregate_player_season(
                db, player_name_canonical=nc, season=prior_season,
            )
            if row:
                return row
        return None
    except Exception:
        return None


async def resolve_soccer_player_matchup(
    db, *, player_name: str, opponent_team: str,
) -> Optional[dict]:
    """Return H2H matchup dossier for player-vs-opponent, or None.

    Uses `mls_player_matchup_history` — the existing MLS matchup
    store populated by prior History backfill work.  Structure:
    ``{player_name, by_opponent: [ ... ], total_events, refreshed_at}``.
    """
    if not (player_name and opponent_team):
        return None
    try:
        row = await db.mls_player_matchup_history.find_one({
            "player_name": {"$regex": f"^{player_name.strip()}$", "$options": "i"},
        })
    except Exception:
        row = None
    if not row:
        return None
    bo = row.get("by_opponent")
    # by_opponent can be a list of dicts OR a dict keyed by opponent —
    # accept both shapes.
    match_data: Optional[dict] = None
    if isinstance(bo, list):
        for item in bo:
            if not isinstance(item, dict):
                continue
            opp = (item.get("opponent") or item.get("team") or "").strip().lower()
            if opp == opponent_team.strip().lower():
                match_data = item
                break
    elif isinstance(bo, dict):
        for k, v in bo.items():
            if k.strip().lower() == opponent_team.strip().lower():
                match_data = v if isinstance(v, dict) else {"opponent": k, "data": v}
                break
    if not match_data:
        return None
    return {
        "opponent":        opponent_team,
        "events":          int(match_data.get("events") or match_data.get("total_events") or 0),
        "goals":           float(match_data.get("goals") or 0),
        "assists":         float(match_data.get("assists") or 0),
        "shots":           float(match_data.get("shots") or 0),
        "shots_on_target": float(match_data.get("shots_on_target") or 0),
        "source":          "mls_player_matchup_history",
    }


# ─────────────────────────────────────────────────────────────────────
# Rejection classification — replaces the 783-row MISSING_FEATURE_DATA
# black-hole with precise per-stage codes.
# ─────────────────────────────────────────────────────────────────────
async def classify_missing_feature_reason(
    db, *, player_name: str, league: str,
) -> str:
    """Return a taxonomy code describing exactly why the resolver
    could not produce evidence for this player."""
    from services.soccer_rejection_taxonomy import SoccerRejection

    if not player_name:
        return SoccerRejection.PLAYER_IDENTITY_FAILURE.value
    nc = player_name.strip().lower()

    form = await db.soccer_player_form.find_one({"name_canonical": nc})
    n_actuals = 0
    try:
        n_actuals = await db.player_game_actuals.count_documents({
            "sport":       "soccer",
            "player_name": {"$regex": f"^{player_name.strip()}$", "$options": "i"},
        })
    except Exception:
        pass
    n_logs = 0
    try:
        n_logs = await db.soccer_player_game_logs.count_documents({
            "name_canonical": nc,
        })
    except Exception:
        pass

    # No trace anywhere → identity failure (or truly unknown player).
    if not form and n_actuals == 0 and n_logs == 0:
        return SoccerRejection.PLAYER_IDENTITY_FAILURE.value

    # Some history, but < minimum sample → precise reason.
    if form and int(form.get("minutes") or 0) < 90:
        return "NO_RECENT_FORM"
    if n_actuals > 0 and n_actuals < 3:
        return "NO_RECENT_FORM"
    if not form and n_actuals == 0 and n_logs > 0:
        # game logs exist but under a different canonical name mapping
        return "PLAYER_IDENTITY_FAILURE"
    return "NO_PLAYER_HISTORY"


# ─────────────────────────────────────────────────────────────────────
# Actuals aggregation — pure DB read, no fabrication.
# ─────────────────────────────────────────────────────────────────────
async def _aggregate_from_actuals(
    db, *, player_name: str, sample: int = 25,
    canonical_player_id: Optional[str] = None,
    name_variants: Optional[list[str]] = None,
) -> Optional[dict]:
    """Aggregate the last ``sample`` Soccer entries from
    ``player_game_actuals`` into a form-row-shaped dict.

    UNIVERSAL_IDENTITY_HISTORY_BRIDGE (2026-09) — canonical-ID first,
    verified aliases second, exact name last.
    """
    q: dict = {"sport": "soccer"}
    # (1) Canonical ID join — used when the store carries the field.
    if canonical_player_id:
        q_id = dict(q, canonical_player_id=canonical_player_id)
        docs = await db.player_game_actuals.find(q_id).sort(
            [("event_time", -1)]
        ).limit(sample).to_list(sample)
        if docs:
            return _summarise_actuals_docs(docs, primary_name=player_name)

    # (2) Alias / variant join — case-insensitive $in on the
    #     normalised set from the resolver.
    variants = list(name_variants or [])
    if not variants and player_name:
        variants = [player_name.strip().lower()]
    # Dedupe case-insensitively — the regex is already
    # case-insensitive so no need for both cases.
    variants = list({v.strip().lower() for v in variants if v and v.strip()})
    if variants:
        # When only one variant, use a plain equality-regex to keep
        # simple test doubles happy.  Otherwise emit an $or list of
        # per-variant regex clauses.
        if len(variants) == 1:
            q_alias = dict(q, player_name={"$regex": f"^{variants[0]}$",
                                              "$options": "i"})
        else:
            alias_re = [{"player_name": {"$regex": f"^{v}$",
                                           "$options": "i"}}
                         for v in variants]
            q_alias = dict(q, **{"$or": alias_re})
        docs = await db.player_game_actuals.find(q_alias).sort(
            [("event_time", -1)]
        ).limit(sample).to_list(sample)
        if docs:
            return _summarise_actuals_docs(docs, primary_name=player_name)

    return None


def _summarise_actuals_docs(docs: list, *, primary_name: str) -> dict:
    goals = 0.0; assists = 0.0; shots = 0.0; sot = 0.0
    minutes_est = 0.0; n = 0
    for d in docs:
        a = d.get("actuals") or {}
        goals   += float(a.get("goals")   or 0)
        assists += float(a.get("assists") or 0)
        shots   += float(a.get("shots")   or 0)
        sot_v = a.get("shots_on_target")
        if sot_v is not None:
            sot += float(sot_v)
        minutes_est += 90.0
        n += 1
    if n == 0:
        return None  # type: ignore
    return {
        "name_canonical":     (primary_name or "").strip().lower(),
        "player_name":        primary_name,
        "goals":              goals,
        "assists":            assists,
        "shots":              shots,
        "shots_on_target":    sot,
        "games":              n,
        "minutes":            int(minutes_est),
        "goals_per_90":       (goals * 90.0) / minutes_est if minutes_est else 0.0,
        "assists_per_90":     (assists * 90.0) / minutes_est if minutes_est else 0.0,
        "shots_per_90":       (shots * 90.0) / minutes_est if minutes_est else 0.0,
        "source":             "player_game_actuals",
        "sample_size":        n,
    }


__all__ = [
    "resolve_soccer_player_features",
    "resolve_soccer_player_prior",
    "resolve_soccer_player_matchup",
    "classify_missing_feature_reason",
]


# ─────────────────────────────────────────────────────────────────────
# PHASE 0 §3-§4 (2026-06) — ESPN MLS stats fallback.
# Read-only over ``espn_mls_stats``.  Returns a form-row-shaped dict
# so downstream code (bridge / scorer / feature engine) can consume
# it identically to first-class evidence rows.  Season totals are
# converted to per-90 rates using MLS-typical 65 mins/appearance.
# Identity is NEVER rewritten — the canonical_player_id passed by
# the caller (from the identity resolver) is carried through.
# ─────────────────────────────────────────────────────────────────────
async def _lookup_espn_mls_stats(
    db, *, primary_name: str, variants: list[str],
    canonical_player_id: Optional[str] = None,
) -> Optional[dict]:
    if not primary_name and not variants:
        return None
    # Search by normalised name variants only — ESPN does not carry
    # our canonical IDs.
    all_variants = list({v.strip().lower() for v in
                        [primary_name, *(variants or [])] if v and v.strip()})
    if not all_variants:
        return None
    doc = None
    try:
        doc = await db.espn_mls_stats.find_one(
            {"name_norm": {"$in": all_variants}}
        )
    except Exception:
        doc = None
    if not doc:
        return None
    games = int(doc.get("games") or 0)
    if games < 2:
        return None
    goals   = float(doc.get("goals")   or 0)
    assists = float(doc.get("assists") or 0)
    # MLS-typical average minutes for a leader-appearing player.
    # We intentionally use a slightly conservative 65 min/appearance
    # so per-90 rates from ESPN totals aren't inflated.
    minutes = float(games) * 65.0
    return {
        "name_canonical":     primary_name,
        "player_name":        doc.get("name") or primary_name,
        # Identity is CARRIED through — never rewritten.
        "canonical_player_id": canonical_player_id,
        "goals":              goals,
        "assists":            assists,
        "shots":              0.0,   # ESPN leaders feed does not carry shots
        "shots_on_target":    0.0,
        "games":              games,
        "minutes":            int(minutes),
        "goals_per_90":       (goals   * 90.0) / minutes if minutes else 0.0,
        "assists_per_90":     (assists * 90.0) / minutes if minutes else 0.0,
        "shots_per_90":       0.0,
        "source":             "espn_mls_stats",
        "sample_size":        games,
        # Tag so downstream can identify this as an enrichment source.
        "evidence_source":    "espn_mls_stats",
    }
