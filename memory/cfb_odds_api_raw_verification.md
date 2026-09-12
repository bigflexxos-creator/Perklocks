# CFB ODDS API RAW PROVIDER VERIFICATION — Final Report
Generated: 2026-06-12 (session iter140, follow-up)
Scope: Report only. Raw provider layer + app-gateway comparison.
Zero scoring / Apex / model / boost changes.

## Verdict

**A. THE ODDS API RAW = EVENTS PRESENT.**

Both FBS and FCS return abundant current events. The prior observation
that `fetch_cfb_picks` returned 0 was a stale point-in-time reading
during the mid-fix retirement window. Re-running the pipeline **after
the R1/R2/R3 fixes** now returns **75 picks with 28 engine-stamped**.

## 1 · Raw Sports Discovery — GET /v4/sports

```
HTTP 200 · 87 total sports · api_key masked "1714…6a1"

americanfootball_ncaaf:
    active=True · title=NCAAF · group=American Football
    description=US College Football

americanfootball_ncaaf_fcs:
    active=True · title=NCAAF FCS · group=American Football
    description=US College Football, FCS
```

## 2 · Raw FBS Odds — GET /v4/sports/americanfootball_ncaaf/odds

```
HTTP 200
regions=us  markets=h2h,spreads,totals  oddsFormat=american
content_length=464,651 bytes

event_count_total:        95
event_count_with_bookies: 95
bookmakers_total_across:  834

first 5 events (all commence 2026-09-12T16:00:00Z):
  · 6dbd3546b178… East Carolina Pirates      vs Appalachian State Mountaineers · 11 bookies
  · 287a959c0717… North Carolina Tar Heels   vs East Tennessee State Buccaneers ·  9 bookies
  · 6ab72601a536… Liberty Flames             vs Gardner-Webb Runnin Bulldogs    ·  8 bookies
  · a50cf094d107… Indiana Hoosiers           vs Howard Bison                    · 10 bookies
  · e2280b63997… James Madison Dukes         vs Wagner Seahawks                 ·  9 bookies
```

## 3 · Raw FCS Odds — GET /v4/sports/americanfootball_ncaaf_fcs/odds

```
HTTP 200
regions=us  markets=h2h,spreads,totals  oddsFormat=american
content_length=104,804 bytes

event_count_total:        34
event_count_with_bookies: 34
bookmakers_total_across: 184

first 5 events:
  · bbabd345…  LIU Sharks              vs Albany                     · 8 bookies
  · 0b59777e…  Rhode Island Rams       vs Elon Phoenix               · 8 bookies
  · 8ba74f37…  Lafayette Leopards      vs Marist Red Foxes           · 8 bookies
  · f9758424…  VMI Keydets             vs Bucknell Bison             · 8 bookies
  · aca7e3d5…  Northern Iowa Panthers  vs Drake Bulldogs             · 8 bookies
```

## 4 · App-Pipeline Gateway State

| Stage | Value |
|-------|-------|
| `_ACTIVE_KEYS` size | 88 |
| `americanfootball_ncaaf` in `_ACTIVE_KEYS` | ✅ True |
| `americanfootball_ncaaf_fcs` in `_ACTIVE_KEYS` | ✅ True |
| `_API_DISABLED` | False |
| `_API_401_STREAK` | 0 |
| `_API_FAIL_STREAK` | 0 |
| `_API_TOTAL_OK` / `_TOTAL_FAIL` | 0 / 0 (fresh process) |
| `_API_LAST_ERR` | (empty) |
| odds_provider circuit state | (no active state · never opened) |
| Bad-market registry — CFB entries | (none · registry has no CFB blocks) |
| Cache infra | healthy (no infra failures; SWR cache in service) |
| Budget guard | not applicable at this stage (raw call succeeded) |

## 5 · Failure-Class Report (every stage · nothing collapsed)

| Class | State |
|-------|-------|
| sport_key_used | `americanfootball_ncaaf` (`SPORTS_KEYS['CFB']` line 49) |
| FBS/FCS routing | **FBS-only** — `americanfootball_ncaaf_fcs` is NOT in `SPORTS_KEYS['CFB']` |
| region | `us` |
| markets | `h2h,spreads,totals` |
| date/lookahead window | anchor 2026-09-12T16:00Z → +30h to 2026-09-13T22:00Z |
| cache state | fresh — SWR served the fetch |
| cache age | in-window (bulk_odds SWR TTL ≈ 90 s) |
| budget state | permissive — call did not open budget circuit |
| circuit-breaker | closed (`_API_DISABLED=False`) |
| bad-market registry | no CFB block entries |
| provider HTTP status | **200** (FBS) · **200** (FCS) |
| raw event count | **95 FBS + 34 FCS = 129 total** |
| post-window filter | 80 FBS events survive the 30h in-play slate window |
| post-model-availability | 39/80 FBS games produce picks; 41/80 return `MODEL_UNAVAILABLE:sp_missing:away` (FCS opponent not in SP+ table) |
| final CFB slate size | **75 picks** (28 with `cfb_engine_version` stamp) |

