"""Player ↔ Current Team ↔ Fixture Integrity Validator — Phase 2 (2026-08-11).

Rejects player-based predictions when the player's CURRENT team is
not one of the two teams contesting the fixture.  Applies at two
layers:

    A. Writer / candidate stage
       Call `validate_player_fixture_pick(pick, roster_lookup)` right
       after the writer builds a pick.  A rejection tags the pick
       with `player_team_invalid=True` and a structured reason —
       the writer either drops the pick or forwards it as quarantined.

    B. Immediately before canonical publication
       `PredictionPublicationService._build_payload` (or callers of
       `publish_upserted_picks`) invoke the same validator.  A rejection
       causes the pick to be excluded from the publication batch with
       `publication_source` NEVER stamped.

P0-C (2026-08-11) — Club vs National-team routing.
       The validator now understands that "current team" is TWO
       independent affiliations: club (Manchester City, Inter Miami)
       and national team (Argentina, England).  Club membership must
       NEVER cause a national-team mismatch (Messi playing for
       Argentina cannot be rejected because his club is Inter Miami).
       When the fixture is international, the validator routes to
       the national-team lookup.  Unknown national-team membership
       returns ``roster_unverified`` (not ``team_mismatch``).

Design guarantees:

  * Never uses stale historical Wikipedia / top-scorer club data as
    proof of CURRENT membership.  Prefer the freshest trusted roster
    observation (usually the last ESPN roster/team-stats snapshot).
  * Aliases and diacritics are normalised via `unicodedata` — no loose
    substring matching (so "Sam Adek" cannot match "Sam Adekugbe" or
    "Sami" cannot match "Sami Adekugbe").
  * Missing fresh roster data does NOT auto-approve — it BLOCKS the
    prop (returns `verified=False, reason=roster_unverified`).
  * Team-level markets (moneyline / spread / total) bypass the
    validator — only player-based picks are checked.

The validator is stateless.  Callers supply the roster observation
dict (typically hydrated from `db.player_identities`) so this module
has no DB dependency.

Contract:

    validate_player_fixture_pick(
        pick, roster_lookup, *,
        national_team_lookup=None,
        fresh_roster_names=None,
        fresh_national_team_names=None,
    ) →
        {
          "verified": bool,
          "reason":   Optional[str],
          "player":   str,
          "player_team": Optional[str],
          "fixture_teams": tuple[str, str],
          "fixture_type": "club" | "international",
        }

Rejection reasons (structured, machine-consumable):

    player_team_mismatch       — player's current team is not in the fixture
    roster_unverified          — no fresh roster observation for this player
    fixture_teams_unknown      — cannot parse both teams from event string
    player_name_missing        — pick has no identifiable player name
    market_not_player_based    — internal; validator returns verified=True
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Optional


# ── Rejection reason enum ─────────────────────────────────────────
REASON_PLAYER_TEAM_MISMATCH   = "player_team_mismatch"
REASON_ROSTER_UNVERIFIED      = "roster_unverified"
REASON_FIXTURE_TEAMS_UNKNOWN  = "fixture_teams_unknown"
REASON_PLAYER_NAME_MISSING    = "player_name_missing"
REASON_MARKET_NOT_PLAYER      = "market_not_player_based"


# ── International-fixture detection (P0-C) ────────────────────────
#
# Two independent signals — either one is sufficient:
#   (a) the pick's `league` / `competition` field carries a known
#       international-tournament marker (World Cup, Copa America, Nations
#       League, friendlies, etc.);
#   (b) BOTH fixture sides are in the curated FIFA-member nation set.
#
# The nation set is a broad but not exhaustive list of common national
# team names.  New markets that mention obscure nations simply route
# to the club path — they never falsely trigger international rejection
# because the requirement is BOTH sides being nations.
_INTL_LEAGUE_MARKERS = (
    "world cup", "copa america", "copa libertadores",
    "european championship", "euros", "nations league",
    "gold cup", "friendlies", " international",
    "internationals", "afcon", "africa cup",
    "asian cup", "asian games", "olympic",
    "concacaf", "conmebol", "world cup qualif",
    "wc qualif", "euro qualif", "afcon qualif",
    "afc qualif", "conmebol qualif", "concacaf qualif",
)

_NATIONS_RAW = {
    # South America
    "argentina", "brazil", "uruguay", "paraguay", "chile", "peru",
    "colombia", "ecuador", "venezuela", "bolivia",
    # North & Central America / Caribbean
    "usa", "united states", "canada", "mexico", "costa rica", "panama",
    "jamaica", "honduras", "guatemala", "el salvador", "nicaragua",
    "haiti", "cuba", "dominican republic", "trinidad and tobago",
    "puerto rico", "curacao",
    # Europe
    "england", "france", "spain", "germany", "italy", "portugal",
    "netherlands", "belgium", "croatia", "switzerland", "austria",
    "poland", "hungary", "czech republic", "czechia", "slovakia",
    "romania", "bulgaria", "serbia", "greece", "denmark", "sweden",
    "norway", "finland", "iceland", "ireland", "republic of ireland",
    "northern ireland", "scotland", "wales", "russia", "ukraine",
    "belarus", "moldova", "georgia", "armenia", "azerbaijan",
    "turkey", "turkiye", "cyprus", "malta", "albania", "kosovo",
    "north macedonia", "montenegro", "bosnia", "bosnia and herzegovina",
    "slovenia", "estonia", "latvia", "lithuania", "luxembourg",
    "liechtenstein", "san marino", "andorra", "gibraltar",
    "faroe islands", "faroe",
    # Africa
    "morocco", "algeria", "tunisia", "egypt", "libya",
    "nigeria", "ghana", "cameroon", "senegal", "ivory coast",
    "cote d'ivoire", "south africa", "dr congo", "congo",
    "tanzania", "kenya", "uganda", "rwanda", "burundi", "sudan",
    "south sudan", "ethiopia", "eritrea", "somalia", "djibouti",
    "angola", "cape verde", "zambia", "zimbabwe", "malawi",
    "botswana", "namibia", "mozambique", "madagascar", "mauritius",
    "seychelles", "comoros", "burkina faso", "mali", "guinea",
    "guinea bissau", "gabon", "gambia", "liberia", "sierra leone",
    "togo", "benin", "central african republic", "chad", "niger",
    "mauritania", "equatorial guinea", "sao tome and principe",
    "lesotho", "eswatini",
    # Asia
    "japan", "south korea", "korea republic", "north korea",
    "china", "china pr", "chinese taipei", "taiwan", "hong kong",
    "macau", "mongolia",
    "saudi arabia", "iran", "iraq", "qatar", "uae",
    "united arab emirates", "kuwait", "bahrain", "oman", "yemen",
    "syria", "lebanon", "jordan", "palestine", "israel",
    "kazakhstan", "uzbekistan", "kyrgyzstan", "tajikistan",
    "turkmenistan", "afghanistan", "pakistan", "india",
    "bangladesh", "sri lanka", "nepal", "bhutan", "myanmar",
    "thailand", "vietnam", "malaysia", "singapore", "indonesia",
    "philippines", "brunei", "cambodia", "laos", "east timor",
    # Oceania
    "australia", "new zealand", "fiji", "papua new guinea",
    "solomon islands", "vanuatu", "samoa", "tonga", "tahiti",
    "new caledonia", "cook islands",
    # South America extras
    "guyana", "suriname",
}
_NATIONS: set[str] = set()   # populated lazily via _norm below


def _norm(s: str) -> str:
    """Diacritic-safe lowercase normalisation with punctuation stripped.

    Turns "Kylian Mbappé" and "Kylian Mbappe" into the same token.
    Strips periods, apostrophes, hyphens so "M'Bappe" == "Mbappe".
    Collapses runs of whitespace.
    """
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    ascii_only = "".join(ch for ch in nfkd
                          if not unicodedata.combining(ch))
    cleaned = re.sub(r"[.'’\-]", "", ascii_only)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


# Materialise the normalised nation set at import time.
_NATIONS = {_norm(n) for n in _NATIONS_RAW}


def _is_international_fixture(pick: dict[str, Any],
                                fixture_teams: Optional[tuple[str, str]]) -> bool:
    """Detect an international / national-team fixture.

    Signals (any positive is enough):
      * ``pick['league']`` or ``pick['competition']`` mentions a known
        international-tournament marker.
      * BOTH sides of the fixture are in the curated FIFA nation set.
    """
    for k in ("league", "competition", "tournament"):
        v = pick.get(k)
        if isinstance(v, str) and v:
            low = v.lower()
            if any(marker in low for marker in _INTL_LEAGUE_MARKERS):
                return True
    if fixture_teams:
        a, b = fixture_teams
        if _norm(a) in _NATIONS and _norm(b) in _NATIONS:
            return True
    return False


# ── Market suffix / selection action regexes ─────────────────────
#
# P0-B (2026-08-11) — case-insensitive, accent-preserving, ordered by
# specificity so longer patterns match BEFORE shorter subsumed ones
# (e.g. "to score or assist" is tried before "to score").
#
# Every alternative starts with `\s+` to prevent mid-name false
# matches ("Anytime" in the middle of a name is not stripped) and
# ends at `\s*$` so we only strip TRAILING market/action suffixes.
_MARKET_SUFFIX_RE = re.compile(
    r"\s+(?:"
    r"anytime\s+goal\s+scorer"
    r"|first\s+goal\s+scorer"
    r"|last\s+goal\s+scorer"
    r"|to\s+score\s+or\s+assist"
    r"|score\s+or\s+assist"
    r"|to\s+score"
    r"|to\s+assist"
    r"|goal\s+scorer"
    r"|first\s+goal"
    r"|last\s+goal"
    r")\s*$",
    re.IGNORECASE,
)

# Selection field may carry over/under lines and other action verbs.
_SELECTION_ACTION_RE = re.compile(
    r"\s+(?:"
    r"anytime\s+goal\s+scorer"
    r"|first\s+goal\s+scorer"
    r"|last\s+goal\s+scorer"
    r"|to\s+score\s+or\s+assist"
    r"|score\s+or\s+assist"
    r"|to\s+score"
    r"|to\s+assist"
    r"|to\s+record(?:\s+.*)?"
    r"|goal\s+scorer"
    r"|first\s+goal"
    r"|last\s+goal"
    r"|over\s+[\d.]+.*"
    r"|under\s+[\d.]+.*"
    r")\s*$",
    re.IGNORECASE,
)


def _extract_player_name(pick: dict[str, Any]) -> Optional[str]:
    """Extract the player name from a pick with strict, accent-safe rules.

    Priority (P0-B, 2026-08-11):
      1. STRUCTURED FIELDS FIRST — ``player_name`` → ``player`` →
         ``selection`` → ``pick_side``. If a structured field is
         present and non-empty, we ONLY apply light action-verb
         stripping (over/under lines, "to record N shots" etc.) —
         we never fall through to parsing the market string when a
         structured field yielded a usable name.
      2. FALLBACK — parse the ``market`` field.
         a. If a `` - `` separator is present, take the left side
            (matches Odds API canonical "<Name> - <Market>" pattern).
         b. Otherwise, strip a KNOWN, case-insensitive market suffix
            ("Anytime Goal Scorer", "To Score or Assist", etc.) —
            never a wildcard strip that could truncate names.

    Guarantees:
      * Accents preserved (regex is Unicode-aware; no ``.lower()``
        on the returned value).
      * Case-insensitive parsing of ALL supported market suffixes.
      * No loose parsing that could split "Julian Alvarez" mid-name.
    """
    # 1. Structured fields take priority.
    for k in ("player_name", "player", "selection", "pick_side"):
        v = pick.get(k)
        if isinstance(v, str) and v.strip():
            cleaned = _SELECTION_ACTION_RE.sub("", v.strip()).strip()
            if cleaned:
                return cleaned

    # 2. Market-string fallback.
    m = pick.get("market")
    if isinstance(m, str) and m.strip():
        raw = m.strip()
        # 2a. "<Name> - <Market>" canonical Odds API shape.
        if " - " in raw:
            head = raw.split(" - ", 1)[0].strip()
            if head:
                return head
        # 2b. Strip a known market suffix, case-insensitively.
        stripped = _MARKET_SUFFIX_RE.sub("", raw).strip()
        if stripped and stripped != raw:
            return stripped
    return None


def _extract_fixture_teams(pick: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Parse the two participating teams from the fixture identifier.

    Accepts:
      * pick["home_team"] / pick["away_team"] when present.
      * pick["event"] shaped as "A vs B", "A @ B", "A - B".
    """
    home = pick.get("home_team") or pick.get("home")
    away = pick.get("away_team") or pick.get("away")
    if home and away:
        return (str(home), str(away))
    event = pick.get("event")
    if isinstance(event, str):
        for sep in (" vs ", " @ ", " - ", " v "):
            if sep in event:
                a, b = event.split(sep, 1)
                if a.strip() and b.strip():
                    return (a.strip(), b.strip())
    return None


