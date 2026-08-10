"""MLS goal-scorer quality gate.

Purpose (user report 2026-07-22):
   > "Malachi Jones shouldn't be on board — he's injured and he sucks.
   >  Why are you missing better players like Mercau? Play on same team
   >  and he more likely to get a score or assist."

Root cause discovered:
   • `soccer_player_form` MongoDB collection has 2774 docs but covers ONLY
     the Big 5 European leagues (EPL/La Liga/Serie A/Bundesliga/Ligue 1).
     MLS coverage = 0 players.
   • `wiki_top_scorers` also had 0 MLS docs.
   • Result: elite-player detection for MLS goal-scorer / SoA markets
     fell back to a stale hardcoded list which was missing regulars
     like Nicolas Mercau (NYC FC), Cavan Sullivan (Philly), and
     over-including reserves surfaced by book pricing.

Fix strategy:
   • Hardcode the ESPN MLS 2025 Top Scorers leaderboard + supplemental
     regulars the user identified as an authoritative whitelist.
   • For MLS Anytime Goal Scorer / To Score or Assist / First Goal
     Scorer picks — apply a HARD GATE via `is_mls_scorer_pick_ok()`:
       1. Player is in the season leaderboard (≥5 season goals), OR
       2. Player is a KNOWN regular starter (curated list), OR
       3. Player is the market favorite (implied ≥ 0.32 — very short
          odds means the whole book agrees they're starting + hot).
     Otherwise → REJECT.

   • This kills reserves like Malachi Jones, Chase Adams, Seymour Reid
     unless the whole book prices them as clear favorites (which they
     won't be if they're reserves).

Data source: ESPN MLS Scoring leaderboard (2025 season screenshot
provided by user 2026-07-22). Refreshed manually until we wire the
ESPN scraper. Names use the exact ESPN spelling AND common variants
so name-normalisation matching survives accent stripping.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, Optional


# ─────────── Runtime cache populated from Mongo `espn_mls_stats` ───────────
# `_espn_index[name_norm] = {goals, assists, games, team}`
# `_espn_names` is a set() of normalised names for O(1) membership.
# Refreshed by services/espn_mls_stats.refresh_mls_leaders() (called from
# server startup + daily loop). Falls back to hardcoded lists below when
# empty (e.g. first boot before scrape runs).
_espn_index: dict[str, dict] = {}
_espn_names: set[str] = set()


def apply_espn_snapshot(by_name_norm: dict[str, dict],
                        names: set[str]) -> None:
    """Called by refresher after every ESPN scrape.

    Phase 2 Final (2026-08-11): also expose the snapshot under
    `_espn_by_name` (the name callers use) and PROPAGATE every
    observation into the canonical `player_identity` registry so the
    publication barrier's freshness gate has real, current data.
    """
    global _espn_index, _espn_names, _espn_by_name
    _espn_index = by_name_norm
    _espn_names = names
    _espn_by_name = by_name_norm    # alias for downstream consumers

    # Propagate to canonical player_identity registry.
    try:
        from datetime import datetime, timezone
        from services.player_identity import upsert_player
        now_iso = datetime.now(timezone.utc).isoformat()
        for norm_name, entry in (by_name_norm or {}).items():
            if not isinstance(entry, dict):
                continue
            display = entry.get("display_name") or entry.get("name") or norm_name
            team = entry.get("team")
            if not team:
                continue
            upsert_player(
                name=display, sport="Soccer", league="MLS",
                provider="espn",
                provider_id=str(entry.get("espn_id") or f"norm:{norm_name}"),
                current_team=team,
                position=entry.get("position"),
                role=entry.get("role"),
                roster_status="active",
                source="espn_mls_leaders",
                observed_at=now_iso,
            )
    except Exception:
        # Never let identity propagation break the gate itself.
        pass


# Late-bound alias for callers that read `_espn_by_name` directly.
_espn_by_name: dict[str, dict] = {}


def _norm(name: str) -> str:
    """Accent-strip + lowercase for robust matching."""
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


# ESPN MLS 2025 Top Scorers (regular season) — every player with ≥13 goals.
# (name, team, season_goals, season_assists_estimate)
MLS_TOP_SCORERS_2025: list[tuple[str, str, int, int]] = [
    ("Lionel Messi",              "Inter Miami CF",       29, 20),
    ("Sam Surridge",              "Nashville SC",         24, 3),
    ("Denis Bouanga",             "LAFC",                 24, 6),
    ("Anders Dreyer",             "San Diego FC",         19, 17),
    ("Dejan Joveljic",            "Sporting Kansas City", 18, 5),
    ("Evander",                   "FC Cincinnati",        18, 15),
    ("Petar Musa",                "FC Dallas",            18, 4),
    ("Hugo Cuypers",              "Chicago Fire FC",      17, 5),
    ("Eric Maxim Choupo-Moting",  "New York Red Bulls",   17, 3),
    ("Alonso Martínez",           "New York City FC",     17, 4),
    ("Hany Mukhtar",              "Nashville SC",         16, 8),
    ("Martín Ojeda",              "Orlando City SC",      16, 8),
    ("Diego Rossi",               "Columbus Crew SC",     16, 7),
    ("Tai Baribo",                "Philadelphia Union",   16, 3),
    ("Brian White",               "Vancouver Whitecaps",  16, 3),
    ("Philip Zinckernagel",       "Chicago Fire FC",      15, 8),
    ("Kévin Denkey",              "FC Cincinnati",        15, 3),
    ("Daniel Musovski",           "Seattle Sounders FC",  14, 3),
    ("Josef Martínez",            "San Jose Earthquakes", 14, 2),
    ("Djordje Mihailovic",        "Toronto FC",           13, 9),
    ("Prince Owusu",              "CF Montreal",          13, 4),
    ("Cristian Arango",           "San Jose Earthquakes", 13, 4),
]


# Known regular starters + creative-mid / SoA candidates the user flagged
# as legit that are BELOW the 13-goal cutoff but still deserve to pass.
# Format: (name, team). If odds book prices them as scorer, we let it
# through the gate.
MLS_REGULAR_STARTERS_2025: list[tuple[str, str]] = [
    # NYC FC — user-flagged 2026-07-22 (Mercau)
    ("Nicolas Mercau",          "New York City FC"),
    ("Talles Magno",            "New York City FC"),
    ("Hannes Wolf",             "New York City FC"),
    ("Máximo Carrizo",          "New York City FC"),
    ("Julián Fernández",        "New York City FC"),
    ("Agustín Ojeda",           "New York City FC"),
    # Columbus Crew regulars
    ("Max Arfsten",             "Columbus Crew SC"),
    ("Daniel Gazdag",           "Columbus Crew SC"),
    ("Dylan Chambost",          "Columbus Crew SC"),
    ("Wessam Abou Ali",         "Columbus Crew SC"),
    ("Steven Moreira",          "Columbus Crew SC"),
    ("Andrés Perea",            "Columbus Crew SC"),
    ("Taha Habroune",           "Columbus Crew SC"),
    # Philadelphia — Cavan Sullivan
    ("Cavan Sullivan",          "Philadelphia Union"),
    ("Bruno Damiani",           "Philadelphia Union"),
    ("Mikael Uhre",             "Philadelphia Union"),
    ("Dániel Gazdag",           "Philadelphia Union"),
    # LAFC support cast
    ("Cengiz Ünder",            "LAFC"),
    ("Olivier Giroud",          "LAFC"),
    ("Nathan Ordaz",            "LAFC"),
    ("Mateusz Bogusz",          "LAFC"),
    # LA Galaxy
    ("Joseph Paintsil",         "LA Galaxy"),
    ("Marco Reus",              "LA Galaxy"),
    ("Gabriel Pec",             "LA Galaxy"),
    ("Christian Ramírez",       "LA Galaxy"),
    # Miami support
    ("Luis Suárez",             "Inter Miami CF"),
    ("Sergio Busquets",         "Inter Miami CF"),
    ("Rodrigo De Paul",         "Inter Miami CF"),
    ("Jordi Alba",              "Inter Miami CF"),
    ("Fafà Picault",            "Inter Miami CF"),
    ("Tadeo Allende",           "Inter Miami CF"),
    ("Baltasar Rodríguez",      "Inter Miami CF"),
    # Chicago
    ("Chris Brady",             "Chicago Fire FC"),
    ("Rominigue Kouame",        "Chicago Fire FC"),
    ("Jonathan Bamba",          "Chicago Fire FC"),
    # Cincinnati support cast
    ("Luca Orellano",           "FC Cincinnati"),
    ("Sergio Santos",           "FC Cincinnati"),
    ("Pavel Bucha",             "FC Cincinnati"),
    # Charlotte
    ("Wilfried Zaha",           "Charlotte FC"),
    ("Idan Toklomati",          "Charlotte FC"),
    ("Patrick Agyemang",        "Charlotte FC"),
    ("Djibril Diani",           "Charlotte FC"),
    ("Pep Biel",                "Charlotte FC"),
    # Atlanta
    ("Emmanuel Latte Lath",     "Atlanta United FC"),
    ("Alexey Miranchuk",        "Atlanta United FC"),
    ("Bartosz Slisz",           "Atlanta United FC"),
    # Sporting KC / Minnesota
    ("Erik Thommy",             "Sporting Kansas City"),
    ("Manu García",             "Sporting Kansas City"),
    ("Bongokuhle Hlongwane",    "Sporting Kansas City"),
    ("Kelvin Yeboah",           "Minnesota United FC"),
    ("Robin Lod",               "Minnesota United FC"),
    ("Tani Oluwaseyi",          "Minnesota United FC"),
    ("Joaquín Pereyra",         "Minnesota United FC"),
    # Nashville
    ("Josh Bauer",              "Nashville SC"),
    ("Alex Muyl",               "Nashville SC"),
    ("Jonathan Pérez",          "Nashville SC"),
    # Montreal
    ("Josef Martínez",          "CF Montreal"),
    ("Caden Clark",             "CF Montreal"),
    # Houston Dynamo
    ("Ezequiel Ponce",          "Houston Dynamo FC"),
    ("Ibrahim Aliyu",           "Houston Dynamo FC"),
    ("Ondřej Lingr",            "Houston Dynamo FC"),
    ("Sebastián Kowalczyk",     "Houston Dynamo FC"),
    ("Guilherme dos Anjos",     "Houston Dynamo FC"),
    ("Ariel Lassiter",          "Houston Dynamo FC"),
    # DC United
    ("Christian Benteke",       "D.C. United"),
    ("Ted Ku-DiPietro",         "D.C. United"),
    ("Jared Stroud",             "D.C. United"),
    ("João Peglow",             "D.C. United"),
    ("Gabriel Pirani",          "D.C. United"),
    # Austin FC
    ("Myrto Uzuni",             "Austin FC"),
    ("Brandon Vazquez",         "Austin FC"),
    ("Jáder Obrian",            "Austin FC"),
    ("Osman Bukari",            "Austin FC"),
    ("Owen Wolff",              "Austin FC"),
    ("Diego Rubio",             "Austin FC"),
    # Seattle
    ("Jordan Morris",           "Seattle Sounders FC"),
    ("Albert Rusnák",           "Seattle Sounders FC"),
    ("Cristian Roldan",         "Seattle Sounders FC"),
    ("Ryan Kent",               "Seattle Sounders FC"),
    ("Danny Musovski",          "Seattle Sounders FC"),  # variant
    # Colorado / San Diego
    ("Rasmus Thelin",           "Colorado Rapids"),
    ("Rafael Navarro",          "Colorado Rapids"),
    ("Djordje Mihailovic",      "Colorado Rapids"),      # rumored trade
    ("Kevin Cabral",            "Colorado Rapids"),
    ("Diego Rubio",             "Colorado Rapids"),
    ("Anders Dreyer",           "San Diego FC"),         # already elite
    ("Chucky Lozano",           "San Diego FC"),
    ("Christian Guzmán",        "San Diego FC"),
    # Portland / Dallas
    ("Antony",                  "Portland Timbers"),
    ("Felipe Mora",             "Portland Timbers"),
    ("Kevin Kelsy",             "Portland Timbers"),
    ("Jonathan Rodríguez",      "Portland Timbers"),
    ("David Da Costa",          "Portland Timbers"),
    ("Petar Musa",              "FC Dallas"),            # elite
    ("Sebastien Ibeagha",       "FC Dallas"),
    ("Anderson Julio",          "FC Dallas"),
    ("Logan Farrington",        "FC Dallas"),
    ("Bernard Kamungo",         "FC Dallas"),
    ("Luciano Acosta",          "FC Dallas"),
    # Real Salt Lake / San Jose
    ("Diego Luna",              "Real Salt Lake"),
    ("Chicho Arango",           "Real Salt Lake"),  # variant of Cristian Arango
    ("William Agada",           "Real Salt Lake"),
    ("Braian Ojeda",            "Real Salt Lake"),
    ("Josef Martinez",          "San Jose Earthquakes"),  # ascii
    ("Cristian Espinoza",       "San Jose Earthquakes"),
    ("Preston Judd",            "San Jose Earthquakes"),
    ("Ousseni Bouda",           "San Jose Earthquakes"),
    ("Beau Leroux",             "San Jose Earthquakes"),
    # Orlando
    ("Marco Pašalić",           "Orlando City SC"),
    ("Facundo Torres",          "Orlando City SC"),
    ("Duncan McGuire",          "Orlando City SC"),
    ("Ramiro Enrique",          "Orlando City SC"),
    # Toronto
    ("Federico Bernardeschi",   "Toronto FC"),
    ("Deybi Flores",             "Toronto FC"),
    ("Prince Owusu",            "Toronto FC"),           # traded scenarios
    # NY Red Bulls
    ("Emil Forsberg",           "New York Red Bulls"),
    ("Elias Manoel",            "New York Red Bulls"),
    ("Peter Stroud",            "New York Red Bulls"),
    # Vancouver
    ("Ryan Gauld",              "Vancouver Whitecaps"),
    ("Thomas Müller",           "Vancouver Whitecaps"),
    # Saint Louis
    ("Cedric Teuchert",         "St. Louis City SC"),
    ("Célio Pompeu",            "St. Louis City SC"),
    ("Simon Becher",            "St. Louis City SC"),
]


# Build normalized lookup sets. Kept module-level so lookups are O(1).
_TOP_SCORER_INDEX: dict[str, tuple[str, int, int]] = {
    _norm(n): (team, g, a) for n, team, g, a in MLS_TOP_SCORERS_2025
}
_STARTER_INDEX: set[str] = {_norm(n) for n, _t in MLS_REGULAR_STARTERS_2025}


def is_mls_scorer_pick_ok(player_name: str, implied_prob: float) -> tuple[bool, str]:
    """Hard-gate for MLS Anytime Goal Scorer / SoA / First Goal Scorer.

    Returns (allowed, reason) so callers can log rejection reasons.
    Order of checks:
      1. Live ESPN season leaderboard (from Mongo, refreshed nightly).
      2. Hardcoded 2025 top-scorer fallback.
      3. Hardcoded regular-starter whitelist.
      4. Market-favorite escape (implied ≥ 0.32).
    """
    if not player_name:
        return False, "empty player name"
    n = _norm(player_name)
    # (1) Live ESPN data — preferred source.
    if n in _espn_names:
        rec = _espn_index[n]
        g, a = rec.get("goals", 0), rec.get("assists", 0)
        return True, f"espn_leader_{g}g_{a}a"
    if n in _TOP_SCORER_INDEX:
        _team, g, _a = _TOP_SCORER_INDEX[n]
        return True, f"mls_top_scorer_{g}g"
    if n in _STARTER_INDEX:
        return True, "mls_regular_starter"
    # Book-priced strong favorite (implied ≥ 32% ≈ +215 or shorter).
    if implied_prob >= 0.32:
        return True, f"market_favorite_{implied_prob:.2f}"
    return False, f"not_top_scorer_or_starter_implied_{implied_prob:.2f}"


def mls_scorer_priority(player_name: str) -> int:
    """Return a priority score (higher = more elite) so we can rank
    remaining candidates. Used for the per-event top-N cap so Messi
    always beats Cavan Sullivan even if both pass the gate.
    """
    if not player_name:
        return 0
    n = _norm(player_name)
    if n in _espn_names:
        rec = _espn_index[n]
        return 200 + rec.get("goals", 0) * 2 + rec.get("assists", 0)
    if n in _TOP_SCORER_INDEX:
        _team, g, a = _TOP_SCORER_INDEX[n]
        return 100 + g + a
    if n in _STARTER_INDEX:
        return 50
    return 0
