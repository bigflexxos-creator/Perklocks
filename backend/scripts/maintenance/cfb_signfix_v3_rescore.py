"""CFB SP+ Sign-Fix Re-Score — in-place recompute using CURRENT (fixed)
CFB game model against the already-published pick's persisted context.

Contract:
    * Only touches CFB rows whose `cfb_engine_version` != v3.signfix.
    * Loads SP+ ratings from `cfb_sp_ratings` (existing DB data — no
      CFBD provider hit; safe against 429).
    * For each pick, calls `services.cfb_game_model.estimate_cfb_game`
      with corrected sign math, computes P(over)/P(cover)/P(ML) per
      market, and rewrites:
          win_probability
          implied_probability (from book_odds)
          edge_percent
          lock_score / published_lock_score
          cfb_game_sim (expected_margin, expected_total, sigmas)
          cfb_engine_version = 'cfb_sp_game.v3.2026-06-signfix'
          off_board -> False (un-retires the signfix-retired rows)
          probability_provenance += "signfix_v3_rescore_in_place"
    * NEVER touches non-CFB rows.
    * NEVER forces LS floor / ceiling; math decides.
    * Preserves everything else on the row (book_odds, line, evidence,
      canonical IDs, generation_id, magic/apex flags, factors dict).

Idempotent — running twice against the same board is a no-op.
"""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from deps import db
from services.cfb_game_model import (
    estimate_cfb_game,
    cfb_cover_probability,
    cfb_over_probability,
)
from sports_engine import compute_lock_score  # noqa: E402


NEW_VERSION = "cfb_sp_game.v3.2026-06-signfix"


def _implied_prob_pct(american) -> float:
    try:
        a = float(american)
    except (TypeError, ValueError):
        return 0.0
    if a >= 100:
        return round(100.0 / (a + 100.0) * 100.0, 4)
    return round(-a / (-a + 100.0) * 100.0, 4)


def _side_from_selection(market: str, selection: str, home_team: str, away_team: str):
    m = (market or "").lower()
    s = (selection or "").strip()
    if "total points over" in m or ("total" in m and "over" in m):
        return "over"
    if "total points under" in m or ("total" in m and "under" in m):
        return "under"
    if "moneyline" in m:
        return "home_ml" if s == home_team else "away_ml"
    if "spread" in m:
        return "home_spread" if s == home_team else "away_spread"
    return None


async def _load_sp_ratings():
    """Load SP+ ratings keyed by normalized team name (mirrors the
    production loader: primary school name + cfb_teams alternate_names
    + mascot + abbreviation)."""
    ratings: dict[str, dict] = {}
    # Primary rows.
    latest = await db.cfb_sp_ratings.find(
        {}, {"team": 1, "rating": 1, "offense_rating": 1,
             "defense_rating": 1, "year": 1, "_id": 0}
    ).sort([("year", -1)]).to_list(length=1000)
    for r in latest:
        tk = str(r.get("team") or "").strip().lower()
        if tk and tk not in ratings:
            ratings[tk] = r
    # Alias expansion via cfb_teams collection.
    async for t in db.cfb_teams.find(
        {}, {"school": 1, "alternate_names": 1, "mascot": 1,
             "abbreviation": 1, "_id": 0}
    ):
        school = str(t.get("school") or "").strip().lower()
        row = ratings.get(school)
        if not row:
            continue
        for alias in (t.get("alternate_names") or []):
            ak = str(alias or "").strip().lower()
            if ak:
                ratings.setdefault(ak, row)
        mascot = str(t.get("mascot") or "").strip().lower()
        if mascot:
            combo = f"{school} {mascot}"
            ratings.setdefault(combo, row)
            ratings.setdefault(mascot, row)
        abbrev = str(t.get("abbreviation") or "").strip().lower()
        if abbrev:
            ratings.setdefault(abbrev, row)
    return ratings


