"""pick_refresh_orchestrator — Phase 3F-1 extraction.

Owns the pick-refresh orchestration formerly implemented inline in
``server._refresh_picks``.  Behaviour-preserving move — every stage,
every helper, every log line is identical to the pre-extraction
implementation.  What CHANGED is where the code lives and the typed
request/result contract at the public boundary.

Design constraints (Phase 3F-1 contract)
────────────────────────────────────────
* Does NOT import ``server``.  All shared collaborators come from
  ``deps`` (shared Mongo owner via Phase 3B), ``sports_engine``,
  ``services.prediction_publication_service``, etc.
* Does NOT introduce a second lease or budget layer.  The caller
  (scheduler / admin route / manual context) is expected to own its
  own ``JobCoordinator`` / ``ProviderBudget`` reservation.
* Public entry point: :class:`PickRefreshOrchestrator` + its
  ``refresh(request) -> result`` coroutine.
* Legacy ``server._refresh_picks(date_str, sport_filter=None) -> int``
  becomes a compatibility wrapper that instantiates the orchestrator,
  runs it, and returns the integer written-count.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

# Shared services.  We deliberately import ``logger`` from ``deps`` so
# we never trigger a circular import of the FastAPI app.  For ``db``
# we use a lazy proxy so that any test which overrides
# ``server.db = <fresh_client_db>`` (a pattern used by
# test_iter83/85/88 to inject a per-test motor client) is respected
# by every helper moved here — the pre-3F-1 helpers lived in
# server.py so ``server.db`` WAS the source of truth for them.
from deps import logger


class _DBProxy:
    """Late-binding db handle.

    Every attribute access resolves through the current process's
    shared owner (Phase 3B) *unless* ``server.db`` has been poked by
    a test to a different handle, in which case that handle wins.
    This preserves the exact semantics of the pre-3F-1 helpers that
    used ``db`` directly from ``server`` module scope.
    """

    __slots__ = ()

    @staticmethod
    def _resolve():
        # Prefer server.db so legacy tests that inject a per-loop
        # client via ``server.db = <fresh>`` still work.
        try:
            import server as _srv  # lazy — server imports us, so it's already loaded when we're called
            _override = _srv.__dict__.get("db")
            if _override is not None:
                return _override
        except Exception:
            pass
        from services.database import get_database
        return get_database()

    def __getattr__(self, name):
        return getattr(_DBProxy._resolve(), name)

    def __getitem__(self, name):
        return _DBProxy._resolve()[name]

    def __repr__(self):
        return f"<_DBProxy → {_DBProxy._resolve()!r}>"


db = _DBProxy()  # module-level attribute, same NAME as the old server.db

# The generation entry point stays in ``sports_engine`` — the
# orchestrator only coordinates.
from sports_engine import generate_all_picks


# ═════════════════════════════════════════════════════════════════════
# Typed request / result contract
# ═════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class PickRefreshRequest:
    """Public entry-point contract for a refresh."""
    slate_date:          str
    sport_filter:        Optional[str] = None
    caller:              str           = "unknown"
    reason:              str           = ""
    force:               bool          = False
    board_version_hint:  Optional[str] = None
    job_name:            Optional[str] = None
    metadata:            dict[str, Any] = field(default_factory=dict)


@dataclass
class PickRefreshResult:
    """Structured result — safe to log, no secrets or provider
    payloads inside.  Caller may treat it as a plain dict."""
    success:             bool           = False
    slate_date:          str            = ""
    sport_filter:        Optional[str]  = None
    caller:              str            = "unknown"
    reason:              str            = ""
    generated_count:     int            = 0
    validated_count:     int            = 0
    published_count:     int            = 0
    rejected_count:      int            = 0
    snapshot_count:      int            = 0
    duration_ms:         int            = 0
    errors:              list[str]      = field(default_factory=list)
    warnings:            list[str]      = field(default_factory=list)
    board_version:       Optional[str]  = None
    publication_source:  str            = "canonical_pipeline"

    def as_dict(self) -> dict[str, Any]:
        return {
            "success":             self.success,
            "slate_date":          self.slate_date,
            "sport_filter":        self.sport_filter,
            "caller":              self.caller,
            "reason":              self.reason,
            "generated_count":     self.generated_count,
            "validated_count":     self.validated_count,
            "published_count":     self.published_count,
            "rejected_count":      self.rejected_count,
            "snapshot_count":      self.snapshot_count,
            "duration_ms":         self.duration_ms,
            "errors":              list(self.errors),
            "warnings":            list(self.warnings),
            "board_version":       self.board_version,
            "publication_source":  self.publication_source,
        }


# ── Moved from server.py lines 1131-1305 (Phase 3F-1) ──
def _dedupe_and_limit_goalscorers(picks: list[dict]) -> list[dict]:
    """Dedupe duplicate goalscorer picks and trim each event's slate.

    Rule (per user 2026-06-22): "Top 3 goalscorers per match — unless more
    are elite (≥70% win prob AND positive edge)."

    Steps:
      1) DEDUP: For each (event, player, market_family) combo where
         market_family is one of:
            - ATGS  (Anytime Goal Scorer + synthetic AGS from To-Score-or-Assist)
            - FGS   (First Goal Scorer)
            - SoA   (To Score or Assist)
         keep only the single best pick (highest lock_score, ties broken by
         best edge_percent). This kills the "same player 3-4× at different
         book prices / synth duplicates" problem.
      2) TRIM: Within each (event, market_family) group, sort by
         win_probability DESC. Keep top 3 by default. Append any extras
         that pass the elite override (win_probability ≥ 70% AND
         edge_percent > 0). This bounds the goalscorer slate on marquee
         games (Ghana @ England had 47 picks; expected ≤ ~5).
    """
    if not picks:
        return picks
    import re as _re

    def _family(market: str) -> str:
        ml = (market or "").lower()
        if "first goal scorer" in ml: return "FGS"
        if "anytime goal scorer" in ml: return "ATGS"
        if "to score or assist" in ml: return "SoA"
        return ""

    # Extract a stable player name from market labels like "Harry Kane
    # Anytime Goal Scorer" / "Bukayo Saka First Goal Scorer" / "Ollie
    # Watkins To Score or Assist".
    _SUFFIXES = (
        " Anytime Goal Scorer",
        " First Goal Scorer",
        " To Score or Assist",
    )
    def _player_from_market(market: str) -> str:
        m = market or ""
        for suf in _SUFFIXES:
            if m.endswith(suf):
                return m[: -len(suf)].strip().lower()
        # Fallback: strip the family suffix even if mid-string
        return _re.sub(
            r"\s*(anytime goal scorer|first goal scorer|to score or assist).*$",
            "",
            m, flags=_re.I,
        ).strip().lower()

    # Phase 1: dedup
    by_key: dict[tuple, dict] = {}
    rest: list[dict] = []
    for p in picks:
        fam = _family(p.get("market") or "")
        if fam == "" or (p.get("sport") or "") != "Soccer":
            rest.append(p)
            continue
        player = _player_from_market(p.get("market") or "")
        key = (p.get("event") or "", player, fam)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = p
            continue
        # Higher lock_score wins; ties → higher edge_percent
        def _score(q: dict):
            try: lock = float(q.get("lock_score") or 0)
            except Exception: lock = 0.0
            try: edge = float(q.get("edge_percent") or 0)
            except Exception: edge = 0.0
            return (lock, edge)
        if _score(p) > _score(existing):
            by_key[key] = p

    # Phase 2: trim per (event, family) — Top 3 + elite-override
    by_event_family: dict[tuple, list[dict]] = {}
    for (event, _player, fam), p in by_key.items():
        by_event_family.setdefault((event, fam), []).append(p)

    kept: list[dict] = []
    trimmed = 0
    for (event, fam), group in by_event_family.items():
        # Sort by win_probability DESC; ties broken by lock_score DESC.
        def _sortkey(q: dict):
            try: wp = float(q.get("win_probability") or 0)
            except Exception: wp = 0.0
            try: ls = float(q.get("lock_score") or 0)
            except Exception: ls = 0.0
            return (-wp, -ls)
        group.sort(key=_sortkey)
        top3 = group[:3]
        extras: list[dict] = []
        for q in group[3:]:
            try:
                wp = float(q.get("win_probability") or 0)
                eg = float(q.get("edge_percent") or 0)
            except Exception:
                wp, eg = 0.0, 0.0
            # Elite override:
            #   1) win prob ≥ 70% AND positive edge (book-favorite tier)
            #   2) elite_protect=True (curated CSL elite seed —
            #      Cryzan, Felipe Sousa, Fábio Abreu, Leonardo, Wu Lei,
            #      Júnior Negrão, Cédric Bakambu, Wesley Moraes, etc.)
            #      Synthetic picks have edge=0 so the 70%+edge clause
            #      can never catch them — without this elite_protect
            #      clause they get silently trimmed at top-3 cap.
            if (wp >= 70.0 and eg > 0) or q.get("elite_protect"):
                extras.append(q)
            else:
                trimmed += 1
        kept.extend(top3 + extras)
    logger.info(
        "Goalscorer dedup+trim: %d unique players × markets, %d trimmed",
        len(by_key), trimmed,
    )
    return rest + kept


def _cap_tennis_totals(picks: list[dict], max_per_side: int = 2) -> list[dict]:
    """Cap Tennis alternate-line Total Games to top-N per (match, side).

    User report 2026-06-22: "Why I got so many tennis overs instead of
    moneyline?" The Odds API exposes 5-7 alt-line Total Games markets per
    match (Over/Under 18.5, 19.5, 20.5, ...). Each survives the lock-floor
    independently, flooding the slate while the lone Moneyline market —
    which the bandit just told us is our HOTTEST tennis arm at +13% ROI /
    Sharpe +1.11 — gets buried.

    Fix: per (match, Over|Under), keep only the TOP-N alt-lines by
    win_probability. Default 2 keeps the most informative lines without
    drowning the matchup. Game-level Tennis Moneyline / Spread untouched.
    """
    if not picks:
        return picks
    import re as _re

    def _is_tennis_total(p: dict) -> bool:
        if (p.get("sport") or "") != "Tennis":
            return False
        m = (p.get("market") or "").lower()
        return ("over " in m or "under " in m) and "games" in m

    def _side(p: dict) -> str:
        m = (p.get("market") or "").lower()
        return "over" if "over " in m else "under"

    by_key: dict[tuple, list[dict]] = {}
    rest: list[dict] = []
    for p in picks:
        if _is_tennis_total(p):
            key = (p.get("event") or "", _side(p))
            by_key.setdefault(key, []).append(p)
        else:
            rest.append(p)

    kept: list[dict] = []
    trimmed = 0
    for _key, group in by_key.items():
        # Sort by win_probability DESC; ties broken by lock_score DESC.
        def _sortkey(q: dict):
            try: wp = float(q.get("win_probability") or 0)
            except Exception: wp = 0.0
            try: ls = float(q.get("lock_score") or 0)
            except Exception: ls = 0.0
            return (-wp, -ls)
        group.sort(key=_sortkey)
        kept.extend(group[:max_per_side])
        trimmed += max(0, len(group) - max_per_side)
    logger.info(
        "Tennis Totals cap: %d (match, side) groups kept top-%d each, %d trimmed",
        len(by_key), max_per_side, trimmed,
    )
    return rest + kept


# ── Moved from server.py lines 1308-2365 (Phase 3F-1) ──
async def _refresh_picks(date_str: str, sport_filter: Optional[str] = None) -> int:
    """Generate today's picks, replace any existing rows for that date.

    Critical: only delete existing picks AFTER we've successfully generated
    new ones. Otherwise, if the upstream API is down/rate-limited, we'd
    end up with an empty board instead of last-known-good picks.

    Pick IDs are deterministic (UUID5 derived from external_id) so cached
    references in user slips and the frontend remain valid across refreshes
    instead of pointing to a brand-new UUID that 404s.

    Args:
      sport_filter: when set (e.g. "MLB"), only re-fetch + replace picks for
        that one sport. Used by the MLB pregame loop (`_mlb_pregame_loop`)
        which runs every 5 min during US afternoons so MLB picks surface
        ~60-90 min pre-game rather than ~5 min pre-game, without burning
        Odds API credits on sports whose slates haven't moved.
    """
    if sport_filter:
        logger.info("Refreshing picks for %s · sport_filter=%s", date_str, sport_filter)
    else:
        logger.info("Refreshing picks for %s", date_str)
    # ── Odds API circuit breaker observability + soft re-arm ─────────
    # If the breaker tripped earlier this process (e.g. transient outage,
    # or operator rotating THE_ODDS_API_KEY mid-day), give it ONE shot
    # to recover. Worst-case cost: 16s stall (2 × 8s timeout) before the
    # breaker re-trips. That's acceptable once per refresh cycle and the
    # only path to self-healing without a manual `/admin/odds-circuit/reset`
    # call. Operator can always observe state via /api/admin/odds-diagnostic.
    try:
        from sports_engine import get_odds_api_status, reset_odds_api_circuit
        st_before = get_odds_api_status()
        if st_before.get("disabled"):
            logger.warning(
                "Refresh starting with Odds API circuit OPEN (%s) — soft re-arming for one retry",
                st_before.get("disabled_reason", "?"),
            )
            reset_odds_api_circuit()
        else:
            logger.info(
                "Odds API state pre-refresh: has_key=%s ok=%d fail=%d streak_401=%d",
                st_before.get("has_key"), st_before.get("total_ok", 0),
                st_before.get("total_fail", 0), st_before.get("consecutive_401s", 0),
            )
    except Exception as _odds_st_err:
        logger.debug("Could not read Odds API status: %s", _odds_st_err)
    picks = await generate_all_picks(date_str, sport_filter=sport_filter)
    # P0-3 (2026-08-11): normalise defensively so the Tennis Extra
    # fallback below can safely append even when the primary path
    # returned ``None`` (recoverable provider failure) rather than an
    # empty list.
    if picks is None:
        picks = []
    # 2026-07-22 diagnostic — count MLS ESPN picks right off the pipeline.
    _mls_espn_from_engine = sum(
        1 for p in (picks or []) if isinstance(p, dict)
        and p.get("source") == "mls_espn_leaderboard"
    )
    if _mls_espn_from_engine:
        logger.info("MLS ESPN picks from engine: %d", _mls_espn_from_engine)
    # Post-refresh observability so the operator can spot a slate that
    # came back smaller than expected without having to tail logs.
    try:
        from sports_engine import get_odds_api_status
        st_after = get_odds_api_status()
        logger.info(
            "Refresh done: %d raw picks | Odds API ok=%d fail=%d disabled=%s",
            len(picks or []), st_after.get("total_ok", 0),
            st_after.get("total_fail", 0), st_after.get("disabled"),
        )
    except Exception:
        pass

    # ── P0-3 (2026-08-11) Tennis Extra fallback — RUNS UNCONDITIONALLY ──
    # Historically this block was placed AFTER the `if not picks: return 0`
    # early-return below, so a primary Odds-API refresh that produced
    # zero Tennis picks (401, provider outage, empty slate) killed the
    # entire Tennis experience even though the free TennisExplorer
    # scrape fallback could have covered every ATP/WTA/Challenger
    # tournament.  We now execute the fallback ALWAYS when the refresh
    # scope covers Tennis (unfiltered refresh, or ``sport_filter ==
    # "Tennis"``), regardless of the primary path's outcome.
    #
    # Dedupe rule: skip any fallback pick whose ``id`` already appears
    # in ``picks`` (primary + fallback both succeeded case).  Both
    # generators use uuid5-based deterministic ids so duplicates are
    # deterministic collisions across refreshes.
    #
    # ATP / WTA / Challenger / ITF coverage stays intact — the fallback
    # honours the same tier + league taxonomy the primary emits.
    _run_tennis_fallback = (
        sport_filter is None
        or (sport_filter or "").lower() == "tennis"
    )
    if _run_tennis_fallback:
        try:
            from tennis_extra import fetch_extra_tennis_picks
            # days_ahead=3 gives today + 3 days of visibility so tournaments
            # starting Monday–Wednesday appear in the feed on Sunday night
            # (user report 2026-07-12: Umag/Bastad/Gstaad/Athens/Iasi ATP
            # matches on TU 14.07 weren't being picked up). TennisExplorer
            # supports future-date URLs natively so this is 3 extra HTTP
            # calls per refresh (still under the 30-min in-process cache).
            extra = await fetch_extra_tennis_picks(
                date_str=date_str, days_ahead=3)
            if extra:
                existing_ids = {p.get("id") for p in (picks or [])}
                added = 0
                for ep in extra:
                    if ep.get("id") in existing_ids:
                        continue        # dedupe just in case
                    picks.append(ep)
                    added += 1
                logger.info(
                    "Tennis Extra: added %d scraped picks (primary had %d)",
                    added, len(picks) - added,
                )
        except Exception as e:
            logger.warning("Tennis Extra scrape skipped: %s", e)

    if not picks:
        if sport_filter:
            logger.info(
                "%s pregame refresh: 0 picks (likely no lines posted yet) — "
                "leaving existing %s rows untouched.", sport_filter, sport_filter,
            )
        else:
            logger.warning(
                "Refresh produced 0 picks for %s — keeping existing rows intact "
                "instead of wiping the board.", date_str,
            )
        return 0
    # ── MLB Batter-vs-Pitcher enrichment ──
    # User spec: "make sure you got batter vs pitcher when making hit
    # prediction". Pulls career BvP splits from MLB Stats API (free,
    # 0 Odds credits), boosts lock_score for batters with strong
    # historical edge vs the opposing starter, and appends a "5-for-12
    # vs Strider" insight bullet to each MLB hit prop card.
    try:
        from mlb_bvp import enrich_picks_bulk as _bvp_enrich
        await _bvp_enrich(picks)
    except Exception as e:
        logger.warning("MLB BvP enrichment skipped: %s", e)
    namespace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for p in picks:
        ext = str(p.get("external_id") or "")
        if ext:
            # Deterministic uuid5 from external_id (preferred — survives refreshes).
            p["id"] = str(uuid.uuid5(namespace, ext))
        elif p.get("id"):
            # Upstream already assigned a stable id (e.g. tennis_extra's
            # sha1 hash of te|event_date|tournament|players). Convert that
            # into a uuid5 so the wire format matches all the other picks
            # AND survives across refreshes — this keeps the user's "Save
            # to Slip" links valid overnight when the same scraped match
            # gets re-ingested 30 minutes later.
            upstream_id = str(p["id"])
            p["id"] = str(uuid.uuid5(namespace, f"upstream:{upstream_id}"))
        else:
            # Last resort: random uuid (churns each refresh — only used
            # when the upstream didn't bother to set anything stable).
            p["id"] = str(uuid.uuid4())

    # ── SportDB enrichment: pull live team-form into Soccer picks. Cached
    # league standings (24h TTL) keep the daily request count to ~10. The
    # enrichment is best-effort — if SportDB is down or budget hit we still
    # save the un-enriched pick and move on.
    try:
        from sportdb_client import refresh_top_leagues, enrich_pick
        await refresh_top_leagues(db)
        enriched = 0
        for p in picks:
            if p.get("sport") == "Soccer":
                before = p.get("win_probability")
                await enrich_pick(db, p)
                if p.get("enriched_by") == "sportdb" and p.get("win_probability") != before:
                    enriched += 1
        if enriched:
            logger.info("SportDB enriched %d Soccer picks", enriched)
    except Exception as e:
        logger.warning("SportDB enrichment skipped: %s", e)

    # ── Self-tuning learning layer: bias predictions based on historical
    # ROI / hit-rate vs expected. Applied AFTER all other enrichment so it
    # sits on top of model + SportDB + Odds-API edge.
    try:
        from learning_engine import apply_learning
        adjusted = 0
        for p in picks:
            before = p.get("win_probability")
            await apply_learning(db, p)
            if p.get("learning") and p.get("win_probability") != before:
                adjusted += 1
        if adjusted:
            logger.info("Learning engine adjusted %d picks", adjusted)
    except Exception as e:
        logger.warning("Learning engine skipped: %s", e)

    # ── Elite Player Boost: world-class players (Mbappé, Haaland, Messi,
    # Kane, Judge, Sinner, Jokic, Wilson, etc.) get a +10 lock_score bump
    # so they auto-qualify for Lock tier — books price them tightly but
    # they're still the safest hit candidates by reputation.
    try:
        from elite_players import apply_elite_boost
        before_elite = sum(1 for p in picks if p.get("elite_player"))
        picks = apply_elite_boost(picks)
        after_elite = sum(1 for p in picks if p.get("elite_player"))
        logger.info("Elite Player Boost applied: %d picks tagged (was %d)",
                    after_elite, before_elite)
    except Exception as e:
        logger.warning("Elite Player Boost skipped: %s", e)

    # ── Goalscorer Dedup + Top-3-with-Elite-Override ───────────────────
    # User report 2026-06-22: "Why is so many goalscorers for England game
    # thought we doing top 3 unless it more that's elite 70+ win pct and
    # have edge?". Root cause was two compounding bugs:
    #   1) The same player got multiple Anytime Goal Scorer picks (one per
    #      bookmaker quote, plus synthetic AGS picks created from each
    #      To-Score-or-Assist quote in elite_players.py).
    #   2) No event-level cap on how many goalscorer picks survived,
    #      causing 47-pick blowouts on marquee international friendlies.
    # Fix: dedup by (event, player, market_family), then trim each event's
    # goalscorer slate to Top-3-by-win_probability + any extras meeting
    # win≥70% AND edge>0 (the "elite override").
    try:
        picks = _dedupe_and_limit_goalscorers(picks)
    except Exception as e:
        logger.warning("Goalscorer dedup/limit skipped: %s", e)

    # ── Tennis Totals cap (Top-1 alt-line per match per side) ──────────
    # User report 2026-06-22: "still not seeing ml in tennis". The slate
    # had 5 visible MLs but 28 Overs, ~5.6:1 ratio buried the Moneylines.
    # Top-2 was still too many — tightening to Top-1 per (match, side)
    # halves Tennis Overs again so MLs (which the bandit says are our
    # hottest arm at +13% ROI) become visually prominent in the feed.
    try:
        picks = _cap_tennis_totals(picks, max_per_side=1)
    except Exception as e:
        logger.warning("Tennis Totals cap skipped: %s", e)

    # ── Per-Player Rolling Form (Phase 2 learning upgrade) ─────────────
    # Apply each player's last-10 hot/cold streak as a ±5 lock_score
    # nudge. Doesn't override the engine — just tilts toward players we've
    # been recently right on and away from cold streaks.
    try:
        from player_form import apply_player_form
        form_counts = await apply_player_form(picks, db)
        if form_counts.get("applied", 0) > 0:
            logger.info(
                "Player Form applied to %d picks (🔥 hot=%d, ❄️ cold=%d, neutral=%d)",
                form_counts.get("applied", 0),
                form_counts.get("hot", 0),
                form_counts.get("cold", 0),
                form_counts.get("neutral", 0),
            )
    except Exception as e:
        logger.warning("Player Form skipped: %s", e)

    # ── Multi-Armed Bandit (Phase 3 learning upgrade) ──────────────────
    # Thompson-sample each strategy arm's Beta posterior, then tilt picks
    # belonging to currently-winning arms (+lift) and currently-losing
    # arms (-lift) by up to ±LIFT_MAX lock points. This auto-discovers
    # which combinations of lock/edge/odds/sport/market are hot RIGHT NOW
    # without us hand-tuning thresholds.
    try:
        from bandit import sample_arms, apply_bandit_lift
        sampled = await sample_arms(db)
        if sampled:
            bandit_counts = apply_bandit_lift(picks, sampled)
            if bandit_counts.get("applied", 0) > 0:
                logger.info(
                    "Bandit (Thompson) applied to %d picks (↑%d ↓%d) across %d arms",
                    bandit_counts.get("applied", 0),
                    bandit_counts.get("lifted_up", 0),
                    bandit_counts.get("lifted_down", 0),
                    len(sampled),
                )
    except Exception as e:
        logger.warning("Bandit lift skipped: %s", e)

    # ── MLB Prop Simulator (Phase A) — Monte Carlo ─────────────────────
    # Real game-mechanics simulation: per-AB outcome distribution from
    # batter K/BB/BA/HR rates × opposing pitcher splits, distributed over
    # expected ABs. 10k MC runs → P(win) + 95% Wilson CI. Replaces the
    # broken "sim_pass" stress-test signal with empirical win probability.
    try:
        from brain.sim_runner import apply_simulations
        sim_counts = apply_simulations(picks)
        if sim_counts.get("applied", 0) > 0:
            logger.info(
                "MLB Simulator applied to %d picks (stronger=%d weaker=%d neutral=%d)",
                sim_counts.get("applied", 0),
                sim_counts.get("stronger", 0),
                sim_counts.get("weaker", 0),
                sim_counts.get("neutral", 0),
            )
    except Exception as e:
        logger.warning("MLB Simulator skipped: %s", e)

    # ── Sportsbook deep-link enrichment: attach home_team / away_team / pick
    # / fanduel_event_id / draftkings_event_id / etc. to every pick. These
    # power the "Add to Bet Slip" deep links from the parlay & detail screens
    # so users land on the correct game page in FanDuel / DraftKings instead
    # of the sportsbook homepage.
    try:
        from event_matcher import enrich_picks_with_event_ids
        enrich_picks_with_event_ids(picks)
        sample = next((p for p in picks if p.get("fanduel_event_id")), None)
        logger.info(
            "Event-ID enrichment applied to %d picks (sample: %s)",
            len(picks),
            sample.get("fanduel_event_id") if sample else "NONE",
        )
    except Exception as e:
        logger.warning("Event-ID enrichment skipped: %s", e)

    # ── Sportsbook Mapping Engine: build a sportsbook-INDEPENDENT
    # ``selection_v2`` per pick + per-book deep-link bundles (best_link /
    # best_depth). The frontend consumes ``sportsbook_mapping[<Book>].best_link``
    # so users land as close to the actual bet as we can manage without a
    # partner API key. UI is unchanged — same buttons, deeper destinations.
    try:
        from sportsbook_mapper import enrich_picks_with_mapping, SUPPORTED_BOOKS
        enrich_picks_with_mapping(picks)
        depth_counts: dict[str, int] = {}
        for p in picks:
            depths = {b: ((p.get("sportsbook_mapping") or {}).get(b) or {}).get("best_depth")
                      for b in SUPPORTED_BOOKS}
            for d in depths.values():
                depth_counts[d or "none"] = depth_counts.get(d or "none", 0) + 1
        logger.info("Sportsbook Mapping: %d picks enriched across %d books · depth=%s",
                    len(picks), len(SUPPORTED_BOOKS), depth_counts)
    except Exception as e:
        logger.warning("Sportsbook mapping enrichment skipped: %s", e)

    # ── Tennis Edge Engine v2: per-pick component scoring + NO_BET filter,
    # 99-LOCK gating, and max-3-per-day cap. Pure post-processing; no extra
    # API calls. Non-tennis picks pass through unchanged.
    try:
        from tennis_engine import apply_tennis_engine, build_tennis_insights
        before_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        picks = await apply_tennis_engine(db, picks)
        after_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        # Attach tennis-specific insights to surviving tennis picks so the
        # Deep Dive UI gets the surface/serve/matchup bullets.
        for p in picks:
            if (p.get("sport") or "").lower() == "tennis":
                tennis_insights = build_tennis_insights(p)
                if tennis_insights:
                    existing = p.get("key_insights") or []
                    p["key_insights"] = tennis_insights + existing
        logger.info("Tennis Edge v2: tennis picks %d → %d (filtered + capped)",
                    before_tennis, after_tennis)
    except Exception as e:
        logger.warning("Tennis Edge v2 skipped: %s", e)

    # ── Bet-Type Classification & Weighted Unit Tagging
    # Per spec: odds ≥ -300 → STRAIGHT (1.0u), ≥ -500 → REDUCED (0.5u),
    # < -500 → PARLAY (0.25u). Real betting behavior — heavy chalk gets
    # smaller stake so ROI math isn't distorted by -500+ lines.
    try:
        from bet_type import classify_bet_type, unit_weight
        for p in picks:
            odds = p.get("book_odds")
            p["bet_type"] = classify_bet_type(odds)
            p["unit_weight"] = unit_weight(odds)
    except Exception as e:
        logger.warning("Bet-type tagging skipped: %s", e)

    # ── Learning System v2: apply ROI/CLV/Calibration/Volume weights +
    # 99-Lock gates + calibration band raises to the freshly-built slate.
    try:
        from learning_system_v2 import apply_v2_to_picks
        picks = await apply_v2_to_picks(picks, db)
    except Exception as e:
        logger.warning("Learning v2 apply skipped: %s", e)

    # ── Phase 4E.3 — Magic Tier post-processing policy cap.
    # WRAP the existing tier (Apex/Elite/Strong/Lock/Playable) with a
    # data-quality / sample-size / stale-odds / lineup-certainty /
    # simulator-validity / calibration-gap-aware cap.  This module
    # NEVER upgrades a tier — it can only downgrade.  When it caps a
    # pick, ``pick["grade"]`` is rewritten to the capped label and the
    # rationale is stashed under ``pick["magic_tier"]`` (internal
    # field; FE reads ``grade`` as before).
    try:
        from services.magic_tier_policy import apply_magic_tier
        _mt_capped = 0
        for _p in picks:
            _d = apply_magic_tier(_p, sport=_p.get("sport"))
            if _d.capped:
                _mt_capped += 1
        if _mt_capped:
            logger.info("Magic Tier policy: %d/%d picks capped",
                        _mt_capped, len(picks))
    except Exception as e:
        logger.warning("Magic Tier policy skipped: %s", e)

    # ── Deep Dive Mode: attach edge/confidence/risk scores, top-3 reasons,
    # and NO-BET flag for low-confidence picks. Internal only; UI unchanged.
    try:
        from deep_dive import deep_dive, NO_BET_THRESHOLD
        no_bet_count = 0
        for p in picks:
            # Tag every fresh pick with the current formula version so the
            # learning engine can isolate clean calibration samples from
            # legacy data.
            p["formula_v"] = 2
            await deep_dive(db, p)
            if p.get("no_bet"):
                no_bet_count += 1
        logger.info("Deep Dive: %d picks analysed, %d flagged NO-BET (conf < %d)",
                    len(picks), no_bet_count, NO_BET_THRESHOLD)
    except Exception as e:
        logger.warning("Deep Dive skipped: %s", e)

    # ── Brain Pipeline v1 — Prediction Memory + Candidate Ranker + hidden
    # Monte Carlo simulator + Decision Filter (PASS verdict) + Confidence
    # Calibration. All seven layers run ON TOP of existing scoring; PASS
    # picks set the existing `no_bet=True` flag so feed endpoints silently
    # drop them with zero UI change. See /app/backend/brain/ for the
    # individual modules.
    try:
        from brain import process_brain
        brain_summary = await process_brain(picks, db)
        logger.info("Brain v%s done in %sms: %s",
                    brain_summary.get("version"),
                    brain_summary.get("elapsed_ms"),
                    brain_summary.get("steps", {}).get("filter"))
    except Exception as e:
        logger.warning("Brain pipeline skipped: %s", e)

    # Deduplicate picks within this batch by `id` — UUID5 hashes can collide
    # if two markets produce identical external_ids (saw this with Anytime
    # Goal Scorer picks generated twice in the same refresh). Keep the first.
    seen_ids: set = set()
    dedup_picks = []
    for p in picks:
        pid = p.get("id")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        dedup_picks.append(p)
    picks = dedup_picks

    # Preserve original `odds_at_pick` and `units_risked` across refreshes so
    # CLV can be measured later (closing_odds is updated by settle). The
    # latest book_odds becomes the running "closing line" snapshot.
    if seen_ids:
        existing = db.picks.find(
            {"id": {"$in": list(seen_ids)}},
            {"_id": 0, "id": 1, "odds_at_pick": 1, "units_risked": 1, "first_seen_at": 1},
        )
        prior: dict[str, dict] = {}
        async for doc in existing:
            prior[doc["id"]] = doc
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat()
        for p in picks:
            pid = p.get("id")
            book = p.get("book_odds")
            prev = prior.get(pid)
            if prev and prev.get("odds_at_pick"):
                p["odds_at_pick"] = prev["odds_at_pick"]
                p["first_seen_at"] = prev.get("first_seen_at", now_iso)
            else:
                p["odds_at_pick"] = book
                p["first_seen_at"] = now_iso
            # closing_odds will be the latest book_odds we saw at refresh time
            # — re-snapshotted on settle below.
            p["closing_odds"] = book
            p["units_risked"] = (prev.get("units_risked") if prev else None) or 1.0

    # Delete previous entries for this date AND any leftover picks with the
    # same UUID5 from a prior day, then insert fresh.
    # When sport_filter is set (e.g. MLB pregame loop), scope the wipe to
    # just that sport so the other sports' rows stay intact.
    #
    # ── STICKY 95+ PINS + OUT-OF-BAND PIPELINE PROTECTION ──
    # Picks that ever crossed 95 lock_score_peak are PINNED — they survive
    # refresh wipes so a user who saw a 99-lock pick yesterday can still
    # find it on today's board (possibly with a "LINE MOVED" indicator if
    # the new generation produced a different version of the same pick).
    #
    # ADDITIONALLY (2026-07-12 permanent fix — user report: "Sweden and
    # Norway goalscorers appeared then they disappeared, please permanently
    # fix"): picks generated by out-of-band pipelines are OWNED by their
    # own refresh loops and MUST NOT be wiped by the main sports_engine
    # refresh. Otherwise, every time the main pipeline fires between the
    # 4-hour hot-scorer / SportDB / CSL cadence, the goalscorer picks
    # vanish for up to 4h until the next external-loop cycle re-inserts
    # them. Excluding these sources + `is_model_only` picks means external
    # pipelines are the *sole* authority over their own picks — the main
    # pipeline only manages what it itself generated.
    _OUT_OF_BAND_SOURCES = [
        "soccer_hot_scorers_v1",
        "csl_espn_leaderboard",
        "csl_espn_live",
        "mls_espn_leaderboard",   # 2026-07-22 — MLS top-scorer picks
        "tennis_extra",
        "tennis_extra_model",
        "tennis_real_odds",
        "mlb_hot_hitters",
        "mlb_hot_hitters_v1",
    ]
    _pin_filter = {
        "$and": [
            {"$or": [
                {"lock_score_peak": {"$exists": False}},
                {"lock_score_peak": {"$lt": 95}},
                # 2026-02 — picks tagged `no_bet=True` (e.g. by the
                # settler / contradiction resolver) MUST be included in
                # the deletion filter even when their `lock_score_peak`
                # crossed the 95 sticky-pin threshold. Otherwise a
                # once-elite pick that was later invalidated will remain
                # pinned forever and keep showing up on the board.
                {"no_bet": True},
            ]},
            # Never wipe out-of-band pipeline picks — they refresh on
            # their own cadence (hot_scorers = 4h, SportDB = 6h, etc.)
            {"source": {"$nin": _OUT_OF_BAND_SOURCES}},
            # `sportdb_scorer*` uses several suffixes (sportdb_scorer_v1,
            # sportdb_scorer_synth, etc.) — match by regex.
            {"source": {"$not": {"$regex": r"^sportdb_scorer", "$options": "i"}}},
            # Extra belt-and-suspenders: any pick flagged as model-only
            # is definitionally not from the main book-odds pipeline.
            {"is_model_only": {"$ne": True}},
        ]
    }
    # ATOMIC-SWAP DEFERRAL (2026-06-28): we used to run delete_many HERE,
    # BEFORE the enrichment passes below (Lock V2, Player Intel, Evidence
    # Governor, MLB Simulator, Sportsbook Mapping, Deep Dive, Brain,
    # Auto-Elite). With ~200 picks that pipeline takes 10-30s — and the
    # DB sits EMPTY for the entire window, so `/api/picks/today` returns
    # `{"picks":[]}` and users see "No locks on the board" every time
    # the refresh fires. Build a deferred-delete closure that we'll call
    # RIGHT BEFORE insert_many — gap shrinks from ~20s to <100ms.
    # User report (2026-06-28): "They come back when I play with tabs
    # but not staying"  ← classic symptom of catching the populated
    # window between refresh cycles.
    async def _apply_atomic_delete():
        if sport_filter:
            await db.picks.delete_many({"pick_date": date_str, "sport": sport_filter, **_pin_filter})
            await db.picks.delete_many({"id": {"$in": list(seen_ids)}, "sport": sport_filter, **_pin_filter})
        else:
            await db.picks.delete_many({"pick_date": date_str, **_pin_filter})
            await db.picks.delete_many({"id": {"$in": list(seen_ids)}, **_pin_filter})
        # ── ID-COLLISION FRESH-OVERWRITE ──
        # If the current refresh re-generates a pick whose `id` is ALSO a
        # sticky 95+ pin in DB, the existing row blocks the new insert
        # (mongo `id` unique index → duplicate key error → silently
        # skipped). The user then sees the STALE lock_score from days
        # ago — e.g. Fábio Abreu landed at lock_score=55.7 (post-evidence
        # demotion from the previous cycle) while the new cycle would have
        # set it to 95.0 via the elite-protect clamp. Fix: ALWAYS delete
        # any existing pick whose `id` is about to be re-inserted by this
        # refresh, regardless of the sticky-pin filter. The peak metadata
        # is preserved on the new pick (peak_lock_score is propagated
        # through the synth + clamp pipeline), so the "highest-ever lock"
        # invariant still holds.
        if seen_ids:
            if sport_filter:
                await db.picks.delete_many(
                    {"id": {"$in": list(seen_ids)}, "sport": sport_filter}
                )
            else:
                await db.picks.delete_many({"id": {"$in": list(seen_ids)}})

        # ── 2026-07-28 DEFECT #4 FIX: semantic-identity delete ─────────
        # ────────────────────────────────────────────────────────────
        # The pick_date-scoped + id-scoped deletes above cannot catch a
        # contradictory row whose (event, player, family, line) matches
        # an incoming pick but whose pick_date sits on a DIFFERENT
        # bucket and whose id is DIFFERENT from any current pick. That
        # was the Wheeler bug — yesterday's Under 6.5 K survived under
        # pick_date=2026-07-27 while today's Over 6.5 K landed on
        # pick_date=2026-07-28.
        #
        # Semantic identity of a prop pick = (sport, event, selection,
        # family, line). Any DB row matching this tuple against ANY
        # incoming pick — regardless of pick_date, id, or side — is
        # stale by definition (Defect #3 has already picked the correct
        # side, and same-side rows on other dates are outdated). Line
        # is REQUIRED so we never accidentally purge a legitimate
        # different-line alt for the same player.
        try:
            import re as _re_sid
            # Ordered longest-first so "Hits + Runs + RBIs" wins over "Hits".
            _MARKET_STAT_PATTERN = _re_sid.compile(
                r"(\d+\.?\d*)\s+(Hits \+ Runs \+ RBIs|Home Runs|Pitching Outs|Earned Runs|Hits Allowed|Total Bases|Runs Scored|Strikeouts|Walks|Hits|RBIs)\s*$",
                _re_sid.IGNORECASE,
            )
            _MARKET_STAT_TO_FAMILY = {
                "strikeouts": "pitcher_strikeouts",
                "hits": "batter_hits",
                "home runs": "batter_home_runs",
                "hits + runs + rbis": "batter_hits_runs_rbis",
                "total bases": "batter_total_bases",
                "rbis": "batter_rbis",
                "runs scored": "batter_runs_scored",
                "walks": "pitcher_walks",
                "pitching outs": "pitcher_outs",
                "earned runs": "pitcher_earned_runs",
                "hits allowed": "pitcher_hits_allowed",
            }
            def _semantic_id(pick_or_row: dict) -> Optional[tuple]:
                _sport = pick_or_row.get("sport")
                _event = pick_or_row.get("event")
                _selection = pick_or_row.get("selection")
                _mkt = pick_or_row.get("market") or ""
                if not (_sport and _event and _selection and _mkt):
                    return None
                _m = _MARKET_STAT_PATTERN.search(_mkt)
                if not _m:
                    return None
                _line = _m.group(1)
                _stat = _m.group(2).lower().strip()
                _family = _MARKET_STAT_TO_FAMILY.get(_stat)
                if not _family:
                    return None
                return (_sport, _event, _selection, _family, _line)

            # Build target index from incoming safe_picks.
            _semantic_targets: dict = {}   # semantic_id → set of incoming ids to preserve
            for _p in safe_picks:
                _sid = _semantic_id(_p)
                if _sid is None:
                    continue
                _semantic_targets.setdefault(_sid, set()).add(_p.get("id"))

            _semantic_deleted = 0
            for _sid, _keep_ids in _semantic_targets.items():
                _sport, _event, _selection, _family, _line = _sid
                _query = {
                    "sport": _sport,
                    "event": _event,
                    "selection": _selection,
                }
                _query.update(_pin_filter)  # never nuke sticky pins here
                _stale_ids: list = []
                async for _row in db.picks.find(
                    _query,
                    {"_id": 0, "id": 1, "market": 1, "pick_date": 1,
                     "sport": 1, "event": 1, "selection": 1},
                ):
                    _row_id = _row.get("id")
                    if not _row_id or _row_id in _keep_ids:
                        continue
                    _row_sid = _semantic_id(_row)
                    if _row_sid == _sid:
                        _stale_ids.append(_row_id)
                        logger.info(
                            "SEMANTIC_DELETE: stale (sport=%s, event=%s, "
                            "selection=%s, family=%s, line=%s) row id=%s "
                            "pick_date=%s market=%r",
                            _sport, _event, _selection, _family, _line,
                            _row_id, _row.get("pick_date"),
                            (_row.get("market") or "")[:80],
                        )
                if _stale_ids:
                    _res = await db.picks.delete_many(
                        {"id": {"$in": _stale_ids}},
                    )
                    _semantic_deleted += int(getattr(_res, "deleted_count", 0) or 0)
            if _semantic_deleted:
                logger.info(
                    "SEMANTIC_DELETE: purged %d stale contradictory rows "
                    "across %d semantic targets",
                    _semantic_deleted, len(_semantic_targets),
                )
        except Exception as _sid_err:
            logger.warning(
                "Semantic-identity delete pass skipped: %s", _sid_err,
            )
        # ── /DEFECT #4 FIX ─────────────────────────────────────────────
    # Defensive write: drop malformed pick docs (missing required fields)
    # so a single broken doc never aborts the entire batch insert. Required
    # fields: id, sport, event_time, market, book_odds.
    REQUIRED = ("id", "sport", "event_time", "market", "book_odds")
    safe_picks = []
    dropped = 0
    _mls_espn_debug = 0   # 2026-07-22 diagnostic
    for p in picks:
        if not isinstance(p, dict):
            dropped += 1
            continue
        missing = [k for k in REQUIRED if not p.get(k)]
        if missing:
            logger.warning("Dropping malformed pick (missing %s): event=%s market=%s",
                         missing, p.get("event"), p.get("market"))
            dropped += 1
            continue
        if p.get("source") == "mls_espn_leaderboard":
            _mls_espn_debug += 1
        safe_picks.append(p)
    if _mls_espn_debug:
        logger.info(
            "MLS ESPN picks reached safe_picks: %d (post malformed-drop)",
            _mls_espn_debug,
        )
    if dropped:
        logger.warning("Skipped %d malformed picks before insert", dropped)

    # ── Lock Engine V2 — SHADOW MODE. Compute v2 scores for every pick.
    # Adds counter_score / survival_score / lock_score_v2 / tier_v2 etc
    # to each pick. The production lock_score field is NEVER touched.
    # Gated by ENABLE_COUNTER_ENGINE env var.
    try:
        from lock_v2.engine import V2_ENABLED, compute_v2_shadow
        if V2_ENABLED and safe_picks:
            v2_tagged = 0
            for p in safe_picks:
                shadow = compute_v2_shadow(p)
                if shadow:
                    p.update(shadow)
                    v2_tagged += 1
            logger.info("Lock V2 shadow tagged %d / %d picks", v2_tagged, len(safe_picks))
    except Exception as _v2_err:
        logger.warning("Lock V2 shadow tagging failed (continuing): %s", _v2_err)

    # ── Player Intelligence enrichment ──
    # Resolve every player-prop pick's market into a canonical profile and
    # attach `player_intel` (archetype, team, position, volatility, usage)
    # so the frontend never has to re-resolve from raw market strings.
    try:
        from player_intel import enrich_picks_with_player_intel
        pi_count = enrich_picks_with_player_intel(safe_picks)
        if pi_count:
            logger.info("Player Intelligence enriched %d picks", pi_count)
    except Exception as _pi_err:
        logger.warning("Player Intelligence enrichment failed (continuing): %s", _pi_err)

    # ── Universal Evidence System ── (2026-06-24)
    # Run the explanation/lock governor on every pick before persistence.
    # Adds: evidence_score, lock_score_raw, evidence_breakdown.
    # Mutates: lock_score (= raw_lock × evidence_multiplier),
    #          key_insights (filtered for hype + evidence-backed).
    # Probability and edge are NEVER mutated.
    try:
        from evidence_engine import build_features_from_pick, govern_pick
        governed_count = 0
        for p in safe_picks:
            try:
                feats = build_features_from_pick(p)
                govern_pick(p, feats)
                governed_count += 1
            except Exception as _per_pick_err:
                # Per-pick failure must not abort the batch — if evidence
                # extraction blows up on one weird pick we still want the
                # others to persist. Surface the error in logs only.
                logger.debug("Evidence governor failed on %s: %s",
                             p.get("id"), _per_pick_err)
        if governed_count:
            logger.info("Evidence governor applied to %d picks", governed_count)
    except Exception as _ev_err:
        logger.warning("Evidence governor unavailable (continuing): %s", _ev_err)

    # ── Elite-protect lock-floor pass ──
    # CSL seed-tier players (Cryzan, Felipe Sousa, Fábio Abreu,
    # Leonardo, Wu Lei, Negrão, Bakambu, etc.) are tagged with
    # `lock_floor` and `elite_protect` by thesportsdb_scorer. The
    # bandit + brain + learning_v2 routinely shave 2-5 lock points
    # off these synthetics. Re-apply the floor as the very last step
    # so the user-curated elite players ALWAYS land on the board at
    # their reputation-based lock score.
    try:
        elite_clamped = 0
        for p in safe_picks:
            floor = p.get("lock_floor")
            if not floor or not p.get("elite_protect"):
                continue
            try:
                floor_f = float(floor)
            except (TypeError, ValueError):
                continue
            updated = False
            for k in ("lock_score", "lock_score_v2", "raw_lock_score", "peak_lock_score"):
                try:
                    cur = float(p.get(k) or 0)
                except (TypeError, ValueError):
                    cur = 0.0
                if cur < floor_f:
                    p[k] = floor_f
                    updated = True
            if updated:
                p["grade"] = "A" if floor_f >= 88 else ("B" if floor_f >= 80 else "C")
                p["confidence"] = p["grade"]
                elite_clamped += 1
        if elite_clamped:
            logger.info("Elite lock-floor clamp: %d synthetic CSL picks restored",
                        elite_clamped)
    except Exception as _ef_err:
        logger.warning("Elite lock-floor clamp failed: %s", _ef_err)

    # ── Universal ESPN-backed pick enrichment ── (2026-06-28)
    # Runs across ALL sports. Adds `pick_rationale` (structured "show your
    # work" data) to every player pick, validates NBA + NFL picks against
    # the `services.active_registry` (drops retired/inactive players),
    # and merges any sport-specific rationale (e.g. CSL ESPN rank).
    # User feedback driving this: "ESPN data should be in pipeline for
    # all sports" + "I want education behind goalscorer, not just random
    # picks". Picks tagged `validation_block` are dropped before persist.
    try:
        from pick_enrichment import enrich_picks_with_active_registry
        en_counts = enrich_picks_with_active_registry(safe_picks)
        # Drop inactive-player picks BEFORE persistence.
        if en_counts.get("blocked_inactive"):
            safe_picks = [p for p in safe_picks if not p.get("validation_block")]
        logger.info(
            "Pick enrichment: %d enriched, %d blocked (inactive), %d skipped (team picks)",
            en_counts.get("enriched", 0),
            en_counts.get("blocked_inactive", 0),
            en_counts.get("skipped_team_pick", 0),
        )
    except Exception as _enr_err:
        logger.warning("Pick enrichment failed (continuing): %s", _enr_err)

    # ── Validation-first architecture (2026-07-04, per user spec) ──
    # Every pick must survive the board_validator gauntlet before it
    # can be published. This is the last gate before insert_many:
    #   §1 contradiction detection (both-sides-of-same-market)
    #   §2 batter-vs-pitcher validation (same-team, non-probable)
    #   §6 board-quality floors (never publish filler picks)
    #   §3 immutable snapshot (locked payload for graders)
    #   §4 rollover tag (permanent on_rollover_at stamp)
    try:
        from board_validator import validate_and_finalize
        pre_count = len(safe_picks)
        safe_picks, val_report = validate_and_finalize(safe_picks)
        logger.info(
            "Board validator: %d → %d picks (contra=%d, bp=%d, quality=%d, rollover=%d)",
            pre_count, len(safe_picks),
            val_report.get("contradictions", {}).get("dropped", 0),
            val_report.get("batter_pitcher", {}).get("dropped", 0),
            val_report.get("board_quality", {}).get("dropped", 0),
            val_report.get("rollover", {}).get("tagged", 0),
        )
        # Log detailed reasons at INFO when anything was dropped so we
        # can debug board-quality regressions from a single log line.
        if any(val_report[k].get("dropped", 0) for k in
               ("contradictions", "batter_pitcher", "board_quality")):
            logger.info("Board validator reasons: %s", val_report)
    except Exception as _bv_err:
        logger.warning("Board validator failed (continuing): %s", _bv_err)

    # ── Monte Carlo simulation engine (2026-07-04 spec, Session 1) ──
    # For every survivor, run scenario-based + multi-model sims and
    # attach `sim_result` (prob / edge / agreement / breakdown). Feeds
    # into the ranker and gives the UI a transparent "why this pick"
    # payload.
    try:
        from sim_engine import simulate_board
        simulate_board(safe_picks, n_simulations=500)
        # Track how many picks got sim results for observability
        sim_ok = sum(1 for p in safe_picks if p.get("sim_result"))
        logger.info("Sim engine: %d/%d picks simulated", sim_ok, len(safe_picks))
    except Exception as _sim_err:
        logger.warning("Sim engine failed (continuing): %s", _sim_err)

    # ── Chalk Kill Switch (2026-07-21) ────────────────────────────────
    # User mandate: "auto-fade any pick priced worse than -250 unless
    # model edge >= 8pp with >=3 aligned data signals". Trap picks stay
    # visible on the board (per user: "I still want the 200 picks for
    # options") but get their lock_score / signal_score capped and a
    # visible warning attached, so users can SEE them without the app
    # RECOMMENDING them as a Lock. This must run LAST — after all
    # lock_score writers (learning_v2 / brain sim_runner) so the cap
    # is authoritative and can't be overwritten downstream.
    # See services/chalk_trap.py for gate details.
    try:
        from services.chalk_trap import apply_chalk_kill_switch
        _ck_stats = apply_chalk_kill_switch(safe_picks)
        logger.info(
            "Chalk Kill Switch: trapped=%d spared_edge=%d spared_dd=%d "
            "spared_alt=%d already_low=%d (of %d chalk / %d total)",
            _ck_stats["trapped"], _ck_stats["spared_by_edge"],
            _ck_stats["spared_by_dd"], _ck_stats["spared_alt"],
            _ck_stats["already_low"], _ck_stats["chalk_seen"],
            _ck_stats["total"],
        )
    except Exception as _ck_err:
        logger.warning("Chalk Kill Switch skipped: %s", _ck_err)

    # ── Longshot Trap (2026-07-21) ────────────────────────────────────
    # Mirror of the Chalk Kill Switch for the OPPOSITE bleed: Soccer
    # 92+ Strong-Lock picks priced at plus-money odds. ROI analysis of
    # 5,309 settled picks: Soccer Strong Lock (92-96) bled -21% ROI
    # (-48u), concentrated in Goal Scorer / SoA markets and +200-and-
    # up longshots (-30% to -74% ROI). Chalk 92+ (<-150 odds) stays
    # profitable (+1.6% to +11%). Trap only touches the bleeding tier.
    # Elite anchor players (Kane / Haaland / Mbappé) and extreme +EV
    # picks (edge >= 12pp + 3 DD signals) escape unchanged.
    try:
        from services.longshot_trap import apply_longshot_trap
        _ls_stats = apply_longshot_trap(safe_picks)
        logger.info(
            "Longshot Trap: trapped=%d spared_elite=%d spared_edge_dd=%d "
            "(of %d seen / %d total)",
            _ls_stats["trapped"], _ls_stats["spared_elite"],
            _ls_stats["spared_edge_dd"], _ls_stats["seen"],
            _ls_stats["total"],
        )
    except Exception as _ls_err:
        logger.warning("Longshot Trap skipped: %s", _ls_err)

    # ── Board Visibility Gate (2026-07-21) ────────────────────────────
    # User mandate: "I don't want the app to grade picks that don't make
    # it to board". Tags every pick with `off_board=True` when it would
    # be hidden from the user (below lock 85, no_bet, validation_block,
    # low-tier grade, or model_only). Settlement modules filter with
    # `off_board: {"$ne": True}` so status stays `pending` on hidden
    # picks — analytics / ROI / bandit / learning ignore them.
    # Runs LAST so it captures every state change made above.
    try:
        from services.board_visibility import tag_board_visibility
        _bv_stats = tag_board_visibility(safe_picks)
        logger.info(
            "Board Visibility: on_board=%d off_board=%d (of %d) reasons=%s",
            _bv_stats["on_board"], _bv_stats["off_board"],
            _bv_stats["total"], _bv_stats.get("reasons") or {},
        )
    except Exception as _bv_err:
        logger.warning("Board Visibility tagging skipped: %s", _bv_err)

    # ── Fusion Enrichment (2026-07-29) ────────────────────────────────
    # Attach Prediction Fusion Engine output (ML + Similar Matchup +
    # Matchup Intelligence + Simulator consensus) to every ON-BOARD
    # player-prop pick BEFORE insert. Off-board picks are skipped to
    # save cycles — they're hidden from the user anyway.
    #
    # We persist to `fusion_predictions` (with pick_id linkage) so the
    # post-settlement grading loop can back-solve `correct` /
    # `winning_component` and feed the adaptive-learning stack.
    #
    # Lazy single-pick enrichment (GET /api/picks/{id}) still works and
    # returns the SAME payload — but now the board also carries it.
    #
    # Bounded concurrency + wrapped exceptions: a fusion engine failure
    # can NEVER take down the pick refresh.
    try:
        from services.pick_fusion_decorator import enrich_picks_bulk
        on_board_picks = [p for p in safe_picks
                           if not p.get("off_board")
                           and not p.get("no_bet")]
        if on_board_picks:
            import time as _t
            _fu_t0 = _t.time()
            await enrich_picks_bulk(
                db, on_board_picks,
                persist=True,
                include_simulator=False,   # simulator excluded from board
                concurrency=8,
            )
            _fu_supported = sum(
                1 for p in on_board_picks
                if isinstance(p.get("fusion"), dict)
                and p["fusion"].get("supported")
            )
            _fu_prob_available = sum(
                1 for p in on_board_picks
                if isinstance(p.get("fusion"), dict)
                and p["fusion"].get("supported")
                and (p["fusion"].get("final_probability") or 0) > 0
            )
            logger.info(
                "Fusion Enrichment: %d/%d on-board picks supported "
                "(%d with non-zero probability) in %.1fs",
                _fu_supported, len(on_board_picks),
                _fu_prob_available, _t.time() - _fu_t0,
            )
    except Exception as _fu_err:
        logger.warning("Fusion Enrichment skipped: %s", _fu_err)

    # ── Elite Evidence Gate (Phase 2, 2026-08-11) ─────────────────────
    # Reputation-anchored elite boosts (applied way earlier by
    # `apply_elite_boost`) are re-evaluated NOW against the enrichment
    # signals that arrived after the boost (form / sim / fusion /
    # bandit / factors / learning).  Elite picks whose evidence does
    # not agree multi-source have their pre-boost lock_score
    # restored — no more famous-⇒-99 without supporting evidence.
    #
    # PRESERVES the elite concept: a passing evidence gate keeps the
    # full elite lock.  Demoted picks are NOT forced off-board — the
    # normal ``>85`` contract still governs eligibility on the
    # restored score.
    #
    # Runs LAST among score-affecting steps so board_visibility (re-
    # tagged below) captures the final state.
    try:
        from services.elite_evidence_gate import apply_elite_evidence_gate
        _eg_stats = apply_elite_evidence_gate(safe_picks)
        if _eg_stats.get("total_elite", 0) > 0:
            logger.info(
                "Elite Evidence Gate: elite=%d passed=%d demoted=%d skipped=%d",
                _eg_stats.get("total_elite", 0),
                _eg_stats.get("passed", 0),
                _eg_stats.get("demoted", 0),
                _eg_stats.get("skipped", 0),
            )
        # Re-tag board visibility so any elite demotion that dropped
        # lock_score under 85 is reflected in the off_board tag before
        # ``insert_many`` persists the batch.
        try:
            from services.board_visibility import tag_board_visibility
            tag_board_visibility(safe_picks)
        except Exception as _bv_err:
            logger.warning(
                "Board Visibility re-tag after elite gate skipped: %s",
                _bv_err,
            )
    except Exception as _eg_err:
        logger.warning("Elite Evidence Gate skipped: %s", _eg_err)

    if safe_picks:
        # ATOMIC-SWAP: do the wipe NOW, immediately before the insert.
        # The enrichment passes above ran on in-memory `safe_picks` —
        # the DB still has the PREVIOUS slate visible to clients this
        # entire time, so users never see "No locks on the board" mid-
        # refresh. Gap between delete + insert is now <100ms instead of
        # the old 10-30s.
        await _apply_atomic_delete()
        # ordered=False already lets pymongo continue past duplicate-key
        # rows, but it STILL raises BulkWriteError at the end, aborting
        # the caller. Catch + count + log so picks that DID land still
        # commit cleanly. Most "duplicates" are picks that were already
        # written by a parallel sport refresh (MLB pregame + full refresh
        # racing), so the data is identical and the error is benign.
        try:
            await db.picks.insert_many(safe_picks, ordered=False)
        except Exception as bulk_err:
            # pymongo BulkWriteError exposes per-doc errors in .details
            details = getattr(bulk_err, "details", None) or {}
            n_inserted = int(details.get("nInserted", 0) or 0)
            write_errors = details.get("writeErrors") or []
            dup_errors = [e for e in write_errors if e.get("code") == 11000]
            other_errors = [e for e in write_errors if e.get("code") != 11000]
            if other_errors:
                # Non-duplicate write errors are real bugs — re-raise.
                logger.error("Unexpected pick insert errors: %s", other_errors[:3])
                raise
            logger.warning(
                "Pick insert: %d inserted, %d duplicates skipped (already in DB).",
                n_inserted, len(dup_errors),
            )
    logger.info("Stored %d picks for %s", len(safe_picks), date_str)

    # ── P0-4 K-MATH BEFORE PUBLICATION (2026-08-08) ────────────────
    # The Over/Under K-math reconciler was previously running AFTER
    # `publish_batch()`, which meant any pick corrected by the
    # reconciler had a canonical snapshot reflecting the PRE-
    # correction values.  Move the reconciler UP so the correction
    # happens BEFORE we snapshot, then re-hydrate `safe_picks` from
    # DB so `publish_batch` sees the final K-math-corrected state
    # for every pick this refresh emits.
    #
    # This is purely an orchestration change: the K-math formulas,
    # dedupe criteria, and correction logic inside
    # `_reconcile_player_prop_contradictions` are unchanged.
    try:
        await _reconcile_player_prop_contradictions(safe_picks, date_str)
    except Exception as e:
        logger.warning("Prop contradiction reconciliation skipped: %s", e)

    # Re-hydrate `safe_picks` from DB now that K-math has run.
    # Reconciler may have (a) mutated canonical fields on the just-
    # inserted picks, or (b) deleted "losing" picks entirely.
    # Publication needs the final state for both cases.  We look
    # up by stable `id` (already the join key across picks and
    # prediction_snapshots).
    if safe_picks:
        try:
            _sp_ids = [p["id"] for p in safe_picks if p.get("id")]
            _fresh: dict[str, dict] = {}
            async for _p in db.picks.find(
                {"id": {"$in": _sp_ids}}, {"_id": 0}
            ):
                _fresh[_p["id"]] = _p
            # Preserve original order; drop picks the reconciler
            # deleted (they no longer exist in db.picks).
            safe_picks = [_fresh[p["id"]] for p in safe_picks
                          if p.get("id") in _fresh]
            _dropped = len(_sp_ids) - len(safe_picks)
            if _dropped:
                logger.info(
                    "K-math reconciler removed %d picks before "
                    "publication (losers)", _dropped,
                )
        except Exception as _rh_err:
            logger.warning(
                "safe_picks re-hydration after K-math failed "
                "(publication will use pre-reconciler values): %s",
                _rh_err,
            )

    # ─── Phase 1a WRITE BARRIER — Prediction Publication Service ───
    # Dual-write mode (2026-08-06).  We emit an immutable snapshot for
    # every candidate that just landed in `picks`, and copy the
    # published_* fields back onto the pick doc.  Endpoints are NOT yet
    # cut over — they continue to read the legacy fields.  Any drift
    # between the legacy fields and the snapshot is recorded to the
    # `publication_mismatch_report` collection for later analysis
    # before Phase 1b flips endpoints over to the snapshot.
    #
    # See: /app/PUBLICATION_CONTRACT.md
    #      /app/backend/services/prediction_publication_service.py
    try:
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        publisher = PredictionPublicationService(db)
        try:
            await publisher.ensure_indices()
        except Exception as _idx_err:
            logger.debug("publication indices ensure failed: %s", _idx_err)

        # ── Phase 2 (2026-08-11) Layer-B player↔team gate ────────────
        # Filter out Soccer player-props whose player's CURRENT team
        # is not on the fixture (or whose roster observation is not
        # fresh).  Invalid picks are marked off_board and NEVER given
        # a publication_source.  Non-Soccer picks pass through.
        try:
            from services.player_team_fixture_validator import (
                validate_player_fixture_pick, tag_pick_with_verdict, _norm,
            )
            roster_lookup: dict[str, str] = {}
            fresh_names: set[str] = set()
            try:
                from services import mls_scorer_gate as _mls
                snap = getattr(_mls, "_espn_by_name", None) or {}
                for name, entry in snap.items():
                    t = entry.get("team") if isinstance(entry, dict) else None
                    if t:
                        key = _norm(name)
                        roster_lookup[key] = t
                        fresh_names.add(key)
            except Exception:
                pass
            for p in safe_picks:
                pn = p.get("player_name") or p.get("player")
                pct = p.get("player_current_team")
                if isinstance(pn, str) and isinstance(pct, str):
                    key = _norm(pn)
                    roster_lookup[key] = pct
                    fresh_names.add(key)
            _publish_batch = []
            _quarantined = 0
            for p in safe_picks:
                if p.get("sport") != "Soccer":
                    _publish_batch.append(p)
                    continue
                verdict = validate_player_fixture_pick(
                    p, roster_lookup,
                    fresh_roster_names=(fresh_names or None),
                )
                if verdict.get("verified"):
                    _publish_batch.append(p)
                    continue
                tag_pick_with_verdict(p, verdict)
                p["off_board"] = True
                _existing = list(p.get("off_board_reasons") or [])
                for tag in ("player_team_invalid",
                             verdict.get("reason") or "unknown"):
                    if tag not in _existing:
                        _existing.append(tag)
                p["off_board_reasons"] = _existing
                _quarantined += 1
            if _quarantined:
                logger.info(
                    "Player↔team gate: %d Soccer player-props quarantined",
                    _quarantined,
                )
        except Exception as _pt_err:
            logger.warning(
                "Player↔team gate skipped (non-fatal): %s", _pt_err,
            )
            _publish_batch = safe_picks

        summary = await publisher.publish_batch(
            _publish_batch, publication_source="canonical_pipeline",
            dual_write=True,
        )
        logger.info(
            "Publication: new=%d existing=%d mismatches=%d errors=%d "
            "board=%s",
            summary.get("new_snapshots", 0),
            summary.get("existing_snapshots", 0),
            summary.get("mismatches_logged", 0),
            len(summary.get("errors", []) or []),
            summary.get("board_version"),
        )
    except Exception as pub_err:
        # Never let publication instability break the refresh.  In
        # Phase 1a everything is dual-write and endpoints still read
        # the legacy fields, so a broken publication is degraded
        # visibility, not degraded UX.
        logger.warning("Publication step failed (non-fatal): %s", pub_err)
        summary = {}

    # ─── Phase 1 Final Closure (2026-08-11) ───────────────────────
    # Emit the full candidate-disposition lifecycle trail for every
    # pick in this refresh cycle.  One central hook — no scattered
    # writes across sport-specific files.  Best-effort: any failure
    # here degrades observability, not the board.
    #
    # Trails written per candidate:
    #   evaluated → (accepted → published → board_eligible)
    #   evaluated → rejected(reason)
    #
    # Reasons come from the pick's own final tags (no_bet /
    # off_board_reasons / validation_block) so the trail matches the
    # actual pipeline decision that hid the pick.
    try:
        from services.candidate_disposition import (
            record_batch_dispositions,
        )
        _disp_stats = await record_batch_dispositions(
            db, safe_picks, publication_summary=summary,
        )
        logger.info(
            "Candidate dispositions: eval=%d acc=%d rej=%d pub=%d elig=%d",
            _disp_stats.get("evaluated", 0),
            _disp_stats.get("accepted", 0),
            _disp_stats.get("rejected", 0),
            _disp_stats.get("published", 0),
            _disp_stats.get("board_eligible", 0),
        )
    except Exception as _disp_err:
        logger.warning(
            "Candidate disposition recording failed (non-fatal): %s",
            _disp_err,
        )

    # 2026-07-22 MLS ESPN post-insert diagnostic. Should reveal whether
    # picks were persisted to Mongo or silently dropped.
    try:
        _mls_espn_in_db = await db.picks.count_documents({
            "id": {"$regex": "^mls-espn"},
            "pick_date": date_str,
        })
        _mls_espn_safe = sum(
            1 for p in safe_picks
            if p.get("source") == "mls_espn_leaderboard"
        )
        logger.info(
            "MLS ESPN diagnostic — safe_picks=%d, in DB for %s=%d",
            _mls_espn_safe, date_str, _mls_espn_in_db,
        )
    except Exception as _e:
        logger.warning("MLS ESPN diagnostic failed: %s", _e)
    # ── Cross-run contradiction reconciliation ────────────────────
    # P0-4 (2026-08-08): moved UP to run BEFORE publish_batch so the
    # canonical snapshot for this refresh reflects K-math-corrected
    # values.  This block used to live here (post-publication) and
    # was flagged in the P0-3 audit as a post-publication canonical
    # mutation.  Retained comment for historical context only —
    # the actual call is executed earlier in this function.
    # ── CSL Guaranteed Elite Injection ──
    # The standard refresh pipeline (learning + bandit + brain + evidence
    # governor + dedupe trims) systematically drops CSL synth picks even
    # when they're tagged elite. We re-run the synthesizer for every CSL
    # event today AFTER the main pipeline lands and force-inject any
    # missing elites into the DB so the user always sees Cryzan, Felipe
    # Sousa, Fábio Abreu, Leonardo, Wu Lei, Júnior Negrão, Cédric
    # Bakambu, Wesley Moraes, Oscar Taty Maritu, Zhang Yuning, Marcão,
    # and any other hot-form scorer in `csl_form_seed.py`.
    try:
        await _ensure_csl_elite_picks(date_str)
    except Exception as e:
        logger.warning("CSL elite-inject failed (non-fatal): %s", e)
    # ── GoalScorer Engine v2 shadow capture ──
    # Best-effort: log a v2 prediction for every soccer goalscorer pick
    # that just landed so calibration data starts accumulating. NEVER
    # raises — strictly shadow.
    try:
        await _shadow_capture_gs_v2(safe_picks)
    except Exception as e:
        logger.debug("gs_v2 shadow capture failed (non-fatal): %s", e)
    # ── Invalidate the slate-wide Signal Rank cache (2026-07-17) ────
    # We just replaced today's picks — any percentile ranks persisted
    # from the previous slate are now stale. Drop the TTL cache so the
    # next /picks/today call rebuilds the ranks against the fresh
    # slate. Safe fallback if the module isn't importable.
    try:
        from services.signal_engine import invalidate_signal_rank
        invalidate_signal_rank(date_str)
    except Exception as _iv_err:
        logger.debug("signal_rank invalidate skipped: %s", _iv_err)
    return len(safe_picks)


# ── Moved from server.py lines 2368-2436 (Phase 3F-1) ──
def _prop_family_key(market: str) -> str:
    """Categorise a player-prop market label into a coarse family.

    Returned families group Over/Under sides of the SAME stat together so
    the contradiction reconciler can identify "same player, opposite
    side" pairs even across refresh runs. Returns "" when the market
    isn't a supported player-prop family.
    """
    m = (market or "").lower()
    # Order matters — check compound families before their sub-strings.
    if "hits + runs + rbis" in m or "hits + runs" in m: return "MLB_HRR"
    if "hits allowed" in m:              return "MLB_HALLOWED"
    if "home run" in m:                  return "MLB_HR"
    if "total bases" in m:               return "MLB_TB"
    if "rbis" in m:                      return "MLB_RBI"
    if "outs recorded" in m:             return "MLB_OUTS"
    if "strikeout" in m:                 return "MLB_K"
    if "hits" in m:                      return "MLB_HITS"
    if "passing yards" in m or "pass yds" in m:  return "NFL_PASS_YDS"
    if "rushing yards" in m or "rush yds" in m:  return "NFL_RUSH_YDS"
    if "receiving yards" in m or "reception yds" in m: return "NFL_REC_YDS"
    if "receptions" in m:                return "NFL_REC"
    if "pass tds" in m or "passing tds" in m: return "NFL_PASS_TDS"
    if "rush tds" in m or "rushing tds" in m: return "NFL_RUSH_TDS"
    if "points" in m:                    return "NBA_PTS"
    if "rebounds" in m:                  return "NBA_REB"
    if "assists" in m:                   return "NBA_AST"
    return ""


# ── 2026-07-28 DEFECT #5 — no_bet schema safety helpers ───────────────
# ──────────────────────────────────────────────────────────────────
# All contradiction "loser" writes MUST go through `_atomic_mark_no_bet`
# so the invariant holds:  `no_bet_reason set  ⇒  no_bet == True`
# AND  `status == "blocked"` (so the row disappears from any endpoint
# that filters by status ∈ {pending, open, None}).
#
# `_enforce_no_bet_schema_invariant` runs at app startup to sweep any
# pre-existing rows where a bad code path or crash left the fields
# inconsistent, and (best-effort) installs a MongoDB $jsonSchema
# validator that rejects future inconsistent writes at the DB layer.

async def _atomic_mark_no_bet(
    query: dict,
    reason: str,
    extra: dict | None = None,
) -> int:
    """Atomically flag every doc matching `query` as no_bet.

    Always writes:
      • no_bet=True
      • no_bet_reason=<reason>
      • status="blocked"

    …in a SINGLE `$set`, so the trio is either fully present or
    entirely absent — never partially-set. Callers must NEVER write
    `no_bet_reason` directly; funnel through this helper.

    Returns the number of modified docs.
    """
    payload = {
        "no_bet": True,
        "no_bet_reason": str(reason or "unspecified"),
        "status": "blocked",
    }
    if extra:
        payload.update(extra)
    res = await db.picks.update_many(query, {"$set": payload})
    return int(getattr(res, "modified_count", 0) or 0)


# ── Moved from server.py lines 2515-2957 (Phase 3F-1) ──
async def _reconcile_player_prop_contradictions(safe_picks: list, date_str: str) -> None:
    """After insert, remove any Over/Under contradictions for the SAME
    (event, player, market_family) — keep only the higher-edge side.

    Scope: only player-prop markets where the pick has a `selection`
    that names a specific player. Team totals / spreads / totals are
    left alone (they're handled by other dedup passes upstream).
    """
    if not safe_picks:
        return
    # Build a set of (event, player, family) touched by this insert so we
    # only re-query rows that could have new contradictions.
    touched: set[tuple[str, str, str]] = set()
    for p in safe_picks:
        market = p.get("market") or ""
        sel = (p.get("selection") or "").strip()
        event = p.get("event") or ""
        family = _prop_family_key(market)
        if not (event and sel and family):
            continue
        # Skip aggregate-selection labels ("Yes", "Over", "Under" without
        # a player) — those aren't player-props.
        if sel.lower() in ("yes", "no", "over", "under"):
            continue
        # Must have an Over or Under indicator to qualify as a two-sided
        # prop where contradiction is possible.
        m_l = market.lower()
        if " over " not in m_l and " under " not in m_l and "over " not in m_l[:5] and "under " not in m_l[:6]:
            continue
        touched.add((event, sel, family))
    if not touched:
        return
    removed_total = 0
    # Consider picks from the last 72h so cross-refresh contradictions for
    # the same event are caught even when pick_date differs (a late-night
    # refresh may bucket a pick under tomorrow's pick_date while the
    # earlier refresh bucketed it under today's — same event/player).
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff_iso = (_dt.now(_tz.utc) - _td(hours=72)).isoformat()

    # ── Line-value extractor (2026-02) ───────────────────────────────
    # Over 7.5 vs Under 7.5 IS a contradiction (strict opposites).
    # Over 7.5 vs Under 8.5 IS NOT (both win at K=8 — user report:
    # "His line is 7.5 not 8.5 so the over should of stayed").
    # Grouping must include the numeric line so we only pair sides of
    # the SAME line.
    import re as _re
    _line_re = _re.compile(r"(?i)(?:over|under)\s+(-?\d+(?:\.\d+)?)")

    def _extract_line(market: str) -> str:
        if not market:
            return ""
        m = _line_re.search(market)
        return m.group(1) if m else ""

    for event, player, family in touched:
        rows = await db.picks.find(
            {"event": event, "selection": player,
             "no_bet": {"$ne": True},
             "created_at": {"$gte": cutoff_iso}},
            {"_id": 0, "id": 1, "market": 1, "edge_percent": 1,
             "lock_score": 1, "created_at": 1, "pick_date": 1,
             # 2026-07-28 — pull the K-math signals so we can consult
             # the shared resolver for MLB_K family contradictions.
             "k_math_gate": 1, "k_math_expected_k": 1,
             # Fields required for cross-pick_date "update-in-place"
             # so the loser row can be transformed into the winner
             # instead of leaving a stale row sitting on the earlier
             # pick_date tagged `no_bet=True`.
             "selection": 1, "book_odds": 1, "book": 1, "side": 1,
             "key_insights": 1, "k_prop_data": 1, "sport": 1,
             "event": 1, "grade": 1, "confidence": 1,
             "probability": 1,
             # P0-5 (2026-08-08): required so the immutability
             # guard can detect prior-refresh canonical publication.
             "publication_source": 1},
        ).to_list(length=200)
        # Group by (family, line) with side. Same family + same line
        # is required for a genuine contradiction — different lines
        # can both win (e.g. Over 7.5 K's AND Under 8.5 K's both hit
        # if the pitcher records exactly 8 strikeouts).
        by_group: dict[str, dict[str, list[dict]]] = {}
        for r in rows:
            m = r.get("market") or ""
            if _prop_family_key(m) != family:
                continue
            line = _extract_line(m)
            if not line:
                # No numeric line detected — skip; can't reason about
                # contradictions when the line is unknown.
                continue
            ml = m.lower()
            if " over " in ml or ml.startswith("over "):
                by_group.setdefault(line, {"over": [], "under": []})["over"].append(r)
            elif " under " in ml or ml.startswith("under "):
                by_group.setdefault(line, {"over": [], "under": []})["under"].append(r)
        for line, by_side in by_group.items():
            if not (by_side["over"] and by_side["under"]):
                continue
            # ── Winner selection ───────────────────────────────────────
            # For MLB_K family, consult the shared K-math resolver first
            # (identical logic to the in-memory K conflict resolver in
            # sports_engine.py). For all other families, or when the
            # K-math signal is missing/indeterminate, fall back to
            # (edge_percent, lock_score).
            def _rank(r: dict) -> tuple:
                return (float(r.get("edge_percent") or 0),
                        float(r.get("lock_score") or 0))
            all_rows = by_side["over"] + by_side["under"]
            winner = None
            if family == "MLB_K":
                try:
                    from services.k_conflict_resolver import resolve_k_family_winner
                    best_over = max(by_side["over"], key=_rank)
                    best_under = max(by_side["under"], key=_rank)
                    try:
                        line_f = float(line)
                    except ValueError:
                        line_f = None
                    winning_side, reason = resolve_k_family_winner(
                        best_over, best_under, line_f,
                    )
                    if winning_side == "over":
                        winner = best_over
                    elif winning_side == "under":
                        winner = best_under
                    if winner is not None:
                        logger.info(
                            "Reconciler K-math winner: %s %s %s @ line=%s (%s)",
                            winning_side, family, player, line, reason,
                        )
                except Exception as _kmath_err:
                    logger.debug("K-math resolver skipped (%s): %s",
                                 player, _kmath_err)
            if winner is None:
                winner = max(all_rows, key=_rank)
            winner_side = "over" if winner in by_side["over"] else "under"
            loser_side = "under" if winner_side == "over" else "over"
            loser_rows = by_side[loser_side]
            if not loser_rows:
                continue

            # ── Cross-pick_date "update in place" (2026-07-28) ─────────
            # If a corrected pick landed as a NEW row on a LATER
            # pick_date while the WRONG-side row still exists on an
            # EARLIER pick_date, update the earlier (loser) row in
            # place with the winner's payload and delete the redundant
            # new row. This prevents the DB from accumulating
            # `no_bet=True` stragglers under older pick_dates every
            # time the K resolver flips a side.
            winner_id = winner.get("id")
            winner_pd = winner.get("pick_date") or ""
            same_side_losers = [
                r for r in loser_rows if r.get("id") and r.get("id") != winner_id
            ]
            older_losers = [
                r for r in same_side_losers
                if (r.get("pick_date") or "") < winner_pd
            ]

            if older_losers and winner_pd:
                keeper = min(older_losers, key=lambda r: r.get("pick_date") or "")
                # ── P0-5 IMMUTABILITY GUARD (2026-08-08) ──────────────
                # Cross-refresh K-math reconciliation would previously
                # mutate the older keeper row IN PLACE, copying the
                # winner's canonical fields (`market`, `selection`,
                # `side`, `book_odds`, `edge_percent`, `lock_score`,
                # `grade`, `confidence`, `probability`, `pick_date`)
                # onto it — even when the keeper had ALREADY been
                # canonically published in an earlier refresh.  That
                # created two divergent truths: the immutable snapshot
                # said one thing and the mutable `picks` row said
                # another.
                #
                # New behaviour: if the keeper carries
                # `publication_source`, treat its published state as
                # immutable per PUBLICATION_CONTRACT §3.  Fall back
                # to the safe path — atomically tag the keeper
                # `no_bet=True` (a lifecycle flag, NOT a canonical
                # field) so /picks/today filters it out, and leave
                # the current-refresh winner in place so the
                # rehydrated `safe_picks` still publishes it as a
                # brand-new snapshot with the correct side.
                #
                # This preserves K-math correction (the correct side
                # is still selected and still lands on the board) but
                # forces the correction to arrive via the normal
                # generation → publication pipeline rather than via a
                # silent post-publication rewrite of historical truth.
                if keeper.get("publication_source"):
                    try:
                        modified = await _atomic_mark_no_bet(
                            {"id": keeper["id"]},
                            (
                                f"cross-refresh K-math flipped side to "
                                f"{winner_side} {family} {line} for "
                                f"{player}; prior published keeper "
                                f"retained immutably (P0-5)"
                            ),
                        )
                        # Also neutralise the sibling losers on the
                        # earlier pick_date that were NOT the keeper
                        # so they don't linger on the board.
                        sibling_ids = [
                            r["id"] for r in same_side_losers
                            if r["id"] != keeper["id"]
                        ]
                        if sibling_ids:
                            modified += await _atomic_mark_no_bet(
                                {"id": {"$in": sibling_ids}},
                                (
                                    f"cross-refresh K-math flipped side "
                                    f"to {winner_side} {family} {line} "
                                    f"for {player} (sibling loser)"
                                ),
                            )
                        removed_total += modified
                        logger.warning(
                            "P0-5 keeper immutability: keeper=%s is "
                            "canonically published — SKIPPED in-place "
                            "K-math mutation; tagged no_bet + kept "
                            "current-refresh winner=%s alive for "
                            "publication",
                            keeper["id"], winner_id,
                        )
                    except Exception as _guard_err:
                        logger.warning(
                            "P0-5 immutability guard write failed "
                            "(keeper=%s): %s",
                            keeper.get("id"), _guard_err,
                        )
                    # Deliberately DO NOT delete the winner (P2) here —
                    # it must survive so `publish_batch` snapshots it
                    # after `safe_picks` is re-hydrated.
                    continue

                # Copy the winner's payload into the keeper row. We
                # intentionally do NOT copy `id`, `created_at`, or
                # `event` — the audit trail preserves the original
                # slot. `pick_date` MUST be updated so the corrected
                # pick shows up on today's board.
                update_payload = {}
                for k in ("market", "selection", "side", "book_odds",
                          "book", "edge_percent", "lock_score",
                          "key_insights", "k_math_gate",
                          "k_math_expected_k", "k_prop_data",
                          "pick_date", "grade", "confidence",
                          "probability"):
                    v = winner.get(k)
                    if v is not None:
                        update_payload[k] = v
                update_payload["corrected_from_side"] = loser_side
                update_payload["corrected_at"] = _dt.now(_tz.utc).isoformat()
                update_payload["corrected_by"] = "reconciler_k_math"
                # Ensure the keeper is NOT flagged no_bet (it's the
                # winner now). Also clear any prior no_bet_reason.
                update_payload["no_bet"] = False
                update_payload["no_bet_reason"] = ""
                try:
                    # ── P0-5 defence-in-depth (2026-08-08) ────────────
                    # Even though we guarded the branch above, add a
                    # Mongo-side filter here so a concurrent
                    # publication landing between the read and the
                    # write cannot slip through — the write matches
                    # zero documents if `publication_source` is set.
                    await db.picks.update_one(
                        {"id": keeper["id"],
                         "publication_source": {"$exists": False}},
                        {"$set": update_payload},
                    )
                    # Delete the newer winner row (redundant now that
                    # the keeper carries the winner's payload).
                    await db.picks.delete_one({"id": winner_id})
                    logger.info(
                        "Reconciler in-place update: keeper=%s (was %s, "
                        "pick_date %s → %s), deleted duplicate winner=%s",
                        keeper["id"], loser_side,
                        keeper.get("pick_date"), winner_pd, winner_id,
                    )
                    removed_total += 1
                except Exception as _upd_err:
                    logger.warning(
                        "In-place reconcile failed (%s → %s): %s",
                        keeper.get("id"), winner_id, _upd_err,
                    )

                # Any remaining loser dupes (not the keeper) → atomic
                # no_bet write. Both fields written in a single $set so
                # `no_bet_reason` can never persist without `no_bet=True`.
                remaining_ids = [
                    r["id"] for r in same_side_losers
                    if r["id"] != keeper["id"]
                ]
                if remaining_ids:
                    modified = await _atomic_mark_no_bet(
                        {"id": {"$in": remaining_ids}},
                        (
                            f"contradicts {winner_side} {family} "
                            f"{line} for {player} (corrected)"
                        ),
                    )
                    removed_total += modified
            else:
                # Standard same-pick_date contradiction path — atomic
                # no_bet write via helper. Helper guarantees no_bet=True,
                # no_bet_reason=..., status="blocked" all in a single
                # $set so the trio can never desync.
                loser_ids = [r["id"] for r in loser_rows if r.get("id")]
                if not loser_ids:
                    continue
                modified = await _atomic_mark_no_bet(
                    {"id": {"$in": loser_ids}},
                    (
                        f"contradicts {winner_side} {family} "
                        f"{line} for {player}"
                    ),
                )
                removed_total += modified
    if removed_total:
        logger.info(
            "Prop contradiction reconciliation: neutralised %d contradicting picks across %d groups",
            removed_total, len(touched),
        )




async def _ensure_csl_elite_picks(date_str: str) -> None:
    """Force-inject any missing CSL elite goalscorer picks for today.

    Runs AFTER the main refresh pipeline. For every CSL event today:
      1. Pull the existing goalscorer player_names from DB.
      2. Re-synthesize via `thesportsdb_scorer.compute_anytime_scorer_picks`.
      3. Any seed-tagged elite player (`elite_protect=True` AND `lock_floor`)
         that's MISSING from DB → insert directly with the synthesizer's
         lock_score (already floor-clamped at the elite tier).

    This is a "guarantee" pass — the upstream filters won't see these
    picks again so they can't trim/demote them.
    """
    import thesportsdb_scorer as _tsdb
    # Pull today's CSL events (from any picks already in DB that have CSL league)
    # De-dupe by NORMALIZED event name — the same match may appear in DB
    # under multiple event_id values (real Odds API uuid, the event name
    # itself, and a missing/dash event_id for team-totals picks).
    events_seen: dict[str, dict] = {}
    cur = db.picks.find(
        {"pick_date": date_str, "league": "China Super League"},
        {"_id": 0, "event": 1, "event_id": 1, "event_time": 1, "home_team": 1, "away_team": 1},
    )
    async for p in cur:
        ev_name = p.get("event") or ""
        if not ev_name:
            continue
        key = ev_name.strip().lower()
        if key in events_seen:
            # Promote real event_id over "-" or the event-name string if we see one
            existing = events_seen[key]
            eid = p.get("event_id")
            if eid and eid != ev_name and (not existing.get("event_id") or existing["event_id"] in ("-", ev_name)):
                existing["event_id"] = eid
            continue
        ht = p.get("home_team") or ""
        at = p.get("away_team") or ""
        # Parse "Away @ Home" if home/away missing
        if (not ht or not at) and " @ " in ev_name:
            at, ht = ev_name.split(" @ ", 1)
        if not ht or not at:
            continue
        events_seen[key] = {
            "event_id": p.get("event_id") or ev_name,
            "event": ev_name,
            "home_team": ht, "away_team": at,
            "event_time": p.get("event_time"),
        }

    if not events_seen:
        return

    injected = 0
    for ev in events_seen.values():
        try:
            tsdb_picks = await _tsdb.compute_anytime_scorer_picks(
                db,
                home_team=ev["home_team"], away_team=ev["away_team"],
                event_id=ev["event_id"], kickoff_iso=ev["event_time"] or "",
                league="China Super League", sport_key="soccer_china_superleague",
                max_per_side=5,
            )
        except Exception as e:
            logger.warning("CSL elite re-synth failed for %s: %s", ev["event"], e)
            continue
        # Only elite_protect picks survive this guarantee pass
        elites = [p for p in tsdb_picks if p.get("elite_protect")]
        if not elites:
            continue
        # Filter out elites already in DB (by player_name + event)
        existing_names: set[str] = set()
        cur2 = db.picks.find(
            {"pick_date": date_str, "event_id": ev["event_id"],
             "market": {"$regex": "Anytime Goal Scorer"}},
            {"_id": 0, "player_name": 1},
        )
        async for d in cur2:
            nm = (d.get("player_name") or "").lower()
            if nm:
                existing_names.add(nm)
        missing = [p for p in elites if (p.get("player_name") or "").lower() not in existing_names]
        if not missing:
            continue
        # Stamp pick_date + insert
        for p in missing:
            p["pick_date"] = date_str
            p["force_injected"] = True   # diagnostic — these bypass the pipeline
        try:
            await db.picks.insert_many(missing, ordered=False)
            injected += len(missing)
            # ── P0-2 canonical publication ─────────────────────────
            # Force-injected CSL elite goalscorer picks are
            # legitimate user-facing predictions but bypassed the
            # main orchestrator publication step above.  Publish
            # them now so an immutable snapshot exists BEFORE the
            # canonical board eligibility gate examines them.
            try:
                from services.publication_helpers import (
                    publish_upserted_picks,
                )
                await publish_upserted_picks(
                    db, missing,
                    publication_source="csl_elite_scorer_inject",
                    caller_label=f"CSL elite scorer inject ({ev['event']})",
                )
            except Exception as _pub_err:
                logger.warning(
                    "CSL elite-inject publication step failed: %s",
                    _pub_err,
                )
        except Exception as bulk_err:
            details = getattr(bulk_err, "details", None) or {}
            n_inserted = int(details.get("nInserted", 0) or 0)
            injected += n_inserted
    if injected:
        logger.info("CSL elite-inject: %d picks force-injected post-pipeline", injected)


async def _shadow_capture_gs_v2(picks: list[dict]) -> None:
    """Run the v2 engine on every soccer goalscorer pick and store the
    prediction. Pure shadow mode — has no effect on the live board.

    Hooked in by user request 2026-06-24 ("hook v2's store_prediction
    into the soccer prop generator so calibration data starts
    accumulating").
    """
    from goal_scorer_engine_v2 import (
        PlayerFeatures, compute_probabilities, store_prediction,
        get_calibration_factor,
    )

    gs_markets = ("anytime goal scorer", "first goal scorer",
                  "last goal scorer", "to score or assist")
    n_stored = 0
    for p in picks or []:
        if p.get("sport") != "Soccer":
            continue
        market_l = (p.get("market") or "").lower()
        if not any(kw in market_l for kw in gs_markets):
            continue
        try:
            # Pull form row (xG / xA / minutes / position / form_score).
            player = (p.get("selection") or "").strip()
            if not player:
                continue
            form = await db.soccer_player_form.find_one(
                {"name_canonical": player.lower()}
            ) or {}
            event = p.get("event") or ""
            # Parse "Away @ Home".
            away_team = home_team = ""
            if " @ " in event:
                away_team, home_team = [x.strip() for x in event.split(" @ ", 1)]
            # Heuristic: if player_team metadata isn't set, fall back to
            # the form-row team and infer opponent from the event string.
            player_team = (
                p.get("player_team")
                or form.get("team")
                or home_team
            )
            opponent = away_team if player_team == home_team else home_team

            features = PlayerFeatures(
                player=player,
                team=player_team or "",
                opponent=opponent or "",
                league=p.get("league") or "",
                xG=float(form.get("xg") or 0.0),
                xA=float(form.get("xa") or 0.0),
                shot_volume=float(form.get("shots_per_90") or 0.0),
                shot_quality=(
                    float(form.get("xg_per_90") or 0.0)
                    / max(0.01, float(form.get("shots_per_90") or 0.01))
                ),
                minutes_played=int(form.get("minutes") or 0),
                games_played=int(form.get("games") or 0),
                starts=int(form.get("games") or 0),
                position=str(form.get("position") or "FW"),
                # Sensible defaults when full feature pipeline isn't wired
                # yet — pick-generator only fires for players the book
                # lists, so "starting_xi" is the right prior for them.
                lineup_confidence="starting_xi",
                recent_form=float(form.get("form_score") or 50) / 100.0,
                minutes_projection=80,
            )
            cal = await get_calibration_factor(
                db,
                league=features.league or "GLOBAL",
                market="p_anytime",
            )
            outputs = compute_probabilities(features, calibration_mult=cal)

            # Stash book price for residual report.
            book_market_key = (
                "anytime" if "anytime" in market_l else
                "first"   if "first goal scorer" in market_l else
                "last"    if "last goal scorer" in market_l else
                "score_or_assist" if "to score or assist" in market_l else
                "anytime"
            )
            await store_prediction(
                db,
                fixture_id=p.get("external_id") or p.get("id"),
                event=event,
                player=player,
                team=player_team or "",
                opponent=opponent or "",
                league=features.league or "",
                outputs=outputs,
                book_prices={book_market_key: p.get("book_odds")},
            )
            n_stored += 1
        except Exception as inner:
            # Per-pick failure must never break the batch.
            logger.debug("gs_v2 shadow capture skipped %s: %s",
                         p.get("id"), inner)
            continue
    if n_stored:
        logger.info("gs_v2 shadow capture: %d soccer goalscorer predictions stored",
                    n_stored)



# ═════════════════════════════════════════════════════════════════════
# Public orchestrator class
# ═════════════════════════════════════════════════════════════════════
class PickRefreshOrchestrator:
    """The single owner of pick-refresh orchestration.

    The heavy lifting lives in :func:`_pipeline_run` (which is the
    verbatim body of the old ``_refresh_picks`` function).  The public
    :meth:`refresh` method wraps it with the typed contract and
    structured error handling.
    """

    def __init__(self, database: Optional[AsyncIOMotorDatabase] = None):
        # Accept an explicit database for tests; default to the shared
        # owner from Phase 3B.
        self._db = database or db

    async def refresh(self, request: PickRefreshRequest) -> PickRefreshResult:
        """Public entry point.  Preserves the behaviour of the old
        ``_refresh_picks`` and returns a structured result."""
        started = time.perf_counter()
        result = PickRefreshResult(
            slate_date=request.slate_date,
            sport_filter=request.sport_filter,
            caller=request.caller,
            reason=request.reason,
        )
        try:
            stored = await _pipeline_run(request.slate_date, request.sport_filter)
            result.published_count = int(stored or 0)
            result.snapshot_count  = int(stored or 0)
            result.success = True
        except Exception as e:
            result.errors.append(f"{type(e).__name__}: {e}")
            logger.exception(
                "PickRefreshOrchestrator failed (caller=%s reason=%s slate=%s sport=%s): %s",
                request.caller, request.reason, request.slate_date,
                request.sport_filter, e,
            )
            raise
        finally:
            result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result


# ── Legacy alias ───────────────────────────────────────────────────
# The body of the old ``_refresh_picks(date_str, sport_filter=None)``
# now lives here as ``_pipeline_run``.  ``server._refresh_picks`` is a
# thin wrapper that instantiates the orchestrator.
async def _pipeline_run(date_str: str, sport_filter: Optional[str] = None) -> int:
    """Run the full refresh pipeline.  Verbatim body of the pre-3F-1
    ``server._refresh_picks``.  Returns the number of picks stored."""
    return await _refresh_picks(date_str, sport_filter=sport_filter)


__all__ = [
    "PickRefreshRequest",
    "PickRefreshResult",
    "PickRefreshOrchestrator",
    "_pipeline_run",
    "_refresh_picks",
    "_dedupe_and_limit_goalscorers",
    "_cap_tennis_totals",
    "_prop_family_key",
    "_atomic_mark_no_bet",
    "_reconcile_player_prop_contradictions",
    "_ensure_csl_elite_picks",
    "_shadow_capture_gs_v2",
]
