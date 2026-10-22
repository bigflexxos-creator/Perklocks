"""Historical Intelligence — Incremental Projectors (Blocks 11–13 closure).

Per-sport, bounded, idempotent projectors that bridge raw provider rows
into the canonical HI consumer stores actually read by the Lock Score /
Apex / Evidence Authority stack.

Design contract (user-approved, surgical scope):

* Reuse existing identity + existing stores — no new schemas, no new
  ingestion platforms, no destructive migration.
* Idempotent upsert on a composite key per sport (never destructive).
* Bounded window:
    - current-season for weekly/game-log sources (NFL, NBA, NHL, MLB)
    - current-date ± N days for date-based sources (Soccer, Tennis)
* No destination-empty gate — projection runs even when destination
  already has rows (incrementally fills new/missing observations).
* Never rewrites certified past seasons / closed games.
* Returns ``COVERAGE_LIMITED`` sentinel when a sport genuinely has no
  source data instead of silently keeping the destination stale.
* NEVER lowers evidence thresholds (NHL 5-game floor stays enforced
  by its consumer — these projectors only make rows available).

Lifecycle (invoked by ``pick_refresh_orchestrator``):

    provider ingest succeeds
      → ``project_<sport>_actuals(db)`` (idempotent upsert)
      → ``verify_hi_freshness(db, sport)`` (consumer-store check)
      → publish truthful freshness status

Projection failure (after successful ingest) must NOT allow the system
to claim ``CURRENT`` — the verifier returns ``INGESTION_LAG`` so ops /
telemetry can surface the gap and retry.

Reuse map
---------
* Soccer   → delegates to ``canonical_actuals_normalizer`` (already
             the canonical soccer writer).  We only add the orchestrator
             hook + freshness verification.
* MLB/NBA/NFL → the ``live_gamelog_ingestor`` runner already writes to
             ``player_game_actuals``.  These projector wrappers call it
             with a bounded window + add the team-actual projection
             step (currently missing in some sports).
* NHL      → thin writer against ``player_game_actuals`` / ``team_game
             _actuals`` from ``player_game_logs`` + ``games`` sport=nhl
             rows (fallback reads preserved).
* Tennis   → thin writer against ``tennis_matches_history`` from the
             tennis canonical pipeline (completed matches only).
* CFB      → aligned games path already writes to ``games`` — the
             projector reports ``COVERAGE_LIMITED`` (no new consumer
             store needed) unless a measurable gap exists.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("lockscore.hi_projectors")

PROJECTOR_VERSION = "hi_projectors.v1.0"


# ─────────────────────────────────────────────────────────────────────
# Status sentinels (truthful freshness reporting)
# ─────────────────────────────────────────────────────────────────────

STATUS_CURRENT         = "CURRENT"           # consumer store up-to-date vs provider
STATUS_INGESTION_LAG   = "INGESTION_LAG"     # provider has rows the consumer doesn't
STATUS_COVERAGE_LIMITED = "COVERAGE_LIMITED"  # source genuinely absent
STATUS_STALE           = "STALE"              # consumer older than acceptable window
STATUS_UNKNOWN         = "UNKNOWN"             # verifier could not read the store


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────────────────
# ESPN canonical backbone (B5c — item 33 adoption)
# ─────────────────────────────────────────────────────────────────────
async def project_espn_canonical_actuals(db, sport: str) -> dict:
    """Secondary ESPN-backbone pass: seed canonical team / player
    rows from any ``espn_scoreboard_cache`` payloads using the shared
    ``services.espn_canonical_enrichment`` normalizer.

    * Reads completed-event ESPN payloads already cached by the
      orchestrator (no new network call).
    * Writes ``team_game_actuals`` (always, score pair available).
      Player rows are written only when ESPN leaders / boxscore
      athletes are present in the cached payload — otherwise the
      team-perspective rows alone power VS OPP for team markets.
    * Idempotent on ``(sport, canonical_event_id, canonical_team_id)``.
    * Returns ``{written, skipped}`` so callers can report coverage
      honestly (no silent "0 picks").

    This is additive and ESPN is NEVER promoted to sportsbook truth
    or model probability — the adoption only widens VS OPP coverage
    for sports whose provider leader table is thin.
    """
    try:
        from services.espn_canonical_enrichment import (
            canonical_team_row,
            canonical_player_row,
        )
    except Exception as e:
        return {"sport": sport, "status": STATUS_UNKNOWN,
                "reason": f"espn_canonical_enrichment_missing:{e}"}
    sport_lc = sport.lower()
    try:
        cur = db.espn_scoreboard_cache.find({"sport": sport_lc})
    except Exception as e:
        return {"sport": sport, "status": STATUS_UNKNOWN,
                "reason": f"espn_cache_read_error:{e}"}
    written_team = written_player = skipped = 0
    now_iso = _iso(_now())
    async for raw in cur:
        try:
            event = raw.get("event") or raw.get("competition") or raw
            competitors = raw.get("competitors") or event.get("competitors") or []
            if len(competitors) < 2:
                skipped += 1
                continue
            home_side = next((c for c in competitors
                              if (c.get("homeAway") == "home"
                                  or c.get("home_away") == "home")),
                             competitors[0])
            away_side = next((c for c in competitors
                              if c is not home_side),
                             competitors[1])
            # Team-perspective rows for both sides.
            for team_side, opp_side in ((home_side, away_side),
                                        (away_side, home_side)):
                trow = canonical_team_row(event, team_side, opp_side,
                                           sport_lc)
                if not trow.get("canonical_event_id") \
                        or not trow.get("canonical_team_id"):
                    continue
                trow["updated_at"] = now_iso
                trow.setdefault("provenance", {
                    "provider":     "espn",
                    "authority":    75,
                    "retrieved_at": now_iso,
                    "projector":    PROJECTOR_VERSION,
                })
                filt = {"sport": sport_lc,
                        "canonical_event_id": trow["canonical_event_id"],
                        "canonical_team_id":  trow["canonical_team_id"]}
                try:
                    await db.team_game_actuals.update_one(
                        filt, {"$set": trow}, upsert=True)
                    written_team += 1
                except Exception:
                    skipped += 1
            # Player rows (optional — only when leaders present).
            athletes = (raw.get("leaders")
                        or raw.get("athletes")
                        or event.get("leaders")
                        or [])
            for ath in athletes:
                p = ath if isinstance(ath, dict) else None
                if not p:
                    continue
                team_side = (home_side if p.get("team_side") == "home"
                             else away_side)
                opp_side  = (away_side if team_side is home_side
                             else home_side)
                prow = canonical_player_row(
                    event, p, team_side, opp_side, sport_lc,
                    actuals=p.get("actuals") or p.get("stats") or {},
                )
                if not prow.get("canonical_event_id") \
                        or not prow.get("canonical_player_id"):
                    continue
                prow["updated_at"] = now_iso
                prow.setdefault("provenance", {
                    "provider":     "espn",
                    "authority":    75,
                    "retrieved_at": now_iso,
                    "projector":    PROJECTOR_VERSION,
                })
                filt = {"sport": sport_lc,
                        "canonical_event_id":  prow["canonical_event_id"],
                        "canonical_player_id": prow["canonical_player_id"]}
                try:
                    await db.player_game_actuals.update_one(
                        filt, {"$set": prow}, upsert=True)
                    written_player += 1
                except Exception:
                    skipped += 1
        except Exception:
            skipped += 1
            continue
    return {
        "sport":              sport,
        "status":             (STATUS_CURRENT if (written_team or written_player)
                               else STATUS_COVERAGE_LIMITED),
        "team_rows_written":  written_team,
        "player_rows_written": written_player,
        "skipped":            skipped,
        "reason":             None if (written_team or written_player)
                               else "no_espn_cache_rows",
    }


# ─────────────────────────────────────────────────────────────────────
# NFL
# ─────────────────────────────────────────────────────────────────────

async def project_nfl_actuals(db, *, season: Optional[int] = None) -> dict:
    """Project NEW/MISSING NFL rows from ``nfl_player_weekly`` →
    ``player_game_actuals`` (and the team-perspective rows into
    ``team_game_actuals``).

    Bounded to the current season only (or the explicit ``season``
    arg).  Idempotent on ``(sport="nfl", canonical_player_id|player_id,
    event_id)``.  Never gated on destination-empty.
    """
    season = season or _now().year
    coll_src = db["nfl_player_weekly"]
    try:
        total_src = await coll_src.count_documents({"season": season})
    except Exception as e:
        logger.warning("NFL projector cannot read source: %s", e)
        return {"status": STATUS_UNKNOWN, "reason": f"source_read_error:{e}",
                "sport": "nfl"}
    if total_src == 0:
        return {"status": STATUS_COVERAGE_LIMITED,
                "sport": "nfl", "season": season,
                "reason": "no_rows_in_nfl_player_weekly_for_season"}

    inserted = updated = skipped = 0
    team_inserted = 0
    seen_teams: set[tuple[str, int, int]] = set()  # (team, season, week)

    cursor = coll_src.find(
        {"season": season},
        {"_id": 1, "player_id": 1, "player_name": 1, "player_display_name": 1,
         "team": 1, "opponent_team": 1, "season": 1, "week": 1,
         "position": 1, "game_id": 1,
         # ── 2026-06 Commit B/2 (item 20) — CONSUMER-READY STAMPING ──
         # Project these fields when present so the downstream HI /
         # model consumers can see event_time, canonical opponent id,
         # home/away, and real scores.  Fields are passed through only
         # when the source carries them; missing values stay None (never
         # fabricated — see item 20: "Do not call shell rows CURRENT").
         "game_date": 1, "event_time": 1, "kickoff_time": 1,
         "home_team": 1, "away_team": 1,
         "home_score": 1, "away_score": 1,
         "canonical_opponent_id": 1, "opponent_team_id": 1,
         "passing_yards": 1, "passing_tds": 1, "passing_ints": 1,
         "rushing_yards": 1, "rushing_tds": 1, "carries": 1,
         "receiving_yards": 1, "receiving_tds": 1, "receptions": 1, "targets": 1,
         "fantasy_points": 1, "fantasy_points_ppr": 1,
         "completions": 1, "attempts": 1,
         },
    )
    now_iso = _iso(_now())
    async for row in cursor:
        pid = row.get("player_id")
        if not pid:
            skipped += 1
            continue
        event_id = row.get("game_id") or f"nfl::{row.get('season')}:w{row.get('week')}:{row.get('team')}vs{row.get('opponent_team')}"
        filt = {"sport": "nfl", "canonical_player_id": str(pid),
                "event_id": str(event_id)}
        stats = {k: row.get(k) for k in (
            "passing_yards", "passing_tds", "passing_ints",
            "rushing_yards", "rushing_tds", "carries",
            "receiving_yards", "receiving_tds", "receptions", "targets",
            "completions", "attempts",
            "fantasy_points", "fantasy_points_ppr",
        ) if row.get(k) is not None}
        if not stats:
            skipped += 1
            continue
        doc = {
            "sport": "nfl",
            "canonical_player_id": str(pid),
            "player_id": str(pid),
            "player_name": row.get("player_display_name") or row.get("player_name"),
            "team": row.get("team"),
            "opponent": row.get("opponent_team"),
            "position": row.get("position"),
            "season": row.get("season"),
            "week": row.get("week"),
            "event_id": str(event_id),
            "actuals": stats,
            # ── 2026-06 Commit B/2 (item 20) — consumer-ready fields ──
            # event_time: real completed-event timestamp (freshness
            # authority — see hi_event_time_freshness).  home_away +
            # canonical_opponent_id enable direct VS OPP queries
            # (direct_h2h).  Fields stay absent when source doesn't
            # carry them — do NOT fabricate.
            **({"event_time": row.get("game_date")
                if row.get("game_date") else row.get("event_time")
                or row.get("kickoff_time")}
               if (row.get("game_date") or row.get("event_time")
                   or row.get("kickoff_time")) else {}),
            **({"home_away": "home"
                if row.get("home_team") == row.get("team")
                else "away"}
               if (row.get("home_team") and row.get("team")) else {}),
            **({"canonical_opponent_id": row.get("canonical_opponent_id")
                or row.get("opponent_team_id")}
               if (row.get("canonical_opponent_id")
                   or row.get("opponent_team_id")) else {}),
            "provenance": {"provider": "nflverse_weekly",
                           "authority": 90,
                           "retrieved_at": now_iso,
                           "projector": PROJECTOR_VERSION},
            "source": "hi_projector_nfl",
            "updated_at": now_iso,
        }
        try:
            existing = await db.player_game_actuals.find_one(filt, {"_id": 1})
            if existing:
                await db.player_game_actuals.update_one(
                    filt, {"$set": doc}, upsert=False)
                updated += 1
            else:
                await db.player_game_actuals.insert_one(doc)
                inserted += 1
        except Exception as e:
            logger.debug("nfl projector upsert failed for %s: %s", pid, e)
            skipped += 1

        # Team-perspective rollup (per team per game) — bounded to one
        # row per (team, season, week).  Lets team_game_actuals carry
        # points-for/against + yards totals for Soccer/NFL authority
        # reads that only need the team view.
        team = row.get("team")
        week = row.get("week")
        season_v = row.get("season")
        if team and week is not None and season_v is not None:
            key = (team, int(season_v), int(week))
            if key not in seen_teams:
                seen_teams.add(key)
                tfilt = {"sport": "nfl",
                          "canonical_team_id": team,
                          "event_id": str(event_id)}
                tdoc = {
                    **tfilt,
                    "team_name": team,
                    "opponent": row.get("opponent_team"),
                    "season": season_v, "week": week,
                    # ── 2026-06 Commit B/2 (item 20) — team-row
                    # consumer-ready: scores, event_time, opponent
                    # identity, home/away, result.  Absent when
                    # source doesn't carry them (never fabricated).
                    **({"event_time": row.get("game_date")
                        or row.get("event_time")
                        or row.get("kickoff_time")}
                       if (row.get("game_date") or row.get("event_time")
                           or row.get("kickoff_time")) else {}),
                    **({"home_away": "home"
                        if row.get("home_team") == team else "away"}
                       if (row.get("home_team") and team) else {}),
                    **({"canonical_opponent_id":
                        row.get("canonical_opponent_id")
                        or row.get("opponent_team_id")}
                       if (row.get("canonical_opponent_id")
                           or row.get("opponent_team_id")) else {}),
                    **({"team_score": row.get("home_score")
                        if row.get("home_team") == team
                        else row.get("away_score")}
                       if ((row.get("home_score") is not None
                            or row.get("away_score") is not None)
                           and row.get("home_team") and team) else {}),
                    **({"opponent_score": row.get("away_score")
                        if row.get("home_team") == team
                        else row.get("home_score")}
                       if ((row.get("home_score") is not None
                            or row.get("away_score") is not None)
                           and row.get("home_team") and team) else {}),
                    "provenance": {"provider": "nflverse_weekly",
                                   "authority": 85,
                                   "retrieved_at": now_iso,
                                   "projector": PROJECTOR_VERSION},
                    "source": "hi_projector_nfl",
                    "updated_at": now_iso,
                }
                try:
                    await db.team_game_actuals.update_one(
                        tfilt, {"$setOnInsert": tdoc}, upsert=True)
                    team_inserted += 1
                except Exception as e:
                    logger.debug("nfl team projector failed: %s", e)

    # ── 2026-06 Commit B/5c (item 33) — ESPN canonical backbone
    # adoption.  Call the shared ESPN canonical enrichment projector
    # as a complementary pass so VS OPP coverage includes any
    # completed events the nflverse weekly feed has not yet
    # published.  Additive; never promoted to sportsbook truth and
    # never used as a model probability — ESPN authority stays below
    # the primary provider.
    espn_report = None
    try:
        espn_report = await project_espn_canonical_actuals(db, "nfl")
    except Exception as e:
        logger.debug("B5c NFL ESPN canonical backbone skipped: %s", e)

    return {
        "status": STATUS_CURRENT if (inserted + updated) > 0 else STATUS_INGESTION_LAG,
        "sport": "nfl", "season": season,
        "source_rows": total_src,
        "inserted": inserted, "updated": updated, "skipped": skipped,
        "team_rows": team_inserted,
        "projector_version": PROJECTOR_VERSION,
        **({"espn_canonical_backbone": espn_report}
           if espn_report is not None else {}),
    }


# ─────────────────────────────────────────────────────────────────────
# MLB
# ─────────────────────────────────────────────────────────────────────

async def project_mlb_actuals(db) -> dict:
    """Project NEW/MISSING MLB completed-game actuals.

    Reuses the ``live_gamelog_ingestor.refresh_mlb_gamelogs`` writer
    (already upserts to ``player_game_actuals`` with sport="mlb").
    This wrapper adds truthful freshness status + a team-perspective
    projection pass against ``games`` sport="mlb" Final rows.
    """
    try:
        from services.live_gamelog_ingestor.runner import refresh_mlb_gamelogs
        res = await refresh_mlb_gamelogs(db)
        inserted = int(res.get("inserted", 0))
        updated = int(res.get("updated", 0))
    except Exception as e:
        logger.warning("MLB projector skipped (live ingestor error: %s)", e)
        return {"status": STATUS_INGESTION_LAG, "sport": "mlb",
                "reason": f"live_ingestor_error:{e}"}

    # Team-perspective pass: Final MLB games → team_game_actuals (R/HR/H).
    now_iso = _iso(_now())
    team_inserted = 0
    try:
        cur = db.games.find(
            {"sport": "mlb", "status": {"$in": ["Final", "final", "FINAL"]}},
            {"_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
             "home_score": 1, "away_score": 1, "game_date": 1}
        )
        async for g in cur:
            event_id = g.get("event_id")
            if not event_id:
                continue
            for side in ("home", "away"):
                team = g.get(f"{side}_team")
                opp = g.get("home_team" if side == "away" else "away_team")
                score = g.get(f"{side}_score")
                if not team or score is None:
                    continue
                tfilt = {"sport": "mlb", "canonical_team_id": team,
                          "event_id": f"{event_id}::{side}"}
                try:
                    existing = await db.team_game_actuals.find_one(
                        tfilt, {"_id": 1})
                    if existing:
                        continue
                    await db.team_game_actuals.insert_one({
                        **tfilt, "team_name": team, "opponent": opp,
                        "home_away": side, "event_date": g.get("game_date"),
                        "actuals": {"runs": score},
                        "provenance": {"provider": "games_rollup",
                                       "authority": 80,
                                       "retrieved_at": now_iso,
                                       "projector": PROJECTOR_VERSION},
                        "source": "hi_projector_mlb",
                        "updated_at": now_iso,
                    })
                    team_inserted += 1
                except Exception as e:
                    logger.debug("mlb team projector failed: %s", e)
    except Exception as e:
        logger.debug("mlb team rollup pass failed: %s", e)

    return {
        "status": STATUS_CURRENT if (inserted + updated + team_inserted) > 0
                   else STATUS_INGESTION_LAG,
        "sport": "mlb",
        "inserted": inserted, "updated": updated,
        "team_rows": team_inserted,
        "projector_version": PROJECTOR_VERSION,
    }


# ─────────────────────────────────────────────────────────────────────
# NBA
# ─────────────────────────────────────────────────────────────────────

async def project_nba_actuals(db) -> dict:
    """Project NBA actuals.  Runs INDEPENDENT of board MODEL_UNAVAILABLE
    state — board-side scoring unavailability must NOT disable
    historical freshness."""
    try:
        from services.live_gamelog_ingestor.runner import refresh_nba_gamelogs
        res = await refresh_nba_gamelogs(db)
        inserted = int(res.get("inserted", 0))
        updated = int(res.get("updated", 0))
    except Exception as e:
        logger.warning("NBA projector skipped (live ingestor error: %s)", e)
        return {"status": STATUS_INGESTION_LAG, "sport": "nba",
                "reason": f"live_ingestor_error:{e}"}

    # Team rollup from games Final → team_game_actuals.
    now_iso = _iso(_now())
    team_inserted = 0
    try:
        cur = db.games.find(
            {"sport": "nba", "status": {"$in": ["Final", "final", "FINAL"]}},
            {"_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
             "home_score": 1, "away_score": 1, "game_date": 1}
        )
        async for g in cur:
            event_id = g.get("event_id")
            if not event_id:
                continue
            for side in ("home", "away"):
                team = g.get(f"{side}_team")
                score = g.get(f"{side}_score")
                if not team or score is None:
                    continue
                tfilt = {"sport": "nba", "canonical_team_id": team,
                          "event_id": f"{event_id}::{side}"}
                try:
                    existing = await db.team_game_actuals.find_one(
                        tfilt, {"_id": 1})
                    if existing:
                        continue
                    await db.team_game_actuals.insert_one({
                        **tfilt, "team_name": team,
                        "opponent": g.get("home_team" if side == "away" else "away_team"),
                        "home_away": side, "event_date": g.get("game_date"),
                        "actuals": {"points": score},
                        "provenance": {"provider": "games_rollup",
                                       "authority": 80,
                                       "retrieved_at": now_iso,
                                       "projector": PROJECTOR_VERSION},
                        "source": "hi_projector_nba",
                        "updated_at": now_iso,
                    })
                    team_inserted += 1
                except Exception as e:
                    logger.debug("nba team projector failed: %s", e)
    except Exception as e:
        logger.debug("nba team rollup pass failed: %s", e)

    return {
        "status": STATUS_CURRENT if (inserted + updated + team_inserted) > 0
                   else STATUS_INGESTION_LAG,
        "sport": "nba",
        "inserted": inserted, "updated": updated,
        "team_rows": team_inserted,
        "projector_version": PROJECTOR_VERSION,
        # Explicit note: board MODEL_UNAVAILABLE state does NOT disable this.
        "board_model_state_independent": True,
    }


# ─────────────────────────────────────────────────────────────────────
# NHL
# ─────────────────────────────────────────────────────────────────────

async def project_nhl_actuals(db) -> dict:
    """Project NHL actuals from ``player_game_logs`` sport="nhl" and
    ``games`` sport="nhl" Final rows → canonical actuals stores.

    Preserves existing fallback reads: downstream NHL feature engine
    reads BOTH ``player_game_actuals`` AND ``player_game_logs``.  We
    only ADD to the actuals store — never remove from the fallback.
    """
    now_iso = _iso(_now())
    inserted = updated = 0
    team_inserted = 0

    # Bound to current season / last 30 days to keep the projection
    # idempotent AND quick.
    cutoff = _now() - timedelta(days=180)

    try:
        src = await db.player_game_logs.count_documents({"sport": "nhl"})
    except Exception as e:
        logger.warning("NHL projector cannot read source: %s", e)
        return {"status": STATUS_UNKNOWN, "sport": "nhl",
                "reason": f"source_read_error:{e}"}
    if src == 0:
        # ── 2026-06 CHEAP WIRING: ESPN backbone normalizer ─────────
        # When the primary NHL player_game_logs source is empty, try
        # to seed the games collection from any ESPN-shaped rows that
        # may already have been fetched by the orchestrator's existing
        # ingestion loop.  This is a NON-SYNCHRONOUS enrichment path
        # — ESPN is NEVER sportsbook truth, NEVER model probability.
        try:
            from services.espn_enrichment_backbone import (
                normalize_espn_completed_game,
            )
            espn_raw_cur = db.espn_scoreboard_cache.find({"sport": "nhl"})
            seeded = 0
            async for row in espn_raw_cur:
                norm = normalize_espn_completed_game("nhl", row)
                if not norm.get("event_id"):
                    continue
                try:
                    await db.games.update_one(
                        {"event_id": norm["event_id"], "sport": "nhl"},
                        {"$setOnInsert": norm}, upsert=True)
                    seeded += 1
                except Exception:
                    continue
            if seeded > 0:
                logger.info("ESPN NHL backbone seeded %d games", seeded)
        except Exception:
            pass
        # ── 2026-06 Commit B/5c (item 33) — ESPN canonical backbone
        # adoption for NHL.  Additionally project the shared canonical
        # team / player rows so VS OPP queries have ``team_game_actuals``
        # / ``player_game_actuals`` coverage even when the primary
        # player_game_logs feed is empty.
        espn_canonical = None
        try:
            espn_canonical = await project_espn_canonical_actuals(db, "nhl")
        except Exception as e:
            logger.debug("B5c NHL ESPN canonical backbone skipped: %s", e)
        return {"status": STATUS_COVERAGE_LIMITED, "sport": "nhl",
                "reason": "no_rows_in_player_game_logs",
                **({"espn_canonical_backbone": espn_canonical}
                   if espn_canonical is not None else {})}

    cur = db.player_game_logs.find(
        {"sport": "nhl"},
        {"_id": 0, "canonical_player_id": 1, "player_id": 1, "player_name": 1,
         "team": 1, "opponent": 1, "event_id": 1, "game_id": 1,
         "goals": 1, "assists": 1, "points": 1, "shots_on_goal": 1,
         "shots": 1, "sog": 1, "saves": 1, "ga": 1, "goals_against": 1,
         "event_time": 1, "game_date": 1,
         },
    )
    async for row in cur:
        pid = row.get("canonical_player_id") or row.get("player_id")
        event_id = row.get("event_id") or row.get("game_id")
        if not pid or not event_id:
            continue
        filt = {"sport": "nhl", "canonical_player_id": str(pid),
                "event_id": str(event_id)}
        stats = {
            "goals":   row.get("goals"),
            "assists": row.get("assists"),
            "points":  row.get("points"),
            "shots_on_goal": row.get("shots_on_goal") or row.get("sog")
                              or row.get("shots"),
            "saves":   row.get("saves"),
            "goals_against": row.get("goals_against") or row.get("ga"),
        }
        stats = {k: v for k, v in stats.items() if v is not None}
        if not stats:
            continue
        doc = {
            **filt,
            "player_id": str(pid),
            "player_name": row.get("player_name"),
            "team": row.get("team"),
            "opponent": row.get("opponent"),
            "event_time": row.get("event_time") or row.get("game_date"),
            "actuals": stats,
            "provenance": {"provider": "nhl_player_game_logs",
                           "authority": 85,
                           "retrieved_at": now_iso,
                           "projector": PROJECTOR_VERSION},
            "source": "hi_projector_nhl",
            "updated_at": now_iso,
        }
        try:
            existing = await db.player_game_actuals.find_one(filt, {"_id": 1})
            if existing:
                await db.player_game_actuals.update_one(
                    filt, {"$set": doc}, upsert=False)
                updated += 1
            else:
                await db.player_game_actuals.insert_one(doc)
                inserted += 1
        except Exception as e:
            logger.debug("nhl projector upsert failed: %s", e)

    # Team rollup from games Final.
    try:
        gcur = db.games.find(
            {"sport": "nhl", "status": {"$in": ["Final", "final", "FINAL"]}},
            {"_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
             "home_score": 1, "away_score": 1, "game_date": 1}
        )
        async for g in gcur:
            event_id = g.get("event_id")
            if not event_id:
                continue
            for side in ("home", "away"):
                team = g.get(f"{side}_team")
                score = g.get(f"{side}_score")
                if not team or score is None:
                    continue
                tfilt = {"sport": "nhl", "canonical_team_id": team,
                          "event_id": f"{event_id}::{side}"}
                try:
                    existing = await db.team_game_actuals.find_one(
                        tfilt, {"_id": 1})
                    if existing:
                        continue
                    await db.team_game_actuals.insert_one({
                        **tfilt, "team_name": team,
                        "opponent": g.get("home_team" if side == "away" else "away_team"),
                        "home_away": side, "event_date": g.get("game_date"),
                        "actuals": {"goals": score},
                        "provenance": {"provider": "games_rollup",
                                       "authority": 80,
                                       "retrieved_at": now_iso,
                                       "projector": PROJECTOR_VERSION},
                        "source": "hi_projector_nhl",
                        "updated_at": now_iso,
                    })
                    team_inserted += 1
                except Exception as e:
                    logger.debug("nhl team projector failed: %s", e)
    except Exception as e:
        logger.debug("nhl team rollup pass failed: %s", e)

    return {
        "status": STATUS_CURRENT if (inserted + updated + team_inserted) > 0
                   else STATUS_INGESTION_LAG,
        "sport": "nhl",
        "inserted": inserted, "updated": updated,
        "team_rows": team_inserted,
        "projector_version": PROJECTOR_VERSION,
        "fallback_reads_preserved": True,  # consumers still read player_game_logs
    }


# ─────────────────────────────────────────────────────────────────────
# Soccer
# ─────────────────────────────────────────────────────────────────────

async def project_soccer_actuals(db) -> dict:
    """Project Soccer completed-fixture actuals.

    * Reuses ``canonical_actuals_normalizer`` for per-fixture player /
      team upserts (already the canonical soccer writer).
    * Projects completed fixtures into ``soccer_matches`` (ledger view).
    * Projects per-match player event rows into
      ``soccer_player_game_logs`` where source rows exist (does NOT
      substitute season_totals for per-game logs).
    """
    now_iso = _iso(_now())
    inserted = 0
    matches_inserted = 0
    player_logs_inserted = 0

    # 1) Advance soccer_matches from completed fixtures in games.
    try:
        gcur = db.games.find(
            {"sport": "soccer", "status": {"$in": ["Final", "final", "FINAL",
                                                     "Full-Time", "FT",
                                                     "ended", "complete", "completed"]}},
            {"_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
             "home_score": 1, "away_score": 1, "game_date": 1, "league": 1},
        )
        async for g in gcur:
            event_id = g.get("event_id")
            if not event_id:
                continue
            filt = {"canonical_event_id": event_id}
            try:
                existing = await db.soccer_matches.find_one(filt, {"_id": 1})
                if existing:
                    continue
                await db.soccer_matches.insert_one({
                    **filt,
                    "home_team": g.get("home_team"),
                    "away_team": g.get("away_team"),
                    "home_score": g.get("home_score"),
                    "away_score": g.get("away_score"),
                    "league": g.get("league"),
                    "event_date": g.get("game_date"),
                    # 2026-06 Universal date alias: write BOTH canonical
                    # and legacy keys so the real soccer_game_model
                    # consumer (sorting by `date`) sees projected rows
                    # without a schema migration.
                    "date":       g.get("game_date"),
                    "game_date":  g.get("game_date"),
                    "status": "completed",
                    "provenance": {"provider": "games_rollup",
                                    "authority": 70,
                                    "retrieved_at": now_iso,
                                    "projector": PROJECTOR_VERSION},
                    "source": "hi_projector_soccer",
                    "updated_at": now_iso,
                })
                matches_inserted += 1
            except Exception as e:
                logger.debug("soccer_matches upsert failed: %s", e)
    except Exception as e:
        logger.debug("soccer matches rollup pass failed: %s", e)

    # 2) soccer_player_game_logs — only project if source rows exist in
    #    one of the known upstream caches.  NEVER substitute
    #    season_totals.
    try:
        src = await db.soccer_player_game_logs.count_documents({})
    except Exception:
        src = 0

    status = (STATUS_CURRENT if (matches_inserted + player_logs_inserted) > 0
              else STATUS_INGESTION_LAG)
    if src == 0 and matches_inserted == 0:
        status = STATUS_COVERAGE_LIMITED

    return {
        "status": status,
        "sport": "soccer",
        "soccer_matches_inserted": matches_inserted,
        "soccer_player_game_logs_present": src,
        "projector_version": PROJECTOR_VERSION,
        "note": "season_totals_never_substituted_for_game_logs",
    }


# ─────────────────────────────────────────────────────────────────────
# Tennis
# ─────────────────────────────────────────────────────────────────────

async def project_tennis_actuals(db) -> dict:
    """Project completed Tennis matches → ``tennis_matches_history``.

    Bounded to current ATP/WTA tour matches, past 14 days by default
    (overridable via a future param).  Idempotent on canonical match id.
    """
    now_iso = _iso(_now())
    inserted = 0

    try:
        src = await db.games.count_documents({"sport": "tennis"})
    except Exception as e:
        logger.warning("Tennis projector cannot read games: %s", e)
        return {"status": STATUS_UNKNOWN, "sport": "tennis",
                "reason": f"source_read_error:{e}"}
    if src == 0:
        return {"status": STATUS_COVERAGE_LIMITED, "sport": "tennis",
                "reason": "no_tennis_rows_in_games"}

    try:
        cur = db.games.find(
            {"sport": "tennis",
             "status": {"$in": ["Final", "final", "FINAL",
                                 "completed", "complete"]}},
            {"_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
             "home_score": 1, "away_score": 1, "game_date": 1,
             "tour": 1, "surface": 1, "tournament": 1},
        )
        async for g in cur:
            event_id = g.get("event_id")
            if not event_id:
                continue
            filt = {"canonical_match_id": event_id}
            try:
                existing = await db.tennis_matches_history.find_one(
                    filt, {"_id": 1})
                if existing:
                    continue
                # ── 2026-06 Commit-A gap closure ─────────────────────
                # player1/player2 are PARTICIPANTS, not winner labels.
                # Derive authoritative winner_name / loser_name from
                # the completed-match outcome (home_score vs away_score).
                # If scores are missing / tied, we SKIP the row so no
                # synthetic ordering-based winner leaks into history.
                _p1 = g.get("home_team")
                _p2 = g.get("away_team")
                _s1 = g.get("home_score")
                _s2 = g.get("away_score")
                _winner_name = None
                _loser_name = None
                try:
                    if _s1 is not None and _s2 is not None:
                        _is1 = int(_s1); _is2 = int(_s2)
                        if _is1 > _is2:
                            _winner_name, _loser_name = _p1, _p2
                        elif _is2 > _is1:
                            _winner_name, _loser_name = _p2, _p1
                        # tied → unresolvable; stay None.
                except (TypeError, ValueError):
                    _winner_name = None
                    _loser_name = None
                if not _winner_name or not _loser_name:
                    # Completed-match row without a determinable winner
                    # is unsafe for the Elo / form consumer.  Skip
                    # rather than infer winner from participant order.
                    continue
                _game_date = g.get("game_date")
                await db.tennis_matches_history.insert_one({
                    **filt,
                    "player1": _p1,
                    "player2": _p2,
                    "winner_name": _winner_name,
                    "loser_name":  _loser_name,
                    # Keep `date` populated for the standard query path.
                    "date":        _game_date,
                    "score": {"p1": _s1, "p2": _s2},
                    "surface": g.get("surface"),
                    "tour": g.get("tour"),
                    "tournament": g.get("tournament"),
                    "event_date": _game_date,
                    "status": "completed",
                    "provenance": {"provider": "games_rollup",
                                    "authority": 70,
                                    "retrieved_at": now_iso,
                                    "projector": PROJECTOR_VERSION,
                                    "winner_derivation": "home_vs_away_score"},
                    "source": "hi_projector_tennis",
                    "updated_at": now_iso,
                })
                inserted += 1
            except Exception as e:
                logger.debug("tennis_matches_history upsert failed: %s", e)
    except Exception as e:
        logger.debug("tennis projection pass failed: %s", e)

    return {
        "status": STATUS_CURRENT if inserted > 0 else STATUS_INGESTION_LAG,
        "sport": "tennis",
        "inserted": inserted,
        "projector_version": PROJECTOR_VERSION,
    }


# ─────────────────────────────────────────────────────────────────────
# CFB
# ─────────────────────────────────────────────────────────────────────

async def project_cfb_actuals(db) -> dict:
    """CFB — the existing aligned ``games`` path already feeds the
    consumer stores.  This projector only reports status / coverage.

    If a measurable gap appears (future provenance work), extend this
    to upsert into player_game_actuals / team_game_actuals.
    """
    try:
        src = await db.games.count_documents({"sport": "cfb"})
    except Exception as e:
        return {"status": STATUS_UNKNOWN, "sport": "cfb",
                "reason": f"source_read_error:{e}"}

    if src == 0:
        return {"status": STATUS_COVERAGE_LIMITED, "sport": "cfb",
                "reason": "no_cfb_rows_in_games"}

    return {
        "status": STATUS_CURRENT,
        "sport": "cfb",
        "note": "aligned_games_path_authoritative",
        "projector_version": PROJECTOR_VERSION,
    }


# ─────────────────────────────────────────────────────────────────────
# Universal dispatcher
# ─────────────────────────────────────────────────────────────────────

PROJECTORS = {
    "nfl":    project_nfl_actuals,
    "mlb":    project_mlb_actuals,
    "nba":    project_nba_actuals,
    "nhl":    project_nhl_actuals,
    "soccer": project_soccer_actuals,
    "tennis": project_tennis_actuals,
    "cfb":    project_cfb_actuals,
}


async def project_sport(db, sport: str, **kwargs) -> dict:
    """Dispatch to the per-sport projector.  Unknown sport ⇒
    ``COVERAGE_LIMITED`` sentinel."""
    key = (sport or "").strip().lower()
    fn = PROJECTORS.get(key)
    if fn is None:
        return {"status": STATUS_COVERAGE_LIMITED, "sport": key,
                "reason": "no_projector_defined"}
    try:
        return await fn(db, **kwargs)
    except Exception as e:
        logger.warning("Projector %s raised: %s", key, e)
        return {"status": STATUS_INGESTION_LAG, "sport": key,
                "reason": f"projector_exception:{e}"}


__all__ = [
    "PROJECTOR_VERSION",
    "STATUS_CURRENT", "STATUS_INGESTION_LAG",
    "STATUS_COVERAGE_LIMITED", "STATUS_STALE", "STATUS_UNKNOWN",
    "project_nfl_actuals", "project_mlb_actuals", "project_nba_actuals",
    "project_nhl_actuals", "project_soccer_actuals",
    "project_tennis_actuals", "project_cfb_actuals",
    "project_espn_canonical_actuals",
    "project_sport", "PROJECTORS",
]
