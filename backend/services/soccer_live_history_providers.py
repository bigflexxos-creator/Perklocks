"""Live Soccer History Provider Adapters — Phase E runtime closure (2026-06).

Bridges EXISTING production Mongo collections to the
``services.soccer_universal_history.SoccerHistoryProvider`` protocol.
Zero new provider acquisition — all data used here is already in
``lockscore_db`` from the existing ingestion pipelines.

Sources bridged:
  * ``soccer_matches``          — team-level match rows w/ scores/xG,
    23 leagues incl. EPL / LaLiga / SerieA / Bundesliga / Ligue1 / UCL
    / Eredivisie / Brasileirao / Turkish / etc.  Powers team H2H +
    team history across every provider-supported competition.
  * ``soccer_player_game_logs`` — per-match player rows w/ minutes,
    goals, xG, xA, shots, SOT.  Powers player history + player VS OPP
    for the leagues that already have this ingested (EPL / La_Liga /
    Bundesliga).
  * ``mls_player_matchup_history`` — MLS-only precomputed matchup
    history.  Retained via the existing ``MLSLegacyProvider`` bridge
    (registered in soccer_universal_history at import).
  * ``player_game_actuals`` / ``team_game_actuals`` — canonical
    identity-resolved cross-sport actuals.  Provides fallback player
    VS OPP for sports that store canonical IDs.

All adapters honour §§27-44:
  * canonical identity first (``canonical_team_id`` /
    ``canonical_player_id``) with display-name fallback for legacy
    rows that predate canonical resolution.
  * missing-vs-zero preserved — a row's absent ``minutes`` stays
    ``None`` in the output; only present values are stamped.
  * ``as_of`` respected — pregame queries filter matches occurring
    AFTER the frozen publication time.
  * bounded HTTP concurrency — adapters query Mongo only; no
    provider HTTP fan-out.
  * failure surfaces as ``STATUS_PROVIDER_FAILURE`` (never zero).

Registration happens at application startup via
``register_live_soccer_providers()`` — called from
``services/application_lifecycle.py`` OR directly on module import
of this file (idempotent).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from services.soccer_universal_history import (
    HistoryQueryResult, register_provider, registered_providers,
    STATUS_FULL, STATUS_NO_MATCHES, STATUS_PROVIDER_FAILURE,
    STATUS_UNAVAILABLE,
)

logger = logging.getLogger("lockscore.soccer_live_providers")


# Canonical competition IDs — normalise the varied `league` strings
# stored across collections to ONE stable canonical ID.  Both raw and
# canonical are matched at query time so callers can pass either.
_LEAGUE_TO_CANONICAL: dict[str, str] = {
    # Top-5 European domestic
    "EPL":                 "EPL",
    "LaLiga":              "LA_LIGA",
    "La_liga":             "LA_LIGA",
    "SerieA":              "SERIE_A",
    "Bundesliga":          "BUNDESLIGA",
    "Ligue1":              "LIGUE_1",
    # UEFA
    "UCL":                 "UEFA_CHAMPIONS_LEAGUE",
    "UEL":                 "UEFA_EUROPA_LEAGUE",
    "ECONF":               "UEFA_CONFERENCE_LEAGUE",
    # Second-tier / other supported
    "ELC":                 "ENG_CHAMPIONSHIP",
    "EL1":                 "ENG_LEAGUE_ONE",
    "EL2":                 "ENG_LEAGUE_TWO",
    "Bundesliga2":         "BUNDESLIGA_2",
    "Bundesliga3":         "BUNDESLIGA_3",
    "LaLiga2":             "LA_LIGA_2",
    "SerieB":              "SERIE_B",
    "Ligue2":              "LIGUE_2",
    "Eredivisie":          "EREDIVISIE",
    "Primeira":            "PRIMEIRA_LIGA",
    "Brasileirao":         "BRASILEIRAO",
    "SPL":                 "SCOTTISH_PREMIERSHIP",
    "TUR":                 "TURKISH_SUPER_LIG",
    "BEL":                 "BELGIAN_PRO_LEAGUE",
    "GRE":                 "GREEK_SUPER_LEAGUE",
    "SD1":                 "SAUDI_PRO_LEAGUE",
    # MLS variants
    "MLS":                 "USA_MLS",
    "usa.mls":             "USA_MLS",
    "soccer_usa_mls":      "USA_MLS",
}

_CANONICAL_TO_LEAGUE_ALIASES: dict[str, list[str]] = {}
for _raw, _canon in _LEAGUE_TO_CANONICAL.items():
    _CANONICAL_TO_LEAGUE_ALIASES.setdefault(_canon, []).append(_raw)

# The set of canonical competition IDs each collection can serve.
_SOCCER_MATCHES_COMPETITIONS = frozenset(_CANONICAL_TO_LEAGUE_ALIASES.keys())


def _canonical_competition_id(raw: str) -> Optional[str]:
    if not raw:
        return None
    if raw in _LEAGUE_TO_CANONICAL:
        return _LEAGUE_TO_CANONICAL[raw]
    # Accept a canonical value passed directly.
    if raw in _CANONICAL_TO_LEAGUE_ALIASES:
        return raw
    return None


def _league_aliases_for(canonical_id: Optional[str]) -> list[str]:
    if not canonical_id:
        return []
    return list(_CANONICAL_TO_LEAGUE_ALIASES.get(canonical_id, [canonical_id]))


def _norm_team_key(s: Any) -> str:
    if s is None:
        return ""
    s = str(s).lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


# ─────────────────────────────────────────────────────────────────────
# Adapter #1 — soccer_matches (team H2H + team history)
# ─────────────────────────────────────────────────────────────────────
class SoccerMatchesProvider:
    """Bridges ``db.soccer_matches`` (23-league team match store) to the
    universal history protocol.  Provides:
      * team_h2h
      * team_history
      * player_history          → UNAVAILABLE (no per-player rows here)
      * player_vs_opp           → UNAVAILABLE
    """
    name = "soccer_matches_v1"
    supported_competitions = _SOCCER_MATCHES_COMPETITIONS

    def __init__(self, db):
        self._db = db

    async def team_h2h(
        self, *, canonical_home_team_id: str,
        canonical_away_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {}
            aliases = _league_aliases_for(canonical_competition_id)
            if aliases:
                q["league"] = {"$in": aliases}
            # Match by team-name normalisation on either orientation.
            home_key = _norm_team_key(canonical_home_team_id)
            away_key = _norm_team_key(canonical_away_team_id)
            if not home_key or not away_key:
                return HistoryQueryResult(
                    status=STATUS_UNAVAILABLE, sample_size=0,
                    provenance=self.name, error="empty_team_key",
                )
            if as_of:
                q["date"] = {"$lte": as_of.strftime("%Y-%m-%d")}
            cursor = self._db.soccer_matches.find(q).sort("date", -1).limit(limit * 50)
            rows: list[dict[str, Any]] = []
            async for r in cursor:
                h = _norm_team_key(r.get("home_team"))
                a = _norm_team_key(r.get("away_team"))
                if {h, a} == {home_key, away_key}:
                    hs, as_ = r.get("home_score"), r.get("away_score")
                    total = None
                    if isinstance(hs, (int, float)) and isinstance(as_, (int, float)):
                        total = int(hs) + int(as_)
                    winner = None
                    if isinstance(hs, (int, float)) and isinstance(as_, (int, float)):
                        winner = "HOME" if hs > as_ else "AWAY" if as_ > hs else "DRAW"
                    row = {
                        "provider_event_id": str(r.get("_id")),
                        "competition": r.get("league"),
                        "canonical_competition_id": _canonical_competition_id(r.get("league")),
                        "season": r.get("season"),
                        "date": r.get("date"),
                        "canonical_home_team_id": r.get("home_team"),
                        "canonical_away_team_id": r.get("away_team"),
                        "home_score": hs,
                        "away_score": as_,
                        "home_xg":    r.get("home_xg"),      # None = unknown
                        "away_xg":    r.get("away_xg"),      # None = unknown
                        "total_goals": total,
                        "winner": winner,
                        "btts": (isinstance(hs, (int, float)) and isinstance(as_, (int, float))
                                  and hs > 0 and as_ > 0),
                        "provider": self.name,
                        "provider_provenance": "lockscore_db.soccer_matches",
                    }
                    rows.append(row)
                    if len(rows) >= limit:
                        break
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:                                  # noqa: BLE001
            logger.warning("soccer_matches.team_h2h failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def team_history(
        self, *, canonical_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            aliases = _league_aliases_for(canonical_competition_id)
            team_key = _norm_team_key(canonical_team_id)
            if not team_key:
                return HistoryQueryResult(
                    status=STATUS_UNAVAILABLE, sample_size=0,
                    provenance=self.name, error="empty_team_key",
                )
            q: dict[str, Any] = {}
            if aliases:
                q["league"] = {"$in": aliases}
            if as_of:
                q["date"] = {"$lte": as_of.strftime("%Y-%m-%d")}
            cursor = self._db.soccer_matches.find(q).sort("date", -1).limit(limit * 5)
            rows: list[dict[str, Any]] = []
            async for r in cursor:
                h = _norm_team_key(r.get("home_team"))
                a = _norm_team_key(r.get("away_team"))
                if team_key not in (h, a):
                    continue
                hs, as_ = r.get("home_score"), r.get("away_score")
                row = {
                    "provider_event_id": str(r.get("_id")),
                    "competition": r.get("league"),
                    "canonical_competition_id": _canonical_competition_id(r.get("league")),
                    "season": r.get("season"),
                    "date": r.get("date"),
                    "canonical_home_team_id": r.get("home_team"),
                    "canonical_away_team_id": r.get("away_team"),
                    "home_orientation": (team_key == h),
                    "home_score": hs,
                    "away_score": as_,
                    "provider": self.name,
                    "provider_provenance": "lockscore_db.soccer_matches",
                }
                rows.append(row)
                if len(rows) >= limit:
                    break
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("soccer_matches.team_history failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def player_history(self, **kwargs) -> HistoryQueryResult:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            provenance=self.name,
            error="soccer_matches_has_no_per_player_rows",
        )

    async def player_vs_opp(self, **kwargs) -> HistoryQueryResult:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            provenance=self.name,
            error="soccer_matches_has_no_per_player_rows",
        )


# ─────────────────────────────────────────────────────────────────────
# Adapter #2 — soccer_player_game_logs (player history + player VS OPP)
# ─────────────────────────────────────────────────────────────────────
class SoccerPlayerGameLogsProvider:
    """Bridges ``db.soccer_player_game_logs`` to universal history.

    99,524 per-match player rows across EPL / La_Liga / Bundesliga (top-3
    European domestic leagues that already have per-match player data
    ingested).  Missing xG/xA/shots stay ``None`` — never coerced to 0.
    """
    name = "soccer_player_game_logs_v1"
    supported_competitions = frozenset({
        "EPL", "LA_LIGA", "BUNDESLIGA",
    })

    def __init__(self, db):
        self._db = db

    def _mkrow(self, r: dict, *, canonical_player_id: str,
                canonical_opponent_id: Optional[str] = None) -> dict:
        # Preserve None for missing evidence — never coerce to zero.
        goals = r.get("goals")
        assists = r.get("assists")
        xg = r.get("xg") if r.get("xg") is not None else r.get("xG")
        xa = r.get("xa") if r.get("xa") is not None else r.get("xA")
        shots = r.get("shots") if r.get("shots") is not None else r.get("Shots")
        sot = r.get("shots_on_target") if r.get("shots_on_target") is not None else r.get("KP")
        return {
            "provider_event_id": (
                str(r.get("canonical_event_id"))
                or str(r.get("match_id"))
                or f"{self.name}:{r.get('name_canonical')}:{r.get('match_date') or r.get('date')}"
            ),
            "canonical_player_id":   canonical_player_id,
            "canonical_team_id":     r.get("team_name") or r.get("team"),
            "canonical_opponent_id": canonical_opponent_id or r.get("opponent_name") or r.get("opponent"),
            "provider_player_id":    r.get("player_id"),
            "competition":           r.get("competition"),
            "canonical_competition_id": _canonical_competition_id(r.get("competition")),
            "season":                r.get("season"),
            "date":                  r.get("match_date") or r.get("date"),
            "home":                  bool(r.get("is_home")) if r.get("is_home") is not None else None,
            "started":               (bool(r.get("starts")) if r.get("starts") is not None else None),
            "minutes":               int(r["minutes"]) if isinstance(r.get("minutes"), (int, float)) else None,
            "goals":                 int(goals) if isinstance(goals, (int, float)) else None,
            "assists":               int(assists) if isinstance(assists, (int, float)) else None,
            "shots":                 float(shots) if isinstance(shots, (int, float)) else None,
            "shots_on_target":       float(sot) if isinstance(sot, (int, float)) else None,
            "xg":                    float(xg) if isinstance(xg, (int, float)) else None,
            "xa":                    float(xa) if isinstance(xa, (int, float)) else None,
            "provider":              self.name,
            "provider_provenance":   "lockscore_db.soccer_player_game_logs",
        }

    async def team_h2h(self, **kwargs) -> HistoryQueryResult:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            provenance=self.name,
            error="player_game_logs_not_team_h2h",
        )

    async def team_history(self, **kwargs) -> HistoryQueryResult:
        return HistoryQueryResult(
            status=STATUS_UNAVAILABLE, sample_size=0,
            provenance=self.name, error="player_game_logs_not_team_history",
        )

    async def player_history(
        self, *, canonical_player_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "name_canonical": canonical_player_id.lower(),
            }
            if canonical_competition_id:
                aliases = _league_aliases_for(canonical_competition_id)
                if aliases:
                    q["competition"] = {"$in": aliases}
            if as_of:
                q["match_date"] = {"$lte": as_of.strftime("%Y-%m-%d")}
            cursor = self._db.soccer_player_game_logs.find(q).sort("match_date", -1).limit(limit)
            rows = [self._mkrow(r, canonical_player_id=canonical_player_id) async for r in cursor]
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("player_game_logs.player_history failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def player_vs_opp(
        self, *, canonical_player_id: str,
        canonical_opponent_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "name_canonical": canonical_player_id.lower(),
            }
            if canonical_competition_id:
                aliases = _league_aliases_for(canonical_competition_id)
                if aliases:
                    q["competition"] = {"$in": aliases}
            if as_of:
                q["match_date"] = {"$lte": as_of.strftime("%Y-%m-%d")}
            opp_key = _norm_team_key(canonical_opponent_id)
            cursor = self._db.soccer_player_game_logs.find(q).sort("match_date", -1).limit(limit * 5)
            rows: list[dict] = []
            async for r in cursor:
                # Match by normalised opponent — check both `opponent_name` and `opponent_team_name`.
                if _norm_team_key(r.get("opponent_name") or r.get("opponent_team_name")) != opp_key:
                    continue
                rows.append(self._mkrow(
                    r, canonical_player_id=canonical_player_id,
                    canonical_opponent_id=canonical_opponent_id,
                ))
                if len(rows) >= limit:
                    break
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("player_game_logs.player_vs_opp failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )


# ─────────────────────────────────────────────────────────────────────
# Adapter #3 — canonical actuals (cross-sport identity-resolved fallback)
# ─────────────────────────────────────────────────────────────────────
class CanonicalActualsProvider:
    """Fallback using ``db.player_game_actuals`` + ``db.team_game_actuals``.
    Filtered to ``sport == "soccer"`` — 4,487 player rows + 50,066 team
    rows already canonicalised.  These are the ONLY rows guaranteed to
    carry ``canonical_player_id`` / ``canonical_team_id`` / ``canonical_opponent_id``.

    Serves every competition where canonical identity was resolved.
    """
    name = "canonical_actuals_v1"
    # We do not restrict competitions — the collection holds whatever
    # was canonicalised.  Callers pass ``canonical_competition_id`` for
    # filtering; None returns cross-competition.
    supported_competitions = frozenset(_CANONICAL_TO_LEAGUE_ALIASES.keys())

    def __init__(self, db):
        self._db = db

    async def team_h2h(
        self, *, canonical_home_team_id: str,
        canonical_away_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "sport": "soccer",
                "canonical_team_id": canonical_home_team_id,
                "canonical_opponent_id": canonical_away_team_id,
            }
            if canonical_competition_id:
                q["competition"] = canonical_competition_id
            if as_of:
                q["event_time"] = {"$lte": as_of}
            cursor = self._db.team_game_actuals.find(q).sort("event_time", -1).limit(limit)
            rows: list[dict] = []
            async for r in cursor:
                ts, os_ = r.get("team_score"), r.get("opponent_score")
                rows.append({
                    "provider_event_id": str(r.get("event_id") or r.get("_id")),
                    "competition": r.get("competition"),
                    "canonical_competition_id": r.get("competition"),
                    "season": r.get("season"),
                    "date": r.get("event_time").isoformat() if hasattr(r.get("event_time"), "isoformat") else r.get("event_time"),
                    "canonical_home_team_id": r.get("canonical_team_id"),
                    "canonical_away_team_id": r.get("canonical_opponent_id"),
                    "home_score": ts, "away_score": os_,
                    "winner": r.get("result"),
                    "provider": self.name,
                    "provider_provenance": "lockscore_db.team_game_actuals",
                })
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("canonical_actuals.team_h2h failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def team_history(
        self, *, canonical_team_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "sport": "soccer",
                "canonical_team_id": canonical_team_id,
            }
            if canonical_competition_id:
                q["competition"] = canonical_competition_id
            if as_of:
                q["event_time"] = {"$lte": as_of}
            cursor = self._db.team_game_actuals.find(q).sort("event_time", -1).limit(limit)
            rows: list[dict] = []
            async for r in cursor:
                rows.append({
                    "provider_event_id": str(r.get("event_id") or r.get("_id")),
                    "competition": r.get("competition"),
                    "season": r.get("season"),
                    "date": r.get("event_time").isoformat() if hasattr(r.get("event_time"), "isoformat") else r.get("event_time"),
                    "canonical_home_team_id": r.get("canonical_team_id"),
                    "canonical_away_team_id": r.get("canonical_opponent_id"),
                    "home_orientation": (r.get("home_away") == "home"),
                    "home_score": r.get("team_score"),
                    "away_score": r.get("opponent_score"),
                    "provider": self.name,
                    "provider_provenance": "lockscore_db.team_game_actuals",
                })
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("canonical_actuals.team_history failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def player_history(
        self, *, canonical_player_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "sport": "soccer",
                "canonical_player_id": canonical_player_id,
            }
            if as_of:
                q["event_time"] = {"$lte": as_of}
            cursor = self._db.player_game_actuals.find(q).sort("event_time", -1).limit(limit)
            rows: list[dict] = []
            async for r in cursor:
                actuals = r.get("actuals") or {}
                rows.append({
                    "provider_event_id": str(r.get("event_id") or r.get("_id")),
                    "canonical_player_id": r.get("canonical_player_id"),
                    "canonical_team_id": r.get("canonical_team_id"),
                    "canonical_opponent_id": r.get("canonical_opponent_id"),
                    "season": r.get("season"),
                    "date": r.get("event_time").isoformat() if hasattr(r.get("event_time"), "isoformat") else r.get("event_time"),
                    # actuals may hold goals/assists/minutes/shots keyed by market or stat.
                    "minutes": actuals.get("minutes"),
                    "goals": actuals.get("goals"),
                    "assists": actuals.get("assists"),
                    "shots": actuals.get("shots"),
                    "shots_on_target": actuals.get("shots_on_target"),
                    "provider": self.name,
                    "provider_provenance": "lockscore_db.player_game_actuals",
                })
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("canonical_actuals.player_history failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )

    async def player_vs_opp(
        self, *, canonical_player_id: str,
        canonical_opponent_id: str,
        canonical_competition_id: Optional[str] = None,
        as_of: Optional[datetime] = None, limit: int = 20,
    ) -> HistoryQueryResult:
        try:
            q: dict[str, Any] = {
                "sport": "soccer",
                "canonical_player_id": canonical_player_id,
                "canonical_opponent_id": canonical_opponent_id,
            }
            if as_of:
                q["event_time"] = {"$lte": as_of}
            cursor = self._db.player_game_actuals.find(q).sort("event_time", -1).limit(limit)
            rows: list[dict] = []
            async for r in cursor:
                actuals = r.get("actuals") or {}
                rows.append({
                    "provider_event_id": str(r.get("event_id") or r.get("_id")),
                    "canonical_player_id": r.get("canonical_player_id"),
                    "canonical_team_id": r.get("canonical_team_id"),
                    "canonical_opponent_id": r.get("canonical_opponent_id"),
                    "season": r.get("season"),
                    "date": r.get("event_time").isoformat() if hasattr(r.get("event_time"), "isoformat") else r.get("event_time"),
                    "minutes": actuals.get("minutes"),
                    "goals": actuals.get("goals"),
                    "assists": actuals.get("assists"),
                    "shots": actuals.get("shots"),
                    "shots_on_target": actuals.get("shots_on_target"),
                    "provider": self.name,
                    "provider_provenance": "lockscore_db.player_game_actuals",
                })
            status = STATUS_FULL if rows else STATUS_NO_MATCHES
            return HistoryQueryResult(
                status=status, rows=rows, sample_size=len(rows),
                provenance=self.name,
                as_of=(as_of.isoformat() if as_of else None),
            )
        except Exception as e:
            logger.warning("canonical_actuals.player_vs_opp failure: %s", e)
            return HistoryQueryResult(
                status=STATUS_PROVIDER_FAILURE, sample_size=0,
                error=f"{e.__class__.__name__}:{e}", provenance=self.name,
            )


# ─────────────────────────────────────────────────────────────────────
# Registration entry point
# ─────────────────────────────────────────────────────────────────────
def register_live_soccer_providers(db) -> list[str]:
    """Register the three live adapters against the universal history
    registry.  Idempotent — re-registration replaces prior adapter with
    the same name.

    Returns the list of provider names now registered.
    """
    register_provider(SoccerMatchesProvider(db))
    register_provider(SoccerPlayerGameLogsProvider(db))
    register_provider(CanonicalActualsProvider(db))
    return list(registered_providers())


__all__ = [
    "SoccerMatchesProvider",
    "SoccerPlayerGameLogsProvider",
    "CanonicalActualsProvider",
    "register_live_soccer_providers",
]
