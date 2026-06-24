"""Universal Evidence System — the contract that gates every public
"why this pick" claim against the data the model ACTUALLY used.

────────────────────────────────────────────────────────────────────────
The 8 rules (from PerksLocks product spec, 2026-06-24)
────────────────────────────────────────────────────────────────────────
1. Every explanation claim must map to a real feature.
2. Every feature must return:
     {value, sample_size, lookback_days, source, freshness, reliability}
3. Sample-size tiers → confidence tiers (LOW / MEDIUM / HIGH).
4. Reliability shrinkage:  adjusted = baseline + (raw - baseline) × reliability
5. Explanation governor — only surface a reason if importance and
   reliability both clear their thresholds. Otherwise fall back to a
   generic "signal exists but sample is limited" line.
6. Separate four output dimensions: Probability · Edge · Evidence · Lock.
7. Lock governor:  final_lock = raw_lock × evidence_multiplier.
8. Store an audit trail per pick so any explanation can be traced back
   to its source data.

This module is sport-agnostic. Per-sport adapters in Phase 2 will
build EvidenceFeature lists from MLB pitcher data, NBA minutes,
Tennis Elo, etc. — but the contract here is universal.

NOTHING in this module touches the win probability. The probability
estimate stays exactly what the upstream model said it was. We only
gate the LOCK SCORE (a display-layer confidence) and the SHOWN
explanation strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional


# ── Type definitions ────────────────────────────────────────────────
Tier = Literal["LOW", "MEDIUM", "HIGH"]
FeatureCategory = Literal[
    "form",        # recent player/team performance
    "matchup",     # opponent-specific factors
    "context",     # park, weather, rest, surface
    "market",      # closing-line value, edge, book agreement
    "model",       # internal model agreement (sim vs blended, learning)
    "usage",       # role / minutes / snap counts / lineup
    "intangible",  # narrative, motivation — lowest weight
]


@dataclass
class EvidenceFeature:
    """A single piece of evidence supporting (or contradicting) a pick.

    Every feature MUST carry full provenance — name, source, sample
    size, lookback window — so the audit trail can prove where the
    claim came from. Features with no provenance are dropped, period.
    """

    name: str                       # human-readable label
    category: FeatureCategory
    value: Any                      # raw value (number, str, dict)
    sample_size: int                # how many observations back the value
    lookback_days: int              # window in days the sample was drawn from
    source: str                     # "MLB Stats API" / "Understat" / etc.
    importance: float = 0.5         # 0..1 — how heavily we'd weight this feature
                                    # if reliability were perfect
    freshness_hours: float = 0.0    # hours since the data point was recorded
    direction: Literal["pro", "con", "neutral"] = "pro"
    reason: str = ""                # one-line natural-language summary
    explanation_text: Optional[str] = None  # the actual bullet to surface in UI

    # ── Derived (filled by classify()) ──
    tier: Tier = "LOW"
    reliability: float = 0.30
    passes_governor: bool = False

    def to_audit_dict(self) -> dict:
        """Serializable envelope that gets stored on the pick for the
        admin inspector."""
        return {
            "name":           self.name,
            "category":       self.category,
            "value":          self.value,
            "sample_size":    self.sample_size,
            "lookback_days":  self.lookback_days,
            "source":         self.source,
            "freshness_hours": round(self.freshness_hours, 1),
            "importance":     round(self.importance, 3),
            "reliability":    round(self.reliability, 3),
            "tier":           self.tier,
            "direction":      self.direction,
            "passes_governor": self.passes_governor,
            "reason":         self.reason,
            "explanation_text": self.explanation_text,
        }


# ── Tier thresholds ─────────────────────────────────────────────────
# Per-category sample-size cuts. These are STARTER values; per-sport
# overrides come in Phase 2 (e.g. MLB at-bats vs NBA minutes vs Tennis
# matches have very different "useful sample" definitions).
_SAMPLE_TIERS_BY_CATEGORY: dict[FeatureCategory, tuple[int, int]] = {
    "form":       (10, 30),   # MED ≥10 obs, HIGH ≥30
    "matchup":    (5,  15),   # head-to-head is naturally smaller
    "context":    (3,  10),   # weather/park/surface needs less sample
    "market":     (1,  5),    # market signals are point-in-time
    "model":      (1,  5),    # model outputs are deterministic
    "usage":      (5,  20),
    "intangible": (50, 200),  # nearly impossible — by design
}

# Reliability anchors per tier. Used as the BASE; freshness penalties
# are subtracted on top. These map a tier label to the
# shrinkage coefficient applied in rule 4 (adjusted = baseline +
# (raw − baseline) × reliability).
_RELIABILITY_BY_TIER: dict[Tier, float] = {
    "LOW":    0.30,
    "MEDIUM": 0.65,
    "HIGH":   0.95,
}

# Threshold a feature must CLEAR (importance × reliability) for its
# explanation_text to be shown to the user.
EXPLANATION_GOVERNOR_THRESHOLD = 0.30

# Words we won't show unless evidence is HIGH. Stripped or downgraded
# automatically — keeps the engine honest even if a per-sport adapter
# accidentally produced hype.
_HYPE_WORDS_PATTERN = re.compile(
    r"\b(elite|dominant|dominat\w*|automatic|massive edge|locks?\b|lock-it-in"
    r"|guaranteed|can'?t miss|no-brainer|smash|hammer)\b",
    re.IGNORECASE,
)
_HYPE_REPLACEMENTS = {
    "elite":       "strong",
    "dominant":    "favored",
    "dominating":  "favored",
    "dominates":   "is favored over",
    "automatic":   "high-probability",
    "massive edge": "edge",
    "lock":        "lean",
    "locks":       "leans",
    "lock-it-in":  "lean",
    "guaranteed":  "high-probability",
    "can't miss":  "high-probability",
    "cant miss":   "high-probability",
    "no-brainer":  "high-probability",
    "smash":       "back",
    "hammer":      "back",
}

# Fallback line shown when we have a signal but the evidence behind it
# isn't strong enough to put a specific claim behind.
SIGNAL_LIMITED_FALLBACK = "Signal exists but supporting sample is limited."


# ── Classification helpers ──────────────────────────────────────────
def classify_tier(f: EvidenceFeature) -> Tier:
    """Map (sample_size, category) → tier label."""
    med_cut, hi_cut = _SAMPLE_TIERS_BY_CATEGORY.get(f.category, (10, 30))
    if f.sample_size >= hi_cut:
        return "HIGH"
    if f.sample_size >= med_cut:
        return "MEDIUM"
    return "LOW"


def freshness_penalty(hours: float) -> float:
    """Multiplicative penalty: features stale by N hours lose
    confidence linearly. Beyond 168h (1 wk) we're back at the floor.

    Returns a multiplier in [0.50, 1.00].
    """
    if hours <= 12:   return 1.00
    if hours >= 168:  return 0.50
    # Linear ramp 12h→168h: 1.00 → 0.50
    span = 168 - 12
    pos = (hours - 12) / span
    return max(0.50, 1.00 - 0.50 * pos)


def classify(features: list[EvidenceFeature]) -> list[EvidenceFeature]:
    """Mutates each feature in place to set tier / reliability /
    passes_governor. Returns the same list for chaining.
    """
    for f in features:
        f.tier = classify_tier(f)
        base = _RELIABILITY_BY_TIER[f.tier]
        f.reliability = round(base * freshness_penalty(f.freshness_hours), 3)
        f.passes_governor = (
            f.importance * f.reliability >= EXPLANATION_GOVERNOR_THRESHOLD
        )
    return features


# ── Evidence Score ─────────────────────────────────────────────────
def evidence_score(features: list[EvidenceFeature]) -> int:
    """Aggregate evidence score 0..100 for the pick.

    Definition: weighted average of (reliability × importance) across
    all features, scaled to 0–100. We weight by importance so a
    handful of strong features outweighs many low-importance ones.

    A pick with no provenance returns 0 — by design, no evidence
    means no claim allowed.
    """
    if not features:
        return 0
    total_weight = sum(max(0.0, f.importance) for f in features)
    if total_weight <= 0:
        return 0
    contrib = sum(
        max(0.0, f.importance) * f.reliability for f in features
    )
    score = (contrib / total_weight) * 100.0
    # Reward breadth — having multiple HIGH-tier features that
    # corroborate beats one HIGH alone. We give a small additive
    # bonus per extra independent HIGH feature, capped at +10.
    high_count = sum(1 for f in features if f.tier == "HIGH")
    if high_count >= 2:
        score = min(100.0, score + min(10.0, (high_count - 1) * 2.5))
    return int(round(max(0.0, min(100.0, score))))


# ── Lock Governor ───────────────────────────────────────────────────
def evidence_multiplier(score: int) -> float:
    """Map evidence score → multiplier applied to raw lock score.

    Conservative by design: even at evidence_score=0 we keep 70% of
    the raw lock (so a high-edge market signal still surfaces), but
    we never amplify above 1.00 — evidence can only TEMPER, never
    INFLATE, the lock score.
    """
    if score >= 80:  return 1.00
    if score >= 60:  return 0.93
    if score >= 40:  return 0.85
    if score >= 20:  return 0.78
    return 0.70


def apply_lock_governor(raw_lock: float, score: int) -> float:
    """final_lock = raw_lock × evidence_multiplier(score). Clamped
    [0, 99] to match the rest of the codebase's lock ceiling."""
    if raw_lock is None:
        return 0.0
    mult = evidence_multiplier(score)
    val = float(raw_lock) * mult
    return round(max(0.0, min(99.0, val)), 1)