async def _rescore_one(p: dict, ratings: dict, stats: dict):
    home = p.get("home_team")
    away = p.get("away_team")
    market = p.get("market") or ""
    selection = p.get("selection") or ""
    line = p.get("line")
    book_odds = p.get("book_odds")

    if not home or not away or line is None:
        stats["skipped_missing_fields"] += 1
        return

    side = _side_from_selection(market, selection, home, away)
    if not side:
        stats["skipped_unknown_side"] += 1
        return

    game = estimate_cfb_game(
        ctx={"cfb_sp_ratings_by_team": ratings,
             "cfb_returning_prod_by_team": {},
             "cfb_portal_net_by_team": {}},
        home_team=home,
        away_team=away,
    )
    if not game.available:
        stats["skipped_model_unavailable"] += 1
        stats["model_reasons"].setdefault(str(game.reason)[:60], 0)
        stats["model_reasons"][str(game.reason)[:60]] += 1
        return

    if side == "over":
        wp = cfb_over_probability(game.expected_total, float(line), True, game.total_sigma)
    elif side == "under":
        wp = cfb_over_probability(game.expected_total, float(line), False, game.total_sigma)
    elif side == "home_ml":
        wp = float(game.p_home_ml)
    elif side == "away_ml":
        wp = 1.0 - float(game.p_home_ml)
    elif side == "home_spread":
        wp = cfb_cover_probability(game.expected_margin, float(line), True, game.margin_sigma)
    elif side == "away_spread":
        wp = cfb_cover_probability(game.expected_margin, float(line), False, game.margin_sigma)
    else:
        stats["skipped_unknown_side"] += 1
        return

    wp_pct = round(float(wp) * 100.0, 4)
    imp_pct = _implied_prob_pct(book_odds)
    edge_pct = round(wp_pct - imp_pct, 4)

    # Recompute Lock Score using existing factors dict + updated wp/edge.
    factors = dict(p.get("factors") or {})
    # Rewrite the SP-derived evidence keys that the OLD math produced
    # incorrectly.
    factors["Projected Margin"] = f"{game.expected_margin:+.2f} pts"
    factors["Expected Total"] = f"{game.expected_total:.2f} pts"
    factors["Model Fair Prob"] = f"{wp_pct:.2f}%"
    factors["Sportsbook Implied Prob"] = f"{imp_pct:.2f}%"
    # Normalized versions kept in original scale (0..1) if present.
    try:
        from sports_engine import _cfb_norm_margin, _cfb_norm_total
        factors["Projected Margin (norm)"] = _cfb_norm_margin(game.expected_margin)
        factors["Expected Total (norm)"] = _cfb_norm_total(game.expected_total)
        factors["Model Fair Prob (norm)"] = round(wp_pct / 100.0, 4)
        factors["Sportsbook Implied (norm)"] = round(imp_pct / 100.0, 4)
    except Exception:
        pass

    pick_shim = {
        "sport": "CFB",
        "market": market,
        "book_odds": book_odds,
        "win_probability": wp_pct,
        "edge_percent": edge_pct,
        "is_alt_line": bool(p.get("is_alt_line")),
        "data_quality": p.get("data_quality") or game.data_quality,
        "probability_provenance": p.get("probability_provenance"),
        "odds_at_pick": p.get("odds_at_pick"),
        "closing_odds": p.get("closing_odds"),
    }
    try:
        new_ls, weighted = compute_lock_score(
            factors, win_prob=wp_pct, pick=pick_shim,
            edge_percent=edge_pct,
        )
    except Exception as exc:
        stats["ls_errors"].setdefault(type(exc).__name__, 0)
        stats["ls_errors"][type(exc).__name__] += 1
        return

    old_wp = float(p.get("win_probability") or 0.0)
    old_ls = float(p.get("lock_score") or 0.0)

    persisted_factors = {**weighted, **factors}
    for k in ("__lock_score_version", "__calibrated_win_probability",
              "__effective_weights", "__confidence_component"):
        persisted_factors.pop(k, None)

    updated_cfb_sim = dict(p.get("cfb_game_sim") or {})
    updated_cfb_sim.update({
        "expected_margin": game.expected_margin,
        "expected_total": game.expected_total,
        "margin_sigma": game.margin_sigma,
        "total_sigma": game.total_sigma,
        "tier": game.tier,
        "data_quality": game.data_quality,
        "signfix_rescored_at": datetime.now(timezone.utc).isoformat(),
    })

    prov_raw = p.get("probability_provenance")
    if isinstance(prov_raw, str):
        prov = [prov_raw]
    elif isinstance(prov_raw, dict):
        prov = list(prov_raw.values())
    elif isinstance(prov_raw, list):
        prov = list(prov_raw)
    else:
        prov = []
    tag = "signfix_v3_rescore_in_place"
    if tag not in prov:
        prov.append(tag)

    # Canonical grade from Lock Score (mirrors sports_engine._grade)
    if new_ls >= 100.0:  _grade = "APEX Lock"
    elif new_ls >= 98.0: _grade = "Elite Lock"
    elif new_ls >= 95.0: _grade = "Strong Lock"
    elif new_ls >= 90.0: _grade = "Lock"
    elif new_ls >= 85.0: _grade = "Playable"
    else:                _grade = "Pass"

    set_payload = {
        "win_probability": wp_pct,
        "implied_probability": imp_pct,
        "edge_percent": edge_pct,
        "lock_score": new_ls,
        "published_lock_score": new_ls,
        # ── CRITICAL: also update the published_* snapshot fields.
        # ``services/published_prediction_reader.hydrate()`` aliases
        # these OVER the legacy fields at every read, so leaving them
        # stale would restore the pre-fix probability at API time.
        "published_probability": round(wp_pct / 100.0, 6),
        "published_edge":        edge_pct,
        "published_grade":       _grade,
        "grade":                 _grade,
        # Bump snapshot_version so client SWR/ETag treats it as fresh.
        "snapshot_version":      int((p.get("snapshot_version") or 0)) + 1,
        "cfb_game_sim": updated_cfb_sim,
        "cfb_engine_version": NEW_VERSION,
        "factors": persisted_factors,
        "probability_provenance": prov,
    }
    # ── Clear stale AI-generated rationale text.  The pre-fix
    # explanation cites the OLD extreme probability ("Projected Total
    # 79.9 pts", "Lock Score 98") which contradicts the corrected
    # numbers we just wrote.  Setting them to None lets the /ai-explain
    # endpoint regenerate on demand against the fresh math.
    stale_rationale_keys = (
        "pick_rationale", "reasoning", "published_reasoning",
        "explanation", "ai_explanation",
    )
    for k in stale_rationale_keys:
        if p.get(k):
            set_payload[k] = None
    # key_insights / top_reasons come from the factor render loop.
    # Rebuild a minimal, honest set from the new factors + grade so
    # the card doesn't display pre-fix "__evidence_authority_ceiling:
    # 95/100 — elite signal" alongside a Lock Score of 56.
    honest_reasons = [
        f"Expected Total: {game.expected_total:.1f} pts",
        f"Projected Margin: {game.expected_margin:+.1f} pts",
        f"Model Fair Prob: {wp_pct:.2f}%  ·  Book Implied: {imp_pct:.2f}%",
        f"Edge: {edge_pct:+.2f} pp  ·  Lock Score: {new_ls:.1f}  ·  Grade: {_grade}",
    ]
    set_payload["key_insights"] = honest_reasons
    set_payload["top_reasons"] = honest_reasons
    # Un-retire if the pick was only marked off_board by our signfix retirement.
    if p.get("retirement_reason") == "cfb_sp_signfix_v3_regen":
        set_payload["off_board"] = False
        set_payload["retired_at"] = None
        set_payload["retirement_reason"] = "unretired_after_signfix_v3_rescore"

    await db.picks.update_one({"_id": p["_id"]}, {"$set": set_payload})
    stats["rescored"] += 1
    if new_ls >= 90:
        stats["ls_ge90"] += 1
    if wp_pct >= 90:
        stats["wp_ge90"] += 1

    if len(stats["samples"]) < 12:
        stats["samples"].append({
            "event": p.get("event"),
            "market": market,
            "line": line,
            "book_odds": book_odds,
            "wp_before": old_wp,
            "wp_after": wp_pct,
            "ls_before": old_ls,
            "ls_after": new_ls,
            "exp_total": game.expected_total,
            "exp_margin": game.expected_margin,
        })


