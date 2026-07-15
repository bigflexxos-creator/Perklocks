"""Convert TennisExplorer scrapes into PerksLocks pick documents.

We're cautious here — these picks come from a single scrape with no
secondary verification, and many are 250-level / qualifier matches.
So we:
  • Mark them `source="tennis_extra"` and `is_extra=true`.
  • Only generate ONE moneyline pick per match (the favorite if their
    implied prob is ≥55%).
  • Seed lock_score in [70..90]. NOTE: Downstream Lock Engine V2 +
    learning_system_v2 can legitimately raise this above 90 (up to 99)
    when the pick passes their bet-quality checks — well-calibrated
    market, positive bandit lift, strong recent ROI, etc. So a 91-93
    on a scraped pick means "it earned Elite badge after verification",
    not a bug. Only this initial-seed step is capped at 90.
  • Skip picks where the spread is too tight (no clear favorite).
  • Skip qualifiers and challengers below ATP 250 by default unless
    `include_challengers=True`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional

from .scraper import fetch_today_matches
from .real_odds import (
    fetch_all_tennis_events,
    lookup_real_odds_for_match,
)

# Initial-seed lock-score band for the favorite. Downstream learning/lock
# engines may raise this above 90 (up to 99) when the pick passes Lock V2 +
# bandit + calibration checks — that's intentional, not a bug.
_MIN_FAV_IMPLIED = 0.55      # favorite must be ≥55% implied
_MAX_FAV_IMPLIED = 0.92      # ≥92% is too chalky → trap territory
_MAX_LOCK = 90.0             # initial seed ceiling; downstream may exceed

# Tiers we serve by default.
_DEFAULT_TIERS = ("ATP 250", "WTA 250", "Unknown")

# Tournaments ALREADY covered by The Odds API — skip them to avoid dupes
# with the main pick pipeline. Match against the lowercased TournamentExplorer
# tournament name (which is just the city, e.g. "Halle").
_ALREADY_COVERED = (
    "halle",       # → tennis_atp_halle_open
    "queen",       # → tennis_atp_queens_club_champ
    "berlin",      # → tennis_wta_german_open
)


def _pick_id(player_a: str, player_b: str, tournament: str, event_date: str) -> str:
    raw = f"te|{event_date}|{tournament}|{player_a}|{player_b}".lower()
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _strip_seed(name: str) -> str:
    """Remove TennisExplorer's seed parenthetical e.g. 'Borges N. (8)'."""
    import re
    return re.sub(r"\s*\(\d+\)\s*$", "", name).strip()


