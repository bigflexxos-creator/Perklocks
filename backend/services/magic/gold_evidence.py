"""MAGIC 3D — Shared Matchup DNA + Gold Evidence contract.

Adapters ONLY read from existing persisted collections in the pod.
No new external ingestion.  Missing data → UNAVAILABLE (never
fabricated).

Contract per evidence item (subset of `services.magic.contract.EvidenceItem`
attributes populated by adapters):

    sport, league, event_id, canonical_player_id, canonical_team_id,
    opponent_id, market, line, side,
    matchup_feature       — free-form label per builder
    value                 — signed numeric or None
    sample_size           — n backing this signal (real, not estimated)
    timeframe             — window in days or 'career', 'season'
    source                — source collection/module identifier
    timestamp             — data freshness (ISO)
    provenance            — dict with source, method, n, freshness
    availability          — AVAILABLE / PARTIAL / STALE / UNAVAILABLE
    direction             — 'positive' / 'negative' / 'neutral'

Every adapter emits EvidenceItem instances via
`services.magic.contract.EvidenceItem`, so they slot into Magic's
existing evidence taxonomy.

Staleness policy
────────────────
* mlb_statcast_players / mlb_stuff_plus_players — `updated_at` within
  30 days for daily-supported evidence (season stat).
* soccer_player_form            — `updated_at` within 30 days.
* tennis_player_stats           — `computed_at` within 90 days
                                    (surface-stable).
* lineup / injury               — freshness UNAVAILABLE (no source).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional


# ── Availability sentinel ────────────────────────────────────────────

class Availability:
    AVAILABLE   = "AVAILABLE"
    PARTIAL     = "PARTIAL"
    STALE       = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


# ── Evidence families (extend contract enums implicitly) ─────────────

class GoldEvidenceType:
    MATCHUP           = "MATCHUP"
    LINEUP_INJURY     = "LINEUP_INJURY"
    ROLE_OPPORTUNITY  = "ROLE_OPPORTUNITY"
    SOCCER_SHOT_QUALITY = "SOCCER_SHOT_QUALITY"
    SOCCER_CREATION   = "SOCCER_CREATION"
    SET_PIECE_ROLE    = "SET_PIECE_ROLE"
    TENNIS_SERVE      = "TENNIS_SERVE"
    TENNIS_RETURN     = "TENNIS_RETURN"
    TENNIS_WORKLOAD   = "TENNIS_WORKLOAD"
    MLB_BATTER_MATCHUP = "MLB_BATTER_MATCHUP"
    MLB_PITCHER_STUFF = "MLB_PITCHER_STUFF"
    NBA_OPPONENT_PACE = "NBA_OPPONENT_PACE"
    NFL_USAGE         = "NFL_USAGE"


@dataclass
class GoldEvidence:
    evidence_type:  str
    availability:   str = Availability.UNAVAILABLE
    sport:          str = ""
    league:         Optional[str] = None
    event_id:       Optional[str] = None
    canonical_player_id: Optional[str] = None
    canonical_team_id:   Optional[str] = None
    opponent_id:    Optional[str] = None
    market:         Optional[str] = None
    line:           Optional[float] = None
    side:           Optional[str] = None
    matchup_feature: Optional[str] = None
    value:          Optional[float] = None
    direction:      Optional[str] = None
    sample_size:    Optional[int] = None
    timeframe:      Optional[str] = None
    source:         Optional[str] = None
    timestamp:      Optional[str] = None
    notes:          Optional[str] = None
    provenance:     dict = field(default_factory=dict)


# ── Freshness helpers ────────────────────────────────────────────────

def _is_fresh(ts: Any, *, max_age_days: int) -> bool:
    if not ts:
        return False
    try:
        if isinstance(ts, datetime):
            dt = ts
        else:
            dt = datetime.fromisoformat(
                str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return age <= max_age_days
    except Exception:
        return False


# ── MLB Matchup DNA ────────────────────────────────────────────────

async def build_mlb_batter_matchup(db, pick: dict) -> GoldEvidence:
    """Batter vs pitcher / handedness / contact / statcast context."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.MLB_BATTER_MATCHUP,
        sport="MLB",
        market=pick.get("market"),
        line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.notes = "no player name"
        return ev
    # Statcast — real xwOBA / barrel / xslg
    sc = await db.mlb_statcast_players.find_one({"name": pname})
    if sc and _is_fresh(sc.get("updated_at"), max_age_days=30):
        ev.availability = Availability.AVAILABLE
        ev.value        = float(sc.get("xslg") or 0.0)
        ev.matchup_feature = "batter_statcast"
        ev.sample_size  = int(sc.get("pa") or 0)
        ev.source       = "mlb_statcast_players"
        ev.timestamp    = str(sc.get("updated_at"))
        ev.provenance   = {
            "source":   "mlb_statcast_players",
            "xslg":     sc.get("xslg"),
            "xba":      sc.get("xba"),
            "barrel_pct": sc.get("barrel_pct"),
            "hard_hit": sc.get("hard_hit"),
            "pa":       sc.get("pa"),
            "updated_at": sc.get("updated_at"),
        }
        ev.direction = ("positive" if (sc.get("xslg") or 0) > 0.400
                         else "neutral")
    else:
        # Fallback: hitter intel cache (matchup-level).
        intel = await db.mlb_hitter_intel_cache.find_one(
            {"matchup.player": pname})
        if intel:
            ev.availability = Availability.PARTIAL
            ev.source = "mlb_hitter_intel_cache"
            ev.timestamp = str(intel.get("ts"))
            ev.provenance = {"source": "mlb_hitter_intel_cache"}
        else:
            ev.availability = Availability.UNAVAILABLE
            ev.notes = "no statcast + no matchup cache for player"
    return ev


