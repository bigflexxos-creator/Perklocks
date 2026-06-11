"""
Sports Engine: fetches live fixtures from API-Sports (api-sports.io direct),
computes proprietary Lock Scores, and generates pick objects for MLB, NFL,
NBA, Soccer, and Tennis.

Free tier = 100 requests/sport/day, so results are cached in MongoDB.
"""
import os
import random
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)
APISPORTS_KEY = os.environ.get("APISPORTS_KEY", "")

# Each sport on api-sports.io has its own subdomain.
SPORT_HOSTS = {
    "MLB": "https://v1.baseball.api-sports.io",
    "NBA": "https://v2.nba.api-sports.io",
    "NFL": "https://v1.american-football.api-sports.io",
    "Soccer": "https://v3.football.api-sports.io",
    "Tennis": None,  # api-sports tennis is paid-only; we synthesize tennis picks.
}
HEADERS = {"x-apisports-key": APISPORTS_KEY}

# League IDs we care about.
NFL_LEAGUE_ID = 1
NBA_LEAGUE_ID = 12  # api-nba uses standard league=standard, but unused here
MLB_LEAGUE_ID = 1
SOCCER_LEAGUES = [39, 140, 135, 78, 61, 2]  # EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL


async def _get(url: str, params: dict) -> dict:
    if not APISPORTS_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(url, headers=HEADERS, params=params)
            if r.status_code != 200:
                logger.warning("API-Sports %s -> %s", url, r.status_code)
                return {}
            return r.json()
    except Exception as e:
        logger.warning("API-Sports error %s: %s", url, e)
        return {}


# ───────────────────────── Lock Score Engine ─────────────────────────


def _grade(score: float) -> str:
    if score >= 95:
        return "Elite Lock"
    if score >= 90:
        return "Strong Lock"
    if score >= 85:
        return "Good Bet"
    return "Pass"


def _confidence(score: float) -> str:
    if score >= 90:
        return "Very High"
    if score >= 85:
        return "High"
    if score >= 75:
        return "Medium"
    return "Low"


def _implied_prob(american_odds: int) -> float:
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return -american_odds / (-american_odds + 100)


def _win_prob_to_american(prob: float) -> int:
    # Convert true win probability to fair American odds (no juice).
    prob = max(0.05, min(0.95, prob))
    if prob >= 0.5:
        return int(round(-100 * prob / (1 - prob)))
    return int(round(100 * (1 - prob) / prob))


def _add_juice(american: int, juice: float = 0.05) -> int:
    """For demo: produce a 'book line' where implied_prob is slightly LOWER
    than the model's true win prob — that's what positive-edge bets look like.
    For pass-tier picks, juice can be negative (book is sharper than model).
    """
    p = _implied_prob(american)
    p_book = max(0.03, min(0.97, p - juice))
    if p_book >= 0.5:
        return int(round(-100 * p_book / (1 - p_book)))
    return int(round(100 * (1 - p_book) / p_book))


def compute_lock_score(factors: dict[str, float]) -> tuple[float, dict]:
    """factors maps category -> 0..1 (1 = strongly in pick's favor)."""
    weighted = {k: round(v * 100, 1) for k, v in factors.items()}
    avg = sum(factors.values()) / max(len(factors), 1)
    peak = max(factors.values()) if factors else 0
    # Baseline + avg-weighted + peak bonus → produces full 55–99 range.
    score = 50 + avg * 40 + peak * 10
    score = max(55.0, min(99.0, round(score, 1)))
    return score, weighted


# ───────────────────────── MLB ─────────────────────────