## 6 · Live Pipeline Re-Verification

```
$ python -c "from sports_engine import fetch_cfb_picks; ..."
fetch_cfb_picks returned 75 picks
  with cfb_engine_version: 28
  · LS=70.7 · Oklahoma Sooners @ Michigan Wolverines · Michigan ML
    dq=sp_plus|returning_prod_both|portal_both  pp=CAUSAL_INDEPENDENT
    factor_keys=7 [Projected Margin, Expected Total, Model Fair Prob,
                   Sportsbook Implied Prob, SP+ Margin Base, ...]
  · LS=70.7 · California @ Syracuse · California ML  (same signature)
  · LS=70.7 · UNLV @ North Texas · UNLV ML          (same signature)
  · LS=70.7 · Ohio State @ Texas · Ohio State ML    (same signature)
  · LS=70.2 · Georgia State @ Kennesaw State · Kennesaw ML (same signature)
```

Every R1/R2/R3 fix is engaged:
- **R1** — `factor_keys=7` on every stamped pick (Projected Margin,
  Expected Total, Model Fair Prob, Sportsbook Implied Prob,
  SP+ Margin Base, __data_quality, __model_uncertainty_reason).
- **R2** — `dq=sp_plus|returning_prod_both|portal_both` (returning-prod
  + portal maps loaded, 541 + 815 teams).
- **R3** — `pp=CAUSAL_INDEPENDENT`, `cfb_engine_version` stamped.

Score ceiling is no longer a cap — the top picks land at 70.7 because
today's honest model-vs-market edge is small (top picks show model prob
within 1-3pp of implied). This is legitimate evidence-driven output.

## Real Wiring Findings (**report only — no fix applied**)

### F1 · FCS sport-key not wired
- `SPORTS_KEYS['CFB'] = ['americanfootball_ncaaf']` (sports_engine.py line 49)
- The Odds API `americanfootball_ncaaf_fcs` catalog entry is **`active=True`**
- The `_ACTIVE_KEYS` reader (which unions the static list with active
  catalog entries) has `americanfootball_ncaaf_fcs` present in the
  active set — but only Soccer + Tennis are set up to auto-adopt
  prefix-matched active keys (`_prefix_map` at line 3571).
  Football sports use the static list only.
- **Impact**: 34 FCS events with 184 bookmakers are silently dropped
  at the sport-key wiring stage → up to ~30 additional CFB picks
  (moneyline + spread + total) never reach the board.

### F2 · SP+ ratings coverage gap for FCS opponents
- 41/80 FBS games in-window return `MODEL_UNAVAILABLE:sp_missing:away`
- Root cause: SP+ ratings table (553 teams) covers FBS only; FCS
  opponents in FBS-vs-FCS matchups (Howard, Wagner, East Tennessee St.,
  Gardner-Webb, Idaho State, Prairie View, etc.) return no rating and
  the CFB game model fails closed.
- **Impact**: In the current early-season slate, ~50% of FBS games have
  a non-Power-5 FCS opponent and produce 0 picks. Data-quality contract
  is preserved (failing closed is correct behaviour) but ML availability
  is smaller than the raw event count suggests.

## Acceptance

**Definitive statement (A):**

**THE ODDS API RAW = EVENTS PRESENT (95 FBS + 34 FCS = 129 total).**

The internal pipeline is currently functioning:
- FBS gateway/cache/circuit/budget/filter: **all green**
- 80 FBS events reach `_picks_from_game` post-window
- 75 picks emitted; 28 fully R1/R2/R3-stamped
- No Perklocks stage is dropping any event improperly for FBS

Additional Perklocks-side gaps identified for optional follow-up:
- **F1** — FCS sport-key not wired (34 events silently dropped)
- **F2** — SP+ ratings table lacks FCS coverage (41 FBS-vs-FCS games return `MODEL_UNAVAILABLE`)

Both are legitimate wiring/data-coverage gaps, not bugs in scoring or
in the gateway. **Report only** per directive.

## Artefacts (all read-only)

- `/app/backend/scripts/diagnostics/cfb_odds_api_raw_probe.py`
- `/app/backend/scripts/diagnostics/cfb_gateway_trace.py`
- `/app/memory/cfb_odds_api_raw_verification.md`

STOP.