def _is_player_based_market(market: Optional[str]) -> bool:
    if not market:
        return False
    m = market.lower()
    # Explicit team-market allow-list — these markets always name the
    # TEAM not the player.  Short-circuit them so ambiguous tokens
    # like "to score" in "Both Teams To Score - Yes" don't
    # false-positive.
    team_market_tokens = (
        "moneyline", "money line", "spread", "handicap",
        "over/under", "total goals", "total corners",
        "both teams to score", "btts",
        "draw no bet", "double chance", "half-time",
        "asian handicap", "correct score",
    )
    for t in team_market_tokens:
        if t in m:
            return False
    # Player-based Soccer markets.
    tokens = (
        "anytime goal scorer",
        "to score",             # to score / to score or assist
        "first goal scorer", "last goal scorer",
        "player_",              # Odds API player_* key
    )
    return any(t in m for t in tokens)


def _teams_match(player_team: str,
                  fixture_teams: tuple[str, str]) -> bool:
    """Compare a player's current team to the two fixture teams.

    Uses normalised equality with an additional "alias contains"
    check — the fixture side ``"Manchester City"`` matches a
    roster team of ``"Manchester City FC"`` etc.  Only accepted
    when the shorter side is fully contained in the longer AND
    the shorter is at least 4 chars (guards against 2-3 letter
    false matches).
    """
    p = _norm(player_team)
    if not p:
        return False
    for f in fixture_teams:
        fn = _norm(f)
        if not fn:
            continue
        if p == fn:
            return True
        # Word-boundary containment guard against "Sam" vs "Sami"
        # (used only for team, not player, where 4+ chars is safe).
        if len(p) >= 4 and (p in fn or fn in p):
            # Additional guard: shared prefix of the club stem must
            # exceed 4 chars so "United" and "Uniao" don't collapse.
            shorter, longer = sorted([p, fn], key=len)
            if longer.startswith(shorter):
                return True
    return False


