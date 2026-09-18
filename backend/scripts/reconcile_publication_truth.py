"""P0.3 one-time reconciliation — persist the truth the reader was already
serving so the publication snapshot becomes the ONLY truth.

Before this closure `hydrate()` let v4-stamped mutable top-level fields
override the frozen snapshot at read time.  The board therefore showed the
v4 top-level numbers while the snapshot lagged.  We now:

  * v4-stamped pending rows whose top-level (lock_score / win_probability /
    edge_percent) differs from published_*  → re-publish through
    PredictionPublicationService (NEW snapshot version, prior deactivated,
    dual-write aliases).  No number the user currently sees changes.
  * any other pending row whose aliases or grade drift from the snapshot →
    re-publish FROM the snapshot values (same truth ⇒ same version), which
    realigns aliases and repairs a non-canonical published_grade.

Usage: python scripts/reconcile_publication_truth.py [--dry-run] [--limit N]
"""
from __future__ import annotations
import asyncio, os, sys, logging
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.WARNING)
logging.getLogger("lockscore.publication").setLevel(logging.ERROR)


def _canon_grade(ls: float) -> str:
    from sports_engine import _grade
    return _grade(float(ls))


async def main() -> None:
    dry = "--dry-run" in sys.argv
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "lockscore_db")]
    from services.prediction_publication_service import PredictionPublicationService
    svc = PredictionPublicationService(db, board_version="board-p03-reconcile")
    q = {"status": "pending", "published_lock_score": {"$exists": True}}
    proj = {"_id": 0}
    n = v4_repub = snap_realign = grade_fix = skipped = 0
    cur = db.picks.find(q, proj)
    async for d in cur:
        n += 1
        if limit and n > limit:
            break
        ls, pls = d.get("lock_score"), d.get("published_lock_score")
        wp, pwp = d.get("win_probability"), d.get("published_probability")
        pwp_pct = round(pwp * 100, 2) if isinstance(pwp, (int, float)) and pwp <= 1 else pwp
        edge, pedge = d.get("edge_percent"), d.get("published_edge")
        grade, pgrade = d.get("grade"), d.get("published_grade")
        v4 = str(d.get("lock_score_version") or "").startswith("v4")
        top_diff = (
            (ls is not None and pls is not None and abs(float(ls) - float(pls)) > 0.001)
            or (wp is not None and pwp_pct is not None and abs(float(wp) - float(pwp_pct)) > 0.05)
            or ((edge is None) != (pedge is None))
            or (edge is not None and pedge is not None and abs(float(edge) - float(pedge)) > 0.005)
        )
        canon_g = _canon_grade(pls if not (v4 and top_diff) else ls) if (pls is not None or ls is not None) else None
        grade_bad = (pgrade != canon_g) or (grade != canon_g)
        if not top_diff and not grade_bad:
            continue
        if v4 and top_diff:
            candidate = dict(d)  # v4 top-level values are the truth the board served
            v4_repub += 1
        else:
            candidate = dict(d)
            candidate["lock_score"] = pls
            candidate["win_probability"] = pwp_pct
            candidate["edge_percent"] = pedge
            candidate["book_odds"] = d.get("published_odds", d.get("book_odds"))
            candidate["line"] = d.get("published_line", d.get("line"))
            snap_realign += 1
        if grade_bad:
            grade_fix += 1
        candidate["grade"] = canon_g
        if dry:
            continue
        try:
            await svc.publish(candidate, publication_source="p03_truth_reconcile")
        except Exception as e:
            skipped += 1
            print("publish failed", d.get("id"), e)
        if (v4_repub + snap_realign) % 2000 == 0:
            print(f"progress scanned={n} v4_repub={v4_repub} realign={snap_realign} grade_fix={grade_fix}")
    print(f"DONE scanned={n} v4_republished={v4_repub} snapshot_realigned={snap_realign} grade_fixed={grade_fix} failed={skipped} dry={dry}")

asyncio.run(main())