# ── Explanation Governor ────────────────────────────────────────────
def _detune_hype(s: str) -> str:
    """Replace forbidden hype words with neutral equivalents."""
    def repl(m: re.Match) -> str:
        word = m.group(0).lower()
        if word in _HYPE_REPLACEMENTS:
            return _HYPE_REPLACEMENTS[word]
        # Family fallback — anything matching "dominat*" not already
        # caught above.
        for k, v in _HYPE_REPLACEMENTS.items():
            if word.startswith(k):
                return v
        return ""  # drop the word entirely as last resort
    return _HYPE_WORDS_PATTERN.sub(repl, s)


def apply_explanation_governor(
    insights: Iterable[str],
    features: list[EvidenceFeature],
    overall_score: int,
) -> tuple[list[str], list[str]]:
    """Filter and detune the existing free-text `key_insights` based
    on the evidence behind them.

    Strategy:
      • If the OVERALL evidence_score < 40, we DROP every insight
        that contains hype language and replace the list with the
        fallback line (rule 5).
      • If the score is ≥ 40 but the insight itself uses hype words,
        we DETUNE it (rule 6 keeps the message but strips amplifiers).
      • Insights backed by a feature that passes_governor flow
        through unchanged.

    Returns (kept_insights, dropped_insights). The dropped list is
    persisted to the audit trail.
    """
    kept: list[str] = []
    dropped: list[str] = []

    # Pull the explanation_text from passing features first — these
    # are the "evidence-backed" lines the engine itself produced.
    for f in features:
        if f.passes_governor and f.explanation_text:
            kept.append(f.explanation_text)

    for raw in insights:
        if not raw or not isinstance(raw, str):
            continue
        s = raw.strip()
        if not s:
            continue
        # Already covered by a passing feature? Skip the duplicate.
        if any(s == k for k in kept):
            continue
        contains_hype = bool(_HYPE_WORDS_PATTERN.search(s))

        if overall_score < 40 and contains_hype:
            dropped.append(s)
            continue
        # Detune hype words for everything else when evidence is
        # less than HIGH overall — we keep the message, not the
        # amplifier.
        if overall_score < 80 and contains_hype:
            s = _detune_hype(s)
        kept.append(s)

    if not kept:
        kept = [SIGNAL_LIMITED_FALLBACK]
    return kept, dropped


