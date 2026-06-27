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
  get_golden_boot_season(player_name) → dict | None
  is_elite_player(player_name) → int (95 | 90 | 85) | None
  iter_team_seed_players(team_name) → list[dict]    (pseudo-roster)
  is_player_blocked_on_team(player_name, team_name) → bool
"""
from __future__ import annotations
import hashlib
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
_GOLDEN_BOOTS: list[dict] = [
    {"season": 2020, "player": "Cédric Bakambu",  "team": "Beijing Guoan",      "goals": 14},
    {"season": 2021, "player": "Júnior Negrão",   "team": "Changchun Yatai",    "goals": 14},
    {"season": 2022, "player": "Marcão",          "team": "Wuhan Three Towns",  "goals": 27},
    # 2026 transfer: Leonardo moved Zhejiang → Shanghai Port for the
    # 2026 season per user feedback 2026-06-27.
    {"season": 2023, "player": "Leonardo",        "team": "Shanghai Port",      "goals": 19},
    {"season": 2024, "player": "Wu Lei",          "team": "Shanghai Port",      "goals": 34},
    {"season": 2025, "player": "Fábio Abreu",     "team": "Beijing Guoan",      "goals": 28},
]

# ───────────────────────── Aliases ─────────────────────────
# TheSportsDB lists Felipe Sousa as "Felipe Silva" — the player's real
# name is Felipe Sousa but his shirt name is Felipe Silva.
_PLAYER_ALIASES: dict[str, str] = {
    "felipe silva": "felipe sousa",
    "leonardo guilherme dietrich da silva felipe": "leonardo",
    # Wesley (CSL seed) ≡ Wesley Moraes (Tier-1 list). Same Brazilian
    # striker — TheSportsDB roster lists just "Wesley", Pinnacle uses
    # the full "Wesley Moraes".
    "wesley": "wesley moraes",
}

# ───────────────────────── Elite tier (lock-score floor) ─────────────────────────
# Hand-curated list of players whose REPUTATION + HISTORY warrants a
# permanent lock-score FLOOR.
#   tier 1 (floor 95) — Golden Boot winners + Cryzan
#   tier 2 (floor 90) — seeded high-form names (rate >= 0.8 g/m)
#   tier 3 (floor 85) — seeded any-form
_TIER1_EXTRA: list[str] = [
    "Cryzan",            # Shandong Luneng — perennial top scorer
    "Wesley Moraes",     # Shenzhen Peng City — Premier League pedigree
    "Wu Lei",            # Shanghai Port — China's national-team captain
    "Leonardo Cittadini",# Shanghai Shenhua — Brazilian midfielder
]

_TIER1_INDEX: set[str] = {_norm(n) for n in _TIER1_EXTRA}
for _row in _GOLDEN_BOOTS:
    _TIER1_INDEX.add(_norm(_row["player"]))


# ───────────────────────── CSL Team-Name Canonical Aliases ─────────────────
# CSL teams are routinely referenced under 2-3 different names (e.g.
# Shanghai SIPG ≡ Shanghai Port, Beijing Guoan ≡ Beijing FC). Map every
# alias to one canonical name so team-equality is exact and we don't
# conflate Shanghai SIPG with Shanghai Shenhua.
_TEAM_ALIASES: dict[str, str] = {
    # Shanghai
    "shanghai sipg":              "shanghai port",
    "shanghai sipg fc":           "shanghai port",
    "shanghai port":              "shanghai port",
    "shanghai port fc":           "shanghai port",
    "shanghai shenhua":           "shanghai shenhua",
    "shanghai shenhua fc":        "shanghai shenhua",
    # Beijing
    "beijing fc":                 "beijing guoan",
    "beijing guoan":              "beijing guoan",
    # Chengdu
    "chengdu rongcheng":          "chengdu rongcheng",
    "chengdu rongcheng fc":       "chengdu rongcheng",
    # Shenzhen — Peng City ≡ Xinpengcheng
    "shenzhen peng city":         "shenzhen peng city",
    "shenzhen peng city fc":      "shenzhen peng city",
    "shenzhen xinpengcheng":      "shenzhen peng city",
    # Wuhan
    "wuhan three towns":          "wuhan three towns",
    "wuhan three towns fc":       "wuhan three towns",
    # Shandong
    "shandong luneng":            "shandong luneng taishan",
    "shandong luneng taishan":    "shandong luneng taishan",
    "shandong luneng taishan fc": "shandong luneng taishan",
    "shandong taishan":           "shandong luneng taishan",
    # Tianjin
    "tianjin jinmen tiger":       "tianjin jinmen tiger",
    "tianjin jinmen tiger fc":    "tianjin jinmen tiger",
    "tianjin teda":               "tianjin jinmen tiger",
    # Dalian
    "dalian yingbo":              "dalian yingbo",
    "dalian yingbo fc":           "dalian yingbo",
    # Henan
    "henan fc":                   "henan fc",
    "henan songshan longmen":     "henan fc",
    # Qingdao
    "qingdao west coast":         "qingdao west coast",
    "qingdao west coast fc":      "qingdao west coast",
    # Liaoning
    "liaoning tieren":            "liaoning tieren",
    "liaoning tieren fc":         "liaoning tieren",
    # Chongqing
    "chongqing tonglianglong":    "chongqing tonglianglong",
    "chongqing tonglianglong fc": "chongqing tonglianglong",
    # Yunnan
    "yunnan yukun":               "yunnan yukun",
    "yunnan yukun fc":            "yunnan yukun",
    # Changchun
    "changchun yatai":            "changchun yatai",
    # Zhejiang
    "zhejiang":                   "zhejiang",
    "zhejiang fc":                "zhejiang",
    "zhejiang professional":      "zhejiang",
}


def _canon_team(name: str) -> str:
    """Normalize a CSL team name to its canonical form (lower-cased)."""
    n = _norm(name)
    if not n:
        return ""
    return _TEAM_ALIASES.get(n, n)


# Players whose TheSportsDB roster is OUT OF DATE due to a transfer —
# block them from the OLD team's synth output. Map: stale_team_canonical
# → {player_norm_names}. Updated from user feedback.
_BLOCKED_PLAYER_BY_TEAM: dict[str, set[str]] = {
    # Leonardo moved Zhejiang → Shanghai Port for the 2026 season but
    # TheSportsDB v1 still lists him on Zhejiang's roster.
    "zhejiang": {"leonardo", "leonardo nascimento lopes de souza"},
}


def is_player_blocked_on_team(player_name: str, team_name: str) -> bool:
    """Return True if `player_name` has been transferred away from
    `team_name` and should be suppressed from the synthesizer's output."""
    team_c = _canon_team(team_name)
    if not team_c:
        return False
    blocked = _BLOCKED_PLAYER_BY_TEAM.get(team_c) or set()
    if not blocked:
        return False
    p_norm = _norm(player_name)
    if p_norm in blocked:
        return True
    # Partial match for very long full names
    for b in blocked:
        if b and (b in p_norm or p_norm in b) and len(b) >= 6:
            return True
    return False


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
    if norm in _TIER1_INDEX:
        return 95
    alias = _PLAYER_ALIASES.get(norm)
    if alias and alias in _TIER1_INDEX:
        return 95
    for seed_norm in _TIER1_INDEX:
        if seed_norm in norm or norm in seed_norm:
            if min(len(seed_norm), len(norm)) >= 4:
                return 95
    form = get_player_form(player_name)
    if form:
        if form["rate_per_match"] >= 0.8:
            return 90
        return 85
    return None


