"""Tennis Extra settler — settles Mallorca/Eastbourne/Challenger picks
from TennisExplorer's results page.

The Tennis Extra picks (`source: "tennis_extra"`) have `auto_settle: False`
and pile up as `pending` forever without this. We scrape the daily
results page and match player names + tournament to mark won/lost.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("lockscore.tennis_extra.settle")

_BASE = "https://www.tennisexplorer.com/results/"
_TIMEOUT = 20.0


def _norm(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"\s*\(\d+\)\s*$", "", n)  # strip seed
    n = re.sub(r"[^a-z0-9\s\.]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


async def _fetch_results_html(date_str: str) -> Optional[str]:
    """date_str = YYYY-MM-DD"""
    y, m, d = date_str.split("-")
    url = f"{_BASE}?year={int(y)}&month={int(m):02d}&day={int(d):02d}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT,
                                       headers={"User-Agent": "Mozilla/5.0 PerksLocks/1.0"}) as cx:
            r = await cx.get(url, follow_redirects=True)
            if r.status_code != 200:
                return None
            return r.text
    except Exception as e:
        logger.warning("results fetch failed for %s: %s", date_str, e)
        return None


def _parse_winners(html: str) -> list[dict]:
    """Return [{tournament, winner_norm, loser_norm}, ...] for tour-grade events."""
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    # Only consider tournaments scraped extras might have come from.
    from .scraper import _is_tour_grade  # reuse exact same filter

    for tbl in soup.find_all("table", class_="result"):
        rows = tbl.find_all("tr")
        current = None
        is_tour = False
        i = 0
        while i < len(rows):
            r = rows[i]
            classes = r.get("class") or []
            if "head" in classes:
                cell = r.find("td")
                current = cell.get_text(" ", strip=True) if cell else None
                is_tour = _is_tour_grade(current or "")
                i += 1
                continue
            if not is_tour or current is None:
                i += 1
                continue
            if i + 1 >= len(rows):
                break
            r1, r2 = r, rows[i + 1]
            if "head" in (r2.get("class") or []):
                i += 1
                continue
            tds1 = [td.get_text(" ", strip=True) for td in r1.find_all("td")]
            tds2 = [td.get_text(" ", strip=True) for td in r2.find_all("td")]
            if len(tds1) < 4 or len(tds2) < 2:
                i += 1
                continue
            name1 = tds1[1].strip()
            name2 = tds2[0].strip()
            # Sets won are at index 2 of row1 and index 1 of row2.
            try:
                s1 = int(tds1[2])
                s2 = int(tds2[1])
            except (ValueError, IndexError):
                i += 2
                continue
            if s1 == s2 or s1 < 0 or s2 < 0:
                i += 2
                continue
            winner, loser = (name1, name2) if s1 > s2 else (name2, name1)
            out.append({
                "tournament": current,
                "winner_norm": _norm(winner),
                "loser_norm": _norm(loser),
            })
            i += 2
    return out


async def settle_tennis_extra(db, *, days_back: int = 3) -> dict:
    """Walk back `days_back` days and settle any pending `tennis_extra` picks.

    2026-07-27 FIX: Previously the query filtered `off_board != True` which
    excluded ~85% of the pending stack (users saw 224 stuck Tennis picks).
    Off-board picks still need settling because user_bets reference them.
    Also switched the date field from `pick_date` to `event_time.day` so
    picks written on July 24 for a match on July 25 settle correctly.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    won = lost = pushed = unmatched = 0
    skipped_future = 0      # ← new: count picks left alone because the match hasn't finished
    for delta in range(0, days_back + 1):
        date_str = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
        html = await _fetch_results_html(date_str)
        if not html:
            continue
        winners = _parse_winners(html)
        if not winners:
            continue
        # Index by tournament for fast lookup.
        idx: dict[str, list[dict]] = {}
        for w in winners:
            idx.setdefault(w["tournament"].lower(), []).append(w)
        # All pending tennis_extra picks whose EVENT_TIME (match day) falls on
        # this date_str — not their pick_date. Two picks written on Jul 24
        # for a match played Jul 25 must settle when we scrape Jul 25 results.
        cursor = db.picks.find({
            "source": "tennis_extra",
            "status": "pending",
            "$or": [
                {"pick_date": date_str},
                {"event_time": {"$regex": f"^{date_str}"}},
            ],
            # 2026-07-27: removed off_board filter (was silently skipping
            # ~85% of the pending queue and creating stuck-pending stack).
        })
        async for p in cursor:
            # ── Guard: don't settle picks whose match hasn't finished ─
            # The tennis_extra pipeline writes picks for TOMORROW with
            # today's pick_date (so they appear on today's slate). Prior
            # to this guard, those picks would get name-matched against
            # today's completed results — declaring tomorrow's matches
            # WON based on a different match today, by the same player.
            # Wait at least 2h after start (longest singles ~3h, but
            # most finish in 60-120 min).
            et_raw = p.get("event_time") or ""
            if et_raw:
                try:
                    et_clean = et_raw.replace("Z", "+00:00") if et_raw.endswith("Z") else et_raw
                    match_start = datetime.fromisoformat(et_clean)
                    if match_start.tzinfo is None:
                        match_start = match_start.replace(tzinfo=timezone.utc)
                    if match_start + timedelta(hours=2) > now:
                        skipped_future += 1
                        continue
                except Exception:
                    pass  # bad event_time — fall through to name match
            league = (p.get("league") or "").lower()
            results = idx.get(league)
            if not results:
                unmatched += 1
                continue
            sel_norm = _norm(p.get("selection") or "")
            match = None
            for r in results:
                if sel_norm in (r["winner_norm"], r["loser_norm"]):
                    match = r
                    break
            if not match:
                # Token-overlap fallback — handles "Draper J." (sel) vs
                # "Jack Draper" (Wimbledon result) by intersecting the
                # tokenised names. We require BOTH a token match AND
                # uniqueness — i.e. only one player on the day has that
                # last name — to avoid the catastrophic Diallo / Quinn
                # / Dimitrov tomorrow-vs-today collisions that flagged
                # this bug.
                sel_tokens = {t for t in sel_norm.split() if len(t) >= 3}
                if sel_tokens:
                    candidates = []
                    for r in results:
                        w_tokens = set(r["winner_norm"].split())
                        l_tokens = set(r["loser_norm"].split())
                        if sel_tokens & w_tokens or sel_tokens & l_tokens:
                            candidates.append(r)
                    if len(candidates) == 1:
                        match = candidates[0]
            if not match:
                unmatched += 1
                continue
            won_match = sel_norm in match["winner_norm"] or bool(
                {t for t in sel_norm.split() if len(t) >= 3}
                & set(match["winner_norm"].split())
            )
            status = "won" if won_match else "lost"
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {
                    "status": status,
                    "settled_at": datetime.now(timezone.utc).isoformat(),
                    "settle_source": "tennis_extra_settler",
                    "settle_winner": match["winner_norm"],
                }},
            )
            # ── Propagate to user_bets (2026-07-21) ─────────────────
            try:
                from routes.user_bets_routes import propagate_pick_settlement
                await propagate_pick_settlement(p["id"], status,
                                                book_odds=p.get("book_odds"))
            except Exception:
                pass
            if status == "won":
                won += 1
            else:
                lost += 1
    return {"won": won, "lost": lost, "pushed": pushed,
            "unmatched": unmatched, "skipped_future": skipped_future}


async def tennis_extra_settler_loop(db) -> None:
    """Long-running 30-min loop."""
    while True:
        try:
            await asyncio.sleep(30 * 60)
            summary = await settle_tennis_extra(db)
            if summary.get("won") or summary.get("lost"):
                logger.info("Tennis Extra settler: %s", summary)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Tennis Extra settler loop error: %s", e)
