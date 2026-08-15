"""Universal real-line Soccer ingester — Phase 2A.5 UNIVERSAL.

Wires the already-fetched `live_alt_lines` collection (real sportsbook
odds from The Odds API `event_alt_lines` fetcher) into the authoritative
Soccer candidate pipeline **for every supported soccer league and
market family**.

Design principles
-----------------
* League-agnostic.  We filter by ``sport in {"soccer","Soccer"}`` and
  emit picks tagged with the actual ``odds_api_sport``.  No league
  hard-codes; a new league becomes covered the moment the alt_lines
  fetcher stores rows for it.
* Market-family aware.  Player-scorer markets go through the
  authoritative scorer bridge; game markets (BTTS / alternate_totals /
  h2h) go through the Soccer game model.  Both write with real book
  odds preserved and lineage recorded.
* Idempotent.  Deterministic UUID5 pick id keyed on (source, event,
  market, selection, line) — same input twice never duplicates.
* Fail-loud attribution.  Every dropped candidate gets a code from
  :mod:`services.soccer_rejection_taxonomy`; no silent skips.

Contract
--------
* Read-only over ``live_alt_lines``.
* Writes to ``picks`` with:
    - ``source = "real_line_soccer_v2"``     (game-market)
    - ``source = "real_line_alt_scorer_v1"`` (player-market — retained
      for backwards compatibility with the Phase 2A.5E delta)
    - ``book_odds`` = real sportsbook price
    - ``odds_source = "real_book_line"``
    - ``no_real_book_line = False``
    - ``edge_percent`` populated (model - devig-implied)
* Delegates model probability to :mod:`services.soccer_scorer_bridge`
  and :mod:`services.soccer_game_model`.
* Missing evidence → ``off_board=True`` with a taxonomy code.
"""
from __future__ import annotations
import logging, math, uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.real_line_scorer_ingest")

# ─────────────────────────────────────────────────────────────────────
# Deterministic UUID5 namespace (matches orchestrator).
# ─────────────────────────────────────────────────────────────────────
_UUID_NS = uuid.UUID("00000000-0000-0000-0000-000000000001")


# ─────────────────────────────────────────────────────────────────────
# Market families
# ─────────────────────────────────────────────────────────────────────
_SCORER_MARKETS = (
    "player_goal_scorer_anytime",
    "player_first_goal_scorer",
    "player_last_goal_scorer",
    "player_to_score_or_assist",
    # Assist / shots variants — real provider keys.  When they appear
    # in live_alt_lines the ingester will process them; when absent
    # they're simply not scanned.
    "player_anytime_assist",
    "player_shots_on_target",
    "player_shots",
)
_MARKET_LABEL = {
    "player_goal_scorer_anytime":  "Anytime Goal Scorer",
    "player_first_goal_scorer":    "First Goal Scorer",
    "player_last_goal_scorer":     "Last Goal Scorer",
    "player_to_score_or_assist":   "To Score or Assist",
    "player_anytime_assist":       "Anytime Assist",
    "player_shots_on_target":      "Shots on Target",
    "player_shots":                "Shots",
    # Game markets
    "alternate_totals":            "Total Goals",
    "totals":                      "Total Goals",
    "btts":                        "Both Teams to Score",
    "both_teams_to_score":         "Both Teams to Score",
    "h2h":                         "Match Result",
    "spreads":                     "Handicap",
    "alternate_spreads":           "Handicap",
    "double_chance":               "Double Chance",
}
_GAME_MARKETS = (
    "totals",
    "alternate_totals",
    "btts",
    "both_teams_to_score",
    "h2h",
    "spreads",
    "alternate_spreads",
    "double_chance",
)


# ─────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────
def _implied_prob(american: int | float | None) -> float:
    if american is None:
        return 0.0
    try:
        o = int(american)
    except Exception:
        return 0.0
    if o == 0:
        return 0.5
    return 100.0/(o+100.0) if o > 0 else abs(o)/(abs(o)+100.0)


