"""P0-C (2026-08-11) — Soccer identity ingestion for the major
competitions we actually publish picks on.

The Phase 2 canonical player_identity registry started life MLS-only.
This module extends coverage to:

  * Big-5 European club leagues — EPL / La Liga / Serie A / Bundesliga /
    Ligue 1 — hydrated from the existing `soccer_player_form`
    collection (already refreshed by the Understat ingest loop).
  * Major international / national-team affiliations — a curated
    bootstrap covering the players most likely to appear in Locks-grade
    picks (Messi, Ronaldo, Mbappé, Vinícius, etc.).  Independent
    freshness stream from club affiliation.

All writes go through the P0-A race-safe `persist_identity` layer, so
older observations cannot overwrite fresher ones and concurrent
replicas cannot duplicate identities.

Callable from server startup (one-shot) and periodic refresh loops.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.soccer_identity_ingest")


# ─────────────────────────────────────────────────────────────────
# Big-5 hydration from `soccer_player_form`
# ─────────────────────────────────────────────────────────────────
_LEAGUE_ALIASES = {
    # Normalised league name  → canonical league label used in
    # player_identities.league so `has_fresh_roster_for_league` can
    # find records consistently.
    "epl":         "EPL",
    "premier league": "EPL",
    "english premier league": "EPL",
    "la_liga":     "La Liga",
    "la liga":     "La Liga",
    "laliga":      "La Liga",
    "serie_a":     "Serie A",
    "serie a":     "Serie A",
    "bundesliga":  "Bundesliga",
    "ligue_1":     "Ligue 1",
    "ligue 1":     "Ligue 1",
    "mls":         "MLS",
}


def _canon_league(league: Optional[str]) -> Optional[str]:
    if not league:
        return None
    k = league.strip().lower()
    return _LEAGUE_ALIASES.get(k, league.strip())


async def hydrate_big5_from_soccer_player_form(
    db, *, min_games: int = 0,
) -> dict[str, Any]:
    """Read `db.soccer_player_form` and upsert an identity record for
    every player.  Returns a diagnostic dict.

    * ``current_team`` = ``doc['team']`` — sometimes a comma-separated
      transfer trail (e.g. "Bournemouth,Manchester City").  We take
      the LAST segment as the current club.
    * ``observed_at`` = ``doc['updated_at']`` ISO string.
    * ``provider``/``provider_id`` = ``understat``/``doc['understat_id']``.
    """
    from services.player_identity import (
        upsert_player, reset_registry_for_tests, persist_registry,
        ensure_identity_indexes,
    )

    await ensure_identity_indexes(db)

    stats = {"leagues": {}, "upserts": 0, "skipped_no_team": 0,
              "skipped_no_name": 0, "min_games": min_games}

    query: dict[str, Any] = {}
    if min_games > 0:
        query["games"] = {"$gte": min_games}

    async for d in db.soccer_player_form.find(
        query,
        {"_id": 0, "player_name": 1, "name_canonical": 1,
         "team": 1, "league": 1, "understat_id": 1,
         "position": 1, "updated_at": 1, "season": 1},
    ):
        name = d.get("player_name") or d.get("name_canonical")
        raw_league = d.get("league")
        if not name:
            stats["skipped_no_name"] += 1
            continue
        team_raw = d.get("team") or ""
        # Some Understat entries list transfer trails as
        # "Old,Current" — the LAST segment is the current club.
        team = ""
        if team_raw:
            team = team_raw.split(",")[-1].strip()
        if not team:
            stats["skipped_no_team"] += 1
            continue
        league = _canon_league(raw_league) or "Soccer"
        upd = d.get("updated_at")
        if hasattr(upd, "isoformat"):
            observed_iso = upd.isoformat()
        elif isinstance(upd, str):
            observed_iso = upd
        else:
            observed_iso = datetime.now(timezone.utc).isoformat()
        pid = d.get("understat_id")
        upsert_player(
            name=str(name), sport="Soccer", league=league,
            provider="understat" if pid else None,
            provider_id=(str(pid) if pid else None),
            current_team=team,
            position=d.get("position"),
            roster_status="active",
            source="soccer_player_form",
            observed_at=observed_iso,
            affiliation_type="club",
        )
        stats["upserts"] += 1
        stats["leagues"][league] = stats["leagues"].get(league, 0) + 1

    n_written = await persist_registry(db)
    stats["mongo_writes"] = n_written
    return stats


# ─────────────────────────────────────────────────────────────────
# National-team bootstrap
# ─────────────────────────────────────────────────────────────────
#
# Curated affiliations for the players most likely to appear in
# Locks-grade international picks.  Every entry is public knowledge
# and represents the player's ACTIVE national-team call-up as of the
# 2026 international window.  These are seeded ONCE via the same
# race-safe persist_identity layer used by MLS ingestion.
#
# The list is intentionally focused on players with legitimate
# international appearances — no youth players, no benched squad
# fillers.  When a national-team roster feed is added later, this
# curated seed will simply become the fallback.
_NATIONAL_TEAM_BOOTSTRAP: list[tuple[str, str]] = [
    # South America
    ("Lionel Messi",          "Argentina"),
    ("Julian Alvarez",        "Argentina"),
    ("Julián Álvarez",        "Argentina"),
    ("Lautaro Martinez",      "Argentina"),
    ("Lautaro Martínez",      "Argentina"),
    ("Alexis Mac Allister",   "Argentina"),
    ("Enzo Fernandez",        "Argentina"),
    ("Enzo Fernández",        "Argentina"),
    ("Rodrigo De Paul",       "Argentina"),
    ("Angel Di Maria",        "Argentina"),
    ("Ángel Di María",        "Argentina"),
    ("Paulo Dybala",          "Argentina"),
    ("Nicolas Otamendi",      "Argentina"),
    ("Cristian Romero",       "Argentina"),
    ("Nicolas Tagliafico",    "Argentina"),
    ("Emiliano Martinez",     "Argentina"),
    ("Emiliano Martínez",     "Argentina"),
    ("Neymar",                "Brazil"),
    ("Vinicius Junior",       "Brazil"),
    ("Vinícius Júnior",       "Brazil"),
    ("Rodrygo",               "Brazil"),
    ("Rodrygo Goes",          "Brazil"),
    ("Casemiro",              "Brazil"),
    ("Marquinhos",            "Brazil"),
    ("Thiago Silva",          "Brazil"),
    ("Endrick",               "Brazil"),
    ("Raphinha",              "Brazil"),
    ("Bruno Guimaraes",       "Brazil"),
    ("Bruno Guimarães",       "Brazil"),
    ("Lucas Paqueta",         "Brazil"),
    ("Lucas Paquetá",         "Brazil"),
    ("Alisson",               "Brazil"),
    ("Ederson",               "Brazil"),
    ("Richarlison",           "Brazil"),
    ("Gabriel Jesus",         "Brazil"),
    ("Gabriel Martinelli",    "Brazil"),
    ("Federico Vinas",        "Uruguay"),
    ("Federico Viñas",        "Uruguay"),
    ("Darwin Nunez",          "Uruguay"),
    ("Darwin Núñez",          "Uruguay"),
    ("Federico Valverde",     "Uruguay"),
    ("Ronald Araujo",         "Uruguay"),
    ("Giorgian De Arrascaeta","Uruguay"),
    ("Luis Suarez",           "Uruguay"),
    ("Luis Suárez",           "Uruguay"),
    ("James Rodriguez",       "Colombia"),
    ("James Rodríguez",       "Colombia"),
    ("Luis Diaz",             "Colombia"),
    ("Luis Díaz",             "Colombia"),
    ("Rafael Santos Borre",   "Colombia"),
    ("Juan Cuadrado",         "Colombia"),
    ("Alexis Sanchez",        "Chile"),
    ("Alexis Sánchez",        "Chile"),
    ("Arturo Vidal",          "Chile"),
    ("Gianluca Lapadula",     "Peru"),
    ("Paolo Guerrero",        "Peru"),

    # Europe — England
    ("Harry Kane",            "England"),
    ("Bukayo Saka",           "England"),
    ("Jude Bellingham",       "England"),
    ("Phil Foden",            "England"),
    ("Cole Palmer",           "England"),
    ("Declan Rice",           "England"),
    ("Trent Alexander-Arnold","England"),
    ("Marcus Rashford",       "England"),
    ("Ollie Watkins",         "England"),
    ("Anthony Gordon",        "England"),
    ("Kyle Walker",           "England"),
    ("John Stones",           "England"),
    ("Jordan Pickford",       "England"),
    ("Jarrod Bowen",          "England"),
    ("Eberechi Eze",          "England"),
    ("Morgan Rogers",         "England"),
    # France
    ("Kylian Mbappe",         "France"),
    ("Kylian Mbappé",         "France"),
    ("Antoine Griezmann",     "France"),
    ("Ousmane Dembele",       "France"),
    ("Ousmane Dembélé",       "France"),
    ("Marcus Thuram",         "France"),
    ("Randal Kolo Muani",     "France"),
    ("Aurelien Tchouameni",   "France"),
    ("Aurélien Tchouaméni",   "France"),
    ("Eduardo Camavinga",     "France"),
    ("N'Golo Kante",          "France"),
    ("N'Golo Kanté",          "France"),
    ("Adrien Rabiot",         "France"),
    ("William Saliba",        "France"),
    ("Ibrahima Konate",       "France"),
    ("Ibrahima Konaté",       "France"),
    ("Theo Hernandez",        "France"),
    ("Theo Hernández",        "France"),
    ("Michael Olise",         "France"),
    ("Bradley Barcola",       "France"),
    # Portugal
    ("Cristiano Ronaldo",     "Portugal"),
    ("Bruno Fernandes",       "Portugal"),
    ("Bernardo Silva",        "Portugal"),
    ("Ruben Dias",            "Portugal"),
    ("Rúben Dias",            "Portugal"),
    ("Ruben Neves",           "Portugal"),
    ("Rúben Neves",           "Portugal"),
    ("Vitinha",               "Portugal"),
    ("Rafael Leao",           "Portugal"),
    ("Rafael Leão",           "Portugal"),
    ("Joao Felix",            "Portugal"),
    ("João Félix",            "Portugal"),
    ("Joao Cancelo",          "Portugal"),
    ("João Cancelo",          "Portugal"),
    ("Diogo Jota",            "Portugal"),
    ("Nuno Mendes",           "Portugal"),
    ("Pepe",                  "Portugal"),
    ("Bernardo Silva",        "Portugal"),
    # Spain
    ("Lamine Yamal",          "Spain"),
    ("Nico Williams",          "Spain"),
    ("Pedri",                  "Spain"),
    ("Gavi",                   "Spain"),
    ("Rodri",                  "Spain"),
    ("Alvaro Morata",          "Spain"),
    ("Álvaro Morata",          "Spain"),
    ("Dani Olmo",              "Spain"),
    ("Fabian Ruiz",            "Spain"),
    ("Fabián Ruiz",            "Spain"),
    ("Aymeric Laporte",        "Spain"),
    ("Marc Cucurella",         "Spain"),
    ("Unai Simon",             "Spain"),
    ("Unai Simón",             "Spain"),
    ("Ferran Torres",          "Spain"),
    ("Mikel Merino",           "Spain"),
    ("Robin Le Normand",       "Spain"),
    # Germany
    ("Kai Havertz",            "Germany"),
    ("Jamal Musiala",          "Germany"),
    ("Florian Wirtz",          "Germany"),
    ("Toni Kroos",             "Germany"),
    ("Ilkay Gundogan",         "Germany"),
    ("İlkay Gündoğan",         "Germany"),
    ("Antonio Rudiger",        "Germany"),
    ("Antonio Rüdiger",        "Germany"),
    ("Joshua Kimmich",         "Germany"),
    ("Leroy Sane",             "Germany"),
    ("Leroy Sané",             "Germany"),
    ("Serge Gnabry",           "Germany"),
    ("Niclas Fullkrug",        "Germany"),
    ("Niclas Füllkrug",        "Germany"),
    ("Manuel Neuer",           "Germany"),
    # Italy
    ("Federico Chiesa",        "Italy"),
    ("Nicolo Barella",         "Italy"),
    ("Nicolò Barella",         "Italy"),
    ("Gianluigi Donnarumma",   "Italy"),
    ("Ciro Immobile",          "Italy"),
    ("Lorenzo Insigne",        "Italy"),
    ("Alessandro Bastoni",     "Italy"),
    ("Sandro Tonali",          "Italy"),
    # Netherlands
    ("Virgil van Dijk",        "Netherlands"),
    ("Memphis Depay",          "Netherlands"),
    ("Frenkie de Jong",        "Netherlands"),
    ("Cody Gakpo",             "Netherlands"),
    ("Xavi Simons",            "Netherlands"),
    ("Denzel Dumfries",        "Netherlands"),
    ("Steven Bergwijn",        "Netherlands"),
    # Belgium
    ("Kevin De Bruyne",        "Belgium"),
    ("Romelu Lukaku",          "Belgium"),
    ("Jeremy Doku",            "Belgium"),
    ("Jérémy Doku",            "Belgium"),
    ("Youri Tielemans",        "Belgium"),
    ("Amadou Onana",           "Belgium"),
    # Croatia
    ("Luka Modric",            "Croatia"),
    ("Luka Modrić",            "Croatia"),
    ("Mateo Kovacic",          "Croatia"),
    ("Mateo Kovačić",          "Croatia"),
    ("Ivan Perisic",           "Croatia"),
    ("Ivan Perišić",           "Croatia"),
    ("Andrej Kramaric",        "Croatia"),
    ("Andrej Kramarić",        "Croatia"),
    # Norway / Poland / Nordic
    ("Erling Haaland",         "Norway"),
    ("Martin Odegaard",        "Norway"),
    ("Martin Ødegaard",        "Norway"),
    ("Robert Lewandowski",     "Poland"),
    ("Piotr Zielinski",        "Poland"),
    ("Piotr Zieliński",        "Poland"),
    ("Alexander Isak",         "Sweden"),
    ("Viktor Gyokeres",        "Sweden"),
    ("Viktor Gyökeres",        "Sweden"),
    ("Rasmus Hojlund",         "Denmark"),
    ("Rasmus Højlund",         "Denmark"),
    ("Christian Eriksen",      "Denmark"),
    # Turkey
    ("Hakan Calhanoglu",       "Turkey"),
    ("Hakan Çalhanoğlu",       "Turkey"),
    ("Arda Guler",             "Turkey"),
    ("Arda Güler",             "Turkey"),
    ("Kenan Yildiz",           "Turkey"),
    ("Kenan Yıldız",           "Turkey"),
    ("Can Yilmaz Uzun",        "Turkey"),
    # Africa
    ("Mohamed Salah",          "Egypt"),
    ("Riyad Mahrez",           "Algeria"),
    ("Ismael Bennacer",        "Algeria"),
    ("Sofiane Feghouli",       "Algeria"),
    ("Achraf Hakimi",          "Morocco"),
    ("Yassine Bounou",         "Morocco"),
    ("Hakim Ziyech",           "Morocco"),
    ("Sofyan Amrabat",         "Morocco"),
    ("Sadio Mane",             "Senegal"),
    ("Sadio Mané",             "Senegal"),
    ("Kalidou Koulibaly",      "Senegal"),
    ("Edouard Mendy",          "Senegal"),
    ("Victor Osimhen",         "Nigeria"),
    ("Ademola Lookman",        "Nigeria"),
    ("Alex Iwobi",             "Nigeria"),
    ("Wilfred Ndidi",          "Nigeria"),
    ("Andre Onana",            "Cameroon"),
    ("André Onana",            "Cameroon"),
    ("Mohammed Kudus",         "Ghana"),
    # Asia / other
    ("Son Heung-min",          "South Korea"),
    ("Lee Kang-in",            "South Korea"),
    ("Takefusa Kubo",          "Japan"),
    ("Kaoru Mitoma",           "Japan"),
    ("Wataru Endo",            "Japan"),
    ("Wataru Endō",            "Japan"),
    # Australia
    ("Aaron Mooy",             "Australia"),
    ("Mathew Ryan",            "Australia"),
    # USA
    ("Christian Pulisic",      "USA"),
    ("Weston McKennie",        "USA"),
    ("Timothy Weah",           "USA"),
    ("Yunus Musah",            "USA"),
    ("Sergino Dest",           "USA"),
    ("Sergiño Dest",           "USA"),
    # Uruguay extras
    ("Giorgian de Arrascaeta", "Uruguay"),
    # Igor Thiago (Brazil goal-scorer)
    ("Igor Thiago",            "Brazil"),
    ("Igor Thiago Nascimento Rodrigues", "Brazil"),
]


async def bootstrap_national_team_identities(
    db, *, source_tag: str = "curated_bootstrap_v1",
) -> dict[str, Any]:
    """Seed the national-team affiliation stream in
    `player_identities` for the curated elite list.  Uses the same
    race-safe `persist_identity` writer so re-runs are idempotent
    and older observations never overwrite fresher ones.
    """
    from services.player_identity import (
        upsert_player, persist_registry, ensure_identity_indexes,
    )
    await ensure_identity_indexes(db)
    now_iso = datetime.now(timezone.utc).isoformat()

    for name, country in _NATIONAL_TEAM_BOOTSTRAP:
        # Every entry seeds under sport=Soccer, league="International"
        # so the national-team stream lives on the same canonical
        # id as any pre-existing club identity for the same player
        # WHEN the name+country identity resolves — otherwise a
        # distinct national-team-only identity is minted.  This
        # never touches club fields.
        upsert_player(
            name=name, sport="Soccer", league="International",
            current_team=country,
            affiliation_type="national_team",
            roster_status="active",
            source=source_tag,
            observed_at=now_iso,
            nationality=country,
        )
    n_written = await persist_registry(db)
    return {
        "bootstrap_players": len(_NATIONAL_TEAM_BOOTSTRAP),
        "mongo_writes": n_written,
        "source": source_tag,
        "observed_at": now_iso,
    }


# ─────────────────────────────────────────────────────────────────
# Combined runner — used by server startup + refresh loop.
# ─────────────────────────────────────────────────────────────────
async def refresh_soccer_identity_registry(db) -> dict[str, Any]:
    """Convenience — one call that hydrates Big-5 clubs + national-team
    curated bootstrap, then persists everything to Mongo."""
    big5 = await hydrate_big5_from_soccer_player_form(db)
    nt = await bootstrap_national_team_identities(db)
    return {"big5": big5, "national_teams": nt}


__all__ = [
    "hydrate_big5_from_soccer_player_form",
    "bootstrap_national_team_identities",
    "refresh_soccer_identity_registry",
    "_NATIONAL_TEAM_BOOTSTRAP",
    "_LEAGUE_ALIASES",
]