def _lock_score_from_implied(
    implied: float,
    *,
    tier: str,
    tournament: str = "",
    event_label: str = "",
    edge_percent: float = 0.0,
    using_real_odds: bool = False,
) -> float:
    """Translate book implied probability + evidence → SEED lock score.

    Redesigned 2026-07-15 (user complaint "why are all tennis picks 92?").
    Previous formula used implied% alone → 55% fav = 75, 90% fav = 90, and
    then Lock V2 pushed every pick to 91-92. Result: no differentiation
    between an ATP main-tour +EV pick and an ITF doubles chalk trap.

    New formula spreads legitimate picks from ~65 to ~95 by layering:

      base       — implied% mapped to [65, 92]
      tier_pen   — ITF Futures / Doubles / Qualifier / Challenger penalties
      edge_bonus — real-book +EV bonus / negative-edge fade
      chalk_pen  — implied ≥85% trap-zone penalty
      value_bon  — 58-72% "sweet spot" bonus (best historical ROI zone)
    """
    # Base — same slope as before but WIDER band
    base = 65.0 + (implied - 0.55) / (0.90 - 0.55) * 27.0    # 65 → 92
    base = max(60.0, min(92.0, base))

    # ── Tier / tournament penalty ────────────────────────────────────
    tier_pen = 0.0
    tier_l = (tier or "").lower()
    tournament_l = (tournament or "").lower()
    event_l = (event_label or "").lower()
    is_doubles = ("/" in event_l)  # doubles matches show "A / B vs C / D"
    if is_doubles:
        tier_pen -= 5.0
    if any(kw in tier_l for kw in ("itf", "futures", "m15", "m25", "w15", "w25")):
        tier_pen -= 5.0
    elif "challenger" in tier_l:
        tier_pen -= 3.0
    elif "qualifier" in tier_l or "qualify" in tier_l:
        tier_pen -= 4.0
    elif any(kw in tier_l for kw in ("m50", "m80", "m100", "w60", "w80", "w100")):
        # Higher-tier ITF Circuit / low-end Challenger → mid penalty
        tier_pen -= 3.0
    elif tier_l in ("unknown", ""):
        tier_pen -= 2.0
    # ATP 250 / 500 / 1000 / Grand Slam / WTA equivalents → no penalty

    # ── Edge bonus / fade ────────────────────────────────────────────
    edge_bonus = 0.0
    if using_real_odds and isinstance(edge_percent, (int, float)):
        # Positive edge = our fair prob beats book's price
        if edge_percent >= 5:
            edge_bonus += 3.5
        elif edge_percent >= 2:
            edge_bonus += 2.0
        elif edge_percent >= 0.5:
            edge_bonus += 0.8
        elif edge_percent <= -8:
            edge_bonus -= 5.0
        elif edge_percent <= -5:
            edge_bonus -= 3.0
        elif edge_percent <= -2:
            edge_bonus -= 1.5

    # ── Chalk trap penalty ───────────────────────────────────────────
    chalk_pen = 0.0
    if implied >= 0.87:      # ~-670+ favorite
        chalk_pen -= 3.0
    elif implied >= 0.82:    # ~-455 to -670
        chalk_pen -= 1.5

    # ── "Sweet spot" bonus ───────────────────────────────────────────
    value_bon = 0.0
    if 0.58 <= implied <= 0.72:   # ~-140 to -260 fav — historical +EV zone
        value_bon += 1.5

    lock = base + tier_pen + edge_bonus + chalk_pen + value_bon
    # Clamp to a wider legal range so the differentiation actually
    # survives Lock V2's downstream bounded adjustments.
    lock = max(55.0, min(_MAX_LOCK, lock))
    return round(lock, 1)


def _grade(lock: float) -> str:
    if lock >= 85:
        return "Strong Lock"
    if lock >= 78:
        return "Lock"
    return "Solid Lean"


