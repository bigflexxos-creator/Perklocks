"""CSL ground-truth seed — Chinese Super League recent form + Golden
Boot history, provided directly by the user 2026-06-27.

Why this exists
---------------
TheSportsDB has CSL fixtures + final scores, but NO per-goal timeline
data for that league. SportDB.dev has the data but its free-plan
quota is exhausted. The `thesportsdb_scorer.compute_player_goal_rate`
function uses a generic position+jersey heuristic when timelines are
empty — that gets the high-volume strikers right (Cryzan, Júnior
Negrão) but misses HOT players that aren't priority-1 strikers
(Felipe Sousa, Fábio Abreu, Wu Lei, Leonardo).

This module ships HAND-CURATED ground-truth so those stars always land
on the board with their REAL recent-form numbers rather than a model
estimate. Update whenever the user pushes a new form snapshot.

Public API
----------
  get_player_form(player_name, team_hint=None) → dict | None

Returns:
  {
    "goals":         int,     # goals in the form window
    "matches":       int,     # apps in the form window
    "rate_per_match": float,  # goals / matches
    "source":        "user_seed",
    "season_goals":  int,     # OPTIONAL — career best from golden-boot table
  }
"""
from __future__ import annotations
import re
import unicodedata
from typing import Optional


def _norm(s: str) -> str:
    """Lowercase + remove diacritics + collapse spaces. Matches "Cádiz"
    against "Cadiz" and "Fábio" against "Fabio"."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


# ───────────────────────── Recent form (last 5 matches) ─────────────────────────
# User-supplied 2026-06-27. Player → (goals, apps, team).
_RECENT_FORM: list[dict] = [
    {"player": "Oscar Taty Maritu",   "team": "Yunnan Yukun",         "goals": 5, "apps": 5},
    {"player": "Rafael Ratão",        "team": "Shanghai Shenhua",     "goals": 5, "apps": 5},
    {"player": "Wellington Silva",    "team": "Chengdu Rongcheng",    "goals": 4, "apps": 5},
    {"player": "Felipe Sousa",        "team": "Chengdu Rongcheng",    "goals": 4, "apps": 3},
    {"player": "Zhang Yuning",        "team": "Beijing Guoan",        "goals": 4, "apps": 5},
    {"player": "Wesley",              "team": "Shenzhen Xinpengcheng", "goals": 4, "apps": 5},
    {"player": "Wang Yudong",         "team": "Zhejiang Professional", "goals": 3, "apps": 5},
    {"player": "Cephas Malele",       "team": "Dalian Yingbo",        "goals": 3, "apps": 5},
    {"player": "Frank Acheampong",    "team": "Dalian Yingbo",        "goals": 3, "apps": 5},
    {"player": "Eden Karzev",         "team": "Shenzhen Xinpengcheng", "goals": 3, "apps": 5},
    {"player": "Jhonder Cádiz",       "team": "Wuhan Three Towns",    "goals": 3, "apps": 5},
    {"player": "Filip Benković",      "team": "Shenzhen Xinpengcheng", "goals": 2, "apps": 5},
    {"player": "Fábio Abreu",         "team": "Beijing Guoan",        "goals": 2, "apps": 5},
    {"player": "Kilian Bevis",        "team": "Wuhan Three Towns",    "goals": 2, "apps": 5},
    {"player": "Guy Mbenza",          "team": "Liaoning Tieren",      "goals": 2, "apps": 5},
]

# ────────────────────── Golden Boot history (recent seasons) ──────────────────────
# Used for elite-tier override — golden-boot winners get a permanent
# lock-score floor of 95 regardless of recent form.
_GOLDEN_BOOTS: list[dict] = [
    {"season": 2020, "player": "Cédric Bakambu",  "team": "Beijing Guoan",                  "goals": 14},
    {"season": 2021, "player": "Júnior Negrão",   "team": "Changchun Yatai",                "goals": 14},
    {"season": 2022, "player": "Marcão",          "team": "Wuhan Three Towns",              "goals": 27},
    {"season": 2023, "player": "Leonardo",        "team": "Changchun Yatai / Zhejiang",     "goals": 19},
    {"season": 2024, "player": "Wu Lei",          "team": "Shanghai Port",                  "goals": 34},
    {"season": 2025, "player": "Fábio Abreu",     "team": "Beijing Guoan",                  "goals": 28},
]

# ───────────────────────── Aliases ─────────────────────────
# TheSportsDB lists Felipe Sousa as "Felipe Silva" — the player's real
# name is Felipe Sousa but his shirt name is Felipe Silva. Map known
# inconsistencies so a roster lookup by either name matches.
_PLAYER_ALIASES: dict[str, str] = {
    "felipe silva": "felipe sousa",
    "leonardo guilherme dietrich da silva felipe": "leonardo",
}


# ───────────────────────── Elite tier (lock-score floor) ─────────────────────────
# Hand-curated list of players whose REPUTATION + HISTORY warrants a
# permanent lock-score FLOOR regardless of what the goalscorer model
# computes. Golden Boot winners are auto-elite. The user-curated list
# below adds tier-1 stars who weren't (yet) Golden Boot winners but
# whose 1xBet/Pinnacle profile would always price them as a heavy
# favorite for anytime scorer.
#
#   tier 1 (floor 95) — Golden Boot winners + Cryzan
#   tier 2 (floor 90) — seeded high-form names (rate >= 0.8 g/m)
#
# These floors are applied in thesportsdb_scorer.compute_anytime_scorer_picks
# AFTER the Poisson lock-score is computed.
_TIER1_EXTRA: list[str] = [
    "Cryzan",            # Shandong Luneng — perennial top scorer
    "Wesley Moraes",     # Shenzhen Peng City — Premier League pedigree
    "Wu Lei",            # Shanghai Port — China's national-team captain
    "Leonardo Cittadini",# Shanghai Port — Brazilian, 2023 Boot context
]

_TIER1_INDEX: set[str] = {_norm(n) for n in _TIER1_EXTRA}
# All Golden Boot winners are tier 1
for _row in _GOLDEN_BOOTS:
    _TIER1_INDEX.add(_norm(_row["player"]))


def is_elite_player(player_name: str) -> Optional[int]:
    """Return a lock-score FLOOR for elite CSL players, or None.

    Resolution order:
      1. Tier-1 (Golden Boot winners + curated stars) → 95
      2. Seeded high-form (rate >= 0.8 g/m)           → 90
      3. Seeded any-form                              → 85
      4. Unknown                                       → None
    """
    norm = _norm(player_name)
    if not norm:
        return None
    # Tier 1
    if norm in _TIER1_INDEX:
        return 95
    # Alias-then-tier-1
    alias = _PLAYER_ALIASES.get(norm)
    if alias and alias in _TIER1_INDEX:
        return 95
    # Partial match (handles "Leonardo Cittadini" vs "Leonardo")
    for seed_norm in _TIER1_INDEX:
        if seed_norm in norm or norm in seed_norm:
            if min(len(seed_norm), len(norm)) >= 4:
                return 95
    # Form-based fallback
    form = get_player_form(player_name)
    if form:
        if form["rate_per_match"] >= 0.8:
            return 90
        return 85
    return None


# ───────────────────────── Team → seeded players (roster fallback) ─────────────────────────
# When TheSportsDB returns an EMPTY roster for a team (happens for
# Beijing FC, Yunnan Yukun, Wuhan Three Towns, Changchun Yatai —
# their v1 player endpoint is empty even with the paid key), inject
# the seeded players for that team as a synthetic roster so they
# still surface on the board.
def iter_team_seed_players(team_name: str) -> list[dict]:
    """Return a list of pseudo-roster entries (idPlayer, strPlayer,
    strPosition, strNumber) for all seeded players whose `team` field
    fuzzy-matches the given team name. Used as a roster fallback when
    the TheSportsDB roster endpoint returns empty.
    """
    tnorm = _norm(team_name)
    if not tnorm:
        return []
    out: list[dict] = []
    seen_names: set[str] = set()

    def _stable_id(player_name: str) -> str:
        """Deterministic pseudo-id so repeated calls produce the same
        `idPlayer` for the same player → downstream pick.id is stable
        → mongo unique-key dedupes correctly."""
        import hashlib
        h = hashlib.md5(_norm(player_name).encode("utf-8")).hexdigest()[:8]
        return f"seed-{h}"

    def _team_matches(seed_team: str) -> bool:
        s = _norm(seed_team)
        if not s:
            return False
        # Tolerate "Beijing Guoan" ↔ "Beijing FC", "Shanghai Shenhua FC"
        # ↔ "Shanghai Shenhua", etc.
        if s == tnorm:
            return True
        # Substring fast path (handles "Chengdu Rongcheng" ↔ "Chengdu Rongcheng FC")
        if s in tnorm or tnorm in s:
            return True
        # City-prefix match — CSL team names always start with the
        # city ("Beijing Guoan" vs "Beijing FC" → both start with
        # "beijing", same team). The first token is usually 4+ chars
        # so we won't false-match on "FC" etc.
        a_tokens = [t for t in s.split() if t not in {"fc", "the", "club"}]
        b_tokens = [t for t in tnorm.split() if t not in {"fc", "the", "club"}]
        if a_tokens and b_tokens and a_tokens[0] == b_tokens[0] and len(a_tokens[0]) >= 4:
            return True
        return False

    pseudo_id = 9_000_000
    for row in _RECENT_FORM:
        if _team_matches(row["team"]) and row["player"] not in seen_names:
            seen_names.add(row["player"])
            pseudo_id += 1
            out.append({
                "idPlayer":     _stable_id(row["player"]),
                "strPlayer":    row["player"],
                "strPosition":  "Centre-Forward",   # seed entries are strikers
                "strNumber":    "9",
                "strNationality": "",
                "strStatus":    "",
                "_seed_origin": True,
            })
    # Also include golden-boot winners for that team (e.g. Fábio Abreu
    # on Beijing Guoan, Wu Lei on Shanghai Port)
    for row in _GOLDEN_BOOTS:
        if _team_matches(row["team"]) and row["player"] not in seen_names:
            seen_names.add(row["player"])
            pseudo_id += 1
            out.append({
                "idPlayer":     _stable_id(row["player"]),
                "strPlayer":    row["player"],
                "strPosition":  "Centre-Forward",
                "strNumber":    "9",
                "strNationality": "",
                "strStatus":    "",
                "_seed_origin": True,
            })
    # Add Cryzan/Wesley Moraes/Leonardo Cittadini hand-mapped to their teams
    # (they're in _TIER1_EXTRA but not in _RECENT_FORM/_GOLDEN_BOOTS)
    _TIER1_TEAM_MAP = {
        "Cryzan":             "Shandong Luneng Taishan",
        "Wesley Moraes":      "Shenzhen Peng City",
        "Leonardo Cittadini": "Shanghai Port",
        # Wu Lei already on golden boot list for Shanghai Port
    }
    for nm, tm in _TIER1_TEAM_MAP.items():
        if _team_matches(tm) and nm not in seen_names:
            seen_names.add(nm)
            pseudo_id += 1
            out.append({
                "idPlayer":     _stable_id(nm),
                "strPlayer":    nm,
                "strPosition":  "Centre-Forward",
                "strNumber":    "9",
                "strNationality": "",
                "strStatus":    "",
                "_seed_origin": True,
            })
    return out


# Pre-build a fast lookup by normalized name.
_FORM_INDEX: dict[str, dict] = {}
for row in _RECENT_FORM:
    _FORM_INDEX[_norm(row["player"])] = row

_GOLDEN_BOOT_INDEX: dict[str, dict] = {}
for row in _GOLDEN_BOOTS:
    _GOLDEN_BOOT_INDEX[_norm(row["player"])] = row


def get_player_form(player_name: str, team_hint: Optional[str] = None) -> Optional[dict]:
    """Look up a CSL player in the recent-form seed. Returns None when
    no match. Matches against full name and any alias.

    Args:
      player_name: Display name from TheSportsDB roster (e.g. "Felipe Silva")
      team_hint:   Optional team display name — currently unused but kept
                   so we can later disambiguate two players with the same
                   last name on different teams.
    """
    norm = _norm(player_name)
    # Direct hit
    row = _FORM_INDEX.get(norm)
    if row:
        return _form_dict(row)
    # Alias hit (e.g. "felipe silva" → "felipe sousa")
    alias = _PLAYER_ALIASES.get(norm)
    if alias:
        row = _FORM_INDEX.get(alias)
        if row:
            return _form_dict(row)
    # Last-name partial match — only when the roster's name CONTAINS the
    # seed's full name. Avoids "Wu" matching "Wu Lei" but allows
    # "Cédric Bakambu Inonga" to match "Cédric Bakambu".
    for seed_norm, seed_row in _FORM_INDEX.items():
        if seed_norm in norm or norm in seed_norm:
            # Sanity: avoid trivially short matches
            if min(len(seed_norm), len(norm)) >= 4:
                return _form_dict(seed_row)
    return None


def get_golden_boot_season(player_name: str) -> Optional[dict]:
    """Returns golden-boot row {season, player, team, goals} or None."""
    norm = _norm(player_name)
    if norm in _GOLDEN_BOOT_INDEX:
        return _GOLDEN_BOOT_INDEX[norm]
    for seed_norm, row in _GOLDEN_BOOT_INDEX.items():
        if seed_norm in norm or norm in seed_norm:
            if min(len(seed_norm), len(norm)) >= 4:
                return row
    return None


def _form_dict(row: dict) -> dict:
    rate = row["goals"] / row["apps"] if row["apps"] else 0.0
    return {
        "goals":   row["goals"],
        "matches": row["apps"],
        "rate_per_match": round(rate, 3),
        "source":  "user_seed",
        "team":    row["team"],
    }