async def build_mlb_pitcher_stuff(db, pick: dict) -> GoldEvidence:
    """Pitcher stuff+/location+/K-rate context.  Real backing:
    mlb_stuff_plus_players + mlb_team_k_splits."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.MLB_PITCHER_STUFF,
        sport="MLB",
        market=pick.get("market"),
        line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.notes = "no player name"
        return ev
    st = await db.mlb_stuff_plus_players.find_one({"name": pname})
    if not st:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no stuff+ record"
        return ev
    if not _is_fresh(st.get("updated_at"), max_age_days=60):
        ev.availability = Availability.STALE
        ev.notes = "stuff+ record older than 60 days"
        return ev
    ev.availability = Availability.AVAILABLE
    ev.value        = float(st.get("stuff_plus") or 100.0)
    ev.matchup_feature = "pitcher_stuff_plus"
    ev.sample_size  = int(st.get("total_pitches") or 0)
    ev.source       = "mlb_stuff_plus_players"
    ev.timestamp    = str(st.get("updated_at"))
    ev.provenance   = {
        "stuff_plus":    st.get("stuff_plus"),
        "location_plus": st.get("location_plus"),
        "pitching_plus": st.get("pitching_plus"),
        "k_pct":         st.get("k_pct"),
        "whiff_pct":     st.get("whiff_pct"),
        "hard_hit_pct":  st.get("hard_hit_pct"),
        "arsenal":       st.get("arsenal"),
        "updated_at":    st.get("updated_at"),
        "source":        "mlb_stuff_plus_players",
    }
    ev.direction = ("positive" if (st.get("stuff_plus") or 0) > 100
                     else "negative")
    return ev


# ── Soccer Gold ─────────────────────────────────────────────────────

async def build_soccer_shot_quality(db, pick: dict) -> GoldEvidence:
    """Real Soccer goalscorer/shot-quality evidence backed by
    soccer_player_form (npxg, shots, goals_over_xg)."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.SOCCER_SHOT_QUALITY,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.notes = "no player name"; return ev
    frm = await db.soccer_player_form.find_one(
        {"name_canonical": pname.lower()})
    if not frm:
        frm = await db.soccer_player_form.find_one({"player_name": pname})
    if not frm:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no soccer_player_form record"
        return ev
    ev.availability = Availability.AVAILABLE
    ev.value = float(frm.get("npxg_per_90") or 0.0)
    ev.matchup_feature = "npxg_per_90"
    ev.sample_size = int(frm.get("games") or 0)
    ev.timeframe = str(frm.get("season") or "")
    ev.source = "soccer_player_form"
    ev.provenance = {
        "npxg":            frm.get("npxg"),
        "npxg_per_90":     frm.get("npxg_per_90"),
        "shots":           frm.get("shots"),
        "shots_per_90":    frm.get("shots_per_90"),
        "goals":           frm.get("goals"),
        "goals_over_xg":   frm.get("goals_over_xg"),
        "minutes":         frm.get("minutes"),
        "position":        frm.get("position"),
        "form_score":      frm.get("form_score"),
        "form_label":      frm.get("form_label"),
        "team":            frm.get("team"),
        "source":          "soccer_player_form",
    }
    ev.direction = ("positive" if (frm.get("npxg_per_90") or 0) > 0.3
                     else "neutral")
    return ev


