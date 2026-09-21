# Parlay 3.0 · Universal Sport Closure — 2026-06-21

Continues from `rollover_immutability_and_parlay30_closure_2026_06_21.md`.
Rollover architecture is preserved unchanged.

## 1 — Universal ModePolicy Proof

Single `ModePolicy` authority in `services/parlay/mode_policy.py` is
sport-agnostic — every layer (route, feasibility, optimizer, health,
pin validator, alternate ranker) imports the same object.

Verified: no `if sport == "NFL"` / `if sport == "MLB"` branches remain
in the parlay path.  The only surviving sport-specific data is the
declarative fail-closed set in the dependency authority, which now
covers ALL supported sports uniformly.

## 2 — Universal Feasibility Matrix (Canonical Parity Preview)

| SPORT   | MODE       | Status       | canonical | mode_eligible | events | max | parlays | actual/target | reasons                       |
| ------- | ---------- | ------------ | --------: | ------------: | -----: | --: | ------: | ------------- | ----------------------------- |
| AUTO    | STANDARD   | READY        | 97        | 97            | 6      | 6   | 3       | 2/3           |                               |
| AUTO    | ADV_SAFER  | READY        | 19        | 19            | 3      | 3   | 3       | 2/3           |                               |
| AUTO    | ADV_EV     | READY        | 92        | 92            | 6      | 6   | 3       | 2/3           |                               |
| AUTO    | HR_10      | PARTIAL_ONLY | 97        | 97            | 6      | 6   | 3       | 5/10          | INSUFFICIENT_UNIQUE_EVENTS    |
| AUTO    | HR_15      | PARTIAL_ONLY | 97        | 97            | 6      | 6   | 3       | 5/15          | INSUFFICIENT_UNIQUE_EVENTS+MARKET_CONCENTRATION_LIMIT |
| AUTO    | HR_20      | PARTIAL_ONLY | 97        | 97            | 6      | 6   | 3       | 5/20          | INSUFFICIENT_UNIQUE_EVENTS+MARKET_CONCENTRATION_LIMIT |
| MLB     | STANDARD   | READY        | 40        | 40            | 4      | 4   | 3       | 2/3           |                               |
| MLB     | ADV_SAFER  | PARTIAL_ONLY | 7         | 7             | 2      | 2   | 1       | 2/3           | INSUFFICIENT_UNIQUE_EVENTS    |
| MLB     | ADV_EV     | READY        | 40        | 40            | 4      | 4   | 3       | 2/3           |                               |
| MLB     | HR_10      | INSUFFICIENT | 40        | 40            | 4      | 4   | 3 (auto-expand) | 7/10  | INSUFFICIENT_UNIQUE_EVENTS    |
| MLB     | HR_15      | INSUFFICIENT | 40        | 40            | 4      | 4   | 3 (auto-expand) | 7/15  | INSUFFICIENT_UNIQUE_EVENTS    |
| MLB     | HR_20      | INSUFFICIENT | 40        | 40            | 4      | 4   | 3 (auto-expand) | 8/20  | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | STANDARD   | INSUFFICIENT | 56        | 56            | 1      | 1   | 3 (expand) | 2/3        | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | ADV_SAFER  | INSUFFICIENT | 12        | 12            | 1      | 1   | 3 (expand) | 2/3        | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | ADV_EV     | INSUFFICIENT | 51        | 51            | 1      | 1   | 3 (expand) | 2/3        | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | HR_10      | INSUFFICIENT | 56        | 56            | 1      | 1   | 0       | -             | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | HR_15      | INSUFFICIENT | 56        | 56            | 1      | 1   | 0       | -             | INSUFFICIENT_UNIQUE_EVENTS    |
| NFL     | HR_20      | INSUFFICIENT | 56        | 56            | 1      | 1   | 0       | -             | INSUFFICIENT_UNIQUE_EVENTS    |
| CFB     | ALL MODES  | INSUFFICIENT | 0         | 0             | 0      | 0   | 0       | -             | INSUFFICIENT_CANONICAL_LEGS   |
| NBA     | ALL MODES  | INSUFFICIENT | 0         | 0             | 0      | 0   | 0       | -             | INSUFFICIENT_CANONICAL_LEGS   |
| SOCCER  | STANDARD   | INSUFFICIENT | 1         | 1             | 1      | 1   | 3 (expand) | 2/3        | INSUFFICIENT_UNIQUE_EVENTS    |
| SOCCER  | HR_10-20   | INSUFFICIENT | 1         | 1             | 1      | 1   | 3 (expand) | 5/10-5/20  | INSUFFICIENT_UNIQUE_EVENTS    |
| TENNIS  | ALL MODES  | INSUFFICIENT | 0         | 0             | 0      | 0   | 0       | -             | INSUFFICIENT_CANONICAL_LEGS   |

Every empty result carries a machine-readable reason code and is
TRUTHFUL — the current canonical slate genuinely does not contain
eligible CFB / NBA / Tennis picks, and NFL has only 1 unique event
(cannot build 10-leg dependency-safe ticket).  Not defects.

## 3 — Universal Dependency Authority

`services/parlay/dependency.py` is now the ONE authority for pair
classification.  Same-event now fails closed for EVERY supported sport
(MLB / NFL / NBA / NHL / UFC / Tennis / CFB / Soccer) — closing the
previous Soccer carve-out.

**Proof (Soccer HR-10 build)** — every card contains 5 legs from 5
DIFFERENT Soccer matches:
```
Barnet vs Crawley · FC Cincinnati vs Montreal · New England vs Real Salt Lake
Finland vs San Marino · Wales vs Portugal   → 5 unique events
```
No same-match combinations survived, confirming the dependency
authority is active universally.

