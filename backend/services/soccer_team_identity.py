"""Soccer canonical team identity — Phase 2A.5B (2026-08).

DELTA CLOSURE for RC1 — universal, safe team identity for Soccer game
model context joins.

Contract
--------
* Return a canonical normalized team key derived from the input name.
* Handle diacritics, punctuation, spacing, FC/CF/AFC/SC noise, and a
  small curated alias table for provider-vs-DB naming differences.
* Never use unsafe broad fuzzy matching.
* If the input cannot be resolved to a canonical key, return
  ``None`` — callers MUST attribute this as ``IDENTITY_UNRESOLVED``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# ─────────────────────────────────────────────────────────────────────
# Alias table — provider display name variants that map to the same
# canonical team.  Left side is the raw name; right side is the
# canonical display name.  Additions are cheap; the table is
# case-insensitive and diacritic-insensitive because ``_norm``
# already strips both.
# ─────────────────────────────────────────────────────────────────────
_RAW_ALIASES: dict[str, str] = {
    # MLS
    "la galaxy": "los angeles galaxy",
    "lafc": "los angeles fc",
    "los angeles football club": "los angeles fc",
    "nyc fc": "new york city fc",
    "nycfc": "new york city fc",
    "ny red bulls": "new york red bulls",
    "sporting kc": "sporting kansas city",
    "atlanta united": "atlanta united fc",
    "cf montreal": "cf montreal",
    "montreal impact": "cf montreal",
    "houston dynamo": "houston dynamo fc",
    "portland timbers": "portland timbers",
    "vancouver whitecaps": "vancouver whitecaps fc",
    "cincinnati": "fc cincinnati",
    "philadelphia union": "philadelphia union",
    "columbus crew": "columbus crew",
    "orlando city": "orlando city sc",
    "st louis city sc": "st louis city",
    "san jose earthquakes": "san jose earthquakes",
    "d.c. united": "dc united",

    # European big clubs
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "newcastle": "newcastle united",
    "brighton": "brighton hove albion",
    "brighton & hove albion": "brighton hove albion",
    "nottingham forest": "nottingham forest",
    "athletic bilbao": "athletic club",
    "atletico madrid": "atletico madrid",
    "atlético madrid": "atletico madrid",
    "real betis": "real betis",
    "real sociedad": "real sociedad",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "milan": "ac milan",
    "juventus": "juventus",
    "napoli": "napoli",
    "bayern": "bayern munich",
    "bayern münchen": "bayern munich",
    "bayern muenchen": "bayern munich",
    "borussia dortmund": "borussia dortmund",
    "bvb": "borussia dortmund",
    "leverkusen": "bayer leverkusen",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint-germain": "paris saint germain",
    "psv": "psv eindhoven",
    "ajax": "ajax amsterdam",
    "feyenoord": "feyenoord rotterdam",
    "porto": "fc porto",
    "sporting": "sporting cp",
    "sporting lisbon": "sporting cp",
    "benfica": "sl benfica",
}


def _strip_diacritics(text: str) -> str:
    n = unicodedata.normalize("NFKD", text)
    return "".join(c for c in n if not unicodedata.combining(c))


def _norm(name: str) -> str:
    """Same normalisation contract as ``sportdb_client._norm``.

    Deliberately kept in sync — do not diverge without updating both.
    """
    if not name:
        return ""
    n = _strip_diacritics(name).lower()
    # Strip common corporate suffixes / prefixes (FC/CF/SC/AFC/CD/AC/SK/BK).
    n = re.sub(r"\b(fc|cf|sc|afc|cd|ac|sk|bk)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Alias table built on normalised keys → normalised canonical values.
_ALIASES: dict[str, str] = {
    _norm(k): _norm(v) for k, v in _RAW_ALIASES.items()
}


def canonical_team_key(name: str) -> Optional[str]:
    """Return a canonical normalised key for a team display name.

    * Returns ``None`` if input is empty.
    * Applies diacritic stripping, punctuation removal, and
      FC/CF/AFC/... noise stripping.
    * Applies a curated alias table for provider ↔ DB variants.
    * The returned key is intended for equality comparison ONLY —
      never for substring matching.
    """
    if not name or not isinstance(name, str):
        return None
    key = _norm(name)
    if not key:
        return None
    # Alias resolution — one hop only (aliases are already canonicalised).
    return _ALIASES.get(key, key)


def teams_equal(a: str, b: str) -> bool:
    """Safe equality — canonical key comparison, no substring match."""
    ka = canonical_team_key(a)
    kb = canonical_team_key(b)
    return ka is not None and kb is not None and ka == kb


__all__ = ["canonical_team_key", "teams_equal", "_norm"]
