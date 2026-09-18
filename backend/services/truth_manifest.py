"""P0.5 — Cross-surface truth manifest + pick truth fingerprint.

Every board/slate response exposes the SAME manifest so Preview, Production
Web and Expo Go can prove they are reading one canonical publication:

    api_origin, environment, board_version, generated_at, data_as_of

For any pick a deterministic fingerprint (sha256 over the canonical scored
fields) can be computed on the server AND on any client that holds the same
row; equal fingerprints ⇒ identical truth.  Presentation-only fields are
excluded on purpose.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

FINGERPRINT_FIELDS: tuple[str, ...] = (
    "canonical_pick_id", "publication_version", "sport", "canonical_event_id",
    "market", "selection", "line", "odds", "book", "model_probability",
    "lock_score", "grade", "model_version", "generation_id",
)


def environment_name() -> str:
    return (
        os.environ.get("PERKLOCKS_ENV")
        or os.environ.get("APP_ENV")
        or ("production" if os.environ.get("EMERGENT_PRODUCTION") else "preview")
    )


def api_origin_from_request(request) -> Optional[str]:
    try:
        h = request.headers
        proto = h.get("x-forwarded-proto") or request.url.scheme
        host = h.get("x-forwarded-host") or h.get("host") or request.url.netloc
        if host:
            return f"{proto}://{host}"
        return str(request.base_url).rstrip("/") or None
    except Exception:
        return os.environ.get("PUBLIC_API_ORIGIN")


def _data_as_of(picks: Iterable[dict]) -> Optional[str]:
    best: Optional[str] = None
    for p in picks:
        for k in ("published_at", "updated_at", "generated_at"):
            v = p.get(k)
            if isinstance(v, datetime):
                v = v.isoformat()
            if isinstance(v, str) and (best is None or v > best):
                best = v
    return best


def build_manifest(request, board_version: Optional[str], picks: Iterable[dict],
                   *, publication_version: Optional[Any] = None) -> dict:
    picks = list(picks)
    return {
        "api_origin": api_origin_from_request(request),
        "environment": environment_name(),
        "board_version": board_version,
        "publication_version": publication_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_as_of": _data_as_of(picks),
        "pick_count": len(picks),
    }


def fingerprint_fields(pick: dict) -> dict:
    """Project a pick onto the canonical fingerprint field set."""
    prob = pick.get("published_probability")
    if prob is None and pick.get("win_probability") is not None:
        try:
            prob = round(float(pick["win_probability"]) / 100.0, 4)
        except (TypeError, ValueError):
            prob = None
    elif isinstance(prob, (int, float)):
        prob = round(float(prob), 4)
    return {
        "canonical_pick_id": pick.get("canonical_pick_id") or pick.get("id") or pick.get("pick_id"),
        "publication_version": pick.get("publication_version") or pick.get("snapshot_version"),
        "sport": pick.get("sport"),
        "canonical_event_id": pick.get("canonical_event_id") or pick.get("event_id") or pick.get("external_id"),
        "market": pick.get("market"),
        "selection": pick.get("selection"),
        "line": pick.get("line"),
        "odds": pick.get("book_odds") if pick.get("book_odds") is not None else pick.get("odds"),
        "book": pick.get("book") or pick.get("bookmaker") or pick.get("sportsbook"),
        "model_probability": prob,
        "lock_score": pick.get("lock_score"),
        "grade": pick.get("grade"),
        "model_version": pick.get("model_version"),
        "generation_id": pick.get("generation_id") or pick.get("board_version"),
    }


def truth_fingerprint(pick: dict) -> str:
    payload = json.dumps(fingerprint_fields(pick), sort_keys=True, default=str,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


__all__ = ["build_manifest", "fingerprint_fields", "truth_fingerprint",
           "environment_name", "api_origin_from_request", "FINGERPRINT_FIELDS"]
