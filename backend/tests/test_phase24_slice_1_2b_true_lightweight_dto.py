"""SLICE 1.2B — TRUE Lightweight Board DTO byte-budget contract
===================================================================

Slice 1.2 shipped a blacklist-based lite payload (~22 fields stripped)
that still weighed ~1.08 MB across 110 picks. Slice 1.2B pivots the
lite path to a WHITELIST projection (see
`backend/server.py::_LITE_BOARD_WHITELIST` and `_strip_for_lite`) so
only the ~55 fields the collapsed LockPickCard actually renders reach
the wire.

Live-DB, ASGI in-process measurement on 2026-09-02:
    BEFORE Slice 1.2B :  1,109,762 B  (110 picks, ~10 KB / pick)
    AFTER  Slice 1.2B :    168,852 B  (110 picks, ~1.5 KB / pick)
                                            −84.8% payload
                                            −85%    per-pick

This contract locks the invariants against future regression:

    LITE_AVG_BYTES_PER_PICK  ≤ 3,000
    LITE_TOTAL_BYTES         ≤ FULL_TOTAL_BYTES × 0.35
    LITE_HAS_ONLY_WHITELIST  every top-level key ∈ _LITE_BOARD_WHITELIST
    NO_HEAVY_LEAKS           ban a rotating list of known-heavy fields
                             from the lite payload (identity_resolution,
                             statcast_batter, external_id, signal_engine,
                             sportsbook_mapping, fusion, snapshot, brain).
    NO_PUBLICATION_TELEMETRY the publication_* / identity_* debug halo
                             does not ride the board wire.
"""
from __future__ import annotations
import os, sys, json
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code}")
    return r.json()["access_token"]


