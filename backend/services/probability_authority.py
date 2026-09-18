"""PERKLOCKS PROBABILITY AUTHORITY — the ONE boundary between specialized
sport/market models and publication.

Flow (permanent):
  point-in-time features → specialized model → raw_model_probability
  → market calibrator (registry champion) → uncertainty/evidence shrinkage
  → calibrated_probability → existing Lock Score engine → immutable publication

Modes (env PROBABILITY_AUTHORITY_MODE): ``shadow`` (default) stamps the
contract beside production truth without altering win_probability;
``active`` lets PROMOTED families (env PROB_AUTH_PROMOTE_<FAMILY>=1 or
registry status "promoted") make calibrated_probability the canonical
win_probability for NEW publications only.  Frozen rows never mutate.

Calibration dataset = settled, previously PUBLISHED rows: the frozen
``published_probability`` was written at ``published_at`` < ``event_time``,
so it is point-in-time by construction (leakage guard enforced).
Fitting is chronological walk-forward (expanding window); champion is
chosen strictly on held-out log loss with a simplicity tie-break; thin
samples keep IDENTITY (reported as NOT ENOUGH EVIDENCE).
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.probability_authority")

REGISTRY = "calibrator_registry"
MODEL_REGISTRY = "model_registry"
AUTHORITY_VERSION = "prob_auth.v1.2026-09-18"
SEED_SOURCES = ("book_implied_seed", "book_implied", "implied_seed", "market_seed")
MIN_TEST_N = 100          # held-out observations required to promote a calibrator
MIN_TRAIN_N = 200
SHRINK_K = 12.0           # pseudo-observations pulling thin-evidence probs to the prior

_MODE = lambda: (os.environ.get("PROBABILITY_AUTHORITY_MODE") or "shadow").strip().lower()
_champions: dict[str, dict] = {}   # family -> registry doc (in-process cache)


# ── family key ────────────────────────────────────────────────────────
_PROP_TOKENS = [
    ("pass yds", "PASS_YARDS"), ("passing yards", "PASS_YARDS"), ("passing yds", "PASS_YARDS"),
    ("pass tds", "PASS_TDS"), ("passing touchdowns", "PASS_TDS"), ("passing tds", "PASS_TDS"),
    ("pass completions", "PASS_COMPLETIONS"), ("completions", "PASS_COMPLETIONS"),
    ("pass attempts", "PASS_ATTEMPTS"), ("interceptions", "INTERCEPTIONS"),
    ("rush yds", "RUSH_YARDS"), ("rushing yards", "RUSH_YARDS"), ("rushing yds", "RUSH_YARDS"),
    ("rush attempts", "RUSH_ATTEMPTS"), ("rushing attempts", "RUSH_ATTEMPTS"),
    ("reception yds", "RECEIVING_YARDS"), ("receiving yards", "RECEIVING_YARDS"), ("receiving", "RECEIVING_YARDS"),
    ("receptions", "RECEPTIONS"),
    ("anytime td", "ATD"), ("anytime touchdown", "ATD"), ("touchdown scorer", "ATD"), ("to score a td", "ATD"),
    ("score or assist", "GOAL_CONTRIBUTION"), ("goal or assist", "GOAL_CONTRIBUTION"),
    ("anytime goal", "GOALSCORER"), ("goal scorer", "GOALSCORER"), ("goalscorer", "GOALSCORER"), ("to score", "GOALSCORER"),
    ("anytime assist", "ASSISTS"), ("assists", "ASSISTS"), ("assist", "ASSISTS"),
    ("shots on target", "SOT"), ("shots", "SHOTS"), ("hits + runs + rbis", "HRR"), ("total bases", "TOTAL_BASES"),
    ("home run", "HOME_RUNS"), ("rbis", "RBIS"), ("hits", "HITS"), ("strikeouts", "STRIKEOUTS"),
    ("outs recorded", "OUTS"), ("pts + reb + ast", "PRA"), ("rebounds", "REBOUNDS"), ("3-pointers", "3PM"),
    ("threes", "3PM"), ("three pointers", "3PM"), ("points", "POINTS"), ("steals", "STEALS"), ("blocks", "BLOCKS"),
    ("aces", "ACES"), ("double faults", "DOUBLE_FAULTS"), ("nrfi", "NRFI"),
]


_TEAM_AMBIGUOUS = {"points", "assists", "assist", "shots", "hits", "to score", "completions",
                   "interceptions", "blocks", "steals", "receiving"}


def market_family_key(pick: dict) -> str:
    sport = str(pick.get("sport") or "").upper().replace(" ", "")
    m = str(pick.get("market") or "").lower()
    fam = ""
    is_player = bool(pick.get("player_name") or pick.get("canonical_player_id")
                     or "player" in str(pick.get("market_key") or "") or "(" in m)
    for tok, f in _PROP_TOKENS:
        if tok in m and (is_player or tok not in _TEAM_AMBIGUOUS):
            fam = f
            break
    if fam and "total points" in m and not is_player:
        fam = ""
    if not fam:
        try:
            from services.canonical_publication_boundary import _derive_market_family
            fam = (_derive_market_family(pick) or "").upper()
        except Exception:
            fam = ""
        if fam in ("MONEYLINE", "H2H"):
            fam = "ML"
        elif fam in ("RUN_LINE", "PUCK_LINE"):
            fam = "SPREAD"
        elif fam == "TEAM_TOTAL":
            fam = "TEAM_TOTAL"
        elif fam.startswith("TOTAL") or fam == "TOTALS":
            fam = "TOTAL"
        if "btts" in m or "both teams" in m:
            fam = "BTTS"
        elif "double chance" in m or "draw no bet" in m:
            fam = "DC_DNB"
    if not fam:
        mk = str(pick.get("market_key") or "").lower()
        fam = {"h2h": "ML", "moneyline": "ML", "spreads": "SPREAD", "spread": "SPREAD",
               "totals": "TOTAL", "total": "TOTAL", "btts": "BTTS", "draw_no_bet": "DC_DNB",
               "double_chance": "DC_DNB", "team_totals": "TEAM_TOTAL"}.get(mk, "")
    return f"{sport}_{fam or 'UNKNOWN'}"


# ── contract ─────────────────────────────────────────────────────────
@dataclass
class ProbabilityContract:
    sport: str
    market_family: str
    canonical_event_id: Optional[str]
    player_id: Optional[str]
    selection: Optional[str]
    sportsbook_line: Optional[float]
    sportsbook_odds: Optional[int]
    raw_model_probability: Optional[float]
    calibrated_probability: Optional[float]
    probability_uncertainty: Optional[float]
    effective_sample_size: Optional[float]
    probability_source: str
    model_version: str
    feature_schema_version: str
    calibrator_version: str
    calibration_method: str
    training_cutoff: Optional[str]
    calibration_cutoff: Optional[str]
    evidence_quality: str
    generated_at: str
    publication_eligible: bool
    fallback_reason: Optional[str] = None
    authority_mode: str = "shadow"
    promoted: bool = False
    probability_delta: Optional[float] = None
    authority_version: str = AUTHORITY_VERSION
    closure: Optional[dict] = None          # sport-family closure record (services.probability_closures)
    closure_probability: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ── calibrators (pure numpy) ─────────────────────────────────────────
def _clip(p: float) -> float:
    return min(0.999, max(0.001, float(p)))


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _fit_platt(p, y):
    """Logistic regression on logit(p): sigmoid(a*logit(p)+b) via Newton."""
    import numpy as np
    x = np.array([_logit(v) for v in p]); yv = np.array(y, dtype=float)
    a, b = 1.0, 0.0
    for _ in range(50):
        z = a * x + b; s = 1 / (1 + np.exp(-z)); w = s * (1 - s) + 1e-9
        g = np.array([np.sum((s - yv) * x), np.sum(s - yv)])
        H = np.array([[np.sum(w * x * x) + 1e-3, np.sum(w * x)], [np.sum(w * x), np.sum(w) + 1e-3]])
        step = np.linalg.solve(H, g); a -= step[0]; b -= step[1]
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"a": float(a), "b": float(b)}


def _fit_beta(p, y):
    """Beta calibration: sigmoid(a*ln p - c*ln(1-p) + b)."""
    import numpy as np
    x1 = np.array([math.log(_clip(v)) for v in p]); x2 = np.array([-math.log(1 - _clip(v)) for v in p])
    X = np.column_stack([x1, x2, np.ones(len(p))]); yv = np.array(y, dtype=float); w = np.array([1.0, 1.0, 0.0])
    for _ in range(60):
        s = 1 / (1 + np.exp(-(X @ w))); W = s * (1 - s) + 1e-9
        g = X.T @ (s - yv); H = (X * W[:, None]).T @ X + 1e-3 * np.eye(3)
        step = np.linalg.solve(H, g); w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return {"a": float(w[0]), "c": float(w[1]), "b": float(w[2])}


def _fit_isotonic(p, y):
    """PAV isotonic regression, stored as breakpoints (needs large N)."""
    import numpy as np
    order = np.argsort(p); xs = np.array(p)[order]; ys = np.array(y, dtype=float)[order]
    blocks = [[xs[i], xs[i], ys[i], 1.0] for i in range(len(xs))]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] > blocks[i + 1][2]:
            a, b = blocks[i], blocks[i + 1]
            n = a[3] + b[3]; blocks[i] = [a[0], b[1], (a[2] * a[3] + b[2] * b[3]) / n, n]
            del blocks[i + 1]; i = max(i - 1, 0)
        else:
            i += 1
    return {"x": [float(b[1]) for b in blocks], "y": [float(b[2]) for b in blocks]}


def apply_calibrator(method: str, params: dict, p: float) -> float:
    p = _clip(p)
    if method == "platt":
        return _clip(_sigmoid(params["a"] * _logit(p) + params["b"]))
    if method == "beta":
        return _clip(_sigmoid(params["a"] * math.log(p) - params["c"] * math.log(1 - p) + params["b"]))
    if method == "isotonic":
        xs, ys = params["x"], params["y"]
        for i, x in enumerate(xs):
            if p <= x:
                if i == 0:
                    return _clip(ys[0])
                x0, y0 = xs[i - 1], ys[i - 1]
                t = (p - x0) / (x - x0) if x > x0 else 1.0
                return _clip(y0 + t * (ys[i] - y0))
        return _clip(ys[-1])
    return p  # identity


def _metrics(p, y) -> dict:
    n = len(p)
    if not n:
        return {"n": 0}
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / n
    ll = -sum(yi * math.log(_clip(pi)) + (1 - yi) * math.log(1 - _clip(pi)) for pi, yi in zip(p, y)) / n
    bands = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.01)]
    table = []
    ece = 0.0
    for lo, hi in bands:
        idx = [i for i, pi in enumerate(p) if lo <= pi < hi]
        if not idx:
            table.append({"band": f"{int(lo*100)}-{int(min(hi,1)*100)}", "n": 0}); continue
        pred = sum(p[i] for i in idx) / len(idx); obs = sum(y[i] for i in idx) / len(idx)
        ece += abs(pred - obs) * len(idx) / n
        table.append({"band": f"{int(lo*100)}-{int(min(hi,1)*100)}", "n": len(idx),
                      "predicted": round(pred, 4), "observed": round(obs, 4), "gap": round(obs - pred, 4)})
    hi_idx = [i for i, pi in enumerate(p) if pi >= 0.9]
    return {"n": n, "brier": round(brier, 5), "log_loss": round(ll, 5), "calibration_error": round(ece, 5),
            "reliability": table, "hi_conf_n": len(hi_idx),
            "hi_conf_predicted": round(sum(p[i] for i in hi_idx) / len(hi_idx), 4) if hi_idx else None,
            "hi_conf_observed": round(sum(y[i] for i in hi_idx) / len(hi_idx), 4) if hi_idx else None}


# ── point-in-time dataset + walk-forward fit ─────────────────────────
async def build_calibration_dataset(db) -> dict[str, list[tuple[str, float, int]]]:
    """family -> chronological [(event_time, published_probability, outcome)].
    LEAKAGE GUARD: a row is admitted only if published_at < event_time."""
    out: dict[str, list] = {}
    leaked = 0
    cur = db.picks.find({"result": {"$in": ["won", "lost"]}, "published_probability": {"$ne": None}},
                        {"_id": 0, "sport": 1, "market": 1, "market_key": 1, "player_name": 1,
                         "canonical_player_id": 1, "published_probability": 1, "result": 1,
                         "event_time": 1, "published_at": 1})
    async for d in cur:
        et, pa = str(d.get("event_time") or ""), str(d.get("published_at") or "")
        if not et or not pa or pa[:19] >= et[:19]:
            leaked += 1
            continue
        try:
            p = float(d["published_probability"]); p = p / 100.0 if p > 1 else p
        except Exception:
            continue
        out.setdefault(market_family_key(d), []).append((et, p, 1 if d["result"] == "won" else 0))
    for k in out:
        out[k].sort(key=lambda r: r[0])
    if leaked:
        logger.info("calibration dataset: %d rows excluded by point-in-time guard", leaked)
    return out


def walk_forward_fit(rows: list[tuple[str, float, int]], n_folds: int = 3) -> dict:
    """Expanding-window chronological folds; champion = lowest held-out log loss,
    ties → simpler method.  Returns registry-ready doc (without family)."""
    n = len(rows)
    ps = [r[1] for r in rows]; ys = [r[2] for r in rows]
    result = {"n_total": n, "candidates": {}, "champion": "identity", "status": "shadow",
              "method": "identity", "params": {}, "prior": round(sum(ys) / n, 4) if n else None,
              "training_range": [rows[0][0], rows[-1][0]] if n else None}
    if n < MIN_TRAIN_N + MIN_TEST_N:
        result["status"] = "NOT_ENOUGH_EVIDENCE"; result["raw_metrics"] = _metrics(ps, ys)
        return result
    fitters = {"identity": None, "platt": _fit_platt, "beta": _fit_beta}
    if n >= 1500:
        fitters["isotonic"] = _fit_isotonic
    folds = []
    step = (n - MIN_TRAIN_N) // n_folds
    for f in range(n_folds):
        tr_end = MIN_TRAIN_N + f * step; te_end = min(n, tr_end + step) if f < n_folds - 1 else n
        if te_end - tr_end < 30:
            continue
        folds.append((tr_end, te_end))
    oos: dict[str, tuple[list, list]] = {m: ([], []) for m in fitters}
    for tr_end, te_end in folds:
        for m, fit in fitters.items():
            params = fit(ps[:tr_end], ys[:tr_end]) if fit else {}
            oos[m][0].extend(apply_calibrator(m, params, p) for p in ps[tr_end:te_end])
            oos[m][1].extend(ys[tr_end:te_end])
    ranking = []
    for m, (pp, yy) in oos.items():
        met = _metrics(pp, yy); result["candidates"][m] = met
        ranking.append((met.get("log_loss", 9), {"identity": 0, "platt": 1, "beta": 2, "isotonic": 3}[m], m))
    ranking.sort()
    champ = ranking[0][2]
    ident_ll = result["candidates"]["identity"]["log_loss"]
    if champ != "identity" and (ident_ll - result["candidates"][champ]["log_loss"]) < 0.002:
        champ = "identity"  # not a defensible improvement → keep simpler
    result["champion"] = champ; result["method"] = champ
    result["params"] = fitters[champ](ps, ys) if fitters[champ] else {}
    result["test_n"] = result["candidates"][champ]["n"]
    result["raw_metrics"] = result["candidates"]["identity"]
    result["calibrated_metrics"] = result["candidates"][champ]
    result["status"] = "shadow_ready" if result["test_n"] >= MIN_TEST_N else "NOT_ENOUGH_EVIDENCE"
    return result


async def fit_all_calibrators(db) -> dict:
    """Fit every family from the point-in-time dataset and persist to the
    registry (versioned, additive; never touches picks)."""
    data = await build_calibration_dataset(db)
    now = datetime.now(timezone.utc).isoformat()
    summary = {}
    for fam, rows in data.items():
        doc = walk_forward_fit(rows)
        promoted_flag = os.environ.get(f"PROB_AUTH_PROMOTE_{fam}", "0") == "1"
        prev = await db[REGISTRY].find_one({"family": fam, "is_active": True}, {"_id": 0, "version": 1})
        version = int((prev or {}).get("version") or 0) + 1
        doc.update({"family": fam, "version": version, "calibrator_version": f"{fam}.cal.v{version}",
                    "authority_version": AUTHORITY_VERSION, "fitted_at": now, "is_active": True,
                    "calibration_cutoff": rows[-1][0] if rows else None,
                    "promotion_status": "promoted" if (promoted_flag and doc["status"] == "shadow_ready") else doc["status"]})
        await db[REGISTRY].update_many({"family": fam, "is_active": True}, {"$set": {"is_active": False}})
        await db[REGISTRY].insert_one(dict(doc))
        _champions[fam] = doc
        summary[fam] = {k: doc.get(k) for k in ("n_total", "test_n", "champion", "status", "promotion_status")}
        cm = doc.get("calibrated_metrics") or {}; rm = doc.get("raw_metrics") or {}
        summary[fam].update({"raw_brier": rm.get("brier"), "cal_brier": cm.get("brier"),
                             "raw_log_loss": rm.get("log_loss"), "cal_log_loss": cm.get("log_loss"),
                             "cal_error": cm.get("calibration_error"), "hi_conf_n": cm.get("hi_conf_n"),
                             "hi_conf_predicted": cm.get("hi_conf_predicted"), "hi_conf_observed": cm.get("hi_conf_observed")})
    logger.info("probability authority: fitted %d families", len(summary))
    return summary


async def load_champions(db) -> None:
    async for d in db[REGISTRY].find({"is_active": True}, {"_id": 0}):
        _champions[d["family"]] = d


# ── evaluation (the boundary) ────────────────────────────────────────
def _ess(pick: dict) -> Optional[float]:
    for k in ("effective_sample_size", "sample_games", "sample_size", "n_games", "historical_n"):
        v = pick.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    ev = pick.get("atd_evidence") or {}
    if isinstance(ev, dict) and isinstance(ev.get("sample_games"), (int, float)):
        return float(ev["sample_games"])
    return None


def evaluate(pick: dict) -> ProbabilityContract:
    fam = market_family_key(pick)
    champ = _champions.get(fam) or {}
    wp = pick.get("win_probability")
    raw = None
    try:
        if wp is not None and not (isinstance(wp, float) and math.isnan(wp)):
            raw = float(wp); raw = raw / 100.0 if raw > 1 else raw
    except Exception:
        raw = None
    source = str(pick.get("probability_source") or pick.get("edge_source") or "sport_model")
    # ── sport-family closure (minutes/lineup, threshold distribution, …) ──
    closure = None
    closure_p = None
    try:
        from services.probability_closures import apply_closure
        closure = apply_closure(pick, fam, raw)
    except Exception as _cexc:  # closure bugs must never block publication truth
        logger.warning("probability closure failed for %s: %s", fam, _cexc)
        closure = None
    p_in = raw
    if closure is not None and closure.probability is not None and raw is not None:
        closure_p = closure.probability
        p_in = closure_p
    method = champ.get("method", "identity") if champ.get("status") == "shadow_ready" else "identity"
    cal = apply_calibrator(method, champ.get("params") or {}, p_in) if p_in is not None else None
    ess = _ess(pick)
    unc = None
    if cal is not None:
        n_eff = ess if ess else 25.0
        unc = math.sqrt(cal * (1 - cal) / max(n_eff, 1.0))
        if closure is not None:
            unc = math.sqrt(unc ** 2 + float(closure.uncertainty_extra or 0.0) ** 2)
        unc = round(unc, 4)
        prior = champ.get("prior")
        if ess is not None and ess < SHRINK_K * 2 and isinstance(prior, (int, float)):
            cal = (ess * cal + SHRINK_K * float(prior)) / (ess + SHRINK_K)
        cal = round(cal, 4)
    reason = None
    eligible = True
    if raw is None:
        eligible, reason = False, "MODEL_PROBABILITY_INVALID"
    elif any(source.startswith(s) for s in SEED_SOURCES):
        eligible, reason = False, "BOOK_IMPLIED_SEED_NOT_PUBLISHABLE"
    elif pick.get("book_odds") is None or pick.get("no_real_book_line"):
        eligible, reason = False, "REAL_LINE_REQUIRED"
    elif closure is not None and not closure.publication_eligible:
        eligible, reason = False, closure.fallback_reason or "CLOSURE_NOT_PUBLISHABLE"
    _changes_prob = (method != "identity") or (closure_p is not None and raw is not None and abs(closure_p - raw) > 1e-9)
    promoted = bool(champ.get("promotion_status") == "promoted"
                    or os.environ.get(f"PROB_AUTH_PROMOTE_{fam}", "0") == "1") and _MODE() == "active" and _changes_prob
    quality = "STRONG" if (ess or 0) >= 15 else "MODEL" if (ess or 0) >= 5 else "LIMITED" if ess else "UNKNOWN"
    return ProbabilityContract(
        sport=str(pick.get("sport") or ""), market_family=fam,
        canonical_event_id=pick.get("canonical_event_id") or pick.get("event_id") or pick.get("event"),
        player_id=pick.get("canonical_player_id") or pick.get("player_id"),
        selection=pick.get("selection") or pick.get("side"),
        sportsbook_line=pick.get("line") if isinstance(pick.get("line"), (int, float)) else None,
        sportsbook_odds=pick.get("book_odds") if isinstance(pick.get("book_odds"), int) else None,
        raw_model_probability=round(raw, 4) if raw is not None else None,
        calibrated_probability=cal, probability_uncertainty=unc, effective_sample_size=ess,
        probability_source=source, model_version=str(pick.get("model_version") or "legacy_unknown"),
        feature_schema_version=str(pick.get("feature_snapshot_version") or "legacy_unknown"),
        calibrator_version=str(champ.get("calibrator_version") or f"{fam}.identity.v0"),
        calibration_method=method, training_cutoff=(champ.get("training_range") or [None, None])[0],
        calibration_cutoff=champ.get("calibration_cutoff"), evidence_quality=quality,
        generated_at=datetime.now(timezone.utc).isoformat(), publication_eligible=eligible,
        fallback_reason=reason, authority_mode=_MODE(), promoted=promoted,
        probability_delta=round((cal - raw) * 100, 2) if (cal is not None and raw is not None) else None,
        closure=closure.to_dict() if closure is not None else None,
        closure_probability=closure_p,
    )


def stamp(pick: dict) -> dict:
    """Stamp the contract onto a NEW candidate (additive fields).  In
    ``active`` mode for a promoted family the calibrated probability
    becomes the canonical win_probability BEFORE Lock Score/publication;
    legacy probability is retained as provenance only."""
    c = evaluate(pick)
    pick["probability_contract"] = c.to_dict()
    pick["raw_model_probability"] = c.raw_model_probability
    pick["calibrated_probability"] = c.calibrated_probability
    pick["probability_uncertainty"] = c.probability_uncertainty
    pick["calibrator_version"] = c.calibrator_version
    pick["probability_authority_mode"] = c.authority_mode
    if not c.publication_eligible:
        pick["publication_eligible"] = False
        pick["probability_fallback_reason"] = c.fallback_reason
    if c.promoted and c.calibrated_probability is not None:
        pick["legacy_probability"] = c.raw_model_probability
        pick["win_probability"] = round(c.calibrated_probability * 100, 2)
        pick["probability_source"] = f"calibrated:{c.calibrator_version}"
        pick["lock_score_recompute_required"] = True
    return pick


__all__ = ["ProbabilityContract", "market_family_key", "evaluate", "stamp", "fit_all_calibrators",
           "load_champions", "walk_forward_fit", "build_calibration_dataset", "apply_calibrator"]