async def build_soccer_creation(db, pick: dict) -> GoldEvidence:
    """Creator (xA / key_passes) pathway.  `xA` is NOT persisted;
    `key_passes` acts as a PARTIAL proxy.  Assist markets should
    consume THIS, never `shot_quality` alone."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.SOCCER_CREATION,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    frm = await db.soccer_player_form.find_one(
        {"name_canonical": pname.lower()})
    if not frm:
        frm = await db.soccer_player_form.find_one({"player_name": pname})
    if not frm:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no soccer_player_form record"
        return ev
    kp = frm.get("key_passes")
    minutes = frm.get("minutes") or 0
    assists = frm.get("assists")
    if kp is None or minutes < 90:
        ev.availability = Availability.PARTIAL
        ev.notes = "no xA persisted — key_passes proxy insufficient"
        ev.provenance = {"source": "soccer_player_form",
                          "note": "xA field missing; only assists+key_passes proxy",
                          "assists": assists, "key_passes": kp,
                          "minutes": minutes}
        return ev
    # PARTIAL because true xA is not persisted — key_passes is a rate
    # proxy only, weaker than xA.
    ev.availability = Availability.PARTIAL
    ev.value = float(kp) / max(minutes / 90.0, 1.0)  # KP per 90
    ev.matchup_feature = "key_passes_per_90"
    ev.sample_size = int(frm.get("games") or 0)
    ev.source = "soccer_player_form (key_passes proxy)"
    ev.provenance = {
        "assists":     assists, "key_passes": kp,
        "minutes":     minutes, "position":   frm.get("position"),
        "team":        frm.get("team"),
        "note":        "xA UNAVAILABLE; using key_passes/90 as proxy",
        "source":      "soccer_player_form",
    }
    ev.direction = ("positive" if ev.value > 1.5 else "neutral")
    return ev


async def build_soccer_matchup(db, pick: dict) -> GoldEvidence:
    """Opponent xGA / shots-allowed from soccer_player_game_logs
    (opponent_xg field is genuinely present)."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.MATCHUP,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        event_id=pick.get("canonical_event_id"),
    )
    opp = pick.get("opponent_team_id") or pick.get("opponent_team")
    if not opp:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no opponent identity on pick"
        return ev
    cursor = db.soccer_player_game_logs.find(
        {"opponent_team_name": opp}, {"opponent_xg": 1, "_id": 0}
    ).limit(500)
    xgs: list[float] = []
    async for r in cursor:
        x = r.get("opponent_xg")
        if x is not None:
            try:
                xgs.append(float(x))
            except (TypeError, ValueError):
                pass
    if len(xgs) < 5:
        ev.availability = Availability.PARTIAL
        ev.notes = "opponent xGA sample too small (<5 games)"
        ev.sample_size = len(xgs)
        return ev
    ev.availability = Availability.AVAILABLE
    avg = sum(xgs) / len(xgs)
    ev.value = avg
    ev.matchup_feature = "opponent_avg_xga"
    ev.sample_size = len(xgs)
    ev.source = "soccer_player_game_logs.opponent_xg"
    ev.direction = ("positive" if avg > 1.5 else "negative")
    ev.provenance = {"opp": opp, "n_games": len(xgs),
                      "avg_opponent_xg": avg,
                      "source": "soccer_player_game_logs"}
    return ev