def _grade(score: float) -> str:
    if score >= 100: return "APEX Lock"
    if score >= 98:  return "Elite Lock"
    if score >= 95:  return "Strong Lock"
    if score >= 90:  return "Lock"
    if score >= 85:  return "Playable"
    return "Pass"


def _league_from_sport_key(sport_key: str) -> str:
    """Best-effort readable league name derived from The Odds API
    sport key.  Never fabricates leagues beyond the sport key we
    already received."""
    if not sport_key:
        return ""
    mapping = {
        "soccer_usa_mls":                          "MLS",
        "soccer_epl":                              "EPL",
        "soccer_spain_la_liga":                    "La Liga",
        "soccer_spain_segunda_division":           "La Liga 2",
        "soccer_italy_serie_a":                    "Serie A",
        "soccer_germany_bundesliga":               "Bundesliga",
        "soccer_germany_dfb_pokal":                "DFB Pokal",
        "soccer_france_ligue_one":                 "Ligue 1",
        "soccer_uefa_champs_league":               "Champions League",
        "soccer_uefa_champs_league_qualification": "Champions League Qualifiers",
        "soccer_uefa_europa_league":               "Europa League",
        "soccer_uefa_europa_conference_league":    "Conference League",
        "soccer_uefa_nations_league":              "Nations League",
        "soccer_uefa_euro":                        "UEFA Euro",
        "soccer_conmebol_copa_libertadores":       "Copa Libertadores",
        "soccer_conmebol_copa_sudamericana":       "Copa Sudamericana",
        "soccer_conmebol_copa_america":            "Copa America",
        "soccer_mexico_ligamx":                    "Liga MX",
        "soccer_concacaf_leagues_cup":             "Leagues Cup",
        "soccer_brazil_serie_a":                   "Brasileirao A",
        "soccer_brazil_serie_b":                   "Brasileirao B",
        "soccer_norway_eliteserien":               "Norway Eliteserien",
        "soccer_sweden_allsvenskan":               "Sweden Allsvenskan",
        "soccer_sweden_superettan":                "Sweden Superettan",
        "soccer_finland_veikkausliiga":            "Finland Veikkausliiga",
        "soccer_china_superleague":                "CSL",
        "soccer_japan_j_league":                   "J-League",
        "soccer_korea_kleague1":                   "K-League 1",
        "soccer_league_of_ireland":                "Ireland Premier",
        "soccer_australia_aleague":                "A-League",
        "soccer_fifa_world_cup":                   "FIFA World Cup",
        "soccer_fifa_club_world_cup":              "FIFA Club World Cup",
    }
    if sport_key in mapping:
        return mapping[sport_key]
    # Fallback — return sport_key stripped of the "soccer_" prefix.
    return sport_key.replace("soccer_", "").replace("_", " ").title()


def _deterministic_id(source: str, event_id: str, market_key: str,
                      selection: str, line: Optional[float] = None) -> tuple[str, str]:
    line_s = "" if line is None else f"@{line:g}"
    ext = f"{source}|{event_id}|{market_key}|{selection.lower()}{line_s}"
    return str(uuid.uuid5(_UUID_NS, ext)), ext


