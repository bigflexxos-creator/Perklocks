"""ESPN-backed soccer leg settler.

The football-data.org free tier doesn't expose goal-scorer events on
/v4/matches/{id}, so we can't settle "Anytime Goal Scorer" props from
the same source the pipeline uses for predictions. ESPN's public soccer
APIs DO expose goal events (in `keyEvents`) and final scores across a
broad set of leagues — they're our pragmatic settlement source.

Supported markets:
  • Moneyline (incl. "Draw" selection)
  • Win or Draw / Double Chance
  • Total Goals Over/Under
  • Both Teams to Score (BTTS) Yes/No
  • Anytime Goal Scorer

Returns "won" / "lost" / "push" / None.  None means we couldn't
positively identify the match or scorer — caller should leave the leg
pending.

Designed to be permissive about team naming because ESPN, football-data,
and the Odds API all spell some teams differently (e.g. "Operário PR"
vs "Operario PR", "Goiás" vs "Goias").
"""
from __future__ import annotations

import logging
import re
import unicodedata as _ud
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.soccer_espn_settle")

# ESPN scoreboard / summary base.
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 12.0

# League slugs ESPN exposes for soccer. Ordered so the most-common
# competitions resolve first. We iterate until we hit the team pair.
# (We intentionally avoid hitting every slug in the world — these cover
#  ~95% of the props the platform surfaces.)
_LEAGUES = [
    # FIFA + UEFA international
    "fifa.world", "fifa.friendly", "fifa.confederations",
    "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "uefa.euro", "uefa.nations",
    "concacaf.gold", "conmebol.copa_america", "conmebol.libertadores",
    "afc.asian", "caf.nations",
    # Big-5 + secondary EU leagues
    "eng.1", "eng.2",
    "esp.1", "esp.2",
    "ger.1", "ger.2",
    "ita.1", "ita.2",
    "fra.1", "fra.2",
    "ned.1", "por.1", "tur.1", "bel.1", "sco.1", "gre.1", "rus.1",
    # Americas
    "usa.1", "usa.2",
    "mex.1", "mex.2",
    "bra.1", "bra.2",
    "arg.1", "chi.1", "col.1", "uru.1", "par.1", "ecu.1", "per.1",
    # Asia/AUS
    "aus.1", "jpn.1", "kor.1", "chn.1",
    # Misc
    "cup.world.club",
]

# Negative cache for league codes that returned 4xx to avoid retrying them
# repeatedly in the same process. Cleared when the process restarts.
_DEAD_LEAGUES: set[str] = set()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
# Explicit transliteration for characters that NFD normalization DOESN'T
# decompose. These are fully-composed glyphs (Unicode category "Ll"/"Lu"
# rather than "Mn"), so the standard `unicodedata.normalize("NFD", s)`
# followed by combining-mark strip leaves them untouched. Without this
# table "Bodø/Glimt" (in-pipe) never equals "Bodo/Glimt" (ESPN feed),
# and every Norwegian/Icelandic team stayed pending forever.
_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O",   # Norwegian / Danish
    "æ": "ae", "Æ": "AE",  # Norwegian / Danish / Icelandic / Old English
    "å": "a", "Å": "A",   # Swedish / Norwegian / Danish / Finnish
    "ð": "d", "Ð": "D",   # Icelandic / Faroese
    "þ": "th", "Þ": "TH",  # Icelandic
    "ß": "ss",             # German (safety)
    "ł": "l", "Ł": "L",   # Polish (safety)
})