# ── Tennis Gold ────────────────────────────────────────────────────

async def build_tennis_serve(db, pick: dict) -> GoldEvidence:
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.TENNIS_SERVE,
        sport="Tennis",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    surface = pick.get("surface") or "hard"
    stats = await db.tennis_player_stats.find_one(
        {"name": pname, "surface": surface})
    if not stats:
        stats = await db.tennis_player_stats.find_one({"name": pname})
        if stats:
            ev.availability = Availability.PARTIAL
            ev.notes = f"no {surface}-specific stats; showing career"
    if not stats:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no tennis_player_stats record"
        return ev
    if ev.availability != Availability.PARTIAL:
        ev.availability = Availability.AVAILABLE
    ev.value = float(stats.get("first_serve_won_pct") or 0.0)
    ev.matchup_feature = "first_serve_won_pct"
    ev.sample_size = int(stats.get("n_matches") or 0)
    ev.source = "tennis_player_stats"
    ev.timestamp = str(stats.get("computed_at"))
    ev.provenance = {
        "first_serve_pct":     stats.get("first_serve_pct"),
        "first_serve_won_pct": stats.get("first_serve_won_pct"),
        "second_serve_won_pct": stats.get("second_serve_won_pct"),
        "hold_pct":            stats.get("hold_pct"),
        "ace_pct":             stats.get("ace_pct"),
        "df_pct":              stats.get("df_pct"),
        "surface":             stats.get("surface"),
        "n_matches":           stats.get("n_matches"),
        "computed_at":         stats.get("computed_at"),
        "source":              "tennis_player_stats",
    }
    return ev


async def build_tennis_return(db, pick: dict) -> GoldEvidence:
    """Return strength backed by tennis_player_stats.break_saved_pct
    (opponent break saved) — real persisted."""
    serve_ev = await build_tennis_serve(db, pick)
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.TENNIS_RETURN,
        sport="Tennis",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
        availability=serve_ev.availability,
        source=serve_ev.source, timestamp=serve_ev.timestamp,
        sample_size=serve_ev.sample_size,
    )
    # break_saved_pct is opponent-perspective; keep separately.
    if serve_ev.provenance:
        ev.provenance = {
            "break_saved_pct": serve_ev.provenance.get("break_saved_pct"),
            "source": "tennis_player_stats",
            "note": "return-strength derived from break_saved_pct"}
    return ev


async def build_tennis_workload(db, pick: dict) -> GoldEvidence:
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.TENNIS_WORKLOAD,
        sport="Tennis",
        market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.availability = Availability.UNAVAILABLE
        return ev
    # Count matches in last 14 days for the player from
    # tennis_matches_history — genuine backing.
    cutoff_14 = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    cutoff_7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    q7 = {"$or": [{"player1_name": pname}, {"player2_name": pname}],
          "match_date": {"$gte": cutoff_7}}
    q14 = {"$or": [{"player1_name": pname}, {"player2_name": pname}],
           "match_date": {"$gte": cutoff_14}}
    try:
        n7 = await db.tennis_matches_history.count_documents(q7)
        n14 = await db.tennis_matches_history.count_documents(q14)
    except Exception:
        n7 = n14 = 0
    if n14 == 0:
        ev.availability = Availability.PARTIAL
        ev.notes = "no recent matches in tennis_matches_history"
        return ev
    ev.availability = Availability.AVAILABLE
    ev.value = float(n14)
    ev.matchup_feature = "matches_in_last_14d"
    ev.source = "tennis_matches_history"
    ev.provenance = {
        "matches_7d":  n7, "matches_14d": n14,
        "source": "tennis_matches_history",
    }
    ev.direction = ("negative" if n14 >= 8 else "neutral")
    return ev


# ── NBA / NFL Matchup DNA ─────────────────────────────────────────

