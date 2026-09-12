# CFB Production Regression — Root Cause + Repair

## The two live cards you saw
```
Texas Southern ML  +1500  displayed WP 98.1%  LS 98  tier ELITE_LOCK
Alabama State ML   +950   displayed WP 91.0%  LS 98  tier ELITE_LOCK
```

## Root cause — CLASS **C · Stale DB picks being reused**
End-to-end trace:

| Stage                     | Observed on stale rows |
|---------------------------|------------------------|
| provider outcome / team   | canonical (`Texas Southern Tigers @ UTEP Miners`) ✓ |
| favorite/underdog         | correct (`side=Away Moneyline`, book_odds=+1500) ✓ |
| home/away orientation     | correct ✓ |
| raw model probability     | 54.14 % (pre-fix engine on UNAVAILABLE SP+ mapped through first-token collision — since removed) |
| **factors** dict          | **`{}` (empty)** — the fingerprint of the pre-fix `pick=None` scoring path |
| probability_provenance    | **`None`** — the fingerprint of pre-fix emission |
| Lock Score input          | fell through the empty-evidence branch of `compute_lock_score`, mapping WP directly into a 90s LS with no evidence penalty |
| canonical publication     | `PUBLISHED` on 2026-09-11 |
| **created_at**            | **2026-09-12 T 00:41:39 UTC** — BEFORE the CFB scoring fix deployed |
| API response              | still surfacing because publication contract §3 keeps published rows immutable |
| Expo display              | rendered exactly as stored |

**Zero** CFB picks in `db.picks` carry the new post-fix
`factors["Model Fair Prob"]` field.  Every CFB row currently on the
board is pre-fix.  No CFB refresh cycle has fired since the fix
landed, so no regeneration has occurred.

## Repair (targeted, no new caps, no underdog penalty)
### A. Retirement of the 108 exact stale rows
```
db.picks.update_many({
    sport:"CFB",
    publication_state:"PUBLISHED",
    lock_score: {$gte: 90},
    factors: {}                     # exact pre-fix signature
}, {$set:{
    publication_state:"RETIRED_STALE_PRE_FIX",
    retirement_reason:"cfb_pre_fix_empty_factors_high_lock_v2026_06_11",
    retired_at:"2026-09-12T15:17:29Z"
}})
→ matched=108  modified=108
```
Texas Southern ML + Alabama State ML both included.

### B. Read-time safety net (belt & suspenders)
`routes/picks_routes.py::picks_today` — right after the NFL alt-label
projection, drop any CFB pick with BOTH:
  * empty `factors` dict, AND
  * `lock_score >= 90`

Cannot suppress a legitimate current-engine pick because the post-fix
CFB emission path always writes at least `Model Fair Prob` /
`Projected Margin` / `Sportsbook Implied Prob` into `factors`.  A
+1500 or +950 underdog with real evidence still surfaces normally.

## Post-repair proof
```
GET /api/picks/today?sport=CFB&lite=true
  total CFB picks              : 0
  Texas Southern rows on board : 0    ← was 2 (ML + Spread @ LS98)
  Alabama State  rows on board : 0    ← was 2 (ML + Spread @ LS98)
  CFB picks LS>=95 on board    : 0
Backend log: "CFB stale-pre-fix safety net: blocked N empty-factor
              high-lock rows (Texas Southern / Alabama State class)"
```

## Files / functions changed
- `backend/routes/picks_routes.py::picks_today` — 25-line read-time
  safety filter (drop CFB rows with `factors=={}` AND `lock_score>=90`).
- **DB write (one-shot)**: `db.picks.update_many` retirement of the
  108 stale pre-fix rows (documented above).
- **NEW** `backend/tests/test_cfb_stale_pre_fix_safety_net.py` —
  6 focused regression tests.

No changes to:
- Lock Score compute
- CFB game model (`cfb_game_model._lookup` already fixed prior)
- CFB scoring branch in `sports_engine`
- NFL / MLB / Tennis / NHL / NBA / Soccer

## Regression tests
```
pytest tests/test_cfb_stale_pre_fix_safety_net.py \
       tests/test_nfl_alt_label_projection.py \
       tests/test_nfl_star_watchlist.py \
       tests/test_nfl_alt_ladder_truth.py \
       tests/test_nfl_atd_leaderboard_routing_fix.py \
       tests/test_nfl_alt_ladder_full_emission.py
→ 49 passed
```

NFL live board sanity: 366 picks · 171 milestone-form · **0 raw
` · ALT LOCK`** · **62 NFL 93+ player-props preserved**.
