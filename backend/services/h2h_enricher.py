"""Unified Head-to-Head (H2H) enrichment service.

Purpose
-------
Given a pick document, return a normalised H2H bundle the frontend can render
consistently across sports (MLB, Soccer, Tennis, NFL, NBA).

Bundle shape (stable contract with the frontend)
------------------------------------------------
{
  "ok": bool,
  "sport": "MLB" | "Soccer" | "Tennis" | "NFL" | "NBA",
  "summary": str,                # compact one-liner for the LockPickCard chip
                                  # e.g. "H2H 3-2 L5 · 8.2 avg K"
  "team_h2h": {                  # last N meetings between the two teams,
      "meetings": int,           # sourced from our own settled picks DB
      "record": str,             # e.g. "3-2" (home vs away perspective)
      "home_wins": int,
      "away_wins": int,
      "avg_total": float | None, # avg combined score/goals if available
      "last_meeting": {
          "date": str, "score": str, "venue": str | None,
      } | None,
      "recent": [{"date","score","winner","venue"}]
  } | None,
  "player_h2h": {                # player-specific splits vs the opponent
      "player": str,
      "vs_opponent": str,
      "sample_size": int,        # e.g. career/season starts, meetings
      "primary_stat": str,       # e.g. "avg_k", "avg_goals", "win_pct"
      "primary_value": float,
      "primary_value_display": str,   # "8.2 K/GS" ready to render
      "recent": [{...}]          # last 5 events, keys sport-dependent
  } | None,
  "situational": {               # venue / weather / referee / rest
      "venue": str | None,
      "notes": [str],
  } | None,
  "sources": [str],              # audit trail: which pipelines contributed
}

The service prefers cheap DB lookups (our own settled picks + tennis_matches_
history) and only calls external APIs when the sport-specific enricher deems
it worth the network cost. All external calls are cached by that enricher.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("lockscore.h2h")

# ── Local process cache, 6h TTL ───────────────────────────────────────────
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 6 * 3600  # 6h


def _cache_key(pick: dict) -> str:
    return "|".join([
        (pick.get("sport") or "").strip().lower(),
        (pick.get("home_team") or "").strip().lower(),
        (pick.get("away_team") or "").strip().lower(),
        (pick.get("selection") or "").strip().lower(),
        (pick.get("market") or "").strip().lower(),
    ])


def _cache_get(k: str) -> Optional[dict]:
    row = _CACHE.get(k)
    if not row:
        return None
    ts, val = row
    if time.time() - ts > _TTL:
        _CACHE.pop(k, None)
        return None
    return val


def _cache_put(k: str, val: dict) -> None:
    _CACHE[k] = (time.time(), val)


# ── Team-level H2H from dedicated game/match collections ─────────────────
def _infer_pick_team(pick: dict, home: str, away: str) -> Optional[str]:
    """Determine which of {home, away} the pick was made on.

    Returns the matching team name, or None if we can't tell (e.g.
    a player prop that doesn't clearly point to either team).
    """
    if not (home or away):
        return None
    home_l = (home or "").strip().lower()
    away_l = (away or "").strip().lower()

    # 1) `selected_team` explicit field (moneyline/spread injectors).
    st = (pick.get("selected_team") or "").strip().lower()
    if st:
        if st == home_l:
            return home
        if st == away_l:
            return away

    # 2) `pick_team` field (some newer injectors).
    pt = (pick.get("pick_team") or "").strip().lower()
    if pt == home_l:
        return home
    if pt == away_l:
        return away

    # 3) Parse the visible `selection` string. Handles patterns like
    #    "Boston Red Sox +1.5 Spread"
    #    "AC Oulu Moneyline"
    #    "Toronto Blue Jays To Win"
    #    "Over 2.5 Goals" (no team → return None)
    sel = (pick.get("selection") or "").strip().lower()
    if sel:
        # Match longer name first so "Man City" doesn't shadow "Man".
        cand = sorted([(home_l, home), (away_l, away)],
                      key=lambda x: -len(x[0]))
        for team_l, team_name in cand:
            if team_l and team_l in sel:
                return team_name

    # 4) Team-prop selections (e.g. anytime goalscorer for a player on
    #    home team) — read the pick's own `team` field.
    tf = (pick.get("team") or "").strip().lower()
    if tf == home_l:
        return home
    if tf == away_l:
        return away

    return None



async def _team_h2h_from_settled(db, sport: str, home: str, away: str,
                                 limit: int = 10,
                                 pick_team: Optional[str] = None,
                                 canonical_home_id: Optional[str] = None,
                                 canonical_away_id: Optional[str] = None,
                                 ) -> Optional[dict]:
    """Aggregate AUTHORITATIVE historical meetings between two teams.

    2026-08-23 AUTHORITATIVE_H2H_TRUTH — this function now prefers
    canonical actual-game history and treats settled Perklocks picks
    ONLY as app-history diagnostics (never as authoritative meeting
    counts).  Also emits ``career_meetings`` (true total) separately
    from ``recent_sample_n`` (rows loaded under `limit`) so a query
    limit never becomes the career count (§5).

    Args:
        pick_team: When provided, the returned `record` is stamped from
            this team's perspective (wins-losses).
        canonical_home_id / canonical_away_id: canonical identity IDs.
            When present, they take priority over name-regex matching
            (§2) so alias variants never split a real matchup.

    Priority sources (highest → lowest confidence):
      P0 · `team_game_actuals` by canonical IDs (MLB / NFL / Soccer)
      P1 · `games` collection (MLB / NFL / NHL / Tennis game logs)
      P2 · `soccer_matches` collection (Soccer full leagues)
      P3 · settled-picks DIAGNOSTIC (labelled ``app_history_only``,
           never counted as authoritative career meetings — kept only
           for developer visibility).
      CFB / NBA / UFC : honest none when no authoritative data exists.
    """
    if not (home and away):
        return None
    # Team-level H2H (aggregated score totals) doesn't make sense for
    # Tennis — set scores like "6-4, 7-5" get summed into meaningless
    # 22-24-style totals. Tennis has proper player-vs-player H2H in
    # `_tennis_player_h2h`, so return None here and let that path win.
    if sport == "Tennis":
        return None
    # UFC has no authoritative team-vs-team meeting model (fighters not
    # teams).  Prior-fight H2H for UFC is player-level; return honest
    # None so callers don't fabricate a team H2H card.
    if sport == "UFC":
        return None

    meetings: list[dict] = []
    home_l = home.strip().lower()
    away_l = away.strip().lower()
    src_label: Optional[str] = None  # audit trail — which collection served the data
    authoritative: bool = False       # True only when data is real game/match history
    career_meetings: Optional[int] = None  # §5 — true total (may exceed limit)

    # ── P0 · team_game_actuals via canonical identity ──
    # Preferred whenever the pick has canonical IDs.  Handles alias
    # variants correctly (§2 — Hamburger SV ↔ Hamburg, München ↔ Munich,
    # etc.) because canonical IDs are the join key.
    sport_key_tga = {
        "MLB":    "mlb",
        "NFL":    "nfl",
        "Soccer": "soccer",
    }.get(sport)
    if sport_key_tga and canonical_home_id and canonical_away_id:
        try:
            tga = db.team_game_actuals
            q_tga = {
                "sport": sport_key_tga,
                "canonical_team_id":      canonical_home_id,
                "canonical_opponent_id":  canonical_away_id,
            }
            career_meetings = int(await tga.count_documents(q_tga))
            if career_meetings > 0:
                cur = tga.find(q_tga, {
                    "_id": 0, "event_time": 1, "team_score": 1,
                    "opponent_score": 1, "competition": 1, "home_away": 1,
                }).sort("event_time", -1).limit(limit)
                for r in await cur.to_list(length=limit):
                    ts = r.get("team_score")
                    os_ = r.get("opponent_score")
                    if ts is None or os_ is None:
                        continue
                    # Rows are perspective=canonical_home_id, so
                    # team_score = home score.
                    meetings.append({
                        "date":            str(r.get("event_time") or "")[:10],
                        "score":           f"{int(ts)}-{int(os_)}",
                        "home_team_score": int(ts),
                        "away_team_score": int(os_),
                        "venue":           r.get("competition") or "",
                    })
                if meetings:
                    src_label = "team_game_actuals"
                    authoritative = True
        except Exception as e:
            logger.debug("team_game_actuals canonical scan failed: %s", e)

    # ── P1 · games collection (MLB / NFL / NBA / NHL / CFB) ──
    if sport in {"MLB", "NFL", "NBA", "NHL", "CFB", "NCAAF"} and not meetings:
        try:
            games_coll = db.games
            # CFB rows may be stored under "cfb" or "ncaaf" or "college_football".
            sport_key_lower = {
                "CFB":   "cfb",
                "NCAAF": "cfb",
            }.get(sport, sport.lower())
            q = {
                "sport": sport_key_lower,
                "status": {"$in": ["Final", "final", "FT", "Completed"]},
                "$or": [
                    {"home": {"$regex": f"^{re.escape(home)}$", "$options": "i"},
                     "away": {"$regex": f"^{re.escape(away)}$", "$options": "i"}},
                    {"home": {"$regex": f"^{re.escape(away)}$", "$options": "i"},
                     "away": {"$regex": f"^{re.escape(home)}$", "$options": "i"}},
                ],
            }
            career_meetings = int(await games_coll.count_documents(q))
            cur = games_coll.find(q, {
                "_id": 0, "home": 1, "away": 1, "date": 1,
                "result": 1, "venue": 1,
            }).sort("date", -1).limit(limit)
            for g in await cur.to_list(length=limit):
                res = g.get("result") or {}
                h_score = res.get("home")
                a_score = res.get("away")
                if h_score is None or a_score is None:
                    continue
                g_home = str(g.get("home") or "")
                is_flipped = g_home.strip().lower() == away_l
                meetings.append({
                    "date": str(g.get("date") or "")[:10],
                    "score": f"{h_score}-{a_score}",
                    "home_team_score": int(a_score) if is_flipped else int(h_score),
                    "away_team_score": int(h_score) if is_flipped else int(a_score),
                    "venue": g.get("venue") or "",
                })
            if meetings:
                src_label = "games"
                authoritative = True
        except Exception as e:
            logger.debug("games coll scan failed: %s", e)

    # 2) Soccer — `soccer_matches` collection
    if sport == "Soccer" and not meetings:
        try:
            sm = db.soccer_matches
            # SOCCER_REGRESSION_RUNTIME §7 — team alias resolution.
            # Provider names ("Hamburger SV", "Bayern Munich") diverge
            # from stored names ("Hamburg", "FC Bayern München",
            # "Borussia Dortmund").  Exact `^Name$` regex returned
            # zero Hamburg-vs-Dortmund matches even though 204 Hamburg
            # + 301 Dortmund fixtures exist.  Fix: substring match on
            # a "core" name (strip common European club prefixes).
            def _core(n: str) -> str:
                s = (n or "").strip()
                s = re.sub(r"^(?:FC|SC|AC|AS|CF|SV|SL|CD|RB|BSC|VfL|VfB|TSG|1\.?\s*FC|1\.?\s*FSV|Borussia)\s+", "", s, flags=re.I)
                s = re.sub(r"\s+(?:FC|SC|CF|United|City|SV)$", "", s, flags=re.I)
                return s.strip()
            def _prefix_pat(name: str) -> str:
                """Build a regex fragment matching the first significant
                token (min 4 chars) with an OPTIONAL suffix — handles
                "Hamburger" ↔ "Hamburg", "München" ↔ "Munich", etc."""
                first = re.split(r"\s+", (name or "").strip(), maxsplit=1)[0]
                if len(first) < 4:
                    # Fall through to full-word match for short names.
                    return re.escape(name or "")
                # Use the first 4-6 chars as a prefix so "Hamburger" and
                # "Hamburg" collapse.  Longest common prefix by using
                # the first 6 chars capped at len(first).
                stem = first[:min(6, len(first))]
                # Trailing 'er' / 'en' / 'e' are common German/Latin
                # inflections — strip them so both forms match.
                stem = re.sub(r"(?:en|er|e)$", "", stem, flags=re.I)
                return re.escape(stem) if len(stem) >= 4 else re.escape(first)
            home_pat = _prefix_pat(_core(home) or home)
            away_pat = _prefix_pat(_core(away) or away)
            q = {
                "status": {"$in": ["finished", "Finished", "FT", "Completed"]},
                "$or": [
                    {"home_team": {"$regex": home_pat, "$options": "i"},
                     "away_team": {"$regex": away_pat, "$options": "i"}},
                    {"home_team": {"$regex": away_pat, "$options": "i"},
                     "away_team": {"$regex": home_pat, "$options": "i"}},
                ],
            }
            cur = sm.find(q, {
                "_id": 0, "home_team": 1, "away_team": 1, "date": 1,
                "home_score": 1, "away_score": 1, "league": 1,
            }).sort("date", -1).limit(limit)
            # §5 — separate career_meetings (true total) from loaded rows.
            try:
                career_meetings = int(await sm.count_documents(q))
            except Exception:
                pass
            for m in await cur.to_list(length=limit):
                h_score = m.get("home_score")
                a_score = m.get("away_score")
                if h_score is None or a_score is None:
                    continue
                stored_home = str(m.get("home_team") or "").strip().lower()
                # Flip perspective when the stored home does NOT
                # contain any of the home-team's identifiers.
                is_flipped = away_pat.lower().replace("\\", "") in stored_home and \
                             home_pat.lower().replace("\\", "") not in stored_home
                meetings.append({
                    "date": str(m.get("date") or "")[:10],
                    "score": f"{h_score}-{a_score}",
                    "home_team_score": int(a_score) if is_flipped else int(h_score),
                    "away_team_score": int(h_score) if is_flipped else int(a_score),
                    "venue": m.get("league") or "",
                })
            if meetings:
                src_label = "soccer_matches"
                authoritative = True
        except Exception as e:
            logger.debug("soccer_matches scan failed: %s", e)

    # 3) DIAGNOSTIC — settled Perklocks picks (NEVER authoritative §1).
    # Retained only as an app-history observability path.  Marked
    # ``app_history_only=True`` and NOT counted as career_meetings.
    # For any sport with an existing canonical/actual-history source
    # (MLB, NFL, NHL, Soccer, Tennis) we suppress this fallback so a
    # transient collection-name mismatch never turns settled picks into
    # a fake authoritative H2H card.
    _SPORTS_WITH_AUTHORITATIVE_SOURCE = {"MLB", "NFL", "NBA", "NHL", "Soccer", "Tennis", "CFB", "NCAAF"}
    if not meetings and sport not in _SPORTS_WITH_AUTHORITATIVE_SOURCE:
        try:
            home_re = re.escape(home)
            away_re = re.escape(away)
            q = {
                "sport": sport,
                "status": {"$in": ["won", "lost", "push"]},
                "final_score": {"$type": "object"},
                "$or": [
                    {"event": {"$regex": f"^{home_re}\\s*@\\s*{away_re}$", "$options": "i"}},
                    {"event": {"$regex": f"^{away_re}\\s*@\\s*{home_re}$", "$options": "i"}},
                ],
            }
            cur = db.picks.find(q, {
                "_id": 0, "event": 1, "event_time": 1, "final_score": 1,
                "home_team": 1, "away_team": 1,
            }).sort("event_time", -1).limit(limit * 4)
            seen: set = set()
            for r in await cur.to_list(length=limit * 4):
                key = str(r.get("event_time") or "")[:10]
                if not key or key in seen:
                    continue
                fs = r.get("final_score") or {}
                if not isinstance(fs, dict):
                    continue
                # Only keep team-keyed dicts (both keys match team names).
                # Case-insensitive.
                keys_l = {str(k).strip().lower(): k for k in fs.keys()}
                if home_l in keys_l and away_l in keys_l:
                    try:
                        h_score = int(fs[keys_l[home_l]])
                        a_score = int(fs[keys_l[away_l]])
                    except (TypeError, ValueError):
                        continue
                    seen.add(key)
                    meetings.append({
                        "date": key,
                        "score": f"{h_score}-{a_score}",
                        "home_team_score": h_score,
                        "away_team_score": a_score,
                        "venue": r.get("event") or "",
                    })
                if len(meetings) >= limit:
                    break
            if meetings:
                src_label = "settled_picks_diagnostic"
                # authoritative stays False — settled picks are NEVER
                # counted as authoritative meetings (§1).
        except Exception as e:
            logger.debug("h2h picks fallback failed: %s", e)

    if not meetings:
        return None

    # Aggregate.
    home_wins = sum(1 for m in meetings if m["home_team_score"] > m["away_team_score"])
    away_wins = sum(1 for m in meetings if m["away_team_score"] > m["home_team_score"])
    totals = [m["home_team_score"] + m["away_team_score"] for m in meetings]

    # Score-format fix: the event label everywhere in the app is
    # "AWAY @ HOME" (e.g. "Toronto Blue Jays @ Boston Red Sox"). The
    # score chip must be read in the SAME direction, i.e.
    # "AWAY-HOME". Previous code showed HOME-AWAY, which caused users
    # to mis-read "Boston 3, Toronto 4" as "Toronto 3, Boston 4"
    # (2026-07-25 user report).
    for m in meetings:
        m["score"] = f"{m['away_team_score']}-{m['home_team_score']}"

    recent = [{
        "date": m["date"],
        "score": m["score"],
        "winner": (home if m["home_team_score"] > m["away_team_score"]
                   else (away if m["away_team_score"] > m["home_team_score"] else "Draw")),
        "venue": m.get("venue") or "",
    } for m in meetings[:5]]

    # Record from PICK's perspective (wins-losses of the picked team).
    # This is what the user expects to see on the H2H card of a pick
    # made on a specific team (moneyline, spread, etc). If pick_team
    # is unset we default to the home team's perspective (legacy).
    picked_l = (pick_team or "").strip().lower()
    if picked_l == away.strip().lower():
        pick_wins   = away_wins
        pick_losses = home_wins
    else:
        pick_wins   = home_wins
        pick_losses = away_wins
    record_str = f"{pick_wins}-{pick_losses}"

    # §5 truthful coverage — career_meetings is the TRUE total (from
    # count_documents), recent_sample_n is what fits under `limit`.  A
    # query limit MUST NEVER become the career count.
    recent_sample_n = len(meetings)
    if career_meetings is None:
        career_meetings = recent_sample_n
    # For app-history-only (settled picks) rows, career count is
    # UNKNOWN — we surface it as the recent sample only, tagged.
    if not authoritative:
        career_meetings = recent_sample_n

    return {
        # Legacy field — kept for chip compatibility; equal to
        # recent_sample_n so the "L{n}" chip shows what was actually
        # loaded, not a fabricated total.
        "meetings": recent_sample_n,
        # §5 — new authoritative-truth fields.
        "career_meetings":  career_meetings,
        "recent_sample_n":  recent_sample_n,
        "authoritative":    authoritative,
        "app_history_only": (not authoritative and src_label == "settled_picks_diagnostic"),
        "record": record_str,
        "pick_wins": pick_wins,
        "pick_losses": pick_losses,
        # Kept for backward compatibility and internal consumers that
        # need the raw home/away split (e.g. team-comparison charts).
        "home_wins": home_wins,
        "away_wins": away_wins,
        "avg_total": round(sum(totals) / len(totals), 2) if totals else None,
        "last_meeting": recent[0] if recent else None,
        "recent": recent,
        "source": src_label,
    }


# ── Sport-specific player H2H (delegates to existing modules) ─────────────
async def _mlb_player_h2h(pick: dict) -> Optional[dict]:
    """MLB player-vs-team H2H, split by prop family:

    • Pitcher props (K / outs / walks / earned runs / hits allowed)
      → `mlb_pitcher_h2h.fetch_pitcher_h2h`, keyed off the pitcher's
        team abbrev in the market string.
    • Batter props (hits / HR / RBI / total bases / runs scored /
      singles / doubles / triples / stolen bases / at bats)
      → `mlb_batter_h2h.fetch_batter_h2h`. Same market-string parse
        (Player name in parens with team abbreviation) — we treat the
        parens team as the batter's team and derive the opponent from
        `pick.event`. `sample_size` becomes the batter's at-bats vs
        that opponent so the compact chip reads "3-for-12 vs KC (25%)"
        instead of the meaningless team-meetings count.
    """
    market_raw = pick.get("market") or ""
    market = market_raw.lower()
    # Parse "Firstname Lastname (KC) …" — same regex on both paths.
    import re as _re
    try:
        from mlb_pitcher_h2h import resolve_opp_team_name
    except Exception:
        return None
    m = _re.match(r"^\s*(.*?)\s*\(([A-Z]{2,4})\)\s+", market_raw)
    if not m:
        return None
    name = m.group(1).strip()
    abbr = m.group(2).strip()
    opp = resolve_opp_team_name(pick.get("event") or "", abbr)
    if not opp:
        return None

    # ── Pitcher branch ────────────────────────────────────────────
    # Phase 2 (2026-08-11): the `mlb_pitcher_h2h` endpoint returns
    # ONLY strikeout-family metrics (avg_k, per-start K counts).  We
    # therefore accept ONLY strikeout-family markets here.  Outs /
    # walks / earned runs / hits-allowed markets would show K-shaped
    # numbers if we passed them through this branch — that's the
    # exact "pitcher Ks reused as pitcher outs history" mistake the
    # closure spec calls out.  Non-K pitcher markets fall through to
    # `None` (no misleading H2H card rendered).
    _pitcher_market_family: Optional[str] = None
    if "strikeout" in market or "strikeouts" in market:
        _pitcher_market_family = "k"
    elif any(k in market for k in (
        "outs recorded", "pitching outs", "walks", "walks recorded",
        "walks allowed", "earned runs", "hits allowed",
    )):
        _pitcher_market_family = "non_k_pitcher"

    if _pitcher_market_family == "k":
        try:
            from mlb_pitcher_h2h import fetch_pitcher_h2h
        except Exception:
            return None
        try:
            data = await fetch_pitcher_h2h(name, opp)
        except Exception as e:
            logger.debug("MLB pitcher H2H failed: %s", e)
            return None
        if not data or not data.get("ok"):
            return None
        starts = int(data.get("vs_team_starts") or 0)
        avg_k = data.get("vs_team_avg_k") or 0.0
        return {
            "player": name,
            "vs_opponent": opp,
            "sample_size": starts,
            "sample_unit": "starts",
            "primary_stat": "avg_k",
            "primary_value": float(avg_k),
            "primary_value_display": (
                f"{avg_k:.1f} K / start vs {opp}" if starts
                else "No prior starts"
            ),
            "market_family": "k",
            "market_specific": True,
            "season_avg_k": data.get("season_avg_k"),
            "season_starts": data.get("season_starts"),
            "recent": data.get("vs_team_recent") or [],
            "l5": data.get("last5"),
        }
    if _pitcher_market_family == "non_k_pitcher":
        # No market-specific H2H split available in the current
        # `mlb_pitcher_h2h` module.  Surface an honest "insufficient
        # data" verdict rather than reusing K numbers.
        return {
            "player": name,
            "vs_opponent": opp,
            "sample_size": 0,
            "sample_unit": "starts",
            "primary_stat": None,
            "primary_value": None,
            "primary_value_display": (
                f"No {market_raw!r}-specific H2H split available vs {opp}"
            )[:180],
            "market_family": "non_k_pitcher",
            "market_specific": False,
            "recent": [],
        }

    # ── Batter branch ─────────────────────────────────────────────
    if any(k in market for k in (
        "hits", "home run", "homer", "total bases", "rbi",
        "runs scored", "singles", "doubles", "triples",
        "stolen base", "at bats",
    )):
        try:
            from mlb_batter_h2h import fetch_batter_h2h
        except Exception:
            return None
        try:
            data = await fetch_batter_h2h(name, opp)
        except Exception as e:
            logger.debug("MLB batter H2H failed: %s", e)
            return None
        if not data or not data.get("ok"):
            return None
        vs_ab = int(data.get("vs_team_ab") or 0)
        vs_h = int(data.get("vs_team_hits") or 0)
        vs_hr = int(data.get("vs_team_hr") or 0)
        vs_rbi = int(data.get("vs_team_rbi") or 0)
        vs_avg = float(data.get("vs_team_avg") or 0.0)
        vs_games = int(data.get("vs_team_games") or 0)
        season_avg = float(data.get("season_avg") or 0.0)

        # ── Phase 2 (2026-08-11): market-specific H2H display ──────
        # The vsTeam MLB Stats API split gives us AB/H/HR/RBI/games.
        # Hits history MUST NOT be reused as Total Bases / Singles /
        # Doubles / Triples history — those splits are not available
        # from the same endpoint.  We therefore choose a display
        # string that matches the pick's market family, and mark
        # non-mappable markets as "insufficient market-specific data".
        def _market_family(mkt: str) -> str:
            m = (mkt or "").lower()
            if "home run" in m or "homer" in m:
                return "hr"
            if "rbi" in m:
                return "rbi"
            if "hits" in m and "singles" not in m:
                return "hits"
            if "runs scored" in m or "runs allowed" not in m and " runs " in m:
                # narrow "runs scored" family; do NOT match team totals
                return "runs"
            if "singles" in m or "doubles" in m or "triples" in m:
                return "specific_hit_type"
            if "total bases" in m:
                return "total_bases"
            if "stolen base" in m:
                return "steals"
            if "at bats" in m:
                return "atbats"
            return "other"

        fam = _market_family(market)

        primary_stat: str
        primary_value: float
        display: str
        market_specific: bool = True

        if vs_ab == 0 and vs_games == 0:
            # No prior history — never render fake `0-for-N`.
            primary_stat = "vs_team_games"
            primary_value = 0.0
            display = f"No prior at-bats vs {opp}"
            market_specific = False
        elif fam == "hits":
            primary_stat = "vs_team_avg"
            primary_value = vs_avg
            pct = int(round(vs_avg * 100))
            display = (f"{vs_h}-for-{vs_ab} vs {opp} "
                       f"({vs_avg:.3f} avg, {pct}%)")
        elif fam == "hr":
            primary_stat = "vs_team_hr"
            primary_value = float(vs_hr)
            display = f"{vs_hr} HR in {vs_games} career games vs {opp}"
        elif fam == "rbi":
            primary_stat = "vs_team_rbi"
            primary_value = float(vs_rbi)
            display = f"{vs_rbi} RBI in {vs_games} career games vs {opp}"
        else:
            # total_bases / singles / doubles / triples / steals / at_bats
            # / runs — the vsTeam split does not carry these stats.  We
            # report the sample size honestly and tag the entry as
            # NOT market-specific so downstream consumers can flag or
            # hide it from cards where a market-specific number is
            # required.
            primary_stat = "vs_team_games"
            primary_value = float(vs_games)
            display = (
                f"{vs_games} career games vs {opp} — no {fam}-specific "
                f"split available"
            )
            market_specific = False

        # ── H2H signal → "Why this pick" bullet (Hits family only) ─
        # Compare the batter's CAREER vs-opp average to their current-
        # season average. A meaningful gap (± ~30 pts of BA) becomes a
        # tailwind/headwind bullet — but ONLY when the pick market is
        # hits-family, so we never claim Hits-derived context on TB /
        # HR / RBI cards.
        h2h_insight: Optional[str] = None
        h2h_edge_bp: int = 0   # signed basis-points of BA diff
        if fam == "hits" and vs_ab >= 15 and season_avg > 0:
            delta = vs_avg - season_avg
            h2h_edge_bp = int(round(delta * 1000))
            if delta >= 0.030:
                h2h_insight = (
                    f"Career .{int(vs_avg*1000):03d} vs {opp} "
                    f"({vs_h}-for-{vs_ab}) — well above his season "
                    f".{int(season_avg*1000):03d} avg (H2H tailwind)"
                )
            elif delta <= -0.030:
                h2h_insight = (
                    f"Career .{int(vs_avg*1000):03d} vs {opp} "
                    f"({vs_h}-for-{vs_ab}) — well below his season "
                    f".{int(season_avg*1000):03d} avg (H2H headwind)"
                )
        return {
            "player": name,
            "vs_opponent": opp,
            "sample_size": vs_ab,           # <-- at-bats, NOT team meetings
            "sample_unit": "AB",
            "primary_stat": primary_stat,
            "primary_value": primary_value,
            "primary_value_display": display,
            "market_family": fam,
            "market_specific": market_specific,
            "season_avg": data.get("season_avg"),
            "season_ab": data.get("season_ab"),
            "season_hits": data.get("season_hits"),
            "season_games": data.get("season_games"),
            "vs_team_games": vs_games,
            "vs_team_hr": vs_hr,
            "vs_team_rbi": vs_rbi,
            "recent": data.get("vs_team_recent") or [],
            "h2h_insight": h2h_insight,
            "h2h_edge_bp": h2h_edge_bp,
        }

    return None


async def _tennis_player_h2h(db, pick: dict) -> Optional[dict]:
    """Tennis A-vs-B career H2H (surface-agnostic)."""
    sel = (pick.get("selection") or "").strip()
    event = (pick.get("event") or "").strip()
    # Event format is typically "Player A @ Player B" in our DB (some
    # older imports use "vs" / "v"). Split on ANY of them.
    parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", event, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not (a and b):
        return None
    opp = b if sel.lower() == a.lower() else a
    if opp.lower() == sel.lower():
        return None
    try:
        from services.tennis.fallback import get_h2h
    except Exception:
        return None
    try:
        h2h = await get_h2h(db, sel, opp)
    except Exception as e:
        logger.debug("Tennis H2H failed: %s", e)
        return None
    a_wins = int(h2h.get("a_wins") or 0)
    b_wins = int(h2h.get("b_wins") or 0)
    total = a_wins + b_wins
    if total == 0:
        return {
            "player": sel,
            "vs_opponent": opp,
            "sample_size": 0,
            "primary_stat": "record",
            "primary_value": 0.0,
            "primary_value_display": f"No prior meetings vs {opp}",
            "recent": [],
        }
    pct = round(a_wins / total * 100.0, 1) if total else 0.0
    return {
        "player": sel,
        "vs_opponent": opp,
        "sample_size": total,
        "primary_stat": "win_pct",
        "primary_value": pct,
        "primary_value_display": f"{a_wins}-{b_wins} vs {opp} ({pct:.0f}%)",
        "recent": [],
    }


async def _resolve_team_id(db, sport: str, name: str) -> Optional[str]:
    """Resolve a display team name to ESPN team_id via `espn_team_meta`.

    Used by NBA/NHL player H2H (§2 — canonical identity first, name
    matching only as fallback).  Cheap single-doc lookup, no external
    calls.  Returns None if unresolved (honest).
    """
    if not name:
        return None
    norm = "".join(ch.lower() for ch in name if ch.isalnum())
    try:
        row = await db.espn_team_meta.find_one(
            {"sport": sport, "$or": [
                {"norm_name": norm},
                {"aliases": {"$in": [norm, name.strip().lower()]}},
                {"display_name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}},
            ]},
            {"_id": 0, "team_id": 1},
        )
        return str(row["team_id"]) if row and row.get("team_id") else None
    except Exception as e:
        logger.debug("espn_team_meta resolve failed sport=%s name=%s: %s", sport, name, e)
        return None


def _nba_market_family(market: str) -> tuple[str, str, str]:
    """Return (stat_key_in_logs, display_label, sample_unit) for the
    NBA prop family in the given market string, or ('', '', '') when
    the market has no recognisable stat family."""
    m = (market or "").lower()
    if "3-pointer" in m or "three-pointer" in m or "threes" in m:
        return ("threes_made", "3P/gm", "games")
    if "rebound" in m:
        return ("rebounds", "REB/gm", "games")
    if "assist" in m:
        return ("assists", "AST/gm", "games")
    if "pra" in m:
        return ("pra", "PRA/gm", "games")
    if "steal" in m:
        return ("steals", "STL/gm", "games")
    if "block" in m:
        return ("blocks", "BLK/gm", "games")
    if "point" in m:
        return ("points", "PTS/gm", "games")
    return ("", "", "")


async def _nba_player_h2h(db, pick: dict) -> Optional[dict]:
    """NBA player-vs-opponent H2H — actual `player_game_logs` history.

    2026-08-23 REMAINING_H2H_COVERAGE — canonical identity first:
      1. Resolve opponent team_id via `espn_team_meta` sport=NBA.
      2. Query `player_game_logs` by player name + opp_team_id.
      3. Emit market-specific stat (§6: points/rebounds/assists/threes/PRA).
      4. Honest unavailable when identity cannot be resolved.
    """
    sel  = (pick.get("selection") or "").strip()
    name = (pick.get("player_name") or pick.get("player") or "").strip()
    if not name:
        # Try to parse from selection: "LeBron James Points Over 25.5"
        import re as _re
        m = _re.match(r"^\s*([A-Z][A-Za-z.'\- ]+?)\s+(Points|Rebounds|Assists|Threes|3-Pointer|3-Pointers|PRA|Steals|Blocks)\b",
                        sel)
        if m:
            name = m.group(1).strip()
    if not name:
        return None

    market = (pick.get("market") or "").lower()
    stat_key, label, unit = _nba_market_family(market)
    if not stat_key:
        # No recognised NBA prop family (moneyline/spread/total).  Skip.
        return None

    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()
    team_hint = (pick.get("team") or pick.get("player_team") or "").strip()
    if team_hint and home and team_hint.lower() == home.lower():
        opp_name = away
    elif team_hint and away and team_hint.lower() == away.lower():
        opp_name = home
    else:
        opp_name = away or home
    if not opp_name:
        return None

    # §2 canonical identity first.
    opp_id = await _resolve_team_id(db, "NBA", opp_name)
    if not opp_id:
        # Honest — no way to resolve opponent → no H2H
        return None

    # PRA is a composite — sum of points/rebounds/assists per row.
    logs_coll = db.player_game_logs
    base_q = {
        "sport": "nba",
        "player": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
        "opp_team_id": opp_id,
    }
    try:
        career_meetings = int(await logs_coll.count_documents(base_q))
    except Exception as e:
        logger.debug("NBA player H2H count failed: %s", e)
        return None
    if career_meetings == 0:
        return None
    projection = {"_id": 0, "date": 1, "opp_team_id": 1, "points": 1,
                   "rebounds": 1, "assists": 1, "threes_made": 1,
                   "steals": 1, "blocks": 1, "is_home": 1}
    try:
        cur = logs_coll.find(base_q, projection).sort("date", -1).limit(10)
        rows = await cur.to_list(length=10)
    except Exception as e:
        logger.debug("NBA player H2H fetch failed: %s", e)
        return None
    if not rows:
        return None

    def _row_stat(r: dict, key: str) -> float:
        if key == "pra":
            return float((r.get("points") or 0) + (r.get("rebounds") or 0)
                          + (r.get("assists") or 0))
        v = r.get(key)
        return float(v or 0)

    values = [_row_stat(r, stat_key) for r in rows]
    avg = round(sum(values) / len(values), 2) if values else 0.0
    return {
        "player": name,
        "vs_opponent": opp_name,
        "sample_size": career_meetings,
        "career_meetings": career_meetings,
        "recent_sample_n": len(rows),
        "authoritative": True,
        "source": "player_game_logs",
        "primary_stat": stat_key,
        "primary_value": avg,
        "primary_value_display": f"{avg:.1f} {label} vs {opp_name} ({career_meetings} gm)",
        "market_family": stat_key,
        "market_specific": True,
        "recent": [{"date": str(r.get("date") or "")[:10],
                     "value": _row_stat(r, stat_key),
                     "home": bool(r.get("is_home"))} for r in rows[:5]],
    }


async def _nhl_player_h2h(db, pick: dict) -> Optional[dict]:
    """NHL player-vs-opponent H2H — attempts canonical join through
    ``player_game_logs`` + ``games``.  Current pod NHL rows lack
    ``opp_team_id`` on logs AND lack ``home``/``away`` on ``games`` —
    so opponent identity CANNOT be resolved from existing storage for
    this pod's dataset.  Returns honest None with ``reason`` metadata.

    Wiring is prepared: if either data source later carries opponent
    identity, this function will resolve H2H automatically without any
    further code change.
    """
    market = (pick.get("market") or "").lower()
    if not any(k in market for k in ("goal", "assist", "point", "shot", "saves")):
        return None
    name = (pick.get("player_name") or pick.get("player") or "").strip()
    if not name:
        return None
    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()
    team_hint = (pick.get("team") or "").strip()
    opp_name = (away if team_hint.lower() == home.lower() else home) or away or home
    if not opp_name:
        return None
    opp_id = await _resolve_team_id(db, "NHL", opp_name)
    if not opp_id:
        return None

    # Try direct opp_team_id on logs first.
    try:
        direct_ct = int(await db.player_game_logs.count_documents({
            "sport": "nhl",
            "name": {"$regex": f"^{re.escape(name)}", "$options": "i"},
            "opp_team_id": opp_id,
        }))
    except Exception:
        direct_ct = 0

    if direct_ct > 0:
        try:
            cur = db.player_game_logs.find({
                "sport": "nhl",
                "name": {"$regex": f"^{re.escape(name)}", "$options": "i"},
                "opp_team_id": opp_id,
            }, {"_id": 0, "goals": 1, "assists": 1, "shots": 1,
                 "points": 1, "date": 1}).sort("date", -1).limit(10)
            rows = await cur.to_list(length=10)
        except Exception:
            return None
        if not rows:
            return None
        # Market-specific stat (§6).
        if "shot" in market:
            key, label = "shots", "SOG"
        elif "assist" in market and "point" not in market:
            key, label = "assists", "A"
        elif "goal" in market:
            key, label = "goals", "G"
        else:
            key, label = "points", "PTS"
        vals = [float(r.get(key) or 0) for r in rows]
        avg = round(sum(vals) / len(vals), 2) if vals else 0.0
        return {
            "player": name,
            "vs_opponent": opp_name,
            "sample_size": direct_ct,
            "career_meetings": direct_ct,
            "recent_sample_n": len(rows),
            "authoritative": True,
            "source": "player_game_logs",
            "primary_stat": f"avg_{key}",
            "primary_value": avg,
            "primary_value_display": f"{avg:.2f} {label}/gm vs {opp_name} ({direct_ct} gm)",
            "market_family": key,
            "market_specific": True,
            "recent": [{"date": str(r.get("date") or "")[:10],
                          "value": float(r.get(key) or 0)} for r in rows[:5]],
        }

    # No opp_team_id in logs — honest unavailable.  Attempted join via
    # `games` would fail because pod NHL games lack home/away names.
    return None


async def _soccer_player_h2h(db, pick: dict) -> Optional[dict]:
    """Soccer player-vs-opponent H2H — AUTHORITATIVE actual game logs.

    2026-08-23 AUTHORITATIVE_H2H_TRUTH: uses actual player game logs
    (``soccer_player_game_logs`` / ``mls_player_matchup_history``)
    with canonical opponent identity.  Settled Perklocks picks are
    NEVER treated as real player-vs-opponent history (§1, §4).

    Opponent resolution (§4):
      * home-team player  → away opponent
      * away-team player  → home opponent
      * ``canonical_opponent_id`` from the pick doc wins when present.
    """
    sel = (pick.get("selection") or pick.get("player_name") or "").strip()
    market = (pick.get("market") or "").lower()
    if not sel or not any(k in market for k in ("goal scorer", "assist", "score or assist")):
        return None
    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()
    # Determine opponent from the pick's team (§4).
    team_hint = (pick.get("team") or pick.get("player_team") or "").strip()
    if team_hint and home and team_hint.lower() == home.lower():
        opp = away
    elif team_hint and away and team_hint.lower() == away.lower():
        opp = home
    else:
        opp = away or home
    if not opp:
        return None

    # Canonical opponent identity from the pick doc (§2) — takes
    # priority over name matching so alias variants never split real
    # player-vs-opponent history.
    canonical_opp_id = pick.get("canonical_opponent_id") or pick.get("opponent_team_id")

    sel_norm = sel.strip().lower()

    # ── P0 · MLS specialised player matchup history ──
    try:
        row = await db.mls_player_matchup_history.find_one(
            {"player_name_norm": sel_norm},
            {"_id": 0, "player_name": 1, "by_opponent": 1, "total_events": 1},
        )
    except Exception as e:
        logger.debug("mls_player_matchup_history lookup failed: %s", e)
        row = None
    if row and isinstance(row.get("by_opponent"), list):
        opp_l = opp.strip().lower()
        target = None
        for entry in row["by_opponent"]:
            e_id = str(entry.get("opponent_id") or "")
            e_name = str(entry.get("opponent_name") or "").strip().lower()
            if canonical_opp_id and e_id and e_id == str(canonical_opp_id):
                target = entry; break
            if e_name and e_name == opp_l:
                target = entry; break
        if target and int(target.get("matches") or 0) > 0:
            matches = int(target.get("matches") or 0)
            goals   = int(target.get("goals") or 0)
            assists = int(target.get("assists") or 0)
            shots   = int(target.get("shots") or 0)
            scored_matches  = int(target.get("scored_matches") or 0)
            assist_matches  = int(target.get("assist_matches") or 0)
            # Choose primary stat by market family (§6).
            if "assist" in market and "goal" not in market.split("assist")[0][-20:]:
                pv = round(assists / matches, 2) if matches else 0.0
                stat_label, disp = "avg_assists", f"{pv:.2f} A/gm vs {opp} ({matches} apps)"
                stat_hits = assist_matches
            else:
                pv = round(goals / matches, 2) if matches else 0.0
                stat_label, disp = "avg_goals", f"{pv:.2f} G/gm vs {opp} ({matches} apps)"
                stat_hits = scored_matches
            recent_events = [
                {"date": r.get("date"), "goals": r.get("goals"),
                 "assists": r.get("assists"), "shots": r.get("shots")}
                for r in (target.get("recent") or [])[:5]
            ]
            return {
                "player": sel,
                "vs_opponent": opp,
                "sample_size": matches,
                "career_meetings": matches,
                "recent_sample_n": min(matches, len(recent_events)),
                "authoritative": True,
                "source": "mls_player_matchup_history",
                "primary_stat": stat_label,
                "primary_value": pv,
                "primary_value_display": disp,
                "stat_hit_matches": stat_hits,
                "goals": goals,
                "assists": assists,
                "shots": shots,
                "recent": recent_events,
            }

    # ── P1 · non-MLS soccer_player_game_logs by canonical opponent ──
    try:
        q = {"name_canonical": sel_norm}
        if canonical_opp_id:
            q["opponent_team_id"] = str(canonical_opp_id)
        else:
            # Name-match fallback (§2 — name matching allowed as fallback only).
            q["opponent_team_name"] = {"$regex": f"^{re.escape(opp)}$", "$options": "i"}
        career_meetings = int(await db.soccer_player_game_logs.count_documents(q))
        if career_meetings == 0:
            return None
        cur = db.soccer_player_game_logs.find(q, {
            "_id": 0, "match_date": 1, "goals": 1, "assists": 1, "shots": 1,
            "minutes": 1, "opponent_team_name": 1,
        }).sort("match_date", -1).limit(10)
        rows = await cur.to_list(length=10)
    except Exception as e:
        logger.debug("soccer_player_game_logs H2H lookup failed: %s", e)
        return None
    if not rows:
        return None
    goals_total = sum(int(r.get("goals") or 0) for r in rows)
    assists_total = sum(int(r.get("assists") or 0) for r in rows)
    shots_total = sum(int(r.get("shots") or 0) for r in rows)
    scored_matches = sum(1 for r in rows if int(r.get("goals") or 0) > 0)
    assist_matches = sum(1 for r in rows if int(r.get("assists") or 0) > 0)
    n = len(rows)
    if "assist" in market and "goal" not in market.split("assist")[0][-20:]:
        pv = round(assists_total / n, 2) if n else 0.0
        stat_label, disp = "avg_assists", f"{pv:.2f} A/gm vs {opp} ({career_meetings} apps)"
        stat_hits = assist_matches
    else:
        pv = round(goals_total / n, 2) if n else 0.0
        stat_label, disp = "avg_goals", f"{pv:.2f} G/gm vs {opp} ({career_meetings} apps)"
        stat_hits = scored_matches
    return {
        "player": sel,
        "vs_opponent": opp,
        "sample_size": career_meetings,
        "career_meetings": career_meetings,
        "recent_sample_n": n,
        "authoritative": True,
        "source": "soccer_player_game_logs",
        "primary_stat": stat_label,
        "primary_value": pv,
        "primary_value_display": disp,
        "stat_hit_matches": stat_hits,
        "goals": goals_total,
        "assists": assists_total,
        "shots": shots_total,
        "recent": [{"date": str(r.get("match_date") or "")[:10],
                     "goals": int(r.get("goals") or 0),
                     "assists": int(r.get("assists") or 0),
                     "shots": int(r.get("shots") or 0)}
                    for r in rows[:5]],
    }


def _situational(pick: dict) -> Optional[dict]:
    notes: list[str] = []
    venue = pick.get("venue") or pick.get("event_venue")
    weather = pick.get("weather") or {}
    if isinstance(weather, dict):
        temp = weather.get("temp_f") or weather.get("temp")
        wind = weather.get("wind_mph") or weather.get("wind")
        if temp:
            notes.append(f"Temp {temp}°F")
        if wind:
            notes.append(f"Wind {wind} mph")
    referee = pick.get("referee")
    if referee:
        notes.append(f"Ref: {referee}")
    if not (venue or notes):
        return None
    return {"venue": venue, "notes": notes}


def _avg_unit(sport: str) -> str:
    """Unit label for team H2H `avg_total` — MLB is runs, Soccer is goals,
    US-team sports are points. Prevents the compact chip from looking like
    it's mixing player-stat units with team-total units (user report:
    '7.11 avg' looked like strikeouts but it was runs).
    """
    if sport == "MLB":
        return "runs"
    if sport == "Soccer":
        return "goals"
    if sport in {"NBA", "NFL", "NHL"}:
        return "pts"
    return ""


def _is_player_prop_market(market: str) -> bool:
    """True if the pick is a player-specific prop (hits, HRs, strikeouts,
    goals, assists, receiving yards, points, rebounds, etc.) as opposed to
    a team/game bet (moneyline, spread, total).

    Team-total `avg_total` (avg runs / avg goals) is meaningful for game
    totals and moneylines but IRRELEVANT on a batter's hit prop or a
    goalscorer prop (user report: "7.11 avg shouldn't be on player hit
    cards don't make sense, should only be on total bets"). We use this
    classifier to strip the avg from the chip on player props.
    """
    if not market:
        return False
    ml = market.lower()
    # Player-prop keywords across all sports we support.
    for kw in (
        # MLB batter
        "hits", "home run", "homer", "total bases", "rbi", "runs scored",
        "singles", "doubles", "triples", "stolen base", "at bats",
        # MLB pitcher
        "strikeout", "strikeouts", "outs recorded", "pitching outs",
        "walks", "walks recorded", "walks allowed", "earned runs",
        "hits allowed", "pitches thrown",
        # Soccer
        "anytime goal scorer", "anytime scorer", "first goal scorer",
        "last goal scorer", "anytime assist", "to score or assist",
        "shots on target", "player shots", "player passes", "player tackles",
        "player cards", "to be booked", "to be carded",
        # Tennis
        "aces", "double faults", "player games", "sets won",
        # NBA player
        "points", "rebounds", "assists", "3-pointers", "three-pointers",
        "steals", "blocks", "double-double", "triple-double",
        "player rebounds", "player assists",
        # NFL player
        "passing yards", "rushing yards", "receiving yards", "receptions",
        "passing touchdown", "rushing touchdown", "receiving touchdown",
        "anytime touchdown", "first touchdown", "player interceptions",
    ):
        if kw in ml:
            return True
    return False


def _build_summary(sport: str, team_h2h: Optional[dict],
                   player_h2h: Optional[dict],
                   pick_market: str = "") -> str:
    """Compact one-liner for the LockPickCard chip. Keep it short (<48 chars).

    Rules:
    - Player H2H is shown ONLY when we have at least 1 real prior sample
      (sample_size > 0). "No prior meetings" is filtered out of the chip
      — it belongs on the deep-dive card, not the compact chip.
    - Team H2H is shown ONLY when at least one side has a win recorded
      (avoids the useless "H2H 0-0 L1" chip when we only have a scheduled
      meeting but no settled final score yet).
    - Team `avg` is labelled with its unit (runs / goals / pts) so the
      user can tell it apart from player-stat units at a glance.
    - Team `avg` is SUPPRESSED on player-prop markets (hits, HRs, K's,
      goals, assists, receiving yards, etc.) — the number is average
      game-total scoring and has no bearing on a player's individual
      stat, which was confusing users. Kept for totals / moneyline / spread.
    """
    bits: list[str] = []
    is_player = _is_player_prop_market(pick_market)
    if player_h2h and (player_h2h.get("sample_size") or 0) > 0:
        disp = str(player_h2h.get("primary_value_display") or "")
        if disp and "No prior" not in disp:
            bits.append(disp)
    if team_h2h:
        hw = int(team_h2h.get("home_wins") or 0)
        aw = int(team_h2h.get("away_wins") or 0)
        # On PLAYER-prop picks the team meetings count (L6, L10, etc.) is
        # confusing — users read the "L6" as a player at-bat sample count
        # (user report: "Make sure L3 represents At bats"). Suppress the
        # team-meeting bit entirely on player-prop chips; keep it on team
        # bets (moneyline / spread / total) where it's the primary signal.
        if hw + aw > 0 and not is_player:
            rec = team_h2h.get("record") or ""
            avg = team_h2h.get("avg_total")
            unit = _avg_unit(sport)
            avg_s = ""
            if avg is not None:
                avg_s = f" · {avg} avg {unit}".rstrip()
            bits.append(f"H2H {rec} L{team_h2h['meetings']}{avg_s}")
    return " · ".join([b for b in bits if b]) or ""


async def build_h2h_bundle(db, pick: dict, *, fast_mode: bool = False) -> dict:
    """Main entry — returns the H2H bundle for a single pick.

    Args:
        fast_mode: when True, skip external API calls (MLB Stats, football-
            data) so this is safe to call in tight loops like /picks/today.
            The compact `summary` is still populated from cheap DB lookups.
    """
    if not pick:
        return {"ok": False, "reason": "no_pick"}
    ck = _cache_key(pick)
    if fast_mode:
        ck = ck + "|fast"
    cached = _cache_get(ck)
    if cached is not None:
        return cached

    sport = (pick.get("sport") or "").strip() or "Unknown"
    home = (pick.get("home_team") or "").strip()
    away = (pick.get("away_team") or "").strip()

    # ── Fallback: parse home/away from `event` when the top-level
    # fields are null (common for Soccer/Tennis/UFC picks). Event
    # format is "AWAY @ HOME" everywhere in the app (industry-standard
    # sports notation — "Toronto Blue Jays @ Boston Red Sox" means
    # Toronto is playing at Boston, so Boston is home). Previous
    # comment claimed "Home @ Away" which was the source of the H2H
    # record inversion the user reported 2026-07-25.
    if not (home and away):
        event = (pick.get("event") or "").strip()
        if event:
            parts = re.split(r"\s+(?:vs\.?|v\.?|@)\s+", event, maxsplit=1,
                             flags=re.IGNORECASE)
            if len(parts) == 2:
                # "@" separator → AWAY @ HOME. "vs" separator has no
                # canonical direction (Tennis/UFC use "vs"); pick[0]
                # remains the visually-first side.
                if "@" in event:
                    away_parsed, home_parsed = parts[0].strip(), parts[1].strip()
                else:
                    away_parsed, home_parsed = parts[0].strip(), parts[1].strip()
                if not home:
                    home = home_parsed
                if not away:
                    away = away_parsed

    # ── Derive the pick's chosen team, so the H2H record can be shown
    # from that team's perspective instead of always defaulting to
    # home (fixes the "0-3 should be 3-0" user report).
    pick_team = _infer_pick_team(pick, home, away)

    sources: list[str] = []

    # Team-level H2H — works for every sport that has final scores logged.
    team_h2h = await _team_h2h_from_settled(
        db, sport, home, away, limit=10,
        pick_team=pick_team,
        canonical_home_id=pick.get("canonical_team_id"),
        canonical_away_id=pick.get("canonical_opponent_id"),
    )
    if team_h2h:
        # Use the specific collection label so the audit trail (`sources`)
        # accurately reflects where the H2H data actually came from.
        sources.append(team_h2h.get("source") or "settled_picks_diagnostic")

    # Player-level H2H — sport-specific.
    player_h2h: Optional[dict] = None
    try:
        if sport == "MLB":
            # MLB pitcher + batter H2H both hit the external MLB Stats
            # API but response times are ~200ms and the module maintains
            # a 12h in-process cache, so it's cheap enough to run in
            # fast_mode too. That gives us the batter's "X-for-Y vs OPP"
            # chip on /picks/today, not just on the deep-dive screen
            # (user report: "Make sure L3 represents At bats should also
            # h2h at bat against team").
            player_h2h = await _mlb_player_h2h(pick)
            if player_h2h:
                sources.append("mlb_stats_api")
        elif sport == "Tennis":
            # Tennis H2H hits our own Mongo collection — cheap; keep it on.
            player_h2h = await _tennis_player_h2h(db, pick)
            if player_h2h:
                sources.append("tennis_matches_history")
        elif sport == "Soccer":
            # 2026-08-23: authoritative canonical player game logs
            # (soccer_player_game_logs + mls_player_matchup_history).
            player_h2h = await _soccer_player_h2h(db, pick)
            if player_h2h:
                sources.append(player_h2h.get("source") or "soccer_player_game_logs")
        elif sport == "NBA":
            # 2026-08-23 REMAINING_H2H_COVERAGE — canonical player_game_logs
            # by name + espn_team_meta opponent id.
            player_h2h = await _nba_player_h2h(db, pick)
            if player_h2h:
                sources.append(player_h2h.get("source") or "player_game_logs")
        elif sport == "NHL":
            # 2026-08-23 REMAINING_H2H_COVERAGE — attempts canonical join.
            # Returns None when opponent identity cannot be resolved from
            # existing storage (honest unavailable).
            player_h2h = await _nhl_player_h2h(db, pick)
            if player_h2h:
                sources.append(player_h2h.get("source") or "player_game_logs")
        # NFL — team-level from settled DB is enough for MVP;
        # player-vs-opp splits deferred to a follow-up when we have the data.
    except Exception as e:
        logger.debug("player H2H failed for sport=%s: %s", sport, e)

    situational = _situational(pick)

    bundle = {
        "ok": bool(team_h2h or player_h2h),
        "sport": sport,
        # SOCCER_REGRESSION_RUNTIME §7 — truthful reason codes.
        # 2026-08-23 AUTHORITATIVE_H2H_TRUTH — extended:
        #   H2H_AUTHORITATIVE          — real game/match history
        #   H2H_APP_HISTORY_ONLY       — only settled-picks diagnostic
        #   H2H_INSUFFICIENT_SAMPLE    — authoritative but <3 events
        #   H2H_SOURCE_UNAVAILABLE     — no source returned any row
        "status": (
            "H2H_AUTHORITATIVE" if (team_h2h and team_h2h.get("authoritative")) else
            "H2H_APP_HISTORY_ONLY" if (team_h2h and team_h2h.get("app_history_only")) else
            ("H2H_AVAILABLE" if team_h2h else "H2H_SOURCE_UNAVAILABLE")
        ),
        "summary": _build_summary(sport, team_h2h, player_h2h,
                                   pick_market=pick.get("market") or ""),
        "team_h2h": team_h2h,
        "player_h2h": player_h2h,
        "situational": situational,
        "sources": sources,
        # is_player_prop tells the frontend whether the team's `avg_total`
        # cell should be shown in the deep-dive team card — same rationale
        # as the compact chip: avg game score is irrelevant on a player prop.
        "is_player_prop": _is_player_prop_market(pick.get("market") or ""),
    }
    _cache_put(ck, bundle)
    return bundle


__all__ = ["build_h2h_bundle"]
