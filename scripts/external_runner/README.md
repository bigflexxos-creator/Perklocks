# PerkLocks External-ML Runner

Portable Python runner that moves NBA / NHL / Soccer heavy ML off the Emergent
web server. Designed to be invoked from a GitHub Actions `workflow_dispatch`
or any CI runner. All it needs is:

- `PERKLOCKS_BASE_URL` — e.g. `https://preview.your-domain.emergent.host`
  (every path is prefixed with `/api`).
- `INGEST_TOKEN` — matches the server's `INGEST_TOKEN` env; passed as the
  `X-Ingest-Token` header on every ingest call.
- One of: `--sport NBA` / `--sport NHL` / `--sport Soccer`.

## Contract

```
EXPORT INPUTS  →  COMPUTE EXTERNALLY  →  STAGE CHUNKS  →  ATOMIC COMMIT
```

The runner NEVER calls `_refresh_picks_build`. It reuses the lower-level
scoring entrypoint `sports_engine.generate_all_picks(date, sport_filter=X)`
in-process (the heavy per-sport sim loop) but DOES NOT reproduce the in-app
publication orchestration (distributed lease, legacy mutators that lack
evidence provenance, generation state machine). The server owns publication:
the runner just stages healthy generations and asks the server to adopt one.

## FanDuel-only authority

Every staged pick MUST carry `bookmaker="fanduel"` and a real sportsbook line
(`book_odds` numeric, `no_real_book_line` falsy). Picks that cannot resolve to
an exact FanDuel wager are REJECTED by the server with
`FANDUEL_EXACT_WAGER_UNAVAILABLE` and are NOT staged.

## 85 threshold — unchanged

The runner never lowers the 85 Lock Score floor. Picks under 85 are logged
and dropped. The server independently re-enforces the floor at ingest.

## Last-good-board protection

A `FAILED` or `INCOMPLETE` generation NEVER touches the live board. The commit
step requires `status=COMPLETE`, every expected chunk received, and (if the
runner declared `pick_count>0`) at least one staged pick.

## Usage

```bash
python -m scripts.external_runner.run \
    --sport NBA --date 2026-06-21 \
    --base-url https://preview.your-domain.emergent.host
```

See `run.py` for the full CLI.
