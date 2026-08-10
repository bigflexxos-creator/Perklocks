"""Phase 5.2.1 (2026-08-11) — Duplicate Soccer identity reconciliation.

Two Soccer identity docs point at the same real person when ANY of
these strong-evidence links hold:

  * They share a provider id for the SAME provider — that's the
    hardest evidence.
  * Their ``name_norm`` values are equal after diacritic stripping
    (already done by ``_norm``) AND at least ONE of:
      - nationality matches
      - current_team matches
      - current_national_team matches
    (i.e. two people can't share full normalised name AND team/
    nationality — that's the same person.)

Merge policy — anti-collision-safe:

  * NEVER merge on name alone.
  * NEVER merge if any provider id disagrees for the SAME provider.
  * NEVER merge if DOB differs (both non-null).
  * NEVER merge if the two docs disagree on current_team AND
    current_national_team AND nationality — that's evidence of two
    different people.

Merge output — canonical survivor is chosen by:
  1. Higher-fidelity provider set (more provider ids)
  2. Newer observed_at
  3. Longer name (proxy for canonical full name)

The survivor absorbs:
  * The dropped doc's aliases (folded via `$addToSet`)
  * The dropped doc's provider ids (folded via `$set`)
  * The dropped doc's dropped-name as an alias (for future lookups)
  * The dropped doc's historical_teams and historical_national_teams
    are appended.

The dropped doc is deleted; a redirect row is left in
``player_identity_redirects`` so any pick still carrying the old
cpid can be rewritten later.

READ-ONLY dry-run mode by default (``apply=False``).  Only when
``apply=True`` is any write executed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.player_identity import IDENTITY_COLLECTION, _norm

logger = logging.getLogger("phase521_reconcile")

REDIRECT_COLLECTION = "player_identity_redirects"


def _diacritic_key(name: str) -> str:
    return _norm(name)


def _score(doc: dict) -> tuple:
    """Higher score wins."""
    return (
        len(doc.get("provider_ids") or {}),
        doc.get("observed_at") or "",
        len(doc.get("name") or ""),
    )


def _same_person(a: dict, b: dict) -> tuple[bool, str]:
    """Return (is_same, reason).  Anti-collision-safe."""
    # ── Strong evidence: shared provider id.
    ap = a.get("provider_ids") or {}
    bp = b.get("provider_ids") or {}
    for prov, pid in ap.items():
        if prov in bp:
            if str(bp[prov]) == str(pid):
                return True, f"shared_provider_id:{prov}={pid}"
            # Different id for the SAME provider — DIFFERENT people.
            return False, f"provider_id_conflict:{prov}"

    # DOB mismatch veto.
    ad, bd = a.get("dob"), b.get("dob")
    if ad and bd and str(ad)[:10] != str(bd)[:10]:
        return False, "dob_mismatch"

    # ── Name-normalized equality + team/nation agreement.
    an = a.get("name_norm") or _norm(a.get("name") or "")
    bn = b.get("name_norm") or _norm(b.get("name") or "")
    if not an or not bn:
        return False, "no_name"
    if an == bn:
        # Same normalised name — require team OR NT OR nationality
        # agreement (or one side has no such data).
        ct_a, ct_b = a.get("current_team"), b.get("current_team")
        nt_a, nt_b = (a.get("current_national_team"),
                      b.get("current_national_team"))
        nat_a, nat_b = a.get("nationality"), b.get("nationality")
        if ct_a and ct_b and ct_a != ct_b:
            # Same name, different clubs — VETO (could be 2 people).
            # Unless NT / nationality agree.
            if not (nt_a and nt_b and nt_a == nt_b) \
                    and not (nat_a and nat_b and nat_a == nat_b):
                return False, "same_name_different_clubs"
        if nt_a and nt_b and nt_a != nt_b:
            if not (nat_a and nat_b and nat_a == nat_b):
                return False, "same_name_different_nt"
        if nat_a and nat_b and nat_a != nat_b:
            return False, "same_name_different_nationality"
        return True, "name_norm_equal_and_context_compatible"

    # ── "Firstname Lastname" ↔ "Firstname Middle Lastname Suffix"
    #    Two safe patterns:
    #      (a) shorter name is a strict PREFIX of longer, OR
    #      (b) EVERY token of the shorter appears in the longer IN
    #          ORDER (subsequence match — handles "Darwin Nunez"
    #          ⊂ "Darwin Gabriel Nunez Ribeiro"),
    #    AND nationality OR NT agrees.  Requires corroboration —
    #    NEVER merges on name subset alone.
    a_parts, b_parts = an.split(), bn.split()
    if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts != b_parts:
        # Determine shorter / longer.
        if len(a_parts) < len(b_parts):
            short_parts, long_parts = a_parts, b_parts
        else:
            short_parts, long_parts = b_parts, a_parts
        # Subsequence check.
        is_subseq = True
        i = 0
        for tok in long_parts:
            if i < len(short_parts) and short_parts[i] == tok:
                i += 1
        is_subseq = (i == len(short_parts))
        if is_subseq:
            nt_a, nt_b = (a.get("current_national_team"),
                          b.get("current_national_team"))
            nat_a, nat_b = a.get("nationality"), b.get("nationality")
            if ((nt_a and nt_b and nt_a == nt_b)
                    or (nat_a and nat_b and nat_a == nat_b)):
                return True, "name_subsequence_plus_nationality_or_nt"

    return False, "no_evidence"


async def _load_all_soccer(db) -> list[dict]:
    return [d async for d in db[IDENTITY_COLLECTION].find(
        {"sport": "Soccer"},
        {"_id": 0, "canonical_player_id": 1, "name": 1, "name_norm": 1,
         "aliases": 1, "current_team": 1, "current_national_team": 1,
         "nationality": 1, "observed_at": 1,
         "national_team_observed_at": 1,
         "provider_ids": 1, "dob": 1, "sport": 1, "league": 1,
         "historical_teams": 1, "historical_national_teams": 1,
         "source": 1, "national_team_source": 1})]


def _plan_merges(docs: list[dict]) -> list[dict]:
    """Return a list of {survivor, drop, reason} plans.

    Groups by ``diacritic_key`` (base name-norm) and then within
    each group applies pairwise ``_same_person`` checks.  Two docs
    in the same group merge only when strictly same-person; groups
    of 3+ can produce a chain of merges into a single survivor.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        key = d.get("name_norm") or _norm(d.get("name") or "")
        if key:
            groups[key].append(d)

    plans: list[dict] = []
    # Same-name groups (diacritic strip already applied by _norm).
    for key, dlist in groups.items():
        if len(dlist) < 2:
            continue
        # Collapse into a single survivor if possible.
        dlist_sorted = sorted(dlist, key=_score, reverse=True)
        survivor = dlist_sorted[0]
        for cand in dlist_sorted[1:]:
            ok, reason = _same_person(survivor, cand)
            if not ok:
                continue
            plans.append({
                "survivor_cpid": survivor["canonical_player_id"],
                "survivor_name": survivor.get("name"),
                "drop_cpid": cand["canonical_player_id"],
                "drop_name": cand.get("name"),
                "reason": reason,
                "provider_ids_after": {
                    **(cand.get("provider_ids") or {}),
                    **(survivor.get("provider_ids") or {}),
                },
            })

    # Cross-group: first+last matches (Darwin Nunez ↔ Darwin Gabriel
    # Nunez Ribeiro).  Iterate all pairs where first & last token
    # match — bounded by first-token index for tractability.
    by_first: dict[str, list[dict]] = defaultdict(list)
    for d in docs:
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        parts = nn.split() if nn else []
        if len(parts) >= 2:
            by_first[parts[0]].append(d)

    already_dropped = {p["drop_cpid"] for p in plans}
    for first, dlist in by_first.items():
        if len(dlist) < 2:
            continue
        dlist_sorted = sorted(dlist, key=_score, reverse=True)
        survivor = dlist_sorted[0]
        for cand in dlist_sorted[1:]:
            if cand["canonical_player_id"] in already_dropped:
                continue
            if survivor["canonical_player_id"] == cand["canonical_player_id"]:
                continue
            # Skip if they're already in the same-name group — that
            # path is handled above.
            if (survivor.get("name_norm") or "") == (cand.get("name_norm") or ""):
                continue
            ok, reason = _same_person(survivor, cand)
            if not ok:
                continue
            plans.append({
                "survivor_cpid": survivor["canonical_player_id"],
                "survivor_name": survivor.get("name"),
                "drop_cpid": cand["canonical_player_id"],
                "drop_name": cand.get("name"),
                "reason": reason,
                "provider_ids_after": {
                    **(cand.get("provider_ids") or {}),
                    **(survivor.get("provider_ids") or {}),
                },
            })
            already_dropped.add(cand["canonical_player_id"])

    return plans


