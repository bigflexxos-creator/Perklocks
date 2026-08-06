# PHASE 4A — MAGIC TIER AUDIT

**Status:** Read-only audit.
**Ground truth:** `services/discovery/magic_finder.py`, `services/alt_line_engine/ranker.py`, `services/alt_line_engine/safeguards.py`, `services/alt_line_engine/distribution.py`, `services/alt_line_engine/explanations.py`, `routes/admin_routes.py` (Magic Tier & Alt-Line admin endpoints).

---

## 1. What "Magic Tier" is in this codebase

The user prompt uses "Magic Tier" as a probability/confidence label. In the code, two related-but-distinct constructs use the "Magic" name:

### 1.1 Magic Finder — `services/discovery/magic_finder.py`
An **aggregator** across four discovery subsystems:
1. `threshold_discovery.analyse_thresholds` — historical hit-rate ladder per player/stat.
2. `alt_line_intelligence.recommend_alt_lines` — recommendations from `alt_line_engine`.
3. `pattern_discovery.discover_patterns` — pattern hit-rate rows.
4. `situation_clustering.find_similar_situations` — nearest-neighbour game context.

Output: a JSON payload with `threshold_analysis`, `alt_line_recommendation`, `patterns`, `similar_situations`, `explanations`, `notes`.

**Not a probability model. Not a tier label. An aggregator for descriptive evidence.**

### 1.2 Alt-Line Magic Tier — `services/alt_line_engine/*`
The Phase 8 alt-line ranker. Produces an `AltLineBundle` of ranked alt lines with a `composite_score` computed as:
```
composite = 0.30·p_norm + 0.25·edge_norm + 0.20·confidence + 0.15·bucket_norm + 0.10·stability
```

**Both surfaces are ADMIN-ONLY.** Both are exposed via `routes/admin_routes.py` (lines 1901–1980), NOT via user-facing endpoints (verified by grep — no `magic_find` calls outside admin routes and tests).

---

## 2. Inputs & weighting audit

### 2.1 Alt-Line Ranker composite

| Component | Weight | Range | Source |
|---|---|---|---|
| `p_norm` | 0.30 | 0-1 | `(p_side − 0.30) / 0.70` normalisation of model probability. |
| `confidence` | 0.20 | 0-1 | `0.6·(1 − residual_std/3) + 0.4·(min(1, top_factors_n/5))`. |
| `bucket_roi_norm` | 0.15 | 0-1 (0.5 default when null) | Historical ROI from `learning_snapshots` for this probability bucket. **Defaults to 0.5** when no data. |
| `edge_norm` | 0.25 | 0-1 (0.5 default when null) | `(p_model − p_implied + 0.05) / 0.15` — normalised to +0.05 to +0.20 edge band. |
| `stability` | 0.10 | 0-1 | Adjacent-threshold P(over) diff — lower diff = higher stability. |

### 2.2 Thresholds & sample-size caps

**`safeguards.is_safe_for_alt_lines(db, sport, player, stat)`** — checked before every bundle emit:
- Confirms the player exists in `player_game_logs`.
- Confirms ≥ N recent games (threshold not confirmed by full-file read but presence implies gate).
- Returns `(False, reason)` if unsafe → bundle emits empty.

**`bucket_roi`** — pulled from `learning_snapshots` where bucket = `very_high/high/medium/low/very_low`. If no snapshot exists → the ranker uses **0.5 default**, treating unknown bucket as neutral. This means **low-sample buckets rank identically to average buckets** — inflates ranking for markets with no historical calibration data.

### 2.3 `market_projection` alt lines

`alt_line_engine/ranker.py::generate_alt_lines` emits `AltLine(source="model_projection")` for every threshold that lacks a matching bookmaker line. These synthesized recommendations WILL rank against real market-line alts.

**Contained risk:** since this is admin-only, no user is served a synthesized line. **But if wired to publication in future** (e.g. as a "Magic Tier" tab in the UI), it would violate the Real-Line policy.

---

## 3. Interaction with Lock Score

**None on the emission path.** The Magic Finder / Alt-Line Ranker do not mutate `lock_score`. They produce independent, admin-only payloads. So:
- ❌ Not fed into Lock Score.
- ❌ Not fed into `sim_runner.apply_simulations`.
- ❌ Not fed into pick emission.

**Consequence:** the "Magic Tier" in the user prompt does not correspond to any tier field on the emitted `picks` collection. It's a **descriptive layer**, not a **ranking classification** applied to the board.

**BUT:** the user prompt appears to conflate "Magic Tier" with a **hypothesized user-facing confidence tier**. If the intent is to elevate Alt-Line Ranker composite scores into a user-facing tier badge, that wiring **does not exist today** — it would need to be built.

---

## 4. Interaction with data quality

**Alt-Line Ranker:**
- `confidence` blends `residual_std` (lower = tighter distribution → more confident) with `top_factors_n` (more explanatory factors = higher confidence).
- **No hard sample-size cap** — a bundle can emit with `confidence = 0.5` (default) if `residual_std` is None.
- **No hard rejection for weak `distribution_supported = False`** — instead the bundle returns `notes: [reason]` and an empty `alt_lines` list.

**Magic Finder:**
- Each of the 4 sub-engines is wrapped in `try/except` and its failure is folded into `notes`. No cross-engine consistency check — the payload emits even if 3 of 4 sub-engines failed.

