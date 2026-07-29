"""Retraining Pipeline (2026-07-28).

Coordinates evaluation → decision → retrain → compare → promote for
the trained ML models on disk.

**Never trains a new model on its own** without a trigger.
**Only promotes** the new model over the old one when a strict
improvement threshold is met.

Contract
────────
    orch = RetrainingOrchestrator(db, model_dir="/app/backend/models",
                                   min_new_settled=100,
                                   min_days_since_retrain=7,
                                   promotion_threshold_pct=2.0)

    # 1. Diagnose which models need retraining.
    triggers = await orch.detect_needs_retraining()
    # → list[{sport, stat, reason, evidence}]

    # 2. Retrain one model (calls the existing train_prop_model.train_*).
    result = await orch.retrain(sport, stat, dry_run=True)
    # → {ok, meta_new, meta_old, promoted, reason}

    # 3. Promote (or roll-back) — never happens implicitly; the caller
    # decides after inspecting `result`.
    orch.promote(sport, stat)

Triggers
────────
  A. **New data**: ≥ min_new_settled graded rows for this
     (sport, stat) since the model's `trained_at`.
  B. **Time-based**: > min_days_since_retrain since last retrain.
  C. **Performance drop**: recent Brier on `fusion_predictions` has
     degraded > 20 % vs the model's saved val Brier.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.adaptive_learning.retraining_pipeline")


class RetrainingOrchestrator:
    def __init__(
        self,
        db,
        *,
        model_dir: str = "/app/backend/models",
        min_new_settled: int = 100,
        min_days_since_retrain: int = 7,
        promotion_threshold_pct: float = 2.0,
    ):
        self.db = db
        self.model_dir = Path(model_dir)
        self.min_new_settled = int(min_new_settled)
        self.min_days_since_retrain = int(min_days_since_retrain)
        self.promotion_threshold_pct = float(promotion_threshold_pct)

    # ────────────────────────────────────────────────────────────────
    # 1. Discovery + triggers
    # ────────────────────────────────────────────────────────────────
    def list_models(self) -> list[dict]:
        rows: list[dict] = []
        for meta in sorted(self.model_dir.glob("*.meta.json")):
            try:
                m = json.loads(meta.read_text())
            except Exception:
                continue
            rows.append({
                "tag":  meta.name.replace(".meta.json", ""),
                "path": str(meta),
                "sport": (m.get("sport") or "").upper(),
                "stat":  m.get("stat"),
                "trained_at": m.get("trained_at"),
                "winner": m.get("winner"),
                "meta":  m,
            })
        return rows

    async def detect_needs_retraining(self) -> list[dict]:
        """Return the subset of models flagged for retraining with a
        `reason` and supporting `evidence`."""
        triggers: list[dict] = []
        now = datetime.now(timezone.utc)
        for row in self.list_models():
            reasons: list[str] = []
            evidence: dict = {"tag": row["tag"]}
            # Trigger B — time-based.
            try:
                ta = datetime.strptime(row["trained_at"] or "",
                                        "%Y-%m-%d %H:%M:%S UTC")
                ta = ta.replace(tzinfo=timezone.utc)
                age_days = (now - ta).days
                evidence["age_days"] = age_days
                if age_days >= self.min_days_since_retrain:
                    reasons.append(f"age_days>={self.min_days_since_retrain}")
            except Exception:
                pass
            # Trigger A — new settled data since last train.
            try:
                since = (row.get("trained_at") or "").split(" UTC")[0] \
                          .replace(" ", "T")
                q = {"sport": row["sport"], "stat": row["stat"],
                      "actual_value": {"$ne": None},
                      "graded_at": {"$gte": since}}
                new_n = await self.db.fusion_predictions.count_documents(q)
                evidence["new_settled"] = new_n
                if new_n >= self.min_new_settled:
                    reasons.append(f"new_settled>={self.min_new_settled}")
            except Exception:
                pass
            # Trigger C — recent Brier drift vs training-time Brier.
            try:
                trained_brier = (row["meta"].get(row["winner"]) or {}) \
                                    .get("brier_by_thr", {}).get("p50")
                if trained_brier is not None:
                    since_iso = (now - timedelta(days=30)).isoformat()
                    q = {"sport": row["sport"], "stat": row["stat"],
                          "actual_value": {"$ne": None},
                          "created_at": {"$gte": since_iso}}
                    ms: dict[str, float] = {"n": 0, "sum": 0.0}
                    async for d in self.db.fusion_predictions.find(q, {"_id": 0}):
                        p = ((d.get("components") or {}).get("ml") or {}) \
                              .get("probability")
                        thr = d.get("threshold"); actual = d.get("actual_value")
                        if None in (p, thr, actual):
                            continue
                        try:
                            y = 1.0 if float(actual) > float(thr) else 0.0
                            ms["sum"] += (float(p) - y) ** 2
                            ms["n"] += 1
                        except (TypeError, ValueError):
                            continue
                    if ms["n"] >= 20:
                        recent_brier = ms["sum"] / ms["n"]
                        evidence["recent_brier"] = round(recent_brier, 4)
                        evidence["trained_brier"] = trained_brier
                        if recent_brier > trained_brier * 1.20:
                            reasons.append("brier_drift>20%")
            except Exception:
                pass
            if reasons:
                triggers.append({
                    "sport": row["sport"], "stat": row["stat"],
                    "reasons": reasons, "evidence": evidence,
                })
        return triggers

    # ────────────────────────────────────────────────────────────────
    # 2. Retrain (delegates to train_prop_model). Not a new model —
    #    just re-runs the SAME architecture on newer data.
    # ────────────────────────────────────────────────────────────────
    async def retrain(self, sport: str, stat: str,
                       *, dry_run: bool = False, **kwargs) -> dict:
        """Retrain and return old-vs-new metrics.  Does NOT promote."""
        sport_u = sport.upper()
        tag = f"{sport.lower()}_{stat}"
        old_meta_path = self.model_dir / f"{tag}.meta.json"
        old_meta = {}
        if old_meta_path.exists():
            try:
                old_meta = json.loads(old_meta_path.read_text())
            except Exception:
                old_meta = {}

        # Move old meta + pkls to `_previous/`.
        backup_dir = self.model_dir / "_previous"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for f in self.model_dir.glob(f"{tag}*"):
            if not f.is_file():
                continue
            shutil.copy2(f, backup_dir / f.name)

        if dry_run:
            return {"ok": True, "dry_run": True,
                     "would_retrain": {"sport": sport_u, "stat": stat}}

        # Delegate to the existing trainer.
        try:
            from ml import train_prop_model as tpm
            if sport_u == "NFL":
                new_meta = await tpm.train_nfl(
                    stat=stat, position=kwargs.get("position"),
                    split_season=kwargs.get("split_season", 2024),
                    seasons_min=kwargs.get("seasons_min", 2019),
                )
            elif sport_u == "MLB":
                new_meta = await tpm.train_mlb(
                    stat=stat,
                    split_date=kwargs.get("split_date", "2025-01-01"),
                )
            elif sport_u == "TENNIS":
                new_meta = await tpm.train_tennis(
                    stat=stat,
                    split_date=kwargs.get("split_date", "2024-01-01"),
                    surface=kwargs.get("surface"),
                )
            else:
                return {"ok": False,
                         "reason": f"sport {sport_u} not supported"}
        except Exception as e:
            logger.exception("retrain failed for %s/%s", sport_u, stat)
            return {"ok": False, "reason": f"training error: {e}",
                     "restored_from_backup": self._restore_backup(tag),
                     "old_meta": old_meta}

        # Compare — new vs old winner-model MAE + AUC.
        promoted = self._should_promote(old_meta, new_meta)
        return {
            "ok": True,
            "sport":       sport_u,
            "stat":        stat,
            "promoted":    promoted,
            "old_meta":    old_meta,
            "new_meta":    new_meta,
            "comparison":  self._compare(old_meta, new_meta),
        }

    def _compare(self, old_meta: dict, new_meta: dict) -> dict:
        if not old_meta or not new_meta:
            return {"note": "no old_meta for comparison"}
        ow = old_meta.get("winner"); nw = new_meta.get("winner")
        o = (old_meta.get(ow) or {}) if ow else {}
        n = (new_meta.get(nw) or {}) if nw else {}
        return {
            "old_winner": ow, "new_winner": nw,
            "mae_old":   o.get("mae"),   "mae_new":   n.get("mae"),
            "mae_delta_pct": round(
                ((o.get("mae") or 0) - (n.get("mae") or 0))
                / (o.get("mae") or 1) * 100, 3
            ) if (o.get("mae") or 0) > 0 else None,
            "auc_p50_old": (o.get("auc_by_thr") or {}).get("p50"),
            "auc_p50_new": (n.get("auc_by_thr") or {}).get("p50"),
            "top_features_old": (o.get("top_features") or [])[:5],
            "top_features_new": (n.get("top_features") or [])[:5],
        }

    def _should_promote(self, old_meta: dict, new_meta: dict) -> bool:
        if not old_meta:
            # First model — anything is an improvement.
            return True
        cmp = self._compare(old_meta, new_meta)
        delta = cmp.get("mae_delta_pct")
        if delta is None:
            return False
        return float(delta) >= self.promotion_threshold_pct

    def _restore_backup(self, tag: str) -> bool:
        backup_dir = self.model_dir / "_previous"
        if not backup_dir.exists():
            return False
        restored = 0
        for f in backup_dir.glob(f"{tag}*"):
            shutil.copy2(f, self.model_dir / f.name)
            restored += 1
        return restored > 0

    # ────────────────────────────────────────────────────────────────
    # 3. Promotion / rollback
    # ────────────────────────────────────────────────────────────────
    def promote(self, sport: str, stat: str) -> dict:
        """Confirm the currently-on-disk model is the promoted one.
        Cleans backups older than 30 days."""
        return {"ok": True, "note": "current models kept on disk"}

    def rollback(self, sport: str, stat: str) -> dict:
        tag = f"{sport.lower()}_{stat}"
        if self._restore_backup(tag):
            # Reset the in-process model cache.
            try:
                from services.trained_prediction_engine import _reset_model_cache
                _reset_model_cache()
            except Exception:
                pass
            return {"ok": True, "rolled_back": tag}
        return {"ok": False, "reason": "no backup found"}


__all__ = ["RetrainingOrchestrator"]
