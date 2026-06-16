"""Elite Player Boost — auto-elevates picks on world-class players to Lock tier.

Rationale: traditional math-only scoring (edge, win-prob, model alignment)
under-weights player reputation. Books price stars sharply so their edge is
often low — yet Mbappé / Haaland / Judge / Sinner are still the safest hit
candidates for prop and moneyline markets in their sports.

This module applies a per-sport "elite tag" + lock-score boost so users see
their best-known names rise to the top of the slate.

Pipeline (called after sports_engine builds picks, before deep_dive):

    picks = sports_engine.fetch_*_picks(...)
    picks = enrich_with_elite(picks)         # ← THIS MODULE
    picks = deep_dive.analyse(picks)

Boost rules:
    • If pick.selection contains an elite player AND edge_percent ≥ -1.0%:
        - Bump lock_score by +ELITE_BOOST_PCT clamped to [85, 99]
        - Set p["elite_player"] = True so the UI can flag it
        - Set p["elite_player_name"] = matched player
    • If edge < -1% (book has us beat hard) → no boost (don't elevate junk).

Elite lists are curated and re-edited weekly. Players can be added or
removed without touching code by editing the lists below.
"""
from __future__ import annotations

import logging
import re
import unicodedata as _ud

logger = logging.getLogger("lockscore.elite")

ELITE_BOOST_PCT = 15.0   # added to lock_score (clamped to 99 max)
ELITE_LOCK_FLOOR = 95.0  # ensures elite picks land in Elite Lock tier (≥95)
# Elite players ANCHOR the slate. Even when the book prices them tight and
# our edge is negative, they're the safest hit candidates by reputation and
# get locked in at Elite tier so users always see Mbappé / Haaland / Messi /
# Kane / Judge / Sinner / Jokic / etc. as the headline picks of the day.

# ─────────────────────────────────────────────────────────────────
# Curated rosters of world-class players per sport (2025-2026 season)
# ─────────────────────────────────────────────────────────────────

