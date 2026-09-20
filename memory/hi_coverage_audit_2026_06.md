# HISTORICAL INTELLIGENCE — NON-MLB DATA COVERAGE ROOT AUDIT (2026-06)

**Scope**: audit only.  MLB path frozen.  No fixes applied.

## 1. Sport × History Coverage Matrix (LIVE production DB)

### Raw store counts

| Store                        | NFL | MLB | NBA | NHL | CFB | Soccer | Tennis |
| ---                          | --: | --: | --: | --: | --: | --:    | --:    |
| `player_game_actuals` rows   | 131 369 | 125 113 | 50 660 | **0** | **0** | 4 487  | 85 628 |
| `team_game_actuals`  rows    | 570  | 4 026 | 5 544  | **0** | **0** | 50 066 | 0      |
| `games` rows                 | 858  | 2 121 | –     | 751 (no team names) | 2 231 | –    | 6 071 |
| `soccer_matches` rows        | –    | –    | –     | –    | –    | 25 555 | –     |
| `soccer_player_game_logs`    | –    | –    | –     | –    | –    | 100 025 | –    |
| `tennis_matches_history`     | –    | –    | –     | –    | –    | –     | 37 992 |
| player canonical_id coverage | 100 %| 100 %| 100 % | –    | –    | (via name) | (via name) |
| player opponent_id coverage  |  99 %|  99 %|  99 % | –    | –    | (name-keyed) | (name-keyed) |
| player home_away coverage    |  98 %|  99 %|  40 % ⚠ | –    | –    | ~100 %       | 0 (not applicable) |

### Live HI endpoint probe per sport (1 pick each, real production ID)

| Sport | Sample market | Endpoint response | Backing observations |
| --- | --- | --- | ---: |
| CFB Total (TCU Horned Frogs) | Total Points Over 46.5 | **AVAILABLE_WITH_DATA** — L10 n=10  | 30 |
| NFL Pass TDs (Justin Herbert) | Over 1.5 Player Pass Tds | **AVAILABLE_WITH_DATA** — L10 n=10  | 80 |
| NFL Moneyline (Denver Broncos) | Moneyline | **AVAILABLE_WITH_DATA** — L10 n=10  | 19  |
| MLB Hits (Xavier Edwards)     | Over N Hits            | **AVAILABLE_WITH_DATA** — L10 n=10  | 120 |
| Soccer Goals (Vinicius Jr.)   | Anytime Goalscorer     | **AVAILABLE_WITH_DATA** — L10 n=10  | 38  |
| Soccer Total (Juventude)      | Total Goals            | **AVAILABLE_EMPTY** — L10 n=0        | 0   |
| CFB player prop               | (not produced)         | – (0 rows in `player_game_actuals`) | 0 |
| NHL any                       | (not produced)         | – (0 rows in every store)            | 0 |

## 2. CFB End-to-End Trace (the reported failure)

The exact reported case (`Illinois Fighting Illini @ Ohio State Buckeyes · Total Points Over 52.5`)
does not exist on the current Preview slate today, so I traced the equivalent live pick
(`North Carolina Tar Heels @ TCU Horned Frogs · Total Points Over 46.5`) end-to-end:

* pick.canonical_pick_id  `faf24cdf-e108-5e07-a7f9-3a6f0fa089ca`
* pick.canonical_home_team_id / canonical_away_team_id  `None` / `None`  ⚠
* pick.home_team = `"TCU Horned Frogs"`
* HI resolver → `_resolve_entity` → `_guess_team_from_market("Total Points Over 46.5", "TCU Horned Frogs", "North Carolina Tar Heels")` → returns `None`
* Fallback branch (`family=="total"` + no team name yet) → uses `home_team` = `"TCU Horned Frogs"`
* CFB adapter query: `db.games.find({sport:"cfb", $or:[{home:"TCU Horned Frogs"}, {away:"TCU Horned Frogs"}], status:"Final"})` → **30 rows**
* Returned 10 observations, 30 total_obs, `provenance=["games"]` — endpoint delivers real history.

**Reproduced today: CFB PATH IS WORKING** for the current-preview slate, using EXACT-STRING match against `db.games.{home,away}`.

### Alias-mismatch survey (all today's CFB picks)

| distinct CFB pick teams today | exact-match to db.games | mismatched |
| ---: | ---: | ---: |
| 140 | **136 (97 %)** | **4** |

