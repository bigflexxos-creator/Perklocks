"""Tennis sport adapter — Elo (overall + surface-specific) + form."""
from __future__ import annotations

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature


class TennisAdapter(SportAdapter):
    SPORT = "TENNIS"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        feats: list[EvidenceFeature] = []
        tx = pick.get("tennis_extra") or {}

        # ── Overall Elo gap ──
        if isinstance(tx, dict):
            elo_gap = tx.get("elo_diff") or tx.get("elo_gap")
            if elo_gap is not None:
                feats.append(EvidenceFeature(
                    name="Overall Elo gap", category="form",
                    value=round(float(elo_gap), 0),
                    sample_size=int(tx.get("player_matches") or 30),
                    lookback_days=365,
                    source="tennis_players (live ledger)",
                    importance=0.85,
                    reason=f"Elo gap = {float(elo_gap):+.0f}",
                ))

            # ── Surface-specific Elo gap (HARD / CLAY / GRASS) ──
            surface = (tx.get("surface") or pick.get("surface") or "").upper()
            if surface and tx.get("surface_elo_diff") is not None:
                feats.append(EvidenceFeature(
                    name=f"{surface} surface Elo gap",
                    category="form",
                    value=round(float(tx["surface_elo_diff"]), 0),
                    sample_size=int(tx.get("surface_matches") or 15),
                    lookback_days=730,
                    source="tennis_players (surface split)",
                    importance=0.90,
                    reason=f"{surface} Elo gap = {float(tx['surface_elo_diff']):+.0f}",
                ))

            # ── Serve / Return hold % ──
            sh = tx.get("serve_hold_pct")
            if sh is not None:
                feats.append(EvidenceFeature(
                    name="Serve hold %", category="form",
                    value=round(float(sh), 1),
                    sample_size=int(tx.get("serve_games") or 100),
                    lookback_days=180,
                    source="tennis_players",
                    importance=0.70,
                    reason=f"Holds {float(sh):.0f}% of service games",
                ))
            rh = tx.get("return_break_pct")
            if rh is not None:
                feats.append(EvidenceFeature(
                    name="Return break %", category="form",
                    value=round(float(rh), 1),
                    sample_size=int(tx.get("return_games") or 100),
                    lookback_days=180,
                    source="tennis_players",
                    importance=0.65,
                    reason=f"Breaks {float(rh):.0f}% of return games",
                ))

            # ── 30-day form W/L ──
            form_w = tx.get("form_30d_wins")
            form_l = tx.get("form_30d_losses")
            if form_w is not None and form_l is not None:
                n = int(form_w) + int(form_l)
                if n > 0:
                    feats.append(EvidenceFeature(
                        name="30-day form W/L", category="form",
                        value=f"{int(form_w)}−{int(form_l)}",
                        sample_size=n, lookback_days=30,
                        source="espn_settlement (live ledger)",
                        importance=0.55,
                        reason=f"Recent form: {int(form_w)}−{int(form_l)} over {n} matches",
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


register(TennisAdapter())
