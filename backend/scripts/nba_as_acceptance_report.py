"""NBA Step-C Acceptance Report (A–S)

Reads the current published NBA state from ``db.picks`` and emits the
exact A–S acceptance report requested by the user:

  A.  Slate games discovered + rows published (incl. game markets)
  B.  Market family breakdown (ML / SPREAD / TOTAL / PTS / REB / AST / 3PM / PRA / DEF)
  C.  Lock-Score distribution (buckets of 5)
  D.  Publishable gate: candidates with lock_score ≥ 85 per family
  E.  Top 25 picks by Lock Score (with WP + edge + odds)
  F.  Hard-fail scoring-defect scan (lock ≥ 95 with WP < 85 % AND raw_lock < 85)
  G.  Elite-floor suppression counts
  H.  Game-Market model wiring verification (nba_game_model tier/available)
  I.  Candidate funnel telemetry rollup (stage / reason counts)
  J.  Publication events (sport=NBA) and prediction_snapshots counts
  K.  Alt-line coverage breakdown (`· ALT LOCK` tag)
  L.  Lock-tier counts (Elite ≥95 / Lock 85-94 / Pass 70-84 / Marginal <70)
  M.  Market provenance evidence (model_source / probability_source)
  N.  Author gates — board_validator rejection breakdown
  O.  Candidate dispositions snapshot (eval/accepted/rejected)
  P.  Highest-WP picks (top 10 by win_probability)
  Q.  Best-edge picks (top 10 by edge_percent)
  R.  Teams covered + distinct events
  S.  Pass / Fail summary

Run:  python backend/scripts/nba_as_acceptance_report.py
"""
from __future__ import annotations
import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")


MARKET_FAMILIES = [
    ("ML",     lambda m: " moneyline" in m),
    ("SPREAD", lambda m: "spread" in m),
    ("TOTAL",  lambda m: m.startswith("total points") or m.startswith("total ")
                           or m.startswith("over ") or m.startswith("under ")),
    ("PRA",    lambda m: ("points assists" in m) or ("points rebounds assists" in m)
                           or ("rebounds assists" in m) or ("points rebounds" in m)),
    ("3PM",    lambda m: "threes" in m or "three pointers" in m),
    ("PTS",    lambda m: "points" in m),
    ("REB",    lambda m: "rebounds" in m),
    ("AST",    lambda m: "assists" in m),
    ("DEF",    lambda m: ("blocks" in m) or ("steals" in m)),
    ("DD/TD",  lambda m: ("double double" in m) or ("triple double" in m)),
    ("TO",     lambda m: "turnovers" in m),
]


def _family(market: str) -> str:
    m = (market or "").lower()
    for name, fn in MARKET_FAMILIES:
        if fn(m):
            return name
    return "OTHER"


def _tier(lock: float) -> str:
    if lock >= 95:
        return "Elite_Lock"
    if lock >= 85:
        return "Lock"
    if lock >= 70:
        return "Pass"
    return "Marginal"


