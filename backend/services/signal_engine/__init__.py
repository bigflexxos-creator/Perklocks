"""PerksLocks Signal Engine — Phase A (universal signals).

Computes six independent betting signals per pick from data ALREADY on
the pick document + the historical game-log store, combines them into a
0-100 Signal Score, and rewrites "Why This Pick" from the actual
strongest signals.

Public API:
    compute_signals(db, pick)            — mutate one pick in place
    decorate_signals_bulk(db, picks)     — bulk variant + best-effort persist
"""
from .engine import SIGNAL_VERSION, compute_signals, decorate_signals_bulk

__all__ = ["SIGNAL_VERSION", "compute_signals", "decorate_signals_bulk"]
