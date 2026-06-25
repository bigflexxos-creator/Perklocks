"""Legacy adapter stubs — kept as a no-op shim for backwards-compat.

Phase 4 (2026-06-25) replaced these with real live adapters in:
  • sport_adapters/nba.py
  • sport_adapters/nfl.py
  • sport_adapters/cfb.py

Each of those imports `register()` at module load and self-registers,
so this file simply imports them so the registry is populated when
anything imports the package.
"""
from __future__ import annotations

# Import-side-effect registration: each module calls register() at the
# bottom. Order doesn't matter since the registry is keyed by SPORT.
from sport_adapters import nba   # noqa: F401
from sport_adapters import nfl   # noqa: F401
from sport_adapters import cfb   # noqa: F401
