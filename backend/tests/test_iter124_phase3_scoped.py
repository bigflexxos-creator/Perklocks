"""Phase 3 (approved scope) — slate_calendar + settings + script-move tests."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services import slate_calendar as SC
from services.settings import AppSettings, SettingsError, ENV_PRODUCTION


# ── slate_calendar ───────────────────────────────────────────────────
def test_slate_date_around_utc_midnight():
    # 03:00 UTC on Jan 15 = 22:00 ET on Jan 14 — slate is still Jan 14.
    dt = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
    assert SC.slate_date_str(dt) == "2026-01-14"
    # 08:00 UTC on Jan 15 = 03:00 ET on Jan 15 — slate is Jan 15.
    dt = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    assert SC.slate_date_str(dt) == "2026-01-15"


def test_dst_boundary_slate_date():
    # DST spring forward: Mar 8 2026 (US).  01:30 ET on Mar 8 exists;
    # 02:30 ET is skipped.  05:00 UTC on Mar 8 = 00:00 ET on Mar 8.
    dt = datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc)
    assert SC.slate_date_str(dt) == "2026-03-08"


def test_nfl_january_maps_to_prior_season():
    # Jan 2026 = 2025 NFL season.
    assert SC.nfl_season(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)) == 2025
    # Sep 2026 = 2026 NFL season.
    assert SC.nfl_season(datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)) == 2026


def test_nfl_regular_season_dates():
    for m, y_season in [(8, 2026), (9, 2026), (10, 2026), (11, 2026),
                         (12, 2026), (1, 2026), (2, 2026)]:
        year = 2026 if m >= 8 else 2027
        d = datetime(year, m, 15, 12, 0, tzinfo=timezone.utc)
        assert SC.nfl_season(d) == y_season, (m, y_season, SC.nfl_season(d))


def test_soccer_split_year_season():
    assert SC.soccer_split_season(datetime(2026, 8, 15, tzinfo=timezone.utc)) == "2026-27"
    assert SC.soccer_split_season(datetime(2026, 4, 15, tzinfo=timezone.utc)) == "2025-26"
    assert SC.soccer_split_season(datetime(2026, 7, 1, tzinfo=timezone.utc)) == "2026-27"


def test_mlb_nba_nhl_seasons():
    assert SC.mlb_season(datetime(2026, 7, 15, tzinfo=timezone.utc)) == 2026
    assert SC.nba_season(datetime(2026, 1, 5, tzinfo=timezone.utc)) == 2025
    assert SC.nba_season(datetime(2026, 11, 5, tzinfo=timezone.utc)) == 2026
    assert SC.nhl_season(datetime(2026, 10, 5, tzinfo=timezone.utc)) == 2026
    assert SC.nhl_season(datetime(2026, 6, 5, tzinfo=timezone.utc)) == 2025


def test_tomorrow_and_rollover_deterministic():
    dt = datetime(2026, 8, 6, 23, 59, tzinfo=timezone.utc)
    assert SC.tomorrow_utc(dt) == "2026-08-07"
    assert SC.board_date_utc(dt) == "2026-08-06"


# ── settings ─────────────────────────────────────────────────────────
def test_production_settings_reject_missing_required(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(SettingsError):
        AppSettings.load()


def test_production_settings_reject_localhost_mongo(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017")
    monkeypatch.setenv("DB_NAME", "prod")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    with pytest.raises(SettingsError):
        AppSettings.load()


def test_production_settings_reject_short_jwt(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGO_URL", "mongodb://prod-cluster:27017")
    monkeypatch.setenv("DB_NAME", "prod")
    monkeypatch.setenv("JWT_SECRET", "shortsecret")
    with pytest.raises(SettingsError):
        AppSettings.load()


def test_development_settings_warn_but_do_not_crash(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("MONGO_URL", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    s = AppSettings.load()
    assert s.environment == "development"
    assert s.warnings, "dev must produce warnings for missing vars"


def test_safe_diagnostics_never_includes_secret_values(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JWT_SECRET", "supersecret-value-that-should-not-leak-anywhere")
    monkeypatch.setenv("MONGO_URL", "mongodb://prod-cluster:27017")
    monkeypatch.setenv("DB_NAME", "prod")
    s = AppSettings.load()
    diag = s.safe_diagnostics()
    dumped = repr(diag)
    assert "supersecret-value" not in dumped
    assert "mongodb://prod-cluster" not in dumped
    assert diag["jwt_secret_present"] is True
    assert diag["mongo_url_present"] is True


# ── Script move safety ──────────────────────────────────────────────
def test_operational_scripts_moved_out_of_tests():
    tests_dir = Path("/app/backend/tests")
    for name in ("analyze_hits.py", "analyze_hits_missing.py",
                  "analyze_history.py", "analyze_hrrbi.py",
                  "backfill_mlb.py", "cleanup_and_settle.py",
                  "diagnose_unsettled.py", "drain_mlb.py", "trace_settle.py"):
        assert not (tests_dir / name).exists(), (
            f"{name} still in tests/ — must be moved to scripts/*/"
        )


def test_moved_scripts_exist_in_new_locations():
    for path in (
        "/app/backend/scripts/diagnostics/analyze_hits.py",
        "/app/backend/scripts/diagnostics/analyze_history.py",
        "/app/backend/scripts/diagnostics/backfill_mlb.py",
        "/app/backend/scripts/maintenance/cleanup_and_settle.py",
        "/app/backend/scripts/maintenance/diagnose_unsettled.py",
        "/app/backend/scripts/maintenance/drain_mlb.py",
        "/app/backend/scripts/maintenance/trace_settle.py",
    ):
        assert Path(path).exists(), path