---

## 5. Interaction with model agreement & simulator

**Zero.** Neither the sport-specific simulators (`sim_mlb`/`sim_nba`/etc.) nor the Beta-Bernoulli `run_simulator` feed the Magic Finder / Alt-Line Ranker. Independence is intended.

**Consequence:** the Magic Tier composite is not corroborated by 20K-run Monte Carlo. If Phase 4B wires the Alt-Line Ranker to publication, it needs to consume the sim output as a corroboration signal (or the sim needs to consume Magic Tier as a soft prior).

---

## 6. Interaction with market price

**Alt-Line Ranker:**
- Uses `p_implied` from market alt-line dict where available.
- `edge_norm` requires both `p_model` and `p_implied`; defaults to 0.5 when `p_implied` is None (i.e. `model_projection` rows).
- **Bucket `edge` and `market_odds`** are exposed on each AltLine dataclass instance.

**No dedicated chalk/underdog re-weighting.** A `p_model = 0.85` with no market price gets `edge_norm = 0.5` (neutral), same as an edge of +5%.

---

## 7. Historical calibration by tier

`bucket_roi` (0.30-0.75 = medium-high, 0.60-0.75 = high, 0.75+ = very_high) lookups in `learning_snapshots`. **Calibration is buckets-of-model-probability, not buckets-of-composite-score.** So the composite score itself is NOT calibrated against historical ROI. **Two picks with `composite_score = 0.82` could have very different real ROIs** because the composite mixes probability with edge with stability, and only the probability bucket is historically calibrated.

**Recommendation (Phase 4B):** if Magic Tier is user-facing, calibrate `composite_score` bands (0.8+ = Tier 1, 0.6-0.8 = Tier 2, ...) against historical ROI segmented by sport+stat+bucket.

---

## 8. Can a weak model receive a high tier?

**Yes.** The composite averages 5 components with default-0.5 fallbacks. A pick can score:
- `p_norm = 0.85` (from book-follow model that reproduces book_implied 90% for a heavy chalk)
- `confidence = 0.5` (default when residual_std is None)
- `bucket_roi = 0.5` (default when no historical data)
- `edge = 0.5` (default when no market line — `model_projection`)
- `stability = 1.0` (calm gradient)

Composite = 0.30·0.85 + 0.20·0.5 + 0.15·0.5 + 0.25·0.5 + 0.10·1.0 = **0.66** — mid-tier. This is not disastrous, but the default 0.5s inflate confidence for markets with **no calibration**.

**Worse case:** an unseen market where `book_implied` is 95% (deep chalk). `p_norm = (0.95 - 0.30)/0.70 = 0.93`. Composite ≈ 0.75 — high tier — **on nothing more than the book price**.

---

## 9. Can one inflated feature dominate?

**Yes.** The largest single weight is `p_norm` at 0.30. But `edge` at 0.25 is close. If `p_model` is inflated (e.g. from a book-follow model), `p_norm` and `edge` are BOTH inflated (they're correlated), pushing composite by 0.55 · <inflation>. **A single misspecified `p_model` cascades through two of the five components.**

Fix: swap `p_norm` for the model's residual-adjusted probability or blend it with the historical bucket midpoint.

---

## 10. Does Magic Tier guarantee a win?

**No — but it is presented as a classification tier without a probability disclaimer.** The composite score has no obvious "this is not a probability" label in the ranker output. If surfaced to users, they will read `composite_score = 0.82` as "82% likely to hit" — WRONG interpretation.

---

## 11. Files audited

- `services/discovery/magic_finder.py` — full read.
- `services/alt_line_engine/ranker.py` — full read.
- `services/alt_line_engine/__init__.py`, `safeguards.py`, `distribution.py`, `explanations.py` — signature-level.
- `routes/admin_routes.py` — endpoint wiring (lines 1901-1980).
- `tests/test_iter98_discovery.py`, `tests/test_iter113_alt_line_engine.py` — cross-checked behavioural invariants.

---

## 12. Defects raised (Magic Tier layer)

| ID | Description | Impact | Likelihood |
|---|---|---|---|
| MT-1 | Composite `bucket_roi` defaults to 0.5 when historical data absent — low-sample markets rank equal to calibrated ones. | 🟡 Medium | Certain (structural) |
| MT-2 | Composite score not calibrated against historical ROI — tier labels have no probability meaning. | 🔴 High if surfaced to users | Certain |
| MT-3 | `p_norm` and `edge` are correlated (both derive from `p_model`) — a single inflated probability moves 55% of the composite. | 🔴 High | Certain (structural) |
| MT-4 | Weak models (book-follow inputs) can achieve mid-to-high composite scores without any real signal. | 🔴 High | Certain (structural) |
| MT-5 | `source="model_projection"` alt lines rank alongside real market alts — real-line policy would be violated if this bundle is surfaced. | 🟡 Medium (contained to admin today) | Certain |
| MT-6 | No hard sample-size cap — a bundle emits with `confidence = 0.5` default. | 🟡 Medium | Certain |
| MT-7 | No cross-engine consistency check in Magic Finder — payload emits even if most sub-engines failed. | 🟡 Low | Certain |
| MT-8 | If wired to publication, no interaction with Lock Score / sim consensus / data-quality caps — Magic Tier acts alone. | 🔴 High if wired | Latent |
