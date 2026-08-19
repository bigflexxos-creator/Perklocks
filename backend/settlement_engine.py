"""Settlement engine — pulls final scores from The Odds API and marks picks
as Won/Lost/Push for moneyline, spread, totals, and win-or-draw markets.

Player props (e.g. "Buxton Over 0.5 Hits", "Anytime Goal Scorer") are NOT
auto-settled here because The Odds API doesn't expose individual player stats.
Those remain as `pending` until a future stats integration or manual mark.
"""
import logging
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from sports_engine import ODDS_KEY, BASE, SPORT_KEYS

logger = logging.getLogger("lockscore.settlement")


SETTLEABLE_KEYWORDS = (
    "moneyline", "spread", "total goals", "total runs", "total points",
    "total games", "total ", "win or draw",
)
# Player-prop keywords. These are kept narrow so they don't false-positive
# on GAME totals like "Total Points Over 171.5" (WNBA/NBA) — the substring
# "points" in such markets used to flag the entire game total as a player
# prop and block settlement. Player props are now identified primarily via
# the `· Props` league suffix (set in sports_engine when building props)
# and only secondarily via these very specific market labels.
PROP_KEYWORDS = (
    "anytime goal scorer", "first goal scorer", "to score or assist",
)


def is_player_prop(pick: dict) -> bool:
    market = (pick.get("market") or "").lower()
    league = (pick.get("league") or "").lower()
    if "props" in league:
        return True
    return any(k in market for k in PROP_KEYWORDS)


def parse_event_teams(event_str: str) -> tuple[Optional[str], Optional[str]]:
    """'Away @ Home' → ('Away', 'Home')."""
    if not event_str or "@" not in event_str:
        return (None, None)
    parts = event_str.split("@", 1)
    return (parts[0].strip(), parts[1].strip())


def _score_for(scores: list[dict], team: str) -> Optional[float]:
    if not team:
        return None
    target = team.strip().lower()
    for s in scores:
        name = (s.get("name") or "").strip().lower()
        if name == target:
            try:
                return float(s.get("score", 0))
            except Exception:
                return None
    return None


def _parse_spread(market: str) -> tuple[Optional[str], Optional[float]]:
    """Extract the team and line from a market string like 'Team +1.5 Spread',
    'Team -1.5 Spread (Alt)', 'Team +1.5 Run Line', 'Team -1.5 Puck Line' etc.
    Handles the ' (Alt)' suffix and Run Line / Puck Line / Handicap variants
    that some sports (MLB / NHL) emit instead of the literal 'Spread'."""
    # Strip trailing " (Alt)" first — settles identically to the main line.
    m_str = re.sub(r"\s*\(Alt\)\s*$", "", market, flags=re.IGNORECASE)
    m = re.match(
        r"^(.+?)\s+([+-]?\d+(?:\.\d+)?)\s+"
        r"(?:Spread|Run\s+Line|Puck\s+Line|Handicap)\s*$",
        m_str, re.IGNORECASE,
    )
    if not m:
        return (None, None)
    return (m.group(1).strip(), float(m.group(2)))