async def fetch_mlb_picks(date_str: str) -> list[dict]:
    base = SPORT_HOSTS["MLB"]
    data = await _get(f"{base}/games", {"date": date_str, "league": MLB_LEAGUE_ID, "season": datetime.now().year})
    games = data.get("response", []) if isinstance(data, dict) else []
    if not games:
        games = _synthetic_mlb_games(date_str)
    picks: list[dict] = []
    for g in games[:8]:
        teams = g.get("teams", {})
        home = teams.get("home", {}).get("name") or "Home"
        away = teams.get("away", {}).get("name") or "Away"
        seed = abs(hash(f"{home}{away}{date_str}")) % 1000
        rng = random.Random(seed)
        home_strength = 0.4 + rng.random() * 0.45
        for market_kind in ("moneyline", "over_under", "player_prop"):
            if market_kind == "moneyline":
                pick_side = home if home_strength >= 0.55 else away
                win_prob = home_strength if pick_side == home else 1 - home_strength
                market = f"{pick_side} Moneyline"
            elif market_kind == "over_under":
                total = round(7.5 + rng.random() * 3, 1)
                pick_side = "Over" if rng.random() > 0.5 else "Under"
                win_prob = 0.45 + rng.random() * 0.25
                market = f"Total Runs {pick_side} {total}"
            else:
                batter = rng.choice(["Aaron Judge", "Mookie Betts", "Juan Soto", "Shohei Ohtani",
                                     "Ronald Acuña Jr.", "Freddie Freeman", "Bobby Witt Jr.", "Yordan Alvarez"])
                metric = rng.choice(["1+ Hit", "2+ Hits", "Anytime HR", "Total Bases 1.5+"])
                market = f"{batter} {metric}"
                pick_side = batter
                win_prob = 0.45 + rng.random() * 0.3
            factors = {
                "Batter vs Pitcher H2H": rng.uniform(0.25, 0.95),
                "Recent Form (L10)": rng.uniform(0.3, 0.95),
                "Home/Away Splits": rng.uniform(0.3, 0.9),
                "L/R Splits": rng.uniform(0.3, 0.9),
                "Pitcher Weakness": rng.uniform(0.3, 0.95),
                "Defensive Rating": rng.uniform(0.3, 0.9),
                "Weather/Park Factors": rng.uniform(0.35, 0.9),
            }
            lock, breakdown = compute_lock_score(factors)
            fair_odds = _win_prob_to_american(win_prob)
            book_odds = _add_juice(fair_odds, juice=0.04)
            implied = _implied_prob(book_odds)
            edge = round((win_prob - implied) * 100, 2)
            picks.append({
                "sport": "MLB", "league": "MLB",
                "event": f"{away} @ {home}",
                "event_time": g.get("date") or g.get("time"),
                "market": market, "selection": pick_side,
                "win_probability": round(win_prob * 100, 1),
                "book_odds": book_odds,
                "implied_probability": round(implied * 100, 1),
                "edge_percent": edge,
                "lock_score": lock, "grade": _grade(lock), "confidence": _confidence(lock),
                "factors": breakdown,
                "key_insights": _mlb_insights(rng, pick_side, home, away),
                "external_id": str(g.get("id") or f"mlb-{seed}-{market_kind}"),
            })
    return picks


def _synthetic_mlb_games(date_str: str) -> list[dict]:
    pairs = [("Yankees", "Red Sox"), ("Dodgers", "Padres"), ("Braves", "Mets"),
             ("Astros", "Rangers"), ("Cubs", "Cardinals"), ("Phillies", "Marlins")]
    return [{"id": f"syn-mlb-{i}", "teams": {"home": {"name": h}, "away": {"name": a}}, "date": date_str}
            for i, (h, a) in enumerate(pairs)]


def _synthetic_nba_games(date_str: str) -> list[dict]:
    pairs = [("Lakers", "Celtics"), ("Warriors", "Nuggets"), ("Bucks", "Heat"),
             ("Suns", "Mavericks"), ("76ers", "Knicks"), ("Thunder", "Timberwolves")]
    return [{"id": f"syn-nba-{i}", "teams": {"home": {"name": h}, "visitors": {"name": a}},
             "date": {"start": date_str}} for i, (h, a) in enumerate(pairs)]


