"""Canonical Pick Model-Evidence Attacher (Pre-Magic Blocker B).

Persists structured model evidence on canonical picks at publication
time.  Pre-Magic Certification found 0/200 picks carried
``model_probability`` — this module fixes that by promoting the
already-computed production model output to a first-class field with
explicit provenance, WITHOUT altering the underlying probability.

**Hard rules (per remediation §8 – §10):**

* ``model_probability`` is populated ONLY when the pick carries a
  genuine production model output.  We derive it from an established
  canonical field (``win_probability`` / ``published_probability``).
* We NEVER derive from:
    - ``implied_probability`` (sportsbook implied)
    - ``book_odds`` / ``odds_at_pick``
    - ``lock_score``
    - ``edge_percent``
    - any displayed / expected value that isn't a proven model output.
* We NEVER conflate model / simulator / calibrated / market
  probabilities.  They live in distinct fields (§9).
* Missing ⇒ UNKNOWN (stored as ``None``), never 0.
* Idempotent — safe to re-run on already-enriched picks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


# Values in these ranges [0, 1] and [0, 100] are both acceptable.
# We NORMALISE to [0, 1] for storage.
def _normalise_prob(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # NaN guard
    if x != x:
        return None
    # Reasonable bounds — anything outside [0, 1.001] gets scaled from
    # a percentage.  If still out of bounds → treat as invalid.
    if -0.001 <= x <= 1.001:
        return max(0.0, min(1.0, x))
    if 0.0 <= x <= 100.5:
        return max(0.0, min(1.0, x / 100.0))
    return None


def extract_model_evidence(pick: dict) -> dict:
    """Return a dict of model-evidence fields to ``$set`` on the pick.

    Model probability sources (in priority order):
      1. ``model_probability``       (already-canonical field — kept)
      2. ``published_probability``   (post-publication canonical)
      3. ``win_probability``         (raw engine output)

    Simulator probability:
      * ``simulator_probability`` if present and distinct from
        ``model_probability`` — never fabricated.

    Calibrated probability:
      * ``calibrated_probability`` if present — kept separate.

    Sportsbook implied probability:
      * ``implied_probability``  — LEFT UNCHANGED and NEVER promoted
        to ``model_probability``.

    Returned keys (only when we could resolve them — missing keys
    stay absent so the update is idempotent):

        model_probability, model_probability_source,
        model_probability_provenance,
        simulator_probability, simulator_probability_source,
        model_evidence_version, model_evidence_attached_at,
    """
    out: dict[str, Any] = {}
    provenance: dict[str, Any] = {}

    # ── model_probability ───────────────────────────────────────
    src = None
    src_field = None
    for f in ("model_probability", "published_probability", "win_probability"):
        v = pick.get(f)
        p = _normalise_prob(v)
        if p is not None:
            src, src_field = p, f
            break
    if src is not None:
        # Preserve existing value if present — idempotence.
        existing = pick.get("model_probability")
        if _normalise_prob(existing) is not None:
            out["model_probability"] = _normalise_prob(existing)
            provenance["kept_existing"] = True
        else:
            out["model_probability"] = src
        out["model_probability_source"] = src_field
        # Provenance — pull whichever engine/version fields already
        # exist on the pick.  We never invent these.
        for k in ("v2_engine_version", "model_version",
                    "calibration_version", "fusion_version",
                    "simulation_version", "scoring_version",
                    "engine", "model_source", "feature_snapshot_version"):
            v = pick.get(k)
            if v is not None and v != "":
                provenance[k] = v
        provenance["source_field"] = src_field
        provenance["kind"] = _classify_kind(src_field, pick)
        out["model_probability_provenance"] = provenance
    # Missing → out.get('model_probability') is absent (stays UNKNOWN).

    # ── simulator_probability ───────────────────────────────────
    for f in ("simulator_probability", "sim_probability",
                "monte_carlo_probability"):
        v = _normalise_prob(pick.get(f))
        if v is not None:
            out["simulator_probability"] = v
            out["simulator_probability_source"] = f
            break

    # ── calibrated_probability (kept distinct) ──────────────────
    for f in ("calibrated_probability",):
        v = _normalise_prob(pick.get(f))
        if v is not None:
            out["calibrated_probability"] = v
            break

    # ── metadata ────────────────────────────────────────────────
    if "model_probability" in out or "simulator_probability" in out:
        out["model_evidence_version"] = 1
        out["model_evidence_attached_at"] = (
            datetime.now(timezone.utc).isoformat())
    return out


def _classify_kind(src_field: str, pick: dict) -> str:
    """Determine what KIND of probability we just persisted.

    Distinct kinds per §9:
      * ``model``       — raw engine output (``win_probability``,
                            ``model_probability``, ``published_probability``).
      * ``calibrated``  — post-hoc calibrated output.
      * ``simulator``   — MC simulator output.
      * ``unknown``     — unable to classify (shouldn't happen for
                            the known source fields — safety net).

    We DO NOT conflate these into a single ambiguous field.
    """
    if src_field in ("model_probability", "win_probability",
                      "published_probability"):
        # If a calibration adjustment has explicitly been applied,
        # mark as calibrated so consumers know.
        cal_marker = pick.get("tennis_calibrated") or \
                      pick.get("calibrated_probability") or \
                      (pick.get("calibration_version") not in
                       (None, "", "legacy_unknown"))
        return "calibrated" if cal_marker else "model"
    if src_field in ("simulator_probability", "sim_probability",
                      "monte_carlo_probability"):
        return "simulator"
    return "unknown"


__all__ = ["extract_model_evidence"]
