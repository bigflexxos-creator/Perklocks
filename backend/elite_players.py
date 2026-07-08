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

Starter gate (2026-07-08)
-------------------------
User feedback: Ollie Watkins / Ivan Toney kept surfacing as Elite Locks even
though neither had started England's recent World Cup matches. To stop
bench players from riding a reputation boost into a false Elite Lock we
now consult `soccer_player_form` at boost time.  If an elite Soccer name
has ≤ 1 appearance in the last 45 days (per the settled+backfilled
`db.picks` cohort) OR has never been backfilled with `roster_verified`
data → we skip the boost AND suppress the synthetic Anytime Goal Scorer
row.  The reputation list itself stays intact so the player automatically
comes back when they return to the XI.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata as _ud

logger = logging.getLogger("lockscore.elite")

ELITE_BOOST_PCT = 15.0   # added to lock_score (clamped to 99 max)
ELITE_LOCK_FLOOR = 95.0  # ensures elite picks land in Elite Lock tier (≥95)

# ── Starter-gate cache (Soccer only) ─────────────────────────────
# `_STARTER_CACHE` maps normalized-lowercase player name → 1 if the
# player has been observed on a roster in the last 45 days, 0 otherwise.
# Refreshed at most once per `_STARTER_TTL_SEC` to keep pick generation
# snappy (this function is called on every daily refresh over ~200 picks).
_STARTER_CACHE: dict[str, int] = {}
_STARTER_LEAGUE_KIND: dict[str, dict[str, int]] = {}
_STARTER_CACHE_TS: float = 0.0
_STARTER_TTL_SEC: int = 60 * 30   # 30 minutes


def _classify_event_league_kind(event: str | None,
                                league: str | None) -> str:
    """Return 'national' if the pick is on a national-team competition,
    else 'club'.  Used by the starter gate so Aston Villa Premier
    League starts don't rescue an England World Cup pick.
    """
    txt = f"{event or ''} {league or ''}".lower()
    NATIONAL_TOKENS = (
        "world cup", "world-cup", "fifa", "euro ", "euro20", "euro 20",
        "uefa nations", "copa america", "asian cup", "gold cup",
        "africa cup", "concacaf nations",
    )
    if any(t in txt for t in NATIONAL_TOKENS):
        return "national"
    return "club"


def _slug_player(name: str) -> str:
    n = _ud.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]", "", n.lower())


def _refresh_starter_cache() -> None:
    """Rebuild `_STARTER_CACHE` from `db.picks` in a single sync query.

    A player is "actively starting" iff they were flagged
    `is_starter=True` (in the backfilled dataset ESPN's `starter: true`)
    OR they logged a settled won/lost pick in the same window (organic
    picks come with lineup verification upstream) — in ≥ 2 rows over
    the last 45 days.

    We ALSO track a per-league-kind index so we can gate a national-
    team pick (e.g. World Cup) on national-team starts only.  Aston
    Villa starts don't rescue Ollie Watkins for an England pick.

    Cache shape:
        _STARTER_CACHE[slug]                = 1 iff ≥ 2 starts anywhere
        _STARTER_LEAGUE_KIND[slug]["national"] = int (start count)
        _STARTER_LEAGUE_KIND[slug]["club"]     = int
    """
    global _STARTER_CACHE_TS
    try:
        from pymongo import MongoClient
        from datetime import date, timedelta
        import os
        client = MongoClient(os.getenv("MONGO_URL"), serverSelectionTimeoutMS=2000)
        db = client[os.environ.get("DB_NAME") or "perkslocks_production"]
        cutoff = (date.today() - timedelta(days=45)).isoformat()
        pipeline = [
            {"$match": {
                "sport": "Soccer",
                "pick_date": {"$gte": cutoff},
                "market": {"$regex": "goal scorer|to score|scorer|assist", "$options": "i"},
                "$or": [
                    # Backfilled: only count STARTS (ignore sub appearances)
                    {"backfilled": True, "is_starter": True},
                    # Organic settled picks — lineup was already resolved upstream
                    {"status": {"$in": ["won", "lost"]}, "backfilled": {"$ne": True}},
                ],
            }},
            {"$group": {
                "_id": {"player": "$player_name",
                        "kind": {"$ifNull": ["$league_kind", "unknown"]}},
                "n": {"$sum": 1},
            }},
        ]
        cache: dict[str, int] = {}
        by_kind: dict[str, dict[str, int]] = {}
        for row in db.picks.aggregate(pipeline):
            _id = row.get("_id") or {}
            nm = _id.get("player") or ""
            kind = _id.get("kind") or "unknown"
            n = row.get("n") or 0
            if not nm:
                continue
            slug = _slug_player(nm)
            by_kind.setdefault(slug, {})[kind] = n
            total = sum(by_kind[slug].values())
            cache[slug] = 1 if total >= 2 else 0
        _STARTER_CACHE.clear()
        _STARTER_CACHE.update(cache)
        _STARTER_LEAGUE_KIND.clear()
        _STARTER_LEAGUE_KIND.update(by_kind)
        _STARTER_CACHE_TS = time.time()
        logger.info("Starter cache refreshed: %d Soccer players "
                    "(actively-starting=%d)", len(cache),
                    sum(1 for v in cache.values() if v == 1))
    except Exception as e:
        logger.warning("starter cache refresh failed: %s", e)