# Nordic team-name aliases. Norwegian / Swedish clubs use unofficial
# abbreviations in some feeds ("HamKam" for Hamarkameratene, "MFF" for
# Malmö FF, "AIK" for AIK Solna) that no amount of accent stripping /
# substring matching can resolve. Populated from Eliteserien + Allsvenskan
# official rosters. Key = alias (lowercase, accent-stripped); Value =
# canonical ESPN displayName (lowercase, accent-stripped).
_TEAM_ALIASES: dict[str, str] = {
    # Norway (Eliteserien)
    "hamkam":      "hamarkameratene",
    "kfum":        "kfum oslo",
    "brann":       "sk brann",
    "start":       "ik start",
    "viking":      "viking fk",
    "sarpsborg":   "sarpsborg fk",
    "sarpsborg 08": "sarpsborg fk",
    "kbk":         "kristiansund bk",
    "kristiansund": "kristiansund bk",
    "molde":       "molde fk",
    "rbk":         "rosenborg",
    "rosenborg bk": "rosenborg",
    "godset":      "strømsgodset",
    "stromsgodset": "strømsgodset",
    "haugesund":   "fk haugesund",
    "tromso":      "tromsø",
    "bodo glimt":  "bodo/glimt",
    "bodo/glimt":  "bodo/glimt",
    "fredrikstad": "fredrikstad fk",
    # Sweden (Allsvenskan)
    "aik":         "aik solna",
    "djurgardens if": "djurgarden",
    "djurgarden if": "djurgarden",
    "hammarby if": "hammarby",
    "hammarby": "hammarby",
    "malmo ff":    "malmo ff",
    "mff":         "malmo ff",
    "goteborg":    "ifk goteborg",
    "ifk gbg":     "ifk goteborg",
    "elfsborg":    "if elfsborg",
    "hacken":      "bk hacken",
    "brommapojkarna": "if brommapojkarna",
    "sirius":      "ik sirius",
    "halmstad":    "halmstads bk",
    "halmstads":   "halmstads bk",
    "mjallby":     "mjallby aif",
    "orebro":      "orebro sk",
    "norrkoping":  "ifk norrkoping",
    "varnamo":     "ifk varnamo",
    "vasteras":    "vasteras sk",
    "gais":        "gais goteborg",
    # Denmark (Superliga) — same alias-heavy style
    "fck":         "fc copenhagen",
    "brondby":     "brondby if",
    "midtjylland": "fc midtjylland",
    "nordsjaelland": "fc nordsjaelland",
    "silkeborg":   "silkeborg if",
    "aalborg":     "aab aalborg",
    "aab":         "aab aalborg",
    "randers":     "randers fc",
    "viborg":      "viborg ff",
    # Finland (Veikkausliiga) — light coverage
    "hjk":         "hjk helsinki",
    "kupsu":       "kups kuopio",
    "kups":        "kups kuopio",
    "haka":        "fc haka",
    "inter turku": "fc inter turku",
}


def _norm(s: str) -> str:
    """Lower + Nordic transliterate + NFD accent-strip + alnum-only-with-space.

    Two-stage normalization (2026-07-13 fix — user report "history not
    grading Norway/Sweden goalscorers"):
      1. Explicit transliteration for precomposed glyphs that don't
         decompose under NFD (ø → o, æ → ae, å → a, ð → d, þ → th, ß → ss).
      2. NFD-strip for the traditional Latin diacritics
         (é → e, ü → u, ñ → n, ó → o, etc.).
    """
    if not s:
        return ""
    s = s.translate(_TRANSLIT)
    s = "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")
    return s.strip().lower()


def _resolve_alias(name: str) -> str:
    """Return the canonical name if `name` is a known Nordic alias,
    else the input unchanged. Both keys and values are already
    accent-stripped and lowercased by _norm.
    """
    return _TEAM_ALIASES.get(name, name)


def _names_match(a: str, b: str) -> bool:
    """Tolerant name match.

    Treats "Operário PR" == "Operario PR", "Manchester United" == "Man United",
    "Goiás" == "Goias", "São Bernardo" == "Sao Bernardo",
    "Bodø/Glimt" == "Bodo/Glimt", "HamKam" == "Hamarkameratene".

    Strategy: transliterate + accent-strip both, resolve Nordic
    aliases, then accept exact match, OR substring match in either
    direction once we trim common suffixes (FC, CF, EC, AC, SC, AFC,
    CFC, etc.) and any leading article ("Os ", "El ", "AL-").
    """
    if not a or not b:
        return False
    na, nb = _resolve_alias(_norm(a)), _resolve_alias(_norm(b))
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Strip common club-suffix noise to compare cores.
    suffix_pat = re.compile(r"\b(fc|cf|ec|ac|sc|afc|cfc|sk|fk|ud|cd|fk|de|do|da)\b\.?", re.IGNORECASE)
    a2 = suffix_pat.sub("", na).strip()
    b2 = suffix_pat.sub("", nb).strip()
    a2 = re.sub(r"\s+", " ", a2)
    b2 = re.sub(r"\s+", " ", b2)
    if a2 and b2 and (a2 == b2):
        return True
    # Substring containment (one is a longer form of the other).
    if len(a2) >= 4 and len(b2) >= 4:
        if a2 in b2 or b2 in a2:
            return True
    # Token-overlap as last resort — share >=70% tokens (e.g. "ilves tampere" vs "ilves").
    ta = set(a2.split())
    tb = set(b2.split())
    if ta and tb:
        common = ta & tb
        if len(common) >= 1 and len(common) / max(len(ta), len(tb)) >= 0.5:
            return True
    return False


