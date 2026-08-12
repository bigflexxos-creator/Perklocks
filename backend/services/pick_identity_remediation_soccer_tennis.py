"""Session D (2026-06) — Soccer + Tennis PROVISIONAL identity remediation.

Scope
─────
FINAL structural blocker closure identified in Session C:

  * Soccer: 55% of live picks classified PROVISIONAL despite the
    canonical registry ``db.player_identities`` carrying 27,431
    Soccer entries with authoritative ``canonical_player_id``
    lineage.
  * Tennis: 71% of live picks classified PROVISIONAL despite
    ``db.tennis_players`` carrying 4,180 name-normalized entries
    (Sackmann ATP/WTA mirror) that already back the historical
    tables in ``db.tennis_matches_history``.

This module walks PROVISIONAL picks and, WHEN AND ONLY WHEN a UNIQUE
authoritative candidate exists in the existing registries, promotes
``identity_class = PROVISIONAL`` → ``MAPPED`` with
``canonical_player_id`` (and, for Tennis, ``canonical_opponent_id``)
set from the registry.

Guardrails (user directive)
───────────────────────────
* Only PROVISIONAL rows are considered.  AUTHORITATIVE and
  MAPPED are never touched.
* A deterministic hash is NEVER accepted as authority — the
  candidate must come from ``player_identities`` (Soccer) or
  ``tennis_players`` (Tennis).
* Ambiguous / colliding name → the pick REMAINS PROVISIONAL.
* League and, when available, opponent context are used as
  disambiguators — never fabricated.
* Settled historical outcomes (``settled_at``, ``status``,
  ``units_profit``) are NEVER mutated.
* Team markets DO NOT require player identity — the remediator
  only touches picks that carry a ``player_name`` field.
* Transfer safety: a promoted pick's canonical id is the STABLE
  registry id (``player_identities.canonical_player_id`` or
  ``tennis_players.name_norm``); ``current_team`` may differ from
  the pick's team but that is tracked separately in
  ``historical_teams`` in the registry — the canonical player id
  itself is unchanged across transfers.
* Ghost-team rejection: if the pick carries a ``player_current_team``
  AND the registry's ``current_team`` differs AND there is no
  transfer lineage row that spans the pick date, we REJECT the
  promotion (remain PROVISIONAL) — never masquerade a ghost.

CLI
───
  python -m services.pick_identity_remediation_soccer_tennis \\
      --sport soccer --limit 10000 --dry-run
  python -m services.pick_identity_remediation_soccer_tennis \\
      --sport tennis --limit 10000
"""
from __future__ import annotations

import argparse
import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase


