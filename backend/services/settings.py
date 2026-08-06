"""settings — Phase 3I typed application settings.

Environment-aware validation.  Distinguishes production vs preview
vs development vs test.  Fails loudly for production misconfiguration
and safely for lower environments.

Production detection
────────────────────
``ENVIRONMENT`` env var (values: ``production``, ``preview``,
``development``, ``test``).  If unset, falls back to
``development``.  When set to ``production`` the required variables
list is enforced.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("lockscore.settings")

ENV_PRODUCTION  = "production"
ENV_PREVIEW     = "preview"
ENV_DEVELOPMENT = "development"
ENV_TEST        = "test"

_REQUIRED_PROD  = ("MONGO_URL", "DB_NAME", "JWT_SECRET")
_UNSAFE_SUBSTR  = ("localhost", "127.0.0.1")


class SettingsError(RuntimeError):
    pass


@dataclass
class AppSettings:
    environment:      str = ENV_DEVELOPMENT
    mongo_url:        Optional[str] = None
    db_name:          Optional[str] = None
    jwt_secret:       Optional[str] = None
    # Non-secret feature flags exposed to admin diagnostics.
    gateway_enabled:  bool = True
    refresh_mode:     str  = "snapshot"
    daily_credit_limit:   int = 3000
    monthly_credit_limit: int = 100_000
    emergency_reserve:    int = 10_000
    warnings:         list[str] = field(default_factory=list)

    # ── Loaders ─────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "AppSettings":
        env = (os.environ.get("ENVIRONMENT", "")
                .strip().lower() or ENV_DEVELOPMENT)
        if env not in (ENV_PRODUCTION, ENV_PREVIEW,
                        ENV_DEVELOPMENT, ENV_TEST):
            env = ENV_DEVELOPMENT
        s = cls(
            environment=env,
            mongo_url=(os.environ.get("MONGO_URL") or None),
            db_name=(os.environ.get("DB_NAME") or None),
            jwt_secret=(os.environ.get("JWT_SECRET") or None),
            gateway_enabled=(
                os.environ.get("ODDS_GATEWAY_ENABLED", "true").lower()
                in ("", "1", "true", "yes", "on")
            ),
            refresh_mode=(
                os.environ.get("ODDS_GLOBAL_REFRESH_MODE", "snapshot")
            ),
            daily_credit_limit=int(
                os.environ.get("ODDS_DAILY_CREDIT_LIMIT") or 3000),
            monthly_credit_limit=int(
                os.environ.get("ODDS_MONTHLY_CREDIT_LIMIT") or 100_000),
            emergency_reserve=int(
                os.environ.get("ODDS_EMERGENCY_RESERVE") or 10_000),
        )
        s._validate()
        return s

    # ── Validation ──────────────────────────────────────────────────
    def _validate(self) -> None:
        if self.environment == ENV_PRODUCTION:
            missing = [k for k in _REQUIRED_PROD
                        if not (os.environ.get(k) or "").strip()]
            if missing:
                raise SettingsError(
                    f"Production environment missing required variables: "
                    f"{','.join(missing)}"
                )
            for lit in _UNSAFE_SUBSTR:
                if self.mongo_url and lit in self.mongo_url.lower():
                    raise SettingsError(
                        f"Production MONGO_URL points at {lit} — refusing"
                    )
            if self.jwt_secret and len(self.jwt_secret) < 32:
                raise SettingsError(
                    "Production JWT_SECRET too short (<32 chars)"
                )
        else:
            # Non-production: warn loudly but do not crash.
            if not self.mongo_url:
                self.warnings.append("MONGO_URL missing (development default)")
            if not self.db_name:
                self.warnings.append("DB_NAME missing (development default)")
            if not self.jwt_secret:
                self.warnings.append("JWT_SECRET missing (ephemeral secret in use)")

    # ── Diagnostics (safe — no secrets) ─────────────────────────────
    def safe_diagnostics(self) -> dict:
        """Return an admin-safe view of settings.  NEVER includes
        secret values — only booleans/lengths/environment flags."""
        return {
            "environment":            self.environment,
            "mongo_url_present":      bool(self.mongo_url),
            "mongo_url_safe":         (
                self.mongo_url is not None
                and not any(x in (self.mongo_url or "").lower()
                             for x in _UNSAFE_SUBSTR)
            ),
            "db_name":                self.db_name,   # not secret
            "jwt_secret_present":     bool(self.jwt_secret),
            "jwt_secret_length":      len(self.jwt_secret or ""),
            "gateway_enabled":        self.gateway_enabled,
            "refresh_mode":           self.refresh_mode,
            "daily_credit_limit":     self.daily_credit_limit,
            "monthly_credit_limit":   self.monthly_credit_limit,
            "emergency_reserve":      self.emergency_reserve,
            "warnings":               list(self.warnings),
        }


__all__ = [
    "AppSettings", "SettingsError",
    "ENV_PRODUCTION", "ENV_PREVIEW", "ENV_DEVELOPMENT", "ENV_TEST",
]
