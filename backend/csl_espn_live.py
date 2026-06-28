"""CSL ESPN Live — authoritative ACTIVE-player + current-season form
fetcher backed by ESPN's free public soccer APIs.

Problem this solves
-------------------
Our synthetic Anytime-Goal-Scorer pipeline (`sportdb_player_scorer.py`)
was happily promoting RETIRED / TRANSFERRED-OUT players for the Chinese
Super League (CSL) because:

  • TheSportsDB CSL data is stale (often a season behind).
  • Our `csl_form_seed.py` is hand-curated and drifts whenever a player
    retires, moves leagues, or gets benched.

ESPN's two public endpoints fix both:

  1. `site.api.espn.com/.../chn.1/teams/{teamId}/roster`
       → current roster with `status.name == "Active"` per athlete.
  2. `sports.core.api.espn.com/.../chn.1/seasons/{yr}/types/1/leaders`
       → top scorers / assists with actual matches + goals + an
         `active: true/false` flag on the resolved athlete document.

We pull both daily, persist to MongoDB, and expose a single sync helper
`is_player_currently_active(name) -> bool | None` (None == unknown ⇒ caller
falls back to legacy heuristics). The synth scorer code then drops any
candidate whose name is KNOWN-INACTIVE.

Author: PerkLocks AI · 2026-06-27
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.csl_espn")

# ─────────────────────────── Config ───────────────────────────
ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/chn.1"
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports/soccer/leagues/chn.1"
HTTP_TIMEOUT_S = 12.0
# Refresh cadence — 12h is plenty; ESPN data refreshes match-by-match.
REFRESH_INTERVAL_S = 12 * 60 * 60
# How long persisted entries are considered fresh enough to trust.
DATA_STALE_AFTER_S = 36 * 60 * 60

# Lazy in-process cache so we never hit ESPN >1× per request burst.
_active_index: dict[str, dict] = {}   # norm_name → record
_scorer_index: dict[str, dict] = {}   # norm_name → record (top scorers)
_last_refresh_at: float = 0.0


def _norm(s: str) -> str:
    """Lower + strip diacritics + collapse spaces. Matches `Crysan` ≡
    `Cryzan`, `Cádiz` ≡ `Cadiz`."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


# Pre-loaded alias table from `csl_form_seed._PLAYER_ALIASES` so a name
# the seed file knows belongs to "Felipe Sousa" (e.g. "Felipe Silva")
# resolves through to the ESPN active-roster check correctly.
def _load_seed_aliases() -> dict[str, str]:
    try:
        import csl_form_seed
        return {k: v for k, v in (csl_form_seed._PLAYER_ALIASES or {}).items()}
    except Exception:
        return {}


_SEED_ALIASES: dict[str, str] = _load_seed_aliases()


def _resolve_alias(key: str) -> str:
    """Returns the canonical normalised name, applying any seed alias."""
    return _norm(_SEED_ALIASES.get(key, key))


# ─────────────────────────── Fetchers ───────────────────────────
async def _fetch_team_list(client: httpx.AsyncClient) -> list[dict]:
    """Returns list of {id, name, abbr} for the 16 CSL clubs."""
    try:
        r = await client.get(f"{ESPN_SITE_BASE}/teams")
        r.raise_for_status()
        out: list[dict] = []
        for sport in r.json().get("sports", []):
            for league in sport.get("leagues", []):
                for entry in league.get("teams", []):
                    t = entry.get("team") or {}
                    if t.get("id"):
                        out.append({
                            "id": t["id"],
                            "name": t.get("displayName") or t.get("name") or "",
                            "abbr": t.get("abbreviation") or "",
                        })
        return out
    except Exception as e:
        logger.warning(f"ESPN CSL team list failed: {e}")
        return []


async def _fetch_team_roster(client: httpx.AsyncClient, team_id: str) -> list[dict]:
    """Returns active athletes for one team. Drops inactive (cut/loaned-out)."""
    try:
        r = await client.get(f"{ESPN_SITE_BASE}/teams/{team_id}/roster")
        r.raise_for_status()
        out: list[dict] = []
        for a in r.json().get("athletes", []):
            status_name = (a.get("status") or {}).get("name") or ""
            # ESPN marks `Active`, `Inactive`, `Suspended`, `Out`, etc.
            # We keep anything that is currently part of the matchday squad.
            if status_name.lower() != "active":
                continue
            out.append({
                "espn_id": a.get("id"),
                "name": a.get("fullName") or a.get("displayName") or "",
                "first_name": a.get("firstName") or "",
                "last_name": a.get("lastName") or "",
                "position": (a.get("position") or {}).get("abbreviation"),
                "jersey": a.get("jersey"),
                "age": a.get("age"),
                "dob": a.get("dateOfBirth"),
                "status": status_name,
            })
        return out
    except Exception as e:
        logger.debug(f"ESPN CSL roster fetch failed for team={team_id}: {e}")
        return []


