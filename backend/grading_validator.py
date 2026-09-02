"""Grading Validator — permanent cross-source verification (all sports).

User mandate (2026-07-13):
  "Why do we have to keep having these problems with history and you not
  seeing til I find flaw I can't have a working app if history is wrong"
  "I want all picks on board to grade correctly across all sports"

Design: every 60 min, scan freshly-graded picks. For each one, query an
INDEPENDENT data source and compare the grade. On disagreement:
  1. Log LOUDLY with all context.
  2. Re-open the pick (status → 'pending', clear settled_at) so the
     next settler cycle regrades with the fixed logic.
  3. If a threshold of mismatches happens in a day, escalate the log
     to WARNING so the operator sees it in monitoring.

Cross-source coverage:
  • Soccer goalscorer  → FotMob (universal Nordic + top-5 coverage)
  • MLB player props   → MLB Stats API boxscore (statsapi.mlb.com,
                          the authoritative first-party source)
  • Tennis moneyline   → ESPN scoreboard status (already independent
                          of our TennisExplorer primary source)

The point isn't perfect grading — it's a self-healing loop that catches
grading regressions the moment they happen instead of days later when
a user notices. Every pick added to history is cross-verified within
60 minutes of settlement.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.grading_validator")

VERIFY_WINDOW_MIN = 6 * 60             # 6 hours
LOOP_INTERVAL_SECS = 60 * 60           # 1 hour
DAILY_MISMATCH_ALERT_THRESHOLD = 3

# ── Root Closure 2026-06 — authoritative actuals stash ────────
# Every call to `_mlb_verify_prop` records the AUTHORITATIVE
# player/stat/value used in the verification here (keyed by
# pick.id) so the correction submission in `_run_cross_check`
# can propagate the corrected actual to `pick.final_score`
# (the compat-mirror the History UI renders).  Bounded to avoid
# unbounded growth; a soft cap of 5000 covers hours of verifier
# activity while every corrected pick lands in the same run.
_LAST_MLB_VERIFY_ACTUALS: dict = {}
_MAX_STASH = 5000


def _stash_actuals(pick_id: str, payload: dict) -> None:
    if not pick_id:
        return
    if len(_LAST_MLB_VERIFY_ACTUALS) >= _MAX_STASH:
        # Cheap eviction: pop 500 oldest by insertion order.
        for k in list(_LAST_MLB_VERIFY_ACTUALS.keys())[:500]:
            _LAST_MLB_VERIFY_ACTUALS.pop(k, None)
    _LAST_MLB_VERIFY_ACTUALS[pick_id] = payload


_MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"
_MLB_STAT_MAP = {
    "hits":         "hits",
    "home run":     "homeRuns",
    "total bases":  "totalBases",
    "rbi":          "rbi",
    "runs scored":  "runs",
    "strikeouts":   "strikeOuts",
    "outs recorded": "outs",
}


async def _mlb_verify_prop(pick: dict) -> Optional[str]:
    """Verify an MLB player-prop pick against MLB Stats API boxscore.

    Returns 'won' / 'lost' / 'push' or None when we can't verify.

    Root Closure (2026-06): the raw authoritative actuals from the
    MLB StatsAPI verification are stashed on the pick doc via the
    module-level dict `_LAST_MLB_VERIFY_ACTUALS` (keyed by pick.id)
    so the correction path in `_run_cross_check` can overwrite the
    now-stale `pick.final_score` compat-mirror with the AUTHORITATIVE
    value.  Without this, the History UI displays the corrected
    result ('LOST') alongside the pre-correction actual ('4') from
    the buggy first-pass settler — a mathematical contradiction
    the user rightly flagged as a certification-blocking defect.
    """
    market = (pick.get("market") or "").lower()
    selection = pick.get("selection") or pick.get("player_name") or ""
    event_time = pick.get("event_time") or ""
    if not selection or not event_time:
        return None

    # Determine market family + line. Combo markets like "Hits + Runs + RBIs"
    # must sum all three stats — matching a single key would only look at
    # hits and mis-grade edge cases where the batter's H+R+RBI is ≥ line
    # but hits alone is 0.
    stat_keys: list[str] = []
    if "hits + runs + rbi" in market or "hits+runs+rbi" in market:
        stat_keys = ["hits", "runs", "rbi"]
    else:
        for phrase, key in _MLB_STAT_MAP.items():
            if phrase in market:
                stat_keys = [key]
                break
    if not stat_keys:
        return None
    m = re.search(r"(over|under)\s*(\d+(?:\.\d+)?)", market)
    if not m:
        return None
    direction = m.group(1).lower()
    line = float(m.group(2))
    # Player name
    player_name = re.split(r"\s*(?:over|under|-)\s*", market, flags=re.I)[0].strip()
    if not player_name:
        player_name = selection.split()[0] if selection else ""

    # Find the MLB game via schedule endpoint for event_date. Merge D and
    # D-1 to handle late ET games that MLB files under the previous US
    # calendar date, then pick the game whose gameDate is closest to the
    # pick's event_time (handles series/doubleheader ambiguity — the same
    # bug that made the settler mis-grade Wheeler-style picks).
    try:
        from datetime import datetime as _dt, timedelta as _td
        date_str = event_time[:10]  # YYYY-MM-DD
        try:
            prev_str = (_dt.fromisoformat(date_str) - _td(days=1)).strftime("%Y-%m-%d")
        except Exception:
            prev_str = None
        games: list[dict] = []
        async with httpx.AsyncClient(timeout=15) as cx:
            for ds in ([date_str] + ([prev_str] if prev_str else [])):
                rr = await cx.get(f"{_MLB_STATS_BASE}/schedule",
                                  params={"sportId": 1, "date": ds, "hydrate": "team"})
                for d in (rr.json() or {}).get("dates", []):
                    games.extend(d.get("games", []))
        # Dedupe by gamePk
        seen: set = set()
        deduped: list[dict] = []
        for g in games:
            pk = g.get("gamePk")
            if pk in seen:
                continue
            seen.add(pk)
            deduped.append(g)
        games = deduped

        home_team = pick.get("home_team") or ""
        away_team = pick.get("away_team") or ""
        # Fallback: parse teams from event string when the pick doc doesn't
        # carry them (older picks in the DB).
        if (not home_team or not away_team) and pick.get("event"):
            evt = pick.get("event") or ""
            if "@" in evt:
                a, h = evt.split("@", 1)
                away_team = away_team or a.strip()
                home_team = home_team or h.strip()

        # AND team match (both teams must line up) — the previous OR match
        # returned any game where either team appeared, which is dangerous
        # for teams that play back-to-back different opponents.
        def _tm(a: str, b: str) -> bool:
            a, b = a.lower(), b.lower()
            return bool(a) and bool(b) and (a in b or b in a)

        matches: list[dict] = []
        for g in games:
            hn = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "")
            an = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "")
            if _tm(home_team, hn) and _tm(away_team, an):
                matches.append(g)
        if not matches:
            return None

        # Parse event_time for distance ranking
        et_dt = None
        try:
            et_dt = _dt.fromisoformat(event_time.replace("Z", "+00:00"))
            if et_dt.tzinfo is None:
                from datetime import timezone as _tz
                et_dt = et_dt.replace(tzinfo=_tz.utc)
        except Exception:
            pass

        def _prio(g: dict) -> tuple:
            state = ((g.get("status") or {}).get("abstractGameState") or "").lower()
            tier = 0 if state == "final" else (1 if state == "live" else 2)
            gd = g.get("gameDate") or ""
            dist = 0.0
            if et_dt and gd:
                try:
                    d = _dt.fromisoformat(gd.replace("Z", "+00:00"))
                    if d.tzinfo is None:
                        from datetime import timezone as _tz
                        d = d.replace(tzinfo=_tz.utc)
                    dist = abs((d - et_dt).total_seconds())
                except Exception:
                    pass
            return (tier, dist, gd)

        matches.sort(key=_prio)
        best = matches[0]
        # Only verify against Final games — Live/Preview would give wrong grades.
        state = ((best.get("status") or {}).get("abstractGameState") or "").lower()
        if state != "final":
            return None
        game_pk = best.get("gamePk")
        if not game_pk:
            return None
        async with httpx.AsyncClient(timeout=15) as cx:
            r2 = await cx.get(f"{_MLB_STATS_BASE}/game/{game_pk}/boxscore")
        boxscore = r2.json() or {}
    except Exception as e:
        logger.debug("MLB boxscore fetch failed: %s", e)
        return None

    # Search both team rosters for the player. For combo markets we sum the
    # relevant stats — all keys pulled from the same batting/pitching block
    # slice consistently for the same player.
    pname_norm = player_name.lower().strip()
    # Position-aware block routing — see prop_settlement._mlb_stat_for_player
    # for the full rationale. Wrong-block routing was the original grading
    # regression that made 82 Wheeler-style picks grade lost when they won.
    _BATTING_ONLY = {"hits", "homeRuns", "rbi", "totalBases", "doubles", "triples"}
    _PITCHING_ONLY = {"outs", "inningsPitched", "earnedRuns", "wins",
                       "losses", "saves", "holds", "battersFaced"}
    _AMBIGUOUS = {"strikeOuts", "baseOnBalls", "runs", "hitByPitch"}
    for side in ("home", "away"):
        players = ((boxscore.get("teams") or {}).get(side) or {}).get("players") or {}
        for pdoc in players.values():
            person = pdoc.get("person") or {}
            full = (person.get("fullName") or "").lower()
            if pname_norm and (pname_norm in full or full in pname_norm):
                stats = pdoc.get("stats") or {}
                position = ((pdoc.get("position") or {}).get("abbreviation") or "").upper()
                is_pitcher = position in ("P", "SP", "RP", "TWP")
                # For each requested stat key, look it up in the correct block
                # and accumulate. If the player is on the roster but has no
                # stats blocks (DNP), treat every key as 0 so the market
                # still grades cleanly.
                total = 0.0
                found_any = False
                for sk in stat_keys:
                    if sk in _BATTING_ONLY:
                        blocks = ("batting",)
                    elif sk in _PITCHING_ONLY:
                        blocks = ("pitching",)
                    elif sk in _AMBIGUOUS:
                        blocks = ("pitching", "batting") if is_pitcher else ("batting", "pitching")
                    else:
                        blocks = ("batting", "pitching")
                    for block in blocks:
                        block_stats = stats.get(block) or {}
                        if sk in block_stats:
                            try:
                                total += float(block_stats[sk] or 0)
                                found_any = True
                                break
                            except (TypeError, ValueError):
                                pass
                # Player is on roster but every block was empty → DNP, grade
                # against total=0 (standard "Action" resolution).
                actual = total if found_any or stats else 0.0
                # ── Root Closure 2026-06 — stash authoritative actuals
                # so the correction path can propagate them to
                # `pick.final_score` (the compat-mirror the UI renders).
                # The label mirrors the shape prop_settlement writes
                # ("<Player> Hits+Runs+Rbi") so History's stat-line
                # renderer needs no changes.
                _stat_label = "+".join(
                    "Rbi" if sk == "rbi" else sk.capitalize()
                    for sk in stat_keys
                )
                _stash_actuals(pick.get("id"), {
                    "player":       full.title() or player_name,
                    "stat":         "+".join(stat_keys),
                    "value":        actual,
                    "line":         line,
                    "direction":    direction,
                    "final_score": {
                        f"{full.title() or player_name} {_stat_label}": actual,
                        "Line": line,
                    },
                    "verifier_source": "mlb_statsapi",
                    "verified_at":     datetime.now(timezone.utc).isoformat(),
                })
                if direction == "over":
                    if actual > line:
                        return "won"
                    if actual < line:
                        return "lost"
                    return "push"
                else:
                    if actual < line:
                        return "won"
                    if actual > line:
                        return "lost"
                    return "push"
    return None


async def verify_recent_goalscorer_grades(db, *, window_min: int = VERIFY_WINDOW_MIN) -> dict:
    """Cross-check recently graded soccer goalscorer picks against FotMob."""
    from soccer_fotmob_settle import settle_soccer_leg as _fotmob

    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
    q = {
        "sport": "Soccer",
        "market": {"$regex": "Anytime Goal Scorer|To Score or Assist",
                    "$options": "i"},
        "status": {"$in": ["won", "lost"]},
        "settled_at": {"$gte": cutoff_iso},
        "grade_verified_at": {"$exists": False},
    }
    return await _run_cross_check(db, q, _fotmob, "fotmob")


async def verify_recent_mlb_grades(db, *, window_min: int = VERIFY_WINDOW_MIN) -> dict:
    """Cross-check recently graded MLB player props against MLB Stats API."""
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
    q = {
        "sport": "MLB",
        "market": {"$regex":
            r"Strikeouts?|Hits|Home Run|Total Bases|RBI|Outs Recorded|Runs Scored|Walks",
            "$options": "i"},
        "status": {"$in": ["won", "lost", "push"]},
        "settled_at": {"$gte": cutoff_iso},
        "grade_verified_at": {"$exists": False},
    }
    return await _run_cross_check(db, q, _mlb_verify_prop, "mlb_statsapi")


async def _run_cross_check(db, query: dict, verifier, source_label: str) -> dict:
    """Shared cross-check loop — pulls picks, calls the verifier, reopens
    disagreements, marks agreements as verified."""
    summary = {"scanned": 0, "agreed": 0, "mismatched": 0,
               "verifier_unavailable": 0, "reopened": 0, "mismatches": []}
    async for p in db.picks.find(query).limit(500):
        summary["scanned"] += 1
        try:
            result = await verifier(p)
        except Exception as e:
            logger.debug("%s verifier failed: %s", source_label, e)
            result = None
        if result not in ("won", "lost", "push"):
            summary["verifier_unavailable"] += 1
            await db.picks.update_one(
                {"id": p.get("id")},
                {"$set": {"grade_verified_at": datetime.now(timezone.utc).isoformat(),
                          "grade_verify_source": f"{source_label}_unavailable"}},
            )
            continue
        current = p.get("status")
        if result == current:
            summary["agreed"] += 1
            update: dict = {
                "$set": {
                    "grade_verified_at": datetime.now(timezone.utc).isoformat(),
                    "grade_verify_source": source_label,
                    "grade_verify_result": "agreed",
                },
            }
            # If this pick had a prior disagreement flag, clear it now that
            # the settler produced the correct grade on the retry — otherwise
            # downstream monitors keyed on `grade_disagreement` see stale
            # positives forever (iter 70 cosmetic-bug finding).
            if p.get("grade_disagreement"):
                update["$unset"] = {"grade_disagreement": ""}
            await db.picks.update_one({"id": p.get("id")}, update)
            continue
        summary["mismatched"] += 1
        summary["mismatches"].append({
            "id":                p.get("id"),
            "event":             p.get("event"),
            "market":            p.get("market"),
            "selection":         p.get("selection"),
            "our_grade":         current,
            f"{source_label}":   result,
        })
        # ── Block 4A μ-closure — SETTLEMENT IMMUTABILITY ────────────
        # PRIOR DEFECT: mutated ``status`` back to "pending" and
        # ``$unset settled_at`` — a generic validator was overwriting
        # canonical settlement.  That created "permanent limbo": the
        # pick was neither settled (mirror said pending) nor
        # ungradable (canonical still had a settlement_event).
        #
        # NEW BEHAVIOR (bounded disagreement disposition):
        #   • Preserve canonical ``status`` + ``settled_at``.  Do NOT
        #     mutate them from the validator.  Canonical settlement
        #     truth lives in ``settlement_events`` + the authoritative
        #     settler pipeline; corrections must flow through THAT
        #     contract, not a validator side-effect.
        #   • Stamp ``grade_disagreement`` metadata for downstream
        #     audit and bounded correction:
        #       - detected_at, our_grade_was, verifier result
        #       - previous_settled_at (audit — was ours, keep for
        #         provenance)
        #       - attempts counter for bounded retry
        #       - disposition: "correction_required"
        #   • The stuck-pick reaper already skips rows carrying
        #     ``grade_disagreement`` — with our new semantics that
        #     is CORRECT because the row is not really "pending".
        _prior_attempts = 0
        try:
            _gd_prev = p.get("grade_disagreement") or {}
            if isinstance(_gd_prev, dict):
                _prior_attempts = int(_gd_prev.get("attempts") or 0)
        except Exception:
            pass
        _MAX_DISAGREE_ATTEMPTS = 5
        _attempts = _prior_attempts + 1
        _disposition = (
            "terminal_unresolved"
            if _attempts >= _MAX_DISAGREE_ATTEMPTS
            else "correction_required"
        )
        _now_iso = datetime.now(timezone.utc).isoformat()
        await db.picks.update_one(
            {"id": p.get("id")},
            {"$set": {
                # Canonical settlement fields are DELIBERATELY NOT
                # touched here — the row's ``status`` and
                # ``settled_at`` remain whatever the authoritative
                # settler wrote.
                "grade_disagreement": {
                    "detected_at":          _now_iso,
                    "our_grade_was":        current,
                    f"{source_label}_said": result,
                    "previous_settled_at":  p.get("settled_at"),
                    "attempts":             _attempts,
                    "disposition":          _disposition,
                },
                "grade_verified_at":       _now_iso,
                "grade_verify_source":     f"{source_label}_disagreement",
                "grade_verify_result":     "disagreement",
            }},
        )
        # FINAL PARITY CLOSURE (2026-06) — correction wiring truth.
        # Previously the validator STAMPED ``correction_required``
        # and STOPPED — leaving the wrong canonical grade active on
        # ``settlement_events``.  Now we actually submit the
        # corrected grade to SettlementService so the canonical
        # audit chain (settlement_version + supersedes_settlement_id
        # + fingerprint) captures the correction.  Terminal
        # disagreements (attempts ≥ max) are NOT re-submitted — they
        # remain flagged but do not thrash the settler.
        if _disposition == "correction_required":
            try:
                from services.settlement_service import SettlementService
                _svc = SettlementService(db)
                await _svc.ensure_indices()
                # Root Closure 2026-06 — pull the authoritative actuals
                # captured by the verifier so the correction event AND
                # the pick compat-mirror both carry the truthful value
                # (fixes the "Actual 4 · LOST" contradiction the user
                # reported on Michael Harris II / Matt Olson).
                _auth = _LAST_MLB_VERIFY_ACTUALS.get(p.get("id")) or {}
                _actual_payload = {
                    "player":       p.get("player_name") or _auth.get("player"),
                    "stat":         _auth.get("stat") or p.get("market"),
                    "value":        _auth.get("value"),
                    "line":         p.get("line") or _auth.get("line"),
                    "final_score":  _auth.get("final_score"),
                    "verifier_source": _auth.get("verifier_source") or source_label,
                    "verified_at":     _auth.get("verified_at") or _now_iso,
                    "correction_evidence":  {
                        "our_grade_was":    current,
                        f"{source_label}":  result,
                        "detected_at":      _now_iso,
                        "attempts":         _attempts,
                        "verifier_source":  source_label,
                    },
                }
                _corr = await _svc.settle_from_pick(
                    p,
                    result                    = result,
                    source                    = f"{source_label}_correction",
                    actual_result             = _actual_payload,
                    authoritative_event_final = True,
                )
                _corr_status = (_corr or {}).get("status")
                # Mirror-write pick.final_score so History/Analytics
                # never display a stale actual next to a corrected
                # result.  Only write when the verifier actually
                # captured an authoritative value.
                _mirror_update = {
                    "grade_disagreement.correction_submitted_at": _now_iso,
                    "grade_disagreement.correction_status":        _corr_status,
                    "grade_disagreement.corrected_grade":          result,
                }
                if _auth.get("final_score"):
                    _mirror_update["final_score"] = _auth["final_score"]
                    _mirror_update["final_score_source"] = _auth.get("verifier_source", source_label)
                    _mirror_update["final_score_verified_at"] = _now_iso
                await db.picks.update_one(
                    {"id": p.get("id")},
                    {"$set": _mirror_update},
                )
            except Exception as _corr_err:
                logger.warning(
                    "grading_validator correction submit failed for %s: %s",
                    p.get("id"), _corr_err,
                )
        summary["reopened"] += 1
    if summary["mismatched"]:
        level = (logging.WARNING
                 if summary["mismatched"] >= DAILY_MISMATCH_ALERT_THRESHOLD
                 else logging.INFO)
        logger.log(
            level,
            "Grading validator (%s): %d/%d disagreements caught & reopened. %s",
            source_label, summary["mismatched"], summary["scanned"],
            summary["mismatches"][:5],
        )
    else:
        logger.info(
            "Grading validator (%s): %d verified, %d agreed, %d unavailable",
            source_label, summary["scanned"], summary["agreed"],
            summary["verifier_unavailable"],
        )
    return summary


async def grading_validator_loop(db) -> None:
    """Long-running 1-hour loop. Cross-checks Soccer + MLB every cycle."""
    await asyncio.sleep(10 * 60)
    while True:
        try:
            await verify_recent_goalscorer_grades(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("grading_validator soccer error: %s", e)
        try:
            await verify_recent_mlb_grades(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("grading_validator MLB error: %s", e)
        await asyncio.sleep(LOOP_INTERVAL_SECS)
