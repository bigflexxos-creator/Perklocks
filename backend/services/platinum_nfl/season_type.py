"""Automatic NFL season-type detection (Block 2B.1A §10).

CONTRACT
────────
Derived STRICTLY from authoritative event metadata:
    * NFL schedule week + season-type tag
    * ``sport_key = "americanfootball_nfl_preseason"`` vs ``americanfootball_nfl``
    * Explicit ``season_type`` / ``game_type`` field on the event
    * Postseason detection via ``week`` >= 19 for the 2021+ NFL 17-game format
      or via explicit ``game_type`` in {"WC","DIV","CONF","SB"}.

If the season type is genuinely unresolved after checking every
available signal, we return ``SeasonType.UNKNOWN`` and the caller
MUST fail closed.

NO manual toggle.  NO admin override.  NO redeploy.  NO code edits
per week.  This module is deterministic function of the event
metadata it receives.

Regular-season vs postseason boundaries (2021+ 17-game format):
    Weeks 1..18       → REGULAR_SEASON
    Weeks 19..22      → POSTSEASON (WC/DIV/CONF/SB)
    Preseason weeks   → PRESEASON

Preseason isolation (§9, §12):
    ``classify_season_type`` is the ONLY authoritative source of
    season truth.  Downstream code MUST tag every stored artifact
    (predictions, sim rows, actuals, calibration) with this label
    so preseason samples never silently mix into regular-season
    calibration.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


class SeasonType(str, enum.Enum):
    PRESEASON      = "PRESEASON"
    REGULAR_SEASON = "REGULAR_SEASON"
    POSTSEASON     = "POSTSEASON"
    UNKNOWN        = "UNKNOWN"


# ── Explicit game-type tags (nflfastR / MLB StatsAPI / The Odds API) ─
_POSTSEASON_TAGS = {"post", "postseason", "wc", "div", "conf", "sb",
                    "wildcard", "divisional", "conference", "super_bowl",
                    "super bowl", "superbowl", "playoff", "playoffs"}
_PRESEASON_TAGS  = {"pre", "preseason"}
_REGULAR_TAGS    = {"reg", "regular", "regular_season"}


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def is_preseason(x: Any) -> bool:
    return classify_season_type(x) is SeasonType.PRESEASON


def is_regular_season(x: Any) -> bool:
    return classify_season_type(x) is SeasonType.REGULAR_SEASON


def is_postseason(x: Any) -> bool:
    return classify_season_type(x) is SeasonType.POSTSEASON


def classify_season_type(event_metadata: Any) -> SeasonType:
    """Return the season type for an NFL event.

    Accepts either a dict-shaped event payload OR a raw sport-key
    string.  Returns ``UNKNOWN`` if no authoritative signal is present.

    Precedence:
        1. Explicit ``game_type`` / ``season_type`` field.
        2. ``sport_key`` string (The Odds API).
        3. ``week`` + ``season`` numeric hints.
        4. UNKNOWN (fail closed).
    """
    if isinstance(event_metadata, str):
        # Raw sport_key string.
        return _from_sport_key(event_metadata)

    if not isinstance(event_metadata, dict):
        return SeasonType.UNKNOWN

    # ── (1) Explicit tags ────────────────────────────────────────
    for key in ("season_type", "game_type", "seasonType", "gameType"):
        v = _lower(event_metadata.get(key))
        if not v:
            continue
        if v in _POSTSEASON_TAGS or "post" in v or "playoff" in v:
            return SeasonType.POSTSEASON
        if v in _PRESEASON_TAGS or "pre" in v:
            return SeasonType.PRESEASON
        if v in _REGULAR_TAGS or "reg" in v:
            return SeasonType.REGULAR_SEASON

    # ── (2) Sport key (The Odds API convention) ──────────────────
    sk = _lower(event_metadata.get("sport_key"))
    if sk:
        r = _from_sport_key(sk)
        if r is not SeasonType.UNKNOWN:
            return r

    # ── (3) Week + season numeric hints (2021+ 17-game format) ───
    wk = event_metadata.get("week")
    if isinstance(wk, (int, float)):
        wki = int(wk)
        # nflfastR convention: preseason weeks are negative or 0..4
        # when stored under a preseason season_type; without such
        # a season_type tag, negative weeks unambiguously = PRESEASON.
        if wki < 0:
            return SeasonType.PRESEASON
        if 1 <= wki <= 18:
            return SeasonType.REGULAR_SEASON
        if 19 <= wki <= 22:
            return SeasonType.POSTSEASON

    # ── (4) Commence-time heuristic BLOCKED ──────────────────────
    # We intentionally do NOT infer season type from calendar month.
    # NFL preseason (Aug), regular (Sep-Jan), postseason (Jan-Feb) —
    # month-based inference is unreliable across timezones and
    # would fail on Thu-night Week 1 that starts in early Sept
    # vs preseason Aug 31 finales.  Fail closed → UNKNOWN.
    return SeasonType.UNKNOWN


def _from_sport_key(sk: str) -> SeasonType:
    sk = _lower(sk)
    if not sk:
        return SeasonType.UNKNOWN
    if "preseason" in sk or sk.endswith("_pre"):
        return SeasonType.PRESEASON
    if "playoffs" in sk or "postseason" in sk or "_post" in sk:
        return SeasonType.POSTSEASON
    if "americanfootball_nfl" in sk or sk in {"nfl"}:
        # Bare NFL sport key without "preseason" or "postseason" =>
        # regular season by convention (The Odds API always uses
        # a distinct preseason/playoffs key when applicable).
        return SeasonType.REGULAR_SEASON
    return SeasonType.UNKNOWN


# ═══════════════════════════════════════════════════════════════════
# Preseason isolation guards (§9, §12)
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SeasonTaggedRow:
    """Immutable envelope so callers cannot silently store preseason
    outputs into regular-season buckets.  A stored row (prediction,
    sim output, actual result) MUST carry an authoritative
    ``season_type`` tag; downstream calibration/history joins that
    ignore the tag are considered a defect.
    """
    season_type: SeasonType
    payload:     Any

    def is_preseason(self) -> bool:
        return self.season_type is SeasonType.PRESEASON


def enforce_no_preseason_contamination(
        rows: list, *, allowed: SeasonType,
) -> list:
    """Return only rows whose ``season_type`` matches ``allowed``.
    Non-matching rows are dropped defensively.  If ``allowed`` is
    REGULAR_SEASON, PRESEASON rows are FILTERED — never silently
    included in regular-season calibration.

    Any row without an explicit ``season_type`` field is DROPPED
    (fail-closed) — the caller must tag rows before persistence.
    """
    out: list = []
    for r in rows:
        st: Optional[SeasonType] = None
        if isinstance(r, SeasonTaggedRow):
            st = r.season_type
        elif isinstance(r, dict):
            v = r.get("season_type")
            if isinstance(v, SeasonType):
                st = v
            elif isinstance(v, str):
                try:
                    st = SeasonType(v)
                except ValueError:
                    st = None
        if st is None:
            continue
        if st is allowed:
            out.append(r)
    return out


__all__ = [
    "SeasonType",
    "classify_season_type",
    "is_preseason", "is_regular_season", "is_postseason",
    "SeasonTaggedRow",
    "enforce_no_preseason_contamination",
]