def _parse_event_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _candidate_dates(event_time_iso: Optional[str]) -> list[str]:
    """Return YYYYMMDD strings to scan on ESPN for a given event time.

    We try the event-time date plus ±1 day to cover late-finishing
    matches that roll past midnight UTC and any timezone drift between
    the parlay snapshot and ESPN's UTC-day grouping.
    """
    dt = _parse_event_iso(event_time_iso) or datetime.now(timezone.utc)
    dates = []
    for delta in (0, -1, 1):
        d = (dt + timedelta(days=delta)).strftime("%Y%m%d")
        if d not in dates:
            dates.append(d)
    return dates


async def _http_get(url: str, params: dict | None = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
            r = await cx.get(url, params=params or {},
                             headers={"User-Agent": _UA, "Accept": "application/json"})
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if 400 <= r.status_code < 500:
            return None
    except Exception as e:
        logger.debug("ESPN GET failed %s: %s", url, e)
    return None


async def _find_event(home_team: str, away_team: str,
                      event_time_iso: Optional[str]) -> Optional[tuple[str, dict]]:
    """Return (league_slug, event_dict) for the first ESPN event that
    matches both teams within ±1 day of `event_time_iso`. None if not
    found across every supported league."""
    dates = _candidate_dates(event_time_iso)
    for league in _LEAGUES:
        if league in _DEAD_LEAGUES:
            continue
        for ds in dates:
            url = f"{_ESPN_BASE}/{league}/scoreboard"
            data = await _http_get(url, {"dates": ds})
            if data is None:
                # Mark known-dead league slugs so we don't keep hammering them.
                _DEAD_LEAGUES.add(league)
                break  # 4xx → skip remaining dates for this league
            events = data.get("events") or []
            for ev in events:
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors") or []
                if len(competitors) < 2:
                    continue
                # ESPN puts home first usually; verify with homeAway flag.
                home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
                hname = ((home_c.get("team") or {}).get("displayName") or "")
                aname = ((away_c.get("team") or {}).get("displayName") or "")
                if _names_match(hname, home_team) and _names_match(aname, away_team):
                    return league, ev
    return None


async def _fetch_summary(league: str, event_id: str) -> Optional[dict]:
    url = f"{_ESPN_BASE}/{league}/summary"
    return await _http_get(url, {"event": str(event_id)})


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────
async def settle_soccer_leg(leg: dict) -> Optional[str]:
    """Best-effort soccer leg settle from ESPN. See module docstring."""
    event = (leg.get("event") or "").strip()
    market = (leg.get("market") or "").strip()
    selection = (leg.get("selection") or "").strip()
    event_time = leg.get("event_time") or leg.get("commence_time")
    if not event or not market:
        return None
    parts = re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return None
    away_team, home_team = parts[0].strip(), parts[1].strip()

    found = await _find_event(home_team, away_team, event_time)
    if not found:
        return None
    league, ev = found

    # Only settle when match is full-time / finished.
    comp = (ev.get("competitions") or [{}])[0]
    status = ((comp.get("status") or {}).get("type") or {}).get("name") or ""
    if status not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET",
                      "STATUS_FINAL_PEN", "STATUS_FORFEIT"):
        return None

    # Pull final scores.
    competitors = comp.get("competitors") or []
    home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
    try:
        home_goals = int(home_c.get("score"))
        away_goals = int(away_c.get("score"))
    except (TypeError, ValueError):
        return None
    total_goals = home_goals + away_goals
    market_lower = market.lower()

    # ─── Anytime Goal Scorer ─────────────────────────────────────────
    if "goal scorer" in market_lower or "to score or assist" in market_lower or "score & assist" in market_lower:
        summary = await _fetch_summary(league, ev.get("id"))
        result: Optional[str] = None
        if summary:
            result = _settle_scorer_market(
                summary, selection, market_lower,
                total_goals=total_goals,
            )
        # ── FotMob PRIMARY for Nordic + fallback everywhere else ──
        # ESPN's goal-scorer detail is unreliable for Allsvenskan /
        # Eliteserien / Superliga / Veikkausliiga — they routinely
        # publish "Kristian Stromland Lien" one match and just the
        # score another. FotMob has universal Nordic coverage from
        # first-party feeds. So for Nordic leagues we ALWAYS consult
        # FotMob (and prefer its answer when it differs from ESPN).
        # For other leagues, FotMob is only consulted when ESPN can't
        # verify (result is None).
        is_nordic = any(k in (league or "").lower() for k in (
            "nor.1", "swe.1", "den.1", "fin.1",
            "allsvenskan", "eliteserien", "superligaen", "veikkausliiga",
        ))
        if is_nordic or result is None:
            try:
                from soccer_fotmob_settle import settle_soccer_leg as _fotmob
                leg = {
                    "sport":      "Soccer",
                    "event":      f"{away_team} @ {home_team}",
                    "market":     market,
                    "selection":  selection,
                    "event_time": event_time,
                }
                fot_result = await _fotmob(leg)
                if fot_result in ("won", "lost", "push"):
                    if result and result != fot_result:
                        logger.warning(
                            "Nordic grade override — ESPN=%s → FotMob=%s for %s (%s)",
                            result, fot_result, selection, market,
                        )
                    return fot_result
            except Exception as e:
                logger.debug("FotMob primary/fallback failed for %s: %s", selection, e)
        return result

    # ─── Moneyline ───────────────────────────────────────────────────
    if "moneyline" in market_lower:
        if not selection:
            return None
        sel = _norm(selection)
        if _names_match(selection, home_team):
            return "won" if home_goals > away_goals else "lost"
        if _names_match(selection, away_team):
            return "won" if away_goals > home_goals else "lost"
        if "draw" in sel:
            return "won" if home_goals == away_goals else "lost"
        return None

    # ─── Win or Draw / Double Chance ─────────────────────────────────
    if "win or draw" in market_lower or "double chance" in market_lower:
        # Selection may be a team OR a phrase like "Home or Draw".
        sel = selection or market
        if _names_match(sel, home_team) or "home" in _norm(sel).split() or _norm(sel).startswith("1x"):
            return "won" if home_goals >= away_goals else "lost"
        if _names_match(sel, away_team) or "away" in _norm(sel).split() or _norm(sel).startswith("x2"):
            return "won" if away_goals >= home_goals else "lost"
        # Phrase "Home or Away" (12) is unusual but support it.
        if _norm(sel) in ("12", "home or away", "away or home"):
            return "won" if home_goals != away_goals else "lost"
        return None

    # ─── Both Teams to Score ─────────────────────────────────────────
    if "both teams to score" in market_lower or "btts" in market_lower:
        is_yes = ("yes" in _norm(selection) or "yes" in market_lower) and "no" not in _norm(selection)
        is_no = _norm(selection) == "no" or " no " in f" {market_lower} "
        btts = (home_goals > 0 and away_goals > 0)
        if is_yes:
            return "won" if btts else "lost"
        if is_no:
            return "won" if not btts else "lost"
        return None

    # ─── Total Goals Over/Under ──────────────────────────────────────
    if "total goals" in market_lower or "total" in market_lower or (
        "goals" in market_lower and ("over" in market_lower or "under" in market_lower)
    ):
        m_line = re.search(r"(\d+(?:\.\d+)?)", market)
        if not m_line:
            return None
        try:
            line = float(m_line.group(1))
        except ValueError:
            return None
        is_over = "over" in market_lower or "over" in _norm(selection)
        is_under = "under" in market_lower or "under" in _norm(selection)
        if is_over:
            if total_goals > line:
                return "won"
            if total_goals < line:
                return "lost"
            return "push"
        if is_under:
            if total_goals < line:
                return "won"
            if total_goals > line:
                return "lost"
            return "push"
        return None

    return None


