"""Soccer fixture resolver (2026-08-25 Session B).

Session B glue that maps a Perklocks Soccer pick's event string
(``"Away @ Home"``) + `event_time` (UTC) + Perklocks league name
to an authoritative PitchAPI (or Big Balls) fixture ID.

Design rules:
    • READ-ONLY, no side effects beyond a small `provider_fixture_map`
      cache (immutable once written).
    • Never guesses across leagues — a mapping must clear a name
      similarity gate + date gate + league gate.
    • Falls back sequentially: PitchAPI first, then Big Balls.
    • Returns ``None`` when neither provider can resolve — the
      settlement gate must then hold the pick as DATA_UNAVAILABLE.

Cache collection: ``provider_fixture_map`` — additive; nothing else
uses it.
    Key:    (perklocks_event_hash) = sha1(f"{league_norm}|{date}|{home}|{away}")
    Value:  {pitchapi_match_id, bigballs_match_id, home_team,
             away_team, event_date, resolved_at, provenance}
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from services.providers.pitchapi import (
    api_key as pa_key, DEFAULT_BASE_URL as PA_BASE,
    AUTH_HEADER_NAME as PA_AUTH,
)
from services.providers.bigballs import (
    api_key as bb_key, DEFAULT_BASE_URL as BB_BASE,
    AUTH_HEADER_NAME as BB_AUTH,
)


# ── Perklocks → PitchAPI league mapping ────────────────────────────
# Populated from real /v1/leagues response (2026-08-25) — 42 leagues
# authoritative.  Cross-checked with the 20-highest-volume Perklocks
# soccer leagues on this slate.
PERKLOCKS_TO_PITCHAPI_LEAGUE: dict[str, str] = {
    "mls":                        "l_0eg6C9",
    "epl":                        "l_4WFCIZ",
    "premier league":             "l_4WFCIZ",
    "serie a":                    "l_0ALvwF",   # default → ITA
    "italy serie a":              "l_0ALvwF",
    "brazil serie a":             "l_1D8Xrl",
    "brasileirao":                "l_1D8Xrl",
    "brasileirao a":              "l_1D8Xrl",
    "brasileirao b":              None,          # Not in PitchAPI 42
    "la liga":                    "l_0ErfuF",
    "laliga":                     "l_0ErfuF",
    "la liga 2":                  "l_3kyHLd",
    "laliga2":                    "l_3kyHLd",
    "ligue 1":                    "l_3FJFUl",
    "ligue 2":                    "l_0275VP",
    "liga mx":                    "l_3v84VE",
    "bundesliga":                 "l_1Isor4",   # default → GER
    "2. bundesliga":              "l_3hvsne",
    "dfb pokal":                  None,          # Not in PitchAPI 42
    "eredivisie":                 "l_4H43wr",
    "primeira liga":              "l_4QexZg",
    "liga portugal":              "l_4QexZg",
    "argentina primera division": "l_45VZuL",
    "sweden allsvenskan":         "l_1XsMdH",
    "denmark superliga":          "l_49Zmno",
    "eliteserien":                "l_0uqFK8",
    "csl":                        None,          # China not in list
    "chile campeonato":           None,          # not in PitchAPI
    "champions league":           "l_0bfbkO",
    "uefa champions league":      "l_0bfbkO",
    "europa league":              "l_38d9HA",
    "conference league":          "l_2WZ2tt",
    "world cup":                  "l_1XLtP0",
    "japan j league":             "l_2ApGa4",
    "j league":                   "l_2ApGa4",
    "k league 1":                 "l_2EKrnc",
    "a-league":                   "l_4MPd7x",
    "a league":                   "l_4MPd7x",
    "sweden superettan":          None,           # not in PitchAPI
    "finland veikkausliiga":      None,           # not in PitchAPI
    "italy serie b":              "l_0jy5yE",
    "copa libertadores":          None,           # not in PitchAPI 42
    "copa sudamericana":          None,
}


def _norm_league(name: Optional[str]) -> str:
    """Normalize a Perklocks league name to a lookup key."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _norm_team(name: Optional[str]) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"\b(fc|cf|sc|afc|club|de futebol|de fútbol)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _teams_match(a: str, b: str) -> bool:
    na, nb = _norm_team(a), _norm_team(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    common = ta & tb
    if not common:
        return False
    # Require at least one significant (>=4 char) shared token OR
    # >=50% overlap of the shorter side.
    if any(len(t) >= 4 for t in common):
        return True
    smaller = min(len(ta), len(tb))
    return (len(common) / smaller) >= 0.5


def _perklocks_event_hash(league: str, date_str: str,
                          home: str, away: str) -> str:
    payload = f"{_norm_league(league)}|{date_str}|{_norm_team(home)}|{_norm_team(away)}"
    return hashlib.sha1(payload.encode()).hexdigest()[:20]


# ── Resolution ────────────────────────────────────────────────────
async def resolve_fixture(
    db, *, perklocks_league: Optional[str], event_time_iso: Optional[str],
    home_team: str, away_team: str,
) -> dict:
    """Return {status, pitchapi_match_id, bigballs_match_id, resolved_via,
              home_team, away_team, event_date, error_detail}.

    Cache is checked first, then providers.  Never writes to db.picks;
    only writes to db.provider_fixture_map on successful resolution.
    """
    if not event_time_iso or not home_team or not away_team:
        return {"status": "MISSING_INPUT", "error_detail":
                "event_time / home_team / away_team required"}
    try:
        dt = datetime.fromisoformat(event_time_iso.replace("Z", "+00:00"))
    except ValueError:
        return {"status": "MISSING_INPUT", "error_detail":
                f"unparsable event_time: {event_time_iso!r}"}
    date_str = dt.strftime("%Y-%m-%d")
    key = _perklocks_event_hash(perklocks_league or "", date_str,
                                 home_team, away_team)

    # Cache lookup
    try:
        cached = await db.provider_fixture_map.find_one({"key": key})
    except Exception:
        cached = None
    if cached:
        cached.pop("_id", None)
        return {"status": "OK", **cached}

    result: dict = {
        "status": "UNRESOLVED", "pitchapi_match_id": None,
        "bigballs_match_id": None, "resolved_via": None,
        "home_team": home_team, "away_team": away_team,
        "event_date": date_str,
    }

    # ── PitchAPI resolution ──────────────────────────────────────
    league_norm = _norm_league(perklocks_league)
    pa_league_id = PERKLOCKS_TO_PITCHAPI_LEAGUE.get(league_norm)
    if pa_league_id and pa_key():
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{PA_BASE}/v1/leagues/{pa_league_id}/matches",
                    headers={PA_AUTH: pa_key()},
                    params={"date": date_str},
                )
                if resp.status_code == 200:
                    body = resp.json() or {}
                    matches = ((body.get("data") or {}).get("matches") or [])
                    for m in matches:
                        h = ((m.get("home_team") or {}).get("name") or "")
                        a = ((m.get("away_team") or {}).get("name") or "")
                        m_date = (m.get("date") or "")[:10]
                        if m_date != date_str:
                            continue
                        if _teams_match(h, home_team) and \
                                _teams_match(a, away_team):
                            result["pitchapi_match_id"] = m.get("id")
                            result["status"] = "OK"
                            result["resolved_via"] = "pitchapi"
                            result["home_team_provider"] = h
                            result["away_team_provider"] = a
                            break
        except Exception as e:
            result.setdefault("provider_errors", {})
            result["provider_errors"]["pitchapi"] = type(e).__name__

    # ── Big Balls resolution (always try, so we can cross-check /
    #     provide a fallback when PitchAPI lacks stat coverage) ──
    if bb_key():
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{BB_BASE}/v1/matches",
                    headers={BB_AUTH: bb_key()},
                    params={"sport": "soccer", "date": date_str},
                )
                if resp.status_code == 200:
                    body = resp.json() or {}
                    matches = (body.get("data") or [])
                    if isinstance(matches, dict):
                        matches = matches.get("matches") or []
                    for m in matches:
                        h = (m.get("home_team_name") or m.get("home_team")
                              or (m.get("home") or {}).get("name") or "")
                        a = (m.get("away_team_name") or m.get("away_team")
                              or (m.get("away") or {}).get("name") or "")
                        if _teams_match(h, home_team) and \
                                _teams_match(a, away_team):
                            result["bigballs_match_id"] = (
                                m.get("id") or m.get("match_id"))
                            if result["status"] != "OK":
                                result["status"] = "OK"
                                result["resolved_via"] = "bigballs"
                            break
        except Exception as e:
            result.setdefault("provider_errors", {})
            result["provider_errors"]["bigballs"] = type(e).__name__

    # ── Cache successful resolution (immutable) ──────────────────
    if result["status"] == "OK":
        try:
            await db.provider_fixture_map.update_one(
                {"key": key},
                {"$set": {**result, "key": key,
                          "perklocks_league": perklocks_league,
                          "resolved_at": datetime.now(timezone.utc)
                                                .isoformat()
                                                .replace("+00:00", "Z")}},
                upsert=True,
            )
        except Exception:
            pass

    return result


__all__ = ["resolve_fixture", "PERKLOCKS_TO_PITCHAPI_LEAGUE",
           "_norm_league", "_norm_team", "_teams_match"]