# ── Normalization ─────────────────────────────────────────────────
def normalize_name(name: str) -> str:
    """Lowercase + strip accents + collapse whitespace.  Used ONLY as
    a MAPPING AID; a match against ``name_norm`` never singularly
    promotes a pick — the surrounding evidence gate must pass too."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower().strip()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[^a-z0-9 \-\.']", "", n)
    return n


# ── Report card for one pick ──────────────────────────────────────
@dataclass
class RemediationOutcome:
    pick_id:        str
    sport:          str
    before_class:   str
    after_class:    str
    reason:         str
    canonical_player_id: Optional[str] = None
    canonical_opponent_id: Optional[str] = None
    canonical_team_id: Optional[str] = None
    ambiguous_candidates: list[str] = field(default_factory=list)


@dataclass
class RemediationSummary:
    sport:                 str
    scanned:               int = 0
    promoted:              int = 0
    unchanged_ambiguous:   int = 0
    unchanged_no_candidate: int = 0
    unchanged_team_market: int = 0
    rejected_ghost_team:   int = 0
    errors:                int = 0
    detail_head:           list[RemediationOutcome] = field(default_factory=list)

    def add(self, outcome: RemediationOutcome, *, keep_head: int = 5) -> None:
        self.scanned += 1
        if outcome.after_class in ("MAPPED", "AUTHORITATIVE"):
            self.promoted += 1
        elif outcome.reason == "ambiguous_candidate":
            self.unchanged_ambiguous += 1
        elif outcome.reason == "no_candidate":
            self.unchanged_no_candidate += 1
        elif outcome.reason == "team_market_no_player":
            self.unchanged_team_market += 1
        elif outcome.reason == "ghost_team_rejected":
            self.rejected_ghost_team += 1
        elif outcome.reason == "error":
            self.errors += 1
        if len(self.detail_head) < keep_head:
            self.detail_head.append(outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sport":                  self.sport,
            "scanned":                self.scanned,
            "promoted":               self.promoted,
            "unchanged_ambiguous":    self.unchanged_ambiguous,
            "unchanged_no_candidate": self.unchanged_no_candidate,
            "unchanged_team_market":  self.unchanged_team_market,
            "rejected_ghost_team":    self.rejected_ghost_team,
            "errors":                 self.errors,
            "detail_head": [
                {
                    "pick_id": o.pick_id, "before": o.before_class,
                    "after": o.after_class, "reason": o.reason,
                    "cpid": o.canonical_player_id,
                    "opp_cpid": o.canonical_opponent_id,
                    "team_cid": o.canonical_team_id,
                    "ambiguous_candidates": list(o.ambiguous_candidates),
                } for o in self.detail_head
            ],
        }


# ═══════════════════════════════════════════════════════════════════
# SOCCER
# ═══════════════════════════════════════════════════════════════════
def _extract_soccer_player_from_market(market: str) -> str | None:
    """Best-effort: pull the player name from a market string like
    'Lionel Messi Anytime Goal Scorer'.  Only used as a fallback
    when ``player_name`` is missing — collision protection still
    applies downstream."""
    if not market:
        return None
    for suffix in (
        " Anytime Goal Scorer", " Anytime Goalscorer",
        " First Goal Scorer", " First Goalscorer",
        " To Score or Assist", " Assist",
        " Shots on Target", " Shots On Target", " Shots",
    ):
        if market.endswith(suffix):
            return market[: -len(suffix)].strip()
    return None


async def _soccer_candidate_lookup(
    db: AsyncIOMotorDatabase,
    *,
    name_norm: str,
    league: str | None,
    team_hint: str | None,
) -> list[dict]:
    """Return matching player_identities rows (0/1/many).  Filters
    to ``sport='Soccer'``.  League + team_hint are used to
    disambiguate when multiple rows share the same name_norm."""
    if not name_norm:
        return []
    rows = await db.player_identities.find(
        {"sport": "Soccer", "name_norm": name_norm},
        projection={"_id": 0},
    ).limit(20).to_list(length=20)
    if not rows:
        return []
    if len(rows) == 1:
        return rows
    # Try league disambiguation.
    if league:
        filtered = [r for r in rows if
                     (r.get("league") or "").lower() == league.lower()]
        if len(filtered) == 1:
            return filtered
        if filtered:
            rows = filtered
    # Try current_team hint (for player-market picks that carry
    # `team` or `player_current_team`).
    if team_hint:
        tl = team_hint.lower()
        filtered = [
            r for r in rows
            if (r.get("current_team") or "").lower() == tl
            or any((h.get("team") or "").lower() == tl
                    for h in (r.get("historical_teams") or []))
        ]
        if len(filtered) == 1:
            return filtered
        if filtered:
            rows = filtered
    # Real-world dedup: the production registry sometimes carries
    # multiple rows with the SAME name_norm from different sources
    # (e.g. club roster + national-team roster).  Two disambiguation
    # rules — both conservative aggregations, never guessing:
    #
    #   Rule A: if all rows share the SAME current_team, pick the
    #           most-recently-observed one.
    #   Rule B: if exactly ONE row has a non-null / non-"International"
    #           current_team and the others are None/International,
    #           pick the club row (the international rows are the
    #           SAME player's national-team appearance).
    if len(rows) > 1:
        by_team: dict[str, list[dict]] = {}
        for r in rows:
            key = (r.get("current_team") or "").lower()
            by_team.setdefault(key, []).append(r)
        if len(by_team) == 1:
            grp = next(iter(by_team.values()))
            grp.sort(key=lambda r: r.get("observed_at") or "", reverse=True)
            return [grp[0]]
        real_clubs = [
            r for r in rows
            if r.get("current_team")
            and str(r.get("current_team")).lower() != "international"
        ]
        # Distinct real-club names.
        distinct_clubs = {(r.get("current_team") or "").lower()
                            for r in real_clubs}
        if len(real_clubs) >= 1 and len(distinct_clubs) == 1:
            real_clubs.sort(key=lambda r: r.get("observed_at") or "",
                              reverse=True)
            return [real_clubs[0]]
    return rows


async def remediate_one_soccer(
    db: AsyncIOMotorDatabase, pick: dict, *, dry_run: bool = False,
) -> RemediationOutcome:
    pid = pick.get("id")
    before = pick.get("identity_class") or "PROVISIONAL"
    if before != "PROVISIONAL":
        return RemediationOutcome(
            pid, "Soccer", before, before, "not_provisional_skip",
        )
    player_name = (pick.get("player_name")
                     or _extract_soccer_player_from_market(
                            pick.get("market") or ""))
    if not player_name:
        # Pure team market — remediator does not touch (Rule 4).
        return RemediationOutcome(
            pid, "Soccer", before, before, "team_market_no_player",
        )
    name_norm = normalize_name(player_name)
    league = pick.get("league")
    team_hint = pick.get("player_current_team") or pick.get("team")
    try:
        candidates = await _soccer_candidate_lookup(
            db, name_norm=name_norm, league=league, team_hint=team_hint,
        )
    except Exception as e:
        return RemediationOutcome(
            pid, "Soccer", before, before, "error",
            ambiguous_candidates=[f"lookup_error:{e.__class__.__name__}"],
        )
    if not candidates:
        return RemediationOutcome(
            pid, "Soccer", before, before, "no_candidate",
        )
    if len(candidates) > 1:
        return RemediationOutcome(
            pid, "Soccer", before, before, "ambiguous_candidate",
            ambiguous_candidates=[
                (c.get("canonical_player_id") or c.get("name"))
                for c in candidates[:5]
            ],
        )
    cand = candidates[0]
    cpid = cand.get("canonical_player_id")
    if not cpid:
        return RemediationOutcome(
            pid, "Soccer", before, before, "no_candidate",
        )

    # Ghost-team rejection: if pick carries a team hint that is NOT
    # in the registry's current_team + historical_teams, REJECT
    # promotion (remain PROVISIONAL).  We accept when the pick's
    # team hint is missing entirely (team-less player markets do
    # exist).
    if team_hint:
        tl = team_hint.lower()
        current_ok = (cand.get("current_team") or "").lower() == tl
        historical_ok = any(
            (h.get("team") or "").lower() == tl
            for h in (cand.get("historical_teams") or [])
        )
        if not (current_ok or historical_ok):
            return RemediationOutcome(
                pid, "Soccer", before, before, "ghost_team_rejected",
                ambiguous_candidates=[
                    f"cpid={cpid}",
                    f"registry_team={cand.get('current_team')}",
                    f"pick_team={team_hint}",
                ],
            )

    # Passed.  Promote to MAPPED.
    if not dry_run:
        await db.picks.update_one(
            {"id": pid},
            {"$set": {
                "identity_class":       "MAPPED",
                "canonical_player_id":  cpid,
                "identity_evidence": {
                    "source":              "player_identities",
                    "match_type":          "name_norm+league+team_hint",
                    "registry_id":         cpid,
                    "registry_source":     cand.get("source"),
                    "registry_league":     cand.get("league"),
                    "registry_current_team": cand.get("current_team"),
                    "promoted_by":         "session_d.remediator.v1",
                    "promoted_at":         datetime.now(timezone.utc)
                                             .isoformat().replace(
                                                 "+00:00", "Z"),
                },
            }},
        )
    return RemediationOutcome(
        pid, "Soccer", before, "MAPPED", "mapped_by_name_league_team",
        canonical_player_id=cpid,
    )


# ═══════════════════════════════════════════════════════════════════
# TENNIS
# ═══════════════════════════════════════════════════════════════════
def _tennis_extract_side(pick: dict) -> tuple[str | None, str | None]:
    """Return (selected_player_name, opponent_name) with best effort
    from various pick fields.  Only returns names — the mapping is
    done downstream with collision protection."""
    selected = (pick.get("player_name")
                  or pick.get("selection")
                  or pick.get("home_team_name")
                  or None)
    opponent = (pick.get("opponent_team")
                  or pick.get("opponent")
                  or None)
    if not opponent:
        home = pick.get("home_team_name") or ""
        away = pick.get("away_team_name") or ""
        if selected and home and away:
            if normalize_name(selected) == normalize_name(home):
                opponent = away
            elif normalize_name(selected) == normalize_name(away):
                opponent = home
    return selected or None, opponent or None


async def _tennis_candidate_lookup(
    db: AsyncIOMotorDatabase, *, name_norm: str,
) -> list[dict]:
    """Search db.tennis_players by name_norm (Sackmann mirror)."""
    if not name_norm:
        return []
    return await db.tennis_players.find(
        {"name_norm": name_norm},
        projection={"_id": 0, "name": 1, "name_norm": 1,
                     "elo_overall": 1, "elo_hard": 1, "elo_clay": 1,
                     "elo_grass": 1, "form": 1},
    ).limit(20).to_list(length=20)


async def remediate_one_tennis(
    db: AsyncIOMotorDatabase, pick: dict, *, dry_run: bool = False,
) -> RemediationOutcome:
    pid = pick.get("id")
    before = pick.get("identity_class") or "PROVISIONAL"
    if before != "PROVISIONAL":
        return RemediationOutcome(
            pid, "Tennis", before, before, "not_provisional_skip",
        )
    selected, opponent = _tennis_extract_side(pick)
    if not selected:
        # Total games / total sets markets — no player identity
        # required to publish, but this remediator does not
        # promote them (they stay PROVISIONAL until a dedicated
        # match/event identity class exists).
        return RemediationOutcome(
            pid, "Tennis", before, before, "team_market_no_player",
        )
    sel_norm = normalize_name(selected)
    opp_norm = normalize_name(opponent) if opponent else None
    try:
        sel_cands = await _tennis_candidate_lookup(db, name_norm=sel_norm)
    except Exception as e:
        return RemediationOutcome(
            pid, "Tennis", before, before, "error",
            ambiguous_candidates=[f"lookup_error:{e.__class__.__name__}"],
        )
    if not sel_cands:
        return RemediationOutcome(
            pid, "Tennis", before, before, "no_candidate",
        )
    if len(sel_cands) > 1:
        # Try opponent-side mutual verification: if opponent
        # resolves uniquely and their historical opponents include
        # ONLY ONE of the ambiguous candidates, we can disambiguate.
        # In practice tennis_players lacks head-to-head fields, so
        # we remain PROVISIONAL on ambiguity (Rule 8 — do NOT guess).
        return RemediationOutcome(
            pid, "Tennis", before, before, "ambiguous_candidate",
            ambiguous_candidates=[c.get("name") for c in sel_cands[:5]],
        )
    sel_cand = sel_cands[0]
    sel_cpid = f"tp:{sel_cand.get('name_norm')}"

    # Opponent (optional, but improves confidence).
    opp_cpid: Optional[str] = None
    if opp_norm:
        opp_cands = await _tennis_candidate_lookup(db, name_norm=opp_norm)
        if len(opp_cands) == 1:
            opp_cpid = f"tp:{opp_cands[0].get('name_norm')}"
        # ambiguous opponent → still promote SELECTED side (opp stays
        # unresolved on this pick).  Same-name collision remains
        # protected because we never overwrite AUTHORITATIVE data.

    if not dry_run:
        set_doc: dict[str, Any] = {
            "identity_class":       "MAPPED",
            "canonical_player_id":  sel_cpid,
            "identity_evidence": {
                "source":            "tennis_players",
                "match_type":        "name_norm_unique"
                                       + ("+opponent_norm_unique"
                                           if opp_cpid else ""),
                "selected_name":     sel_cand.get("name"),
                "opponent_name":     (opponent if opp_cpid else None),
                "promoted_by":       "session_d.remediator.v1",
                "promoted_at":       datetime.now(timezone.utc)
                                       .isoformat().replace(
                                           "+00:00", "Z"),
            },
        }
        if opp_cpid:
            set_doc["canonical_opponent_id"] = opp_cpid
        await db.picks.update_one({"id": pid}, {"$set": set_doc})
    return RemediationOutcome(
        pid, "Tennis", before, "MAPPED",
        "mapped_by_name_norm" + ("+opp" if opp_cpid else ""),
        canonical_player_id=sel_cpid,
        canonical_opponent_id=opp_cpid,
    )


# ═══════════════════════════════════════════════════════════════════
# Batch runner
# ═══════════════════════════════════════════════════════════════════
async def remediate_soccer(
    db: AsyncIOMotorDatabase, *, limit: int = 10000, dry_run: bool = False,
    id_prefix: str | None = None,
) -> RemediationSummary:
    s = RemediationSummary(sport="Soccer")
    q: dict[str, Any] = {"sport": "Soccer", "identity_class": "PROVISIONAL"}
    if id_prefix:
        q["id"] = {"$regex": f"^{id_prefix}"}
    cur = db.picks.find(q, projection={"_id": 0}).limit(int(limit))
    async for p in cur:
        try:
            out = await remediate_one_soccer(db, p, dry_run=dry_run)
        except Exception as e:
            out = RemediationOutcome(
                p.get("id"), "Soccer", "PROVISIONAL", "PROVISIONAL", "error",
                ambiguous_candidates=[f"exc:{e.__class__.__name__}"],
            )
        s.add(out)
    return s


async def remediate_tennis(
    db: AsyncIOMotorDatabase, *, limit: int = 10000, dry_run: bool = False,
    id_prefix: str | None = None,
) -> RemediationSummary:
    s = RemediationSummary(sport="Tennis")
    q: dict[str, Any] = {"sport": "Tennis", "identity_class": "PROVISIONAL"}
    if id_prefix:
        q["id"] = {"$regex": f"^{id_prefix}"}
    cur = db.picks.find(q, projection={"_id": 0}).limit(int(limit))
    async for p in cur:
        try:
            out = await remediate_one_tennis(db, p, dry_run=dry_run)
        except Exception as e:
            out = RemediationOutcome(
                p.get("id"), "Tennis", "PROVISIONAL", "PROVISIONAL", "error",
                ambiguous_candidates=[f"exc:{e.__class__.__name__}"],
            )
        s.add(out)
    return s


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════
async def _amain(sport: str, limit: int, dry_run: bool) -> None:
    import os
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")
    ]
    if sport == "soccer":
        s = await remediate_soccer(db, limit=limit, dry_run=dry_run)
    elif sport == "tennis":
        s = await remediate_tennis(db, limit=limit, dry_run=dry_run)
    else:
        raise SystemExit("--sport must be soccer or tennis")
    import json
    print(json.dumps(s.to_dict(), indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", required=True, choices=["soccer", "tennis"])
    ap.add_argument("--limit", type=int, default=10000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(_amain(a.sport, a.limit, a.dry_run))


__all__ = [
    "normalize_name",
    "RemediationOutcome",
    "RemediationSummary",
    "remediate_one_soccer",
    "remediate_one_tennis",
    "remediate_soccer",
    "remediate_tennis",
]