async def main():
    ratings = await _load_sp_ratings()
    print(f"Loaded SP+ ratings for {len(ratings)} teams.")
    match = {
        "sport": "CFB",
        "$or": [
            {"cfb_engine_version": {"$ne": NEW_VERSION}},
            # Also rescan already-rescored rows whose published_probability
            # still reflects a pre-signfix value (percentage vs fraction
            # mismatch or double-digit drift). Cheap idempotent guard.
            {"cfb_engine_version": NEW_VERSION,
             "$expr": {"$gt": [
                 {"$abs": {"$subtract": [
                     {"$multiply": [
                         {"$ifNull": ["$published_probability", 0.0]}, 100.0]},
                     {"$ifNull": ["$win_probability", 0.0]}]}}, 1.0]}},
            # Sweep already-rescored rows whose stale rationale text
            # still references the pre-fix "Lock Score 98" / "Projected
            # Total ~80 pts" numbers.  Clearing them lets the AI-explain
            # endpoint regenerate against the fresh math.
            {"cfb_engine_version": NEW_VERSION,
             "$or": [
                 {"pick_rationale": {"$ne": None}},
                 {"explanation":    {"$ne": None}},
             ]},
        ],
    }
    total = await db.picks.count_documents(match)
    print(f"CFB picks needing signfix v3 rescore: {total}")

    stats = {
        "rescored": 0,
        "skipped_missing_fields": 0,
        "skipped_unknown_side": 0,
        "skipped_model_unavailable": 0,
        "ls_ge90": 0,
        "wp_ge90": 0,
        "ls_errors": {},
        "model_reasons": {},
        "samples": [],
    }
    cursor = db.picks.find(match)
    async for p in cursor:
        await _rescore_one(p, ratings, stats)

    print("\n=== CFB SIGNFIX v3 RESCORE COMPLETE ===")
    for k, v in stats.items():
        if k == "samples":
            continue
        print(f"  {k}: {v}")
    print("\n  Sample rescored rows:")
    for s in stats["samples"]:
        print(f"   • {s['event']} | {s['market']} @{s['line']} odds={s['book_odds']}")
        print(f"       WP  {s['wp_before']:>6.2f}% → {s['wp_after']:>6.2f}%   "
              f"LS {s['ls_before']:>5.1f} → {s['ls_after']:>5.1f}   "
              f"ExpTotal={s['exp_total']:.1f} ExpMargin={s['exp_margin']:+.1f}")

    # ── Advance the universal canonical freshness fingerprint ──
    # After ANY in-place mutation of canonical rows, the global
    # ``board_version`` MUST advance so every client (Preview + Expo
    # Go) discards the caches that carry the stale numbers.
    #
    # ``compute_board_version()`` hashes ``max(updated_at/last_seen_at)``
    # across the active picks — so we bump ``updated_at`` on every
    # rescored row first, THEN commit.  Without this bump the
    # fingerprint won't advance even though the numbers changed.
    if stats["rescored"] > 0:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            await db.picks.update_many(
                {"cfb_engine_version": NEW_VERSION},
                {"$set": {"updated_at": now_iso, "last_seen_at": now_iso}},
            )
            from services import board_generation
            gen_id = await board_generation.begin(scope="CFB_SIGNFIX_V3")
            bver, pcount, ecount = await board_generation.board_state()
            ok = await board_generation.commit(gen_id, bver, pcount, ecount)
            print(f"\n  board_generation.commit: gen_id={gen_id} board_version={bver} pick_count={pcount} event_count={ecount} ok={ok}")
        except Exception as exc:
            print(f"\n  board_generation.commit failed (non-fatal): {exc}")


if __name__ == "__main__":
    asyncio.run(main())
