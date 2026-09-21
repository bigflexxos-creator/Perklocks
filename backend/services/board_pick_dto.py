"""BoardPickDTO — Gate 2 lite-payload projection (2026-06).

Purpose
-------

Reduce the /api/picks/today?lite=true payload to only what the board
list actually renders while preserving canonical betting truth.

Baseline (before this projection, measured on prod Preview 2026-06-20):

    total bytes     2 295 363     (2.29 MB)
    picks           744
    avg bytes/pick  3 124
    p95 bytes/pick  3 909

Top contributors that this projection removes / trims:

    published_pick_contract       ~663 B/pick   duplicate of flattened truth
    pick_rationale                ~293 B/pick   trimmed to summary / lean / top
    why_this_pick                 ~264 B/pick   trimmed to 2 board-safe bullets
    player_form                   ~187 B/pick   trimmed to streak-safe subset
    locks_eligibility             ~140 B/pick   internal
    home_meta / away_meta         ~185 B/pick   kept (abbrev + logo used)
    player_meta                   ~ 73 B/pick   trimmed to headshot-safe subset
    external_id                   ~ 76 B/pick   detail-only
    apex_blockers                 ~ 62 B/pick   kept (max 2 items, first-blocker only)
    truth_fingerprint             ~ 26 B/pick   internal
    board_version / pick_date /
    model_version / publication_state  ~63 B    envelope-only

Canonical fields ALWAYS retained (frontend renders these):

    id, canonical_pick_id, sport, league, event, event_time,
    commence_time, market, selection, side, line, book_odds,
    lock_score, published_lock_score, win_probability,
    edge_percent, confidence, grade, tier_v2, signal_score,
    signal_rank, matchup_grade, matchup_score,
    home_team, away_team, player_name, player_team,
    imagery (player_headshot_url, home_meta.logo, away_meta.logo),
    is_apex, elite_player, sim_win_probability,
    sim_disagreement_with_model

The DTO is a PROJECTION (drop / trim in place); it does NOT rename or
change semantics of any preserved field.
"""
from __future__ import annotations

from typing import Any, Iterable

# Whole-field drop set — proven unused by board list renderers.
_DROP_FIELDS: frozenset[str] = frozenset({
    # Big duplicate/internal metadata
    "published_pick_contract",
    "published_pick_contract_provenance",
    "truth_fingerprint",
    "locks_eligibility",
    "bvp_history",
    "external_id",
    "publication_state",

    # Envelope-level (present once at response root — pointless per pick)
    "pick_date",
    "model_version",
    "board_version",

    # Redundant duplicates (canonical field already retained)
    "published_grade",           # dup: grade
    "published_edge",            # dup: edge_percent
    "published_probability",     # dup: win_probability
    "lock_score_v2",             # dup: lock_score (kept as-is)
    # implied_probability is now RETAINED (Phase A root closure) — the
    # frontend rendered "undefined%" when this field was dropped because
    # no downstream derivation was performed.  Truth stays on the wire.
})

# Player-form fields the streak badge uses.
_PLAYER_FORM_KEEP: frozenset[str] = frozenset({
    "n_picks", "current_streak", "streak_source",
    "games_logged", "trend", "consistency",
})

# Player-meta fields the headshot renders.
_PLAYER_META_KEEP: frozenset[str] = frozenset({
    "display_name", "team", "headshot_url",
})

# Team-meta fields the board card renders.
_TEAM_META_KEEP: frozenset[str] = frozenset({
    "logo", "abbrev", "color", "alt_color",
})

# Pick-rationale sub-fields the board card renders (rest is detail).
_RATIONALE_KEEP: frozenset[str] = frozenset({
    "summary", "lean", "top",
})

_WHY_MAX_BULLETS = 2
_WHY_MAX_CHARS = 140
_APEX_MAX_BLOCKERS = 2


def _trim_player_form(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    return {k: v[k] for k in _PLAYER_FORM_KEEP if k in v}


def _trim_player_meta(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    return {k: v[k] for k in _PLAYER_META_KEEP if k in v}


def _trim_team_meta(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    return {k: v[k] for k in _TEAM_META_KEEP if k in v}


def _trim_rationale(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    return {k: v[k] for k in _RATIONALE_KEEP if k in v}


def _trim_why(v: Any) -> Any:
    if not isinstance(v, list):
        return v
    out: list[str] = []
    for item in v:
        if not isinstance(item, str):
            continue
        if item.startswith("__"):        # internal metadata leaks
            continue
        s = item if len(item) <= _WHY_MAX_CHARS else item[: _WHY_MAX_CHARS - 1] + "…"
        out.append(s)
        if len(out) >= _WHY_MAX_BULLETS:
            break
    return out


def _trim_apex_blockers(v: Any) -> Any:
    if not isinstance(v, list):
        return v
    return v[: _APEX_MAX_BLOCKERS]


def project_board_dto(pick: dict) -> dict:
    """Return a *shallow-copy* projection of ``pick`` shaped for the board
    list.  Preserves canonical betting truth verbatim; only removes /
    trims proven detail-only fields.

    Phase A root closure (2026-06):
      * ``implied_probability`` is now always retained.  When the
        upstream pick lacks it BUT carries a valid ``book_odds`` value,
        it is derived from odds via the canonical
        ``probability_units.implied_probability_from_odds`` helper.
        This closes the frontend ``undefined%`` bug at the DTO layer.
    """
    out: dict[str, Any] = {}
    for k, v in pick.items():
        if k in _DROP_FIELDS:
            continue
        if k == "player_form":
            out[k] = _trim_player_form(v)
        elif k == "player_meta":
            out[k] = _trim_player_meta(v)
        elif k in ("home_meta", "away_meta"):
            out[k] = _trim_team_meta(v)
        elif k == "pick_rationale":
            out[k] = _trim_rationale(v)
        elif k == "why_this_pick":
            out[k] = _trim_why(v)
        elif k == "why_not_this_pick":
            out[k] = _trim_why(v)
        elif k == "apex_blockers":
            out[k] = _trim_apex_blockers(v)
        else:
            out[k] = v
    # ── implied_probability derive-if-missing ──────────────────────
    # Frontend must NEVER render "undefined%".  If the pick has valid
    # book_odds but no implied_probability, derive it here (percent
    # scale — the frontend displays it as `${implied_probability}%`).
    ip = out.get("implied_probability")
    _needs_derive = (
        ip is None or ip == "" or (isinstance(ip, float) and (ip != ip))
    )
    if _needs_derive:
        odds = out.get("book_odds")
        try:
            from services.probability_units import implied_probability_from_odds
            frac = implied_probability_from_odds(odds)
            if frac is not None:
                out["implied_probability"] = round(frac * 100.0, 2)
        except Exception:
            pass
    return out


def project_board_dto_list(picks: Iterable[dict]) -> list[dict]:
    return [project_board_dto(p) for p in picks]


__all__ = ["project_board_dto", "project_board_dto_list"]
