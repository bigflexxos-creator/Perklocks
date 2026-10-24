# NHL FanDuel Exact-Wager-Identity Certification
## 2026-10 One-Pass Fix — Option A (strict, NHL-only, surgical)

**Date:** 2026-10-24  
**Scope:** NHL odds attachment path only. Enforces exact wager identity: `event + player + market + side + exact line + book=fanduel`. Fail-closed with `no_real_book_line=True` when the exact FanDuel wager is unavailable.

---

## Final Verdicts

| Gate | Result |
|---|---|
| **NHL_EXACT_FANDUEL_IDENTITY** | **PASS** |
| **CROSS_LINE_COLLISION** | **0** |
| **CROSS_SIDE_COLLISION** | **0** |
| **CROSS_EVENT_COLLISION** | **0** |
| **CROSS_PLAYER_COLLISION** | **0** |
| **NON_FANDUEL_FALLBACK** | **0** |
| **READY_FOR_PRODUCTION_CUTOVER** | **YES** |

---

## What Changed (code, surgical)

1. **`backend/services/observed_quote_adapter.py`**
   - `extract_observed_quotes()` bucket key is now `(market, player, side, canon(line))`. Line is a first-class dimension — Over 1.5 and Over 2.5 can no longer collapse.
   - `quotes_for()` accepts `line=`, `bookmaker=`, and `require_line_match=` filters. NHL callers set all three; non-NHL callers keep the legacy aggregation path.
   - `_canon_line()` rounds to 1-decimal precision for strict equality (guards against float noise like `2.4999999`).

2. **`backend/services/observed_quote.py`**
   - New `select_exact_fanduel_quote(quotes, pick_event_id, pick_player, pick_market_key, pick_side, pick_line)` → `(quote, reason)`. Validates each candidate quote individually against the full wager-identity tuple; fail-closed with a specific reason code (`no_fanduel_quote`, `cross_event_rejected`, `cross_player_rejected`, `cross_market_rejected`, `cross_side_rejected`, `cross_line_rejected`).
   - Verifies identity on the quote OBJECT itself — never trusts an upstream bucket key.

3. **`backend/services/observed_quote_attach_pass.py`**
   - New NHL branch inside `attach_observed_quotes()`:
     - Pulls pick's exact line via `_pick_line_value()`.
     - Calls `quotes_for(..., line=pick_line, bookmaker="fanduel", require_line_match=True)` — strict exact match, no partial fallback.
     - Runs `select_exact_fanduel_quote(...)` for defense-in-depth verification of the quote object.
     - On any mismatch: strips `observed_quotes` and sets `pick["nhl_identity_reason"]` so the universal lineage pass fail-closes with `no_real_book_line=True`.
     - **Removed the side-less fallback for NHL** (the old `quotes_for(..., side=None)` line that let Over/Under mix).
     - **No non-FanDuel fallback for NHL** — DK/BetMGM/Caesars/PointsBet are not considered.
   - Non-NHL path is byte-for-byte unchanged (MLB, Tennis still use the legacy aggregation + side-less fallback).

4. **`backend/services/quote_lineage_guard.py`**
   - `harden_lineage()` accepts optional `require_identity={bookmaker, event_id, player, market_key, side, line}`.
   - When set (NHL), re-checks the selected quote's own fields against the identity tuple. Any mismatch → `no_real_book_line=True` with a precise `lineage_reason` (`identity_book_mismatch`, `identity_event_mismatch`, `identity_player_mismatch`, `identity_market_mismatch`, `identity_side_mismatch`, `identity_line_mismatch`).
   - Returns a new `selected_identity` sub-dict carrying the verified identity, so downstream code and tests can assert on the QUOTE OBJECT, not just the pick's own fields.

5. **`backend/services/prepublication_lineage_pass.py`**
   - For NHL picks, constructs `require_identity` from the pick's own fields and passes it to `harden_lineage`.
   - Stores `pick["selected_quote_identity"]` and `pick["lineage_reason"]` on each pick so downstream observability and tests can prove identity directly on the quote object.
   - Non-NHL path uses no `require_identity` → legacy behavior preserved.