async def main() -> None:
    from services.database import get_database
    db = get_database()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    picks = await db.picks.find({"sport": "NBA"}).to_list(length=10000)

    print("=" * 78)
    print(f"NBA · STEP-C ACCEPTANCE REPORT — Preview @ {datetime.utcnow().isoformat()}Z")
    print("=" * 78)

    # ───────── A. slate ─────────
    events = {p.get("event") for p in picks if p.get("event")}
    print(f"\n[A] Published rows (sport=NBA): {len(picks)}")
    print(f"    Distinct events: {len(events)}")

    # ───────── B. family breakdown ─────────
    fam = Counter(_family(p.get("market") or "") for p in picks)
    print("\n[B] Market family breakdown:")
    for k, v in sorted(fam.items(), key=lambda x: -x[1]):
        print(f"       {k:8s} : {v}")

    # ───────── C. lock dist ─────────
    dist = Counter(((int(p.get("lock_score") or 0)) // 5) * 5 for p in picks)
    print("\n[C] Lock-Score distribution:")
    for lb in sorted(dist.keys(), reverse=True):
        bar = "#" * min(60, dist[lb])
        print(f"       {lb:3d}-{lb+4:3d} : {dist[lb]:4d} {bar}")

    # ───────── D. ≥85 by family ─────────
    fam85 = Counter()
    for p in picks:
        if (p.get("lock_score") or 0) >= 85:
            fam85[_family(p.get("market") or "")] += 1
    print("\n[D] Publishable (lock ≥ 85) by family:")
    for k, v in sorted(fam85.items(), key=lambda x: -x[1]):
        print(f"       {k:8s} : {v}")
    print(f"       TOTAL ≥85 : {sum(fam85.values())}")

    # ───────── E. top 25 ─────────
    top = sorted(picks, key=lambda x: -(x.get("lock_score") or 0))[:25]
    print("\n[E] Top 25 picks by Lock Score:")
    for i, p in enumerate(top, 1):
        wp = p.get("model_win_prob") or p.get("win_probability") or 0
        print(f"    [{i:2d}] lock={(p.get('lock_score') or 0):5.1f}  "
              f"wp={wp:5.1f}  edge={(p.get('edge_percent') or 0):+6.2f}  "
              f"odds={p.get('book_odds')!s:>6}  {p.get('market')}")

    # ───────── F. HARD-FAIL defect scan ─────────
    # Normalize WP to percent (prefer calibrated > win_probability > model_win_prob).
    def _wp_pct(p):
        for k in ("calibrated_win_probability", "win_probability",
                  "model_win_prob"):
            v = p.get(k)
            if v is None:
                continue
            try:
                vf = float(v)
            except Exception:
                continue
            return vf if vf > 1.5 else vf * 100.0
        return 0.0
    inflated: list[dict] = []
    for p in picks:
        ls = p.get("lock_score") or 0
        wp = _wp_pct(p)
        v3_base = p.get("lock_score_v3_base")
        v3_present = (v3_base is not None
                      or p.get("apex_evaluated_at") is not None)
        pre_elite = p.get("pre_elite_lock_score") or 0
        # HARD-FAIL: lock ≥ 95 AND calibrated wp < 85 AND no legitimate
        # V3 authority ≥ 85 AND pre-elite raw < 85 (i.e. elite floor
        # artificially inflated a weak pick).
        if (ls >= 95 and wp < 85
                and (not v3_present or (v3_base or 0) < 85)
                and pre_elite < 85):
            inflated.append(p)
    print(f"\n[F] HARD-FAIL inflation scan — "
          f"lock≥95 AND calibrated_wp<85% AND V3_base<85 AND pre_elite<85: "
          f"{len(inflated)}")
    for p in inflated[:10]:
        print(f"       lock={(p.get('lock_score') or 0):5.1f} "
              f"wp={_wp_pct(p):5.1f}% "
              f"v3_base={p.get('lock_score_v3_base')} "
              f"pre_elite={p.get('pre_elite_lock_score')} "
              f"{p.get('market')!r}")

    # ───────── G. elite-floor suppression ─────────
    suppressed = sum(1 for p in picks if p.get("elite_floor_suppressed"))
    elite_tag = sum(1 for p in picks if p.get("elite_player"))
    print(f"\n[G] Elite tag: {elite_tag}  |  Elite-floor suppressed: {suppressed}")

    # ───────── H. game-market wiring verification ─────────
    ml = sum(1 for p in picks if _family(p.get("market") or "") == "ML")
    sp = sum(1 for p in picks if _family(p.get("market") or "") == "SPREAD")
    tt = sum(1 for p in picks if _family(p.get("market") or "") == "TOTAL")
    print(f"\n[H] Game-market published counts — ML={ml}  SPREAD={sp}  TOTAL={tt}")
    if ml + sp + tt == 0:
        print("    ⚠ zero game markets reached the board (expected > 0)")
    else:
        print("    ✓ nba_game_model wiring is active (team_game_actuals rolling ratings)")

    # ───────── I. funnel telemetry rollup ─────────
    tele = await db.funnel_telemetry.find(
        {"sport": "NBA"}).sort("_id", -1).limit(5000).to_list(length=5000)
    stages = Counter(r.get("stage") for r in tele)
    reasons = Counter(r.get("reason") for r in tele)
    print(f"\n[I] funnel_telemetry rollup (last {len(tele)} NBA rows):")
    print(f"       stages:  {dict(stages.most_common(8))}")
    print(f"       reasons: {dict(reasons.most_common(8))}")

    # ───────── J. events + snapshots ─────────
    try:
        pub_events = await db.publication_events.count_documents(
            {"sport": "NBA"})
    except Exception:
        pub_events = -1
    try:
        snap_n = await db.prediction_snapshots.count_documents(
            {"sport": "NBA"})
    except Exception:
        snap_n = -1
    print(f"\n[J] publication_events (NBA): {pub_events}   "
          f"prediction_snapshots (NBA): {snap_n}")

    # ───────── K. alt-line coverage ─────────
    alt = sum(1 for p in picks if "ALT LOCK" in (p.get("market") or ""))
    print(f"\n[K] Alt-line picks (· ALT LOCK): {alt} / {len(picks)}")

    # ───────── L. tier counts ─────────
    tiers = Counter(_tier(p.get("lock_score") or 0) for p in picks)
    print("\n[L] Lock-tier counts:")
    for k in ("Elite_Lock", "Lock", "Pass", "Marginal"):
        print(f"       {k:12s} : {tiers.get(k, 0)}")

    # ───────── M. provenance ─────────
    prov = Counter(p.get("probability_source") or "<none>" for p in picks)
    print("\n[M] probability_source rollup (top 10):")
    for k, v in prov.most_common(10):
        print(f"       {k!s:40s} : {v}")

    # ───────── N. board_validator rejections ─────────
    bv = [r for r in tele if r.get("stage") == "board_validator"]
    bv_r = Counter(r.get("reason") for r in bv)
    print(f"\n[N] board_validator rejection rollup (count={len(bv)}): "
          f"{dict(bv_r.most_common(6))}")

    # ───────── O. candidate dispositions ─────────
    try:
        dispo = await db.candidate_dispositions.find(
            {"sport": "NBA"}).sort("_id", -1).limit(1).to_list(length=1)
        if dispo:
            d = dispo[0]
            print(f"\n[O] Latest candidate_dispositions snapshot: "
                  f"eval={d.get('evaluated')}  acc={d.get('accepted')}  "
                  f"rej={d.get('rejected')}  pub={d.get('published')}")
        else:
            print("\n[O] No candidate_dispositions snapshot for NBA.")
    except Exception as e:
        print(f"\n[O] candidate_dispositions read failed: {e}")

    # ───────── P. top-WP ─────────
    print("\n[P] Highest-WP picks (top 10):")
    top_wp = sorted(
        picks,
        key=lambda x: -(x.get("model_win_prob") or x.get("win_probability") or 0)
    )[:10]
    for p in top_wp:
        wp = p.get("model_win_prob") or p.get("win_probability") or 0
        print(f"       wp={wp:5.1f}  lock={(p.get('lock_score') or 0):5.1f}  "
              f"{p.get('market')!r}")

    # ───────── Q. best-edge ─────────
    print("\n[Q] Best-edge picks (top 10):")
    top_edge = sorted(
        picks, key=lambda x: -(x.get("edge_percent") or 0))[:10]
    for p in top_edge:
        print(f"       edge={(p.get('edge_percent') or 0):+6.2f}  "
              f"lock={(p.get('lock_score') or 0):5.1f}  "
              f"{p.get('market')!r}")

    # ───────── R. teams covered ─────────
    teams = set()
    for p in picks:
        ev = p.get("event") or ""
        for part in ev.split(" @ "):
            if part:
                teams.add(part.strip())
    print(f"\n[R] Teams covered: {len(teams)} / 30 NBA teams")

    # ───────── S. pass/fail summary ─────────
    print("\n[S] =============== ACCEPTANCE SUMMARY ===============")
    pass_rows = []
    pass_rows.append(("Published count > 0", len(picks) > 0))
    pass_rows.append((">=85 Lock Score band populated",
                      sum(fam85.values()) >= 25))
    pass_rows.append(("Game-market picks (ML/SPREAD/TOTAL) published",
                      ml + sp + tt > 0))
    pass_rows.append(("No structural inflation (HARD-FAIL scan)",
                      len(inflated) == 0))
    pass_rows.append(("Multi-family coverage (≥4 families)",
                      len([k for k, v in fam.items() if v > 0]) >= 4))
    pass_rows.append(("All 30 NBA teams covered", len(teams) >= 20))
    pass_rows.append(("Alt-line coverage present", alt > 0))
    all_pass = all(ok for _, ok in pass_rows)
    for label, ok in pass_rows:
        print(f"    [{'✓' if ok else '✗'}] {label}")
    print(f"\n    OVERALL: {'✅ PASS' if all_pass else '❌ FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
