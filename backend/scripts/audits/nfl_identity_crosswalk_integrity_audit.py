"""
NFL Identity Crosswalk — Pre-Deployment Integrity Audit (READ-ONLY).
======================================================================

P0 pre-deployment check.  Confirms the generated GSIS ↔ ESPN crosswalk
did not create false mappings from the name/team bridge used by
``scripts/maintenance/build_nfl_identity_crosswalk.py``.

Scope — audit the FULLY-PAIRED crosswalk rows ONLY (``gsis_player_id``
AND ``espn_player_id`` both present).  Rows with a single identifier
cannot be a false ``cross-namespace`` mapping, so they are reported
separately but not treated as ambiguous.

Checks (read-only, fails CLOSED):

  1. AMBIGUOUS_GSIS         — one GSIS → multiple distinct ESPNs.
  2. AMBIGUOUS_ESPN         — one ESPN → multiple distinct GSISes.
  3. NAME_COLLISIONS        — identical normalised name mapped to
                              > 1 DISTINCT paired identity.
  4. SUFFIX_COLLISIONS      — two distinct identities whose names
                              differ ONLY by a suffix (Jr/Sr/II/III).
  5. TEAM_CONFLICTS         — crosswalk ``team`` disagrees with the
                              latest-week ``nfl_player_weekly`` team
                              (or ``db.players`` team) for the SAME
                              identity.  Flags trade/stale-team risk.
  6. SAME_NAME_DIFFERENT_TEAMS — identical normalised name on
                              distinct teams across paired rows.
  7. RETIRED_VS_ACTIVE      — identical normalised name where one
                              identity has recent weekly activity and
                              the other does not (active-vs-historical
                              collision).
  8. PROVENANCE_CONFLICTS   — the sources that contributed to one row
                              disagree on name or team.

Any crosswalk row that trips ANY of 1-8 is added to UNRESOLVED and
would be EXCLUDED from the authoritative crosswalk by the audit's
fail-closed policy (no code change made by this script).

Outputs:
    TOTAL_FULLY_PAIRED
    AMBIGUOUS_GSIS
    AMBIGUOUS_ESPN
    NAME_COLLISIONS
    SUFFIX_COLLISIONS
    TEAM_CONFLICTS
    SAME_NAME_DIFFERENT_TEAMS
    RETIRED_VS_ACTIVE
    PROVENANCE_CONFLICTS
    UNRESOLVED
    SAFE_UNIQUE_PAIRS

Then re-verifies the seven canonical canaries:
    Brock Purdy, Christian McCaffrey, Patrick Mahomes, Josh Allen,
    Jalen Hurts, CeeDee Lamb, Lamar Jackson.

If every counter above is zero AND every canary resolves to one
paired identity → prints ``READY_TO_DEPLOY``.

Usage:
    python backend/scripts/audits/nfl_identity_crosswalk_integrity_audit.py
    python backend/scripts/audits/nfl_identity_crosswalk_integrity_audit.py --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=str(ROOT / "backend" / ".env"))
load_dotenv(dotenv_path=str(ROOT / ".env"))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


SUFFIX_TOKENS = ("jr", "sr", "ii", "iii", "iv", "v")

CANARY_NAMES = [
    ("Brock Purdy", None),
    ("Christian McCaffrey", None),
    ("Patrick Mahomes", None),
    ("Josh Allen", None),
    ("Jalen Hurts", None),
    ("CeeDee Lamb", None),
    ("Lamar Jackson", None),
]


# ─────────────────────────────────────────────────────────────────────
# Normalisation helpers (duplicated from services.nfl_identity_crosswalk
# to keep this script independent of runtime caches).
# ─────────────────────────────────────────────────────────────────────

def _norm_name(s: Any) -> str:
    if not s:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(s).lower()).strip()


def _strip_suffix(name_norm: str) -> tuple[str, str | None]:
    """Return (base, suffix) for a normalised name.

    ``brockpurdyjr`` → (``brockpurdy``, ``jr``).  No suffix → (name, None).
    """
    for suf in SUFFIX_TOKENS:
        if name_norm.endswith(suf) and len(name_norm) > len(suf) + 2:
            return name_norm[: -len(suf)], suf
    return name_norm, None


def _team_match(a: str | None, b: str | None) -> bool:
    """Flexible team match — handles short-code vs full-name."""
    if not a or not b:
        # Missing team is treated as ``match`` so we don't falsely
        # flag identity rows that simply lack a team.
        return True
    an = a.lower().strip()
    bn = b.lower().strip()
    if an == bn:
        return True
    return (an in bn) or (bn in an)


# ─────────────────────────────────────────────────────────────────────
# Audit entry point.
# ─────────────────────────────────────────────────────────────────────

async def audit(db) -> dict:
    coll = db["nfl_identity_crosswalk"]

    # ── Load all fully-paired rows into memory (~2752 rows, trivial).
    paired: list[dict] = []
    async for r in coll.find(
        {
            "sport": "nfl",
            "gsis_player_id": {"$ne": None, "$exists": True, "$nin": [None, ""]},
            "espn_player_id": {"$ne": None, "$exists": True, "$nin": [None, ""]},
        }
    ):
        # Normalise identifier strings for comparison safety.
        r["gsis_player_id"] = str(r.get("gsis_player_id") or "")
        r["espn_player_id"] = str(r.get("espn_player_id") or "")
        r["name_norm"] = r.get("name_norm") or _norm_name(r.get("name"))
        paired.append(r)

    total_fully_paired = len(paired)

    # ──  1. AMBIGUOUS_GSIS  — one GSIS → multiple distinct ESPNs.
    gsis_to_espns: dict[str, set[str]] = defaultdict(set)
    for r in paired:
        gsis_to_espns[r["gsis_player_id"]].add(r["espn_player_id"])
    ambiguous_gsis_rows = [
        {"gsis": g, "espn_ids": sorted(e)}
        for g, e in gsis_to_espns.items() if len(e) > 1
    ]

    # ──  2. AMBIGUOUS_ESPN  — one ESPN → multiple distinct GSISes.
    espn_to_gsises: dict[str, set[str]] = defaultdict(set)
    for r in paired:
        espn_to_gsises[r["espn_player_id"]].add(r["gsis_player_id"])
    ambiguous_espn_rows = [
        {"espn": e, "gsis_ids": sorted(g)}
        for e, g in espn_to_gsises.items() if len(g) > 1
    ]

    # ──  3. NAME_COLLISIONS  — identical name_norm → >1 DISTINCT identity.
    name_to_identities: dict[str, set[tuple[str, str]]] = defaultdict(set)
    name_to_rows: dict[str, list[dict]] = defaultdict(list)
    for r in paired:
        if not r["name_norm"]:
            continue
        ident = (r["gsis_player_id"], r["espn_player_id"])
        name_to_identities[r["name_norm"]].add(ident)
        name_to_rows[r["name_norm"]].append(r)
    name_collision_rows = [
        {
            "name_norm": n,
            "name_samples": sorted({r.get("name", "") for r in name_to_rows[n]}),
            "identities": [{"gsis": g, "espn": e} for (g, e) in sorted(ids)],
            "teams": sorted({r.get("team") or "" for r in name_to_rows[n]}),
        }
        for n, ids in name_to_identities.items()
        if len(ids) > 1
    ]

    # ──  4. SUFFIX_COLLISIONS — distinct identities whose names differ
    # ONLY by a suffix (Jr/Sr/II/III).  We index identities by their
    # (base_name_without_suffix, team) and look for pairs where the
    # suffix differs but the base and team match.
    base_team_to_identities: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in paired:
        base, suf = _strip_suffix(r["name_norm"])
        if not base:
            continue
        key = (base, (r.get("team") or "").lower())
        base_team_to_identities[key].append({**r, "_suf": suf or ""})
    suffix_collision_rows: list[dict] = []
    for (base, team_key), rows in base_team_to_identities.items():
        if len(rows) < 2:
            continue
        suf_set = {rr["_suf"] for rr in rows}
        ident_set = {(rr["gsis_player_id"], rr["espn_player_id"]) for rr in rows}
        # Only flag when it's truly a suffix delta on DISTINCT identities.
        if len(ident_set) > 1 and len(suf_set) > 1:
            suffix_collision_rows.append({
                "base": base,
                "team": team_key,
                "suffixes": sorted(s for s in suf_set),
                "identities": [
                    {"gsis": g, "espn": e, "name": next(
                        (rr.get("name", "") for rr in rows
                         if rr["gsis_player_id"] == g and rr["espn_player_id"] == e), "")}
                    for (g, e) in sorted(ident_set)
                ],
            })

    # ── Build identity → latest-weekly team mapping for TEAM_CONFLICTS
    # and RETIRED_VS_ACTIVE.
    gsis_ids = {r["gsis_player_id"] for r in paired}
    weekly_latest: dict[str, dict] = {}
    weekly_recent_count: dict[str, int] = defaultdict(int)
    if "nfl_player_weekly" in await db.list_collection_names():
        # Aggregate most-recent team + recent sample count per GSIS.
        # Order is (season desc, week desc).
        pipeline = [
            {"$match": {"player_id": {"$in": sorted(gsis_ids)}}},
            {"$sort": {"season": -1, "week": -1}},
            {"$group": {
                "_id": "$player_id",
                "latest_team": {"$first": "$team"},
                "latest_name": {"$first": "$player_display_name"},
                "latest_season": {"$first": "$season"},
                "latest_week": {"$first": "$week"},
                "n": {"$sum": 1},
            }},
        ]
        async for g in db["nfl_player_weekly"].aggregate(pipeline, allowDiskUse=True):
            weekly_latest[str(g["_id"])] = g
            weekly_recent_count[str(g["_id"])] = int(g.get("n", 0))

    # ──  5. TEAM_CONFLICTS — crosswalk.team disagrees with weekly team.
    team_conflict_rows: list[dict] = []
    for r in paired:
        g = r["gsis_player_id"]
        wk = weekly_latest.get(g)
        if not wk:
            continue
        cw_team = r.get("team") or ""
        wk_team = wk.get("latest_team") or ""
        if not cw_team or not wk_team:
            continue
        # weekly team is a 3-letter code (``SF``), crosswalk may be
        # full-name (``San Francisco 49ers``) — the _team_match helper
        # handles substring either way.  BUT a 3-letter weekly code
        # against a *different* 3-letter crosswalk code (SF vs KC) is
        # a true conflict, so plain substring is enough.
        if not _team_match(cw_team, wk_team):
            team_conflict_rows.append({
                "gsis": g,
                "espn": r["espn_player_id"],
                "name": r.get("name"),
                "crosswalk_team": cw_team,
                "weekly_latest_team": wk_team,
                "latest_season": wk.get("latest_season"),
                "latest_week": wk.get("latest_week"),
            })

    # ──  6. SAME_NAME_DIFFERENT_TEAMS — identical normalised name on
    # distinct teams across paired identities (subset of
    # NAME_COLLISIONS, broken out for operator visibility).
    same_name_diff_team_rows: list[dict] = []
    for n, rows in name_to_rows.items():
        if len({(r.get("team") or "").lower() for r in rows if r.get("team")}) > 1:
            # Only when the identities are distinct — exclude the
            # single-identity ``row has two team-codes`` edge case.
            idents = {(r["gsis_player_id"], r["espn_player_id"]) for r in rows}
            if len(idents) > 1:
                same_name_diff_team_rows.append({
                    "name_norm": n,
                    "name_samples": sorted({r.get("name", "") for r in rows}),
                    "teams": sorted({r.get("team") or "" for r in rows}),
                    "identities": [{"gsis": g, "espn": e} for (g, e) in sorted(idents)],
                })

    # ──  7. RETIRED_VS_ACTIVE — identical normalised name where one
    # identity has recent weekly activity and the other has none.
    retired_vs_active_rows: list[dict] = []
    for n, rows in name_to_rows.items():
        idents = {(r["gsis_player_id"], r["espn_player_id"]) for r in rows}
        if len(idents) <= 1:
            continue
        active_count: list[tuple[tuple[str, str], int]] = []
        for (g, _e) in idents:
            active_count.append(((g, _e), weekly_recent_count.get(g, 0)))
        actives = [ac for ac in active_count if ac[1] > 0]
        inactives = [ac for ac in active_count if ac[1] == 0]
        if actives and inactives:
            retired_vs_active_rows.append({
                "name_norm": n,
                "name_samples": sorted({r.get("name", "") for r in rows}),
                "active": [{"gsis": g, "espn": e, "weekly_rows": c}
                           for ((g, e), c) in actives],
                "inactive": [{"gsis": g, "espn": e, "weekly_rows": c}
                             for ((g, e), c) in inactives],
            })

    # ──  8. PROVENANCE_CONFLICTS — the recorded ``provenance`` sources
    # disagree on name or team.
    # ``db.players`` is authoritative for team; ``nfl_player_weekly``
    # may lag (last-season team).  Flag only when the paired row
    # also diverges from the authoritative ``db.players`` on name_norm
    # or has conflicting team between the two sources beyond the
    # short-code / long-name boundary already handled above.
    db_players_name_team: dict[str, tuple[str | None, str | None]] = {}
    async for p in db["players"].find(
        {"sport": "nfl", "player_id": {"$in": sorted(gsis_ids)}},
        {"player_id": 1, "name": 1, "team": 1, "_id": 0},
    ):
        db_players_name_team[str(p.get("player_id"))] = (
            p.get("name"), p.get("team"),
        )
    provenance_conflict_rows: list[dict] = []
    for r in paired:
        g = r["gsis_player_id"]
        prov = set(r.get("provenance") or [])
        if "db.players" not in prov or "nfl_player_weekly" not in prov:
            continue
        dbp_name, dbp_team = db_players_name_team.get(g, (None, None))
        wk = weekly_latest.get(g)
        if not dbp_name or not wk:
            continue
        name_mismatch = (
            dbp_name and wk.get("latest_name")
            and _norm_name(dbp_name) != _norm_name(wk.get("latest_name"))
        )
        team_mismatch = (
            dbp_team and wk.get("latest_team")
            and not _team_match(dbp_team, wk.get("latest_team"))
        )
        if name_mismatch or team_mismatch:
            provenance_conflict_rows.append({
                "gsis": g,
                "espn": r["espn_player_id"],
                "crosswalk_name": r.get("name"),
                "db_players_name": dbp_name,
                "weekly_name": wk.get("latest_name"),
                "db_players_team": dbp_team,
                "weekly_team": wk.get("latest_team"),
                "name_mismatch": bool(name_mismatch),
                "team_mismatch": bool(team_mismatch),
            })

    # ── UNRESOLVED — union of every bad identity across all checks.
    bad_identities: set[tuple[str, str]] = set()

    def _add_from_pairs(key_pairs: list[dict], gkey: str, ekey: str) -> None:
        for row in key_pairs:
            gv = row.get(gkey)
            ev = row.get(ekey)
            if isinstance(gv, list) and isinstance(ev, list):
                for g in gv:
                    for e in ev:
                        bad_identities.add((g, e))
            elif isinstance(gv, str) and isinstance(ev, list):
                for e in ev:
                    bad_identities.add((gv, e))
            elif isinstance(gv, list) and isinstance(ev, str):
                for g in gv:
                    bad_identities.add((g, ev))

    for row in ambiguous_gsis_rows:
        for e in row["espn_ids"]:
            bad_identities.add((row["gsis"], e))
    for row in ambiguous_espn_rows:
        for g in row["gsis_ids"]:
            bad_identities.add((g, row["espn"]))
    for row in name_collision_rows:
        for ident in row["identities"]:
            bad_identities.add((ident["gsis"], ident["espn"]))
    for row in suffix_collision_rows:
        for ident in row["identities"]:
            bad_identities.add((ident["gsis"], ident["espn"]))
    for row in team_conflict_rows:
        bad_identities.add((row["gsis"], row["espn"]))
    for row in same_name_diff_team_rows:
        for ident in row["identities"]:
            bad_identities.add((ident["gsis"], ident["espn"]))
    for row in retired_vs_active_rows:
        for ident in row["active"] + row["inactive"]:
            bad_identities.add((ident["gsis"], ident["espn"]))
    for row in provenance_conflict_rows:
        bad_identities.add((row["gsis"], row["espn"]))

    unresolved = len(bad_identities)
    safe_unique_pairs = total_fully_paired - unresolved

    # Hard-ambiguity subset — rows where the PRIMARY resolver path
    # (direct GSIS/ESPN alias lookup) is non-deterministic because
    # one external id resolves to >1 crosswalk row.  These are the
    # *only* categories that can poison a pick-keyed historical
    # pull without triggering the name-fallback fail-closed branch.
    hard_bad: set[tuple[str, str]] = set()
    for row in ambiguous_gsis_rows:
        for e in row["espn_ids"]:
            hard_bad.add((row["gsis"], e))
    for row in ambiguous_espn_rows:
        for g in row["gsis_ids"]:
            hard_bad.add((g, row["espn"]))
    hard_ambiguous = len(hard_bad)
    hard_safe_unique_pairs = total_fully_paired - hard_ambiguous

    # ── Risk-weighted TEAM_CONFLICT subset: stale team ONLY matters
    # when the identity shares a normalised name with another paired
    # identity (otherwise the resolver never needs team to
    # disambiguate).  Report this subset so the operator can see
    # the "real" trade-risk count distinct from benign roster lag.
    multi_name = {n for n, ids in name_to_identities.items() if len(ids) > 1}
    team_conflicts_with_collision_risk = [
        r for r in team_conflict_rows
        if _norm_name(r.get("name")) in multi_name
    ]

    # ── Canary verification.
    canaries_out = []
    for name, _team in CANARY_NAMES:
        n = _norm_name(name)
        matches = [r for r in paired if r["name_norm"] == n]
        canaries_out.append({
            "canary": name,
            "name_norm": n,
            "match_count": len(matches),
            "identities": [
                {
                    "gsis": r["gsis_player_id"],
                    "espn": r["espn_player_id"],
                    "team": r.get("team"),
                    "canonical_player_id": r.get("canonical_player_id") or r.get("_id"),
                    "provenance": r.get("provenance"),
                }
                for r in matches
            ],
            "is_safe": (
                len(matches) == 1
                and (matches[0]["gsis_player_id"], matches[0]["espn_player_id"])
                not in bad_identities
            ) if matches else False,
        })

    return {
        "TOTAL_FULLY_PAIRED": total_fully_paired,
        "AMBIGUOUS_GSIS": len(ambiguous_gsis_rows),
        "AMBIGUOUS_ESPN": len(ambiguous_espn_rows),
        "NAME_COLLISIONS": len(name_collision_rows),
        "SUFFIX_COLLISIONS": len(suffix_collision_rows),
        "TEAM_CONFLICTS": len(team_conflict_rows),
        "TEAM_CONFLICTS_WITH_COLLISION_RISK": len(team_conflicts_with_collision_risk),
        "SAME_NAME_DIFFERENT_TEAMS": len(same_name_diff_team_rows),
        "RETIRED_VS_ACTIVE": len(retired_vs_active_rows),
        "PROVENANCE_CONFLICTS": len(provenance_conflict_rows),
        "UNRESOLVED": unresolved,
        "SAFE_UNIQUE_PAIRS": safe_unique_pairs,
        "HARD_AMBIGUOUS_IDENTITIES": hard_ambiguous,
        "HARD_SAFE_UNIQUE_PAIRS": hard_safe_unique_pairs,
        "canaries": canaries_out,
        "exclusions": {
            "hard_ambiguous_pairs": sorted(hard_bad),
            "all_flagged_pairs": sorted(bad_identities),
        },
        "details": {
            "ambiguous_gsis": ambiguous_gsis_rows[:20],
            "ambiguous_espn": ambiguous_espn_rows[:20],
            "name_collisions": name_collision_rows[:20],
            "suffix_collisions": suffix_collision_rows[:20],
            "team_conflicts": team_conflict_rows[:20],
            "team_conflicts_with_collision_risk": team_conflicts_with_collision_risk[:20],
            "same_name_different_teams": same_name_diff_team_rows[:20],
            "retired_vs_active": retired_vs_active_rows[:20],
            "provenance_conflicts": provenance_conflict_rows[:20],
        },
    }


async def main(args):
    uri = os.environ["MONGO_URL"]
    dbn = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(uri)
    db = client[dbn]

    report = await audit(db)

    # Print the headline counters.
    headline = [
        "TOTAL_FULLY_PAIRED",
        "AMBIGUOUS_GSIS",
        "AMBIGUOUS_ESPN",
        "NAME_COLLISIONS",
        "SUFFIX_COLLISIONS",
        "TEAM_CONFLICTS",
        "TEAM_CONFLICTS_WITH_COLLISION_RISK",
        "SAME_NAME_DIFFERENT_TEAMS",
        "RETIRED_VS_ACTIVE",
        "PROVENANCE_CONFLICTS",
        "UNRESOLVED",
        "SAFE_UNIQUE_PAIRS",
        "HARD_AMBIGUOUS_IDENTITIES",
        "HARD_SAFE_UNIQUE_PAIRS",
    ]
    print("\n── NFL Identity Crosswalk — Integrity Audit ──")
    for k in headline:
        print(f"  {k:36s} {report[k]}")

    print("\n── Canary verification ──")
    all_canaries_safe = True
    for c in report["canaries"]:
        safe = "OK " if c["is_safe"] else "FAIL"
        print(f"  [{safe}] {c['canary']:22s} matches={c['match_count']} "
              f"identities={c['identities']}")
        if not c["is_safe"]:
            all_canaries_safe = False

    audit_pass = (
        report["AMBIGUOUS_GSIS"] == 0
        and report["AMBIGUOUS_ESPN"] == 0
        and report["NAME_COLLISIONS"] == 0
        and report["SUFFIX_COLLISIONS"] == 0
        and report["TEAM_CONFLICTS"] == 0
        and report["SAME_NAME_DIFFERENT_TEAMS"] == 0
        and report["RETIRED_VS_ACTIVE"] == 0
        and report["PROVENANCE_CONFLICTS"] == 0
        and report["UNRESOLVED"] == 0
        and all_canaries_safe
    )
    print("\n" + ("READY_TO_DEPLOY" if audit_pass else "AUDIT_FAIL"))

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=str))
        print(f"\nFull report written to {args.json}")

    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--json", default=None, help="write full report to a JSON file")
    asyncio.run(main(p.parse_args()))