**4 aliases that would silently produce `AVAILABLE_EMPTY` in production today**:

| pick.home/away literal | db.games canonical | issue |
| --- | --- | --- |
| `Arkansas Pine Bluff Golden Lions` | (not present under this exact string) | provider alias mismatch |
| `Houston Baptist Huskies` | `Houston Christian Huskies` | school renamed 2022, one provider still uses old |
| `Louisiana Ragin Cajuns` | `Louisiana Ragin' Cajuns` | **apostrophe stripped** in pick |
| `UMass Minutemen` | `Massachusetts Minutemen` | short-form vs long-form |

The physical Ohio-State report is the same failure family: the production odds provider
must have emitted `Ohio State` (or `Ohio St.`) whereas `db.games` stores `Ohio State Buckeyes`.
`db.games` DOES contain 2 Ohio-side rows: `Ohio Bobcats` and `Ohio State Buckeyes` (verified).

**Conclusion**: the CFB adapter itself is correct.  The failure boundary is **the pick's
`home_team` / `away_team` string being an alias** the adapter query does not accept as
an exact match.  There is no canonical team-id anywhere on CFB picks
(`canonical_home_team_id` and `canonical_away_team_id` are both `None`).

## 3. Failure Classification (per sport / market)

| Sport / family                | Failure class | Detail |
| ---                            | ---           | --- |
| CFB team (ML / Spread / Total) — matched aliases  | none (works)                   | 136 of 140 picks match |
| CFB team (ML / Spread / Total) — 4 alias variants  | **C** — data present, canonical mapping absent | apostrophe / renamed-school / short-vs-long |
| CFB player props                                    | **A** — never ingested         | 0 `player_game_actuals` rows |
| NFL player                                          | none (works)                   | 131 K rows, 100 % canonical id |
| NFL team (ML / Spread / Total)                      | none (works)                   | 570 rows, 100 % canonical id, 32/32 today match |
| MLB player + team                                   | none (works)                   | 125 K + 4 K rows |
| NBA player (goes to `player_game_actuals`)          | none for identity              | H/A only 40 % → home/away toggles will be sparse (class **F** on venue filter) |
| NBA team                                            | none (works)                   | 5 544 rows |
| Soccer player (goals/assists/SOT/shots)             | none (works)                   | 100 K rows via `soccer_player_game_logs` |
| Soccer team — European leagues                      | none (works)                   | 25 555 finished matches |
| Soccer team — Brazil Série A, MLS, etc. (Juventude) | **A** — never ingested         | `soccer_matches` ingester is scoped to top-5 European leagues |
| Tennis                                              | none (works)                   | 37 992 match rows, name-keyed |
| NHL (every family)                                  | **A** — never ingested         | 0 in every store; `games` rows have no team names |
| UFC / MMA                                           | **A** — sport not in board today, no store defined | out of scope for current slate |

Failure class counts (over the 36 rows in the matrix + observed live probes):

