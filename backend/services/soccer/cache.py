"""Mongo-backed cache with per-document source tracking.

Every SoccerMatch / SoccerTeam / SoccerStanding / SoccerFixture we cache
carries a `source` field so we can:
   • Audit which provider gave us which data point
   • Rank source reliability over time (agree-with-close vs agree-with-actual)
   • Prefer more-reliable sources on next refresh

Collections:
    soccer_matches      — historical + current match results
    soccer_teams        — team metadata
    soccer_standings    — league tables
    soccer_fixtures     — upcoming scheduled matches
    soccer_ingest_log   — provider run history for observability
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.services.soccer.cache")


def _match_key(m: dict) -> dict:
    """Stable key for a match doc — same match from two providers should
    dedupe here. We use (league, season, home, away, date) since match IDs
    aren't stable across providers."""
    return {
        "league":    m.get("league"),
        "season":    m.get("season"),
        "home_team": m.get("home_team"),
        "away_team": m.get("away_team"),
        "date":      m.get("date"),
    }


async def upsert_match(db, match: dict) -> None:
    """Upsert one match. If a doc already exists from a MORE-authoritative
    source (see PROVIDER_TRUST rank), skip. Otherwise merge — never
    overwrite a non-None field with None."""
    existing = await db.soccer_matches.find_one(_match_key(match))
    if existing:
        merged = {**existing}
        for k, v in match.items():
            if v is None:
                continue
            # Prefer higher-trust source when there's a real conflict on
            # a key data point (scores, closing odds).
            if k in ("home_score", "away_score",
                     "home_odds_close", "draw_odds_close", "away_odds_close"):
                incoming_trust = PROVIDER_TRUST.get(match.get("source"), 0)
                existing_trust = PROVIDER_TRUST.get(existing.get("source"), 0)
                if incoming_trust > existing_trust or merged.get(k) is None:
                    merged[k] = v
                # else keep existing
            else:
                merged[k] = v
        await db.soccer_matches.update_one(
            _match_key(match), {"$set": merged},
        )
    else:
        await db.soccer_matches.insert_one(match)


async def upsert_matches_bulk(db, matches: Iterable[dict]) -> int:
    """Batched upsert. Returns count of matches processed."""
    n = 0
    for m in matches:
        try:
            await upsert_match(db, m)
            n += 1
        except Exception as e:
            logger.debug("upsert_match error: %s", e)
    return n


async def upsert_team(db, team: dict) -> None:
    await db.soccer_teams.update_one(
        {"name": team.get("name"), "league": team.get("league")},
        {"$set": team},
        upsert=True,
    )


async def upsert_standing(db, standing: dict) -> None:
    await db.soccer_standings.update_one(
        {"league": standing.get("league"),
         "season": standing.get("season"),
         "team":   standing.get("team")},
        {"$set": standing},
        upsert=True,
    )


async def upsert_fixture(db, fixture: dict) -> None:
    await db.soccer_fixtures.update_one(
        {"league": fixture.get("league"),
         "season": fixture.get("season"),
         "home_team": fixture.get("home_team"),
         "away_team": fixture.get("away_team"),
         "utc_kickoff": fixture.get("utc_kickoff")},
        {"$set": fixture},
        upsert=True,
    )


async def log_ingest_run(db, source: str, kind: str, result: dict) -> None:
    """Observability record. `kind` in ('matches','teams','standings','fixtures')."""
    await db.soccer_ingest_log.insert_one({
        "source":     source,
        "kind":       kind,
        "at":         datetime.now(timezone.utc).isoformat(),
        "result":     result,
    })


# Trust ranking — higher = more authoritative. Used by upsert_match when
# two providers disagree on a stat. football-data.co.uk is #1 because
# their CSVs are the industry standard for historical odds + results
# (used by academic papers). Football-Data.org is close because their
# results are curated but their odds coverage is thinner.
PROVIDER_TRUST: dict[str, int] = {
    "football_data_co_uk": 100,
    "football_data_org":    90,
    "openligadb":           80,   # authoritative for German leagues
    "thesportsdb":          60,
    "espn":                 55,
    "understat":            70,   # highest for xG specifically
    "unknown":               0,
}
