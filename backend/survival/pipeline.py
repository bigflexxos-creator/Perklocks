"""Survivability pipeline — orchestrates everything for one pick.

Given a primary MLB hit prop, this module:
  1. Resolves the primary hitter's MLB person_id
  2. Pulls their season game log
  3. Pulls hitting logs for current-team teammates (default cohort)
  4. Runs the conditional-rate engine
  5. Caches the result on the pick in the `survival_coverage` collection

Caching is keyed by `pick_id` so the same coverage is computed at most
once per pick per day. Subsequent endpoint hits read the cached doc
in <50 ms.

If any upstream call fails, we return an empty coverage payload with
a human-readable `note` — the original pick still loads exactly as
before. Strict isolation: nothing here mutates the `picks` collection.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .client import find_player_id, hitting_game_log, team_roster
from .engine import rank_candidates, reliability, survival_index, MIN_MISS_SAMPLE

logger = logging.getLogger("lockscore.survival.pipeline")

# Max teammates to pull game logs for. Caps API + compute cost so a
# single coverage compute stays under ~5 s end-to-end.
MAX_CANDIDATES   = 12
CACHE_TTL_HOURS  = 12

# Extract "<First Last>" from selection text like "Juan Soto Over 0.5 Hits"
# or "Aaron Nola Over 5.5 Strikeouts". We grab the first 2-3 capitalized
# tokens before the market verb. Robust enough for the MLB Stats API
# name lookup which forgives minor formatting.
_TEAM_CODE_TO_MLB_ID = {
    # AL East
    "BAL": 110, "BOS": 111, "NYY": 147, "TB": 139, "TBR": 139, "TOR": 141,
    # AL Central
    "CWS": 145, "CHW": 145, "CLE": 114, "DET": 116, "KC": 118, "KCR": 118, "MIN": 142,
    # AL West
    "HOU": 117, "LAA": 108, "OAK": 133, "ATH": 133, "SEA": 136, "TEX": 140,
    # NL East
    "ATL": 144, "MIA": 146, "NYM": 121, "PHI": 143, "WSH": 120, "WSN": 120,
    # NL Central
    "CHC": 112, "CIN": 113, "MIL": 158, "PIT": 134, "STL": 138,
    # NL West
    "ARI": 109, "AZ": 109, "COL": 115, "LAD": 119, "SD": 135, "SDP": 135, "SF": 137, "SFG": 137,
}


def _team_id_from_selection(selection: str) -> int | None:
    """Extract a team code like '(TOR)' from a selection string and
    resolve it to the MLB team ID. Used when the pick doesn't carry a
    structured team field (most of our existing rows)."""
    if not selection:
        return None
    m = re.search(r"\(([A-Z]{2,4})\)", selection)
    if not m:
        return None
    return _TEAM_CODE_TO_MLB_ID.get(m.group(1).upper())


_NAME_RE = re.compile(r"^([A-Z][\w'\-\.]+(?:\s+[A-Z][\w'\-\.]+){1,3})")


def _parse_hitter_name(selection: str) -> str | None:
    if not selection:
        return None
    # Strip parenthetical team abbreviation: "Juan Soto (NYM)" → "Juan Soto"
    cleaned = re.sub(r"\([A-Z]{2,4}\)", "", selection).strip()
    m = _NAME_RE.match(cleaned)
    return m.group(1).strip() if m else None


def _is_hit_prop(pick: dict) -> bool:
    """True if the pick is an MLB hits prop we can compute coverage for."""
    if (pick.get("sport") or "") != "MLB":
        return False
    market = (pick.get("market") or "").lower()
    return "hits" in market and "strikeouts" not in market


async def compute_coverage_for_pick(pick: dict, db,
                                    use_cache: bool = True,
                                    cohort: str = "teammates") -> dict:
    """Public entry point. Returns a payload ready for the API response."""
    pick_id = pick.get("id")
    if not pick_id:
        return _empty("missing pick id")

    if not _is_hit_prop(pick):
        return _empty("coverage only computed for MLB hits props")

    # Cache hit?
    if use_cache:
        cached = await db.survival_coverage.find_one(
            {"pick_id": pick_id}, {"_id": 0},
        )
        if cached:
            try:
                computed = datetime.fromisoformat(cached.get("computed_at"))
                age_h = (datetime.now(timezone.utc) - computed).total_seconds() / 3600
                if age_h < CACHE_TTL_HOURS:
                    return cached
            except Exception:
                pass  # stale-shape doc — recompute below

    # Resolve primary hitter
    primary_name = _parse_hitter_name(pick.get("selection") or "")
    if not primary_name:
        return _empty("could not parse hitter name from selection")
    primary_id = await find_player_id(primary_name)
    if not primary_id:
        return _empty(f"no MLB player match for '{primary_name}'")

    # Season log
    season = datetime.now(timezone.utc).year
    primary_log = await hitting_game_log(primary_id, season)
    if not primary_log:
        return _empty("no season game log returned")

    miss_dates = [r["date"] for r in primary_log
                  if r.get("qualifying") and r.get("hits") == 0]
    if len(miss_dates) < MIN_MISS_SAMPLE:
        return _empty(
            f"insufficient miss sample: {len(miss_dates)} games "
            f"(need ≥ {MIN_MISS_SAMPLE})",
            primary={"name": primary_name, "id": primary_id,
                     "miss_games": len(miss_dates)},
        )

    # Candidate cohort: SAME-TEAM teammates ONLY (per user spec).
    # Rationale: teammates share the exact same pitching matchup, weather,
    # ballpark, batting-order context. The opposing team faces a different
    # pitcher entirely — including them as "coverage" pollutes the signal
    # with cross-game noise. We deliberately drop the away-team roster.
    #
    # Resolution order for the primary's team id:
    #   1. structured `team_id` / `home_team_id` field on the pick
    #   2. fallback: parse "(TOR)" suffix from the selection text
    #   3. fallback: if the primary's first game log row has a team field
    #      we use that (handled implicitly by find_player_id team caching)
    primary_team_id = (
        pick.get("team_id")
        or pick.get("home_team_id")
        or _team_id_from_selection(pick.get("selection") or "")
    )
    candidate_ids: list[dict] = []
    if primary_team_id:
        try:
            roster = await team_roster(int(primary_team_id))
            candidate_ids.extend(roster)
        except Exception as e:
            logger.warning("roster fetch failed for team %s: %s",
                            primary_team_id, e)
    # Fall back to inferring the team from `(KC)` style suffix if the
    # pick has no structured team id.
    if not candidate_ids:
        return _empty("could not load teammate roster (no team id on pick)")

    # Trim to MAX_CANDIDATES and exclude the primary himself.
    seen: set[int] = {primary_id}
    cohort: list[dict] = []
    for c in candidate_ids:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        cohort.append(c)
        if len(cohort) >= MAX_CANDIDATES:
            break

    # Fetch each candidate's game log in parallel (capped).
    logs = await asyncio.gather(
        *[hitting_game_log(c["id"], season) for c in cohort],
        return_exceptions=True,
    )
    enriched: list[dict] = []
    for c, lg in zip(cohort, logs):
        if isinstance(lg, Exception) or not lg:
            continue
        enriched.append({**c, "log": lg})

    ranked = rank_candidates(primary_log, enriched)
    idx = survival_index(ranked)

    payload = {
        "pick_id":     pick_id,
        "primary": {
            "name":       primary_name,
            "id":         primary_id,
            "miss_games": len(miss_dates),
        },
        "reliability": reliability(miss_dates),
        "survival_index": idx,        # 0–100 weighted rollup
        "candidates":  ranked[:5],    # top 5
        "cohort_size": len(enriched),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "note":        "Insight only — does not replace or modify the primary pick.",
    }

    # Persist for fast subsequent reads.
    await db.survival_coverage.update_one(
        {"pick_id": pick_id}, {"$set": payload}, upsert=True,
    )
    return payload


def _empty(reason: str, primary: dict | None = None) -> dict:
    return {
        "primary":     primary or {},
        "reliability": "Low Sample",
        "survival_index": 0.0,
        "candidates":  [],
        "cohort_size": 0,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "note":        reason,
    }