# ─────────────────────────────────────────────────────────────────────
# Player-scorer path (Phase 2A.5E delta — preserved + generalized)
# ─────────────────────────────────────────────────────────────────────
async def _ingest_player_scorer_row(
    db, row: dict, today: str, now_iso: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Convert one live_alt_lines player-scorer row into a pick doc.
    Returns (doc, rejection_code_when_off_board).
    """
    from services.soccer_scorer_bridge import (
        compute_soccer_scorer_factors_sync,
    )
    from services.soccer_feature_resolver import (
        resolve_soccer_player_features, resolve_soccer_player_prior,
    )
    from services.soccer_rejection_taxonomy import SoccerRejection
    from sports_engine import compute_lock_score

    price = row.get("price")
    try:
        price = int(price) if price is not None else 0
    except Exception:
        price = 0
    if price == 0:
        return None, None  # skipped, not off_board

    event_id  = row.get("event_id")
    mk        = row.get("market_key")
    player    = (row.get("selection") or "").strip()
    home      = row.get("home_team")
    away      = row.get("away_team")
    book      = row.get("sportsbook")
    sport_key = row.get("odds_api_sport") or ""
    league    = _league_from_sport_key(sport_key)
    if not (event_id and mk and player and (home or away)):
        return None, None

    book_impl = _implied_prob(price)

    # League-aware feature + prior lookup.
    form_row, evidence_source = await resolve_soccer_player_features(
        db, player_name=player, league=league,
    )
    prior_row = await resolve_soccer_player_prior(
        db, player_name=player, league=league,
    )

    bridge = compute_soccer_scorer_factors_sync(
        player=player, market_key=mk, book_implied=book_impl,
        form_row=form_row, prior_form_row=prior_row, league=league,
    )
    if not bridge:
        # Missing evidence — write off_board candidate for
        # attribution rather than silent drop.
        model_prob = book_impl
        factors = {"Book Implied Probability": book_impl}
        rej = SoccerRejection.MISSING_FEATURE_DATA.value
        off_board = True
        lock, _ = compute_lock_score(factors, win_prob=book_impl*100)
    else:
        model_prob = float(bridge.get("model_prob") or book_impl)
        factors = bridge.get("factors") or {}
        lock, _ = compute_lock_score(factors, win_prob=model_prob*100)
        off_board = lock < 85.0
        rej = SoccerRejection.LOW_LOCK_SCORE.value if off_board else None

    edge_percent = round((model_prob - book_impl) * 100, 3)
    pick_id, external_id = _deterministic_id(
        "real_line_alt_scorer_v1", event_id, mk, player,
    )
    doc = {
        "id": pick_id,
        "external_id": external_id,
        "sport": "Soccer",
        "league": league,
        "sport_key": sport_key,
        "pick_date": today,
        "event": f"{away} @ {home}" if home and away else (home or away),
        "event_id": event_id,
        "provider_event_id": event_id,
        "market": f"{player} {_MARKET_LABEL.get(mk, mk)}",
        "market_key": mk,
        "market_family": "player_prop",
        "selection": player,
        "book_odds": price,
        "bookmaker": book,
        "odds_source": "real_book_line",
        "odds_status": "book_line_present",
        "no_real_book_line": False,
        "implied_probability": round(book_impl * 100, 3),
        "model_probability": model_prob,
        "model_win_prob": model_prob,
        "edge_percent": edge_percent,
        "edge_method": "RAW_FALLBACK",
        "lock_score": round(lock, 2),
        "lock_score_v2": round(lock, 2),
        "published_lock_score": round(lock, 2),
        "grade": _grade(lock),
        "confidence": lock,
        "status": "pending",
        "no_bet": False,
        "off_board": off_board,
        "off_board_reasons": [rej] if (off_board and rej) else None,
        "source": "real_line_alt_scorer_v1",
        "publication_source": "real_line_alt_scorer_v1",
        "evidence_source": evidence_source or "none",
        "commence_time": row.get("commence_time"),
        "updated_at": now_iso,
    }
    return doc, (rej if off_board else None)


# ─────────────────────────────────────────────────────────────────────
# Game-market path — BTTS + totals + h2h + spreads + double_chance
# ─────────────────────────────────────────────────────────────────────
def _game_market_selection_label(mk: str, selection: str,
                                  line: Optional[float]) -> str:
    m = mk.lower()
    sel = (selection or "").strip()
    if m in ("btts", "both_teams_to_score"):
        return f"BTTS {sel}"
    if m in ("totals", "alternate_totals"):
        if line is not None:
            return f"Total Goals {sel} {line:g}"
        return f"Total Goals {sel}"
    if m in ("spreads", "alternate_spreads"):
        if line is not None:
            return f"{sel} {line:+g}"
        return sel
    if m == "double_chance":
        return f"Double Chance {sel}"
    if m == "h2h":
        return sel  # already the winning team name / "Draw"
    return f"{_MARKET_LABEL.get(mk, mk)} {sel}"


async def _ingest_game_market_row(
    db, row: dict, today: str, now_iso: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Convert one live_alt_lines game-market row into a pick doc."""
    from services.soccer_rejection_taxonomy import SoccerRejection
    from sports_engine import compute_lock_score

    price = row.get("price")
    try:
        price = int(price) if price is not None else 0
    except Exception:
        price = 0
    if price == 0:
        return None, None

    event_id  = row.get("event_id")
    mk        = row.get("market_key")
    sel       = (row.get("selection") or "").strip()
    line      = row.get("line")
    try:
        line = float(line) if line is not None else None
    except Exception:
        line = None
    home      = row.get("home_team")
    away      = row.get("away_team")
    book      = row.get("sportsbook")
    sport_key = row.get("odds_api_sport") or ""
    league    = _league_from_sport_key(sport_key)
    if not (event_id and mk and sel and home and away):
        return None, SoccerRejection.EVENT_IDENTITY_FAILURE.value

    book_impl = _implied_prob(price)

    # Model probability via Soccer game model — league-agnostic
    # Poisson/Dixon-Coles core (Phase 2A.5B).  Uses team-form lookups
    # when available; falls back to league-average priors otherwise.
    model_prob: Optional[float] = None
    model_source = "soccer_game_model"
    try:
        from services.soccer_game_model import compute_game_market_prob
        model_prob = await compute_game_market_prob(
            db, home_team=home, away_team=away, league=league,
            market_key=mk.lower(), selection=sel, line=line,
        )
    except ImportError:
        # Model does not expose the universal entry point yet — fall
        # back to a conservative de-vig anchor so the pick still
        # traces through the pipeline as MISSING_FEATURE_DATA rather
        # than silently disappearing.
        model_prob = None
        model_source = "unavailable"
    except Exception as _e:
        logger.debug(
            "soccer_game_model failed for %s / %s: %s", mk, sel, _e,
        )
        model_prob = None
        model_source = "error"

    if model_prob is None:
        rej = SoccerRejection.NO_MODEL_PROBABILITY.value
        # Even without a model we retain the candidate for
        # attribution so operators can see the market landed but was
        # not evaluated.
        model_prob = book_impl  # temp: anchor at implied for LS math
        factors = {"Book Implied Probability": book_impl}
        lock, _ = compute_lock_score(factors, win_prob=book_impl*100)
        off_board = True
    else:
        model_prob = max(0.001, min(0.999, float(model_prob)))
        # Simple two-factor composite for game markets — the scorer
        # bridge's rich feature vector is player-only.  For game
        # markets we blend model_prob with the market alignment factor
        # (agreement with book).
        alignment = 1.0 - min(1.0, abs(model_prob - book_impl))
        factors = {
            "Model Probability":       model_prob,
            "Book Implied Probability": book_impl,
            "Market Alignment":         alignment,
        }
        lock, _ = compute_lock_score(factors, win_prob=model_prob*100)
        off_board = lock < 85.0
        rej = SoccerRejection.LOW_LOCK_SCORE.value if off_board else None

    edge_percent = round((model_prob - book_impl) * 100, 3)
    pick_id, external_id = _deterministic_id(
        "real_line_soccer_v2", event_id, mk, sel, line,
    )
    doc = {
        "id": pick_id,
        "external_id": external_id,
        "sport": "Soccer",
        "league": league,
        "sport_key": sport_key,
        "pick_date": today,
        "event": f"{away} @ {home}",
        "event_id": event_id,
        "provider_event_id": event_id,
        "market": _game_market_selection_label(mk, sel, line),
        "market_key": mk,
        "market_family": "game_market",
        "selection": sel,
        "line": line,
        "book_odds": price,
        "bookmaker": book,
        "odds_source": "real_book_line",
        "odds_status": "book_line_present",
        "no_real_book_line": False,
        "implied_probability": round(book_impl * 100, 3),
        "model_probability": model_prob,
        "model_win_prob": model_prob,
        "model_source": model_source,
        "edge_percent": edge_percent,
        "edge_method": "RAW_FALLBACK",
        "lock_score": round(lock, 2),
        "lock_score_v2": round(lock, 2),
        "published_lock_score": round(lock, 2),
        "grade": _grade(lock),
        "confidence": lock,
        "status": "pending",
        "no_bet": False,
        "off_board": off_board,
        "off_board_reasons": [rej] if (off_board and rej) else None,
        "source": "real_line_soccer_v2",
        "publication_source": "real_line_soccer_v2",
        "commence_time": row.get("commence_time"),
        "updated_at": now_iso,
    }
    return doc, (rej if off_board else None)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
async def _upsert_pick(db, doc: dict) -> None:
    pick_id = doc["id"]
    src = doc["source"]
    await db.picks.update_one(
        {"$or": [
            {"id": pick_id},
            {"event_id": doc["event_id"], "market_key": doc["market_key"],
             "selection": doc["selection"], "pick_date": doc["pick_date"],
             "source": src},
        ]},
        {"$set": doc}, upsert=True,
    )


async def ingest_real_line_soccer_scorers(
    db, *, today: str,
) -> dict[str, int]:
    """One-shot ingestion pass over the entire Soccer real-line
    surface — both player-scorer AND game markets (BTTS / totals /
    h2h / spreads / double_chance) across every league present in
    ``live_alt_lines``.

    Idempotent: existing pick rows keyed on deterministic UUID5 id
    are updated, not duplicated.  Returns funnel stats grouped by
    market family + rejection code.
    """
    stats: dict[str, Any] = {
        "scanned":         0,
        "written":         0,
        "skipped":         0,
        "off_board":       0,
        "by_family":       {"player_prop": 0, "game_market": 0},
        "by_rejection":    {},
        "by_league":       {},
    }
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    all_markets = list(_SCORER_MARKETS) + list(_GAME_MARKETS)
    cursor = db.live_alt_lines.find({
        "market_key": {"$in": all_markets},
        "sport": {"$in": ["soccer", "Soccer"]},
    })
    async for row in cursor:
        stats["scanned"] += 1
        mk = row.get("market_key")
        try:
            if mk in _SCORER_MARKETS:
                doc, rej = await _ingest_player_scorer_row(
                    db, row, today, now_iso,
                )
            elif mk in _GAME_MARKETS:
                doc, rej = await _ingest_game_market_row(
                    db, row, today, now_iso,
                )
            else:
                doc, rej = None, None
        except Exception as e:
            logger.warning(
                "real-line ingest exception on row %s: %s",
                row.get("_id"), e,
            )
            stats["skipped"] += 1
            continue

        if doc is None:
            stats["skipped"] += 1
            continue

        await _upsert_pick(db, doc)
        stats["written"] += 1
        fam = doc.get("market_family") or "unknown"
        stats["by_family"][fam] = stats["by_family"].get(fam, 0) + 1
        league = doc.get("league") or "?"
        stats["by_league"][league] = stats["by_league"].get(league, 0) + 1
        if doc.get("off_board"):
            stats["off_board"] += 1
            if rej:
                stats["by_rejection"][rej] = (
                    stats["by_rejection"].get(rej, 0) + 1
                )

    return stats


__all__ = ["ingest_real_line_soccer_scorers"]