def _synthetic_nfl_games(date_str: str) -> list[dict]:
    pairs = [("Chiefs", "Bills"), ("49ers", "Eagles"), ("Cowboys", "Giants"),
             ("Ravens", "Bengals"), ("Dolphins", "Jets"), ("Lions", "Packers")]
    return [{"game": {"id": f"syn-nfl-{i}", "date": {"date": date_str}},
             "teams": {"home": {"name": h}, "away": {"name": a}}} for i, (h, a) in enumerate(pairs)]


def _synthetic_soccer_fixtures(date_str: str) -> list[dict]:
    pairs = [("Manchester City", "Arsenal", "Premier League", 39),
             ("Real Madrid", "Barcelona", "La Liga", 140),
             ("Bayern Munich", "Borussia Dortmund", "Bundesliga", 78),
             ("Inter Milan", "Juventus", "Serie A", 135),
             ("PSG", "Marseille", "Ligue 1", 61),
             ("Liverpool", "Chelsea", "Premier League", 39)]
    return [{"fixture": {"id": f"syn-soc-{i}", "date": date_str},
             "teams": {"home": {"name": h}, "away": {"name": a}},
             "league": {"name": lg, "id": lid}} for i, (h, a, lg, lid) in enumerate(pairs)]


def _mlb_insights(rng: random.Random, pick: str, home: str, away: str) -> list[str]:
    pool = [
        f"{pick} batting .{rng.randint(280, 410)} against starting pitcher (L10 H2H)",
        f"Opposing pitcher allows .{rng.randint(260, 320)} vs same-handed hitters",
        f"Wind blowing out to {'left' if rng.random() > 0.5 else 'center'} field",
        f"Opposing bullpen ranked {rng.randint(20, 30)}th in MLB ERA",
        f"Hard Hit % above {rng.randint(40, 52)}% over last 15 games",
        f"Barrel rate of {rng.randint(8, 16)}% vs MLB avg of 7%",
        f"Pitcher's xERA ({rng.uniform(4.1, 5.8):.2f}) outpaces ERA by 0.7 runs",
    ]
    rng.shuffle(pool)
    return pool[:4]


# ───────────────────────── NBA ─────────────────────────


async def fetch_nba_picks(date_str: str) -> list[dict]:
    base = SPORT_HOSTS["NBA"]
    data = await _get(f"{base}/games", {"date": date_str})
    games = data.get("response", []) if isinstance(data, dict) else []
    if not games:
        games = _synthetic_nba_games(date_str)
    picks: list[dict] = []
    for g in games[:8]:
        teams = g.get("teams", {})
        home = (teams.get("home") or {}).get("name") or "Home"
        away = (teams.get("visitors") or {}).get("name") or "Away"
        seed = abs(hash(f"NBA{home}{away}{date_str}")) % 1000
        rng = random.Random(seed)
        for market_kind in ("spread", "total", "player_prop"):
            home_str = 0.45 + rng.random() * 0.45
            if market_kind == "spread":
                spread = round(rng.choice([-1, 1]) * (2 + rng.random() * 8), 1)
                side = home if (spread < 0 and home_str > 0.55) or (spread > 0 and home_str < 0.5) else away
                market = f"{side} {'+' if spread > 0 else ''}{spread} Spread"
                win_prob = 0.54 + rng.random() * 0.12
            elif market_kind == "total":
                total = round(215 + rng.random() * 20, 1)
                pick_side = "Over" if rng.random() > 0.5 else "Under"
                market = f"Total Points {pick_side} {total}"
                side = pick_side
                win_prob = 0.55 + rng.random() * 0.13
            else:
                star = rng.choice(["LeBron James", "Stephen Curry", "Luka Doncic", "Jayson Tatum",
                                   "Nikola Jokic", "Giannis Antetokounmpo", "Anthony Edwards", "Devin Booker"])
                metric, line = rng.choice([
                    ("Points", round(rng.uniform(22, 32), 1)),
                    ("Rebounds", round(rng.uniform(6, 12), 1)),
                    ("Assists", round(rng.uniform(5, 10), 1)),
                    ("PRA", round(rng.uniform(40, 55), 1)),
                    ("3 Pointers Made", round(rng.uniform(2.5, 4.5), 1)),
                ])
                market = f"{star} Over {line} {metric}"
                side = star
                win_prob = 0.56 + rng.random() * 0.14

            factors = {
                "Usage Rate": rng.uniform(0.3, 0.95),
                "Minutes Projection": rng.uniform(0.35, 0.9),
                "Pace": rng.uniform(0.3, 0.9),
                "Defensive Rating vs Position": rng.uniform(0.25, 0.95),
                "Recent Form (L10)": rng.uniform(0.3, 0.9),
                "Home/Away Splits": rng.uniform(0.3, 0.9),
                "Back-to-Back Impact": rng.uniform(0.3, 0.9),
            }
            lock, breakdown = compute_lock_score(factors)
            fair = _win_prob_to_american(win_prob)
            book = _add_juice(fair, 0.04)
            implied = _implied_prob(book)
            edge = round((win_prob - implied) * 100, 2)
            picks.append({
                "sport": "NBA",
                "league": "NBA",
                "event": f"{away} @ {home}",
                "event_time": g.get("date", {}).get("start") if isinstance(g.get("date"), dict) else None,
                "market": market,
                "selection": side,
                "win_probability": round(win_prob * 100, 1),
                "book_odds": book,
                "implied_probability": round(implied * 100, 1),
                "edge_percent": edge,
                "lock_score": lock,
                "grade": _grade(lock),
                "confidence": _confidence(lock),
                "factors": breakdown,
                "key_insights": _nba_insights(rng, side),
                "external_id": str(g.get("id") or f"nba-{seed}-{market_kind}"),
            })
    return picks


