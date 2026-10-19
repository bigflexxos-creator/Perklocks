# NFL Identity Crosswalk — Pre-Deployment Integrity Audit Report

**Date**: 2026-06-19
**Script**: `backend/scripts/audits/nfl_identity_crosswalk_integrity_audit.py`
**Mode**: READ-ONLY (no code or data changes made)
**Database**: `lockscore_db` / collection `nfl_identity_crosswalk`

---

## Headline Counters

| Metric | Count |
|---|---|
| `TOTAL_FULLY_PAIRED` | **2,752** |
| `AMBIGUOUS_GSIS` | 0 |
| `AMBIGUOUS_ESPN` | **7** |
| `NAME_COLLISIONS` | 13 |
| `SUFFIX_COLLISIONS` | 0 |
| `TEAM_CONFLICTS` | 874 |
| `TEAM_CONFLICTS_WITH_COLLISION_RISK` | 7 |
| `SAME_NAME_DIFFERENT_TEAMS` | 13 |
| `RETIRED_VS_ACTIVE` | 0 |
| `PROVENANCE_CONFLICTS` | 0 |
| `UNRESOLVED` (strict union of all flags) | **893** |
| `SAFE_UNIQUE_PAIRS` (strict) | **1,859** |
| `HARD_AMBIGUOUS_IDENTITIES` | **14** |
| `HARD_SAFE_UNIQUE_PAIRS` | **2,738** |

---

## Verdict: `AUDIT_FAIL`

7 ESPN IDs are each shared across two distinct GSIS IDs → **14 crosswalk
rows participate in a hard-ambiguous (one ESPN → two GSIS) mapping**.
These rows must be **excluded** from the authoritative crosswalk before
deployment, per the explicit fail-closed directive.

Hard-ambiguous identities (strongly recommended for exclusion):

| ESPN ID | Collides with GSIS pair | Likely cause |
|---|---|---|
| `2976316` | `00-0028908` ↔ `00-0032765` | "Michael Thomas" (HOU vs NO) |
| `3155647` | `00-0035681` ↔ `00-0039472` | "Mike Edwards" (BUF vs TB) |
| `3895831` | `00-0026982` ↔ `00-0034430` | "Andre Smith" (CIN vs TEN) |
| `3914630` | `00-0033903` ↔ `00-0036363` | "Josh Jones" (BAL vs SEA) |
| `4367699` | `00-0034980` ↔ `00-0039483` | "Isaiah Johnson" (LV vs MIA) |
| `4567123` | `00-0038978` ↔ `00-0039137` | "Byron Young" (LA vs PHI) |
| `4569371` | `00-0033161` ↔ `00-0039451` | "Isaiah Williams" (DET vs NYJ) |

**Root cause** — in `build_nfl_identity_crosswalk.py` Phase 2 the
nflverse `nfl_player_weekly` rows for the *second* same-name player
re-used the first player's `espn_player_id` picked out of the phase-1
`db.players` cache when the (name, team) match failed and the fallback
collapsed to a name-only hit.  Both GSIS rows then ended up pointing at
the same ESPN id.

---

## Soft-Signal Breakdown

### `NAME_COLLISIONS` (13)
Normalised names shared across >1 distinct paired identity.
7 of these are the hard-ambiguous ESPN overlaps above.
The remaining **6 are legitimate different players with distinct GSIS
AND distinct ESPN ids** — not corrupted mappings; the resolver's primary
path still works, and the name-based secondary path already fails
closed on multi-match.  Documented for operator visibility:

| Name | GSIS pair | ESPN pair | Teams |
|---|---|---|---|
| Aaron Brewer | 00-0028946 / 00-0036171 | 15241 / 4040537 | ARI / MIA |
| Jordan Phillips | 00-0031557 / 00-0040175 | 2577466 / 4837188 | BUF / MIA |
| Jonah Williams | 00-0035629 / 00-0035944 | 4040726 / 4032481 | CIN / DET |
| Brandon Johnson | 00-0037382 / 00-0040027 | 4035198 / 4594338 | PHI / PIT |
| Jaylon Jones | 00-0037106 / 00-0038407 | 4047655 / 4685145 | CHI / IND |
| Marcus Harris | 00-0039433 / 00-0040201 | 4573152 / 4570469 | KC / TEN |

### `TEAM_CONFLICTS` (874) vs `TEAM_CONFLICTS_WITH_COLLISION_RISK` (7)
874 identities carry a `team` in the crosswalk that differs from
`nfl_player_weekly`'s latest team — the vast majority are **benign
roster-lag** (free-agent moves, retirements, trades such as Von Miller
→ DAL, Kirk Cousins → LV, Calais Campbell → BAL).  The crosswalk team
is only used by the resolver's name-based secondary path when there
are multiple same-name candidates to disambiguate.

**Only the 7 overlapping with a name-collision could actually cause a
bad match**, and those 7 are already captured in the hard-ambiguous set
above — no new identities to exclude.

### `SUFFIX_COLLISIONS` (0)
No pair of distinct identities differ *only* by a Jr/Sr/II/III suffix
on the same base-and-team key.  Clean.

### `RETIRED_VS_ACTIVE` (0)
Among the 13 name-collision groups, no cross-era pair where one
identity has zero recent weekly rows while the other has recent
activity.  All colliders are from overlapping eras.  Clean.

### `PROVENANCE_CONFLICTS` (0)
Every crosswalk row sourced from BOTH `db.players` AND
`nfl_player_weekly` agrees on normalised name and (team-flexible)
team.  No source-dataset disagreement.  Clean.

---

## Canary Re-Verification

All 7 canaries resolve to **exactly one** paired identity and are
**not** in the ambiguous set:

| Canary | GSIS | ESPN | Team | Status |
|---|---|---|---|---|
| Brock Purdy | 00-0037834 | 4361741 | SF | ✅ OK |
| Christian McCaffrey | 00-0033280 | 3117251 | SF | ✅ OK |
| Patrick Mahomes | 00-0033873 | 3139477 | KC | ✅ OK |
| Josh Allen | 00-0034857 | 3918298 | BUF | ✅ OK |
| Jalen Hurts | 00-0036389 | 4040715 | PHI | ✅ OK |
| CeeDee Lamb | 00-0036358 | 4241389 | DAL | ✅ OK |
| Lamar Jackson | 00-0034796 | 3916387 | BAL | ✅ OK |

---

## Recommended Next Step

1. Apply a **fail-closed exclusion filter** to the 14 hard-ambiguous
   crosswalk rows (seven ESPN ids × two GSIS siblings each) before
   deploying.  This is a one-liner delete under `nfl_identity_crosswalk`
   (or a `blocklist` flag) — the resolver's current secondary name-path
   already handles the resulting missing-pair case by failing closed.
2. Re-run this audit; expected result on a clean set:
   - `AMBIGUOUS_ESPN` = 0
   - `HARD_AMBIGUOUS_IDENTITIES` = 0
   - Verdict `READY_TO_DEPLOY`.
3. No other changes required — history ingestion, scoring, models,
   thresholds, NHL, CFB, and board generation remain untouched.

Full machine-readable report:
`backend/scripts/audits/last_audit_report.json`
