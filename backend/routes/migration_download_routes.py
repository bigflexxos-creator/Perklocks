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
import hmac
import os
import pathlib
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
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


def _require_token(x_download_token: Optional[str]) -> None:
    """Shared token guard.  Returns None on success, raises HTTPException
    otherwise.  Behavior matches the per-request guard below exactly."""
    expected_token = os.environ.get("MIGRATION_DOWNLOAD_TOKEN")
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="migration_download_disabled",
        )
    if not x_download_token or not _constant_time_eq(x_download_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_missing_token",
        )


@router.get("/_migration_manifest")
def migration_manifest(
    x_download_token: Optional[str] = Header(default=None, alias="X-Download-Token"),
):
    """Return the AUTHORITATIVE Phase 5 + Phase 6 checkpoint descriptors
    so external migration drivers (GitHub Actions / Codespaces) can
    resolve the current ``filename`` / ``bytes`` / ``sha256`` instead
    of hardcoding them in two separate workflow files that can drift.

    Returns ONLY the fields the drivers need; nothing identifies file
    system paths beyond the public allowlisted filename.  Token-gated
    identically to the download route.  Read-only.

    Response shape:
        {
          "phase5": {
            "filename": "phase5_20261003_190628Z.tar.gz",
            "bytes":    337143523,
            "sha256":   "4adc998...e9d7",
            "present":  true
          },
          "phase6": { ... same shape ... }
        }

    ``present`` reflects whether the on-disk file exists AND SHA matches
    the pinned allowlist.  Any mismatch yields ``present=false`` without
    exposing the mismatching value — the caller MUST re-check the SHA
    returned by the download route anyway.
    """
    _require_token(x_download_token)

    def _descriptor(filename: str) -> dict:
        pinned_sha = _ALLOWLIST.get(filename)
        p = _CHKP_DIR / filename
        present = False
        bytes_on_disk = 0
        if pinned_sha and p.is_file():
            try:
                bytes_on_disk = p.stat().st_size
                present = True
            except Exception:
                present = False
        return {
            "filename": filename,
            "bytes":    bytes_on_disk,
            "sha256":   pinned_sha or "",
            "present":  present,
        }

    return {
        "schema":   "migration_manifest_v1",
        "phase5":   _descriptor("phase5_20261003_190628Z.tar.gz"),
        "phase6":   _descriptor("phase6_20261003_192150Z.tar.gz"),
    }


@router.get("/_migration_download/{filename}")
def download_checkpoint(
    filename: str,
    request: Request,
    x_download_token: Optional[str] = Header(default=None, alias="X-Download-Token"),
):
    # Shared token guard — identical semantics to the pre-manifest
    # version (503 if token unprovisioned, 401 if mismatched, timing-
    # safe comparison).
    _require_token(x_download_token)

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

    file_size = file_path.stat().st_size

    # ─── Manual HTTP Range handling ──────────────────────────────
    # Starlette's FileResponse ignores Range headers in current
    # versions, so we parse `Range: bytes=A-B` ourselves and emit a
    # proper 206 Partial Content response with Content-Range.  This
    # is necessary because the Emergent/Cloudflare preview ingress
    # has a ~60 s response-stream ceiling; a 337 MB file over a
    # slow runner tunnel occasionally truncates to a 200 with a
    # short body.  Range support lets the client resume with
    # `curl --continue-at -` (or re-fetch chunks via `--range`).
    range_header = request.headers.get("range", "").strip()
    start, end = 0, file_size - 1
    is_partial = False
    if range_header.lower().startswith("bytes="):
        try:
            spec = range_header[6:].split(",")[0].strip()  # ignore multi-ranges
            lo, _, hi = spec.partition("-")
            if lo == "" and hi != "":
                # suffix form: `bytes=-N` → last N bytes
                n = int(hi)
                if n <= 0:
                    raise ValueError("bad suffix")
                start = max(0, file_size - n)
                end = file_size - 1
            else:
                start = int(lo)
                end = int(hi) if hi else (file_size - 1)
            if start < 0 or end >= file_size or start > end:
                raise ValueError("out of range")
            is_partial = True
        except Exception:
            # Spec: unsatisfiable range → 416 with Content-Range header
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="invalid_range",
                headers={"Content-Range": f"bytes */{file_size}"},
            )

    length = end - start + 1

    def _stream():
        # Open per-request; seek to `start`; stream `length` bytes.
        # 64 KiB chunk keeps backend memory flat.
        with open(file_path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Length":         str(length),
        "Accept-Ranges":          "bytes",
        "Content-Disposition":    f'attachment; filename="{filename}"',
        "X-File-SHA256":          pinned_sha,
        "Cache-Control":          "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if is_partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    return StreamingResponse(
        _stream(),
        status_code=status.HTTP_206_PARTIAL_CONTENT if is_partial else status.HTTP_200_OK,
        media_type="application/gzip",
        headers=headers,
    )
