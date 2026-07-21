"""Elite ATP/WTA player registry — Signal Engine tennis boost source.

Rationale (2026-07-21):
  The signal engine's `is_elite_tagged` conviction floor is worth +22 raw
  points (score 72 minimum). In soccer / NBA / NFL, star players get this
  tag via the `elite_players.py` pipeline. Tennis players NEVER did — so
  Alcaraz, Sinner, Djokovic, Sabalenka, Świątek etc. were silently missing
  the elite floor other sports' stars received.

  This module exposes `is_elite_tennis_player(name)` that the signal
  engine consults to set `pick['tennis_elite']=True` when the pick side
  is one of the top ATP/WTA stars. The engine.py conviction-boost block
  then honors this flag the same way it honors `is_elite` for other
  sports.

  List is curated (top 20 ATP + top 20 WTA as of mid-2026) — no external
  API dependency. Update periodically as the tour tops shift.
"""
from __future__ import annotations

import re
import unicodedata

# Top ATP players (Grand Slam contenders + top-15 seeds mid-2026).
# Names are stored lower-case, ASCII-folded, punctuation-stripped.
_ATP_ELITE = {
    "carlos alcaraz",
    "jannik sinner",
    "novak djokovic",
    "alexander zverev",
    "daniil medvedev",
    "taylor fritz",
    "casper ruud",
    "andrey rublev",
    "hubert hurkacz",
    "grigor dimitrov",
    "stefanos tsitsipas",
    "alex de minaur",
    "holger rune",
    "ben shelton",
    "tommy paul",
    "frances tiafoe",
    "lorenzo musetti",
    "jack draper",
    "arthur fils",
    "sebastian korda",
    "karen khachanov",
    "felix auger aliassime",
    "denis shapovalov",
    "rafael nadal",  # legacy — still counts when active on wildcard entries
}

# Top WTA players.
_WTA_ELITE = {
    "aryna sabalenka",
    "iga swiatek",
    "coco gauff",
    "elena rybakina",
    "jessica pegula",
    "qinwen zheng",
    "jasmine paolini",
    "emma navarro",
    "danielle collins",
    "beatriz haddad maia",
    "daria kasatkina",
    "madison keys",
    "barbora krejcikova",
    "paula badosa",
    "mirra andreeva",
    "diana shnaider",
    "marketa vondrousova",
    "elina svitolina",
    "ons jabeur",
    "victoria azarenka",
    "naomi osaka",
    "karolina muchova",
    "donna vekic",
    "linda noskova",
    "sofia kenin",
}

_ELITE_ALL = _ATP_ELITE | _WTA_ELITE

# Precompute normalized last-name → full name for TennisExplorer-style
# "Lastname X." parsing. e.g. "alcaraz" → "carlos alcaraz".
_LASTNAME_INDEX: dict[str, list[str]] = {}
for _full in _ELITE_ALL:
    _parts = _full.split()
    if len(_parts) >= 2:
        _last = _parts[-1]
        _LASTNAME_INDEX.setdefault(_last, []).append(_full)


_NON_ALPHA = re.compile(r"[^a-z\s]")
_TE_INITIAL = re.compile(r"^([a-z\-']+)\s+([a-z])\.?$")


def _norm(name: str) -> str:
    """Lowercase + ASCII-fold + strip punctuation. Matches how names
    are keyed in the elite sets above."""
    if not name:
        return ""
    # NFKD strips diacritics (Djokovič → Djokovic).
    n = unicodedata.normalize("NFKD", name)
    n = n.encode("ascii", "ignore").decode("ascii").lower().strip()
    n = _NON_ALPHA.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def is_elite_tennis_player(name: str) -> bool:
    """Return True when `name` matches one of the top ATP/WTA stars.

    Handles three name formats:
      1. Full name: "Carlos Alcaraz"      → direct set membership.
      2. TE-style:  "Alcaraz C."          → last-name + initial match.
      3. Last name only: "Alcaraz"        → matched via last-name index
                                            (only when unique — avoids
                                            "Djokovic N." false-positive
                                            when only one Djokovic in set).
    """
    if not name:
        return False
    nn = _norm(name)
    if not nn:
        return False

    # Case 1: full name direct match.
    if nn in _ELITE_ALL:
        return True

    # Case 2: TennisExplorer "Alcaraz C." format.
    m = _TE_INITIAL.match(nn)
    if m:
        last = m.group(1)
        initial = m.group(2)
        candidates = _LASTNAME_INDEX.get(last, [])
        for full in candidates:
            first_word = full.split()[0]
            if first_word.startswith(initial):
                return True
        # Fallback: single-candidate last name is a safe match.
        if len(candidates) == 1:
            return True
        return False

    # Case 3: bare last name (rare — usually multi-word events).
    parts = nn.split()
    if len(parts) == 1 and parts[0] in _LASTNAME_INDEX:
        if len(_LASTNAME_INDEX[parts[0]]) == 1:
            return True

    # Case 4: prefix-of-full match ("Djokovic" → "novak djokovic" ends with).
    for full in _ELITE_ALL:
        if full.endswith(" " + nn) or full.startswith(nn + " "):
            return True

    return False


__all__ = ["is_elite_tennis_player"]
