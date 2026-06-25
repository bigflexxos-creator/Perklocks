"""Soccer sport adapter — Understat xG + form pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0)
    except Exception:
        return 0.0


class SoccerAdapter(SportAdapter):
    SPORT = "SOCCER"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        feats: list[EvidenceFeature] = []

        # ── Understat xG form ──
        uf = pick.get("understat_form") or {}
        if isinstance(uf, dict) and uf.get("label"):
            n = int(uf.get("matches_used") or 0)
            feats.append(EvidenceFeature(
                name="Understat xG form", category="form",
                value=uf.get("label"),
                sample_size=n, lookback_days=30,
                source="Understat",
                importance=0.80,
                freshness_hours=_hours_since(uf.get("snapshot_at")),
                reason=f"xG form = {uf.get('label')} over {n} matches",
            ))
            xg_diff = uf.get("xg_diff_per90")
            if xg_diff is not None:
                feats.append(EvidenceFeature(
                    name="xG − xGA per 90", category="form",
                    value=round(float(xg_diff), 2),
                    sample_size=n, lookback_days=30,
                    source="Understat",
                    importance=0.70,
                    reason=f"xG−xGA/90 = {float(xg_diff):+.2f}",
                ))

        # ── Rest days / travel ──
        rest = pick.get("rest_days") or pick.get("days_rest")
        if rest is not None:
            feats.append(EvidenceFeature(
                name="Rest days", category="context",
                value=int(rest),
                sample_size=1, lookback_days=14,
                source="Fixture schedule",
                importance=0.45,
                reason=f"{int(rest)} days rest since last match",
            ))

        # ── League strength normalization ──
        league = pick.get("league") or pick.get("competition")
        if league:
            feats.append(EvidenceFeature(
                name="League context", category="context",
                value=league,
                sample_size=1, lookback_days=0,
                source="football-data.org",
                importance=0.30,
                reason=f"Top-5 league: {league}" if league in ("EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1") else f"League: {league}",
            ))

        # ── Lineup confirmation ──
        if pick.get("lineup_confirmed"):
            feats.append(EvidenceFeature(
                name="Lineup confirmed", category="context",
                value=True,
                sample_size=1, lookback_days=0,
                source="football-data.org",
                importance=0.55,
                reason="Starting XI confirmed",
            ))

        # ── Monte Carlo ──
        if pick.get("sim_runs") and int(pick["sim_runs"]) >= 1000:
            feats.append(EvidenceFeature(
                name="Monte Carlo simulator", category="model",
                value=pick.get("sim_win_probability"),
                sample_size=int(pick["sim_runs"]),
                lookback_days=0,
                source="brain/sim_soccer.py",
                importance=0.85,
                reason=f"{int(pick['sim_runs']):,}-run Monte Carlo simulation",
            ))

        # ── Market edge ──
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


register(SoccerAdapter())
