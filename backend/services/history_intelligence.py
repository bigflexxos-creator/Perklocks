"""History Intelligence — Shadow-mode research service (Session G).

READ-ONLY. Computes recency-weighted historical distributions and
H2H shrinkage estimates PER PICK. Output goes to
`pick_enrichment.history_shadow` only. NEVER read by Lock Score,
Magic, APEX, board eligibility, model probability, Parlay, or
Rollover.

Design rules:
  • Recency-weighted with exponential decay (half-life 90 days).
  • H2H shrinkage: (n * raw + k * baseline) / (n + k), k=10.
    Small H2H samples regress strongly toward the player's
    broader (opponent-agnostic) baseline.
  • Zero H2H meetings = UNKNOWN, never negative evidence.
  • Old observations receive strictly less influence than recent ones.
  • As-of-pick temporal filter: history rows with
    event_time >= cutoff are excluded (no future leakage).
  • Data quality gauge derived from career_n / recent_n / h2h_n.

Shadow output schema (matches the user's specified fields):
    history_shadow = {
      "history_version": "hi.v1",
      "generated_at": ISO8601 Z,
      "sport": str, "market_family": str,
      "projection": float | None,      # recency-weighted mean
      "probability": float | None,     # P(actual >/< line) empirical
      "reliability": "high"|"medium"|"low"|"insufficient",
      "career_n": int, "recent_n": int, "h2h_n": int,
      "h2h_raw": float | None,
      "h2h_shrunk": float | None,
      "mean": float | None, "median": float | None,
      "q25": float | None, "q75": float | None,
      "variance": float | None, "stdev": float | None,
      "current_line_hit_rate": float | None,
      "home_away_split": {...} | None,
      "context": {...},
      "data_quality": str,
    }

Version bumps: increment HISTORY_VERSION when features change so
we can compare across runs without overwriting newer with older.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from statistics import mean, median, variance, quantiles
from typing import Any, Optional


HISTORY_VERSION = "hi.v1"
RECENCY_HALF_LIFE_DAYS = 90.0
H2H_SHRINKAGE_K = 10.0  # sample size where shrunk ≈ 0.5*raw+0.5*baseline
RECENT_L = 20  # L20 window (rows) — recent_n uses this
CAREER_CAP = 500  # cap career sample for perf

# Sport → market_family → actuals-field key (from player_game_actuals.actuals)
_STAT_MAP: dict[str, dict[str, str]] = {
    "mlb": {
        "hits": "h", "total bases": "tb", "home runs": "hr",
        "rbis": "rbi", "runs": "r",
        "strikeouts": "strikeouts", "k": "k", "outs": "outs",
        "singles": "singles", "doubles": "doubles",
        "triples": "triples", "hits+runs+rbis": "hrb",
    },
    "nfl": {
        "passing yards": "pass_yds", "passing tds": "pass_tds",
        "interceptions": "interceptions",
        "completions": "completions", "attempts": "attempts",
        "rushing yards": "rush_yds", "rushing attempts": "rush_attempts",
        "rushing tds": "rush_tds",
        "receiving yards": "rec_yds", "receptions": "receptions",
        "receiving tds": "rec_tds", "targets": "targets",
    },
    "nba": {
        "points": "points", "rebounds": "rebounds",
        "assists": "assists", "threes made": "threes_made",
        "steals": "steals", "blocks": "blocks",
        "turnovers": "turnovers",
        "pts+reb": "prb", "pts+ast": "pra",
        "reb+ast": "rebast", "pts+reb+ast": "pra_all",
    },
    "tennis": {
        "aces": "aces", "double faults": "double_faults",
        "games won": "games_won", "sets won": "sets_won",
    },
    "soccer": {
        "goals": "goals", "assists": "assists",
        "shots": "shots", "shots on target": "shots_on_target",
        "goals + assists": "goals_plus_assists",
    },
}


def _market_family(market: str, sport: str) -> tuple[str, Optional[str]]:
    """Return (family_label, actuals_key) — actuals_key may be None
    when market is a team market (Moneyline / Spread / Total)."""
    m = (market or "").lower()
    for label, key in _STAT_MAP.get(sport.lower(), {}).items():
        if label in m:
            return label, key
    if "moneyline" in m: return "moneyline", None
    if "spread"    in m: return "spread", None
    if "total"     in m or "over/under" in m: return "total", None
    return "unknown", None


def _get_stat(actuals: dict, key: str) -> Optional[float]:
    if not actuals or not key:
        return None
    v = actuals.get(key)
    if v is None:
        # goals_plus_assists synth fallback for soccer
        if key == "goals_plus_assists":
            g, a = actuals.get("goals"), actuals.get("assists")
            if g is None or a is None: return None
            try:
                return float(g) + float(a)
            except (TypeError, ValueError):
                return None
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _decay_weight(event_iso: Optional[str], now: datetime) -> float:
    if not event_iso:
        return 0.5
    try:
        dt = datetime.fromisoformat(event_iso.replace("Z", "+00:00"))
    except Exception:
        return 0.5
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    return 0.5 ** (days / RECENCY_HALF_LIFE_DAYS)


def _cutoff_from_pick(pick: dict) -> str:
    """ISO cutoff = event_time (preferred) or published_at or now."""
    for k in ("event_time", "published_at", "created_at"):
        v = pick.get(k)
        if v: return str(v)
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hit(value: float, line: float, side: str) -> bool:
    s = (side or "").lower()
    if "over" in s or "yes" in s:  return value >  line
    if "under" in s or "no" in s:  return value <  line
    return value >= line


async def compute_history_shadow(db, pick: dict) -> dict:
    """Compute the shadow-mode history bundle for one pick.
    NEVER modifies the pick or writes to any prediction/enrichment
    field. Returns the bundle; caller writes it to pick_enrichment.
    """
    sport_raw = pick.get("sport") or ""
    sport = sport_raw.lower()
    market = pick.get("market") or ""
    family, stat_key = _market_family(market, sport)
    cpid = pick.get("canonical_player_id")
    copp = pick.get("canonical_opponent_id")
    line = pick.get("line")
    side = pick.get("side") or ""
    cutoff = _cutoff_from_pick(pick)

    bundle: dict[str, Any] = {
        "history_version": HISTORY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat()
            .replace("+00:00", "Z"),
        "sport": sport_raw, "market_family": family,
        "projection": None, "probability": None,
        "reliability": "insufficient",
        "career_n": 0, "recent_n": 0, "h2h_n": 0,
        "h2h_raw": None, "h2h_shrunk": None,
        "mean": None, "median": None,
        "q25": None, "q75": None,
        "variance": None, "stdev": None,
        "current_line_hit_rate": None,
        "home_away_split": None,
        "context": {"cpid": cpid, "opponent": copp,
                    "line": line, "side": side,
                    "cutoff": cutoff, "stat_key": stat_key},
        "data_quality": "unknown",
    }

    # Team markets: return structural bundle with UNKNOWN (no player stat)
    if not stat_key:
        bundle["data_quality"] = "team_market_unsupported"
        return bundle
    if not cpid:
        bundle["data_quality"] = "no_canonical_player"
        return bundle

    # Query career (up to CAREER_CAP most recent pregame rows)
    q_career = {"sport": sport, "canonical_player_id": cpid,
                "event_time": {"$lt": cutoff}}
    rows = []
    try:
        cursor = db.player_game_actuals.find(q_career, {
            "_id": 0, "event_time": 1, "actuals": 1,
            "canonical_opponent_id": 1, "home_away": 1,
            "surface": 1, "event_id": 1,
        }).sort([("event_time", -1)]).limit(CAREER_CAP)
        async for r in cursor:
            v = _get_stat(r.get("actuals") or {}, stat_key)
            if v is not None:
                rows.append({"v": v, "t": r.get("event_time"),
                             "opp": r.get("canonical_opponent_id"),
                             "ha": r.get("home_away"),
                             "surface": r.get("surface"),
                             "event_id": r.get("event_id")})

        # MLB fallback: pga rows often have event_time=None.  Pull
        # rows with no event_time filter, then attach event_time from
        # team_game_actuals (which does carry it) — but strictly
        # enforce cutoff (event_time < cutoff) using the joined time.
        if not rows and sport == "mlb":
            cursor2 = db.player_game_actuals.find(
                {"sport": sport, "canonical_player_id": cpid},
                {"_id": 0, "event_id": 1, "actuals": 1,
                 "canonical_opponent_id": 1, "home_away": 1,
                 "event_time": 1}
            ).limit(CAREER_CAP * 2)
            raw = []
            async for r in cursor2:
                v = _get_stat(r.get("actuals") or {}, stat_key)
                if v is not None:
                    raw.append({"v": v, "t": r.get("event_time"),
                                "opp": r.get("canonical_opponent_id"),
                                "ha": r.get("home_away"),
                                "event_id": r.get("event_id")})
            # Resolve event_time from tga per event_id
            eids = [str(r["event_id"]) for r in raw
                    if r.get("t") is None and r.get("event_id")]
            if eids:
                tmap = {}
                async for t in db.team_game_actuals.find(
                        {"sport": "mlb", "event_id": {"$in": eids}},
                        {"event_id": 1, "event_time": 1, "_id": 0}):
                    if t.get("event_time"):
                        tmap[str(t["event_id"])] = t["event_time"]
                for r in raw:
                    if r["t"] is None and r["event_id"]:
                        r["t"] = tmap.get(str(r["event_id"]))
            # Enforce cutoff
            for r in raw:
                if r.get("t") and r["t"] < cutoff:
                    rows.append(r)
            rows.sort(key=lambda r: r.get("t") or "", reverse=True)
            rows = rows[:CAREER_CAP]
    except Exception as e:
        bundle["data_quality"] = f"query_error:{type(e).__name__}"
        return bundle

    if not rows:
        bundle["data_quality"] = "no_history"
        return bundle

    now = datetime.now(timezone.utc)
    values = [r["v"] for r in rows]
    bundle["career_n"] = len(rows)
    bundle["mean"] = round(mean(values), 4)
    bundle["median"] = round(median(values), 4)
    try:
        q = quantiles(values, n=4)
        bundle["q25"] = round(q[0], 4)
        bundle["q75"] = round(q[2], 4)
    except Exception:
        pass
    if len(values) >= 2:
        v = variance(values)
        bundle["variance"] = round(v, 4)
        bundle["stdev"] = round(v ** 0.5, 4)

    # Recent L20 slice
    recent_rows = rows[:RECENT_L]
    bundle["recent_n"] = len(recent_rows)

    # Recency-weighted projection (career, exp decay)
    weighted_sum = 0.0
    weight_total = 0.0
    for r in rows:
        w = _decay_weight(r.get("t"), now)
        # Tennis surface bump: same surface → 1.5x, different → 0.75x
        if sport == "tennis" and pick.get("surface") and r.get("surface"):
            if str(pick.get("surface")).lower() == str(r.get("surface")).lower():
                w *= 1.5
            else:
                w *= 0.75
        weighted_sum += w * r["v"]
        weight_total += w
    if weight_total > 0:
        bundle["projection"] = round(weighted_sum / weight_total, 4)

    # Current-line hit rate (empirical on career values)
    if line is not None:
        try:
            L = float(line)
            hits = sum(1 for v in values if _hit(v, L, side))
            bundle["current_line_hit_rate"] = round(hits / len(values), 4)
            bundle["probability"] = round(hits / len(values), 4)
        except (TypeError, ValueError):
            pass

    # H2H (only if opponent known) — sample-size shrinkage
    if copp:
        h2h_rows = [r for r in rows if str(r.get("opp") or "").lower()
                    == str(copp).lower()]
        h2h_n = len(h2h_rows)
        bundle["h2h_n"] = h2h_n
        if h2h_n > 0:
            h2h_vals = [r["v"] for r in h2h_rows]
            h2h_raw = mean(h2h_vals)
            bundle["h2h_raw"] = round(h2h_raw, 4)
            baseline = bundle["projection"] or bundle["mean"] or h2h_raw
            shrunk = (h2h_n * h2h_raw
                      + H2H_SHRINKAGE_K * baseline) \
                     / (h2h_n + H2H_SHRINKAGE_K)
            bundle["h2h_shrunk"] = round(shrunk, 4)
        # else: h2h_n=0 → h2h_raw/shrunk stay None (UNKNOWN, not negative)

    # Home/away split
    home_vals = [r["v"] for r in rows if r.get("ha") == "home"]
    away_vals = [r["v"] for r in rows if r.get("ha") == "away"]
    if home_vals or away_vals:
        bundle["home_away_split"] = {
            "home_n": len(home_vals),
            "home_mean": round(mean(home_vals), 4) if home_vals else None,
            "away_n": len(away_vals),
            "away_mean": round(mean(away_vals), 4) if away_vals else None,
        }

    # Reliability tier
    career_n = bundle["career_n"]
    if career_n >= 30:   bundle["reliability"] = "high"
    elif career_n >= 12: bundle["reliability"] = "medium"
    elif career_n >= 5:  bundle["reliability"] = "low"
    else:                bundle["reliability"] = "insufficient"

    # Data quality label
    quality_bits = []
    if career_n >= 30: quality_bits.append("career_ok")
    if bundle["recent_n"] >= 5: quality_bits.append("recent_ok")
    if bundle["h2h_n"] >= 3: quality_bits.append("h2h_present")
    elif bundle["h2h_n"] == 0: quality_bits.append("h2h_unknown")
    else: quality_bits.append("h2h_tiny_shrunk")
    bundle["data_quality"] = "|".join(quality_bits) if quality_bits \
        else "unknown"

    return bundle


async def upsert_shadow(db, pick_id, bundle: dict) -> str:
    """Idempotent write to pick_enrichment.history_shadow. Skips
    overwrite when the stored bundle is same-or-newer version.
    Returns 'inserted' | 'updated' | 'skipped_newer'."""
    existing = await db.pick_enrichment.find_one(
        {"pick_id": pick_id},
        {"history_shadow.history_version": 1,
         "history_shadow.generated_at": 1})
    if existing and existing.get("history_shadow"):
        cur = existing["history_shadow"]
        if cur.get("history_version") == bundle.get("history_version"):
            # same version → skip if we already have one
            return "skipped_newer"
        # newer version wins; older won't overwrite newer versions
        try:
            if cur.get("history_version", "") > bundle.get("history_version"):
                return "skipped_newer"
        except Exception:
            pass
    await db.pick_enrichment.update_one(
        {"pick_id": pick_id},
        {"$set": {"history_shadow": bundle,
                  "history_shadow_updated_at":
                      datetime.now(timezone.utc).isoformat()
                          .replace("+00:00", "Z")},
         "$setOnInsert": {"pick_id": pick_id,
                          "created_at":
                              datetime.now(timezone.utc).isoformat()
                                  .replace("+00:00", "Z")}},
        upsert=True)
    return "inserted" if not existing else "updated"


async def backfill_settled_shadow(db, *, sport: str, limit: int = 5000,
                                  dry_run: bool = False) -> dict:
    """P6 support: bounded backfill of settled player-line picks with a
    stamped history_shadow, computed as-of the pick's pregame cutoff
    (no future leakage). Chronologically ordered — oldest first — so
    we never use future games to explain older picks."""
    from pymongo import ASCENDING
    stats = {"scanned": 0, "computed": 0, "inserted": 0, "updated": 0,
             "skipped_newer": 0, "skipped_ineligible": 0}
    q = {"sport": sport,
         "settled_at": {"$exists": True},
         "canonical_player_id": {"$exists": True, "$ne": None},
         "line": {"$exists": True, "$ne": None}}
    cursor = db.picks.find(q).sort([("event_time", ASCENDING)]).limit(limit)
    async for pick in cursor:
        stats["scanned"] += 1
        if not pick.get("event_time"):
            stats["skipped_ineligible"] += 1
            continue
        bundle = await compute_history_shadow(db, pick)
        stats["computed"] += 1
        if dry_run:
            continue
        pid = str(pick.get("_id"))
        r = await upsert_shadow(db, pid, bundle)
        if   r == "inserted": stats["inserted"] += 1
        elif r == "updated":  stats["updated"] += 1
        else: stats["skipped_newer"] += 1
    return stats


__all__ = ["compute_history_shadow", "upsert_shadow",
           "backfill_settled_shadow", "HISTORY_VERSION"]