def _resolve_season_for_url() -> str:
    """ESPN's CSL season is calendar year (Mar–Nov). Use current year, with
    a Jan→Feb fallback to last year since the league hasn't kicked off yet."""
    now = datetime.now(timezone.utc)
    return str(now.year if now.month >= 3 else now.year - 1)


async def _fetch_leaders(client: httpx.AsyncClient, season: str) -> list[dict]:
    """Returns top scorers + assist leaders with resolved names + active flag."""
    out: list[dict] = []
    try:
        r = await client.get(
            f"{ESPN_CORE_BASE}/seasons/{season}/types/1/leaders",
            params={"lang": "en", "region": "us"},
        )
        r.raise_for_status()
        leaders = r.json().get("categories", [])
    except Exception as e:
        logger.warning(f"ESPN CSL leaders fetch failed: {e}")
        return out

    # Resolve each athlete + team in parallel (limited).
    for cat in leaders:
        cat_name = cat.get("name", "")
        # We only care about goal/assist categories, skip clean sheets etc.
        if not any(k in cat_name.lower() for k in ("goals", "assists")):
            continue
        for L in cat.get("leaders", []) or []:
            ath_ref = (L.get("athlete") or {}).get("$ref")
            team_ref = (L.get("team") or {}).get("$ref")
            if not ath_ref:
                continue
            try:
                ar = await client.get(ath_ref)
                ar.raise_for_status()
                ath = ar.json()
            except Exception:
                continue
            team_name = ""
            if team_ref:
                try:
                    tr = await client.get(team_ref)
                    tr.raise_for_status()
                    team_name = (tr.json() or {}).get("displayName") or ""
                except Exception:
                    pass
            out.append({
                "category": cat_name,                          # goalsLeaders / assistsLeaders
                "name": ath.get("fullName") or ath.get("displayName") or "",
                "espn_id": ath.get("id"),
                "active": ath.get("active"),                   # KEY field
                "age": ath.get("age"),
                "team": team_name,
                "value": L.get("value"),                       # numeric (goals)
                "display": L.get("displayValue"),              # "Matches: 16, Goals: 12"
            })
    return out


# ─────────────────────────── Public refresh ───────────────────────────
async def refresh(db) -> dict[str, Any]:
    """Pulls fresh ESPN CSL data, persists to MongoDB, fills in-memory caches.
    Returns a summary dict suitable for /api/admin/csl-espn-status."""
    started = time.time()
    season = _resolve_season_for_url()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, headers={
        "User-Agent": "PerkLocks-AI/1.0 (+ESPN public)",
        "Accept": "application/json",
    }) as client:
        teams = await _fetch_team_list(client)
        if not teams:
            return {"ok": False, "reason": "ESPN team list empty"}
        # Parallel roster fetches with bounded concurrency.
        sem = asyncio.Semaphore(6)
        async def _one(team: dict):
            async with sem:
                roster = await _fetch_team_roster(client, str(team["id"]))
                for p in roster:
                    p["team"] = team["name"]
                    p["team_id"] = team["id"]
                return roster
        results = await asyncio.gather(*(_one(t) for t in teams))
        all_active: list[dict] = [p for r in results for p in r]
        leaders = await _fetch_leaders(client, season)

    refreshed_at = datetime.now(timezone.utc)

    # ── Persist (best-effort) ──
    try:
        coll = db.csl_espn_state
        await coll.update_one(
            {"_id": "active_roster"},
            {"$set": {
                "players": all_active,
                "refreshed_at": refreshed_at,
                "season": season,
            }},
            upsert=True,
        )
        await coll.update_one(
            {"_id": "season_leaders"},
            {"$set": {
                "leaders": leaders,
                "refreshed_at": refreshed_at,
                "season": season,
            }},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"Mongo persist of CSL ESPN state failed: {e}")

    # ── Rebuild in-process indexes ──
    global _active_index, _scorer_index, _last_refresh_at
    _active_index = {_norm(p["name"]): p for p in all_active if p.get("name")}
    _scorer_index = {}
    for L in leaders:
        # Some players appear in BOTH goals + assists; merge.
        key = _norm(L.get("name") or "")
        if not key:
            continue
        prev = _scorer_index.get(key, {})
        # Take goals + assists into a single row.
        if "goals" in (L.get("category") or "").lower():
            prev["goals"] = L.get("value")
            prev["matches"] = _extract_matches(L.get("display"))
        elif "assists" in (L.get("category") or "").lower():
            prev["assists"] = L.get("value")
        # Common metadata
        prev.setdefault("name", L["name"])
        prev.setdefault("team", L.get("team"))
        prev.setdefault("active", L.get("active"))
        prev.setdefault("age", L.get("age"))
        prev.setdefault("espn_id", L.get("espn_id"))
        _scorer_index[key] = prev
    _last_refresh_at = time.time()

    summary = {
        "ok": True,
        "season": season,
        "teams": len(teams),
        "players_active": len(all_active),
        "scorer_rows": len(_scorer_index),
        "inactive_seen": sum(1 for v in _scorer_index.values() if v.get("active") is False),
        "elapsed_sec": round(time.time() - started, 1),
        "refreshed_at": refreshed_at.isoformat(),
    }
    logger.info(f"CSL ESPN refresh: {summary}")
    return summary


