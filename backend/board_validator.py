"""Validation-first architecture for PerksLocks board publishing.

User spec 2026-07-04: "PerksLocks needs to move to a validation-first
architecture instead of a generation-first architecture. Every published
pick should be fully validated, explainable, traceable, and graded
against the exact pick that was originally published."

This module implements Session-1 scope:

  §1 Contradiction detection — reject both-sides-of-same-market
  §2 Batter vs pitcher validation — reject impossible matchups
  §3 Immutable snapshot — every pick gets a locked `snapshot` payload
  §6 Board quality gate — never publish "filler" picks below threshold
  §4 Rollover tagging — pin picks to the rollover board at publish time

Sessions 2-3 (chalk-bias fix, lock-score integrity, evidence threshold,
automated integrity checks) build on this foundation.

Note: the existing `pick_validator.py` handles math-drift healing on
the DB side (edge/implied-prob/lock-score consistency). This module is
distinct — it runs at PUBLISH time against the in-memory candidate
list, BEFORE picks are written to the picks collection.

USAGE
    from board_validator import validate_and_finalize
    safe_picks, report = validate_and_finalize(safe_picks)
    logger.info("Board validator: %s", report)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.board_validator")


# ─────────────────────── shared helpers ───────────────────────────────

def _norm(s: str) -> str:
    if not s:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    ).strip().lower()


def _line_from_market(market: str) -> Optional[str]:
    if not market:
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)", market)
    return m.group(1) if m else None


def _side_from_market(market: str) -> Optional[str]:
    ml = (market or "").lower()
    if "team total" in ml:
        if "over" in ml:
            return "team_over"
        if "under" in ml:
            return "team_under"
        return None
    if "over" in ml:
        return "over"
    if "under" in ml:
        return "under"
    return None


def _has_real_player(pick: dict) -> bool:
    """True if the pick has an explicit player identity (not just an
    Over/Under game total). Prefer explicit `selection`/`player_name`
    fields; fall back to detecting the (TEAM) abbreviation pattern
    that marks bookmaker-formatted player-prop markets."""
    sel = (pick.get("selection") or "").strip().lower()
    if sel and sel not in ("over", "under", "yes", "no"):
        return True
    if pick.get("player_name"):
        return True
    # Bookmaker player-prop markets have "Player Name (ABBR)" prefix.
    m = re.search(r"\(([A-Z]{2,4})\)", pick.get("market") or "")
    return bool(m)


def _extract_player(pick: dict) -> str:
    if not _has_real_player(pick):
        return ""
    try:
        from quality_gate import _extract_player_from_pick
        return _norm(_extract_player_from_pick(pick) or "")
    except Exception:
        return _norm(pick.get("selection") or "")


def _score(pick: dict) -> float:
    return (
        float(pick.get("lock_score_v2") or pick.get("lock_score") or 0)
        + max(0.0, float(pick.get("edge_percent") or 0)) * 0.05
    )


# ─────────────────────── §1 Contradiction detection ───────────────────

def remove_contradictions(picks: list[dict]) -> tuple[list[dict], dict]:
    """Remove picks contradicting each other on the same market/game.

    Handled:
      • Game Total Over vs Under (same line)
      • Team Total Over vs Under (same team + line)
      • Player prop Over vs Under (same player + stat + line)
      • Both-team Moneyline on same game
    """
    stats = {"scanned": len(picks), "dropped": 0, "reasons": {}}
    if not picks:
        return picks, stats

    groups: dict[tuple, list[dict]] = {}
    for p in picks:
        event = p.get("event") or ""
        market = p.get("market") or ""
        ml = market.lower()
        line = _line_from_market(market)
        side = _side_from_market(market)
        if side is None:
            continue
        if side in ("team_over", "team_under"):
            m = re.match(r"^(.+?)\s+Team\s+Total", market, re.IGNORECASE)
            team = _norm(m.group(1)) if m else ""
            key = (event, "team_total", team, line)
        elif "goal scorer" in ml or "to score" in ml:
            key = (event, "player_prop", _extract_player(p), line)
        elif (
            _extract_player(p)
            and any(k in ml for k in (
                "hits", "strikeouts", "outs recorded", "total bases",
                " runs", "rbi", "points", "rebounds", "assists",
                "games", "sets", "home run",
            ))
        ):
            # Player prop: requires an actual player name AND a stat keyword.
            family = re.sub(r"\s*(over|under)\s+[-\d.]+.*", "", ml).strip()
            key = (event, "player_prop", _extract_player(p), family, line)
        else:
            # Bare game total (no player) — group by line only.
            key = (event, "game_total", line)
        groups.setdefault(key, []).append(p)

    keep: set[int] = {id(p) for p in picks}
    for key, bucket in groups.items():
        if len(bucket) <= 1:
            continue
        sides_present = {
            _side_from_market(b.get("market") or "") for b in bucket
        }
        if len(sides_present) <= 1:
            continue
        bucket.sort(key=_score, reverse=True)
        winner = bucket[0]
        for loser in bucket[1:]:
            if id(loser) in keep:
                keep.discard(id(loser))
                stats["dropped"] += 1
                r = f"contradict_{key[1]}"
                stats["reasons"][r] = stats["reasons"].get(r, 0) + 1
                logger.debug("Drop contradiction (%s): %s vs kept %s",
                             r, loser.get("market"), winner.get("market"))

    # Both-teams Moneyline on same event
    ml_groups: dict[str, list[dict]] = {}
    for p in picks:
        market = (p.get("market") or "").lower()
        if "moneyline" not in market and not market.endswith(" to win"):
            continue
        if id(p) not in keep:
            continue
        event = p.get("event") or ""
        if not event:
            continue
        ml_groups.setdefault(event, []).append(p)
    for event, bucket in ml_groups.items():
        if len(bucket) <= 1:
            continue
        bucket.sort(key=_score, reverse=True)
        for loser in bucket[1:]:
            keep.discard(id(loser))
            stats["dropped"] += 1
            stats["reasons"]["contradict_moneyline"] = (
                stats["reasons"].get("contradict_moneyline", 0) + 1
            )

    return [p for p in picks if id(p) in keep], stats


# ─────────────────────── §2 Batter vs pitcher validation ──────────────

_MLB_ABBR: dict[str, str] = {
    "ATL": "Atlanta Braves", "AZ": "Arizona Diamondbacks",
    "ARI": "Arizona Diamondbacks", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Athletics",
    "ATH": "Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres",
    "SEA": "Seattle Mariners", "SF": "San Francisco Giants",
    "SFG": "San Francisco Giants", "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
}


def _player_team_from_market(market: str) -> Optional[str]:
    if not market:
        return None
    m = re.search(r"\(([A-Z]{2,4})\)", market)
    if not m:
        return None
    return _MLB_ABBR.get(m.group(1).upper())


def validate_batter_pitcher(picks: list[dict]) -> tuple[list[dict], dict]:
    """Reject impossible MLB matchups."""
    stats = {"scanned": 0, "dropped": 0, "reasons": {}}
    survivors: list[dict] = []
    for p in picks:
        if (p.get("sport") or "").upper() != "MLB":
            survivors.append(p)
            continue
        market = p.get("market") or ""
        ml = market.lower()
        is_player_market = any(k in ml for k in (
            "hits", "home run", "strikeout", "outs recorded",
            "total bases", " rbi", "hits + runs",
        ))
        if not is_player_market:
            stats["scanned"] += 1
            survivors.append(p)
            continue

        stats["scanned"] += 1
        player_team = _player_team_from_market(market)
        event = (p.get("event") or "").strip()
        parts = re.split(r"\s+@\s+", event)
        if len(parts) != 2 or not player_team:
            survivors.append(p)
            continue
        away, home = parts[0].strip(), parts[1].strip()

        # Expand event team abbreviations if present ("NYY @ BOS" → full names)
        def _expand(t: str) -> str:
            u = t.strip().upper()
            return _MLB_ABBR.get(u, t)
        away_full = _expand(away)
        home_full = _expand(home)

        # Rule 1: player must be on one of the two teams playing.
        pt_norm = _norm(player_team)
        if pt_norm not in (_norm(away), _norm(home),
                            _norm(away_full), _norm(home_full)):
            stats["dropped"] += 1
            stats["reasons"]["player_team_not_in_event"] = (
                stats["reasons"].get("player_team_not_in_event", 0) + 1
            )
            logger.warning(
                "REJECT: player team %r not in event %r (%s)",
                player_team, event, market,
            )
            continue

        # Rule 2: opposing-pitcher must be from the OTHER team (never own).
        opp = (home_full if pt_norm in (_norm(away), _norm(away_full))
                else away_full)
        opp_pitcher_team = (
            p.get("opposing_pitcher_team")
            or (p.get("pick_rationale") or {}).get("opp_pitcher_team")
        )
        if opp_pitcher_team and _norm(opp_pitcher_team) != _norm(opp):
            stats["dropped"] += 1
            stats["reasons"]["batter_faces_own_team_pitcher"] = (
                stats["reasons"].get("batter_faces_own_team_pitcher", 0) + 1
            )
            logger.warning(
                "REJECT: batter %s (%s) vs pitcher from same team %s",
                _extract_player(p), player_team, opp_pitcher_team,
            )
            continue

        # Rule 3: pitcher props must be the probable starter when known.
        is_pitcher_market = ("strikeout" in ml or "outs recorded" in ml)
        if is_pitcher_market and p.get("is_probable_pitcher") is False:
            stats["dropped"] += 1
            stats["reasons"]["pitcher_not_probable"] = (
                stats["reasons"].get("pitcher_not_probable", 0) + 1
            )
            logger.warning("REJECT: %s not probable starter — %s",
                           _extract_player(p), market)
            continue

        survivors.append(p)

    return survivors, stats


# ─────────────────────── §6 Board quality gate ────────────────────────

_BOARD_QUALITY_FLOORS: dict[str, dict[str, float]] = {
    "default":                {"lock_min": 65, "edge_min": -3.0, "win_prob_min": 0.45},
    "MLB_prop":               {"lock_min": 70, "edge_min":  0.0, "win_prob_min": 0.50},
    "Soccer_ags":             {"lock_min": 75, "edge_min":  0.0, "win_prob_min": 0.30},
    "Tennis":                 {"lock_min": 70, "edge_min": -2.0, "win_prob_min": 0.50},
    # Tennis Extra (TennisExplorer scrape for Umag/Bastad/Gstaad/Athens/
    # Iasi/Kitzbuhel/etc.) is book-anchored — edge_percent is definitionally
    # 0.0 or slightly negative due to vig math between no-vig `win_probability`
    # and vig-included `book_odds`. Chalk favorites (-300 → -500) come out
    # at edge = -3% to -5% purely from the vig gap. Use a wide edge floor
    # (-10%) and lower lock_min so the ATP/WTA 250 slate isn't nuked.
    # User report 2026-07-12: "Why are these tennis games not being picked
    # up?" (Umag/Bastad/Gstaad on TU 14.07 all missing).
    "Tennis_scrape":          {"lock_min": 65, "edge_min": -10.0, "win_prob_min": 0.45},
    # 2026-07-22 — MLS ESPN leaderboard picks are LEADERBOARD-anchored
    # (source itself IS the evidence). Their `factors` dict is empty
    # (no bucket_n/xg model) and `apply_v2_calibration` doesn't touch
    # them so edge/win_prob defaults are looser. Give them their own
    # floor so board_quality doesn't nuke them.
    "Soccer_ags_scrape":      {"lock_min": 80, "edge_min": -1.0, "win_prob_min": 0.25},
}


_SOCCER_SCRAPE_SOURCES = {"mls_espn_leaderboard", "csl_espn_leaderboard"}


_TENNIS_SCRAPE_SOURCES = {"tennis_extra", "tennis_extra_model"}


def _quality_key(pick: dict) -> str:
    sport = (pick.get("sport") or "").upper()
    market = (pick.get("market") or "").lower()
    if sport == "MLB" and any(k in market for k in (
        "hits", "strikeouts", "outs recorded", "home run", "total bases", "rbi",
    )):
        return "MLB_prop"
    if sport == "SOCCER" and (
        "goal scorer" in market or "to score or assist" in market
    ):
        # 2026-07-22 — Leaderboard-sourced picks (source == mls_espn_
        # leaderboard) get their own lenient floor.
        if (pick.get("source") or "").lower() in _SOCCER_SCRAPE_SOURCES:
            return "Soccer_ags_scrape"
        return "Soccer_ags"
    if sport == "TENNIS":
        # Book-anchored scrape picks get their own key (see explanation on
        # `Tennis_scrape` floor definition above).
        if (pick.get("source") or "").lower() in _TENNIS_SCRAPE_SOURCES:
            return "Tennis_scrape"
        return "Tennis"
    return "default"


def enforce_board_quality(picks: list[dict]) -> tuple[list[dict], dict]:
    stats = {"scanned": len(picks), "dropped": 0, "reasons": {}}
    survivors: list[dict] = []
    for p in picks:
        key = _quality_key(p)
        floors = _BOARD_QUALITY_FLOORS.get(key, _BOARD_QUALITY_FLOORS["default"])
        lock = float(p.get("lock_score_v2") or p.get("lock_score") or 0)
        edge = float(p.get("edge_percent") or 0)
        wp = float(p.get("win_probability") or 0)
        if wp > 1.0:
            wp = wp / 100.0
        if lock < floors["lock_min"]:
            stats["dropped"] += 1
            r = f"lock_below_{int(floors['lock_min'])}"
            stats["reasons"][r] = stats["reasons"].get(r, 0) + 1
            continue
        if edge < floors["edge_min"]:
            stats["dropped"] += 1
            stats["reasons"]["edge_negative"] = (
                stats["reasons"].get("edge_negative", 0) + 1
            )
            continue
        if wp < floors["win_prob_min"]:
            stats["dropped"] += 1
            stats["reasons"]["win_prob_low"] = (
                stats["reasons"].get("win_prob_low", 0) + 1
            )
            continue
        survivors.append(p)
    return survivors, stats


# ─────────────────────── §8 Real-line integrity ───────────────────────
# Emergent Support durable fix (2026-06): picks without a real
# sportsbook line MUST NOT populate the main Locks board and MUST NOT
# be treated as if edge were 0.  Model-only picks are ANNOTATED (not
# dropped) so Extended Coverage endpoints can still serve them.

def enforce_real_market_line(picks: list[dict]) -> tuple[list[dict], dict]:
    """Route model-only / no-real-book-line picks off the main board.

    A pick is considered model-only when ANY of the following holds:
      * ``no_real_book_line == True``
      * ``model_only == True``
      * ``book_odds`` is None / missing / non-numeric
      * ``implied_probability`` is None / missing / non-numeric

    These picks are ANNOTATED with:
      * ``hide_from_main_board = True``
      * ``is_extra = True``
      * ``main_board_reclassified_reason = "no_real_book_line"``

    They are NOT dropped — Extended Coverage may still surface them.
    Missing edge is preserved as ``None`` (never coerced to 0).
    """
    stats = {"scanned": len(picks), "annotated": 0, "reasons": {}}
    for p in picks:
        reason = None
        if p.get("no_real_book_line") is True:
            reason = "no_real_book_line_flag"
        elif p.get("model_only") is True:
            reason = "model_only_flag"
        else:
            bo = p.get("book_odds")
            ip = p.get("implied_probability")
            if bo is None:
                reason = "book_odds_null"
            elif ip is None:
                reason = "implied_probability_null"
            else:
                try:
                    int(bo); float(ip)
                except (TypeError, ValueError):
                    reason = "book_odds_or_implied_prob_non_numeric"
        if reason is None:
            continue
        # Never coerce missing edge to 0 — leave as-is (typically None).
        if p.get("edge_percent") == 0:
            # Defensive: if edge got silently set to 0 despite no real
            # line, restore it to None so downstream filters can't
            # mistake it for a real 0% edge.
            p["edge_percent"] = None
        p["hide_from_main_board"] = True
        p["is_extra"] = True
        p["model_only"] = True
        p.setdefault("main_board_reclassified_reason", reason)
        p.setdefault("main_board_reclassified_at",
                     datetime.now(timezone.utc).isoformat())
        stats["annotated"] += 1
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    return picks, stats


# ─────────────────────── §3 Immutable snapshot ────────────────────────

def apply_immutable_snapshot(picks: list[dict]) -> tuple[list[dict], dict]:
    """Attach a locked `snapshot` payload to every pick. Idempotent."""
    stats = {"applied": 0, "already": 0}
    now = datetime.now(timezone.utc).isoformat()
    for p in picks:
        if p.get("snapshot"):
            stats["already"] += 1
            continue
        p["snapshot"] = {
            "pick_id": p.get("id"),
            "event_id": p.get("event_id") or p.get("game_id"),
            "sport": p.get("sport"),
            "league": p.get("league"),
            "event": p.get("event"),
            "market": p.get("market"),
            "selection": p.get("selection"),
            "line": _line_from_market(p.get("market") or ""),
            "book_odds": p.get("book_odds"),
            "bookmaker": p.get("bookmaker"),
            "event_time": p.get("event_time"),
            "player": _extract_player(p) or None,
            "home_team": p.get("home_team"),
            "away_team": p.get("away_team"),
            "lock_score": p.get("lock_score"),
            "lock_score_v2": p.get("lock_score_v2"),
            "win_probability": p.get("win_probability"),
            "edge_percent": p.get("edge_percent"),
            "grade": p.get("grade"),
            "confidence": p.get("confidence"),
            "reasoning": p.get("pick_rationale") or {},
            "published_at": now,
        }
        stats["applied"] += 1
    return picks, stats


# ─────────────────────── §4 Rollover tagging ──────────────────────────

ROLLOVER_LOCK_MIN = 95
ROLLOVER_WIN_PROB_MIN = 0.80
ROLLOVER_EDGE_MIN = 4.0


def _wp_frac(v) -> float:
    try:
        f = float(v or 0)
    except Exception:
        return 0.0
    return f / 100.0 if f > 1.0 else f


def tag_rollover_picks(picks: list[dict]) -> tuple[list[dict], dict]:
    """Stamp `on_rollover_at` on qualifying picks. Idempotent.

    Bug fix (2026-07-08): earlier revision preferred `lock_score_v2`
    (shadow-mode conservative score, always 2-4 pts below V1) which
    NEVER cleared the 95 gate — so `tag_rollover_picks` silently
    tagged nothing and Rollover History fell back to the legacy
    threshold path, which matched every pick because board_quality
    had already filtered to lock_score ≥ 89.  Result: the Rollover tab
    duplicated the "All" tab.  We now use the *user-facing*
    `lock_score` (V1) as the source of truth — that's the score the
    Rollover board itself uses at publish time.
    """
    stats = {"tagged": 0}
    now = datetime.now(timezone.utc).isoformat()
    for p in picks:
        if p.get("on_rollover_at"):
            continue
        lock = float(p.get("lock_score") or 0)
        edge = float(p.get("edge_percent") or 0)
        wp = _wp_frac(p.get("win_probability"))
        if (lock >= ROLLOVER_LOCK_MIN
                and wp >= ROLLOVER_WIN_PROB_MIN
                and edge >= ROLLOVER_EDGE_MIN):
            p["on_rollover_at"] = now
            stats["tagged"] += 1
    return picks, stats


# ─────────────────────── §10 Automated integrity checks ─────────────
# Final pre-publish gate. Rejects picks with missing metadata, invalid
# sportsbook lines, unplayable / ungraded state, or duplicate identity.

_ODDS_MIN = -100000  # absurd chalk cap
_ODDS_MAX = 100000   # lottery cap
_REQUIRED_FIELDS = ("id", "sport", "event", "market", "event_time", "book_odds")


def integrity_check(picks: list[dict]) -> tuple[list[dict], dict]:
    """Reject picks that cannot be safely published:

      • missing required fields (id / sport / event / market / event_time / book_odds)
      • book_odds outside sane range (-100 < |odds| < 100000 excluded)
      • event_time not parseable
      • duplicate identity (event + market + selection) — keep highest lock
    """
    stats = {"scanned": len(picks), "dropped": 0, "reasons": {}}
    if not picks:
        return picks, stats
    survivors: list[dict] = []
    dedupe: dict[tuple, dict] = {}
    for p in picks:
        # Required fields
        missing = [f for f in _REQUIRED_FIELDS if not p.get(f)]
        if missing:
            stats["dropped"] += 1
            r = f"missing_{missing[0]}"
            stats["reasons"][r] = stats["reasons"].get(r, 0) + 1
            # Phase 1C §8 — funnel-attributable integrity drop.
            try:
                from services import funnel_telemetry as _funnel
                _funnel.record(
                    sport=p.get("sport") or "unknown",
                    market=p.get("market") or "*",
                    stage="board_validator",
                    reason="INTEGRITY_CHECK_FAILED",
                    event=p.get("event"), detail=r,
                )
            except Exception:
                pass
            continue
        # Odds sanity — American odds must be ≥ +100 or ≤ -100 (no 0/±99).
        try:
            odds = int(p.get("book_odds"))
        except (TypeError, ValueError):
            stats["dropped"] += 1
            stats["reasons"]["invalid_odds"] = (
                stats["reasons"].get("invalid_odds", 0) + 1
            )
            continue
        if odds == 0 or (-100 < odds < 100) or abs(odds) > _ODDS_MAX:
            stats["dropped"] += 1
            stats["reasons"]["invalid_odds"] = (
                stats["reasons"].get("invalid_odds", 0) + 1
            )
            continue
        # Event-time parseable
        try:
            datetime.fromisoformat(
                (p.get("event_time") or "").replace("Z", "+00:00")
            )
        except Exception:
            stats["dropped"] += 1
            stats["reasons"]["invalid_event_time"] = (
                stats["reasons"].get("invalid_event_time", 0) + 1
            )
            continue
        # Dedupe by identity — keep highest lock
        key = (p.get("sport"), p.get("event"), p.get("market"),
               (p.get("selection") or "").strip().lower())
        existing = dedupe.get(key)
        if existing and _score(existing) >= _score(p):
            stats["dropped"] += 1
            stats["reasons"]["duplicate_identity"] = (
                stats["reasons"].get("duplicate_identity", 0) + 1
            )
            continue
        if existing:
            # Replace worse existing
            survivors.remove(existing)
            stats["dropped"] += 1
            stats["reasons"]["duplicate_identity"] = (
                stats["reasons"].get("duplicate_identity", 0) + 1
            )
        dedupe[key] = p
        survivors.append(p)
    return survivors, stats


# ─────────────────────── §7 Evidence threshold ──────────────────────
# Each pick must be supported by N independent evidence factors so we
# don't publish "just-one-signal" picks. Counted signals:
#   1. Non-empty pick_rationale (structured reasoning)
#   2. Bucket data (learning engine sample ≥ 20)
#   3. Model factors (≥ 3 factor axes)
#   4. Positive edge (edge_percent ≥ 1.5)
#   5. Positive EV (ev_units > 0)
#   6. Sport-specific data (recent_form / xG / h2h / injury)
# Threshold: at least 3 of 6 must be present.

MIN_EVIDENCE_COUNT = 3
# ── LOW-EVIDENCE SOURCES (2026-07-22) ──────────────────────────────
# Sources listed here get a reduced evidence threshold (1 of 6 signals)
# because they're SELF-VALIDATING pipelines — the source itself IS
# strong evidence (ESPN MLS leaderboard = player has actual season
# scoring history; tennis_extra = ATP/WTA verified match). Without
# this, MLS ESPN picks were dropped en masse by evidence_threshold
# because `pick_rationale` gets rebuilt by pick_enrichment and loses
# our custom `matchup` field.
_LOW_EVIDENCE_SOURCES = {
    "tennis_extra",
    "tennis_extra_model",
    "mls_espn_leaderboard",
    "csl_espn_leaderboard",
}
MIN_EVIDENCE_COUNT_SCRAPE = 1
# Book-anchored scrape picks (TennisExplorer for Umag/Bastad/Gstaad/
# Athens/Iasi/Kitzbuhel etc.) don't have bucket_n / recent_form / EV
# columns — the scrape itself IS the evidence. Use a lower threshold
# for them so the ATP/WTA 250 slate surfaces (2026-07-12 user report).
# NB: this second binding is the ACTIVE one used by evidence_threshold
# (Python late-binding — last assignment wins). Keep this in sync with
# the primary definition above.
_LOW_EVIDENCE_SOURCES = {
    "tennis_extra",
    "tennis_extra_model",
    "mls_espn_leaderboard",
    "csl_espn_leaderboard",
}
MIN_EVIDENCE_COUNT_SCRAPE = 1


def evidence_threshold(picks: list[dict]) -> tuple[list[dict], dict]:
    stats = {"scanned": len(picks), "dropped": 0, "reasons": {}}
    survivors: list[dict] = []
    for p in picks:
        evidence = 0
        rationale = p.get("pick_rationale") or {}
        if rationale and isinstance(rationale, dict) and len(rationale) > 0:
            evidence += 1
        components = p.get("lock_components") or {}
        if components.get("bucket_n", 0) >= 20:
            evidence += 1
        factors = p.get("factors") or {}
        if isinstance(factors, dict) and len(factors) >= 3:
            evidence += 1
        if float(p.get("edge_percent") or 0) >= 1.5:
            evidence += 1
        if float(components.get("ev_units") or 0) > 0:
            evidence += 1
        # Sport-specific evidence
        if any(
            (isinstance(rationale, dict) and rationale.get(k)) for k in (
                "recent_form", "xg", "h2h", "vs_pitcher", "recent_l5",
                "recent_l10", "espn_rank", "matchup",
            )
        ):
            evidence += 1
        p["evidence_count"] = evidence
        # Determine per-pick threshold (scrape sources get a lower bar).
        src_lc = (p.get("source") or "").lower()
        threshold = (
            MIN_EVIDENCE_COUNT_SCRAPE if src_lc in _LOW_EVIDENCE_SOURCES
            else MIN_EVIDENCE_COUNT
        )
        # ── PHASE 1D — NFL Platinum authoritative-model evidence ─────
        # The gate previously only recognized rationale/bucket/factors/
        # edge/EV signals, so live Platinum game candidates (empty
        # factor dict by design) died at EVIDENCE_THRESHOLD despite a
        # full authoritative path.  Recognize TWO genuinely independent
        # categories — never multi-counting one simulation's derived
        # fields:
        #   1. exact-line causal simulation probability (the model)
        #   2. team-rating/expected-margin context (the model's INPUT
        #      evidence from real stored game results)
        _plat = p.get("platinum_game_sim") or {}
        if (p.get("model_source") == "platinum_nfl_game_sim"
                and _plat.get("sim_probability") is not None):
            evidence += 1
            if (_plat.get("expected_margin_home") is not None
                    or _plat.get("expected_total") is not None):
                evidence += 1
        # ── 2026-08-27 CFB SP+ authoritative-model evidence ──────────
        # Mirror of NFL Platinum: recognize the CFB SP+ game model's
        # independent probability + expected margin/total as two
        # genuinely-independent evidence categories.  The probability
        # is the model's exact-line output (ML / cover / O-U).  The
        # expected_margin / expected_total are model INPUT-side
        # evidence from real stored team ratings — different signal
        # class from the exact-line probability.  No math changes.
        _cfb_sim = p.get("cfb_game_sim") or {}
        if (p.get("model_source") == "cfb_sp_game_model"
                and _cfb_sim.get("sim_probability") is not None):
            evidence += 1
            if (_cfb_sim.get("expected_margin") is not None
                    or _cfb_sim.get("expected_total") is not None):
                evidence += 1

        if evidence < threshold:
            stats["dropped"] += 1
            r = f"only_{evidence}_of_{threshold}_signals"
            stats["reasons"][r] = stats["reasons"].get(r, 0) + 1
            # Phase 1C §8 — this was a SILENT drop (evidence drops were
            # excluded from the orchestrator's board-validator log line).
            # Every rejection must be funnel-attributable.
            try:
                from services import funnel_telemetry as _funnel
                _funnel.record(
                    sport=p.get("sport") or "unknown",
                    market=p.get("market") or "*",
                    stage="board_validator",
                    reason="EVIDENCE_THRESHOLD",
                    event=p.get("event"),
                    detail=r,
                )
            except Exception:
                pass
            continue
        survivors.append(p)
    return survivors, stats


# ─────────────────────── Top-level orchestrator ───────────────────────

def validate_and_finalize(picks: list[dict]) -> tuple[list[dict], dict]:
    """Full validation pipeline (§5 spec). Order matters —
    cheap deterministic checks first, evidence + snapshot last.

      1. contradictions           (§1)
      2. batter_pitcher           (§2)
      3. real_market_line         (§8 — Support 2026-06 durable fix:
                                        route model-only picks to
                                        Extended Coverage; annotate,
                                        never fabricate market data)
      4. integrity_check          (§10 — required fields, odds sanity, dedupe)
      5. board_quality            (§6)
      6. evidence_threshold       (§7 — min 3-of-6 independent signals)
      7. snapshot                 (§3 — lock immutable payload)
      8. rollover                 (§4 — pin to rollover board when qualifying)
    """
    report: dict = {"input_count": len(picks)}
    picks, r1 = remove_contradictions(picks)
    report["contradictions"] = r1
    picks, r2 = validate_batter_pitcher(picks)
    report["batter_pitcher"] = r2
    picks, r_real = enforce_real_market_line(picks)
    report["real_market_line"] = r_real
    picks, r3 = integrity_check(picks)
    report["integrity"] = r3
    picks, r4 = enforce_board_quality(picks)
    report["board_quality"] = r4
    picks, r5 = evidence_threshold(picks)
    report["evidence"] = r5
    picks, r6 = apply_immutable_snapshot(picks)
    report["snapshot"] = r6
    picks, r7 = tag_rollover_picks(picks)
    report["rollover"] = r7
    report["output_count"] = len(picks)
    return picks, report