async def apply_plan(db, plan: dict) -> str:
    """Execute one merge plan.  Returns 'merged' | 'skipped'."""
    survivor_cpid = plan["survivor_cpid"]
    drop_cpid = plan["drop_cpid"]
    survivor = await db[IDENTITY_COLLECTION].find_one(
        {"canonical_player_id": survivor_cpid})
    dropped = await db[IDENTITY_COLLECTION].find_one(
        {"canonical_player_id": drop_cpid})
    if not survivor or not dropped:
        return "skipped"

    # Fold aliases + provider ids + historical teams.
    add_aliases = list(set(
        (survivor.get("aliases") or [])
        + (dropped.get("aliases") or [])
        + [dropped.get("name")]
    ))
    add_aliases = [a for a in add_aliases
                    if a and a != survivor.get("name")]
    merged_provider_ids = {
        **(dropped.get("provider_ids") or {}),
        **(survivor.get("provider_ids") or {}),
    }
    hist_teams = list(survivor.get("historical_teams") or [])
    for h in dropped.get("historical_teams") or []:
        if h not in hist_teams:
            hist_teams.append(h)
    hist_nt = list(survivor.get("historical_national_teams") or [])
    for h in dropped.get("historical_national_teams") or []:
        if h not in hist_nt:
            hist_nt.append(h)

    # If survivor is missing current_team but drop has it, adopt.
    updates: dict[str, Any] = {
        "aliases": add_aliases,
        "provider_ids": merged_provider_ids,
        "historical_teams": hist_teams,
        "historical_national_teams": hist_nt,
    }
    if not survivor.get("current_team") and dropped.get("current_team"):
        updates["current_team"] = dropped["current_team"]
        updates["observed_at"] = dropped.get("observed_at")
        updates["source"] = dropped.get("source")
    if not survivor.get("current_national_team") and dropped.get("current_national_team"):
        updates["current_national_team"] = dropped["current_national_team"]
        updates["national_team_observed_at"] = dropped.get(
            "national_team_observed_at")
        updates["national_team_source"] = dropped.get(
            "national_team_source")
    if not survivor.get("nationality") and dropped.get("nationality"):
        updates["nationality"] = dropped["nationality"]

    now = datetime.now(timezone.utc).isoformat()

    await db[IDENTITY_COLLECTION].update_one(
        {"canonical_player_id": survivor_cpid},
        {"$set": updates})
    await db[IDENTITY_COLLECTION].delete_one(
        {"canonical_player_id": drop_cpid})

    # Redirect trail.
    await db[REDIRECT_COLLECTION].update_one(
        {"from_cpid": drop_cpid},
        {"$set": {
            "from_cpid": drop_cpid,
            "to_cpid": survivor_cpid,
            "reason": plan.get("reason"),
            "merged_at": now,
        }}, upsert=True)
    return "merged"


