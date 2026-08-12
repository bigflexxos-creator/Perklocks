"""Admin routes — Pre-Magic Certification.

READ-ONLY endpoints that expose the Pre-Magic certification matrix.
No route in this file writes to any collection or wires any consumer.

Endpoints
---------
GET  /api/admin/certification/pre-magic
     Return the live certification matrix.  Runs every check on the
     current pod DB and persists ``/tmp/pre_magic_certification.json``.

GET  /api/admin/certification/pre-magic/latest
     Serve the most recent on-disk certification report (fast — no DB
     hits).  Falls back to running the live matrix if no file exists.

The matrix's ``magic_consumption`` field is ALWAYS ``NOT_WIRED``.
Under no circumstances does this router promote Magic.  §15.
"""
from __future__ import annotations

import json
import os
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import UserPublic
from deps import current_admin, db
from services.pre_magic_certification import (
    build_certification_matrix,
    write_certification_report,
)
from services.pre_magic_certification.certifier import DEFAULT_REPORT_PATH


router = APIRouter(prefix="/api/admin/certification", tags=["certification"])


@router.get("/pre-magic")
async def pre_magic_certification(
    user: Annotated[UserPublic, Depends(current_admin)],
    live_pick_sample: int = Query(25, ge=0, le=250),
    market_sample: int = Query(200, ge=1, le=1000),
    persist: bool = Query(True),
):
    """Run the complete Pre-Magic certification harness.

    Query params
    ------------
    * ``live_pick_sample``  — number of most-recent published picks
      to probe end-to-end (default 25, max 250).  Set to 0 to skip
      the live reachability check.
    * ``market_sample``     — cap on picks inspected by the
      market-readiness / soccer / model-readiness checks.
    * ``persist``           — write ``pre_magic_certification.json``
      to disk (default True).
    """
    try:
        matrix = await build_certification_matrix(
            db,
            live_pick_sample=live_pick_sample,
            market_sample=market_sample,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"certification run failed: {e!r}",
        )
    path = None
    if persist:
        path = write_certification_report(matrix)
    body = matrix.to_dict()
    body["report_path"] = path
    return body


@router.get("/pre-magic/latest")
async def pre_magic_certification_latest(
    user: Annotated[UserPublic, Depends(current_admin)],
    path: Optional[str] = Query(None),
):
    """Return the last-written certification report, if any.

    Falls back to a fresh run when no report exists on disk yet.  Use
    the primary ``/pre-magic`` endpoint to force a re-run.
    """
    p = path or DEFAULT_REPORT_PATH
    if p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["report_path"] = p
            data["from_cache"] = True
            return data
        except Exception as e:
            raise HTTPException(500, f"cached report unreadable: {e!r}")
    # No report — run fresh (persist so future calls are fast).
    matrix = await build_certification_matrix(db)
    p2 = write_certification_report(matrix)
    body = matrix.to_dict()
    body["report_path"] = p2
    body["from_cache"] = False
    return body