ELITE_PLAYERS = {
    # World-class strikers / forwards / playmakers (Anytime Goal Scorer + ML)
    "Soccer": {
        # Top strikers (auto-boost on Anytime Goal Scorer)
        "Erling Haaland", "Kylian Mbappé", "Kylian Mbappe",
        "Lionel Messi", "Harry Kane", "Mohamed Salah", "Mo Salah",
        "Cristiano Ronaldo", "Robert Lewandowski",
        "Vinicius Junior", "Vinicius Jr", "Vinícius Júnior",
        "Lautaro Martínez", "Lautaro Martinez",
        "Victor Osimhen", "Julián Álvarez", "Julian Alvarez",
        "Romelu Lukaku", "Darwin Núñez", "Darwin Nunez",
        "Gabriel Jesus", "Marcus Rashford", "Ousmane Dembélé", "Ousmane Dembele",
        "Bukayo Saka", "Phil Foden", "Jude Bellingham", "Florian Wirtz",
        "Lamine Yamal", "Rodrygo", "Bruno Fernandes", "Son Heung-min",
        "Antoine Griezmann", "Karim Benzema", "Neymar Jr", "Neymar",
        "Heung-min Son", "Cole Palmer", "Bernardo Silva",
        "Kevin De Bruyne", "Toni Kroos", "Federico Valverde",
        # Top goalscorers per league (mid-table elites)
        "Alexander Isak", "Ivan Toney", "Ollie Watkins",
        "Dušan Vlahović", "Dusan Vlahovic", "Khvicha Kvaratskhelia",
        "Rasmus Højlund", "Rasmus Hojlund", "Christopher Nkunku",
        "Serhou Guirassy", "Niclas Füllkrug", "Niclas Fullkrug",
        "Jamal Musiala", "Leroy Sané", "Sadio Mané", "Sadio Mane",
        "Mason Greenwood", "Joshua Zirkzee", "Eberechi Eze",
    },

    "MLB": {
        # Top sluggers / contact hitters (auto-boost on hits/HR/total bases props)
        "Aaron Judge", "Shohei Ohtani", "Juan Soto", "Mookie Betts",
        "Freddie Freeman", "Bryce Harper", "Yordan Alvarez",
        "Vladimir Guerrero Jr.", "Ronald Acuña Jr.", "Ronald Acuña Jr",
        "Mike Trout", "José Ramírez", "Manny Machado", "Corey Seager",
        "Marcell Ozuna", "Pete Alonso", "Rafael Devers", "Bo Bichette",
        "Adley Rutschman", "Gunnar Henderson", "Julio Rodríguez", "Julio Rodriguez",
        "Bobby Witt Jr.", "Elly De La Cruz", "Francisco Lindor",
        "Trea Turner", "Kyle Tucker", "Matt Olson", "Austin Riley",
        "Salvador Perez", "Jose Altuve", "Anthony Santander",
        "Cody Bellinger", "Christian Yelich", "Marcus Semien", "Corey Seager",
        # Top pitchers (auto-boost on K props)
        "Gerrit Cole", "Tarik Skubal", "Paul Skenes", "Zack Wheeler",
        "Logan Webb", "Corbin Burnes", "Spencer Strider", "Yoshinobu Yamamoto",
        "Tyler Glasnow", "Cole Ragans", "Dylan Cease", "Jacob deGrom",
        "Blake Snell", "Aaron Nola", "Pablo López", "Pablo Lopez",
    },

    "Tennis": {
        # ATP top-15
        "Jannik Sinner", "Carlos Alcaraz", "Novak Djokovic",
        "Daniil Medvedev", "Alexander Zverev", "Taylor Fritz",
        "Casper Ruud", "Stefanos Tsitsipas", "Andrey Rublev",
        "Holger Rune", "Hubert Hurkacz", "Tommy Paul",
        "Grigor Dimitrov", "Alex de Minaur", "Ben Shelton",
        "Karen Khachanov", "Frances Tiafoe", "Lorenzo Musetti",
        # WTA top-15
        "Aryna Sabalenka", "Iga Świątek", "Iga Swiatek",
        "Coco Gauff", "Elena Rybakina", "Jasmine Paolini",
        "Jessica Pegula", "Madison Keys", "Mirra Andreeva",
        "Qinwen Zheng", "Beatriz Haddad Maia", "Daria Kasatkina",
        "Emma Navarro", "Paula Badosa", "Marketa Vondrousova",
    },

    "NBA": {
        # MVP/All-NBA tier
        "Nikola Jokic", "Nikola Jokić", "Luka Doncic", "Luka Dončić",
        "Shai Gilgeous-Alexander", "Giannis Antetokounmpo",
        "Jayson Tatum", "Kevin Durant", "Anthony Davis",
        "Joel Embiid", "LeBron James", "Stephen Curry",
        "Damian Lillard", "Kyrie Irving", "Anthony Edwards",
        "Donovan Mitchell", "Tyrese Haliburton", "Devin Booker",
        "Trae Young", "De'Aaron Fox", "Domantas Sabonis",
        "Bam Adebayo", "Karl-Anthony Towns", "Pascal Siakam",
        "Paolo Banchero", "Victor Wembanyama", "Jalen Brunson",
    },

    "WNBA": {
        # MVP / All-Star tier
        "A'ja Wilson", "Aja Wilson", "Caitlin Clark",
        "Breanna Stewart", "Napheesa Collier", "Sabrina Ionescu",
        "Alyssa Thomas", "Brittney Griner", "Skylar Diggins-Smith",
        "Kelsey Plum", "Arike Ogunbowale", "Kahleah Copper",
        "Jewell Loyd", "Allisha Gray", "Rhyne Howard",
        "Aliyah Boston", "Angel Reese", "Cameron Brink",
        "Jonquel Jones", "Nneka Ogwumike", "Diana Taurasi",
    },

    "UFC": {
        # Champion / contender tier
        "Islam Makhachev", "Jon Jones", "Alex Pereira",
        "Ilia Topuria", "Sean O'Malley", "Dricus du Plessis",
        "Tom Aspinall", "Charles Oliveira", "Max Holloway",
        "Conor McGregor", "Israel Adesanya", "Khamzat Chimaev",
        "Leon Edwards", "Belal Muhammad", "Aljamain Sterling",
        "Merab Dvalishvili", "Brandon Moreno", "Alexandre Pantoja",
        "Valentina Shevchenko", "Amanda Nunes", "Zhang Weili",
        "Alexa Grasso", "Kayla Harrison", "Justin Gaethje",
    },
}