## 4 — HIGH_RISK Edge Policy Decision

Decision: HIGH_RISK is a **HIGH-PAYOUT / HIGH-VARIANCE** mode, NOT an
EV-oriented mode.  ADVANCED_HIGH_EV explicitly exists for EV.
Therefore HIGH_RISK's edge is now a **ranking input** (via
`score_leg`'s `edge_component`), NOT a hard admission gate.

Changed: `HIGH_RISK.min_edge_pct = None` (was 1.0).  The previous +1 %
veto was one of the originally-identified starvation sources.  Live
proof: HIGH_RISK NFL ticket now legitimately admits a Davante Adams
-1.4 % edge leg (Lock 96) that the +1 % veto would have vetoed.

## 5 — Health Weights Wired Through

`parlay_optimizer.parlay_health(policy=...)` now uses per-mode
`health_weights` from the ModePolicy.  Regression test
`test_high_risk_ticket_health_does_not_collapse_from_leg_count`
asserts the HIGH_RISK score gap between a 3-leg and 12-leg ticket is
SMALLER than under default weights — the mode no longer
auto-punishes 12-leg builds for being 12 legs.

## 6 — Pin Validation Proof

`services/parlay/pins_and_alternates.validate_pin` returns
PIN_ACCEPTED / PIN_CONFLICT with machine-readable reason codes:
`NOT_CANONICAL`, `NO_REAL_ODDS`, `BELOW_LOCK_FLOOR`,
`BELOW_EDGE_FLOOR`, `STATUS_INVALID`, `DEPENDENCY_CONFLICT`.

Live proof (nonexistent pin id):
```json
[{"pick_id":"NON_EXISTENT_ID_12345","status":"PIN_CONFLICT",
  "reason_code":"NOT_CANONICAL",
  "message":"This wager is no longer available on the current board."}]
```

`pin_report` is now attached to every parlay response so the frontend
can render truthful PIN_CONFLICT copy.

## 7 — Alternate Ranking Proof

Alternates now rank by dependency safety → mode-eligibility distance
→ event dup → market-family dup → joint-survival delta → Lock score
(final tiebreak), not raw Lock.  Regression tests assert:

* Same-event candidates are EXCLUDED (not just ranked low).
* Higher-WP candidate ranks ABOVE higher-Lock candidate when the
  higher-WP preserves more of the base survival.

## 8 — Regenerate Determinism Proof

* `refresh_nonce=0` twice → **byte-identical ticket fingerprints**.
* `refresh_nonce=5` → different fingerprints (controlled diversity).

Response now includes:
```json
"regenerate": {
  "refresh_nonce": 0,
  "deterministic_base": true,
  "ticket_fingerprints": [ [sorted leg ids per card], ... ]
}
```

## 9 — Saved-Parlay Universal Wager Immutability Proof

Built a 4-sport parlay (MLB · NFL · Soccer · Tennis) and saved it via
`parlay_history.save_parlay`.  Every leg preserved:
sport, event, market, selection, line, book_odds, sportsbook,
win_probability, published_lock_score, `frozen_wager_version=2`.
`frozen_wager_version=2` also stamped at the doc level.
Save was idempotent — re-invoking with the same legs returned the
same parlay id.

## 10 — Frontend Physical Verification

Screenshots captured on Canonical Parity Preview:
* Rollover — v6-official-slate, `frozen_wager_provenance="leg_v2"` ✓
* Parlay STANDARD — 3 cards render cleanly ✓
* Parlay HIGH_RISK target=10 — 3 partial 6-leg cards render with
  NFL/MLB/Soccer mixed AUTO build ✓
* Regenerate button visible and enabled ✓

Backend additive fields (`is_partial`, `requested_target_legs`,
`effective_target_legs`, `feasibility`, `mode_key`, `pin_report`,
`regenerate.deterministic_base`) available for follow-up UI polish;
existing renderer is BC-compatible (no regressions observed).

## Files Changed (Universal Closure)

* `backend/services/parlay/dependency.py` — **NEW** — single dep authority
* `backend/services/parlay/pins_and_alternates.py` — **NEW** — pin
  validation + alternate ranker
* `backend/services/parlay/mode_policy.py` — HIGH_RISK edge=None (from 1.0)
* `backend/parlay_optimizer.py` — `parlay_health()` takes `policy`,
  `diversification_ok()` uses universal dep authority (no more
  hardcoded sport tuple), `is_eligible_leg()` drops hard +1 % edge
  gate, `build_top_parlays()` / `build_one_parlay()` accept `policy`
* `backend/routes/parlay_routes.py` — pin validation + alternate
  ranker wiring + `pin_report` + `regenerate` block in response
* `backend/tests/test_parlay30_universal_closure.py` — **NEW** — 26 tests

## Regression Suite

`test_rollover_immutability_and_parlay30.py`: 17 pass
`test_parlay30_universal_closure.py`: 26 pass
**Total: 43 pass · 0 fail**

## Known Truthful Insufficiencies (NOT defects)

* CFB / NBA / Tennis — zero canonical-eligible picks on the current
  Preview slate.  When the slate publishes those sports the same
  architecture generates without change.
* NFL only 1 unique event — 10-leg dependency-safe ticket is
  geometrically impossible.  Auto-expand pulled other-sport legs to
  build 2/3 partial mixed tickets on the shorter modes.
* Soccer only 1 event after canonical filter, but auto-expand pulls
  in wider Soccer slate (Champions League Women + others) — HR-10
  therefore builds a 5-leg all-Soccer ticket from 5 distinct matches.

## Production Published: NO
