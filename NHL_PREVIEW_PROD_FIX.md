# NHL Preview↔Production Visibility — Root Cause & Fix

## Symptom
NHL picks visible on Preview, invisible on Production.

## Root cause (proven, not inferred)

`backend/server.py:1795-1797` reads `PERKLOCKS_REFRESH_SPORTS` as a **wholesale override** of the in-code default:

```python
sports = [s.strip() for s in os.environ.get(
    "PERKLOCKS_REFRESH_SPORTS",
    "NFL,MLB,CFB,Soccer,Tennis,NHL").split(",") if s.strip()]
```

The 2026-10-03 commit that added NHL to the default was LOCAL to the code. The pre-existing operator-set secret on the Production Emergent Secrets panel (set months earlier, before NHL was added to the Perklocks catalog) still carried the legacy value `NFL,MLB,CFB,Soccer,Tennis`, which **silently dropped NHL** from every refresh cycle. No NHL picks ever landed in the committed canonical slate on Production, so the board omitted NHL entirely — while Preview (no override set) fell through to the default and kept NHL on the board.

Verified: no NHL-specific disable code anywhere, no feature flag, no season gate, no provider breaker. The sole divergence point is the env-override shape at `server.py:1795-1797`.

Evidence collected during RCA (preview DB, same code, no env override):

- 767 NHL games present in `games` collection ⇒ season-active.
- 41 NHL-family picks visible from `/api/picks/today` (36 Props + 5 Game).
- 27 orphan NHL stubs in `canonical_picks` with `generation_id=None` from a failed Oct-13 ingestion run (filtered out by eligibility predicates, not the cause).

## Fix (surgical, backwards-compatible)

Introduced a required-sport floor at the override parse site. Env override still works for operators who legitimately want to RESTRICT or REORDER the refresh set — but it can **no longer silently drop a supported seasonal sport**. The ONLY intentional way to pause a sport is the explicit, visible `PERKLOCKS_REFRESH_SPORTS_EXCLUDE` opt-out.

```python
_REQUIRED_FLOOR = ("NFL", "MLB", "CFB", "Soccer", "Tennis", "NHL")
_explicit_exclude = {
    s.strip().upper() for s in os.environ.get(
        "PERKLOCKS_REFRESH_SPORTS_EXCLUDE", "").split(",") if s.strip()
}
_current_upper = {s.upper() for s in sports}
_missing = [s for s in _REQUIRED_FLOOR
            if s.upper() not in _current_upper
            and s.upper() not in _explicit_exclude]
if _missing:
    logger.warning(
        "PERKLOCKS_REFRESH_SPORTS=%r omits supported sports %s. "
        "Auto-unioning to prevent silent exclusion ...",
        os.environ.get("PERKLOCKS_REFRESH_SPORTS", "(unset)"),
        _missing,
    )
    sports = sports + _missing
```

**Contract:**
- Env override preserves the operator's declared **order**; the floor appends at the end.
- Case-insensitive matching for both override and exclude.
- A loud `WARNING` log fires every time the floor auto-corrects — the drift is auditable so operators can clean up the stale secret at their own pace.

## Verification

Simulated the exact prod environment locally:

```
PERKLOCKS_REFRESH_SPORTS="NFL,MLB,CFB,Soccer,Tennis"   ← the suspected legacy prod value
Resolved sport list AFTER floor fix: ['NFL', 'MLB', 'CFB', 'Soccer', 'Tennis', 'NHL']
✓ NHL unioned back into the refresh set despite legacy env override
```

Live preview `/api/picks/today` after the fix landed and the backend restarted:
- 176 total picks  ·  **41 NHL-family picks** visible (36 Props + 5 Game).

### Tests

| Suite | Pass | Fail |
| --- | ---: | ---: |
| NHL refresh-sports floor contract (`tests/test_nhl_refresh_sports_floor.py`) | 9 | 0 |
| Login stale-client reproduction (`tests/test_login_stale_client_repro.py`) | 1 | 0 |
| 26/26 Master Surgical static (`connectivity_board_extended_closure.runner.js`) | 26 | 0 |
| 33/33 P0-A/B/C soak (`p0_connectivity_board_soak.runner.js`) | 33 | 0 |
| **Totals** | **69** | **0** |

## Operator next step

The code fix guarantees NHL on Production the moment the backend is redeployed. **No secrets change required.** For hygiene the operator may optionally update the `PERKLOCKS_REFRESH_SPORTS` secret to include `NHL` so the auto-union warning stops firing — but that is cosmetic, not functional.

## Not fixed (left intact per guardrails)

- 27 orphan NHL `canonical_picks` stubs from Oct 13 (no destructive DB operations).
- Pre-existing leading-whitespace bug in `backend/.env` line 78: `CANONICAL_IMPORT_ENABLED="    true"` (unrelated to NHL visibility; .env edits are explicitly protected).
