"""migration_download_routes — TEMPORARY, PREVIEW-ONLY.

One-time token-gated streaming download of the SHA-verified R3
cutover checkpoints so that an external GitHub Codespaces
environment can obtain them without exposing EMERGENT_LLM_KEY or
touching Production.

HARD GUARANTEES
───────────────
1. Read-only. Serves bytes from /app/reconcile_workspace/checkpoints/
   under a fixed allowlist; **does not** mutate, delete, rename, or
   enumerate anything.
2. Allowlist-only filenames. No path traversal, no wildcards.
3. Token required from env (``MIGRATION_DOWNLOAD_TOKEN``). If the env
   var is missing, the route returns 503 for every request — the
   route cannot be "accidentally open".
4. Only accepts the token via the ``X-Download-Token`` header so the
   secret never lands in access logs or ``curl``'s shell history URL.
5. Streams in chunks (64 KiB) so the backend memory footprint stays
   constant regardless of file size.
6. Returns the file's exact pinned SHA-256 in the ``X-File-SHA256``
   response header so the client can short-circuit verify before
   consuming the body.
7. Logging is deliberately silent on auth outcomes — no 401 bodies
   contain reasons, no server log echoes the token or filename beyond
   the allowlist key.

REMOVAL PLAN
────────────
After the user confirms Codespaces has downloaded both files and
SHA-verified them, delete this file, unregister it in server.py, and
remove ``MIGRATION_DOWNLOAD_TOKEN`` from backend/.env.
"""
from __future__ import annotations

import hmac
import os
import pathlib
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["_migration_download"])

# Allowlist: pinned filename → pinned SHA-256.  Any other filename
# — including anything with slashes, `..`, URL-encoding, or case
# variants — is rejected with 404.  The dict IS the security boundary.
_ALLOWLIST: dict[str, str] = {
    "phase5_20261003_190628Z.tar.gz":
        "4adc99890885e7b712adfd491124c2ef681715996167d19719ddb04f8eb1e9d7",
    "phase6_20261003_192150Z.tar.gz":
        "327a38903daf3bd910ab50b68f69415cfcf181624655b42fb791af43042b6c22",
}

_CHKP_DIR = pathlib.Path("/app/reconcile_workspace/checkpoints")
_CHUNK = 64 * 1024  # 64 KiB streaming chunks


def _constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe comparison; avoids leaking token length / content
    via response-time variance."""
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


@router.get("/_migration_download/{filename}")
def download_checkpoint(
    filename: str,
    x_download_token: Optional[str] = Header(default=None, alias="X-Download-Token"),
):
    expected_token = os.environ.get("MIGRATION_DOWNLOAD_TOKEN")
    # If the operator has not provisioned a token at all, the route is
    # inert.  This is the fail-closed state after the migration ends.
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="migration_download_disabled",
        )

    # Timing-safe token check first (so unauthenticated callers cannot
    # probe the allowlist by observing which filenames return 404
    # vs 401).
    if not x_download_token or not _constant_time_eq(x_download_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_missing_token",
        )

    # Allowlist enforcement.  Deliberately an exact-equality check.
    pinned_sha = _ALLOWLIST.get(filename)
    if pinned_sha is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="file_not_in_allowlist",
        )

    # Belt-and-braces: refuse anything that even LOOKS like traversal.
    # (Already impossible given the allowlist dict, but we keep the
    # defensive check so a future refactor cannot silently open a hole.)
    if (("/" in filename) or ("\\" in filename) or (".." in filename)
            or filename.startswith(".") or filename != pathlib.PurePosixPath(filename).name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="file_not_in_allowlist",
        )

    file_path = _CHKP_DIR / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="checkpoint_not_present_on_server",
        )

    size = file_path.stat().st_size

    def _stream():
        # Open per-request so a container recycle mid-stream fails
        # the client-side download cleanly (which the SHA check on the
        # client side will catch) rather than returning a partial body
        # tagged as success.
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Length":        str(size),
            "Content-Disposition":   f'attachment; filename="{filename}"',
            # Client-side SHA short-circuit (optional); the pinned
            # value is also hardcoded on the Codespaces consumer.
            "X-File-SHA256":         pinned_sha,
            "Cache-Control":         "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