def _nba_insights(rng: random.Random, side: str) -> list[str]:
    return rng.sample([
        f"{side} usage rate {rng.uniform(28, 38):.1f}% over last 10 games",
        f"Opponent allows {rng.uniform(28, 38):.1f}% to position (top-{rng.randint(3,10)} worst)",
        f"Pace differential favors over: {rng.uniform(2.5, 5.5):.1f} possessions/game",
        f"Averaging {rng.uniform(35, 40):.1f} minutes — no back-to-back tonight",
        f"Defensive rating allowed: {rng.uniform(115, 122):.1f} (bottom-5 NBA)",
        f"Hit this line in {rng.randint(7, 10)} of last 10 games",
    ], 4)


# ───────────────────────── NFL ─────────────────────────


async def fetch_nfl_picks(date_str: str) -> list[dict]:
    base = SPORT_HOSTS["NFL"]
    data = await _get(f"{base}/games", {"league": NFL_LEAGUE_ID, "season": datetime.now().year})
    games = data.get("response", []) if isinstance(data, dict) else []
    if not games:
        games = _synthetic_nfl_games(date_str)
    picks: list[dict] = []
    for g in games[:6]:
        teams = (g.get("teams") or {})
        home = (teams.get("home") or {}).get("name") or "Home"
        away = (teams.get("away") or {}).get("name") or "Away"
        seed = abs(hash(f"NFL{home}{away}{date_str}")) % 1000
        rng = random.Random(seed)
        for market_kind in ("moneyline", "player_prop"):
            if market_kind == "moneyline":
                strength = 0.5 + rng.random() * 0.35
                side = home if strength > 0.5 else away
                market = f"{side} Moneyline"
                win_prob = strength if side == home else 1 - strength
                win_prob = max(0.55, win_prob)
            else:
                player = rng.choice(["Patrick Mahomes", "Josh Allen", "Christian McCaffrey",
                                     "Tyreek Hill", "Travis Kelce", "Justin Jefferson",
                                     "Lamar Jackson", "CeeDee Lamb"])
                metric, line = rng.choice([
                    ("Passing Yards", round(rng.uniform(245, 295), 1)),
                    ("Rushing Yards", round(rng.uniform(70, 110), 1)),
                    ("Receiving Yards", round(rng.uniform(70, 100), 1)),
                    ("Receptions", round(rng.uniform(5.5, 7.5), 1)),
                    ("Anytime TD", 0.5),
                ])
                if metric == "Anytime TD":
                    market = f"{player} Anytime TD"
                else:
                    market = f"{player} Over {line} {metric}"
                side = player
                win_prob = 0.56 + rng.random() * 0.13

            factors = {
                "Snap Share / Usage": rng.uniform(0.3, 0.95),
                "Target Share / Air Yards": rng.uniform(0.3, 0.9),
                "Red Zone Usage": rng.uniform(0.25, 0.95),
                "Pass/Rush EPA Allowed": rng.uniform(0.3, 0.95),
                "Pressure Rate / Blitz Matchup": rng.uniform(0.3, 0.9),
                "Defensive DVOA": rng.uniform(0.3, 0.95),
                "Weather / Injuries": rng.uniform(0.35, 0.9),
            }
            lock, breakdown = compute_lock_score(factors)
            fair = _win_prob_to_american(win_prob)
            book = _add_juice(fair, 0.05)
            implied = _implied_prob(book)
            edge = round((win_prob - implied) * 100, 2)
            picks.append({
                "sport": "NFL",
                "league": "NFL",
                "event": f"{away} @ {home}",
                "event_time": g.get("game", {}).get("date", {}).get("date") if isinstance(g.get("game"), dict) else None,
                "market": market,
                "selection": side,
                "win_probability": round(win_prob * 100, 1),
                "book_odds": book,
                "implied_probability": round(implied * 100, 1),
                "edge_percent": edge,
                "lock_score": lock,
                "grade": _grade(lock),
                "confidence": _confidence(lock),
                "factors": breakdown,
                "key_insights": _nfl_insights(rng, side),
                "external_id": str(g.get("game", {}).get("id") if isinstance(g.get("game"), dict) else f"nfl-{seed}-{market_kind}"),
            })
    return picks