def _is_actively_starting_soccer(player_name: str,
                                 league_kind: str | None = None) -> bool:
    """Return True iff the elite striker has ≥ 2 STARTS in the last 45
    days (ESPN `starter: true`).

    If `league_kind` is provided ("national" or "club"), require ≥ 2
    starts in that specific league kind — an England World Cup pick
    isn't rescued by Aston Villa starts and vice versa.

    Elite-list players whose name we recognise from the reputation
    roster but who have **zero** recent starts fail the gate
    (fail-CLOSED for names we know).  Truly unknown names (never seen
    in DB, not in elite list) fail-open so a debut / new-transfer
    player still gets a fair shake.
    """
    global _STARTER_CACHE_TS
    if time.time() - _STARTER_CACHE_TS > _STARTER_TTL_SEC:
        _refresh_starter_cache()
    slug = _slug_player(player_name or "")

    # Fail-CLOSED for known elite names with zero data.  This is how we
    # catch Ivan Toney (loan to Al Ahli, no recent England call-ups) —
    # he's on the elite list but has no `is_starter=True` rows.
    known_elite = False
    for name in ELITE_PLAYERS.get("Soccer", set()):
        if _slug_player(name) == slug:
            known_elite = True
            break
    if slug not in _STARTER_CACHE:
        return not known_elite   # elite w/ no data → fail; otherwise fail-open

    by_kind = _STARTER_LEAGUE_KIND.get(slug) or {}
    if league_kind:
        # Require ≥ 2 starts IN THE SPECIFIC LEAGUE KIND.  1 lone
        # England appearance in 45 days is not enough to justify a
        # World-Cup Elite Lock.
        return by_kind.get(league_kind, 0) >= 2
    return _STARTER_CACHE[slug] == 1
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
        "Erling Haaland", "Erling Braut Haaland",
        "Kylian Mbappé", "Kylian Mbappe",
        "Lionel Messi", "Harry Kane", "Mohamed Salah", "Mo Salah",
        "Cristiano Ronaldo", "Cristiano Ronaldo dos Santos Aveiro",
        "Robert Lewandowski",
        "Vinicius Junior", "Vinicius Jr", "Vinícius Júnior",
        "Vinicius Jose Paixao de Oliveira Junior",
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
        # 2026 breakout strikers (Sporting / national-team marquee scorers)
        "Viktor Gyökeres", "Viktor Gyokeres",
        "Benjamin Šeško", "Benjamin Sesko",
        "Hugo Ekitike", "Mateo Retegui", "Artem Dovbyk",
        "Loïs Openda", "Lois Openda", "Santiago Giménez", "Santiago Gimenez",
        "Mikel Oyarzabal", "Bryan Mbeumo", "Yoane Wissa",
        "Cody Gakpo", "Memphis Depay",
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
            # PRESERVE upstream elite_player=True tag. Synthetic
            # CSL goalscorer picks (Cryzan, Felipe Sousa, Fábio
            # Abreu, Leonardo, Wu Lei, Júnior Negrão, Cédric Bakambu,
            # Wesley Moraes, …) are tagged elite_player=True by
            # thesportsdb_scorer.py with their OWN reputation-based
            # `lock_floor`. The ELITE_PLAYERS list here is curated for
            # top-5 European leagues + World Cup stars and intentionally
            # doesn't include CSL — we must NOT reset that flag to False
            # or the pick_validator's edge-zeroing carve-out + the
            # _dedupe_and_limit_goalscorers elite override both fail and
            # the pick gets silently trimmed at top-3.
            if not p.get("elite_player"):
                p["elite_player"] = False
            continue
        # Extra safety check for Tennis spreads / Soccer ML: confirm the
        # selection text actually contains the elite player name. The event
        # field is intentionally excluded above.
        sel_text = (p.get("selection") or "") + " " + (p.get("market") or "")
        if not find_elite_player(sport, sel_text):
            p["elite_player"] = False
            continue
        # ── Starter gate (Soccer only) ─────────────────────────────
        # Suppress the reputation boost for rotational / benched
        # strikers so Ollie Watkins / Ivan Toney (currently ~zero
        # England starts in last 45 days) don't ride reputation into
        # false Elite Locks.  A player automatically re-qualifies as
        # soon as they log ≥ 2 backfilled or settled roster
        # appearances in a 45-day window.  We also scope the gate to
        # the league-kind of the pick — a World Cup pick requires
        # national-team starts, not club starts.
        if sport == "Soccer":
            kind = _classify_event_league_kind(p.get("event"), p.get("league"))
            if not _is_actively_starting_soccer(canonical, league_kind=kind):
                p["elite_player"] = False
                p["elite_player_gate"] = f"not_starting_{kind}"
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

    # ── Synthetic Anytime Goal Scorer for elite strikers
    # When an elite striker has a "To Score or Assist" pick but no standalone
    # "Anytime Goal Scorer" (because the Odds API didn't expose that market
    # for this league), synthesize one. Goal-only probability ≈ 75% of
    # score-or-assist probability for strikers (assists are less common).
    ELITE_STRIKERS = {
        "Erling Haaland", "Erling Braut Haaland",
        "Kylian Mbappé", "Kylian Mbappe", "Harry Kane",
        "Lionel Messi", "Robert Lewandowski", "Mohamed Salah",
        "Victor Osimhen", "Lautaro Martínez", "Julián Álvarez",
        "Romelu Lukaku", "Darwin Núñez", "Cristiano Ronaldo",
        "Cristiano Ronaldo dos Santos Aveiro",
        "Bukayo Saka", "Vinicius Junior", "Vinicius Jr",
        "Alexander Isak", "Ollie Watkins", "Ivan Toney",
        "Khvicha Kvaratskhelia", "Jude Bellingham", "Jamal Musiala",
        "Serhou Guirassy", "Dušan Vlahović", "Dusan Vlahovic",
        # 2026 breakout marquee scorers
        "Viktor Gyökeres", "Viktor Gyokeres",
        "Benjamin Šeško", "Benjamin Sesko",
        "Hugo Ekitike", "Mateo Retegui", "Artem Dovbyk",
        "Loïs Openda", "Lois Openda",
        "Santiago Giménez", "Santiago Gimenez",
        "Mikel Oyarzabal", "Cody Gakpo", "Memphis Depay",
    }
    synth_added = 0
    soa_picks_by_player: dict[tuple, dict] = {}
    ags_picks_by_player: dict[tuple, dict] = {}
    ags_existing: set = set()
    fgs_existing: set = set()
    soa_existing: set = set()
    for p in picks:
        if (p.get("sport") or "") != "Soccer":
            continue
        market = p.get("market") or ""
        for striker in ELITE_STRIKERS:
            if striker.lower() in market.lower():
                if "To Score or Assist" in market:
                    soa_picks_by_player[(striker, p.get("event"))] = p
                    soa_existing.add((striker, p.get("event")))
                elif "Anytime Goal Scorer" in market:
                    ags_picks_by_player[(striker, p.get("event"))] = p
                    ags_existing.add((striker, p.get("event")))
                elif "First Goal Scorer" in market:
                    fgs_existing.add((striker, p.get("event")))
                break
    # For each elite SoA pick without a matching AGS / FGS, create synthetics.
    import uuid
    for (striker, event), soa_pick in soa_picks_by_player.items():
        # Starter gate — don't synthesise AGS for a bench player.  Same
        # data source as the main boost path: STARTS count in the last
        # 45 days, scoped to the league kind of THIS event.
        kind = _classify_event_league_kind(event, soa_pick.get("league"))
        if not _is_actively_starting_soccer(striker, league_kind=kind):
            logger.info("Skipping synth AGS for %s @ %s — no recent %s starts",
                        striker, event, kind)
            continue
        soa_win = float(soa_pick.get("win_probability") or 0)
        soa_odds = float(soa_pick.get("book_odds") or -150)

        # ── Synthetic ANYTIME GOAL SCORER (~75% of SoA prob)
        if (striker, event) not in ags_existing:
            ags_win = round(soa_win * 0.75, 1)
            ags_odds = int(soa_odds + 40) if soa_odds < 0 else int(soa_odds * 1.3)
            ags_implied = round(
                (100 / (1 + (abs(ags_odds) / 100.0))) if ags_odds < 0
                else (100 / (1 + ags_odds / 100.0)), 1
            )
            synth = {**soa_pick}
            synth["id"] = str(uuid.uuid4())
            synth["market"] = f"{striker} Anytime Goal Scorer"
            synth["win_probability"] = ags_win
            synth["book_odds"] = ags_odds
            synth["implied_probability"] = ags_implied
            synth["edge_percent"] = round(ags_win - ags_implied, 2)
            synth["lock_score"] = round(min(99.0, max(ELITE_LOCK_FLOOR,
                float(soa_pick.get("lock_score") or 90) - 2)), 1)
            synth["synthetic_ags"] = True
            synth["elite_player"] = True
            synth["elite_player_name"] = striker
            synth["selection"] = "Yes"
            synth["key_insights"] = [
                f"⭐ Elite Striker Lock: {striker} Anytime Goal Scorer "
                f"(synthesized from book's To Score or Assist line)."
            ] + (soa_pick.get("key_insights") or [])
            picks.append(synth)
            ags_existing.add((striker, event))
            synth_added += 1

        # ── Synthetic FIRST GOAL SCORER (~25% of AGS prob, longer odds)
        if (striker, event) not in fgs_existing:
            # First-goal probability ≈ 25-30% of anytime-goal probability.
            fgs_win = round(soa_win * 0.75 * 0.28, 1)
            # First goal scorer markets typically price +400 to +700 for elites.
            fgs_odds = max(300, int(abs(soa_odds) * 2.5) if soa_odds < 0 else int(soa_odds * 3.5))
            fgs_implied = round(100 / (1 + fgs_odds / 100.0), 1)
            synth = {**soa_pick}
            synth["id"] = str(uuid.uuid4())
            synth["market"] = f"{striker} First Goal Scorer"
            synth["win_probability"] = fgs_win
            synth["book_odds"] = fgs_odds
            synth["implied_probability"] = fgs_implied
            synth["edge_percent"] = round(fgs_win - fgs_implied, 2)
            # First-goal-scorer is a lottery-ticket bet so keep lock_score
            # below Elite tier (max 92) even for elite players — match the
            # market's inherently higher variance.
            synth["lock_score"] = round(min(92.0,
                float(soa_pick.get("lock_score") or 88) - 5), 1)
            synth["synthetic_fgs"] = True
            synth["elite_player"] = True
            synth["elite_player_name"] = striker
            synth["selection"] = "Yes"
            synth["key_insights"] = [
                f"⭐ Elite Striker Lottery: {striker} First Goal Scorer "
                f"(synthesized; high-variance market — strikers like {striker} "
                f"score first in ~12-18% of starts)."
            ] + (soa_pick.get("key_insights") or [])
            picks.append(synth)
            fgs_existing.add((striker, event))
            synth_added += 1

    # ── Fallback: derive synthetic FGS + SoA from real AGS picks for elite
    # strikers when the book did NOT expose To Score or Assist (e.g. Norway
    # vs Senegal, France vs Iraq). Without this, Haaland/Mbappé would only
    # show Anytime Goal Scorer (1 pick) instead of the full triple-market.
    for (striker, event), ags_pick in ags_picks_by_player.items():
        ags_win = float(ags_pick.get("win_probability") or 0)
        ags_odds = float(ags_pick.get("book_odds") or -100)

        # ── Synthetic FIRST GOAL SCORER (~25% of AGS prob)
        if (striker, event) not in fgs_existing:
            fgs_win = round(ags_win * 0.28, 1)
            # FGS odds: roughly 3× longer than AGS for elite strikers
            if ags_odds < 0:
                fgs_odds = max(300, int(abs(ags_odds) * 3.0))
            else:
                fgs_odds = max(300, int(ags_odds * 2.5))
            fgs_implied = round(100 / (1 + fgs_odds / 100.0), 1)
            synth = {**ags_pick}
            synth["id"] = str(uuid.uuid4())
            synth["market"] = f"{striker} First Goal Scorer"
            synth["win_probability"] = fgs_win
            synth["book_odds"] = fgs_odds
            synth["implied_probability"] = fgs_implied
            synth["edge_percent"] = round(fgs_win - fgs_implied, 2)
            synth["lock_score"] = round(min(92.0,
                float(ags_pick.get("lock_score") or 88) - 3), 1)
            synth["synthetic_fgs"] = True
            synth["elite_player"] = True
            synth["elite_player_name"] = striker
            synth["selection"] = "Yes"
            synth["key_insights"] = [
                f"⭐ Elite Striker Lottery: {striker} First Goal Scorer "
                f"(synthesized from book's Anytime Goal Scorer line — "
                f"strikers like {striker} score first in ~12-18% of starts)."
            ] + (ags_pick.get("key_insights") or [])
            picks.append(synth)
            fgs_existing.add((striker, event))
            synth_added += 1

        # ── Synthetic TO SCORE OR ASSIST (~33% boost over AGS)
        # A striker who's anytime-goal at 50% is roughly 60-70% to score-or-
        # assist (assists ~20-30% of contributions for top strikers).
        if (striker, event) not in soa_existing:
            soa_win = round(min(85.0, ags_win * 1.33), 1)
            # SoA odds: tighter than AGS by ~50-70 American points.
            if ags_odds < 0:
                soa_odds = int(ags_odds - 60)  # more negative = chalkier
            else:
                soa_odds = max(-300, int(ags_odds * 0.6))  # cross to negative
            if soa_odds < 0:
                soa_implied = round(100 / (1 + (abs(soa_odds) / 100.0)) * (abs(soa_odds)/100.0) * (100/abs(soa_odds)), 1)
                # Simpler: implied = abs(odds) / (abs(odds) + 100) * 100
                soa_implied = round(abs(soa_odds) / (abs(soa_odds) + 100.0) * 100, 1)
            else:
                soa_implied = round(100 / (1 + soa_odds / 100.0), 1)
            synth = {**ags_pick}
            synth["id"] = str(uuid.uuid4())
            synth["market"] = f"{striker} To Score or Assist"
            synth["win_probability"] = soa_win
            synth["book_odds"] = soa_odds
            synth["implied_probability"] = soa_implied
            synth["edge_percent"] = round(soa_win - soa_implied, 2)
            synth["lock_score"] = round(min(99.0, max(ELITE_LOCK_FLOOR,
                float(ags_pick.get("lock_score") or 92) + 2)), 1)
            synth["synthetic_soa"] = True
            synth["elite_player"] = True
            synth["elite_player_name"] = striker
            synth["selection"] = "Yes"
            synth["key_insights"] = [
                f"⭐ Elite Striker Lock: {striker} To Score or Assist "
                f"(synthesized from book's Anytime Goal Scorer line — "
                f"adds ~20-30% probability for assists)."
            ] + (ags_pick.get("key_insights") or [])
            picks.append(synth)
            soa_existing.add((striker, event))
            synth_added += 1

    if synth_added:
        logger.info("Synthetic AGS+FGS+SoA picks added: %d", synth_added)

    # ── Final starter gate for Soccer scorer markets ──────────────
    # Suppress ANY goalscorer / to-score pick where our roster-verified
    # activity signal says the player hasn't started recently — even if
    # they aren't in the ELITE_STRIKERS list.  This catches Odds-API
    # feeds that publish scorer odds for every extended-squad player
    # (bench forwards, backups) which used to leak into the board as
    # nominally-priced picks.  Players with no historical data at all
    # still pass through (fail-open), so newcomers aren't false-negated.
    _SCORER_RE = re.compile(r"anytime\s+goal\s+scorer|to\s+score|first\s+goal\s+scorer", re.I)
    filtered_out = 0
    kept: list[dict] = []
    for p in picks:
        if (p.get("sport") or "") != "Soccer":
            kept.append(p)
            continue
        market = p.get("market") or ""
        if not _SCORER_RE.search(market):
            kept.append(p)
            continue
        # Extract player from "<Player> Anytime Goal Scorer" market string.
        m = re.match(r"^(.+?)\s+(?:Anytime\s+Goal\s+Scorer|To\s+Score(?:\s+or\s+Assist)?|First\s+Goal\s+Scorer)",
                     market, re.I)
        if not m:
            kept.append(p)
            continue
        player = m.group(1).strip()
        kind = _classify_event_league_kind(p.get("event"), p.get("league"))
        if _is_actively_starting_soccer(player, league_kind=kind):
            kept.append(p)
        else:
            filtered_out += 1
            logger.info("Starter-gate drop: %s @ %s (no recent %s starts)",
                        player, p.get("event", "?"), kind)
    if filtered_out:
        logger.info("Starter-gate suppressed %d Soccer scorer picks", filtered_out)
    return kept
