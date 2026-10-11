"""Preferred-book policy drift demonstration — 2026-10-03.

Compares the OLD median-based price with the NEW preferred-book
atomic selection on representative synthetic fixtures across MLB /
CFB / NFL / NHL / NBA.  Prints the exact bookmaker selected, the
median vs selected-book price, and the resulting
implied_probability / edge / Lock Score drift.

This is a demonstration + regression script (not a production path).
Run with:
    cd /app/backend && python -m tests.demos.preferred_book_drift_demo
"""
from __future__ import annotations

import json
import statistics
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))

from sports_engine import (
    _PREFERRED_BOOKS,
    _select_preferred_book_market,
    _price_from_outcome,
    _implied_prob,
    _median_price,
    _consensus_market,
    _build_pick,
)
from services.canonical_publication_boundary import (
    PublicationState,
    evaluate_publication,
)


# ─── Synthetic Odds-API game payload (representative shape) ────────
def _mk_game(sport, home, away, *, books):
    """books is a list of {key, h2h: [(team, price), ...], totals: (line, over_price, under_price)}"""
    return {
        "id": f"GAME-{sport}-{home}-{away}",
        "sport_key": sport,
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-10-03T23:00:00Z",
        "bookmakers": [
            {
                "key": b["key"],
                "title": b["key"].title(),
                "last_update": "2026-10-03T22:00:00Z",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": name, "price": price}
                        for name, price in b["h2h"]
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over",  "price": b["totals"][1], "point": b["totals"][0]},
                        {"name": "Under", "price": b["totals"][2], "point": b["totals"][0]},
                    ]},
                ],
            } for b in books
        ],
    }


# ─── Fixtures (realistic Odds-API-shaped per-book quotes) ──────────
FIXTURES = [
    {
        "sport": "MLB", "home": "Yankees", "away": "Red Sox",
        "books": [
            # Order DOES NOT follow _PREFERRED_BOOKS — proving the policy
            # correctly picks fanduel first regardless of payload order.
            {"key": "betmgm",     "h2h": [("Yankees", -138), ("Red Sox", +118)], "totals": (8.5, -108, -112)},
            {"key": "fanduel",    "h2h": [("Yankees", -142), ("Red Sox", +122)], "totals": (8.5, -110, -110)},
            {"key": "draftkings", "h2h": [("Yankees", -140), ("Red Sox", +120)], "totals": (8.5, -115, -105)},
            {"key": "caesars",    "h2h": [("Yankees", -135), ("Red Sox", +115)], "totals": (8.5, -112, -108)},
        ],
    },
    {
        "sport": "CFB", "home": "Georgia", "away": "Alabama",
        "books": [
            {"key": "draftkings", "h2h": [("Alabama", +125), ("Georgia", -148)], "totals": (54.5, -112, -108)},
            {"key": "fanduel",    "h2h": [("Alabama", +120), ("Georgia", -145)], "totals": (54.5, -108, -112)},
            {"key": "betmgm",     "h2h": [("Alabama", +130), ("Georgia", -152)], "totals": (54.5, -110, -110)},
        ],
    },
    {
        "sport": "NFL", "home": "Chiefs", "away": "Bills",
        "books": [
            {"key": "caesars",    "h2h": [("Chiefs", -155), ("Bills", +132)], "totals": (47.5, -105, -115)},
            {"key": "betmgm",     "h2h": [("Chiefs", -162), ("Bills", +138)], "totals": (47.5, -110, -110)},
            {"key": "fanduel",    "h2h": [("Chiefs", -158), ("Bills", +134)], "totals": (47.5, -108, -112)},
            {"key": "draftkings", "h2h": [("Chiefs", -160), ("Bills", +136)], "totals": (47.5, -115, -105)},
        ],
    },
    {
        "sport": "NHL", "home": "Oilers", "away": "Panthers",
        "books": [
            {"key": "pointsbet",  "h2h": [("Oilers", -118), ("Panthers", -102)], "totals": (6.5, -110, -110)},
            {"key": "fanduel",    "h2h": [("Oilers", -115), ("Panthers", -105)], "totals": (6.5, -112, -108)},
            {"key": "betmgm",     "h2h": [("Oilers", -122), ("Panthers", +102)], "totals": (6.5, -108, -112)},
        ],
    },
    {
        "sport": "NBA", "home": "Lakers", "away": "Celtics",
        "books": [
            {"key": "betmgm",     "h2h": [("Lakers", +148), ("Celtics", -175)], "totals": (223.5, -108, -112)},
            {"key": "draftkings", "h2h": [("Lakers", +150), ("Celtics", -180)], "totals": (223.5, -110, -110)},
            {"key": "fanduel",    "h2h": [("Lakers", +145), ("Celtics", -172)], "totals": (223.5, -112, -108)},
        ],
    },
]