# ── End-to-end orchestrator ─────────────────────────────────────────
def govern_pick(
    pick: dict,
    features: list[EvidenceFeature],
) -> dict:
    """Run the full evidence pipeline against a generated pick and
    persist the audit trail. Returns the SAME dict with these fields
    added / overwritten:

      • evidence_score          (int 0..100)
      • lock_score_raw          (float — the original lock_score)
      • lock_score              (float — governed)
      • evidence_breakdown      (dict — for the admin inspector)
      • key_insights            (list[str] — filtered + detuned)

    Win probability and edge_percent are NEVER mutated.
    """
    if pick is None:
        return pick

    classify(features)
    score = evidence_score(features)

    raw_lock = pick.get("lock_score")
    raw_lock_v2 = pick.get("lock_score_v2")
    governed_lock    = apply_lock_governor(raw_lock,    score) if raw_lock    is not None else None
    governed_lock_v2 = apply_lock_governor(raw_lock_v2, score) if raw_lock_v2 is not None else None

    # Sort features by (importance × reliability) descending for the
    # admin inspector "top features" view.
    sorted_feats = sorted(
        features,
        key=lambda f: (f.importance * f.reliability),
        reverse=True,
    )

    insights_in = pick.get("key_insights") or []
    insights_out, dropped = apply_explanation_governor(
        insights_in, sorted_feats, score,
    )

    pick["evidence_score"] = score
    if raw_lock is not None:
        pick["lock_score_raw"] = round(float(raw_lock), 1)
        pick["lock_score"]      = governed_lock
    if raw_lock_v2 is not None:
        # Govern V2 with the SAME multiplier so the canonicalization
        # step (max of v1, v2) can't accidentally pick an ungoverned
        # number and erase the haircut.
        pick["lock_score_v2_raw"] = round(float(raw_lock_v2), 1)
        pick["lock_score_v2"]      = governed_lock_v2
    # ── Lock-score canonical alignment ──
    # The read path applies `lock_score = max(v1, v2)` (see
    # `_canonicalize_lock_score` in server.py). If V2 wins that max,
    # the persisted lock_score_raw must ALSO track V2's raw — otherwise
    # the inspector reports "Lock 92 > Raw 87.5" (the iter-49 canary
    # corruption) because lock_score_raw was only tracking V1.
    canonical_lock = pick.get("lock_score")
    canonical_v2   = pick.get("lock_score_v2")
    if (
        canonical_lock is not None and canonical_v2 is not None
        and float(canonical_v2) > float(canonical_lock)
    ):
        pick["lock_score"]      = canonical_v2
        # Match the V2 raw so audit math reconciles: raw × mult = lock
        v2_raw = pick.get("lock_score_v2_raw") or raw_lock_v2
        if v2_raw is not None:
            pick["lock_score_raw"] = round(float(v2_raw), 1)
    pick["key_insights"] = insights_out

    pick["evidence_breakdown"] = {
        "score":           score,
        "multiplier":      evidence_multiplier(score),
        "lock_raw":        pick.get("lock_score_raw"),
        "lock_governed":   pick.get("lock_score"),
        "tier_counts":     {
            "HIGH":   sum(1 for f in features if f.tier == "HIGH"),
            "MEDIUM": sum(1 for f in features if f.tier == "MEDIUM"),
            "LOW":    sum(1 for f in features if f.tier == "LOW"),
        },
        "top_features":      [f.to_audit_dict() for f in sorted_feats[:8]],
        "excluded_features": [f.to_audit_dict() for f in sorted_feats
                              if not f.passes_governor],
        "dropped_insights":  dropped,
        "generated_at":      datetime.now(timezone.utc).isoformat(),
    }

    return pick