# ──────────────────────────────────────────────────────────────────────
# Goal-scorer settlement helpers
# ──────────────────────────────────────────────────────────────────────
def _settle_scorer_market(
    summary: dict, selection: str, market_lower: str,
    *, total_goals: int = -1,
) -> Optional[str]:
    """Resolve goal-scorer style markets from an ESPN /summary payload.

    The `keyEvents` array contains every meaningful event; goals are
    flagged with `scoringPlay: true` and we can read the scorer name
    out of `participants[].athlete.displayName` or fall back to the
    natural-language `text` field.

    Returns:
      "won" / "lost" — confidently graded
      None           — cannot verify (caller should try FotMob fallback)

    Ambiguity guard (2026-07-13): ESPN doesn't publish goal-scorer
    detail for Allsvenskan / Eliteserien / lower-tier competitions.
    When the scoreboard shows the match had goals but keyEvents has
    NO scorer entries at all, we return None instead of grading LOST
    — so the caller can fall through to FotMob (which has universal
    coverage). Genuine 0-0 draws still grade LOST correctly because
    total_goals == 0.
    """
    if not selection:
        return None
    key_events = summary.get("keyEvents") or []
    scorers = _extract_scorers(key_events)
    if not scorers:
        if "anytime" in market_lower or "goal scorer" in market_lower:
            if total_goals > 0:
                # Match had goals but ESPN doesn't know who scored — abstain.
                return None
            # 0-0 draw (or unknown score) — every "Anytime" pick loses.
            return "lost"
        return None
    if _scorer_match(selection, scorers):
        return "won"
    return "lost"


