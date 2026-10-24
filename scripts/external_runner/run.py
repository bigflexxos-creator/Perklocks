"""PerkLocks external-ML runner — portable NBA / NHL / Soccer runner.

Invocation (local Preview smoke test, inside the Emergent pod):

    cd /app && python -m scripts.external_runner.run \
        --sport NBA --date 2026-06-21 \
        --base-url http://localhost:8001 \
        --dry-run                       # score only, do NOT stage/commit

GitHub Actions / workflow_dispatch (Preview — no schedule yet):

    cd $GITHUB_WORKSPACE && python -m scripts.external_runner.run \
        --sport NBA \
        --base-url ${{ secrets.PERKLOCKS_BASE_URL }}
    # INGEST_TOKEN is read from env; GitHub provisions it as a secret.

Contract (per the one-pass migration spec):

    EXPORT INPUTS  →  COMPUTE EXTERNALLY  →  STAGE CHUNKS  →  ATOMIC COMMIT

Explicit NON-GOALS:

* Never call ``_refresh_picks_build`` — the whole point is to bypass
  that orchestrator (distributed lease, server-side state machine,
  publication side effects) and let the server own the final
  last-good-board swap.
* Never lower the 85 Lock Score floor.
* Never fabricate picks or history.
* Never bypass FanDuel-only authority (``prepublication_lineage_pass``
  pins the exact FanDuel observed quote; FanDuel-less picks are
  dropped locally and re-rejected server-side with
  ``FANDUEL_EXACT_WAGER_UNAVAILABLE``).

What the runner DOES do, step by step:

1. GET /api/export/inputs       (observability + verifies ingest token)
2. Side-channel acquisition triggers:
     * NHL: clear NHL feature-engine caches (fresh ingest between runs)
     * Soccer: run `services.soccer_prop_inject.run_once()` so AGS
       props are injected into the pipeline exactly as production does.
3. Core scoring:
     * `sports_engine.generate_all_picks(date_str, sport_filter=sport)`
       — the exact lower-level fetch+score dispatcher the orchestrator
       uses, called directly WITHOUT the orchestrator wrapper.
4. NHL-only context attach:
     * For every NHL pick, call
       `services.nhl_feature_engine.build_nhl_sim_context(db, pick)`
       and set `pick["nhl_sim_context"]` so the simulator in step 5
       has independent evidence to score with.
5. Simulator pass:
     * `brain.sim_runner.apply_simulations(picks)` — in-place lock_score
       anchoring across supported sports. Same math as production.
6. Legacy elite-boost parity:
     * `elite_players.apply_elite_boost(picks)` — replayed verbatim
       so Preview scoring semantics are identical to today's live
       board. (See /app/memory/LEGACY_ARTIFICIAL_SCORE_MUTATION.md —
       follow-up pass will decide whether to attach evidence
       provenance to this mutator or remove it.)
7. Soccer AGS merge:
     * `run_once()` writes AGS picks directly into `db.picks` with
       `pick_date=today`. We read them back for the current sport
       so they ride through the lineage pass and are included in
       the staged chunk. Future-dated AGS picks are left untouched.
8. Prepublication lineage pass:
     * `services.prepublication_lineage_pass.prepublication_lineage_pass(picks)`
       — this is THE call that upgrades `book_odds` / `bookmaker` to
       the exact observed FanDuel quote via the shared
       `quote_lineage_guard.harden_lineage` adapter.
9. FanDuel-only enforcement + ≥85 floor (local pre-check).
10. POST /api/ingest/board (idempotent chunk-by-chunk).
11. POST /api/ingest/commit (atomic last-good-board switch).

A retry re-sends the same `generation_id` + chunk indexes; the
server-side dedup is idempotent on `(generation_id, chunk_no)` and on
`(generation_id, canonical_pick_id)`.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Optional

try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore

logger = logging.getLogger("external_runner")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

SUPPORTED = ("NBA", "NHL", "Soccer")
CHUNK_SIZE = 100  # picks per POST /api/ingest/board request


# ═════════════════════════════════════════════════════════════════════
# Backend path bootstrap — the runner needs the backend's own scoring
# modules (sports_engine, brain.sim_runner, services.*). On the pod
# these live in /app/backend; from GitHub Actions the repo root
# contains /backend/. Add both candidates to sys.path so one import
# strategy covers both environments.
# ═════════════════════════════════════════════════════════════════════
def _bootstrap_backend_path() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("..", os.path.join("..", "..")):
        candidate_root = os.path.abspath(os.path.join(here, rel))
        if os.path.isdir(os.path.join(candidate_root, "backend")):
            backend_dir = os.path.join(candidate_root, "backend")
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)
            if candidate_root not in sys.path:
                sys.path.insert(0, candidate_root)
            return
    # Last-ditch — assume we're already in a Python path that resolves.
    logger.warning("backend path bootstrap could not locate /backend "
                   "— imports may fail if backend code is not on PYTHONPATH")


# ─────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────
def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


async def _heartbeat(client, base_url, token, *, job_id: str,
                     sport: Optional[str], stage: str,
                     generation_id: Optional[str] = None,
                     message: Optional[str] = None,
                     error: Optional[str] = None) -> None:
    """Best-effort heartbeat — never raises."""
    try:
        await client.post(
            f"{base_url}/api/ingest/heartbeat",
            headers={"X-Ingest-Token": token},
            json={
                "job_id":        job_id,
                "sport":         sport,
                "stage":         stage[:48],
                "generation_id": generation_id,
                "message":       (message or "")[:400] or None,
                "error":         (error or "")[:800] or None,
            },
            timeout=10.0,
        )
    except Exception as e:
        logger.warning("heartbeat %s failed: %s", stage, e)


async def _export_inputs(client, base_url, token, sport: str) -> dict:
    r = await client.get(
        f"{base_url}/api/export/inputs",
        headers={"X-Ingest-Token": token},
        params={"sport": sport, "limit": 500},
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


async def _stage_chunk(client, base_url, token, *, sport: str,
                       generation_id: str, chunk_no: int,
                       model_version: str, computed_at: str,
                       chunk_count: int, picks: list[dict],
                       job_id: str) -> dict:
    r = await client.post(
        f"{base_url}/api/ingest/board",
        headers={"X-Ingest-Token": token},
        json={
            "sport":          sport,
            "generation_id":  generation_id,
            "chunk_no":       chunk_no,
            "model_version":  model_version,
            "computed_at":    computed_at,
            "chunk_count":    chunk_count,
            "picks":          picks,
            "job_id":         job_id,
        },
        timeout=90.0,
    )
    if r.status_code >= 400:
        logger.error("stage_chunk failed status=%d body=%s",
                     r.status_code, r.text[:800])
    r.raise_for_status()
    return r.json()


async def _commit(client, base_url, token, *, sport: str,
                  generation_id: str, expected_chunks: int) -> dict:
    r = await client.post(
        f"{base_url}/api/ingest/commit",
        headers={"X-Ingest-Token": token},
        json={"sport": sport, "generation_id": generation_id,
              "expected_chunks": expected_chunks},
        timeout=120.0,
    )
    if r.status_code >= 400:
        logger.error("commit failed status=%d body=%s",
                     r.status_code, r.text[:800])
    r.raise_for_status()
    return r.json()


async def _mark_failed(client, base_url, token, *,
                       generation_id: str, reason: str) -> None:
    try:
        await client.post(
            f"{base_url}/api/ingest/fail",
            headers={"X-Ingest-Token": token},
            json={"generation_id": generation_id, "reason": reason[:400]},
            timeout=15.0,
        )
    except Exception as e:
        logger.warning("mark_failed failed: %s", e)


# ═════════════════════════════════════════════════════════════════════
# Per-sport orchestration — directly invokes LOWER-LEVEL services.
# ═════════════════════════════════════════════════════════════════════
async def _soccer_ags_trigger() -> dict:
    """Run `soccer_prop_inject.run_once()` so AGS picks land in db.picks.

    ``run_once`` writes directly into ``db.picks`` with its own
    canonical publication barrier applied inline — identical to the
    production path.  We later read those picks back for the current
    date and merge them into the runner's in-memory list so they
    traverse the same lineage pass as the main pipeline.
    """
    try:
        from services.soccer_prop_inject import run_once
    except Exception as e:
        logger.warning("soccer AGS trigger import failed: %s", e)
        return {"ok": False, "error": f"import_error:{e}"}
    try:
        return await run_once()
    except Exception as e:
        logger.warning("soccer AGS trigger run failed: %s", e)
        return {"ok": False, "error": str(e)}


async def _nhl_feature_attach(picks: list[dict]) -> int:
    """For every NHL pick, build and attach `nhl_sim_context`.

    This is the exact loop the orchestrator ran before NHL moved to
    external mode (`pick_refresh_orchestrator.py:1227-1246`).  Caches
    are cleared once so stats reflect freshly-ingested box scores.
    """
    try:
        from services.nhl_feature_engine import (
            build_nhl_sim_context, clear_caches,
        )
        from deps import db
    except Exception as e:
        logger.warning("NHL feature engine import failed: %s", e)
        return 0
    try:
        clear_caches()
    except Exception:
        pass
    attached = 0
    for p in picks:
        if not isinstance(p, dict):
            continue
        if str(p.get("sport") or "").upper() != "NHL":
            continue
        try:
            ctx = await build_nhl_sim_context(db, p)
            if ctx is not None:
                p["nhl_sim_context"] = ctx
                attached += 1
        except Exception as e:
            logger.debug("nhl feature attach failed for %s: %s",
                          p.get("id"), e)
    return attached


async def _merge_soccer_ags_picks(picks: list[dict], date_str: str) -> int:
    """Read back AGS picks that `run_once()` just wrote and merge them
    into the in-memory list for the current date so they ride through
    the lineage pass and get staged.

    Dedup by id — if a pick is already in the list, we skip it.
    Only merges picks with ``pick_date=date_str`` so we don't
    accidentally re-date future AGS picks to today when staging.
    """
    try:
        from deps import db
    except Exception as e:
        logger.warning("db import failed in AGS merge: %s", e)
        return 0
    seen_ids = {p.get("id") for p in picks if isinstance(p, dict) and p.get("id")}
    added = 0
    try:
        cur = db.picks.find(
            {
                "sport": "Soccer",
                "pick_date": date_str,
                "publication_route": "soccer_prop_canonical_inject",
                "status": {"$in": ["pending", "open", None]},
            },
            {"_id": 0},
        )
        async for row in cur:
            rid = row.get("id")
            if rid and rid in seen_ids:
                continue
            # Drop server-only fields that must not round-trip through
            # the ingest API (observed_quotes we KEEP so lineage can
            # pin FanDuel; everything else stays).
            picks.append(dict(row))
            if rid:
                seen_ids.add(rid)
            added += 1
    except Exception as e:
        logger.warning("AGS merge read failed: %s", e)
    return added


def _apply_simulations_safe(picks: list[dict]) -> dict:
    """Call `brain.sim_runner.apply_simulations` — mutates in place."""
    try:
        from brain.sim_runner import apply_simulations
    except Exception as e:
        logger.warning("sim_runner import failed: %s", e)
        return {"error": f"import_error:{e}"}
    try:
        return apply_simulations(picks) or {}
    except Exception as e:
        logger.warning("apply_simulations failed: %s", e)
        return {"error": str(e)}


def _apply_elite_boost_safe(picks: list[dict]) -> list[dict]:
    """Replay the legacy elite-boost mutator for scoring parity.

    Documented in /app/memory/LEGACY_ARTIFICIAL_SCORE_MUTATION.md —
    retained here ONLY to keep Preview semantics identical to the
    in-app pipeline for this migration pass.  Do not add new boost
    logic here.
    """
    try:
        from elite_players import apply_elite_boost
    except Exception as e:
        logger.warning("elite_boost import failed: %s", e)
        return picks
    try:
        return apply_elite_boost(picks) or picks
    except Exception as e:
        logger.warning("apply_elite_boost failed: %s", e)
        return picks


def _apply_lineage_pass_safe(picks: list[dict]) -> dict:
    """Pin each pick's `book_odds` / `bookmaker` to the exact observed
    FanDuel quote.  Picks without `observed_quotes` are left alone
    (they come from unmigrated producers that already set FanDuel
    directly, if at all)."""
    try:
        from services.prepublication_lineage_pass import prepublication_lineage_pass
    except Exception as e:
        logger.warning("lineage pass import failed: %s", e)
        return {"error": f"import_error:{e}"}
    try:
        return prepublication_lineage_pass(picks) or {}
    except Exception as e:
        logger.warning("prepublication_lineage_pass failed: %s", e)
        return {"error": str(e)}


async def _score_sport(sport: str, date_str: str) -> tuple[list[dict], dict]:
    """Run the full lower-level scoring pipeline for one sport.

    Returns (picks, stage_metrics).  Does NOT touch the live board.
    """
    stage_metrics: dict[str, Any] = {}

    # 1) Side-channel acquisition triggers.
    if sport == "Soccer":
        ags = await _soccer_ags_trigger()
        stage_metrics["soccer_ags"] = {
            "ok": ags.get("ok"),
            "picks_written": ags.get("picks_written"),
            "by_sport_key": ags.get("by_sport_key"),
            "error": ags.get("error"),
        }

    # 2) Lower-level scoring entrypoint — mirrors orchestrator :709.
    try:
        from sports_engine import generate_all_picks
    except Exception as e:
        raise RuntimeError(f"generate_all_picks import failed: {e}") from e
    picks = list(await generate_all_picks(
        date_str, sport_filter=sport) or [])
    stage_metrics["scored_raw"] = len(picks)

    # 3) Merge Soccer AGS picks that were written directly to db.picks.
    if sport == "Soccer":
        merged = await _merge_soccer_ags_picks(picks, date_str)
        stage_metrics["soccer_ags_merged"] = merged

    # 4) NHL feature-engine attach — orchestrator :1227-1246.
    if sport == "NHL":
        stage_metrics["nhl_feature_attached"] = (
            await _nhl_feature_attach(picks))

    # 5) Simulator pass — orchestrator :1249.
    stage_metrics["sim_counts"] = _apply_simulations_safe(picks)

    # 6) Legacy elite boost — orchestrator :1127-1129 (parity only).
    picks = _apply_elite_boost_safe(picks)

    # 7) Prepublication lineage pass — orchestrator :1906.  THIS is
    #    the call that pins book_odds to the exact observed FanDuel
    #    quote (via quote_lineage_guard.harden_lineage).
    stage_metrics["lineage"] = _apply_lineage_pass_safe(picks)

    return picks, stage_metrics


# ─────────────────────────────────────────────────────────────────────
# Normalisation + FanDuel-only enforcement (local pre-check)
# ─────────────────────────────────────────────────────────────────────
def _normalise_fanduel(picks: list[dict], sport: str) -> tuple[list[dict], dict]:
    """Keep ONLY picks that already carry an exact FanDuel wager and
    pass the ≥85 lock floor.  The server independently re-enforces this
    at ingest; this pre-check keeps us from spamming the ingest route
    with picks that will be rejected server-side.
    """
    kept: list[dict] = []
    stats = {"total": len(picks), "kept": 0,
             "rejected_wrong_sport": 0,
             "rejected_not_fanduel": 0,
             "rejected_below_85":   0,
             "rejected_no_book_line": 0,
             "rejected_other":      0}
    sport_lc = sport.lower()
    for p in picks:
        if not isinstance(p, dict):
            stats["rejected_other"] += 1
            continue
        sp = str(p.get("sport") or "").strip()
        if sp.lower() != sport_lc:
            stats["rejected_wrong_sport"] += 1
            continue
        book = str(p.get("bookmaker") or p.get("bookmaker_key") or "").lower()
        if book != "fanduel":
            stats["rejected_not_fanduel"] += 1
            continue
        if p.get("no_real_book_line") or p.get("book_odds") in (None, ""):
            stats["rejected_no_book_line"] += 1
            continue
        try:
            ls = float(p.get("published_lock_score")
                       or p.get("lock_score") or 0)
        except (TypeError, ValueError):
            ls = 0.0
        if ls < 85.0:
            stats["rejected_below_85"] += 1
            continue
        kept.append(p)
    stats["kept"] = len(kept)
    return kept, stats


def _chunk(picks: list[dict], size: int) -> list[list[dict]]:
    return [picks[i:i + size] for i in range(0, len(picks), size)] or [[]]


# ─────────────────────────────────────────────────────────────────────
# Pick sanitation — strip non-JSON-serialisable fields before HTTP
# ─────────────────────────────────────────────────────────────────────
_IGNORE_KEYS = {"_id"}


def _jsonable(v: Any) -> Any:
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=_dt.timezone.utc)
        return v.isoformat().replace("+00:00", "Z")
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items() if k not in _IGNORE_KEYS}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    try:
        import math
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    except Exception:
        pass
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _sanitise_picks(picks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in picks:
        if not isinstance(p, dict):
            continue
        d = {k: _jsonable(v) for k, v in p.items() if k not in _IGNORE_KEYS}
        out.append(d)
    return out


# ═════════════════════════════════════════════════════════════════════
# Main — orchestrates export → score → stage → commit
# ═════════════════════════════════════════════════════════════════════
async def run(sport: str, date_str: str, *, base_url: str, token: str,
              dry_run: bool = False) -> dict:
    if httpx is None:
        raise RuntimeError(
            "httpx is required: `pip install httpx` or add to runner deps")
    if sport not in SUPPORTED:
        raise ValueError(f"unsupported sport: {sport} (want {SUPPORTED})")

    _bootstrap_backend_path()

    job_id = f"ext-{sport.lower()}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    generation_id = f"gen-{sport.lower()}-{date_str}-{uuid.uuid4().hex}"
    computed_at = _utc_now_iso()
    model_version = f"external_v1_{sport.lower()}"
    t0 = time.monotonic()

    async with httpx.AsyncClient() as client:
        try:
            await _heartbeat(
                client, base_url, token, job_id=job_id, sport=sport,
                stage="START", generation_id=generation_id,
                message=f"date={date_str} dry_run={dry_run}",
            )

            # 1) Export inputs (observability + token verification).
            try:
                exp = await _export_inputs(client, base_url, token, sport)
                await _heartbeat(
                    client, base_url, token, job_id=job_id, sport=sport,
                    stage="INPUTS_FETCHED", generation_id=generation_id,
                    message=f"events={len(exp.get('events') or [])}",
                )
            except Exception as e:
                # A broken /api/export is a HARD stop — if the server
                # can't export, our stage won't match reality.
                await _mark_failed(client, base_url, token,
                                   generation_id=generation_id,
                                   reason=f"export_error: {e}")
                await _heartbeat(
                    client, base_url, token, job_id=job_id, sport=sport,
                    stage="INPUTS_FAILED", generation_id=generation_id,
                    error=str(e)[:400],
                )
                return {"ok": False, "stage": "EXPORT",
                        "generation_id": generation_id, "error": str(e)}

            # 2) Core scoring (lower-level, NOT _refresh_picks_build).
            await _heartbeat(
                client, base_url, token, job_id=job_id, sport=sport,
                stage="SCORING", generation_id=generation_id,
            )
            try:
                raw, stage_metrics = await _score_sport(sport, date_str)
            except Exception as e:
                await _mark_failed(client, base_url, token,
                                   generation_id=generation_id,
                                   reason=f"scoring_error: {e}")
                await _heartbeat(
                    client, base_url, token, job_id=job_id, sport=sport,
                    stage="SCORING_ERROR", generation_id=generation_id,
                    error=str(e)[:400],
                )
                return {"ok": False, "stage": "SCORING",
                        "generation_id": generation_id, "error": str(e)}

            logger.info("stage metrics for %s: %s", sport,
                        json.dumps(stage_metrics, default=str))

            # 3) FanDuel-only + ≥85 pre-check.
            kept, filt_stats = _normalise_fanduel(raw, sport)
            logger.info("Scored %d raw picks → kept %d (%s)",
                        filt_stats["total"], filt_stats["kept"],
                        json.dumps(filt_stats))
            kept = _sanitise_picks(kept)

            if dry_run:
                return {"ok": True, "dry_run": True,
                        "filter_stats": filt_stats,
                        "stage_metrics": stage_metrics,
                        "sample_pick": (kept[0] if kept else None),
                        "generation_id": generation_id,
                        "duration_sec": round(time.monotonic() - t0, 2)}

            # 4) Stage chunks.
            chunks = _chunk(kept, CHUNK_SIZE)
            chunk_count = len(chunks)
            await _heartbeat(
                client, base_url, token, job_id=job_id, sport=sport,
                stage="STAGING", generation_id=generation_id,
                message=f"{chunk_count} chunks x ≤{CHUNK_SIZE} picks",
            )
            for idx, c in enumerate(chunks):
                try:
                    await _stage_chunk(
                        client, base_url, token, sport=sport,
                        generation_id=generation_id, chunk_no=idx,
                        model_version=model_version, computed_at=computed_at,
                        chunk_count=chunk_count, picks=c, job_id=job_id,
                    )
                except Exception as e:
                    await _mark_failed(client, base_url, token,
                                       generation_id=generation_id,
                                       reason=f"stage_chunk_error chunk={idx}: {e}")
                    await _heartbeat(
                        client, base_url, token, job_id=job_id, sport=sport,
                        stage="STAGE_ERROR", generation_id=generation_id,
                        error=f"chunk={idx}: {e}"[:400],
                    )
                    return {"ok": False, "stage": "STAGE",
                            "generation_id": generation_id,
                            "chunk_failed": idx, "error": str(e)}

            # 5) Commit (atomic last-good-board switch).
            await _heartbeat(
                client, base_url, token, job_id=job_id, sport=sport,
                stage="COMMITTING", generation_id=generation_id,
            )
            try:
                commit_res = await _commit(
                    client, base_url, token, sport=sport,
                    generation_id=generation_id, expected_chunks=chunk_count)
            except Exception as e:
                await _mark_failed(client, base_url, token,
                                   generation_id=generation_id,
                                   reason=f"commit_error: {e}")
                await _heartbeat(
                    client, base_url, token, job_id=job_id, sport=sport,
                    stage="COMMIT_ERROR", generation_id=generation_id,
                    error=str(e)[:400],
                )
                return {"ok": False, "stage": "COMMIT",
                        "generation_id": generation_id, "error": str(e)}

            await _heartbeat(
                client, base_url, token, job_id=job_id, sport=sport,
                stage="DONE", generation_id=generation_id,
                message=f"committed pick_count={filt_stats['kept']}",
            )
            return {
                "ok":            True,
                "sport":         sport,
                "date":          date_str,
                "generation_id": generation_id,
                "filter_stats":  filt_stats,
                "stage_metrics": stage_metrics,
                "commit":        commit_res,
                "duration_sec":  round(time.monotonic() - t0, 2),
            }
        finally:
            # Final best-effort heartbeat with duration.
            try:
                await _heartbeat(
                    client, base_url, token, job_id=job_id, sport=sport,
                    stage="END", generation_id=generation_id,
                    message=f"total_sec={round(time.monotonic() - t0, 2)}",
                )
            except Exception:
                pass


def _default_date() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def main() -> int:
    p = argparse.ArgumentParser(description="PerkLocks External-ML Runner")
    p.add_argument("--sport", required=True, choices=SUPPORTED)
    p.add_argument("--date", default=_default_date(),
                   help="YYYY-MM-DD (UTC today by default)")
    p.add_argument("--base-url", default=os.environ.get("PERKLOCKS_BASE_URL",
                                                        "http://localhost:8001"))
    p.add_argument("--token", default=os.environ.get("INGEST_TOKEN", ""))
    p.add_argument("--dry-run", action="store_true",
                   help="Score only; do NOT stage/commit")
    args = p.parse_args()

    if not args.token:
        logger.error("INGEST_TOKEN missing — set env or pass --token")
        return 2

    result = asyncio.run(run(
        args.sport, args.date,
        base_url=args.base_url.rstrip("/"),
        token=args.token, dry_run=args.dry_run,
    ))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
