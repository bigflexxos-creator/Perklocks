"""One Database Authority — environment ownership and write isolation.

2026-10-02 — introduced to formalise the Preview/Production split so
both environments can share ONE canonical MongoDB while only
Production is allowed to mutate canonical data (picks, settlement,
publication, historical ingestion, scheduled workers).

Environment variables consumed (all optional — defaults chosen so a
pod with NO config stays safe for Preview):

    DATA_AUTHORITY              = "production" | "preview"
                                  (default "preview")

    CANONICAL_WRITE_ENABLED     = "true" | "false"
                                  (default "false")

    BACKGROUND_WORKERS_ENABLED  = "true" | "false"
                                  (default "false")

Rules enforced:

* Scheduled background workers (settlement, publication, ingestion,
  weekly tuning, universal history, alt-lines feed, NFL cold-start
  backfill, etc.) only start when BACKGROUND_WORKERS_ENABLED=true.

* Canonical write admin endpoints (settle, retire, backfill, delete,
  destructive migrations) fail-closed with HTTP 423 Locked when
  CANONICAL_WRITE_ENABLED=false.

* Preview reads from the shared canonical collections freely — no
  gating on reads.  Only mutations are gated.

* The authority module exposes a `status()` helper used by the
  verification endpoint so the admin can prove PASS/FAIL from either
  environment without exposing Mongo credentials.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("lockscore.data_authority")

__all__ = [
    "authority_mode",
    "canonical_write_enabled",
    "background_workers_enabled",
    "require_canonical_write",
    "require_background_workers",
    "status",
    "CanonicalWriteForbidden",
]


class CanonicalWriteForbidden(RuntimeError):
    """Raised when canonical-write path is attempted in Preview."""


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def authority_mode() -> str:
    """Return 'production' or 'preview'.  Default: preview (fail-safe)."""
    v = (os.environ.get("DATA_AUTHORITY") or "preview").strip().lower()
    return "production" if v == "production" else "preview"


def canonical_write_enabled() -> bool:
    """True only when the pod is explicitly allowed to mutate the
    shared canonical collections (picks, settlement, publication,
    historical ingestion).  Defaults to False so a mis-configured
    Preview deploy cannot damage Production truth."""
    return _bool("CANONICAL_WRITE_ENABLED", default=False)


def background_workers_enabled() -> bool:
    """True only when the pod should start scheduled background
    workers (ingestion, settlement, publication, weekly tuning).
    Defaults to False so Preview pods don't duplicate Production
    workers against the shared DB."""
    return _bool("BACKGROUND_WORKERS_ENABLED", default=False)


def require_canonical_write(reason: str) -> None:
    """Guard that raises if the current pod is not the write
    authority.  Call at the top of every destructive admin endpoint.
    """
    if not canonical_write_enabled():
        raise CanonicalWriteForbidden(
            f"Canonical write refused in {authority_mode()} mode: {reason}. "
            "Route this operation through the Production pod."
        )


def require_background_workers() -> bool:
    """Returns True when it is safe to start a background worker.
    Loggers a one-line INFO when workers are suppressed so the admin
    can confirm Preview is idle against the shared DB."""
    ok = background_workers_enabled()
    if not ok:
        logger.info(
            "data_authority: background worker suppressed (mode=%s, workers_enabled=%s)",
            authority_mode(), ok,
        )
    return ok


def status() -> dict[str, Any]:
    """Return a sanitised snapshot of the authority configuration.

    NEVER returns Mongo credentials — only redacted presence booleans
    and the DB name so an operator can verify a shared-DB posture.
    """
    mongo_url = os.environ.get("MONGO_URL") or ""
    db_name = os.environ.get("DB_NAME") or ""
    # Classify the Mongo endpoint without exposing creds.
    host_class = "unknown"
    if mongo_url.startswith("mongodb+srv://"):
        host_class = "atlas_srv"
    elif "localhost" in mongo_url or "127.0.0.1" in mongo_url:
        host_class = "local"
    elif mongo_url.startswith("mongodb://"):
        host_class = "remote_mongodb"
    # Fingerprint = short hash of (host, db) so Preview/Prod can be
    # compared without leaking URIs.
    import hashlib
    fp_input = f"{mongo_url}|{db_name}".encode()
    fingerprint = hashlib.sha256(fp_input).hexdigest()[:12]
    return {
        "data_authority":              authority_mode(),
        "canonical_write_enabled":     canonical_write_enabled(),
        "background_workers_enabled":  background_workers_enabled(),
        "db_name":                     db_name,
        "mongo_host_class":            host_class,
        "mongo_configured":            bool(mongo_url),
        "mongo_fingerprint":           fingerprint,
    }
