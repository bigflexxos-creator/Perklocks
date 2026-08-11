"""Universal Reachability Standard (§5).

Builds a per-pick ``ReachabilityReport`` that traces the complete
applicable production chain:

    REAL / AUTHORITATIVE SOURCE
        → VALIDATED
        → CANONICAL IDENTITY
        → CURRENT EVENT / ROSTER VALIDATION where applicable
        → REAL MARKET / LINE where required
        → REQUIRED EVIDENCE
        → PRODUCTION MODEL / ENGINE CONSUMPTION
        → GENERATED
        → CANONICAL PUBLISHED
        → VISIBLE
        → FROZEN
        → AUTHORITATIVELY SETTLED
        → MEASURABLE

Per §5 the report also preserves INDEPENDENT status for each
downstream consumer (LOCKS / ROLLOVER / PARLAY / HISTORY /
ANALYTICS / MAGIC / SPECIALIZED_ANALYTICS) — a generic PASS is
never collapsed across all consumers.

This module is read-only.  It inspects an already-persisted pick
document (plus optional adjacent evidence like the pregame
snapshot / settlement record) and reports what can be proven.  It
NEVER manufactures PASS from module existence (§10, §14).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .vocabulary import (
    ProductionStage,
    StageStatus,
    DropReason,
    stage_status_pass,
    stage_status_fail,
    stage_status_unknown,
    stage_status_not_applicable,
    is_terminal,
)


class ConsumerSurface(str, enum.Enum):
    """The distinct downstream consumers that must each preserve
    independent eligibility state (§5).  Do NOT collapse."""
    LOCKS                   = "LOCKS"
    ROLLOVER                = "ROLLOVER"
    PARLAY                  = "PARLAY"
    HISTORY                 = "HISTORY"
    ANALYTICS               = "ANALYTICS"
    MAGIC                   = "MAGIC"
    SPECIALIZED_ANALYTICS   = "SPECIALIZED_ANALYTICS"


# Markets that are game-level (as opposed to player-level).  For
# these markets, roster validation legitimately DOES NOT APPLY —
# we mark CURRENT_ROSTER_VALID as NOT_APPLICABLE per §2.
GAME_LEVEL_MARKETS: frozenset[str] = frozenset({
    "h2h", "spreads", "totals", "btts", "double_chance",
})


@dataclass
class ReachabilityReport:
    pick_id:              Optional[str] = None
    canonical_prediction_id: Optional[str] = None
    sport:                Optional[str] = None
    market:               Optional[str] = None
    generated_at:         str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    stages:               dict[str, dict] = field(default_factory=dict)
    consumers:            dict[str, dict] = field(default_factory=dict)
    supported:            bool = False        # ALL applicable stages PASS
    unknown_stages:       list[str] = field(default_factory=list)
    failed_stages:        list[str] = field(default_factory=list)
    not_applicable_stages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pick_id":                self.pick_id,
            "canonical_prediction_id": self.canonical_prediction_id,
            "sport":                  self.sport,
            "market":                 self.market,
            "generated_at":           self.generated_at,
            "stages":                 self.stages,
            "consumers":              self.consumers,
            "supported":              self.supported,
            "unknown_stages":         self.unknown_stages,
            "failed_stages":          self.failed_stages,
            "not_applicable_stages":  self.not_applicable_stages,
        }


# ═══════════════════════════════════════════════════════════════════
# Stage evaluators (all take a pick dict + optional context)
# ═══════════════════════════════════════════════════════════════════
def _real_book_odds(pick: dict) -> bool:
    try:
        n = int(pick.get("book_odds"))
    except (TypeError, ValueError):
        return False
    if n == 0:
        return False
    if pick.get("no_real_book_line") is True:
        return False
    prov = pick.get("odds_provenance")
    if isinstance(prov, str) and prov.upper() in {
        "MODEL", "SYNTHETIC", "FAIR", "MODEL_ONLY", "COMPUTED",
    }:
        return False
    return True


def _eval_data_available(pick: dict) -> dict:
    # Every canonical pick must reference a source event; if none
    # is set we cannot prove data availability.
    if pick.get("event_id") or pick.get("home_team") or pick.get("away_team"):
        return stage_status_pass(evidence="pick.event_id/teams present")
    return stage_status_unknown(detail="no event/team reference on pick")


def _eval_identity(pick: dict) -> dict:
    # Player markets require player identity; game markets don't.
    market = (pick.get("market") or "").lower()
    if market in GAME_LEVEL_MARKETS or not pick.get("player_name"):
        return stage_status_not_applicable(detail="game-level market")
    if pick.get("canonical_player_id") or pick.get("player_id"):
        return stage_status_pass(evidence="canonical_player_id present")
    return stage_status_fail(
        DropReason.IDENTITY_UNRESOLVED,
        detail="player_name present but no canonical_player_id")


def _eval_current_event_valid(pick: dict) -> dict:
    # A canonical pick that survived publication must have referenced
    # an event that existed at publication time.  We accept the
    # presence of a commence_time / kickoff timestamp as evidence.
    if pick.get("commence_time") or pick.get("kickoff"):
        return stage_status_pass(evidence="commence_time present")
    return stage_status_unknown(detail="no commence_time on pick")


def _eval_current_roster_valid(pick: dict) -> dict:
    market = (pick.get("market") or "").lower()
    if market in GAME_LEVEL_MARKETS or not pick.get("player_name"):
        return stage_status_not_applicable(detail="game-level market")
    if pick.get("current_team"):
        return stage_status_pass(evidence="current_team present")
    return stage_status_unknown(
        detail="no current_team evidence on pick record")


def _eval_real_market(pick: dict) -> dict:
    if _real_book_odds(pick):
        return stage_status_pass(evidence=f"book_odds={pick.get('book_odds')}")
    return stage_status_fail(
        DropReason.REAL_LINE_UNAVAILABLE,
        detail="book_odds missing/zero/synthetic or no_real_book_line=True")


def _eval_evidence(pick: dict) -> dict:
    # Evidence is optional on game markets but expected on player
    # props.  We accept the presence of ``evidence`` /
    # ``rationale`` / ``model_probability`` as proof.
    if pick.get("evidence") or pick.get("rationale") or (
        pick.get("model_probability") is not None
    ):
        return stage_status_pass(evidence="evidence/rationale/model_probability")
    return stage_status_unknown(detail="no evidence surface on pick")


def _eval_model_consumed(pick: dict) -> dict:
    if pick.get("model_probability") is not None and \
       pick.get("model_probability") != "":
        return stage_status_pass(
            evidence=f"model_probability={pick.get('model_probability')}")
    return stage_status_unknown(detail="no model_probability recorded")


def _eval_candidate_generated(pick: dict) -> dict:
    # Any pick document in db.picks is by definition a generated
    # candidate — its persistence proves the candidate generator ran.
    return stage_status_pass(evidence="pick document exists")


def _eval_canonical_published(pick: dict) -> dict:
    gate = pick.get("publication_gate")
    if gate == "canonical_barrier_rejected":
        return stage_status_fail(
            DropReason.PUBLICATION_REJECTED,
            detail=", ".join(pick.get("barrier_failures") or []) or "rejected")
    if gate == "canonical_barrier_passed":
        return stage_status_pass(evidence="canonical_barrier_passed")
    # Legacy records may pre-date the barrier — do not crash.
    return stage_status_unknown(
        detail="pick predates canonical_publication_barrier metadata")


def _eval_visible(pick: dict) -> dict:
    if pick.get("off_board") is True or pick.get("no_bet") is True:
        return stage_status_fail(
            DropReason.BOARD_INELIGIBLE,
            detail="off_board / no_bet set")
    try:
        lock = float(pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        lock = 0.0
    if lock < 85:
        return stage_status_fail(
            DropReason.BOARD_INELIGIBLE,
            detail=f"lock_score {lock} < 85")
    return stage_status_pass(evidence=f"lock_score={lock}")


def _eval_locks(pick: dict) -> dict:
    # Locks visibility matches the "visible" gate for the 85 floor.
    return _eval_visible(pick)


def _eval_rollover(pick: dict) -> dict:
    if pick.get("off_board") is True or pick.get("no_bet") is True:
        return stage_status_fail(
            DropReason.ROLLOVER_INELIGIBLE, detail="off_board / no_bet")
    try:
        lock = float(pick.get("lock_score") or 0)
    except (TypeError, ValueError):
        lock = 0.0
    if lock < 89:
        return stage_status_fail(
            DropReason.ROLLOVER_INELIGIBLE,
            detail=f"lock_score {lock} < 89 (rollover floor)")
    return stage_status_pass(evidence=f"lock_score={lock} >= 89")


def _eval_parlay(pick: dict) -> dict:
    if pick.get("off_board") is True or pick.get("no_bet") is True:
        return stage_status_fail(
            DropReason.PARLAY_INELIGIBLE, detail="off_board / no_bet")
    return _eval_visible(pick)   # parlay uses the 85 floor


def _eval_frozen(pick: dict, snapshot: Optional[dict]) -> dict:
    if snapshot and snapshot.get("snapshot_hash"):
        return stage_status_pass(evidence="pregame_snapshot hash present")
    if pick.get("pregame_snapshot_id"):
        return stage_status_pass(
            evidence=f"pick.pregame_snapshot_id={pick.get('pregame_snapshot_id')}")
    return stage_status_unknown(detail="no pregame snapshot linked yet")


def _eval_settled(pick: dict, settlement: Optional[dict]) -> dict:
    if settlement:
        return stage_status_pass(evidence="settlement record present")
    status = (pick.get("settlement_status") or "").lower()
    if status in {"won", "lost", "push", "void"}:
        return stage_status_pass(evidence=f"settlement_status={status}")
    if status in {"pending", ""}:
        return stage_status_fail(
            DropReason.SETTLEMENT_PENDING,
            detail=f"settlement_status={status or 'missing'}")
    return stage_status_unknown(detail=f"settlement_status={status!r}")


def _eval_measurable(pick: dict, settlement: Optional[dict],
                       analytics_linked: Optional[bool]) -> dict:
    if analytics_linked is True:
        return stage_status_pass(evidence="analytics row linked")
    if analytics_linked is False:
        return stage_status_fail(
            DropReason.ANALYTICS_WRITE_FAILED,
            detail="analytics could not attribute settlement")
    # Unknown → we cannot prove measurability from the pick alone.
    return stage_status_unknown(detail="analytics linkage not evaluated")


def build_reachability_report(
    pick: dict,
    *,
    pregame_snapshot: Optional[dict] = None,
    settlement_record: Optional[dict] = None,
    analytics_linked: Optional[bool] = None,
) -> ReachabilityReport:
    """Assemble a complete reachability report for a single pick.

    The caller passes whatever adjacent evidence it can prove — no
    stage is upgraded to PASS on the strength of module existence
    (§14).
    """
    report = ReachabilityReport(
        pick_id=pick.get("id") or pick.get("external_id"),
        canonical_prediction_id=pick.get("canonical_prediction_id"),
        sport=pick.get("sport"),
        market=pick.get("market"),
    )

    # Chain evaluation.
    report.stages[ProductionStage.DATA_AVAILABLE.value]        = _eval_data_available(pick)
    report.stages[ProductionStage.IDENTITY_RESOLVED.value]     = _eval_identity(pick)
    report.stages[ProductionStage.CURRENT_EVENT_VALID.value]   = _eval_current_event_valid(pick)
    report.stages[ProductionStage.CURRENT_ROSTER_VALID.value]  = _eval_current_roster_valid(pick)
    report.stages[ProductionStage.REAL_MARKET_AVAILABLE.value] = _eval_real_market(pick)
    report.stages[ProductionStage.EVIDENCE_AVAILABLE.value]    = _eval_evidence(pick)
    report.stages[ProductionStage.MODEL_CONSUMED.value]        = _eval_model_consumed(pick)
    report.stages[ProductionStage.CANDIDATE_GENERATED.value]   = _eval_candidate_generated(pick)
    report.stages[ProductionStage.CANONICAL_PUBLISHED.value]   = _eval_canonical_published(pick)
    report.stages[ProductionStage.VISIBLE_TO_CONSUMER.value]   = _eval_visible(pick)
    report.stages[ProductionStage.LOCKS_ELIGIBLE.value]        = _eval_locks(pick)
    report.stages[ProductionStage.ROLLOVER_ELIGIBLE.value]     = _eval_rollover(pick)
    report.stages[ProductionStage.PARLAY_ELIGIBLE.value]       = _eval_parlay(pick)
    report.stages[ProductionStage.PREGAME_FROZEN.value]        = _eval_frozen(pick, pregame_snapshot)
    report.stages[ProductionStage.SETTLED.value]               = _eval_settled(pick, settlement_record)
    report.stages[ProductionStage.MEASURABLE.value]            = _eval_measurable(
        pick, settlement_record, analytics_linked)

    # Consumer surfaces — INDEPENDENT status per §5.
    report.consumers[ConsumerSurface.LOCKS.value]     = report.stages[ProductionStage.LOCKS_ELIGIBLE.value]
    report.consumers[ConsumerSurface.ROLLOVER.value]  = report.stages[ProductionStage.ROLLOVER_ELIGIBLE.value]
    report.consumers[ConsumerSurface.PARLAY.value]    = report.stages[ProductionStage.PARLAY_ELIGIBLE.value]
    # HISTORY / ANALYTICS / MAGIC surfaces are proven independently
    # by callers with access to the correct downstream stores — we
    # report UNKNOWN when not proven, never fake PASS.
    report.consumers[ConsumerSurface.HISTORY.value]   = (
        stage_status_pass(evidence="pregame_snapshot linked")
        if pregame_snapshot else
        stage_status_unknown(detail="history linkage not proven")
    )
    report.consumers[ConsumerSurface.ANALYTICS.value] = (
        stage_status_pass(evidence="analytics linked")
        if analytics_linked is True else
        stage_status_fail(DropReason.ANALYTICS_WRITE_FAILED,
                          detail="analytics linkage failed")
        if analytics_linked is False else
        stage_status_unknown(detail="analytics linkage not evaluated")
    )
    report.consumers[ConsumerSurface.MAGIC.value] = stage_status_unknown(
        detail="Magic 2.0 is not certified — awaiting Phase 5.3 Stage 2+")
    report.consumers[ConsumerSurface.SPECIALIZED_ANALYTICS.value] = stage_status_unknown(
        detail="specialized analytics proof not evaluated")

    # Rollups.
    for stage_name, info in report.stages.items():
        st = info.get("status")
        if st == StageStatus.FAIL.value:
            report.failed_stages.append(stage_name)
        elif st == StageStatus.UNKNOWN.value:
            report.unknown_stages.append(stage_name)
        elif st == StageStatus.NOT_APPLICABLE.value:
            report.not_applicable_stages.append(stage_name)

    # ``supported`` is true only when every APPLICABLE stage PASSed.
    report.supported = (
        len(report.failed_stages) == 0 and
        len(report.unknown_stages) == 0
    )
    return report


def reachability_summary(reports: list[ReachabilityReport]) -> dict:
    """Aggregate summary across a batch of picks."""
    total = len(reports)
    supported = sum(1 for r in reports if r.supported)
    by_stage_fail: dict[str, int] = {}
    by_stage_unknown: dict[str, int] = {}
    for r in reports:
        for s in r.failed_stages:
            by_stage_fail[s] = by_stage_fail.get(s, 0) + 1
        for s in r.unknown_stages:
            by_stage_unknown[s] = by_stage_unknown.get(s, 0) + 1
    return {
        "total":            total,
        "supported":        supported,
        "unsupported":      total - supported,
        "fail_by_stage":    by_stage_fail,
        "unknown_by_stage": by_stage_unknown,
    }


__all__ = [
    "ConsumerSurface",
    "ReachabilityReport",
    "GAME_LEVEL_MARKETS",
    "build_reachability_report",
    "reachability_summary",
]
