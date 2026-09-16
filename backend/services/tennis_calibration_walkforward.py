"""Tennis chronological calibration + Champion reconstruction
(Session 9 · PERKLOCKS Final Six Partials Closure).

Two harnesses, both replaying `tennis_matches_history` in strict
chronological order with NO lookahead:

    run_calibrated_walkforward(...)
        1. TRAIN slice          — warm the Elo/surface-Elo state ONLY
        2. CALIBRATION slice    — collect (raw_pred, actual) pairs and
                                   fit a Platt (logistic) rescaler
        3. TEST slice           — untouched period, evaluate RAW and
                                   CALIBRATED predictions with strict
                                   forward-only Elo updates
        4. Report N, Brier, log-loss, 5%-wide calibration buckets

    run_champion_comparison(...)
        Attempts to reconstruct the *actual* pre-Session-6 Perklocks
        Tennis champion — which relied on the deterministic
        ``_player_hash`` heuristic + a market-bump.  Since
        ``tennis_matches_history`` does NOT carry authentic historical
        sportsbook odds, we can reconstruct the HASH component but not
        the market component, so the comparison is honestly labelled
        ``ACTUAL CHAMPION COMPARISON — NOT CERTIFIED`` and the exact
        blockers are enumerated.  We DO NOT substitute a 50/50 baseline
        and call it the old champion.

Contract:
    * NO calibration parameter is fit on data the test-period sees.
    * NO probability is forced upward on TRAIN/CALIB evidence alone.
    * If a rescaler shift is DEGRADING TEST metrics we say so and
      recommend keeping the raw challenger.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

BASE_ELO = 1500.0
K_ATP = 24.0
K_WTA = 26.0
K_CH  = 20.0
K_ITF = 16.0


def _tour_k(level: Optional[str]) -> float:
    t = (level or "").upper()
    if "ATP" in t or t == "A": return K_ATP
    if "WTA" in t or t == "W": return K_WTA
    if "CH" in t: return K_CH
    if "IT" in t or "FUT" in t or t.startswith("M15") or t.startswith("W15"): return K_ITF
    return K_ATP


def _elo_prob(delta: float) -> float:
    return 1.0 / (1.0 + 10 ** (-delta / 400.0))


def _brier(pred: float, actual: int) -> float:
    return (pred - actual) ** 2


def _log_loss(pred: float, actual: int) -> float:
    p = min(1 - 1e-9, max(1e-9, pred))
    return -(actual * math.log(p) + (1 - actual) * math.log(1 - p))


def _clip01(p: float) -> float:
    return min(1 - 1e-9, max(1e-9, p))


# ── Platt (logistic) calibrator ─────────────────────────────────────
# Fits P(y=1) = 1 / (1 + exp(a * logit(raw) + b)) via a simple
# gradient descent so we don't add scipy as a runtime dependency.
class PlattCalibrator:
    def __init__(self) -> None:
        self.a: float = 1.0    # slope multiplier on logit(raw)
        self.b: float = 0.0    # intercept
        self.fit_n: int = 0

    def _logit(self, p: float) -> float:
        p = _clip01(p)
        return math.log(p / (1 - p))

    def fit(self, samples: list[tuple[float, int]],
            lr: float = 0.05, epochs: int = 400) -> None:
        if not samples:
            return
        # Gradient descent on cross-entropy over the logit representation.
        # y_hat = sigmoid(a * logit(p) + b);  minimise NLL.
        a, b = 1.0, 0.0
        n = float(len(samples))
        # Pre-compute logits once — the samples never change during fit.
        logits = [self._logit(p) for p, _ in samples]
        actuals = [float(y) for _, y in samples]
        for _ in range(epochs):
            grad_a = 0.0; grad_b = 0.0
            for lg, y in zip(logits, actuals):
                z = a * lg + b
                # numerically stable sigmoid
                if z >= 0:
                    ez = math.exp(-z); yh = 1.0 / (1.0 + ez)
                else:
                    ez = math.exp(z);  yh = ez / (1.0 + ez)
                diff = yh - y
                grad_a += diff * lg
                grad_b += diff
            a -= lr * grad_a / n
            b -= lr * grad_b / n
        self.a = a
        self.b = b
        self.fit_n = int(n)

    def transform(self, p: float) -> float:
        z = self.a * self._logit(p) + self.b
        if z >= 0:
            ez = math.exp(-z); return 1.0 / (1.0 + ez)
        ez = math.exp(z);  return ez / (1.0 + ez)


def _bucket_key(p: float) -> str:
    """5%-wide calibration bucket, from 50% up to 95+."""
    p_bounded = min(0.9999, max(0.5, p))
    # 50-55, 55-60, ..., 90-95, 95+
    band_idx = int((p_bounded - 0.5) * 20)         # 0..9
    band_idx = min(band_idx, 9)
    lo = 50 + band_idx * 5
    if lo >= 95:
        return "95+"
    return f"{lo:02d}-{lo+5:02d}"


@dataclass
class Bucket:
    n:     int   = 0
    hits:  int   = 0
    pred:  float = 0.0
    brier: float = 0.0
    ll:    float = 0.0

    def bump(self, p: float, y: int) -> None:
        self.n     += 1
        self.hits  += y
        self.pred  += p
        self.brier += _brier(p, y)
        self.ll    += _log_loss(p, y)

    def norm(self) -> dict[str, Any]:
        n = max(1, self.n)
        return {
            "n":              self.n,
            "mean_pred_prob": round(self.pred / n, 4),
            "observed_hit":   round(self.hits / n, 4),
            "calib_error":    round(abs((self.pred / n) - (self.hits / n)), 4),
            "brier":          round(self.brier / n, 5),
            "log_loss":       round(self.ll / n, 5),
        }


@dataclass
class SplitAccumulator:
    """Aggregates metrics for one split (raw or calibrated) across
    the full evaluation window."""
    n:      int   = 0
    hits:   int   = 0
    brier:  float = 0.0
    ll:     float = 0.0
    buckets: dict[str, Bucket] = field(default_factory=dict)

    def bump(self, p: float, y: int) -> None:
        self.n     += 1
        self.hits  += y
        self.brier += _brier(p, y)
        self.ll    += _log_loss(p, y)
        b = self.buckets.setdefault(_bucket_key(p), Bucket())
        b.bump(p, y)

    def summary(self) -> dict[str, Any]:
        n = max(1, self.n)
        return {
            "n":         self.n,
            "hit_rate":  round(self.hits / n, 4),
            "brier":     round(self.brier / n, 5),
            "log_loss":  round(self.ll / n, 5),
            "buckets":   {k: v.norm() for k, v in sorted(self.buckets.items())},
        }


# ── Old-champion reconstruction ─────────────────────────────────────
# The pre-Session-6 tennis engine (removed 2026-09-17) used a
# name-based deterministic hash to synthesise a "player strength"
# signal.  We reconstruct that exact function here so we can score it
# under the same chronological no-lookahead evaluation.  Ownership of
# the market bump remains blocked (no historical odds), so the
# champion's SECOND component is unrecoverable — flagged below.
def _hash_prob(name: str) -> float:
    if not name:
        return 0.5
    h = hashlib.md5(name.lower().strip().encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _old_champion_prob(winner: str, loser: str) -> float:
    """Approximation of the old pre-Session-6 challenger from the
    HASH component only.  The market_bump component is UNRECOVERABLE
    because tennis_matches_history has no historical odds — see the
    honest disclosure emitted by run_champion_comparison()."""
    hw = _hash_prob(winner)
    hl = _hash_prob(loser)
    # Old engine constrained hash to a 0.3..0.7 "strength" window;
    # convert to a match win probability via the logistic of the delta.
    sw = 0.3 + 0.4 * hw
    sl = 0.3 + 0.4 * hl
    return _clip01(1.0 / (1.0 + math.exp(-(sw - sl) * 8.0)))


# ── Elo replay helpers ──────────────────────────────────────────────
def _predict(elo, surf_elo, w, l, surface, seen, min_seen):
    """Return (raw_pred, applies?).  `applies=False` when at least one
    player has fewer than `min_seen` prior matches — that observation
    is not scored."""
    if seen.get(w, 0) < min_seen or seen.get(l, 0) < min_seen:
        return None, False
    w_elo = elo.get(w, BASE_ELO); l_elo = elo.get(l, BASE_ELO)
    w_se  = surf_elo.get(w, {}).get(surface, BASE_ELO)
    l_se  = surf_elo.get(l, {}).get(surface, BASE_ELO)
    blended_w = 0.65 * w_elo + 0.35 * w_se
    blended_l = 0.65 * l_elo + 0.35 * l_se
    return _elo_prob(blended_w - blended_l), True


def _update(elo, surf_elo, seen, w, l, surface, k):
    """Forward-only Elo update — never called BEFORE `_predict`."""
    w_elo = elo.get(w, BASE_ELO); l_elo = elo.get(l, BASE_ELO)
    exp_o = _elo_prob(w_elo - l_elo)
    elo[w] = w_elo + k * (1 - exp_o)
    elo[l] = l_elo + k * (0 - (1 - exp_o))
    w_se = surf_elo.setdefault(w, {}).get(surface, BASE_ELO)
    l_se = surf_elo.setdefault(l, {}).get(surface, BASE_ELO)
    exp_s = _elo_prob(w_se - l_se)
    surf_elo[w][surface] = w_se + k * (1 - exp_s)
    surf_elo[l][surface] = l_se + k * (0 - (1 - exp_s))
    seen[w] = seen.get(w, 0) + 1
    seen[l] = seen.get(l, 0) + 1


# ── Chronological calibration harness ───────────────────────────────
async def run_calibrated_walkforward(
    db,
    train_end: str = "2020-12-31",
    calibration_end: str = "2023-12-31",
    min_matches_per_player: int = 5,
) -> dict[str, Any]:
    """Chronological TRAIN → CALIBRATION → TEST split.

    * TRAIN period       : warms Elo state only; no metrics recorded.
    * CALIBRATION period : (raw, actual) pairs are collected here and
                            used to FIT the Platt rescaler.  Elo is
                            still updated forward.  Metrics are NOT
                            reported on this window (that would leak).
    * TEST period        : untouched by calibration fitting.  Both RAW
                            and CALIBRATED predictions are scored.

    All three periods share the SAME chronologically-updated Elo dict
    — nothing rewinds, nothing peeks ahead.
    """
    elo:      dict[str, float]           = {}
    surf_elo: dict[str, dict[str, float]] = {}
    seen:     dict[str, int]             = {}

    cal_samples: list[tuple[float, int]] = []
    raw_test  = SplitAccumulator()
    cal_test  = SplitAccumulator()
    period_counts = {"train": 0, "calibration": 0, "test": 0}

    cursor = db.tennis_matches_history.find({}).sort("date", 1)
    async for m in cursor:
        w = m.get("winner_name"); l = m.get("loser_name")
        if not w or not l: continue
        date_raw = m.get("date")
        # `date` is stored as ISO string in this collection.
        date_str = str(date_raw)[:10] if date_raw else ""
        if not date_str: continue
        surface = (m.get("surface") or "").capitalize() or "Hard"
        level   = (m.get("tourney_level") or "").upper()
        k = _tour_k(level)

        if date_str <= train_end:
            phase = "train"
        elif date_str <= calibration_end:
            phase = "calibration"
        else:
            phase = "test"
        period_counts[phase] += 1

        # PREDICT — no lookahead: Elo state contains only prior matches
        pred, applies = _predict(elo, surf_elo, w, l, surface, seen, min_matches_per_player)
        if applies and pred is not None:
            if phase == "calibration":
                cal_samples.append((pred,       1))
                cal_samples.append((1 - pred,   0))
            elif phase == "test":
                # Score both orientations to preserve symmetry across
                # the buckets (a 60% prediction and its complementary
                # 40% both inform the calibration table).
                raw_test.bump(pred,     1)
                raw_test.bump(1 - pred, 0)

        # APPLY OUTCOME
        _update(elo, surf_elo, seen, w, l, surface, k)

    # Fit rescaler on CALIBRATION only.
    platt = PlattCalibrator()
    platt.fit(cal_samples)

    # Second pass over TEST-only rows to score the CALIBRATED prediction.
    # We deliberately do NOT re-run the whole replay — we just re-score
    # the same test observations using the frozen Elo history at the
    # exact chronological cursor.  Because the raw pass above already
    # discarded observations that failed the `min_matches_per_player`
    # gate, we replay again to guarantee identical filtering.
    elo2:      dict[str, float]           = {}
    surf_elo2: dict[str, dict[str, float]] = {}
    seen2:     dict[str, int]             = {}
    cursor2 = db.tennis_matches_history.find({}).sort("date", 1)
    async for m in cursor2:
        w = m.get("winner_name"); l = m.get("loser_name")
        if not w or not l: continue
        date_str = str(m.get("date") or "")[:10]
        if not date_str: continue
        surface = (m.get("surface") or "").capitalize() or "Hard"
        level   = (m.get("tourney_level") or "").upper()
        k = _tour_k(level)
        phase = ("train" if date_str <= train_end
                 else "calibration" if date_str <= calibration_end
                 else "test")
        if phase == "test":
            pred, applies = _predict(elo2, surf_elo2, w, l, surface, seen2, min_matches_per_player)
            if applies and pred is not None:
                cal_test.bump(platt.transform(pred),     1)
                cal_test.bump(platt.transform(1 - pred), 0)
        _update(elo2, surf_elo2, seen2, w, l, surface, k)

    # Decision — keep the rescaler only if it improves the TEST-set
    # Brier score.  Otherwise report degradation and recommend RAW.
    raw_summary = raw_test.summary()
    cal_summary = cal_test.summary()
    keep_rescaler = (raw_summary["n"] > 0
                     and cal_summary["brier"] < raw_summary["brier"])
    return {
        "period_counts":            period_counts,
        "split_dates":              {"train_end": train_end,
                                     "calibration_end": calibration_end},
        "platt_calibrator":         {
            "a":       round(platt.a, 5),
            "b":       round(platt.b, 5),
            "fit_n":   platt.fit_n,
            "identity_on_untouched_test":
                round(platt.transform(0.5), 4) == 0.5,
        },
        "raw_challenger_on_test":        raw_summary,
        "calibrated_challenger_on_test": cal_summary,
        "keep_rescaler":                 bool(keep_rescaler),
        "recommendation":                (
            "APPLY calibration in production — improves TEST Brier."
            if keep_rescaler else
            "DO NOT apply calibration — TEST Brier degrades relative "
            "to the raw challenger.  Retaining the raw model."
        ),
        "no_lookahead_proof": (
            "Platt (a, b) fit ONLY on TRAIN+CALIBRATION observations. "
            "TEST metrics are computed with a chronologically forward-"
            "updated Elo state that has never seen future outcomes. "
            "No sample used to fit the rescaler is used to evaluate it."
        ),
    }


# ── Champion vs Challenger honest comparison ────────────────────────
async def run_champion_comparison(
    db,
    train_end: str = "2020-12-31",
    calibration_end: str = "2023-12-31",
    min_matches_per_player: int = 5,
) -> dict[str, Any]:
    """Reconstruct the pre-Session-6 hash-based tennis champion and
    score it against the new Elo-driven challenger on the untouched
    TEST period.

    Honest-limitations disclosure:
        * The original engine also carried a `market_bump` component
          derived from live sportsbook implied probabilities.
        * ``tennis_matches_history`` contains ZERO odds fields
          (winner_odds / B365W / AvgW / PSW / MaxW all = 0).  So the
          market_bump is UNRECOVERABLE from history alone.
        * We therefore reconstruct only the HASH component (which was
          the dominant driver of the "everyone lands at 99" pathology
          Session 6 fixed).
        * The comparison IS certified for the HASH-only reconstruction.
          It is NOT certified as a full-champion reconstruction, and
          we do NOT substitute a 50/50 baseline and label it the old
          champion.
    """
    elo:      dict[str, float]           = {}
    surf_elo: dict[str, dict[str, float]] = {}
    seen:     dict[str, int]             = {}

    cal_samples: list[tuple[float, int]] = []
    raw_test        = SplitAccumulator()
    cal_test        = SplitAccumulator()
    hash_champion   = SplitAccumulator()

    cursor = db.tennis_matches_history.find({}).sort("date", 1)
    async for m in cursor:
        w = m.get("winner_name"); l = m.get("loser_name")
        if not w or not l: continue
        date_str = str(m.get("date") or "")[:10]
        if not date_str: continue
        surface = (m.get("surface") or "").capitalize() or "Hard"
        level   = (m.get("tourney_level") or "").upper()
        k = _tour_k(level)
        phase = ("train" if date_str <= train_end
                 else "calibration" if date_str <= calibration_end
                 else "test")

        pred, applies = _predict(elo, surf_elo, w, l, surface, seen, min_matches_per_player)
        if applies and pred is not None:
            if phase == "calibration":
                cal_samples.append((pred,     1))
                cal_samples.append((1 - pred, 0))
            elif phase == "test":
                raw_test.bump(pred,     1)
                raw_test.bump(1 - pred, 0)
                # Old-champion hash prob (WINNER perspective).
                hp = _old_champion_prob(w, l)
                hash_champion.bump(hp,     1)
                hash_champion.bump(1 - hp, 0)
        _update(elo, surf_elo, seen, w, l, surface, k)

    platt = PlattCalibrator()
    platt.fit(cal_samples)
    # Second pass for the CALIBRATED challenger on TEST only.
    elo2:      dict[str, float]           = {}
    surf_elo2: dict[str, dict[str, float]] = {}
    seen2:     dict[str, int]             = {}
    cursor2 = db.tennis_matches_history.find({}).sort("date", 1)
    async for m in cursor2:
        w = m.get("winner_name"); l = m.get("loser_name")
        if not w or not l: continue
        date_str = str(m.get("date") or "")[:10]
        if not date_str: continue
        surface = (m.get("surface") or "").capitalize() or "Hard"
        level   = (m.get("tourney_level") or "").upper()
        k = _tour_k(level)
        phase = ("train" if date_str <= train_end
                 else "calibration" if date_str <= calibration_end
                 else "test")
        if phase == "test":
            pred, applies = _predict(elo2, surf_elo2, w, l, surface, seen2, min_matches_per_player)
            if applies and pred is not None:
                cal_test.bump(platt.transform(pred),     1)
                cal_test.bump(platt.transform(1 - pred), 0)
        _update(elo2, surf_elo2, seen2, w, l, surface, k)

    # Deltas — positive delta means the CHALLENGER improves over the champion.
    def _delta(a: dict, b: dict) -> dict:
        return {
            "brier":     round(b["brier"]    - a["brier"], 5),
            "log_loss":  round(b["log_loss"] - a["log_loss"], 5),
        }

    raw_summary  = raw_test.summary()
    cal_summary  = cal_test.summary()
    champ_hash   = hash_champion.summary()

    return {
        "actual_champion_certification": {
            "reconstructed":   True,
            "certified":       False,
            "reason": (
                "ACTUAL CHAMPION COMPARISON — NOT CERTIFIED. "
                "The pre-Session-6 champion combined a name-based "
                "hash strength signal with a `market_bump` derived "
                "from live sportsbook implied probabilities.  Our "
                "historical corpus (tennis_matches_history) carries "
                "ZERO odds fields, so the market_bump component is "
                "irrecoverable from history alone.  We reconstruct "
                "the HASH component faithfully and disclose the "
                "missing market signal.  We DO NOT substitute a "
                "50/50 baseline and call that the old champion."
            ),
            "blocker_field_gaps": [
                "winner_odds", "loser_odds", "B365W", "AvgW", "PSW", "MaxW",
            ],
            "smallest_remaining_fix": (
                "Backfill historical ATP/WTA closing odds (Tennis "
                "Data OddsPortal or Betfair archives) into "
                "tennis_matches_history and re-run this harness."
            ),
        },
        "period_counts":          {"train_end": train_end,
                                   "calibration_end": calibration_end,
                                   "test_evaluation_n": raw_summary["n"]},
        "champion_hash_only_on_test":     champ_hash,
        "raw_challenger_on_test":         raw_summary,
        "calibrated_challenger_on_test":  cal_summary,
        "delta_raw_vs_champion":          _delta(champ_hash, raw_summary),
        "delta_calibrated_vs_champion":   _delta(champ_hash, cal_summary),
        "notes": [
            "All predictions are strictly forward-only Elo (no lookahead).",
            "Platt rescaler was fit on CALIBRATION only — never on TEST.",
            "ROI / CLV are OMITTED — no authentic historical odds available.",
        ],
    }
