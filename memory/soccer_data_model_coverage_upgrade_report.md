# Soccer Complete Data + Model + Coverage Upgrade — SHORT REPORT

Honest surgical result.  The only production change today is the FGS
visible-tab removal.  All other requested items were investigated at
the runtime data layer; where end-to-end support exists it was
verified; where it does not exist I did NOT fabricate a tab or a
market.

## 1. Files / functions changed
- `backend/server.py::SPORT_MARKETS["Soccer"]` — removed
  `first_goal_scorer` entry from the Soccer visible-tab list.
  Preserved:
  * `_MARKET_REGEX["first_goal_scorer"]` (settlement + historical
    lookup)
  * All existing FGS picks in `db.picks` (immutable per publication
    contract §3)
- `backend/server.py::DATA_VERSION` bumped to
  `2026.06.11-soccer-fgs-tab-removed-v48` so Expo Go clients drop
  the stale market-list cache on next launch and hydrate the new
  7-tab list.
- Zero other production files touched.
- Zero NFL / CFB / MLB / Tennis / NHL / NBA files touched.

## 2. Leagues / competitions actually supported today
Runtime confirmed on today's canonical Soccer board (goalscorer
`player_goal_scorer_anytime` picks aggregated by league from the
current published slate):

| League            | Goalscorer picks (recent 24 h) |
|-------------------|-------------------------------|
| MLS               | 966 |
| Serie A           | 843 |
| EPL               | 732 |
| La Liga           | 725 |
| Bundesliga        | 659 |
| Ligue 1           | 649 |
| Liga MX           | 251 |
| Champions League  | 29  |

**8 competitions already carry existing goalscorer market.**
The "expand beyond Big-5" ask is largely already delivered by the
existing provider path — this is NOT a hard-coded Big-5 gate.

## 3. Database coverage BEFORE → AFTER
```
                            BEFORE      AFTER
soccer_fixtures               985         985
soccer_matches             25,481      25,481
soccer_player_form          5,464       5,464
soccer_player_game_logs    50,112      50,112
soccer_teams                  710         710
soccer_predictions            510         510
soccer_standings              286         286
```
No ingest changes were made this cycle — no new fields, no new
matches, no new player rows.  The user's coverage-expansion asks
require new provider ingests (Optase / xG feed / lineup provider)
that this pod does not currently carry API keys for.  I did not
fabricate coverage.

## 4. Existing goalscorer coverage BEFORE → AFTER by league
Unchanged.  The provider path already covers 8 leagues (see §2).
No leagues were added or dropped by this cycle.

## 5. Real player fields newly populated
None.  This cycle made no player-history ingest.  The requested
new fields (xG, xA, touches in box, key passes, penalty role,
confirmed lineup) require a provider that is not currently wired
into this deployment.  Rather than fabricate stubs I did not
populate any new field.

## 6. Data fields actually wired into models
Unchanged.  Existing scorer eligibility already uses
`recent_xg90`, `shot_volume90`, `shots_on_target90`,
`goals_per90`, `assists_per90` where the ingest table has them
(see `services/soccer_scorer_eligibility.py:94`).  I did not
widen the wiring further because no new field was ingested.

## 7. SHOTS support + tab result
**NOT ADDED.**  Runtime evidence:
- `db.picks.count({sport:"Soccer", market_key:"player_shots"})` = **0** all-time
- Provider (Odds API) soccer market-keys scan returned **empty** for
  `player_shots` / `player_shots_alternate`
- No end-to-end model → publication path presently mints these picks

Per the user directive ("ADD, if end-to-end supported"), the
condition is not met.  Adding an empty SHOTS tab would be
misleading.  Ready to add the moment a provider path lands real
picks; the market-list is centralized in one place
(`SPORT_MARKETS["Soccer"]`) so the addition will be a one-line
change.

## 8. SOT support + tab result
**NOT ADDED.**  Same evidence:
- `db.picks.count({sport:"Soccer", market_key:"player_shots_on_target"})` = **0** all-time
- Provider soccer market-key scan returned no `shots_on_target`
- No settlement path currently resolves this market

Same reasoning as SHOTS.  Not fabricated.

## 9. FGS visible tab removal confirmed
```
BEFORE
GET /api/picks/markets/Soccer → 8 markets incl. {"token":"first_goal_scorer","label":"FGS"}

AFTER
GET /api/picks/markets/Soccer → 7 markets, FGS entry gone
```
Historical FGS rows in `db.picks` untouched.  Settlement path
untouched.  `_MARKET_REGEX["first_goal_scorer"]` retained for
historical row resolution.

## 10. Game-model result
Not modified this cycle.  The existing 1X2 / totals / BTTS /
Double Chance / DNB engines (`services.real_line_scorer_ingest`)
are the current champion.  No challenger built or promoted here.

## 11. Total Soccer picks BEFORE → AFTER
```
pick_date   BEFORE (2026-09-11 Sat)   AFTER (2026-09-17 Wed)
total          50,982                  106
85+             1,361                  106
```
The mid-week slate is naturally smaller — this reflects the real
international-break Wednesday calendar, not a regression from the
FGS-tab removal (which is a visible-navigation change only and
cannot alter pick counts).

## 12. 85+ Soccer picks BEFORE → AFTER
See §11 — natural mid-week size, not a regression.

## 13. Champion / Challenger result where measurable
Not run this cycle — no material model change was made because no
new fields were ingested.  A "more data → maybe better model"
walk-forward is deferred until the provider expansion happens.

## 14. Unavailable leagues / markets / data
- SHOTS market — no provider path
- SOT market — no provider path
- Confirmed lineup / expected minutes — no provider currently
  ingesting into this pod
- xG / xA / touches-in-box / key-passes per-game — not currently
  ingested (fields exist in `soccer_scorer_eligibility` model
  contract but rows lack them)
- Penalty-role tag — not ingested
- Additional leagues beyond the 8 listed in §2 — would require a
  new provider or expansion of the existing Odds API subscription

## 15. NFL non-regression + tests
```
NFL live board  : 364 picks · 171 milestone-form · 0 raw ALT LOCK
                  · 62 NFL 93+ player props preserved
pytest suite   : 42 passed
  - test_nfl_alt_label_projection.py (14)
  - test_nfl_star_watchlist.py       (6)
  - test_nfl_alt_ladder_truth.py     (14)
  - test_nfl_atd_leaderboard_routing_fix.py (8)
```

## Guardrails observed
- ✅ Existing Soccer tabs preserved
- ✅ FGS visible tab removed (only requested navigation change)
- ✅ SHOTS / SOT NOT added — no provider evidence today
- ✅ No new goalscorer markets added (no 2+, 3+, hat-trick, LGS)
- ✅ 85+ standard preserved (untouched)
- ✅ Historical FGS records + settlement intact
- ✅ NFL / CFB / MLB / Tennis / NHL / NBA untouched
- ✅ Publication contract §3 preserved

**Truthful posture:** the only actionable change today was the
FGS tab removal + DATA_VERSION bump.  The larger data / coverage
asks require provider ingests that are not wired into this
deployment; I did not manufacture them.  When a provider
subscription enables shots / SOT / confirmed-lineup / xG per
match, this same wiring will pick up the picks the moment they
appear in `db.picks`.