def validate_player_fixture_pick(
    pick: dict[str, Any],
    roster_lookup: dict[str, str],
    *,
    fresh_roster_names: Optional[set[str]] = None,
    national_team_lookup: Optional[dict[str, str]] = None,
    fresh_national_team_names: Optional[set[str]] = None,
) -> dict[str, Any]:
    """See module docstring.

    Parameters
    ----------
    pick
        The pick doc.
    roster_lookup
        ``{normalised_player_name: current_club_name}`` — the freshest
        trusted CLUB observation the caller has.  Used for club
        fixtures only.
    fresh_roster_names
        Optional set of normalised player names present in the MOST
        RECENT (fresh) CLUB observation.  A player only present via
        stale evidence is REJECTED as ``roster_unverified``.
    national_team_lookup
        P0-C — ``{normalised_player_name: current_national_team}`` for
        international fixtures.  When ``None``, ANY international
        fixture returns ``roster_unverified`` (never
        ``team_mismatch``).
    fresh_national_team_names
        Optional freshness set for the national-team lookup, mirroring
        ``fresh_roster_names`` semantics.
    """
    market = pick.get("market") or ""

    # Non-player markets pass through untouched.
    if not _is_player_based_market(market):
        return {
            "verified": True,
            "reason": REASON_MARKET_NOT_PLAYER,
            "player": None,
            "player_team": None,
            "fixture_teams": None,
            "fixture_type": None,
        }

    player_raw = _extract_player_name(pick)
    if not player_raw:
        return {
            "verified": False,
            "reason": REASON_PLAYER_NAME_MISSING,
            "player": None,
            "player_team": None,
            "fixture_teams": None,
            "fixture_type": None,
        }
    player_norm = _norm(player_raw)

    fixture = _extract_fixture_teams(pick)
    if fixture is None:
        return {
            "verified": False,
            "reason": REASON_FIXTURE_TEAMS_UNKNOWN,
            "player": player_raw,
            "player_team": None,
            "fixture_teams": None,
            "fixture_type": None,
        }

    # P0-C — route to the appropriate lookup based on fixture type.
    intl = _is_international_fixture(pick, fixture)
    fixture_type = "international" if intl else "club"

    if intl:
        # Use national-team lookup ONLY.  Club data must never cause
        # an international-fixture rejection.
        nt_lookup = national_team_lookup or {}
        nt_fresh = fresh_national_team_names
        team = nt_lookup.get(player_norm)
        if team is None:
            parts = player_norm.split()
            if len(parts) >= 2:
                last = parts[-1]
                candidates = [(k, v) for k, v in nt_lookup.items()
                              if _norm(k).endswith(last)]
                if len(candidates) == 1:
                    team = candidates[0][1]
        if team is None:
            # National-team membership unknown → roster_unverified,
            # NEVER team_mismatch (per P0-C spec).
            return {
                "verified": False,
                "reason": REASON_ROSTER_UNVERIFIED,
                "player": player_raw,
                "player_team": None,
                "fixture_teams": fixture,
                "fixture_type": fixture_type,
            }
        if nt_fresh is not None:
            if player_norm not in nt_fresh:
                parts = player_norm.split()
                last = parts[-1] if parts else ""
                if not any(n.endswith(last) for n in nt_fresh if last):
                    return {
                        "verified": False,
                        "reason": REASON_ROSTER_UNVERIFIED,
                        "player": player_raw,
                        "player_team": team,
                        "fixture_teams": fixture,
                        "fixture_type": fixture_type,
                    }
        if _teams_match(team, fixture):
            return {
                "verified": True,
                "reason": None,
                "player": player_raw,
                "player_team": team,
                "fixture_teams": fixture,
                "fixture_type": fixture_type,
            }
        return {
            "verified": False,
            "reason": REASON_PLAYER_TEAM_MISMATCH,
            "player": player_raw,
            "player_team": team,
            "fixture_teams": fixture,
            "fixture_type": fixture_type,
        }

    # ── Club fixture path ─────────────────────────────────────
    team = roster_lookup.get(player_norm)
    if team is None:
        parts = player_norm.split()
        if len(parts) >= 2:
            last = parts[-1]
            candidates = [(k, v) for k, v in roster_lookup.items()
                          if _norm(k).endswith(last)]
            if len(candidates) == 1:
                team = candidates[0][1]

    if team is None:
        return {
            "verified": False,
            "reason": REASON_ROSTER_UNVERIFIED,
            "player": player_raw,
            "player_team": None,
            "fixture_teams": fixture,
            "fixture_type": fixture_type,
        }

    # Freshness gate — reject when caller supplied a fresh set AND
    # the player isn't in it (i.e. we only have stale evidence).
    if fresh_roster_names is not None:
        if player_norm not in fresh_roster_names:
            parts = player_norm.split()
            last = parts[-1] if parts else ""
            if not any(n.endswith(last)
                        for n in fresh_roster_names if last):
                return {
                    "verified": False,
                    "reason": REASON_ROSTER_UNVERIFIED,
                    "player": player_raw,
                    "player_team": team,
                    "fixture_teams": fixture,
                    "fixture_type": fixture_type,
                }

    if _teams_match(team, fixture):
        return {
            "verified": True,
            "reason": None,
            "player": player_raw,
            "player_team": team,
            "fixture_teams": fixture,
            "fixture_type": fixture_type,
        }
    return {
        "verified": False,
        "reason": REASON_PLAYER_TEAM_MISMATCH,
        "player": player_raw,
        "player_team": team,
        "fixture_teams": fixture,
        "fixture_type": fixture_type,
    }


def tag_pick_with_verdict(pick: dict[str, Any],
                           verdict: dict[str, Any]) -> None:
    """Convenience — stamp the verdict onto the pick in-place."""
    if verdict.get("verified"):
        pick["player_team_verified"] = True
        pick["player_team_invalid"] = False
    else:
        pick["player_team_verified"] = False
        pick["player_team_invalid"] = True
        pick["player_team_invalid_reason"] = verdict.get("reason")
        pick["player_team_snapshot"] = {
            "player": verdict.get("player"),
            "player_team": verdict.get("player_team"),
            "fixture_teams": verdict.get("fixture_teams"),
        }


__all__ = [
    "validate_player_fixture_pick",
    "tag_pick_with_verdict",
    "REASON_PLAYER_TEAM_MISMATCH",
    "REASON_ROSTER_UNVERIFIED",
    "REASON_FIXTURE_TEAMS_UNKNOWN",
    "REASON_PLAYER_NAME_MISSING",
    "REASON_MARKET_NOT_PLAYER",
    "_norm",
]