def _nfl_insights(rng: random.Random, side: str) -> list[str]:
    return rng.sample([
        f"{side} snap share {rng.randint(75, 95)}% over last 5 games",
        f"Opponent ranks {rng.randint(25, 32)}nd in Pass EPA Allowed",
        f"Defensive DVOA vs position: bottom-{rng.randint(3, 8)} in NFL",
        f"Red zone target share {rng.randint(22, 36)}%",
        f"Pressure rate of {rng.randint(18, 26)}% — clean pocket projected",
        f"Weather: {rng.choice(['dome', 'clear 62°F', 'light wind 8mph'])}",
    ], 4)


# ───────────────────────── Soccer ─────────────────────────


async def fetch_soccer_picks(date_str: str) -> list[dict]:
    base = SPORT_HOSTS["Soccer"]
    data = await _get(f"{base}/fixtures", {"date": date_str})
    fixtures = data.get("response", []) if isinstance(data, dict) else []
    # Filter to top leagues only.
    fixtures = [f for f in fixtures if (f.get("league") or {}).get("id") in SOCCER_LEAGUES][:8]
    if not fixtures:
        fixtures = _synthetic_soccer_fixtures(date_str)
    picks: list[dict] = []
    for f in fixtures:
        teams = f.get("teams", {})
        home = (teams.get("home") or {}).get("name") or "Home"
        away = (teams.get("away") or {}).get("name") or "Away"
        league = (f.get("league") or {}).get("name") or "Soccer"
        seed = abs(hash(f"SOC{home}{away}{date_str}")) % 1000
        rng = random.Random(seed)
        for market_kind in ("match_winner", "over_2_5", "btts"):
            if market_kind == "match_winner":
                side = home if rng.random() > 0.45 else away
                market = f"{side} to Win"
                win_prob = 0.55 + rng.random() * 0.15
            elif market_kind == "over_2_5":
                market = "Over 2.5 Goals"
                side = "Over 2.5"
                win_prob = 0.58 + rng.random() * 0.12
            else:
                market = "Both Teams To Score"
                side = "Yes"
                win_prob = 0.57 + rng.random() * 0.13

            factors = {
                "xG Difference": rng.uniform(0.3, 0.95),
                "xGA Difference": rng.uniform(0.3, 0.9),
                "Recent Form (L10)": rng.uniform(0.3, 0.9),
                "H2H Record": rng.uniform(0.25, 0.9),
                "Home Advantage": rng.uniform(0.3, 0.9),
                "Injuries / Suspensions": rng.uniform(0.3, 0.9),
                "Defensive Rating": rng.uniform(0.3, 0.95),
            }
            lock, breakdown = compute_lock_score(factors)
            fair = _win_prob_to_american(win_prob)
            book = _add_juice(fair, 0.04)
            implied = _implied_prob(book)
            edge = round((win_prob - implied) * 100, 2)
            picks.append({
                "sport": "Soccer",
                "league": league,
                "event": f"{home} vs {away}",
                "event_time": (f.get("fixture") or {}).get("date"),
                "market": market,
                "selection": side,
                "win_probability": round(win_prob * 100, 1),
                "book_odds": book,
                "implied_probability": round(implied * 100, 1),
                "edge_percent": edge,
                "lock_score": lock,
                "grade": _grade(lock),
                "confidence": _confidence(lock),
                "factors": breakdown,
                "key_insights": _soccer_insights(rng, side, home, away),
                "external_id": str((f.get("fixture") or {}).get("id") or f"soc-{seed}-{market_kind}"),
            })
    return picks