def _fetch(path: str, tok: str):
    r = httpx.get(f"{_BASE}{path}",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    assert r.status_code == 200, r.text[:200]
    return r


# ── 1. Byte budget: lite avg ≤ 3 KB / pick, ≤ 35 % of full ──
def test_slice_1_2b_lite_payload_avg_under_3kb_per_pick():
    tok = _tok()
    r_lite = _fetch("/api/picks/today?lite=true", tok)
    picks = r_lite.json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    total = len(r_lite.content)
    avg = total / len(picks)
    assert avg <= 3000, (
        f"LITE_AVG_BYTES_PER_PICK regression: {avg:.0f}B > 3000B budget "
        f"(total={total:,}B / n={len(picks)})"
    )


def test_slice_1_2b_lite_at_most_35pct_of_full():
    tok = _tok()
    r_full = _fetch("/api/picks/today", tok)
    r_lite = _fetch("/api/picks/today?lite=true", tok)
    full = len(r_full.content)
    lite = len(r_lite.content)
    ratio = lite / max(full, 1)
    assert ratio <= 0.35, (
        f"LITE payload {lite:,}B is {ratio*100:.1f}% of FULL {full:,}B "
        f"(budget: ≤35%). Slice 1.2B regression."
    )


# ── 2. Whitelist-only projection ──
_ALLOWED_TOP_LEVEL_KEYS = None  # populated on first read from server.py


def _load_whitelist():
    global _ALLOWED_TOP_LEVEL_KEYS
    if _ALLOWED_TOP_LEVEL_KEYS is None:
        from server import _LITE_BOARD_WHITELIST
        _ALLOWED_TOP_LEVEL_KEYS = set(_LITE_BOARD_WHITELIST)
    return _ALLOWED_TOP_LEVEL_KEYS


def test_slice_1_2b_lite_has_only_whitelist_fields():
    tok = _tok()
    allowed = _load_whitelist()
    picks = _fetch("/api/picks/today?lite=true", tok).json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    offenders = {}
    for p in picks[:80]:
        for k in p.keys():
            if k not in allowed:
                offenders.setdefault(k, 0)
                offenders[k] += 1
    assert not offenders, (
        f"LITE_HAS_ONLY_WHITELIST breach: unexpected keys leaked into board DTO: "
        f"{sorted(offenders.items(), key=lambda t: -t[1])[:10]}"
    )


# ── 3. No known-heavy leaks (canary list) ──
_BANNED_HEAVY_FIELDS = frozenset({
    "identity_resolution", "statcast_batter", "statcast_pitcher",
    "external_id", "signal_engine", "sportsbook_mapping",
    "fusion", "snapshot", "brain", "evidence_breakdown",
    "v2_reasons", "key_insights", "top_reasons", "learning",
    "factors", "lock_components", "sim_alt_lines",
    "tennis_components", "player_intel", "calibration_band_warning",
    "marquee_reason", "deep_dive_warning", "historical_signal",
    "bandit_arms_matched", "ump_zone", "stuff_plus",
})


def test_slice_1_2b_no_heavy_field_leaks():
    tok = _tok()
    picks = _fetch("/api/picks/today?lite=true", tok).json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    leaks = {}
    for p in picks:
        for k in _BANNED_HEAVY_FIELDS & set(p.keys()):
            leaks.setdefault(k, 0)
            leaks[k] += 1
    assert not leaks, (
        f"NO_HEAVY_LEAKS breach: {leaks} — these heavy fields must live only on /api/picks/{{id}}"
    )


# ── 4. No publication / identity telemetry ──
_TELEMETRY_PATTERNS = (
    "publication_", "identity_", "provenance", "payload_hash",
    "idempotency_key", "canonical_wager_id", "canonical_event_id",
    "provider_", "no_vig_", "commence_time_utc",
    "convergence_", "calibration_version", "feature_snapshot_version",
    "fusion_version", "model_version", "simulation_version",
    "scoring_version", "validator_version", "model_evidence_",
    "signal_rank_computed_at",
)


def test_slice_1_2b_no_publication_or_identity_telemetry():
    tok = _tok()
    allowed = _load_whitelist()  # whitelist may explicitly grant a name
    picks = _fetch("/api/picks/today?lite=true", tok).json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    offenders = {}
    for p in picks:
        for k in p.keys():
            if k in allowed:
                continue
            if any(k.startswith(pat) or k == pat.rstrip("_")
                   for pat in _TELEMETRY_PATTERNS):
                offenders.setdefault(k, 0)
                offenders[k] += 1
    assert not offenders, (
        f"NO_PUBLICATION_TELEMETRY breach — debug halo leaking into wire: {offenders}"
    )


# ── 5. Board card required fields still survive ──
_BOARD_CRITICAL = ("id", "sport", "market", "selection", "lock_score",
                     "grade", "locks_eligibility")


def test_slice_1_2b_preserves_board_required_fields():
    tok = _tok()
    picks = _fetch("/api/picks/today?lite=true", tok).json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    for p in picks[:40]:
        for f in _BOARD_CRITICAL:
            assert f in p, (
                f"BOARD_CRITICAL_FIELD_MISSING: {f!r} on pick {p.get('id')}"
            )


# ── 6. Deep breakdown still returns the full document ──
def test_slice_1_2b_pick_detail_still_returns_full_document():
    tok = _tok()
    lite = _fetch("/api/picks/today?lite=true", tok).json().get("picks", [])
    if not lite:
        pytest.skip("no picks live")
    pid = lite[0].get("id")
    r = _fetch(f"/api/picks/{pid}", tok)
    detail = r.json()
    # Full breakdown must expose fields we stripped from the board DTO
    # (proves the Detail endpoint is genuinely the deep-dive source).
    expected_deep = {"evidence_breakdown", "sportsbook_mapping",
                      "signal_engine", "key_insights", "brain",
                      "top_reasons", "learning", "factors",
                      "lock_components"}
    present = expected_deep & set(detail.keys())
    # Not every deep field applies to every pick (sport-specific) so we
    # only assert AT LEAST ONE deep field survives on the detail path.
    assert present, (
        f"pick detail returned no deep fields — detail endpoint may have "
        f"been accidentally slimmed. keys={list(detail.keys())[:20]}"
    )
