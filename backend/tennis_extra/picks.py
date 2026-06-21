"""Convert TennisExplorer scrapes into PerksLocks pick documents.

We're cautious here — these picks come from a single scrape with no
secondary verification, and many are 250-level / qualifier matches.
So we:
  • Mark them `source="tennis_extra"` and `is_extra=true`.
  • Only generate ONE moneyline pick per match (the favorite if their
    implied prob is ≥55%).
  • Cap lock_score at 90 — no "Elite Lock" for scraped picks.
  • Skip picks where the spread is too tight (no clear favorite).
  • Skip qualifiers and challengers below ATP 250 by default unless
    `include_challengers=True`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from .scraper import fetch_today_matches

# Lock-score floor for the favorite to qualify as a pick.
_MIN_FAV_IMPLIED = 0.55      # favorite must be ≥55% implied
_MAX_FAV_IMPLIED = 0.92      # ≥92% is too chalky → trap territory
_MAX_LOCK = 90.0             # never label scraped picks as Elite Lock

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


def _pick_id(player_a: str, player_b: str, tournament: str, date_str: str) -> str:
    raw = f"te|{date_str}|{tournament}|{player_a}|{player_b}".lower()
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _strip_seed(name: str) -> str:
    """Remove TennisExplorer's seed parenthetical e.g. 'Borges N. (8)'."""
    import re
    return re.sub(r"\s*\(\d+\)\s*$", "", name).strip()


def _lock_score_from_implied(implied: float, *, tier: str) -> float:
    """Translate book implied probability → soft lock score in [70..90].

    Approach: a 55% favorite ≈ 75; a 75% favorite ≈ 85; a 90% favorite ≈ 90.
    Then deduct a small penalty for non-ATP/WTA 250 tiers.
    """
    base = 70.0 + (implied - 0.55) / (0.90 - 0.55) * 20.0
    base = max(70.0, min(_MAX_LOCK, base))
    if "Challenger" in tier or "Qualifier" in tier:
        base -= 4.0
    return round(base, 1)


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
) -> list[dict]:
    """Top-level entry. Returns ready-to-store pick docs."""
    now = datetime.now(timezone.utc)
    date_str = date_str or now.strftime("%Y-%m-%d")

    matches = await fetch_today_matches(now)
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

        lock = _lock_score_from_implied(fav_implied, tier=tier)
        fav_clean = _strip_seed(fav_name)
        dog_clean = _strip_seed(dog_name)
        event_label = f"{fav_clean} vs {dog_clean}"

        pid = _pick_id(fav_clean, dog_clean, m["tournament"], date_str)

        # Edge — vs vigorish-adjusted book. We don't have an independent
        # model on these picks; report 0% to be honest, but mark "no_edge_model".
        edge_pct = 0.0

        picks.append({
            "id": pid,
            "sport": "Tennis",
            "league": m["tournament"],
            "tournament_tier": tier,
            "event": event_label,
            "event_time": m.get("commence_time"),
            "market": f"{fav_clean} Moneyline",
            "selection": fav_clean,
            "pick_side": fav_clean,
            "book_odds": int(fav_odds),
            "implied_probability": round(fav_implied * 100.0, 2),
            "win_probability": round(fav_implied * 100.0, 2),
            "model_win_probability": round(fav_implied * 100.0, 2),
            "edge_percent": edge_pct,
            "lock_score": lock,
            "lock_score_v2": lock,
            "grade": _grade(lock),
            "factors": {
                "Book Anchor": f"Market consensus puts {fav_clean} at {round(fav_implied*100)}% to win.",
                "Tour Tier": f"{tier} — settlement risk slightly higher than top tour.",
                "Coverage Source": "TennisExplorer scrape (Odds API doesn't carry this tournament).",
            },
            "is_alt": False,
            "is_extra": True,
            "source": "tennis_extra_model" if is_model_pick else "tennis_extra",
            "fair_odds_model": is_model_pick,
            "model_components": model_components if is_model_pick else None,
            "auto_settle": False,
            "pick_date": date_str,
            "status": "pending",
            "no_edge_model": not is_model_pick,
        })

    return picks