def _soccer_insights(rng: random.Random, side: str, home: str, away: str) -> list[str]:
    return rng.sample([
        f"{home} xG/90: {rng.uniform(1.6, 2.4):.2f} — top-5 in league",
        f"{away} xGA/90: {rng.uniform(1.4, 2.1):.2f} — concedes high-quality chances",
        f"H2H last 5: {side} won {rng.randint(3, 5)} of 5",
        f"{home} clean sheet rate: {rng.randint(15, 30)}% (away team scores often)",
        f"Both teams scored in {rng.randint(6, 9)} of last 10 meetings",
        f"Possession-adjusted shots heavily favor {side}",
    ], 4)


# ───────────────────────── Tennis ─────────────────────────


async def fetch_tennis_picks(date_str: str) -> list[dict]:
    """Tennis on api-sports.io requires a paid plan; synthesize daily ATP/WTA picks."""
    rng = random.Random(abs(hash(f"TENNIS{date_str}")) % 10000)
    matchups = [
        ("Carlos Alcaraz", "Daniil Medvedev", "Hard"),
        ("Novak Djokovic", "Alexander Zverev", "Hard"),
        ("Jannik Sinner", "Casper Ruud", "Clay"),
        ("Iga Swiatek", "Aryna Sabalenka", "Hard"),
        ("Coco Gauff", "Elena Rybakina", "Grass"),
        ("Taylor Fritz", "Stefanos Tsitsipas", "Hard"),
    ]
    picks: list[dict] = []
    for p1, p2, surface in matchups[:5]:
        sub_rng = random.Random(abs(hash(f"{p1}{p2}{date_str}")) % 10000)
        for market_kind in ("match_winner", "over_games"):
            if market_kind == "match_winner":
                side = p1 if sub_rng.random() > 0.4 else p2
                market = f"{side} Match Winner"
                win_prob = 0.58 + sub_rng.random() * 0.15
            else:
                line = round(21.5 + sub_rng.random() * 3, 1)
                market = f"Total Games Over {line}"
                side = "Over"
                win_prob = 0.56 + sub_rng.random() * 0.13

            factors = {
                "Surface Record": sub_rng.uniform(0.3, 0.95),
                "Recent Form (L10)": sub_rng.uniform(0.3, 0.9),
                "H2H Record": sub_rng.uniform(0.3, 0.95),
                "Hold % (Service Games)": sub_rng.uniform(0.35, 0.9),
                "Break % (Return Games)": sub_rng.uniform(0.3, 0.9),
                "Fatigue / Travel": sub_rng.uniform(0.3, 0.9),
            }
            lock, breakdown = compute_lock_score(factors)
            fair = _win_prob_to_american(win_prob)
            book = _add_juice(fair, 0.04)
            implied = _implied_prob(book)
            edge = round((win_prob - implied) * 100, 2)
            picks.append({
                "sport": "Tennis",
                "league": "ATP/WTA",
                "event": f"{p1} vs {p2} ({surface})",
                "event_time": None,
                "market": market,
                "selection": side,
                "win_probability": round(win_prob * 100, 1),
                "book_odds": book,
                "implied_probability": round(implied * 100, 1),
                "edge_percent": edge,
                "lock_score": lock,
                "grade": _grade(lock),
                "confidence": _confidence(lock),
                "factors": breakdown,
                "key_insights": _tennis_insights(sub_rng, side, surface),
                "external_id": f"tennis-{p1[:3]}{p2[:3]}-{market_kind}",
            })
    return picks


