"""Phase 5.1 (2026-08-11) — READ-ONLY NBA-44 root-cause diagnostic.

Answers the single question: WHY did 44 out of 47 NBA player picks
fail Universal Player Identity resolution during the Phase 5 dry run?

The script categorises every unresolved NBA pick into the buckets
listed in the Phase 5.1 spec — no writes, no mutations.

Usage:
    cd /app/backend && python -m scripts.phase51_nba_root_cause
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import player_identity as _pi
from services.universal_publication_barrier import (
    validate_universal, STATUS_UNRESOLVED,
    _extract_player_name_universal,
)
from services.player_team_fixture_validator import (
    _norm, _extract_fixture_teams,
)
from services.player_identity import IDENTITY_COLLECTION


BUCKETS = (
    "player_absent_from_source_roster",
    "roster_collection_empty_or_stale",
    "roster_data_exists_but_not_persisted",
    "universal_adapter_reads_wrong_collection",
    "provider_id_mismatch",
    "name_normalization_mismatch",
    "team_alias_mismatch",
    "suffix_mismatch",
    "punctuation_or_diacritic_mismatch",
    "trade_or_team_change_mismatch",
    "duplicate_name_ambiguity",
    "missing_team_or_context",
    "name_extraction_failure",
    "other",
)


async def _load_nba_identity_lookup(db):
    lookup: dict[str, str] = {}
    fresh: set[str] = set()
    id_docs: list[dict[str, Any]] = []
    cursor = db[IDENTITY_COLLECTION].find(
        {"sport": "NBA"},
        {"_id": 0, "name": 1, "name_norm": 1, "aliases": 1,
         "current_team": 1, "observed_at": 1, "provider_ids": 1,
         "canonical_player_id": 1})
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async for d in cursor:
        id_docs.append(d)
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        team = d.get("current_team")
        if nn and team:
            lookup[nn] = team
            if (d.get("observed_at") or "") >= cutoff:
                fresh.add(nn)
    return lookup, fresh, id_docs


def _has_diacritics(s: str) -> bool:
    import unicodedata
    return any(unicodedata.category(c) == "Mn"
                for c in unicodedata.normalize("NFKD", s))


_SUFFIXES = (" jr", " sr", " ii", " iii", " iv", " jr.", " sr.")


def _classify(pick: dict, verdict: dict, roster_lookup: dict[str, str],
               id_docs: list[dict]) -> tuple[str, dict]:
    """Return (bucket, diagnostic_row)."""
    market = pick.get("market") or ""
    player_raw = verdict.get("player") or _extract_player_name_universal(pick)
    fixture = verdict.get("fixture_teams") or _extract_fixture_teams(pick)
    reason = verdict.get("reason")

    diag = {
        "pick_id": pick.get("id"),
        "market": market,
        "event": pick.get("event"),
        "player_extracted": player_raw,
        "pick_team_hint": pick.get("player_team") or pick.get("home_team"),
        "fixture_teams": fixture,
        "available_provider_ids": {
            k: v for k, v in (pick.get("provider_ids") or {}).items()
        } if pick.get("provider_ids") else {},
        "roster_source": "espn_nba_athletes (per adapter)",
        "reason_from_validator": reason,
    }

    # ── Extraction failure
    if not player_raw:
        diag["why_resolve_failed"] = "player name could not be extracted"
        return "name_extraction_failure", diag

    if not fixture:
        diag["why_resolve_failed"] = "no home/away fixture teams parsed"
        return "missing_team_or_context", diag

    if len(id_docs) == 0:
        diag["why_resolve_failed"] = "player_identities.sport=NBA is empty"
        return "roster_collection_empty_or_stale", diag

    # Direct name-norm hit?
    pn = _norm(player_raw)
    hit = pn in roster_lookup

    if hit:
        # Name matched — team must have diverged.
        diag["expected_current_team"] = roster_lookup[pn]
        diag["why_resolve_failed"] = (
            f"identity resolved on name; roster team "
            f"'{roster_lookup[pn]}' did not match fixture "
            f"{fixture}"
        )
        return "trade_or_team_change_mismatch", diag

    # Check for suffix / punctuation / diacritic differences on a
    # relaxed roster scan.
    parts = pn.split()
    last = parts[-1] if parts else ""
    candidates_last = [k for k in roster_lookup if k.endswith(last)] if last else []

    # Suffix mismatch (Jr., III, ...): pick has suffix, registry does not
    for suf in _SUFFIXES:
        if pn.endswith(suf) and pn[: -len(suf)].strip() in roster_lookup:
            diag["why_resolve_failed"] = (
                f"pick includes suffix '{suf.strip()}'; "
                "registry key does not")
            diag["expected_current_team"] = (
                roster_lookup[pn[: -len(suf)].strip()])
            return "suffix_mismatch", diag

    # Duplicate name ambiguity: multiple registry candidates share
    # this last name.
    if len(candidates_last) > 1:
        diag["why_resolve_failed"] = (
            f"{len(candidates_last)} NBA identities share last name "
            f"'{last}' — cannot resolve without provider id / DOB")
        diag["candidates_last_name"] = candidates_last
        return "duplicate_name_ambiguity", diag

    # Diacritic differences (Jokić vs Jokic).
    if _has_diacritics(player_raw or ""):
        diag["why_resolve_failed"] = (
            "pick has diacritics; registry may be ASCII-only")
        return "punctuation_or_diacritic_mismatch", diag

    # If ANY identity doc exists but no match on normalized name at all,
    # the player is genuinely absent from the persisted identity source.
    diag["why_resolve_failed"] = (
        "player is not present in db.player_identities.sport=NBA")
    return "player_absent_from_source_roster", diag


async def run(db) -> dict[str, Any]:
    roster_lookup, fresh, id_docs = await _load_nba_identity_lookup(db)
    picks_cursor = db.picks.find(
        {"sport": "NBA",
         "$or": [{"status": {"$in": ["open", "pending",
                                       "OPEN", "PENDING"]}},
                 {"resolution": {"$in": [None, "", "pending"]}}]},
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "event": 1,
         "player": 1, "player_name": 1, "selection": 1,
         "home_team": 1, "away_team": 1, "league": 1,
         "lock_score": 1, "published_lock_score": 1,
         "player_team": 1, "provider_ids": 1})
    unresolved: list[dict] = []
    resolved_ct = 0
    scanned_ct = 0
    async for p in picks_cursor:
        scanned_ct += 1
        v = validate_universal(
            p, roster_lookup=roster_lookup, fresh_roster_names=fresh)
        if v.get("status") == "verified":
            resolved_ct += 1
            continue
        # Only classify true UNRESOLVED (not confirmed mismatch).
        if v.get("status") != STATUS_UNRESOLVED:
            continue
        bucket, diag = _classify(p, v, roster_lookup, id_docs)
        diag["bucket"] = bucket
        unresolved.append(diag)
    counts = Counter(u["bucket"] for u in unresolved)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "nba_identity_universe_size": len(id_docs),
        "nba_identity_lookup_keys": len(roster_lookup),
        "nba_fresh_identities": len(fresh),
        "picks_scanned": scanned_ct,
        "picks_resolved": resolved_ct,
        "picks_unresolved": len(unresolved),
        "bucket_counts": {b: counts.get(b, 0) for b in BUCKETS},
        "representative_examples": unresolved[:15],
    }


async def _main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]
    report = await run(db)
    path = "/tmp/phase51_nba_root_cause.json"
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    print("=" * 72)
    print("Phase 5.1 — NBA-44 Root-Cause (READ-ONLY)")
    print("=" * 72)
    for k in ("nba_identity_universe_size", "nba_identity_lookup_keys",
              "nba_fresh_identities", "picks_scanned",
              "picks_resolved", "picks_unresolved"):
        print(f"    {k}: {report[k]}")
    print("\nBUCKET COUNTS:")
    for b, c in report["bucket_counts"].items():
        if c:
            print(f"    {c:4d}  {b}")
    print(f"\n[report written] {path}")
    client.close()


if __name__ == "__main__":
    asyncio.run(_main())