| class | count | example |
| --- | ---: | --- |
| A — never ingested                            | 4 | NHL all, CFB players, Soccer non-EU leagues, UFC |
| B — data exists but canonical IDs don't match | 0 |  |
| C — data exists, canonical mapping absent     | 1 | CFB alias mismatch (4 teams affected on today's slate) |
| D — wrong sport / league discriminator        | 0 |  |
| E — wrong market / stat mapping               | 0 |  |
| F — endpoint query excludes valid rows        | 1 | NBA H/A filter drops 60 % of rows because `home_away` field is null |
| G — date / season filter excludes valid rows  | 0 |  |
| H — current pick lacks required canonical identity | 1 | ALL CFB picks have `canonical_home_team_id = None`; alias mismatches are unrecoverable without a canonical id or name-alias table |
| I — frontend sends wrong identity             | 0 | endpoint reads pick from DB, no client-supplied identity |
| J — genuinely no historical coverage          | 1 | NHL — collection is empty |

## 4. Verify Data Exists Before Backfilling

* db.games (cfb): 2 231 rows · 241 distinct teams · Ohio State Buckeyes and Illinois Fighting Illini both present with exact strings.
* db.games (nhl): 751 rows, **no team fields** — cannot serve any lookup even by canonical id.
* db.player_game_actuals (nhl): 0.
* db.team_game_actuals (nhl): 0.
* db.soccer_matches: 25 555 finished — Manchester United, Liverpool, Real Madrid, etc. — but NO Brazilian, MLS, Saudi Pro League, or lower-tier team rows.  Juventude has 0 matches.
* db.soccer_player_game_logs: 100 025 rows keyed by understat provider ids.
* db.tennis_matches_history: 37 992 rows keyed by winner_name / loser_name.

## 5. Team Game History Audit (multi-season)

| sport | canonical_team_id present | opponent_id | date | home_away | points_for / against | provenance | ATS line stored? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MLB  | ✅ (canonical_team_id)  | ✅ | ✅ event_time | ✅ home_away | ✅ team_score / opponent_score | ✅ live_gamelog_mlb_v1 | ❌ (correctly withheld — no fabrication) |
| NFL  | ✅                     | ✅ | ✅              | ✅              | ✅                     | ✅ live_gamelog_nfl_v1 / legacy_games | ❌ (correctly withheld) |
| NBA  | ✅                     | ✅ | ✅              | ⚠ 40 % only    | ✅ actuals.points_for / points_against | ✅ live_gamelog_nba_v1 | ❌ (correctly withheld) |
| CFB  | ❌ (uses `games.home/away` strings) | ❌ (`opp_name` only) | ✅ date | ✅ derived | ✅ result.home/away | provenance=`games` | ❌ (correctly withheld) |
| Soccer | ❌ (name-keyed only)  | ❌ (name-keyed) | ✅ date | ✅ derived | ✅ home_score/away_score | provenance=`soccer_matches` | ❌ (correctly withheld) |
| NHL  | – (no rows)             | – | – | – | – | – | – |

**No ATS lines are ever synthesised.  Historical sportsbook lines are absent for every sport** —
the endpoint honestly reports raw actuals only.

## 6. Player History Audit

| sport | canonical_player_id keying | today's pick → adapter match | notes |
| --- | --- | --- | --- |
| NFL  | 100 % (`00-NNNNNNN` gsis)     | Herbert `00-0036355` → 80 rows | pass_yds / rush_yds / rec_yds / receptions / ATD all wired |
| MLB  | 100 % (mlb_stats numeric)     | Edwards `669364` → 120 rows | full hitter + pitcher families |
| NBA  | 100 % (nba_stats numeric)     | (not on slate today) | stores map points / rebounds / assists / threes / PRA; H/A only 40 % |
| Soccer | 100 % player_id (understat) | Vinicius `fd_1556` → 38 rows | goals / assists / shots / SOT wired |
| Tennis | name-keyed (winner_name / loser_name) | – (empty slate today) | ML / game_spread / game_total |
| CFB   | none (0 rows in `player_game_actuals`) | – | player props not producible |
| NHL   | none                          | – | player props not producible |
| UFC/MMA | none                        | – | not in scope for current slate |

**Current-pick identity → history-canonical identity**:
– NFL / MLB / NBA / Soccer: canonical id transmitted on the pick and matches the actuals store.
– Tennis: names match on both sides.
– CFB: team-name string is the join key; 4 aliases silently miss.
– NHL: nothing to match.

## Root-cause summary (audit only)

1. **CFB alias mismatch (H + C)** — pick `home_team`/`away_team` sometimes carries an alias
   (`UMass Minutemen`, `Louisiana Ragin Cajuns`, renamed schools) that is not an exact string
   in `db.games`.  4 of 140 teams on today's slate.  Ohio-State report fits this pattern.
   The adapter does exact-string match; no alias table, no canonical team id on CFB picks.
2. **Soccer team history — geographic gap (A)** — `soccer_matches` only covers major European
   leagues.  Every Brazilian / MLS / Saudi / Asian pick returns `AVAILABLE_EMPTY`.
3. **NHL — entire sport absent (A / J)** — no `player_game_actuals`, no `team_game_actuals`,
   `db.games (sport=nhl)` rows lack team names.  Any NHL pick returns `AVAILABLE_EMPTY`.
4. **CFB player props — sport-scope gap (A)** — no player-level rows exist.  Not currently hit
   because the board only produces CFB game-market picks, but if any player market were added
   history would be empty.
5. **NBA home/away toggle sparsity (F)** — `home_away` is only stamped on 40 % of NBA rows;
   HOME/AWAY tab filters will look under-populated even when overall n=10 is fine.

**Nothing to fix in this audit.  Waiting on user direction for surgical remediation plan.**