def _stable_id(player_name: str) -> str:
    """Deterministic pseudo-id so repeated calls produce the same
    `idPlayer` for the same player → downstream pick.id is stable
    → mongo unique-key dedupes correctly."""
    h = hashlib.md5(_norm(player_name).encode("utf-8")).hexdigest()[:8]
    return f"seed-{h}"


# ───────────────────────── Team → seeded players (roster fallback) ─────────────────────────
def iter_team_seed_players(team_name: str) -> list[dict]:
    """Return a list of pseudo-roster entries for all seeded players
    whose canonical team matches `team_name`. Used both as roster fallback
    when TheSportsDB returns empty AND as a merge layer to inject
    transfer-affected stars (e.g. Leonardo moving to Shanghai Port) onto
    the new team's roster."""
    target = _canon_team(team_name)
    if not target:
        return []

    out: list[dict] = []
    seen_names: set[str] = set()

    def _team_matches(seed_team: str) -> bool:
        return _canon_team(seed_team) == target

    for row in _RECENT_FORM:
        if _team_matches(row["team"]) and row["player"] not in seen_names:
            seen_names.add(row["player"])
            out.append({
                "idPlayer":       _stable_id(row["player"]),
                "strPlayer":      row["player"],
                "strPosition":    "Centre-Forward",
                "strNumber":      "9",
                "strNationality": "",
                "strStatus":      "",
                "_seed_origin":   True,
            })
    for row in _GOLDEN_BOOTS:
        if _team_matches(row["team"]) and row["player"] not in seen_names:
            seen_names.add(row["player"])
            out.append({
                "idPlayer":       _stable_id(row["player"]),
                "strPlayer":      row["player"],
                "strPosition":    "Centre-Forward",
                "strNumber":      "9",
                "strNationality": "",
                "strStatus":      "",
                "_seed_origin":   True,
            })
    # Tier-1 hand-mapped stars (not in form/golden-boot tables).
    _TIER1_TEAM_MAP = {
        "Cryzan":             "Shandong Luneng Taishan",
        "Wesley Moraes":      "Shenzhen Peng City",
        "Leonardo Cittadini": "Shanghai Shenhua",
        # Wu Lei already on golden-boot list for Shanghai Port.
    }
    for nm, tm in _TIER1_TEAM_MAP.items():
        if _team_matches(tm) and nm not in seen_names:
            seen_names.add(nm)
            out.append({
                "idPlayer":       _stable_id(nm),
                "strPlayer":      nm,
                "strPosition":    "Centre-Forward",
                "strNumber":      "9",
                "strNationality": "",
                "strStatus":      "",
                "_seed_origin":   True,
            })
    return out


# ───────────────────────── Fast lookup indices ─────────────────────────
_FORM_INDEX: dict[str, dict] = {}
for row in _RECENT_FORM:
    _FORM_INDEX[_norm(row["player"])] = row

_GOLDEN_BOOT_INDEX: dict[str, dict] = {}
for row in _GOLDEN_BOOTS:
    _GOLDEN_BOOT_INDEX[_norm(row["player"])] = row


def get_player_form(player_name: str, team_hint: Optional[str] = None) -> Optional[dict]:
    """Look up a CSL player in the recent-form seed. Returns None when
    no match."""
    norm = _norm(player_name)
    row = _FORM_INDEX.get(norm)
    if row:
        return _form_dict(row)
    alias = _PLAYER_ALIASES.get(norm)
    if alias:
        row = _FORM_INDEX.get(alias)
        if row:
            return _form_dict(row)
    for seed_norm, seed_row in _FORM_INDEX.items():
        if seed_norm in norm or norm in seed_norm:
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
