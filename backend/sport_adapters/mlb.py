"""MLB sport adapter — deeper feature pull than the generic Phase 1 extractor.

Pulls from data the codebase already collects upstream:
  • pitcher rolling K/9, BB/9, last-3/5/10 starts → from player_profiles_v2
  • batter rolling AVG/OPS/ISO → from player_intel + h2h
  • park factor → from event/venue metadata where present
  • weather overlay → from pick.weather if attached
  • bullpen fatigue → from pick.bullpen_fatigue_score if attached
  • lineup confirmation → from pick.lineup_confirmed flag
  • handedness → from pick.pitcher_throws / pick.batter_bats
  • monte carlo → from brain/sim_mlb output already on the pick

Features that aren't yet plumbed return None gracefully — the
Evidence Governor naturally down-weights the pick when its features
are sparse. That's the design.
"""
from __future__ import annotations

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature


class MLBAdapter(SportAdapter):
    SPORT = "MLB"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        feats: list[EvidenceFeature] = []
        market = (pick.get("market") or "").lower()

        # ── PITCHER props (Strikeouts, Outs Recorded, Hits Allowed) ──
        is_pitcher_prop = (
            "strikeout" in market or "outs recorded" in market
            or "hits allowed" in market or "earned runs" in market
        )
        if is_pitcher_prop:
            prof = pick.get("pitcher_profile") or pick.get("player_form") or {}
            k9 = prof.get("k9_rolling") or prof.get("k_per_9")
            if k9 is not None:
                feats.append(EvidenceFeature(
                    name="Pitcher rolling K/9", category="form",
                    value=round(float(k9), 2),
                    sample_size=int(prof.get("starts_sample") or 10),
                    lookback_days=45,
                    source="MLB Stats API",
                    importance=0.85,
                    reason=f"Rolling K/9 = {float(k9):.2f} over last {int(prof.get('starts_sample') or 10)} starts",
                ))
            bb9 = prof.get("bb9_rolling") or prof.get("bb_per_9")
            if bb9 is not None:
                feats.append(EvidenceFeature(
                    name="Pitcher rolling BB/9", category="form",
                    value=round(float(bb9), 2),
                    sample_size=int(prof.get("starts_sample") or 10),
                    lookback_days=45,
                    source="MLB Stats API",
                    importance=0.55,
                    reason=f"BB/9 = {float(bb9):.2f}",
                ))

        # ── BATTER props ──
        is_batter_prop = (
            "hits" in market or "home run" in market
            or "total bases" in market or "rbi" in market
            or "hits + runs + rbi" in market
        )
        if is_batter_prop:
            prof = pick.get("batter_profile") or pick.get("player_form") or {}
            avg = prof.get("avg_rolling") or prof.get("hit_rate")
            if avg is not None:
                feats.append(EvidenceFeature(
                    name="Batter rolling AVG", category="form",
                    value=round(float(avg), 3),
                    sample_size=int(prof.get("pa_sample") or prof.get("n_picks") or 20),
                    lookback_days=30,
                    source="MLB Stats API",
                    importance=0.80,
                    reason=f"Rolling AVG = {float(avg):.3f} over {int(prof.get('pa_sample') or 20)} PA",
                ))
            ops = prof.get("ops_rolling") or prof.get("ops")
            if ops is not None:
                feats.append(EvidenceFeature(
                    name="Batter rolling OPS", category="form",
                    value=round(float(ops), 3),
                    sample_size=int(prof.get("pa_sample") or 20),
                    lookback_days=30,
                    source="MLB Stats API",
                    importance=0.75,
                    reason=f"OPS = {float(ops):.3f}",
                ))
            # Pitcher hand split for the batter.
            split = prof.get("vs_hand_split") or {}
            opp_hand = pick.get("pitcher_throws") or pick.get("opp_pitcher_hand")
            split_avg = split.get(opp_hand) if isinstance(split, dict) and opp_hand else None
            if split_avg is not None:
                feats.append(EvidenceFeature(
                    name=f"Batter vs {opp_hand}HP split", category="matchup",
                    value=round(float(split_avg), 3),
                    sample_size=int(split.get(f"{opp_hand}_pa") or 0),
                    lookback_days=730,
                    source="MLB Stats API",
                    importance=0.65,
                    reason=f"AVG vs {opp_hand}HP = {float(split_avg):.3f}",
                ))

        # ── H2H (batter vs starting pitcher) ──
        h2h = pick.get("pitcher_h2h") or {}
        if isinstance(h2h, dict) and h2h.get("plate_appearances"):
            pa = int(h2h.get("plate_appearances") or 0)
            feats.append(EvidenceFeature(
                name="Pitcher↔Batter H2H history",
                category="matchup",
                value=h2h.get("avg") or h2h.get("ops"),
                sample_size=pa,
                lookback_days=730,
                source="MLB Stats API",
                importance=0.55 if pa < 10 else 0.70,
                reason=f"{pa} career PA between this batter ↔ starting pitcher",
            ))

        # ── Park factor ──
        park = pick.get("park_factor") or pick.get("venue_factor")
        if park is not None:
            feats.append(EvidenceFeature(
                name="Park factor", category="context",
                value=round(float(park), 2),
                sample_size=3000, lookback_days=365,
                source="Statcast / venue index",
                importance=0.40,
                reason=f"Venue factor = {float(park):.2f}",
            ))

        # ── Weather overlay ──
        weather = pick.get("weather") or {}
        if isinstance(weather, dict) and weather.get("summary"):
            feats.append(EvidenceFeature(
                name="Weather impact", category="context",
                value=weather.get("summary"),
                sample_size=1, lookback_days=0,
                source="Weather API",
                importance=0.35,
                reason=f"Weather: {weather.get('summary')}",
            ))

        # ── Bullpen fatigue ──
        bp = pick.get("bullpen_fatigue_score")
        if bp is not None:
            feats.append(EvidenceFeature(
                name="Bullpen fatigue", category="context",
                value=round(float(bp), 2),
                sample_size=int(pick.get("bullpen_sample") or 7),
                lookback_days=7,
                source="MLB Stats API",
                importance=0.50,
                reason=f"Bullpen fatigue score = {float(bp):.2f}",
            ))

        # ── Lineup confirmation ──
        if pick.get("lineup_confirmed"):
            feats.append(EvidenceFeature(
                name="Lineup confirmed", category="context",
                value=True,
                sample_size=1, lookback_days=0,
                source="MLB Stats API",
                importance=0.55,
                reason="Starting lineup officially posted",
            ))

        # ── Monte Carlo simulator output ──
        if pick.get("sim_runs") and int(pick["sim_runs"]) >= 1000:
            feats.append(EvidenceFeature(
                name="Monte Carlo simulator", category="model",
                value=pick.get("sim_win_probability"),
                sample_size=int(pick["sim_runs"]),
                lookback_days=0,
                source="brain/sim_mlb.py",
                importance=0.85,
                reason=f"{int(pick['sim_runs']):,}-run Monte Carlo",
                explanation_text=(
                    f"Monte Carlo over {int(pick['sim_runs']):,} runs returns "
                    f"{pick.get('sim_win_probability'):.1f}% win probability"
                    if pick.get("sim_win_probability") is not None else None
                ),
            ))

        # ── Universal market edge — always include if present ──
        # Prefer edge_percent_raw if Phase-3 shrinkage has been applied — without
        # this, repeated govern_pick calls feed the SHRUNK edge back into the
        # feature builder → lower importance → lower evidence_score → more
        # shrinkage next pass → compounding collapse to market consensus.
        edge = pick.get("edge_percent_raw")
        if edge is None:
            edge = pick.get("edge_percent")
        if edge is not None:
            try:
                e = float(edge)
                feats.append(EvidenceFeature(
                    name="Closing-line market edge", category="market",
                    value=e, sample_size=10, lookback_days=0,
                    source="The Odds API",
                    importance=min(1.0, abs(e) / 8.0),
                    reason=f"{e:+.1f}% edge vs market consensus",
                ))
            except Exception:
                pass

        return feats


register(MLBAdapter())