def _parse_total_line(market: str) -> Optional[float]:
    """Extract the numeric line from a total market string. Supports both
    'Over 8.5' and 'Under 8.5' variants (also 'Over 8.5 Runs (Alt)' etc.)."""
    m = re.search(r"(?:Over|Under)\s+(\d+(?:\.\d+)?)", market, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _parse_total_side(market: str) -> Optional[str]:
    """Return 'over' or 'under' based on market wording, else None."""
    if re.search(r"\bunder\b", market, re.IGNORECASE):
        return "under"
    if re.search(r"\bover\b", market, re.IGNORECASE):
        return "over"
    return None


def _parse_team_total(market: str) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """Extract (team, side, line) from strings like:
      • 'St. Louis Cardinals Team Total Over 3.5'
      • 'New York Yankees Team Total Under 4.5 (Alt)'

    Returns (None, None, None) when the market doesn't look like a Team Total.
    """
    # Drop trailing " (Alt)" — same settlement rule as main.
    m_str = re.sub(r"\s*\(Alt\)\s*$", "", market, flags=re.IGNORECASE)
    m = re.match(
        r"^(?P<team>.+?)\s+Team\s+Total\s+(?P<side>Over|Under)\s+"
        r"(?P<line>\d+(?:\.\d+)?)\s*$",
        m_str, re.IGNORECASE,
    )
    if not m:
        return (None, None, None)
    return (m.group("team").strip(), m.group("side").lower(), float(m.group("line")))


def settle_pick(pick: dict, score_payload: dict) -> Optional[str]:
    """Return 'won' / 'lost' / 'push' / None (not yet settleable)."""
    if not score_payload.get("completed"):
        return None
    scores = score_payload.get("scores") or []
    if not scores:
        return None

    market = (pick.get("market") or "").lower()
    selection = pick.get("selection") or ""
    away, home = parse_event_teams(pick.get("event") or "")
    away_score = _score_for(scores, away)
    home_score = _score_for(scores, home)
    if away_score is None or home_score is None:
        return None
    total = away_score + home_score

    # Moneyline (and Win or Draw)
    if "moneyline" in market:
        # Soccer / Tennis / UFC: 3-way (or no-draw) markets where the pick
        # is on a SPECIFIC team to WIN — a draw means the team failed to
        # win, which is a LOSS, not a push.
        # NBA / NFL / MLB / NHL / KBO / WNBA: 2-way moneylines that can never
        # end in a regulation tie (extras decide), so equal scores in those
        # leagues genuinely shouldn't happen — treat as push defensively.
        sport = (pick.get("sport") or "").lower()
        is_3way = sport in ("soccer", "tennis", "ufc", "mma")
        if away_score == home_score:
            return "lost" if is_3way else "push"
        winner = away if away_score > home_score else home
        return "won" if winner == selection else "lost"

    if "win or draw" in market:
        # Selection wins if their team didn't lose.
        if not selection:
            return None
        if selection == away:
            return "won" if away_score >= home_score else "lost"
        if selection == home:
            return "won" if home_score >= away_score else "lost"
        return None

    # Spread (main + Alt run-line / puck-line)
    if (
        "spread" in market
        or "run line" in market
        or "puck line" in market
        or "handicap" in market
    ):
        team, line = _parse_spread(pick.get("market") or "")
        if not team or line is None:
            return None
        team_score = _score_for(scores, team)
        opp_score = _score_for(scores, home if team == away else away)
        if team_score is None or opp_score is None:
            return None
        margin = team_score - opp_score + line
        if abs(margin) < 0.01:
            return "push"
        return "won" if margin > 0 else "lost"

    # ── Team Totals (MUST come before generic Totals) ─────────────────
    # e.g. "St. Louis Cardinals Team Total Over 3.5",
    #      "Yankees Team Total Under 4.5 (Alt)"
    # The prior settler treated these as game totals and always compared
    # the ~7-run game aggregate to a ~3-run team line — inflating win
    # rates on Team Total Over picks to ~95%+ (a statistical impossibility).
    # Fix: extract the team, use ONLY that team's score.
    if "team total" in market:
        team, side, line = _parse_team_total(pick.get("market") or "")
        if not team or side is None or line is None:
            return None
        team_score = _score_for(scores, team)
        if team_score is None:
            return None
        if abs(team_score - line) < 0.01:
            return "push"
        if side == "over":
            return "won" if team_score > line else "lost"
        # under
        return "won" if team_score < line else "lost"

    # Game Totals (Over / Under, incl. Alt)
    if ("total" in market or " runs" in market or " goals" in market
            or " points" in market or " games" in market) and (
                "over" in market or "under" in market):
        line = _parse_total_line(pick.get("market") or "")
        side = _parse_total_side(pick.get("market") or "")
        if line is None or side is None:
            return None
        if abs(total - line) < 0.01:
            return "push"
        if side == "over":
            return "won" if total > line else "lost"
        return "won" if total < line else "lost"

    return None  # unrecognized market — leave pending


async def _fetch_scores(sport_key: str) -> list[dict]:
    """Fetch completed-game scores for a sport over the last 3 days."""
    if not ODDS_KEY:
        return []
    url = f"{BASE}/sports/{sport_key}/scores"
    params = {"apiKey": ODDS_KEY, "daysFrom": 3}
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(url, params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("scores fetch failed for %s: %s", sport_key, e)
        return []


def _match_score_for_pick(pick: dict, all_scores: list[dict]) -> Optional[dict]:
    """Find the score payload that corresponds to this pick.

    Three-layer match — most specific first to handle doubleheaders and
    suspended/resumed games correctly:

      1. Odds API event-ID exact match (we stored this as fanduel_event_id
         via event_matcher at pick generation time). Bulletproof for
         doubleheaders since each game has a unique event ID.
      2. Teams + commence_time within ±3 hours (fallback when no event_id).
      3. Teams only — last resort, may match wrong game in a doubleheader.
    """
    event_id = pick.get("fanduel_event_id") or pick.get("event_id")
    if event_id:
        for s in all_scores:
            if s.get("id") == event_id:
                return s
    away, home = parse_event_teams(pick.get("event") or "")
    if not away or not home:
        return None
    al, hl = away.lower(), home.lower()

    # Layer 2: team + commence_time (handles doubleheaders)
    pick_time = pick.get("event_time") or ""
    pick_dt = None
    try:
        # Permissive ISO parser — handles both `Z` and `+00:00` suffixes
        # (tennis-extra emits the latter).
        iso = pick_time[:-1] + "+00:00" if pick_time.endswith("Z") else pick_time
        pick_dt = datetime.fromisoformat(iso)
        if pick_dt.tzinfo is None:
            pick_dt = pick_dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    candidates = [
        s for s in all_scores
        if (s.get("home_team") or "").lower() == hl
        and (s.get("away_team") or "").lower() == al
    ]
    if pick_dt and candidates:
        # Pick the score whose commence_time is closest to the pick's event_time.
        def _delta(s):
            try:
                ct = s.get("commence_time", "") or ""
                iso2 = ct[:-1] + "+00:00" if ct.endswith("Z") else ct
                st = datetime.fromisoformat(iso2)
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                return abs((st - pick_dt).total_seconds())
            except Exception:
                return 10**9
        candidates.sort(key=_delta)
        # Reject if even the closest is > 3 hours off — likely a different day's
        # game in the same matchup; safer to wait than mis-grade.
        if _delta(candidates[0]) <= 3 * 3600:
            return candidates[0]
        return None
    # Layer 3: team-only fallback
    return candidates[0] if candidates else None


async def settle_due_picks(db, sport_filter: Optional[list[str]] = None) -> dict:
    """Find all pending picks whose game has completed, mark each as won/lost/push.

    Returns counts: {settled, won, lost, push, skipped, auto_voided}.

    Args:
      sport_filter: if set, only process picks for these sports. Used by the
        MLB-only 5-min loop to avoid burning Odds API credits on
        Soccer/Tennis/UFC/NBA on every tick (those use a 15-min cadence).
    """
    # μ-closure LIVE (2026-06) — Settlement Reachability Fix 3B:
    # Settlement eligibility is derived from HISTORICAL publication
    # truth, NOT current Board visibility.  A pick that WAS canonically
    # published to the user is still owed a grade even if it later
    # became off_board (superseded, ladder-hidden, or removed by
    # current display rules).  Removing the "off_board != True"
    # exclusion here + gating on canonical publication ensures no
    # legitimate wager stays PENDING forever after being pulled off
    # the current board.
    from services.canonical_board_source import (
        canonical_publication_filter,
    )
    _canon_filt = canonical_publication_filter()
    query: dict = {"status": {"$in": [None, "pending"]},
                   # canonical PUBLISHED (or legacy bridge — see
                   # canonical_publication_filter) — historical
                   # publication is what qualifies for grading.
                   **_canon_filt,
                   # ── Phase A micro-closure (2026-06): actionable-only
                   # candidate filter.  Picks marked with
                   # ``settlement_block: True`` have no authoritative
                   # grading path and are excluded from the queue so
                   # they cannot occupy head batches → guarantees
                   # forward progress across bounded runs.
                   "settlement_block": {"$ne": True},
                   # ── 1C μ-closure — RETRY_AFTER FORWARD PROGRESS ──
                   # Rows that already failed authoritative settlement
                   # carry ``next_settlement_attempt_at`` set in the
                   # future.  Skip them until that timestamp arrives so
                   # they cannot repeatedly occupy the head cohort and
                   # block newer due picks.  Rows without the field
                   # (never attempted) match via $exists=False branch.
                   "$or": [
                       {"next_settlement_attempt_at": {"$exists": False}},
                       {"next_settlement_attempt_at": {
                           "$lte": datetime.now(timezone.utc).isoformat()
                       }},
                   ]}
    if sport_filter:
        query["sport"] = {"$in": list(sport_filter)}
    # ── Phase A (2026-06): Starvation Fix ─────────────────────────
    # The unsorted find() used to return picks in natural insertion
    # order — meaning when >2000 pending picks accumulated, the
    # OLDEST unresolved picks NEVER surfaced (they were pushed off
    # the batch by newer inserts).  History showed 0 graded picks in
    # production because the queue-head kept churning on skippable
    # rows (props_pending / in-progress) while resolvable older picks
    # starved.
    #
    # Fix: sort ascending by ``event_time`` so the oldest completed
    # events get the first attempt every run.  Combined with the
    # bounded 2000-doc limit we now guarantee forward progress:
    # unresolved OLD picks are always in the head cohort until they
    # either settle, void, or get marked SETTLEMENT_UNSUPPORTED.
    cursor = db.picks.find(query, {"_id": 0}).sort("event_time", 1)
    picks = await cursor.to_list(length=2000)
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0,
              "skipped": 0, "props_pending": 0, "auto_voided": 0,
              "unsupported_terminated": 0, "candidates_examined": 0,
              "attempts": 0, "success": 0, "fail": 0,
              "terminal_reasons": {}}
    counts["candidates_examined"] = len(picks)
    if not picks:
        # Even on an empty queue we record a telemetry beat so the
        # admin dashboard can prove the settler ran and found nothing
        # (vs. failed silently — a completely different failure mode).
        try:
            from services.settlement_telemetry import (
                record_run, oldest_unresolved_age_seconds,
            )
            counts["oldest_unresolved_age_seconds"] = (
                await oldest_unresolved_age_seconds(db)
            )
            await record_run(db, {**counts,
                                    "sport_filter": sport_filter})
        except Exception as _te:
            logger.debug("telemetry empty-run skipped: %s", _te)
        return counts

    # ── Auto-void stale picks (>14 days past event_time) ──────────────
    # Score APIs only expose recent games (3-14 day windows depending on
    # sport), so beyond 14 days they truly can't be settled. User spec
    # 2026-06-22: "I want bets to settle win loss so we can learn from
    # picks" — auto-void is LAST RESORT after settlement has had 14 days
    # of attempts. Bumped from 5d to 14d to maximize the learning signal.
    now_utc_for_void = datetime.now(timezone.utc)
    void_cutoff_iso = (now_utc_for_void - timedelta(days=14)).isoformat()
    stale_ids: list[str] = []
    for p in picks:
        et = p.get("event_time") or ""
        if not et:
            continue
        try:
            iso = et[:-1] + "+00:00" if et.endswith("Z") else et
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now_utc_for_void - dt).total_seconds() > 14 * 86400:
                stale_ids.append(p.get("id"))
        except Exception:
            pass
    if stale_ids:
        # P0.2b — route auto-void through SettlementService instead of
        # a bulk db.picks.update_many.  VOID is not an outcome result
        # so the FINAL barrier is bypassed; identity/versioning still
        # applies.  This keeps the compatibility mirror as the SOLE
        # write path for pick status.
        try:
            from services.settlement_service import SettlementService
            _svc_void = SettlementService(db)
            await _svc_void.ensure_indices()
            voided_n = 0
            for _sid in stale_ids:
                try:
                    _stale_pick = next(
                        (pp for pp in picks if pp.get("id") == _sid), {})
                    _res = await _svc_void.settle_from_pick(
                        _stale_pick or {"id": _sid},
                        result="void",
                        source="settlement_engine:auto_void_stale_14d",
                        authoritative_event_final=False,
                        analytics_mirror={
                            "void_reason": "auto_void_stale_14d",
                        },
                    )
                    if _res.get("status") in ("NEW_SETTLEMENT",
                                              "CORRECTION_APPLIED"):
                        voided_n += 1
                except Exception as _ve:
                    logger.debug("auto-void via svc failed for %s: %s",
                                 _sid, _ve)
            counts["auto_voided"] = voided_n
        except Exception as _sve:
            logger.warning("SettlementService auto-void err: %s", _sve)
        # Remove voided picks from in-memory processing list
        voided_set = set(stale_ids)
        picks = [p for p in picks if p.get("id") not in voided_set]

    # ── Phase A (2026-06) — SETTLEMENT_UNSUPPORTED terminator ─────
    # (2026-06 micro-closure) Some published markets have NO
    # authoritative grading path in the current settler set (Soccer
    # Shots, Cards, Corners, First Goalscorer, HT/FT, Correct
    # Score, Asian Handicap).  They cannot be graded and MUST NOT
    # be fabricated as VOID — lack of Perklocks settlement
    # capability is NOT an authoritative sportsbook VOID.
    #
    # Instead we stamp a BLOCKED disposition into pick METADATA
    # (leaving canonical ``status='pending'`` untouched, so
    # SettlementService remains the sole outcome writer and History
    # W/L/PUSH/VOID hit-rate/ROI is not contaminated), and add
    # ``settlement_block: True`` — future settlement passes exclude
    # these picks via the candidate-query gate, giving true forward
    # progress.
    #
    # Frozen publication truth is preserved (no status/result/off_board
    # mutation on the published record).  Analytics can still audit
    # the terminal reasons by grouping on ``settlement_block_reason``.
    try:
        from services.settlement_capability import classify, UNSUPPORTED
        _now_iso = datetime.now(timezone.utc).isoformat()
        _kept: list[dict] = []
        _terminated_reasons: dict[str, int] = {}
        _block_ids: list[str] = []
        _block_reasons_by_id: dict[str, str] = {}
        for p in picks:
            _et = p.get("event_time") or ""
            _event_past = bool(_et and _et < _now_iso)
            if not _event_past:
                _kept.append(p)
                continue
            status_c, reason = classify(
                p.get("sport"), p.get("market"), p.get("league"))
            if status_c != UNSUPPORTED:
                _kept.append(p)
                continue
            _pid = p.get("id")
            if not _pid:
                _kept.append(p)
                continue
            _block_ids.append(_pid)
            _block_reasons_by_id[_pid] = reason or "settler_unsupported:generic_unknown"
            _terminated_reasons[reason] = (
                _terminated_reasons.get(reason, 0) + 1)
        if _block_ids:
            # Bulk metadata write — NOT a status write.  Canonical
            # status stays ``pending`` so no outcome is fabricated.
            try:
                from pymongo import UpdateOne
                ops = [
                    UpdateOne(
                        {"id": _pid, "status": {"$in": [None, "pending"]}},
                        {"$set": {
                            "settlement_block":        True,
                            "settlement_block_reason": _block_reasons_by_id[_pid],
                            "settlement_block_at":     _now_iso,
                        }},
                    ) for _pid in _block_ids
                ]
                if ops:
                    res = await db.picks.bulk_write(ops, ordered=False)
                    counts["unsupported_terminated"] = (
                        res.modified_count or len(_block_ids))
            except Exception as _bwe:
                logger.warning("unsupported block metadata write err: %s", _bwe)
                counts["unsupported_terminated"] = 0
        picks = _kept
        if _terminated_reasons:
            counts["terminal_reasons"].update(_terminated_reasons)
    except Exception as _uce:
        logger.warning("settlement_capability terminator err: %s", _uce)

    # Group by sport so we batch score fetches.
    by_sport: dict[str, list[dict]] = {}
    for p in picks:
        sp = p.get("sport")
        if not sp:
            continue
        by_sport.setdefault(sp, []).append(p)

    # Fetch scores per sport_key. For MLB we now try the free MLB Stats API
    # FIRST (zero Odds-credit cost, faster, official data); only fall back
    # to The Odds API if MLB Stats API returns nothing.
    scores_cache: dict[str, list[dict]] = {}
    for sport, sport_picks in by_sport.items():
        all_scores: list[dict] = []
        # MLB fast-path: free official scores
        if sport == "MLB":
            try:
                from mlb_live import fetch_mlb_scores
                # MLB pulled from MLB Stats API (free, 0 Odds credits).
                # 14-day window: lets us settle stuck picks up to 2 weeks
                # old so the learning engine gets real W/L signal instead
                # of silent auto-voids. Per user 2026-06-22: "I want bets
                # to settle win loss so we can learn from picks."
                mlb_data = await fetch_mlb_scores(days_back=14)
                if mlb_data:
                    all_scores.extend(mlb_data)
                    logger.info(
                        "MLB settlement source: MLB Stats API (free) — %d games, 0 Odds credits used",
                        len(mlb_data),
                    )
            except Exception as e:
                logger.warning("MLB Stats API path failed, falling back to Odds API: %s", e)
        # Fallback / non-MLB sports: original Odds API path
        if not all_scores:
            keys = SPORT_KEYS.get(sport, [])
            # ── Phase 2δ closeout: scope down to sport_keys that
            # actually have unsettled published picks so we don't
            # burn scores credits on empty leagues.
            try:
                from services.settlement_scope import active_sport_keys as _ask
                active = set(await _ask(db))
                if active:
                    scoped = [k for k in keys if k in active]
                    if scoped:
                        keys = scoped
                    else:
                        # No active keys for this sport → no picks to
                        # score.  Skip the fan-out entirely.
                        logger.debug(
                            "settlement: skipping %s — no active sport_keys",
                            sport,
                        )
                        keys = []
            except Exception as _sc_err:
                logger.debug("settlement_scope filter err: %s", _sc_err)
            for key in keys:
                data = await _fetch_scores(key)
                if data:
                    all_scores.extend(data)
                await asyncio.sleep(0.6)  # throttle to avoid 429
        scores_cache[sport] = all_scores

    # ── Settlement-eligibility window ──────────────────────────────
    # Used to live at a blanket `3-hours-after-first-pitch` cutoff which
    # delayed grading by 45-75 min after games actually ended. Now keyed by
    # sport: how soon after first pitch is the game realistically over.
    # The `completed: true` flag on the score payload is the primary signal —
    # this min-elapsed gate just keeps us from racing against in-progress
    # games when the upstream feed is briefly inconsistent.
    MIN_ELAPSED_MIN: dict[str, int] = {
        "MLB":    30,   # 9-inning game ≈ 2.5h but many end well before that
        "NBA":    60,   # 48 min game ≈ 2h
        "NFL":    90,   # 60 min game ≈ 3h
        "Soccer": 100,  # 90 min + 10 min stoppage
        "Tennis": 45,   # best-of-3 ≈ 90 min
        "UFC":    20,   # most fights end well before the distance
        "KBO":    30,
    }
    now_utc = datetime.now(timezone.utc)
    for sport, sport_picks in by_sport.items():
        all_scores = scores_cache.get(sport, [])
        if not all_scores:
            continue
        min_elapsed = timedelta(minutes=MIN_ELAPSED_MIN.get(sport, 30))
        for pick in sport_picks:
            # Skip player props — can't settle without player stats.
            if is_player_prop(pick):
                counts["props_pending"] += 1
                continue
            # Game must have started long enough ago to plausibly have ended.
            et = pick.get("event_time") or ""
            try:
                # Permissive ISO parser handles both `Z` and `+00:00` suffix
                # (tennis-extra scraper emits `+00:00`). The strict strptime
                # used to fail for those → fell into the `except: pass`
                # branch → graded picks against in-progress scores OR (more
                # commonly) simply skipped them forever.
                iso = et[:-1] + "+00:00" if et.endswith("Z") else et
                dt = datetime.fromisoformat(iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now_utc - dt) < min_elapsed:
                    counts["skipped"] += 1
                    continue
            except Exception:
                pass
            score_payload = _match_score_for_pick(pick, all_scores)
            if not score_payload:
                counts["skipped"] += 1
                continue
            # CRITICAL: only grade against COMPLETED games. The Odds API
            # returns in-progress games in the scores endpoint too — without
            # this gate we'd grade against partial scores (the bug behind
            # the 6/17 Orioles mis-grade where the game wasn't even over yet).
            if not score_payload.get("completed"):
                counts["skipped"] += 1
                continue
            outcome = settle_pick(pick, score_payload)
            if not outcome:
                counts["skipped"] += 1
                continue
            counts["attempts"] += 1
            scores_dict = {s["name"]: s["score"] for s in (score_payload.get("scores") or [])}
            # Compute units_profit + CLV at settle time so the analytics
            # dashboard never has to recompute from raw odds.
            from analytics import (american_profit_per_unit, clv_units,
                                    confidence_bucket)
            from bet_type import classify_bet_type, unit_weight
            odds_used = pick.get("closing_odds") or pick.get("book_odds")
            # Per-bet-type weighting: heavy chalk (-300/-500+) gets reduced or
            # parlay-only stakes instead of flat $100 so ROI matches reality.
            bet_type = classify_bet_type(odds_used)
            w = unit_weight(odds_used)
            raw_profit = american_profit_per_unit(odds_used or 0, outcome)
            units_profit = round(raw_profit * w, 4)
            units_risked = w if outcome != "push" else 0.0
            clv = clv_units(pick.get("odds_at_pick"), pick.get("closing_odds") or pick.get("book_odds"))
            # ── P0.2b Canonical routing ───────────────────────────
            # Adapter's job (this file): resolve the authoritative
            # FINAL state + actual result.  SettlementService owns
            # the canonical write AND the compat mirror; no direct
            # `db.picks.update_one` for status here.
            _actual = {
                "final_score":  scores_dict,
                "units_risked": units_risked,
                "units_profit": units_profit,
                "clv_value":    clv,
            }
            _analytics = {
                "final_score":       scores_dict,
                "units_risked":      units_risked,
                "units_profit":      units_profit,
                "bet_type":          bet_type,
                "unit_weight":       w,
                "clv_value":         clv,
                "confidence_bucket": confidence_bucket(pick.get("lock_score")),
            }
            try:
                from services.settlement_service import SettlementService
                _settle_svc = SettlementService(db)
                await _settle_svc.ensure_indices()
                _svc_res = await _settle_svc.settle_from_pick(
                    pick,
                    result                    = outcome,
                    source                    = "settlement_engine",
                    actual_result             = _actual,
                    authoritative_event_final = True,   # score_payload["completed"] proved above
                    analytics_mirror          = _analytics,
                )
                _svc_status = (_svc_res or {}).get("status", "")
                if _svc_status in ("NEW_SETTLEMENT",
                                    "CORRECTION_APPLIED",
                                    "ALREADY_SETTLED_IDENTICAL"):
                    counts["success"] += 1
                else:
                    counts["fail"] += 1
                    _reason = f"svc_refusal:{_svc_status or 'unknown'}"
                    counts["terminal_reasons"][_reason] = (
                        counts["terminal_reasons"].get(_reason, 0) + 1)
                    # ── 1C μ-closure — RETRY_AFTER BACKOFF ─────────
                    # Stamp bounded exponential backoff so this row
                    # cannot repeatedly occupy the head cohort.
                    try:
                        _attempts = int(pick.get("settle_attempts") or 0) + 1
                        _delay_min = min(1440,
                            5 * (3 ** max(0, _attempts - 1)))  # 5→15→45→135→405→1215→1440
                        _next = (datetime.now(timezone.utc)
                                 + timedelta(minutes=_delay_min)).isoformat()
                        await db.picks.update_one(
                            {"id": pick.get("id")},
                            {"$set": {
                                "next_settlement_attempt_at": _next,
                                "settle_attempts": _attempts,
                                "last_settle_failure_reason": _reason,
                            }},
                        )
                    except Exception:
                        pass
            except Exception as _s_err:
                counts["fail"] += 1
                counts["terminal_reasons"]["svc_exception"] = (
                    counts["terminal_reasons"].get("svc_exception", 0) + 1)
                logger.warning(
                    "settlement_service.record failed for %s: %s",
                    pick.get("id"), _s_err)
                # Same retry_after backoff on exception paths.
                try:
                    _attempts = int(pick.get("settle_attempts") or 0) + 1
                    _delay_min = min(1440,
                        5 * (3 ** max(0, _attempts - 1)))
                    _next = (datetime.now(timezone.utc)
                             + timedelta(minutes=_delay_min)).isoformat()
                    await db.picks.update_one(
                        {"id": pick.get("id")},
                        {"$set": {
                            "next_settlement_attempt_at": _next,
                            "settle_attempts": _attempts,
                            "last_settle_failure_reason": "svc_exception",
                        }},
                    )
                except Exception:
                    pass
            # ── Propagate to user_bets (2026-07-21) ─────────────────
            # If any user has tracked this pick via /user/bets/track,
            # their personal bet gets settled with the same outcome.
            # Straight bets settle immediately; parlays settle only
            # once ALL legs are done.
            try:
                from routes.user_bets_routes import propagate_pick_settlement
                await propagate_pick_settlement(pick["id"], outcome,
                                                book_odds=odds_used)
            except Exception as _upe:
                logger.debug("user_bets propagation skipped: %s", _upe)
            counts[outcome] += 1
            counts["settled"] += 1
    logger.info("Settlement complete: %s", counts)

    # Player props can't be graded from The Odds API scores; delegate to the
    # dedicated prop engine which pulls player stats from MLB Stats API + ESPN.
    try:
        from prop_settlement import settle_player_props
        prop_counts = await settle_player_props(db)
        counts["props_settled"] = prop_counts.get("settled", 0)
        counts["props_won"] = prop_counts.get("won", 0)
        counts["props_lost"] = prop_counts.get("lost", 0)
        counts["props_push"] = prop_counts.get("push", 0)
        counts["won"] += prop_counts.get("won", 0)
        counts["lost"] += prop_counts.get("lost", 0)
        counts["push"] += prop_counts.get("push", 0)
        counts["settled"] += prop_counts.get("settled", 0)
        if prop_counts.get("settled"):
            logger.info("Player-prop settlement: %s", prop_counts)
    except Exception as e:
        logger.warning("prop settlement failed: %s", e)

    # ── KBO settlement REMOVED (2026-07-04, per user request):
    # KBO market removed from product scope. Historical KBO settler code
    # kept in kbo_settlement.py for reference but no longer invoked. All
    # KBO picks were already blocked at generation (sports_engine.py L54)
    # and the analytics dashboard already excludes them by league regex.

    # ── Soccer picks-table settler (2026-07-04, per user: goalscorer in
    # analytics "never updated after we did the 3 top goalscorer").
    # The previous flow only settled soccer via parlay legs, so soccer
    # AGS / SoA picks in the main picks table stayed pending forever
    # and their outcomes never entered the Analytics dashboard.
    try:
        from soccer_espn_settle import settle_soccer_picks_via_espn
        soccer_counts = await settle_soccer_picks_via_espn(db)
        counts["soccer_settled"] = soccer_counts.get("settled", 0)
        counts["soccer_won"] = soccer_counts.get("won", 0)
        counts["soccer_lost"] = soccer_counts.get("lost", 0)
        counts["won"] += soccer_counts.get("won", 0)
        counts["lost"] += soccer_counts.get("lost", 0)
        counts["push"] += soccer_counts.get("push", 0)
        counts["settled"] += soccer_counts.get("settled", 0)
        if soccer_counts.get("settled"):
            logger.info("Soccer ESPN settler: %s", soccer_counts)
    except Exception as e:
        logger.warning("Soccer picks settle failed: %s", e)

    # ── ESPN fallback settler for Tennis / UFC / WNBA-NBA player props.
    # The Odds API is slow/lacking coverage for these — ESPN has free public
    # box-scores. Each handler only operates on its own sport so it's safe
    # to run alongside the primary settler.
    try:
        from espn_settlement import settle_via_espn
        espn = await settle_via_espn(db)
        for k in ("tennis", "ufc", "props"):
            sub = espn.get(k, {})
            if sub.get("settled"):
                counts[f"espn_{k}_settled"] = sub.get("settled", 0)
                counts[f"espn_{k}_won"] = sub.get("won", 0)
                counts[f"espn_{k}_lost"] = sub.get("lost", 0)
                counts["won"] += sub.get("won", 0)
                counts["lost"] += sub.get("lost", 0)
                counts["push"] += sub.get("push", 0)
                counts["settled"] += sub.get("settled", 0)
                logger.info("ESPN %s settled: %s", k, sub)
    except Exception as e:
        logger.warning("ESPN settlement failed: %s", e)

    # ── Recompute self-tuning weights from the freshly-updated outcomes.
    # Cheap (pure aggregation over `picks`), runs after every settlement.
    try:
        from learning_engine import recompute_learned_weights
        weights = await recompute_learned_weights(db)
        counts["learning_buckets"] = sum(1 for b in weights.get("buckets", []) if b.get("active"))
    except Exception as e:
        logger.warning("learning recompute failed: %s", e)

    # ── Phase 2 learning — Per-Player Rolling Form (last-10 hot/cold). ─
    # Updates the `player_form` collection so the next pick refresh can
    # nudge lock_scores by ±5 based on each player's recent track record
    # on our picks. Time-decayed (30-day half-life), shrinkage-stable.
    try:
        from player_form import recompute_player_form
        form_rows = await recompute_player_form(db)
        counts["player_form_rows"] = form_rows
        logger.info("Player Form recomputed: %d player-market rows", form_rows)
    except Exception as e:
        logger.warning("Player Form recompute failed: %s", e)

    # ── Phase 3 learning — Multi-Armed Bandit arm states refresh. ──────
    # Rebuilds each strategy arm's Beta(α, β) posterior from every settled
    # pick. Cheap aggregation. The next refresh will Thompson-sample these
    # to decide which arms to favor.
    try:
        from bandit import refresh_arm_states
        arm_states = await refresh_arm_states(db)
        counts["bandit_arms"] = len(arm_states)
        # Log top 3 arms by posterior mean for transparency
        ranked = sorted(arm_states.values(),
                        key=lambda s: s.get("posterior_mean", 0),
                        reverse=True)
        top_summary = ", ".join(
            f"{s['arm']}={s['posterior_mean']:.2f}(n={s['n']})" for s in ranked[:3]
        )
        logger.info("Bandit arm states refreshed: %d arms · top: %s",
                    len(arm_states), top_summary)
    except Exception as e:
        logger.warning("Bandit arm refresh failed: %s", e)

    # ── Self-healing math validator — silently corrects edge/implied/lock
    # drift, including any post-learning win-prob stacking. Pure DB-side
    # math, no external API calls, runs every settlement cycle.
    try:
        from pick_validator import validate_and_heal
        heal_counts = await validate_and_heal(db)
        counts["validator"] = heal_counts
    except Exception as e:
        logger.warning("validator failed: %s", e)

    # ── Rollover History tag stamping (2026-07-08) ─────────────────
    # Re-derives the V4 top-3 rollover slate for each date we JUST
    # graded and stamps `on_rollover_at` onto exactly those 3 picks.
    # This is why History → Rollover matches what was on the live
    # Rollover tab, not a threshold approximation.
    try:
        from rollover_history_tagger import stamp_rollover_history_tags
        tag_res = await stamp_rollover_history_tags(db)
        counts["rollover_tags"] = tag_res
        logger.info("Rollover history tags refreshed: %s", tag_res)
    except Exception as e:
        logger.warning("Rollover history tagger failed: %s", e)

    # ── Phase A telemetry (2026-06) ────────────────────────────────
    # Record a run-summary doc so the admin dashboard / boundary
    # trace can observe queue health WITHOUT introducing a new
    # observability framework.  Failure is swallowed inside
    # record_run itself; this MUST NEVER be able to fail the loop.
    try:
        from services.settlement_telemetry import (
            record_run, oldest_unresolved_age_seconds,
        )
        counts["oldest_unresolved_age_seconds"] = (
            await oldest_unresolved_age_seconds(db)
        )
        await record_run(db, {**counts, "sport_filter": sport_filter})
    except Exception as _te:
        logger.debug("telemetry final-run write skipped: %s", _te)

    return counts