def _run():
    print("\n" + "=" * 78)
    print(" PREFERRED-BOOK DRIFT DEMO — Old median vs preferred-book quote")
    print("=" * 78)
    print(f"Preferred-book order (unchanged, imported from soccer/real_odds.py):")
    print(f"  {' > '.join(_PREFERRED_BOOKS)}\n")

    header = ("SPORT   MARKET   OLD median  NEW book   NEW price  Δ American  "
              "OLD impl%   NEW impl%   Δ pp")
    print(header)
    print("-" * len(header))

    for fx in FIXTURES:
        sport, home, away = fx["sport"], fx["home"], fx["away"]
        game = _mk_game(sport, home, away, books=fx["books"])

        # ── OLD: median across all books ────────────────────────────
        h2h_outs = _consensus_market(game, "h2h")
        totals_outs = _consensus_market(game, "totals")
        old_home_ml = _median_price(h2h_outs, home)
        old_away_ml = _median_price(h2h_outs, away)
        old_over_price = _median_price(totals_outs, "Over")
        old_under_price = _median_price(totals_outs, "Under")

        # ── NEW: preferred-book atomic selection ────────────────────
        h2h_market, ml_book = _select_preferred_book_market(game, "h2h")
        tot_market, tot_book = _select_preferred_book_market(game, "totals")
        new_home_ml = _price_from_outcome(next((o for o in (h2h_market.get("outcomes") or []) if o.get("name") == home), None))
        new_away_ml = _price_from_outcome(next((o for o in (h2h_market.get("outcomes") or []) if o.get("name") == away), None))
        new_over_price = _price_from_outcome(next((o for o in (tot_market.get("outcomes") or []) if o.get("name") == "Over"), None))
        new_under_price = _price_from_outcome(next((o for o in (tot_market.get("outcomes") or []) if o.get("name") == "Under"), None))

        # ── Compare home-side ML ────────────────────────────────────
        d_ml = new_home_ml - old_home_ml
        d_impl = (_implied_prob(new_home_ml) - _implied_prob(old_home_ml)) * 100
        print(f"{sport:7s} ML-home  {old_home_ml:+7d}     {ml_book:11s} {new_home_ml:+7d}   {d_ml:+5d}       "
              f"{_implied_prob(old_home_ml)*100:6.2f}%     {_implied_prob(new_home_ml)*100:6.2f}%    {d_impl:+5.2f}")
        # Away-side ML
        d_ml = new_away_ml - old_away_ml
        d_impl = (_implied_prob(new_away_ml) - _implied_prob(old_away_ml)) * 100
        print(f"{sport:7s} ML-away  {old_away_ml:+7d}     {ml_book:11s} {new_away_ml:+7d}   {d_ml:+5d}       "
              f"{_implied_prob(old_away_ml)*100:6.2f}%     {_implied_prob(new_away_ml)*100:6.2f}%    {d_impl:+5.2f}")
        # Totals Over
        d_ml = new_over_price - old_over_price
        d_impl = (_implied_prob(new_over_price) - _implied_prob(old_over_price)) * 100
        print(f"{sport:7s} T-Over   {old_over_price:+7d}     {tot_book:11s} {new_over_price:+7d}   {d_ml:+5d}       "
              f"{_implied_prob(old_over_price)*100:6.2f}%     {_implied_prob(new_over_price)*100:6.2f}%    {d_impl:+5.2f}")
        print()

    print("=" * 78)
    print(" Lock-Score drift smoke-check — same model_win_prob, old vs new book_odds")
    print("=" * 78)
    for fx in FIXTURES[:3]:
        sport = fx["sport"]
        home, away = fx["home"], fx["away"]
        game = _mk_game(sport, home, away, books=fx["books"])
        # Compute median vs preferred for home-ML
        old_price = _median_price(_consensus_market(game, "h2h"), home)
        h2h_market, book = _select_preferred_book_market(game, "h2h")
        new_price = _price_from_outcome(next((o for o in (h2h_market.get("outcomes") or []) if o.get("name") == home), None))

        model_wp = 0.58                   # held constant
        old_pick = _build_pick(
            sport=sport, league=sport, event=f"{away} @ {home}",
            event_time="2026-10-03T23:00:00Z",
            market=f"{home} Moneyline", pick_side=home,
            model_win_prob=model_wp, book_odds=old_price,
            lock=0.0, factors={}, insights=[],
            external_id=f"{sport}-OLD-ML",
            home_team_name=home, away_team_name=away,
        )
        new_pick = _build_pick(
            sport=sport, league=sport, event=f"{away} @ {home}",
            event_time="2026-10-03T23:00:00Z",
            market=f"{home} Moneyline", pick_side=home,
            model_win_prob=model_wp, book_odds=new_price,
            lock=0.0, factors={}, insights=[],
            external_id=f"{sport}-NEW-ML",
            home_team_name=home, away_team_name=away,
            odds_source="the_odds_api", bookmaker=book,
        )
        print(f"{sport:5s} home-ML:  old={old_price:+5d} new={new_price:+5d} ({book:11s})  "
              + (
                f"OLD book_implied={old_pick['implied_probability']:6.2f}%  "
                f"NEW book_implied={new_pick['implied_probability']:6.2f}%  "
                f"OLD edge={old_pick['edge_percent']:+6.2f}%  "
                f"NEW edge={new_pick['edge_percent']:+6.2f}%"
                if (old_pick and new_pick)
                else f"(pick builder rejected one side — not a drift concern)"
              ))

    print("\nNote: 'lock_score' is passed through as the ``lock`` kwarg from "
          "``compute_lock_score`` call sites — it is NOT re-computed inside "
          "``_build_pick``.  Lock Score drift surfaces only via the real "
          "producer call chain (``compute_lock_score(…, pick={'book_odds': …})"
          "``), which the ML/totals builders already invoke upstream of "
          "``_build_pick``.  This demo isolates the ``book_odds`` -> "
          "implied_probability -> edge chain for clarity.")


if __name__ == "__main__":
    _run()
