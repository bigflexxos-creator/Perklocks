"""Seeded canonical player catalog — ~120 marquee athletes across 4 sports.

These seeds:
  • Provide day-1 archetype + alias coverage for the most-bet stars
  • Serve as ground truth that auto-learning / API-Sports CANNOT override
  • Are merged into the `player_profiles_v2` collection on startup

Name format:
  canonical = the most common display name
  aliases   = misspellings, romanizations, short names, common nicknames

Schema per row:
  (canonical, sport, team_or_country, position, archetype, [aliases...])
"""
from __future__ import annotations

# (canonical, sport, team, position, archetype, aliases)
SEEDS: list[tuple] = [
    # ============================= SOCCER (Top 35) =============================
    # World-class strikers / finishers (high-xG attackers)
    ("Erling Haaland",       "Soccer", "Manchester City / Norway",  "ST",  "high-xG attacker",   ["Erling Braut Haaland", "Haaland", "E. Haaland"]),
    ("Kylian Mbappé",        "Soccer", "Real Madrid / France",      "FW",  "high-xG attacker",   ["Kylian Mbappe", "Mbappé", "Mbappe", "K. Mbappé"]),
    ("Harry Kane",           "Soccer", "Bayern Munich / England",   "ST",  "finisher",           ["H. Kane", "Kane"]),
    ("Robert Lewandowski",   "Soccer", "Barcelona / Poland",        "ST",  "finisher",           ["Lewandowski", "R. Lewandowski"]),
    ("Victor Osimhen",       "Soccer", "Galatasaray / Nigeria",     "ST",  "finisher",           ["Osimhen", "V. Osimhen"]),
    ("Viktor Gyökeres",      "Soccer", "Arsenal / Sweden",          "ST",  "finisher",           ["Viktor Gyokeres", "Gyökeres", "Gyokeres"]),
    ("Lautaro Martínez",     "Soccer", "Inter Milan / Argentina",   "ST",  "finisher",           ["Lautaro Martinez", "Lautaro", "L. Martínez"]),
    ("Dušan Vlahović",       "Soccer", "Juventus / Serbia",         "ST",  "finisher",           ["Dusan Vlahovic", "Vlahović", "Vlahovic"]),
    ("Alexander Isak",       "Soccer", "Liverpool / Sweden",        "ST",  "finisher",           ["A. Isak", "Isak"]),
    ("Benjamin Šeško",        "Soccer", "Manchester United / Slovenia", "ST", "high-xG attacker", ["Benjamin Sesko", "Sesko", "Šeško"]),
    ("Hugo Ekitike",         "Soccer", "Liverpool / France",        "ST",  "finisher",           ["H. Ekitike", "Ekitike"]),
    ("Serhou Guirassy",      "Soccer", "Dortmund / Guinea",         "ST",  "finisher",           ["Guirassy", "S. Guirassy"]),
    # Playmakers / creators
    ("Lionel Messi",         "Soccer", "Inter Miami / Argentina",   "RW/AM", "playmaker",        ["L. Messi", "Messi", "Leo Messi"]),
    ("Kevin De Bruyne",      "Soccer", "Napoli / Belgium",          "AM",  "playmaker",          ["KDB", "De Bruyne", "K. De Bruyne"]),
    ("Bruno Fernandes",      "Soccer", "Manchester United / Portugal", "AM", "playmaker",      ["B. Fernandes", "Bruno"]),
    ("Jude Bellingham",      "Soccer", "Real Madrid / England",     "AM",  "creator",            ["J. Bellingham", "Bellingham"]),
    ("Florian Wirtz",        "Soccer", "Liverpool / Germany",       "AM",  "creator",            ["F. Wirtz", "Wirtz"]),
    ("Phil Foden",           "Soccer", "Manchester City / England", "AM",  "creator",            ["P. Foden", "Foden"]),
    ("Cole Palmer",          "Soccer", "Chelsea / England",         "AM",  "creator",            ["C. Palmer", "Palmer"]),
    ("Lamine Yamal",         "Soccer", "Barcelona / Spain",         "RW",  "creator",            ["L. Yamal", "Yamal"]),
    # Wingers / inverted forwards
    ("Mohamed Salah",        "Soccer", "Liverpool / Egypt",         "RW",  "finisher",           ["Mo Salah", "Salah", "M. Salah"]),
    ("Vinícius Júnior",       "Soccer", "Real Madrid / Brazil",      "LW",  "high-xG attacker",   ["Vinicius Jr", "Vinicius Junior", "Vini Jr", "Vinicius Júnior", "Vinicius Jose Paixao de Oliveira Junior"]),
    ("Bukayo Saka",          "Soccer", "Arsenal / England",         "RW",  "creator",            ["B. Saka", "Saka"]),
    ("Rodrygo",              "Soccer", "Real Madrid / Brazil",      "FW",  "creator",            ["Rodrygo Goes"]),
    ("Ousmane Dembélé",       "Soccer", "Paris SG / France",         "FW",  "creator",            ["Ousmane Dembele", "Dembélé", "Dembele"]),
    ("Khvicha Kvaratskhelia","Soccer", "Paris SG / Georgia",        "LW",  "creator",            ["Kvaratskhelia", "Kvara", "K. Kvaratskhelia"]),
    # Defensive anchors (CB / DM)
    ("Virgil van Dijk",      "Soccer", "Liverpool / Netherlands",   "CB",  "defensive anchor",   ["Van Dijk", "V. van Dijk"]),
    ("Rodri",                "Soccer", "Manchester City / Spain",   "DM",  "defensive anchor",   ["Rodri Hernandez", "R. Hernández"]),
    ("William Saliba",       "Soccer", "Arsenal / France",          "CB",  "defensive anchor",   ["Saliba", "W. Saliba"]),
    ("Ronald Araujo",        "Soccer", "Barcelona / Uruguay",       "CB",  "defensive anchor",   ["Araújo", "R. Araujo"]),
    # Other elite scorers commonly bet
    ("Julian Alvarez",       "Soccer", "Atletico Madrid / Argentina", "FW", "finisher",         ["Julián Álvarez", "Álvarez", "J. Alvarez"]),
    ("Romelu Lukaku",        "Soccer", "Napoli / Belgium",          "ST",  "finisher",           ["R. Lukaku", "Lukaku"]),
    ("Cristiano Ronaldo",    "Soccer", "Al Nassr / Portugal",       "ST",  "finisher",           ["CR7", "Ronaldo", "C. Ronaldo"]),
    ("Heung-min Son",        "Soccer", "LAFC / South Korea",        "FW",  "finisher",           ["Son Heung-min", "H. Son", "Son"]),
    ("Ollie Watkins",        "Soccer", "Aston Villa / England",     "ST",  "finisher",           ["O. Watkins", "Watkins"]),

    # ============================== NBA (Top 25) ==============================
    ("LeBron James",         "NBA", "Los Angeles Lakers", "SF", "two-way wing",     ["LBJ", "L. James"]),
    ("Stephen Curry",        "NBA", "Golden State Warriors", "PG", "volume shooter", ["Steph Curry", "S. Curry", "Curry"]),
    ("Kevin Durant",         "NBA", "Phoenix Suns", "SF/PF", "scorer",              ["KD", "K. Durant"]),
    ("Giannis Antetokounmpo","NBA", "Milwaukee Bucks", "PF", "scorer",              ["Greek Freak", "Giannis", "G. Antetokounmpo"]),
    ("Nikola Jokić",         "NBA", "Denver Nuggets", "C", "facilitator",          ["Nikola Jokic", "Joker", "Jokic", "Jokić"]),
    ("Luka Dončić",          "NBA", "LA Lakers", "PG", "scorer",                   ["Luka Doncic", "Doncic", "Dončić"]),
    ("Joel Embiid",          "NBA", "Philadelphia 76ers", "C", "scorer",            ["J. Embiid", "Embiid"]),
    ("Jayson Tatum",         "NBA", "Boston Celtics", "SF", "two-way wing",         ["J. Tatum", "Tatum"]),
    ("Anthony Davis",        "NBA", "Dallas Mavericks", "PF/C", "rim protector",    ["AD", "A. Davis"]),
    ("Shai Gilgeous-Alexander", "NBA", "OKC Thunder", "PG", "scorer",               ["SGA", "Shai", "S. Gilgeous-Alexander"]),
    ("Victor Wembanyama",    "NBA", "San Antonio Spurs", "C", "rim protector",      ["Wemby", "Wembanyama", "V. Wembanyama"]),
    ("Jaylen Brown",         "NBA", "Boston Celtics", "SG", "two-way wing",         ["J. Brown"]),
    ("Devin Booker",         "NBA", "Phoenix Suns", "SG", "scorer",                 ["D. Booker", "Booker"]),
    ("Damian Lillard",       "NBA", "Milwaukee Bucks", "PG", "volume shooter",      ["Dame", "D. Lillard"]),
    ("James Harden",         "NBA", "LA Clippers", "SG", "facilitator",             ["J. Harden", "Beard"]),
    ("Tyrese Haliburton",    "NBA", "Indiana Pacers", "PG", "facilitator",          ["Hali", "T. Haliburton"]),
    ("Donovan Mitchell",     "NBA", "Cleveland Cavaliers", "SG", "scorer",          ["Spida", "D. Mitchell"]),
    ("Anthony Edwards",      "NBA", "Minnesota Timberwolves", "SG", "scorer",       ["Ant", "Ant-Man", "A. Edwards"]),
    ("Trae Young",           "NBA", "Atlanta Hawks", "PG", "facilitator",           ["Ice Trae", "T. Young"]),
    ("Karl-Anthony Towns",   "NBA", "New York Knicks", "C", "volume shooter",       ["KAT", "K. Towns"]),
    ("Jamal Murray",         "NBA", "Denver Nuggets", "PG", "scorer",               ["J. Murray"]),
    ("Paolo Banchero",       "NBA", "Orlando Magic", "PF", "scorer",                ["P. Banchero", "Paolo"]),
    ("Cade Cunningham",      "NBA", "Detroit Pistons", "PG", "facilitator",         ["C. Cunningham"]),
    ("De'Aaron Fox",         "NBA", "Sacramento Kings", "PG", "scorer",             ["D. Fox", "Swipa"]),
    ("Domantas Sabonis",     "NBA", "Sacramento Kings", "PF/C", "facilitator",      ["D. Sabonis", "Sabonis"]),

    # ============================== NFL (Top 25) ==============================
    ("Patrick Mahomes",      "NFL", "Kansas City Chiefs", "QB", "dual-threat QB",   ["P. Mahomes", "Mahomes"]),
    ("Josh Allen",           "NFL", "Buffalo Bills", "QB", "dual-threat QB",        ["J. Allen"]),
    ("Lamar Jackson",        "NFL", "Baltimore Ravens", "QB", "dual-threat QB",     ["L. Jackson", "L8"]),
    ("Jalen Hurts",          "NFL", "Philadelphia Eagles", "QB", "dual-threat QB",  ["J. Hurts"]),
    ("Joe Burrow",           "NFL", "Cincinnati Bengals", "QB", "pocket QB",        ["J. Burrow", "Joey B"]),
    ("Justin Herbert",       "NFL", "LA Chargers", "QB", "pocket QB",                ["J. Herbert"]),
    ("Tua Tagovailoa",       "NFL", "Miami Dolphins", "QB", "pocket QB",            ["Tua", "T. Tagovailoa"]),
    ("Caleb Williams",       "NFL", "Chicago Bears", "QB", "dual-threat QB",        ["C. Williams"]),
    ("Jayden Daniels",       "NFL", "Washington Commanders", "QB", "dual-threat QB",["J. Daniels"]),
    ("C.J. Stroud",          "NFL", "Houston Texans", "QB", "pocket QB",            ["CJ Stroud", "Stroud"]),
    ("Saquon Barkley",       "NFL", "Philadelphia Eagles", "RB", "workhorse RB",    ["S. Barkley", "Saquon"]),
    ("Christian McCaffrey",  "NFL", "SF 49ers", "RB", "workhorse RB",              ["CMC", "C. McCaffrey"]),
    ("Derrick Henry",        "NFL", "Baltimore Ravens", "RB", "power back",        ["King Henry", "D. Henry"]),
    ("Bijan Robinson",       "NFL", "Atlanta Falcons", "RB", "workhorse RB",       ["B. Robinson", "Bijan"]),
    ("Jahmyr Gibbs",         "NFL", "Detroit Lions", "RB", "workhorse RB",         ["J. Gibbs"]),
    ("Justin Jefferson",     "NFL", "Minnesota Vikings", "WR", "deep threat",      ["JJ", "J. Jefferson"]),
    ("Ja'Marr Chase",        "NFL", "Cincinnati Bengals", "WR", "deep threat",     ["J. Chase"]),
    ("CeeDee Lamb",          "NFL", "Dallas Cowboys", "WR", "deep threat",         ["C. Lamb"]),
    ("Tyreek Hill",          "NFL", "Miami Dolphins", "WR", "deep threat",         ["Cheetah", "T. Hill"]),
    ("A.J. Brown",           "NFL", "Philadelphia Eagles", "WR", "red zone target",["AJ Brown", "A. Brown"]),
    ("Amon-Ra St. Brown",    "NFL", "Detroit Lions", "WR", "possession receiver",  ["ARSB", "A. St. Brown"]),
    ("Travis Kelce",         "NFL", "Kansas City Chiefs", "TE", "red zone target", ["T. Kelce"]),
    ("Sam LaPorta",          "NFL", "Detroit Lions", "TE", "red zone target",      ["S. LaPorta"]),
    ("Trevon Diggs",         "NFL", "Dallas Cowboys", "CB", "shutdown CB",         ["T. Diggs"]),
    ("Myles Garrett",        "NFL", "Cleveland Browns", "DE", "edge rusher",       ["M. Garrett"]),

    # ============================== Tennis (Top 20) ==============================
    ("Novak Djokovic",       "Tennis", "Serbia", "ATP", "counterpuncher",   ["Djoker", "N. Djokovic", "Djokovic"]),
    ("Jannik Sinner",        "Tennis", "Italy", "ATP", "aggressive server", ["J. Sinner", "Sinner"]),
    ("Carlos Alcaraz",       "Tennis", "Spain", "ATP", "all-court player",  ["C. Alcaraz", "Alcaraz", "Carlitos"]),
    ("Alexander Zverev",     "Tennis", "Germany", "ATP", "aggressive server", ["Sascha", "A. Zverev"]),
    ("Daniil Medvedev",      "Tennis", "Russia", "ATP", "baseline grinder", ["D. Medvedev", "Medvedev"]),
    ("Andrey Rublev",        "Tennis", "Russia", "ATP", "aggressive server", ["A. Rublev", "Rublev"]),
    ("Stefanos Tsitsipas",   "Tennis", "Greece", "ATP", "all-court player", ["Tsitsipas", "S. Tsitsipas"]),
    ("Holger Rune",          "Tennis", "Denmark", "ATP", "aggressive server", ["H. Rune", "Rune"]),
    ("Casper Ruud",          "Tennis", "Norway", "ATP", "baseline grinder", ["C. Ruud"]),
    ("Taylor Fritz",         "Tennis", "USA", "ATP", "aggressive server", ["T. Fritz"]),
    ("Iga Świątek",          "Tennis", "Poland", "WTA", "baseline grinder", ["Iga Swiatek", "Swiatek", "Świątek"]),
    ("Aryna Sabalenka",      "Tennis", "Belarus", "WTA", "aggressive server", ["A. Sabalenka", "Sabalenka"]),
    ("Coco Gauff",           "Tennis", "USA", "WTA", "all-court player", ["C. Gauff"]),
    ("Elena Rybakina",       "Tennis", "Kazakhstan", "WTA", "aggressive server", ["E. Rybakina", "Rybakina"]),
    ("Jasmine Paolini",      "Tennis", "Italy", "WTA", "counterpuncher", ["J. Paolini", "Paolini"]),
    ("Qinwen Zheng",         "Tennis", "China", "WTA", "aggressive server", ["Zheng Qinwen", "Q. Zheng"]),
    ("Mirra Andreeva",       "Tennis", "Russia", "WTA", "all-court player", ["M. Andreeva", "Andreeva"]),
    ("Madison Keys",         "Tennis", "USA", "WTA", "aggressive server", ["M. Keys"]),
    ("Jessica Pegula",       "Tennis", "USA", "WTA", "baseline grinder", ["J. Pegula"]),
    ("Paula Badosa",         "Tennis", "Spain", "WTA", "aggressive server", ["P. Badosa", "Badosa"]),
]


def seed_rows() -> list[dict]:
    """Return the seed catalog as dicts with normalized alias index."""
    out: list[dict] = []
    for canonical, sport, team, position, archetype, aliases in SEEDS:
        out.append({
            "canonical_name":  canonical,
            "sport":           sport,
            "team":            team,
            "position":        position,
            "archetype":       archetype,
            "archetype_source":"seed",
            "aliases":         list({canonical, *aliases}),
            "usage_intensity": "high",   # seeds are all marquee = high usage
            "volatility":      None,     # filled by settled-pick learning
            "sample_size":     0,
            "source":          "seed",
        })
    return out