def _extract_scorers(key_events: list[dict]) -> list[str]:
    """Pull the canonical scorer name from every keyEvent that's a goal."""
    out: list[str] = []
    for e in key_events:
        if not e.get("scoringPlay"):
            continue
        tp = (e.get("type") or {}).get("type") or (e.get("type") or {}).get("text") or ""
        # Only count regulation/ET goals — exclude shootouts unless the
        # market explicitly counts them (rare; leave out for safety).
        if e.get("shootout"):
            continue
        # Skip own goals (the credited scorer is the defender, not a
        # bookmaker scorer for the prop) — text usually contains "Own Goal".
        text = (e.get("text") or "")
        if "own goal" in text.lower():
            continue
        # Prefer the athlete in `participants[*].athlete` (most reliable).
        scorer = None
        for p in (e.get("participants") or []):
            if (p.get("type") or "scorer").lower() in {"scorer", "athlete", "scorer-1"} or not p.get("type"):
                a = p.get("athlete") or {}
                scorer = a.get("displayName") or a.get("shortName") or scorer
                if scorer:
                    break
        if not scorer:
            # Fall back to parsing the `shortText` like
            # "Cristiano Ronaldo Goal - Volley" → "Cristiano Ronaldo".
            short = e.get("shortText") or ""
            scorer = re.sub(r"\s+(Goal.*|Penalty.*|Header.*)$", "", short, flags=re.IGNORECASE).strip()
        if not scorer:
            # Last-ditch: pull from full text "Goal! ... [Name] (Team) ..."
            m = re.search(r"goal!?\s*[^.]*?\.\s*([A-Z][\w'\-\.\u00C0-\u024F]+(?:\s+[A-Z][\w'\-\.\u00C0-\u024F]+)+)",
                          text, re.IGNORECASE)
            if m:
                scorer = m.group(1)
        if scorer:
            out.append(scorer)
    return out