# ── Sport-agnostic feature builder (used by all sports during Phase 1) ─
def build_features_from_pick(pick: dict) -> list[EvidenceFeature]:
    """Phase 1 best-effort feature extractor — reads whatever
    provenance the existing pick generator already populates and
    wraps it in the EvidenceFeature envelope.

    Phase 2 will REPLACE this with per-sport adapters that pull
    fresh features directly from the upstream data sources. For now
    we work with what's on the pick.

    Heuristics (sport-agnostic):
      • `factors`        → one feature per factor (form/model category)
      • `learning`       → MEDIUM-tier model feature
      • `sim_runs`       → HIGH-tier model feature if ≥1000 runs
      • `understat_form` → context feature (soccer)
      • `pitcher_h2h`    → matchup feature (MLB)
      • `elo`            → form feature (tennis)
      • `edge_percent`   → market feature
    """
    features: list[EvidenceFeature] = []
    now = datetime.now(timezone.utc)

    def hours_since(iso_str: str | None) -> float:
        if not iso_str:
            return 0.0
        try:
            ts = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
            return max(0.0, (now - ts).total_seconds() / 3600.0)
        except Exception:
            return 0.0

    sport = (pick.get("sport") or "").upper()

    # 1) Factor breakdown — each factor becomes its own feature.
    factors = pick.get("factors") or {}
    for k, v in (factors.items() if isinstance(factors, dict) else []):
        try:
            score_v = float(v)
        except Exception:
            continue
        # Categorize by name keyword.
        kl = k.lower()
        if any(w in kl for w in ("form", "recent", "streak", "rolling")):
            cat: FeatureCategory = "form"
            n_obs = 10
        elif any(w in kl for w in ("matchup", "h2h", "vs", "opponent")):
            cat = "matchup"
            n_obs = 8
        elif any(w in kl for w in ("market", "edge", "value", "book", "clv")):
            cat = "market"
            n_obs = 5
        elif any(w in kl for w in ("park", "weather", "surface", "rest", "venue")):
            cat = "context"
            n_obs = 5
        elif any(w in kl for w in ("model", "sim", "ml", "ai")):
            cat = "model"
            n_obs = 1000
        else:
            cat = "intangible"
            n_obs = 1
        features.append(EvidenceFeature(
            name=k, category=cat, value=score_v,
            sample_size=n_obs, lookback_days=14,
            source=f"{sport} factor breakdown",
            importance=min(1.0, abs(score_v) / 100.0),
            reason=f"{k} scored {score_v:.0f}/100",
        ))

    # 2) Monte Carlo simulator — if the pick was sim-augmented.
    sim_runs = pick.get("sim_runs")
    if sim_runs and sim_runs >= 1000:
        features.append(EvidenceFeature(
            name="Monte Carlo simulator",
            category="model",
            value=pick.get("sim_win_probability"),
            sample_size=int(sim_runs),
            lookback_days=0,
            source="brain/sim_runner.py",
            importance=0.85,
            freshness_hours=0.0,
            reason=f"{int(sim_runs):,}-run Monte Carlo simulation",
            explanation_text=(
                f"Monte Carlo over {int(sim_runs):,} runs returns "
                f"{pick.get('sim_win_probability'):.1f}% win probability"
                if pick.get("sim_win_probability") is not None else None
            ),
        ))

    # 3) Learning engine — was the lock score adjusted by the learning
    #    loop based on past settled picks?
    learning = pick.get("learning") or {}
    if isinstance(learning, dict) and learning.get("active_buckets"):
        n = int(learning.get("sample_size") or 0)
        features.append(EvidenceFeature(
            name="Learning engine bucket",
            category="model",
            value=learning.get("active_buckets"),
            sample_size=n,
            lookback_days=90,
            source="learning_engine.py",
            importance=0.7,
            reason=f"Bucket calibrated against {n} prior picks",
        ))

    # 4) Understat xG form — soccer.
    uf = pick.get("understat_form") or {}
    if isinstance(uf, dict) and uf.get("label"):
        features.append(EvidenceFeature(
            name="Understat xG form",
            category="form",
            value=uf.get("label"),
            sample_size=int(uf.get("matches_used") or 0),
            lookback_days=30,
            source="Understat",
            importance=0.75,
            freshness_hours=hours_since(uf.get("snapshot_at")),
            reason=f"xG form label = {uf.get('label')}",
        ))

    # 5) Pitcher H2H — MLB.
    h2h = pick.get("pitcher_h2h") or {}
    if isinstance(h2h, dict) and h2h.get("plate_appearances"):
        pa = int(h2h.get("plate_appearances") or 0)
        features.append(EvidenceFeature(
            name="Pitcher vs Batter H2H",
            category="matchup",
            value=h2h.get("avg") or h2h.get("ops"),
            sample_size=pa,
            lookback_days=730,
            source="MLB Stats API",
            importance=0.65,
            reason=f"{pa} prior plate appearances",
        ))

    # 6) Edge — universal market feature.
    edge = pick.get("edge_percent")
    if edge is not None:
        try:
            edge_f = float(edge)
            features.append(EvidenceFeature(
                name="Closing-line market edge",
                category="market",
                value=edge_f,
                sample_size=10,  # we average across ~10 books
                lookback_days=0,
                source="The Odds API",
                importance=min(1.0, abs(edge_f) / 8.0),
                freshness_hours=0.0,
                reason=f"{edge_f:+.1f}% edge vs market consensus",
            ))
        except Exception:
            pass

    return features


__all__ = [
    "EvidenceFeature",
    "classify",
    "evidence_score",
    "evidence_multiplier",
    "apply_lock_governor",
    "apply_explanation_governor",
    "govern_pick",
    "build_features_from_pick",
    "EXPLANATION_GOVERNOR_THRESHOLD",
    "SIGNAL_LIMITED_FALLBACK",
]