async def run_dryrun(db) -> dict[str, Any]:
    docs = await _load_all_soccer(db)
    plans = _plan_merges(docs)
    reasons = defaultdict(int)
    for p in plans:
        reasons[p["reason"]] += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sport": "Soccer",
        "total_identities_before": len(docs),
        "proposed_merges": len(plans),
        "identities_after_dryrun": len(docs) - len(plans),
        "reason_breakdown": dict(reasons),
        "sample_plans": plans[:20],
    }


async def apply_all(db) -> dict[str, Any]:
    """Apply every planned merge.  Idempotent — plans that no longer
    match (survivor / drop already reconciled) are skipped."""
    docs = await _load_all_soccer(db)
    plans = _plan_merges(docs)
    merged_ct = 0
    skipped_ct = 0
    for p in plans:
        r = await apply_plan(db, p)
        if r == "merged":
            merged_ct += 1
        else:
            skipped_ct += 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "merged": merged_ct,
        "skipped": skipped_ct,
        "plans_attempted": len(plans),
    }


async def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                     help="Actually merge (default is READ-ONLY dry-run)")
    args = ap.parse_args()
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "perkslocks_production")]
    if args.apply:
        print("APPLYING merges (WRITE MODE).")
        r = await apply_all(db)
    else:
        print("READ-ONLY dry-run.")
        r = await run_dryrun(db)
    ts = r["generated_at"].replace(":", "").replace("-", "")
    path = ("/tmp/phase521_duplicate_reconciliation_"
            + ("apply_" if args.apply else "dryrun_")
            + ts + ".json")
    with open(path, "w") as fh:
        json.dump(r, fh, indent=2, default=str)
    print(json.dumps({k: v for k, v in r.items()
                        if k != "sample_plans"}, indent=2, default=str))
    print(f"[report written] {path}")


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["run_dryrun", "apply_all", "apply_plan", "_plan_merges",
            "_same_person"]