def _scorer_match(selection: str, scorers: list[str]) -> bool:
    """True if `selection` matches any scorer name (accent + case insensitive,
    handles middle names, initials, and pick-suffix noise).

    Fixes covered (2026-07-13 root-cause "Kristian Lien graded lost"):
      • Strip trailing pick-market noise ("to score", "to score or assist",
        "anytime goal scorer", etc.) so `sel_last` is the actual player
        surname, not the word "score".
      • Match on FIRST + LAST name both appearing in ESPN's scorer string,
        even when a middle name splits them ("Kristian Lien" ⊂
        "Kristian Stromland Lien"). This is the #1 grading regression
        source — Nordic and Latin American players routinely use middle
        names in official broadcast feeds.
    """
    sel = _norm(selection)
    if not sel:
        return False
    # Trim market-suffix noise so we compare "kristian lien" not
    # "kristian lien to score" (which turned sel_last into "score").
    for suffix in (
        " to score or assist", " to score", " anytime goal scorer",
        " goal scorer", " score or assist", " to score & assist",
    ):
        if sel.endswith(suffix):
            sel = sel[: -len(suffix)].strip()
            break
    if not sel:
        return False
    sel_parts = sel.split()
    sel_first = sel_parts[0] if sel_parts else ""
    sel_last  = sel_parts[-1] if sel_parts else ""
    for s in scorers:
        ns = _norm(s)
        if not ns:
            continue
        # Exact match
        if ns == sel:
            return True
        # Bidirectional substring (handles "C. Ronaldo" vs "Cristiano Ronaldo")
        if (sel in ns) or (ns in sel):
            return True
        ns_parts = ns.split()
        # Last-name only match (min 4 chars so we don't match "Silva" everywhere)
        if len(sel_last) >= 4 and ns_parts and ns_parts[-1] == sel_last:
            return True
        # First + last both present anywhere in the scorer's name string.
        # Critical for middle-name cases: "Kristian Lien" ⊂ "Kristian
        # Stromland Lien" (first token match + last token match, middle
        # name in between). Requires both tokens ≥ 3 chars so the
        # short-form pattern doesn't false-match ("A. Silva" vs
        # "Andre Silva" already handled by substring above).
        if (len(sel_first) >= 3 and len(sel_last) >= 4
                and sel_first in ns_parts and sel_last in ns_parts):
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Batch DB settler — wires soccer legs from the main `picks` collection
# to ESPN scores. Previously we only settled soccer via parlay legs
# (see parlay_leg_settle.settle_soccer_leg import). That meant soccer
# picks in the main picks table stayed `pending` FOREVER, and the
# Analytics dashboard's soccer AGS / SoA rows never populated.
# ──────────────────────────────────────────────────────────────────────

# Map user-facing league names to ESPN league slugs. Keeps the batch
# settler O(picks) instead of O(picks × leagues) by hinting exactly
# which ESPN endpoint to hit for each pick.
_LEAGUE_NAME_TO_SLUG: dict[str, str] = {
    "china super league": "chn.1",
    "csl": "chn.1",
    "premier league": "eng.1",
    "epl": "eng.1",
    "english premier league": "eng.1",
    "la liga": "esp.1",
    "spanish la liga": "esp.1",
    "primera division": "esp.1",
    "serie a": "ita.1",
    "italian serie a": "ita.1",
    "bundesliga": "ger.1",
    "german bundesliga": "ger.1",
    "ligue 1": "fra.1",
    "french ligue 1": "fra.1",
    "eredivisie": "ned.1",
    "primeira liga": "por.1",
    "liga portugal": "por.1",
    "russian premier league": "rus.1",
    "mls": "usa.1",
    "major league soccer": "usa.1",
    "liga mx": "mex.1",
    "expansion mx": "mex.2",
    "brasileirão": "bra.1",
    "brasileirao": "bra.1",
    "brazilian serie a": "bra.1",
    "brazilian serie b": "bra.2",
    "brasileirão série b": "bra.2",
    "argentine primera division": "arg.1",
    "argentine liga profesional": "arg.1",
    "colombian primera a": "col.1",
    "chilean primera division": "chi.1",
    "peruvian primera division": "per.1",
    "concacaf champions cup": "concacaf.champions",
    "conmebol libertadores": "conmebol.libertadores",
    "copa libertadores": "conmebol.libertadores",
    "copa sudamericana": "conmebol.sudamericana",
    "champions league": "uefa.champions",
    "uefa champions league": "uefa.champions",
    "europa league": "uefa.europa",
    "uefa europa league": "uefa.europa",
    "uefa conference league": "uefa.europa.conf",
    "afc champions league": "afc.champions",
    "efl championship": "eng.2",
    "english championship": "eng.2",
    "efl league one": "eng.3",
    "efl league two": "eng.4",
    "fa cup": "eng.fa",
    "carabao cup": "eng.league_cup",
    "copa del rey": "esp.copa_del_rey",
    "coppa italia": "ita.coppa_italia",
    "dfb pokal": "ger.dfb_pokal",
    "coupe de france": "fra.coupe_de_france",
    "allsvenskan": "swe.1",
    "veikkausliiga": "fin.1",
    "eliteserien": "nor.1",
    "1. hnl": "cro.1",
    "croatian hnl": "cro.1",
    "j1 league": "jpn.1",
    "j league": "jpn.1",
    "k league 1": "kor.1",
    "k1 league": "kor.1",
    "australian a-league": "aus.1",
    "a-league men": "aus.1",
}