def _extract_matches(display: Optional[str]) -> Optional[int]:
    """Parse "Matches: 16, Goals: 12" → 16."""
    if not display:
        return None
    m = re.search(r"matches?:\s*(\d+)", display, re.IGNORECASE)
    return int(m.group(1)) if m else None


# ─────────────────────────── Hydration on startup ───────────────────────────
async def hydrate_from_db(db) -> None:
    """Load the most-recent persisted ESPN snapshot into memory without
    hitting the network. Called on app startup so /api/picks/today returns
    correct goalscorer picks before the first scheduled refresh runs."""
    global _active_index, _scorer_index, _last_refresh_at
    try:
        roster_doc = await db.csl_espn_state.find_one({"_id": "active_roster"})
        leaders_doc = await db.csl_espn_state.find_one({"_id": "season_leaders"})
        if roster_doc:
            for p in roster_doc.get("players", []):
                if p.get("name"):
                    _active_index[_norm(p["name"])] = p
        if leaders_doc:
            for L in leaders_doc.get("leaders", []):
                key = _norm(L.get("name") or "")
                if not key:
                    continue
                prev = _scorer_index.get(key, {})
                if "goals" in (L.get("category") or "").lower():
                    prev["goals"] = L.get("value")
                    prev["matches"] = _extract_matches(L.get("display"))
                elif "assists" in (L.get("category") or "").lower():
                    prev["assists"] = L.get("value")
                prev.setdefault("name", L["name"])
                prev.setdefault("team", L.get("team"))
                prev.setdefault("active", L.get("active"))
                prev.setdefault("age", L.get("age"))
                _scorer_index[key] = prev
            refreshed = leaders_doc.get("refreshed_at")
            if isinstance(refreshed, datetime):
                _last_refresh_at = refreshed.timestamp()
    except Exception as e:
        logger.debug(f"CSL ESPN hydrate skipped: {e}")


# ─────────────────────────── Public sync helpers ───────────────────────────
def _name_match(query: str, candidate: str) -> bool:
    """Tolerant name matcher used when an exact normalised key doesn't hit.
    Handles:
      • Single-token CSL stage names: "Crysan" ≡ "Cryzan" (Levenshtein ≤ 1)
      • Stage-name vs full-name: "Wesley" matches "Wesley Moraes"
      • Reversed token order: "Cádiz Jhonder" ≡ "Jhonder Cádiz"
    """
    if not query or not candidate:
        return False
    if query == candidate:
        return True
    qt = query.split()
    ct = candidate.split()
    # Stage-name match: one side is a single token that equals the other
    # side's last OR first token.
    if len(qt) == 1 and (qt[0] == ct[0] or qt[0] == ct[-1]):
        return True
    if len(ct) == 1 and (ct[0] == qt[0] or ct[0] == qt[-1]):
        return True
    # Levenshtein-1 on single-token stage names (Crysan / Cryzan).
    if len(qt) == 1 and len(ct) == 1:
        return _levenshtein_le1(qt[0], ct[0])
    # Reversed last/first name.
    if set(qt) == set(ct):
        return True
    return False