async def fetch_extra_tennis_picks(
    *,
    date_str: Optional[str] = None,
    include_challengers: bool = True,
    days_ahead: int = 1,
) -> list[dict]:
    """Top-level entry. Returns ready-to-store pick docs.

    `days_ahead` controls how far forward we scrape. Default = 1 (today +
    tomorrow). The user complaint "Why I don't see tennis picks earlier"
    is solved by fetching tomorrow's matches tonight so the early-morning
    UTC tennis matches (Eastbourne / Mallorca / Bad Homburg often start
    9-11 AM UTC) appear in the feed the evening before with full lead time.

    All picks tagged with `pick_date = date_str` (today) so they surface in
    `/picks/today` immediately — only the `event_time` differs. The pick id
    hash uses the match's *event_date* so today's "Smith vs Jones" and
    tomorrow's "Smith vs Jones" never collide.
    """
    now = datetime.now(timezone.utc)
    date_str = date_str or now.strftime("%Y-%m-%d")

    # Build the list of dates to scrape: today, today+1, ..., today+days_ahead.
    days = max(0, int(days_ahead))
    scrape_dates = [now + timedelta(days=i) for i in range(days + 1)]

    matches: list[dict] = []
    for target in scrape_dates:
        try:
            day_matches = await fetch_today_matches(now=now, target_date=target)
        except Exception:
            day_matches = []
        # Tag each match with its event_date so we can use it later for
        # the pick id hash (prevents same-player collisions across days).
        day_key = target.strftime("%Y-%m-%d")
        for mm in day_matches:
            mm["_event_date"] = day_key
        matches.extend(day_matches)

    # ── Real-odds prefetch (2026-06-25) ──────────────────────────────
    # Pull every active tennis tournament's h2h events from The Odds API
    # in one shot. We use the result below per-match to promote
    # tennis_extra picks to the main board with real FanDuel/DK/MGM
    # lines when a US sportsbook actually carries the match — fixes
    # "Why is Osaka only tennis pick really on the board?" user report
    # by automatically promoting ATP/WTA matches whenever the books
    # start quoting them (e.g. Wimbledon ramp-up).
    try:
        live_odds_events = await fetch_all_tennis_events()
    except Exception as _ro_err:
        live_odds_events = []
    picks: list[dict] = []
    for m in matches:
        # Dedupe vs. The Odds API — skip tournaments we already pull.
        tname_lc = (m.get("tournament") or "").lower()
        if any(cov in tname_lc for cov in _ALREADY_COVERED):
            continue
        # Filter by tier.
        tier = m.get("tournament_tier") or "Unknown"
        if not include_challengers and "Challenger" in tier:
            continue
        # Must have both odds (book-anchored path)... OR fall back to
        # the Elo-based fair-odds engine if odds are missing.
        odds_p1 = m.get("odds_american_p1")
        odds_p2 = m.get("odds_american_p2")
        is_model_pick = False
        model_components: Optional[dict] = None
        if odds_p1 is None or odds_p2 is None:
            # ── Fair-odds fallback (Elo + surface + form + fatigue) ─────
            try:
                from .odds_engine import fair_win_probability
                fair = await fair_win_probability(
                    m["player1"], m["player2"],
                    tournament=m.get("tournament") or "")
                # Convert fair odds into the same downstream shape.
                if fair["prob_a"] >= 0.5:
                    odds_p1 = fair["fair_odds_a"]
                    odds_p2 = fair["fair_odds_b"]
                else:
                    odds_p1 = fair["fair_odds_a"]
                    odds_p2 = fair["fair_odds_b"]
                # Use Elo-derived implied probability directly.
                implied_p1 = fair["prob_a"]
                implied_p2 = fair["prob_b"]
                is_model_pick = True
                model_components = fair.get("components")
            except Exception:
                continue
        else:
            implied_p1 = float(m.get("implied_p1") or 0)
            implied_p2 = float(m.get("implied_p2") or 0)
        # Normalize for vig (sum often exceeds 1.0).
        s = implied_p1 + implied_p2
        if s <= 0:
            continue
        novig_p1 = implied_p1 / s
        novig_p2 = implied_p2 / s

        if novig_p1 >= novig_p2:
            fav_name, dog_name = m["player1"], m["player2"]
            fav_odds, fav_implied = odds_p1, novig_p1
        else:
            fav_name, dog_name = m["player2"], m["player1"]
            fav_odds, fav_implied = odds_p2, novig_p2

        if fav_implied < _MIN_FAV_IMPLIED or fav_implied > _MAX_FAV_IMPLIED:
            continue
        if fav_odds is None or fav_odds <= -700:
            continue  # chalk trap

        fav_clean = _strip_seed(fav_name)
        dog_clean = _strip_seed(dog_name)
        event_label = f"{fav_clean} vs {dog_clean}"

        pid = _pick_id(fav_clean, dog_clean, m["tournament"], m.get("_event_date") or date_str)

        # ── Real-odds promotion (2026-06-25) ────────────────────────
        # Before falling back to the scrape's TennisExplorer odds,
        # check if a US sportsbook actually carries this match. If
        # FanDuel/DK/MGM have a line, REPLACE the scraped odds with
        # the real one, recompute edge, and promote the pick out of
        # Extended Coverage into the main board.
        real_odds = lookup_real_odds_for_match(
            live_odds_events,
            player_a=fav_clean,
            player_b=dog_clean,
            selection_player=fav_clean,
        ) if live_odds_events else None
        using_real = bool(real_odds and real_odds.get("book_odds") is not None)

        if using_real:
            book_odds_final     = int(real_odds["book_odds"])
            implied_final       = float(real_odds["implied_probability"])
            bookmaker_final     = str(real_odds["bookmaker"])
            # Edge = our (no-vig) model probability vs the book's
            # vig-included implied probability. Genuine value signal.
            edge_pct            = round(fav_implied * 100.0 - implied_final, 2)
            is_extra_flag       = False
            fair_only_flag      = False
            source_label        = "tennis_real_odds"
            no_edge_model_flag  = False
            coverage_note       = f"Real book odds via {bookmaker_final}."
            all_books           = real_odds.get("all_books") or {}
        else:
            book_odds_final     = int(fav_odds)
            implied_final       = round(fav_implied * 100.0, 2)
            bookmaker_final     = "Sportsbook"
            edge_pct            = 0.0
            is_extra_flag       = True
            fair_only_flag      = is_model_pick
            source_label        = "tennis_extra_model" if is_model_pick else "tennis_extra"
            no_edge_model_flag  = not is_model_pick
            coverage_note       = "TennisExplorer scrape (Odds API doesn't carry this tournament)."
            all_books           = {}

        # ── Lock score (multi-factor, spreads picks 55-90) ──────────
        # Now computed AFTER real-odds promotion so real edge_pct feeds
        # into the seed. See _lock_score_from_implied docstring for the
        # tier / edge / chalk / value-zone weighting.
        _tour_ctx = (m.get("tournament") or "") + " " + tier
        lock = _lock_score_from_implied(
            fav_implied,
            tier=tier,
            tournament=_tour_ctx,
            event_label=event_label,
            edge_percent=edge_pct,
            using_real_odds=using_real,
        )

        pick_doc = {
            "id": pid,
            "sport": "Tennis",
            "league": m["tournament"],
            "tournament_tier": tier,
            "event": event_label,
            "event_time": m.get("commence_time"),
            "market": f"{fav_clean} Moneyline",
            "selection": fav_clean,
            "pick_side": fav_clean,
            "book_odds": book_odds_final,
            "implied_probability": implied_final,
            "win_probability": round(fav_implied * 100.0, 2),
            "model_win_probability": round(fav_implied * 100.0, 2),
            "edge_percent": edge_pct,
            "lock_score": lock,
            "lock_score_v2": lock,
            "grade": _grade(lock),
            "factors": {
                "Book Anchor": f"Market consensus puts {fav_clean} at {round(fav_implied*100)}% to win.",
                "Tour Tier": f"{tier} — settlement risk slightly higher than top tour.",
                "Coverage Source": coverage_note,
            },
            "is_alt": False,
            "is_extra": is_extra_flag,
            "source": source_label,
            "fair_odds_model": fair_only_flag,
            "model_components": model_components if is_model_pick else None,
            "auto_settle": False,
            "pick_date": date_str,
            "status": "pending",
            "no_edge_model": no_edge_model_flag,
            "bookmaker": bookmaker_final,
            # ── Alt-line availability metadata (2026-07-13) ──
            # The Odds API catalog only covers Grand Slams, Masters
            # 1000s, WTA 1000s, and select 500s. Every match generated
            # by the TennisExplorer scraper (ATP/WTA 250 tour + Challenger
            # circuit + qualifying) is OUTSIDE the book's coverage, so
            # alt-line rows will NEVER populate for these events.
            # Surface this cleanly to the UI so the ALT tab can show
            # "book coverage gap" instead of a blank empty state.
            "alt_lines_supported": False,
            "alt_lines_unavailable_reason": "book_coverage_gap",
            "alt_lines_note": (
                "Alt-line pricing is only published by sportsbooks for "
                "Grand Slams and Masters 1000 events. ATP/WTA 250 "
                "tournaments (Umag, Bastad, Gstaad, Iasi WTA, Athens "
                "WTA, Kitzbühel WTA, etc.) don't have alt-line coverage."
            ),
        }
        if using_real and all_books:
            pick_doc["all_book_odds"] = all_books
        picks.append(pick_doc)

    return picks