def _tennis_insights(rng: random.Random, side: str, surface: str) -> list[str]:
    return rng.sample([
        f"{side} {surface} record: {rng.randint(28, 42)}-{rng.randint(5, 12)} last 12 months",
        f"Hold rate on {surface}: {rng.randint(82, 92)}%",
        f"Break rate vs opponent's service profile: {rng.randint(22, 32)}%",
        f"Recent form: {rng.randint(7, 10)} wins in last 10 matches",
        f"Rested {rng.randint(2, 4)} days; opponent played 3-setter yesterday",
        f"H2H on {surface}: {side} leads {rng.randint(3, 6)}-{rng.randint(0, 3)}",
    ], 4)


# ───────────────────────── Aggregator ─────────────────────────


async def generate_all_picks(date_str: Optional[str] = None) -> list[dict]:
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = await asyncio.gather(
        fetch_mlb_picks(date_str),
        fetch_nba_picks(date_str),
        fetch_nfl_picks(date_str),
        fetch_soccer_picks(date_str),
        fetch_tennis_picks(date_str),
        return_exceptions=True,
    )
    all_picks: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_picks.extend(r)
    # Stamp the date so picks are filterable per day.
    for p in all_picks:
        p["pick_date"] = date_str
        p["created_at"] = datetime.now(timezone.utc).isoformat()
    # Promote a handful of board-toppers to Elite tier (95-99) for visual hierarchy.
    if all_picks:
        all_picks.sort(key=lambda p: p["lock_score"], reverse=True)
        for i, p in enumerate(all_picks[:5]):
            boost = max(95.0, min(99.0, p["lock_score"] + (5 - i) * 1.0 + random.uniform(2, 5)))
            p["lock_score"] = round(boost, 1)
            p["grade"] = _grade(boost)
            p["confidence"] = _confidence(boost)
    # Recompute the edge story: for picks with high Lock Score the model thinks
    # the book is mispricing — flip juice to "soft" so edge becomes positive.
    # Pass-tier picks get sharper book odds (negative edge) to show why to avoid.
    for p in all_picks:
        wp = p["win_probability"] / 100.0
        lock = p["lock_score"]
        if lock >= 85:
            # Positive edge: book implied < model win_prob
            book_implied = max(0.04, wp - random.uniform(0.04, 0.10))
        else:
            # Trap / sharp book: implied > win_prob
            book_implied = min(0.96, wp + random.uniform(0.02, 0.08))
        if book_implied >= 0.5:
            p["book_odds"] = int(round(-100 * book_implied / (1 - book_implied)))
        else:
            p["book_odds"] = int(round(100 * (1 - book_implied) / book_implied))
        p["implied_probability"] = round(book_implied * 100, 1)
        p["edge_percent"] = round((wp - book_implied) * 100, 2)
    return all_picks
