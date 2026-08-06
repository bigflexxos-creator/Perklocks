# Phase 3D — Alias Risk Report

**Date:** 2026-08-06
**Scope:** Enumerate the specific alias-resolution paths in the code
that could merge distinct entities if we transitioned to canonical
IDs today.  Migration is DEFERRED until every risk is resolved with a
manual alias entry or provider-ID enrichment.

## Team-alias risks
- `sport_adapters/*.py` sometimes maps abbreviations (e.g. `LAL` → Lakers). Abbreviation collision is possible across leagues (`LAL` = Lakers vs Los Angeles Lakers WNBA affiliate). ✅ Current code is sport-scoped so no live collision; Phase 3D contracts include `sport` field to enforce the guarantee going forward.
- `soccer` team names sometimes differ between providers (`Man United` vs `Manchester United` vs `Manchester Utd`). All resolve to fallback in the current dry-run scan. **Risk if we start merging**: two providers' rows would coalesce onto whichever fallback ID is generated first.

## Player-alias risks
- **HIGH-RISK CASE**: `Aaron Judge (NYY)` vs a future NAIA baseball player also named `Aaron Judge`. Current resolver requires `team_id` context for fallback, so this collision cannot happen — the sport+team+name key differs. ✅
- **MEDIUM-RISK CASE**: Two players with the same normalised name on the same team (e.g. father/son on the same college roster). No live example found in the current dry-run. Documented; would require manual alias.
- **LOW-RISK CASE**: `Mike Trout` vs `Michael Trout`. Different normalised names → different fallback IDs. ✅ Preserved.
- `elite_players.py` uses `first_name.lower()_last_name.lower()`. Two players with the exact same first + last (e.g. `Chris Sale` × N) → same key. Not observed in current elite roster, but documented.

## Event-alias risks
- `alt_lines_feed._make_market_id()` uses text event names, which are provider-dependent. Same real match under two provider names (e.g. `Arsenal vs Chelsea` vs `Arsenal FC vs Chelsea FC`) yields two market ids. This is CURRENTLY the desired behaviour (no false merges); merging would require a manual event alias.
- Tennis events use different spellings across providers (`Novak Djokovic vs Jannik Sinner` vs `Djokovic N vs Sinner J`). Same risk as soccer. Documented.

## Market-contract collisions in the dry-run
9 collision groups on picks (2–4 rows each). All 9 fell into the "fallback" quality bucket, meaning the resolver would not merge them automatically — the collisions are *reported*, not applied. Sampling of the collision groups shows they are typically:
- Same player, same market, different bookmakers being consolidated in `picks` for display purposes (intentional — pick is the recommendation; the sportsbook is a separate `pick_bookmakers` list on some schemas).
- Same player + market + line on the same day at different game times (rare — likely doubleheader).

## Rules encoded to prevent risky merges
1. Missing team_id on a player fallback → `identity_quality = "unresolved"`. Never mergeable.
2. Missing sport on a team fallback → `identity_quality = "unresolved"`.
3. Missing bookmaker or line on a market contract → `identity_quality = "fallback"`. Still uniquely identifies via the composite key (all fields included).
4. Provider ID always beats name matching (`test_provider_id_beats_alias_matching`).
5. First-token name matching is explicitly forbidden (`test_first_token_matching_is_not_used`).

## Not-yet-implemented alias registry
- Deferred to a future Phase 3D-2: a `provider_alias_registry` collection where operators can map `{provider, provider_id} → canonical_id` overrides. Not needed in Phase 3D because we did not touch live rows.