def _levenshtein_le1(a: str, b: str) -> bool:
    """True iff Levenshtein(a,b) ≤ 1. Fast bail-outs cover 99% of calls."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):  # substitution
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    # insertion / deletion — the longer string should contain the shorter
    # one with exactly one extra char.
    if len(a) > len(b):
        a, b = b, a
    # a is shorter
    for i in range(len(b)):
        if b[:i] + b[i+1:] == a:
            return True
    return False


def is_player_currently_active(name: str, *, team_hint: Optional[str] = None) -> Optional[bool]:
    """Returns True if ESPN has the player on a CSL roster as Active, False if
    we have evidence they are INACTIVE (e.g. explicit ESPN active=False, or
    NOT present anywhere in current CSL roster + scorer indexes), or None
    when we just don't have a fresh-enough snapshot to decide.
    """
    if not name:
        return None
    if not _active_index and not _scorer_index:
        return None
    if _last_refresh_at and (time.time() - _last_refresh_at) > DATA_STALE_AFTER_S:
        return None
    key = _resolve_alias(_norm(name))
    # Direct roster hit → active.
    if key in _active_index:
        return True
    # Tolerant roster match (handles Crysan/Cryzan, Wesley/Wesley Moraes).
    for k in _active_index.keys():
        if _name_match(key, k):
            return True
    # Leader doc evidence — explicit hit OR tolerant match.
    leader = _scorer_index.get(key)
    if leader is None:
        for k, ld in _scorer_index.items():
            if _name_match(key, k):
                leader = ld
                break
    if leader is not None:
        return bool(leader.get("active"))
    # Not present anywhere → not active in CSL.
    return False


def get_live_form(name: str) -> Optional[dict]:
    """Returns the most-recent ESPN season form for a player, or None.
    Uses the same tolerant matching as `is_player_currently_active`."""
    if not name:
        return None
    key = _resolve_alias(_norm(name))
    leader = _scorer_index.get(key)
    if leader is None:
        for k, ld in _scorer_index.items():
            if _name_match(key, k):
                leader = ld
                break
    if not leader:
        return None
    matches = leader.get("matches") or 0
    goals = leader.get("goals") or 0
    rate = (goals / matches) if matches else 0.0
    return {
        "source": "espn_chn1_live",
        "team": leader.get("team"),
        "matches": matches,
        "goals": goals,
        "assists": leader.get("assists") or 0,
        "rate_per_match": rate,
        "espn_active": leader.get("active"),
        "age": leader.get("age"),
    }


def snapshot_state() -> dict[str, Any]:
    """Debug/admin endpoint payload."""
    return {
        "active_players": len(_active_index),
        "scorer_rows": len(_scorer_index),
        "last_refresh_at": (
            datetime.fromtimestamp(_last_refresh_at, tz=timezone.utc).isoformat()
            if _last_refresh_at else None
        ),
        "is_stale": (
            (time.time() - _last_refresh_at) > DATA_STALE_AFTER_S
            if _last_refresh_at else True
        ),
        "top_scorers_sample": sorted(
            (
                {
                    "name": v.get("name"),
                    "team": v.get("team"),
                    "goals": v.get("goals"),
                    "assists": v.get("assists"),
                    "matches": v.get("matches"),
                    "active": v.get("active"),
                }
                for v in _scorer_index.values()
            ),
            key=lambda r: (r.get("goals") or 0),
            reverse=True,
        )[:10],
    }


# ─────────────────────────── Scheduler ───────────────────────────
_refresh_task: Optional[asyncio.Task] = None


async def _refresh_loop(db) -> None:
    """Background loop — does an initial refresh, then sleeps REFRESH_INTERVAL_S
    between subsequent runs. Errors are caught and logged so the loop never
    dies silently."""
    # Skip the very first refresh in test mode — saves API calls during pytest.
    if os.getenv("PERKLOCKS_DISABLE_NETWORK") == "1":
        logger.info("CSL ESPN scheduler skipped (PERKLOCKS_DISABLE_NETWORK=1)")
        return
    while True:
        try:
            await refresh(db)
        except Exception as e:
            logger.warning(f"CSL ESPN refresh loop iteration failed: {e}")
        await asyncio.sleep(REFRESH_INTERVAL_S)


def arm_scheduler(db) -> None:
    """Wire the refresh loop into the running asyncio loop. Idempotent."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or not loop.is_running():
        logger.debug("CSL ESPN scheduler not armed — no running event loop yet.")
        return
    _refresh_task = loop.create_task(_refresh_loop(db))
    logger.info("CSL ESPN live scheduler armed (12-h cadence)")