def _league_slug_for(pick_league: Optional[str]) -> Optional[str]:
    if not pick_league:
        return None
    key = pick_league.strip().lower()
    if key in _LEAGUE_NAME_TO_SLUG:
        return _LEAGUE_NAME_TO_SLUG[key]
    # Substring probe for looser matches (e.g. "China Super League (Regular Season)")
    for name, slug in _LEAGUE_NAME_TO_SLUG.items():
        if len(name) >= 6 and name in key:
            return slug
    return None


async def settle_soccer_picks_via_espn(db, *, days_back: int = 14,
                                          max_picks: int = 400) -> dict:
    """Iterate pending soccer picks whose event_time is in the past and
    apply the ESPN soccer settler. Updates status/result/units_profit.

    Optimisation: picks are grouped by (league_slug, YYYYMMDD) so we
    only fetch each ESPN scoreboard ONCE per run, then match all picks
    for that day/league. Cuts run time from O(picks × leagues) to
    O(unique scoreboards).

    Returns a summary dict. Safe to call repeatedly.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(days=days_back)).isoformat()
    hi = now.isoformat()

    summary = {"scanned": 0, "settled": 0, "won": 0, "lost": 0,
               "push": 0, "skipped": 0, "no_match": 0, "no_league": 0}
    cursor = db.picks.find({
        "sport": "Soccer",
        "status": {"$in": [None, "pending"]},
        "off_board": {"$ne": True},  # Board-visibility gate (2026-07-21)
        "event_time": {"$gte": lo, "$lte": hi},
        "$or": [
            {"market": {"$regex": "Anytime Goal Scorer", "$options": "i"}},
            {"market": {"$regex": "To Score or Assist", "$options": "i"}},
            {"market": {"$regex": "Moneyline", "$options": "i"}},
            {"market": {"$regex": "Total Goals", "$options": "i"}},
            {"market": {"$regex": "Both Teams To Score", "$options": "i"}},
            {"market": {"$regex": "Win or Draw", "$options": "i"}},
            {"market": {"$regex": "Draw No Bet", "$options": "i"}},
            {"market": {"$regex": "Double Chance", "$options": "i"}},
        ],
    }).limit(max_picks)

    try:
        from quality_gate import _extract_player_from_pick
    except Exception:
        def _extract_player_from_pick(p):  # type: ignore
            return (p.get("selection") or "").strip()

    def _american_to_profit(units_risked: float, odds: int) -> float:
        if not odds:
            return 0.0
        if odds > 0:
            return units_risked * odds / 100.0
        return units_risked * 100.0 / abs(odds)

    # Scoreboard cache: (league_slug, YYYYMMDD) → list of events
    scoreboard_cache: dict[tuple[str, str], list[dict]] = {}
    # Summary cache: (league_slug, event_id) → summary json
    summary_cache: dict[tuple[str, str], Optional[dict]] = {}

    async def _get_scoreboard(slug: str, date_str: str) -> list[dict]:
        key = (slug, date_str)
        if key in scoreboard_cache:
            return scoreboard_cache[key]
        url = f"{_ESPN_BASE}/{slug}/scoreboard"
        data = await _http_get(url, {"dates": date_str})
        events = (data or {}).get("events") or []
        scoreboard_cache[key] = events
        return events

    picks_to_process = await cursor.to_list(length=max_picks)
    for p in picks_to_process:
        summary["scanned"] += 1
        market = p.get("market") or ""
        market_l = market.lower()
        sel = (p.get("selection") or "").strip()
        if ("goal scorer" in market_l or "to score or assist" in market_l
                or "score & assist" in market_l):
            if not sel or sel.lower() in ("yes", "no"):
                sel = _extract_player_from_pick(p) or sel

        # Resolve ESPN league slug from the pick's league name.
        slug = _league_slug_for(p.get("league"))
        if not slug:
            summary["no_league"] += 1
            continue

        # Parse teams from event string.
        event = (p.get("event") or "").strip()
        parts = re.split(r"\s+@\s+", event)
        if len(parts) != 2:
            summary["skipped"] += 1
            continue
        away_team, home_team = parts[0].strip(), parts[1].strip()

        # Probe up to 3 candidate dates (event day ±1)
        dates_to_try = _candidate_dates(p.get("event_time"))
        matched_ev = None
        for ds in dates_to_try:
            events = await _get_scoreboard(slug, ds)
            for ev in events:
                comp = (ev.get("competitions") or [{}])[0]
                competitors = comp.get("competitors") or []
                if len(competitors) < 2:
                    continue
                home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
                hname = ((home_c.get("team") or {}).get("displayName") or "")
                aname = ((away_c.get("team") or {}).get("displayName") or "")
                if _names_match(hname, home_team) and _names_match(aname, away_team):
                    matched_ev = (slug, ev)
                    break
            if matched_ev:
                break
        if not matched_ev:
            summary["no_match"] += 1
            continue
        slug_matched, ev = matched_ev
        comp = (ev.get("competitions") or [{}])[0]
        status_name = ((comp.get("status") or {}).get("type") or {}).get("name") or ""
        if status_name not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET",
                               "STATUS_FINAL_PEN", "STATUS_FORFEIT"):
            summary["skipped"] += 1
            continue

        competitors = comp.get("competitors") or []
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
        try:
            home_goals = int(home_c.get("score"))
            away_goals = int(away_c.get("score"))
        except (TypeError, ValueError):
            summary["skipped"] += 1
            continue

        outcome: Optional[str] = None
        if ("goal scorer" in market_l or "to score or assist" in market_l
                or "score & assist" in market_l):
            skey = (slug_matched, str(ev.get("id")))
            if skey not in summary_cache:
                summary_cache[skey] = await _fetch_summary(slug_matched, ev.get("id"))
            summ = summary_cache[skey]
            if summ is not None:
                outcome = _settle_scorer_market(summ, sel, market_l)
        elif "moneyline" in market_l:
            if sel:
                nsel = _norm(sel)
                if _names_match(sel, home_team):
                    outcome = "won" if home_goals > away_goals else "lost"
                elif _names_match(sel, away_team):
                    outcome = "won" if away_goals > home_goals else "lost"
                elif "draw" in nsel:
                    outcome = "won" if home_goals == away_goals else "lost"
        elif "total goals" in market_l:
            m = re.search(r"(\d+(?:\.\d+)?)", market)
            if m:
                line = float(m.group(1))
                total = home_goals + away_goals
                if "under" in market_l:
                    outcome = "push" if abs(total - line) < 0.01 else ("won" if total < line else "lost")
                else:
                    outcome = "push" if abs(total - line) < 0.01 else ("won" if total > line else "lost")

        if outcome not in ("won", "lost", "push"):
            summary["no_match" if outcome is None else "skipped"] += 1
            continue

        units_risked = float(p.get("units_risked") or 1.0)
        odds = int(p.get("odds_at_pick") or p.get("book_odds") or -110)
        if outcome == "won":
            profit = round(_american_to_profit(units_risked, odds), 4)
        elif outcome == "lost":
            profit = -units_risked
        else:
            profit = 0.0
        await db.picks.update_one(
            {"_id": p["_id"]},
            {"$set": {
                "status": outcome,
                "result": outcome,
                "units_profit": profit,
                "settled_at": now.isoformat(),
                "settled_by": "soccer_espn_batch_v1",
            }},
        )
        summary["settled"] += 1
        summary[outcome] = summary.get(outcome, 0) + 1
    return summary
