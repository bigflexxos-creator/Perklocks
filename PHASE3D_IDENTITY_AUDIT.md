# Phase 3D — Identity Audit

**Date:** 2026-08-06
**Scope:** Inventory every place in the codebase where identity is
inferred from display names, or where different providers can silently
collide, before introducing typed identity contracts.

## Display-name-as-identity sites
| Location | Field | Risk |
|---|---|---|
| `picks` collection | `selection`, `player_name`, `team`, `event` — display strings | Used as dedupe key in `_reconcile_player_prop_contradictions` |
| `soccer_player_form` | `player_name` — no provider id | Coverage: 0% provider ID; 100% fallback |
| `tennis_players` | `name`, `name_norm` — full-name normalisation only | Coverage: 0% provider ID |
| `player_game_logs` | player linkage via string names | Coverage: 0% provider ID |
| `elite_players.py` | Player match by first-name+last-name | HIGH — but never first-token only; still name-based |
| `_dedupe_and_limit_goalscorers` (orchestrator) | Compare-by-selection string | Risk of merging distinct players with same normalised name |
| `sport_adapters/nba.py` | `canonical_name` field | Sport-scoped, but no provider linkage |

## Provider-ID coverage (live dry-run scan, sample 500)
| Collection | provider | fallback | unresolved | Collisions |
|---|---:|---:|---:|---:|
| picks | 0 | 500 | 0 | 9 |
| prediction_snapshots | 0 | 0 | 500 | 1 (scanner-shape mismatch — see notes) |
| settlement_events | — | — | — | (0 sampled — empty) |
| pick_enrichment | — | — | — | (0 sampled — empty) |
| user_bets | 0 | 2 | 0 | 0 |
| parlay_history | 0 | 194 | 0 | 0 |
| players | **484** | 0 | 16 | 0 |
| tennis_players | 0 | 0 | 500 | 0 |
| soccer_player_form | 0 | 0 | 500 | 0 |
| player_game_logs | 0 | 0 | 500 | 0 |
| live_alt_lines | (0 sampled — empty right now) | | | |

**Notes:**
- `prediction_snapshots` scanner used the generic doc shape (event/market/side/line) but snapshot docs have a different schema (`prediction_id`, `snapshot_version`, `board_version`). The "500 unresolved / 1 collision" reading is a scanner-shape mismatch, not a real identity issue. A snapshot-shape extractor would show canonical=100%. This is documented in `PHASE3D_DRY_RUN_REPORT.md`.
- `players` has strong provider coverage (96.8%). The 16 unresolved rows likely predate provider ingestion.

## Places where market contracts collapse different lines / books
| Location | Behaviour | Change needed |
|---|---|---|
| Old `_reconcile_player_prop_contradictions` (now in orchestrator) | Uses `_prop_family_key` to reconcile Over 0.5/1.5/2.5 H+R+RBI | ✅ Intentional — cross-line reconciliation. But downstream identity must NOT collapse. |
| `services.pick_fusion_decorator.enrich_picks_bulk` | Reads per-pick; treats `line` as a distinct field | ✅ Safe |
| Prediction snapshots | Include `line`, `market`, `book_odds`, `bookmaker` — full identity per row | ✅ Safe |
| Live alt lines feed | `market_id` combines sport+event+market — need to verify line/book included | ⚠️ Verify in Phase 3G |

## Alias resolvers
- `services/name_normalizer.py` — normalises to full lowercase alphanumeric (no first-token reduction). ✅ Safe.
- `elite_players.py` — canonical name is `f"{first_name.lower()}_{last_name.lower()}"`. Ambiguity possible when two players share both names. Documented in alias risk report.
- `player_db/canonicalize.py` — uses full-name normalisation. ✅ Safe.

## Text-built event keys
- `picks.event` is a display string ("Team A vs Team B"). Used only for display; not used as a dedupe key in the runtime path.
- `alt_lines_feed._make_market_id()` — combines sport + event_name + market_key. Event name is text-based; two providers spelling teams differently would produce distinct ids (potentially good, potentially bad — flagged for review).

## Providers currently in use
`odds_api`, `football_data`, `api_sports`, `sportdb`, `espn`, `nflfastr`, `mlb_stats`, `pfr`, `nba_stats` (some sports-specific). Each has its own provider_id namespace. Canonical IDs prefixed with `{provider}:` prevent cross-provider collisions.