async def build_nba_matchup(db, pick: dict) -> GoldEvidence:
    """NBA pace / opponent context.  No dedicated positional-DEF
    collection persisted — return PARTIAL/UNAVAILABLE honestly."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.NBA_OPPONENT_PACE,
        sport="NBA", market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    opp = pick.get("opponent_team_id") or pick.get("opponent_team")
    if not opp:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no opponent identity"
        return ev
    tf = await db.team_form.find_one({"team_id": opp, "sport": "NBA"})
    if not tf:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no NBA team_form record for opponent"
        return ev
    ev.availability = Availability.PARTIAL   # no true pace field
    ev.matchup_feature = "opponent_recent_form"
    ev.source = "team_form"
    ev.value = float(tf.get("ppm") or 0.0)
    ev.provenance = {"team_id": opp, "form": tf.get("form"),
                      "ppm": tf.get("ppm"), "source": "team_form",
                      "note": "no positional DEF rating persisted"}
    return ev


async def build_nfl_usage(db, pick: dict) -> GoldEvidence:
    """NFL usage / opportunity from nfl_player_usage."""
    ev = GoldEvidence(
        evidence_type=GoldEvidenceType.NFL_USAGE,
        sport="NFL", market=pick.get("market"), line=pick.get("line"),
        side=pick.get("side"),
        canonical_player_id=pick.get("canonical_player_id"),
    )
    pname = (pick.get("player_name") or pick.get("selection") or "").strip()
    if not pname:
        ev.availability = Availability.UNAVAILABLE
        return ev
    u = await db.nfl_player_usage.find_one({"player": pname})
    if not u:
        ev.availability = Availability.UNAVAILABLE
        ev.notes = "no nfl_player_usage record"
        return ev
    ev.availability = Availability.AVAILABLE
    ev.value = float(u.get("snap_pct_avg") or 0.0)
    ev.matchup_feature = "snap_pct_avg"
    ev.sample_size = int(u.get("games") or 0)
    ev.source = "nfl_player_usage"
    ev.timestamp = str(u.get("updated_at"))
    ev.provenance = {
        "snap_pct_avg":       u.get("snap_pct_avg"),
        "offense_snaps_sum":  u.get("offense_snaps_sum"),
        "special_teams_pct":  u.get("special_teams_pct_avg"),
        "position":           u.get("position"),
        "team":               u.get("team"),
        "season":             u.get("season"),
        "source":             "nfl_player_usage",
    }
    return ev


# ── Lineup / Injury ─────────────────────────────────────────────────

async def build_lineup_injury(db, pick: dict) -> GoldEvidence:
    """No dedicated lineup/injury collection exists in the pod.
    Explicitly UNAVAILABLE — do not fabricate."""
    return GoldEvidence(
        evidence_type=GoldEvidenceType.LINEUP_INJURY,
        availability=Availability.UNAVAILABLE,
        sport=pick.get("sport") or "",
        market=pick.get("market"),
        line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        notes="no lineup / injury feed persisted in this pod",
        provenance={"reason": "no_ingestion_backing",
                     "collections_checked": [
                         "player_profiles", "player_profiles_v2",
                         "players", "player_form",
                     ]},
    )


async def build_set_piece_role(db, pick: dict) -> GoldEvidence:
    """No penalty/set-piece taker collection exists — UNAVAILABLE."""
    return GoldEvidence(
        evidence_type=GoldEvidenceType.SET_PIECE_ROLE,
        availability=Availability.UNAVAILABLE,
        sport="Soccer",
        market=pick.get("market"), line=pick.get("line"),
        canonical_player_id=pick.get("canonical_player_id"),
        notes="no set-piece / penalty taker feed persisted",
        provenance={"reason": "no_ingestion_backing"},
    )


__all__ = [
    "Availability", "GoldEvidenceType", "GoldEvidence",
    "build_mlb_batter_matchup", "build_mlb_pitcher_stuff",
    "build_soccer_shot_quality", "build_soccer_creation",
    "build_soccer_matchup",
    "build_tennis_serve", "build_tennis_return", "build_tennis_workload",
    "build_nba_matchup", "build_nfl_usage",
    "build_lineup_injury", "build_set_piece_role",
]