6. **`backend/tests/test_nhl_fanduel_identity.py`** — 11 new tests (all pass):
   - `test_a_cross_line_collision_resolved` — Over 1.5 and Over 2.5 are isolated.
   - `test_b_cross_side_collision_resolved` — Under never attaches to an Over pick.
   - `test_c_non_fanduel_fallback_refused` — DK/BetMGM/Caesars present, FanDuel missing → refused.
   - `test_d_cross_event_collision_refused` — wrong event_id → refused.
   - `test_e_cross_player_collision_refused` — wrong player → refused.
   - `test_f_same_identity_two_events_isolated` — identical identity minus event, the OTHER event's quote never attaches (user's extra scenario).
   - `test_g_selected_quote_object_identity_verified` — asserts `pick["selected_quote_identity"]` reflects the actual quote OBJECT, not just the pick fields.
   - `test_h_harden_lineage_fail_closed[book_mismatch]` — book=DK on a FanDuel-required pick → fail-closed with `identity_book_mismatch`.
   - `test_h_harden_lineage_fail_closed[line_mismatch]` — FanDuel quote at wrong line → fail-closed with `identity_line_mismatch`.
   - `test_h_harden_lineage_fail_closed[side_mismatch]` — FanDuel quote on wrong side → fail-closed with `identity_side_mismatch`.
   - `test_i_non_nhl_mlb_unchanged_no_identity_strictness` — proves scope: MLB still uses legacy aggregation / no FanDuel-only enforcement / no identity re-check.

---

## Test Run

```
tests/test_nhl_fanduel_identity.py::test_a_cross_line_collision_resolved           PASSED
tests/test_nhl_fanduel_identity.py::test_b_cross_side_collision_resolved           PASSED
tests/test_nhl_fanduel_identity.py::test_c_non_fanduel_fallback_refused            PASSED
tests/test_nhl_fanduel_identity.py::test_d_cross_event_collision_refused           PASSED
tests/test_nhl_fanduel_identity.py::test_e_cross_player_collision_refused          PASSED
tests/test_nhl_fanduel_identity.py::test_f_same_identity_two_events_isolated       PASSED
tests/test_nhl_fanduel_identity.py::test_g_selected_quote_object_identity_verified PASSED
tests/test_nhl_fanduel_identity.py::test_h_harden_lineage_fail_closed[book]        PASSED
tests/test_nhl_fanduel_identity.py::test_h_harden_lineage_fail_closed[line]        PASSED
tests/test_nhl_fanduel_identity.py::test_h_harden_lineage_fail_closed[side]        PASSED
tests/test_nhl_fanduel_identity.py::test_i_non_nhl_mlb_unchanged_no_identity_strictness PASSED
============================== 11 passed in 0.05s ==============================
```

**Pre-existing lineage test suite (regression):**
- `test_observed_quote_lineage.py` — 11/11 PASS (no regression).
- `test_commit_b1_lineage.py` — 28/29 PASS. The single failure (`test_pick_build_audit_covers_every_active_call_site`) is **pre-existing on the baseline before this patch** — confirmed by `git stash` baseline rerun — and unrelated to this fix (concerns `_build_pick` call-site line numbers in `sports_engine.py`, not touched here).
- `test_nhl_refresh_sports_floor.py::test_source_contains_required_floor_tuple` — also pre-existing baseline failure (checks a hardcoded `_REQUIRED_FLOOR` string in `server.py`, not touched here).

**Backend health:** `/api/picks/today` returns 401 (auth-required, expected). Backend restarted cleanly.

---

## Guardrails Honored

- ✅ NHL only (MLB regression test proves it).
- ✅ No scoring/formula changes.
- ✅ 85 threshold unchanged.
- ✅ No frontend changes.
- ✅ No other-sport behavior changes.
- ✅ No fabrication — missing exact FanDuel wager → `no_real_book_line=True`, not a substitute.
- ✅ No deploy.
- ✅ No republish.
- ✅ Begin → validate → commit atomicity preserved (fix is upstream of commit).
- ✅ Immutable history untouched.
- ✅ No second board authority.