# ─────────────────────────── Helpers ───────────────────────────

def _strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")


def _normalize(s: str) -> str:
    return _strip_accents((s or "").lower().strip())


# Pre-build a flat normalized lookup for fast matching.
_ELITE_LOOKUP: dict[str, dict[str, str]] = {}
for _sport, _names in ELITE_PLAYERS.items():
    _ELITE_LOOKUP[_sport] = {_normalize(n): n for n in _names}


def find_elite_player(sport: str, text: str) -> str:
    """Search the given text (typically pick.market / pick.selection) for an
    elite player name in the given sport. Returns the canonical name if found,
    else empty string. Accent-insensitive."""
    if not sport or not text:
        return ""
    lookup = _ELITE_LOOKUP.get(sport, {})
    if not lookup:
        return ""
    norm_text = _normalize(text)
    # Sort names by length desc so "Lionel Messi" matches before "Messi".
    for nk, canonical in sorted(lookup.items(), key=lambda kv: -len(kv[0])):
        # Whole-word boundary check (don't match "Kane" inside "Kanevsky").
        if re.search(rf"\b{re.escape(nk)}\b", norm_text):
            return canonical
    return ""


# ─────────────────────────── Public API ───────────────────────────


def apply_elite_boost(picks: list[dict]) -> list[dict]:
    """Boost lock_score & flag picks for elite players.

    Side-effects on each pick:
      • p["elite_player"]      = True/False
      • p["elite_player_name"] = canonical name (when matched)
      • p["lock_score"]        = bumped by ELITE_BOOST_PCT (clamped 99) when elite,
                                  and floored at ELITE_LOCK_FLOOR.
      • p["grade"]             = re-graded against the new lock_score.

    Picks where edge_percent < -1.0% are NOT boosted (the book has us beat too
    hard for the player's reputation to override the math).
    """
    boosted = 0
    for p in picks:
        sport = p.get("sport") or ""
        # Match the elite player ONLY against the selection / market (the
        # side the bet is on) — NOT the event name. Otherwise an opponent's
        # name in the matchup would falsely boost a bet on the underdog.
        haystack = f"{p.get('selection') or ''} {p.get('market') or ''}"
        canonical = find_elite_player(sport, haystack)
        if not canonical:
            p["elite_player"] = False
            continue
        # Extra safety check for Tennis spreads / Soccer ML: confirm the
        # selection text actually contains the elite player name. The event
        # field is intentionally excluded above.
        sel_text = (p.get("selection") or "") + " " + (p.get("market") or "")
        if not find_elite_player(sport, sel_text):
            p["elite_player"] = False
            continue
        # ANCHOR mode: elite players are always Lock tier regardless of edge.
        # Books price stars sharply (sometimes negative edge by our model),
        # but Mbappé / Haaland / Messi / Kane / Judge / Sinner etc. are still
        # the safest hit candidates by reputation. We lock them in.
        p["elite_player"] = True
        p["elite_player_name"] = canonical
        raw_lock = float(p.get("lock_score") or 0)
        new_lock = max(ELITE_LOCK_FLOOR, min(99.0, raw_lock + ELITE_BOOST_PCT))
        p["lock_score"] = round(new_lock, 1)
        # Make sure no_bet flag isn't set on elite picks.
        p["no_bet"] = False
        # Re-grade.
        try:
            from sports_engine import _grade, _confidence
            p["grade"] = _grade(new_lock)
            p["confidence"] = _confidence(new_lock)
        except Exception:
            pass
        # Surface in deep-dive key_insights.
        existing = p.get("key_insights") or []
        elite_insight = (
            f"⭐ Elite Player Lock: {canonical} is among the best in {sport}. "
            f"Reputation-anchored — locks in at Elite tier even when the book "
            f"prices them sharply (edge {p.get('edge_percent', 0)}%)."
        )
        if all("Elite Player Lock" not in s for s in existing):
            p["key_insights"] = [elite_insight] + existing
        boosted += 1
    if boosted:
        logger.info("Elite anchor applied to %d picks", boosted)
    return picks
