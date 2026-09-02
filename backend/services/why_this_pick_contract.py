"""Why-This-Pick — structured payload contract (Phase 3).

Every publishable pick MUST carry a rationale in ONE canonical shape:

    {
      "summary":        str,             # short humanised one-liner
      "evidence":       [str, ...],      # 1..N bullet points
      "concerns":       [str, ...],      # 0..N caveats (may be empty)
      "data_source":    str,             # provenance tag
      "model_win_prob_pct": float,
      "edge_percent":       float | None,
      "lock_score":         float,
    }

Optional structured helpers:
    "matchup_summary", "similar_matchup_summary",
    "monte_carlo_summary", "trained_model_summary",
    "top_factors": [dict, ...],
    "counter_factors": [dict, ...],
    "sample_sizes": {name: int},

Phase 3 rules:
  1. Rationale MUST be a dict (never a bare string, never None on a
     publishable pick).
  2. Rationale MUST contain at least ONE evidence bullet OR a
     structured factor payload — no empty-shell rationales.
  3. Bare "fallback text generation" (a single ``summary`` with an
     empty ``evidence`` list AND no factor payload) is REJECTED for
     publication so the frozen snapshot never contains vacuous text.
  4. The rationale that appears on a Locks card MUST be the same
     dict frozen into `published_reasoning` on the snapshot.  The
     UI never re-generates it at render time.
"""
from __future__ import annotations
from typing import Any


REQUIRED_KEYS: tuple[str, ...] = (
    "summary", "evidence", "concerns", "data_source",
    "model_win_prob_pct", "edge_percent", "lock_score",
)

# A rationale is considered "substantive" iff at least ONE of these
# structured payloads is non-empty.  A vacuous fallback (just a
# summary line and empty lists) fails the substantive test.
SUBSTANTIVE_PAYLOAD_KEYS: tuple[str, ...] = (
    "evidence",              # bullet list
    "top_factors",           # structured factor rows
    "counter_factors",
    "matchup_summary",       # engine summaries
    "similar_matchup_summary",
    "monte_carlo_summary",
    "trained_model_summary",
    "stats_this_season",     # sport-specific structured stats
)


class RationaleContractError(ValueError):
    pass


def validate_rationale(rationale: Any) -> dict[str, Any]:
    """Validate a rationale payload for the Phase-3 contract.

    Returns a dict {ok, reasons[], missing_keys[], is_substantive}.
    Never raises — pipelines can inspect ``ok`` and fail closed or
    open per policy.  Publication service uses this to REJECT
    vacuous rationales before freezing the snapshot.
    """
    result: dict[str, Any] = {
        "ok": False,
        "reasons": [],
        "missing_keys": [],
        "is_substantive": False,
    }
    if rationale is None:
        result["reasons"].append("rationale_is_none")
        return result
    if isinstance(rationale, str):
        result["reasons"].append("rationale_is_bare_string")
        return result
    if not isinstance(rationale, dict):
        result["reasons"].append(
            f"rationale_wrong_type:{type(rationale).__name__}")
        return result

    missing = [k for k in REQUIRED_KEYS if k not in rationale]
    if missing:
        result["missing_keys"] = missing
        result["reasons"].append(f"missing_required_keys:{missing}")
        return result

    # Type checks for the critical list fields.
    for lk in ("evidence", "concerns"):
        v = rationale.get(lk)
        if not isinstance(v, list):
            result["reasons"].append(f"{lk}_not_a_list")
            return result

    is_substantive = _has_substantive_payload(rationale)
    result["is_substantive"] = is_substantive
    if not is_substantive:
        result["reasons"].append("vacuous_rationale_no_evidence_or_factors")
        return result

    result["ok"] = True
    return result


def _has_substantive_payload(rationale: dict) -> bool:
    """A rationale is substantive iff ANY of the substantive-payload
    fields is non-empty."""
    for k in SUBSTANTIVE_PAYLOAD_KEYS:
        v = rationale.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)) and len(v) == 0:
            continue
        if isinstance(v, dict) and not v:
            continue
        return True
    return False


def assert_publishable_rationale(rationale: Any) -> None:
    """Publication-time hard assertion.  Raises `RationaleContractError`
    on any contract failure so a vacuous rationale can NEVER be
    frozen into ``published_reasoning``.
    """
    verdict = validate_rationale(rationale)
    if not verdict["ok"]:
        raise RationaleContractError(
            "rationale contract failed: "
            + "; ".join(verdict["reasons"])
        )


__all__ = [
    "REQUIRED_KEYS",
    "SUBSTANTIVE_PAYLOAD_KEYS",
    "RationaleContractError",
    "validate_rationale",
    "assert_publishable_rationale",
]
