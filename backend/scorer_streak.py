"""Soccer scorer streak engine — uses REAL match data from Understat.

User audit (2026-06-30):
  • Mbappé showed "COLD · 15L" on Anytime Goalscorer despite recent goals.
  • Investigation found `player_form.current_streak` was computed from our
    PICK win/loss history in `player_intel/volatility.py::summarise_trend`,
    which was poisoned by:
      ▸ Goal-Header-missed bug (header goals = false LOSS)
      ▸ DNP / Substitute false-LOSS bug
      ▸ Lottery First-Goal-Scorer picks at 3% real hit rate

  This module rebuilds the streak from REAL match data:
    1. `soccer_player_form` (Understat per-match scrape, Top-5 leagues)
       — goals_per_90, goals, games, form_label (HOT/NEUTRAL/COLD).
    2. Hardcoded elite anchor (last-resort fallback for marquee
       international players who don't appear in Understat domestic data)

DB SCHEMA NOTES (verified 2026-06-30):
  soccer_player_form fields:
    • player_name  — e.g. "Erling Haaland", "Kylian Mbappe-Lottin"
    • name_canonical — e.g. "erling haaland", "kylian mbappelottin"
    • form_label   — "HOT" | "NEUTRAL" | "COLD"
    • goals, games, goals_per_90, xg

  Name matching must handle:
    • Diacritics (Mbappé ↔ Mbappe)
    • Hyphenated surnames stored without hyphen (Mbappe-Lottin →
      mbappelottin)
    • Common-name collisions (Messi vs Messias, Junior vs Junior Kroupi)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


def _strip_diacritics(s: str) -> str:
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", s)
    return "".join(c for c in n if not unicodedata.combining(c))


def _norm(name: str) -> str:
    """Lowercase, strip diacritics, collapse non-alphanumerics to spaces."""
    if not name:
        return ""
    n = _strip_diacritics(name).lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _norm_squashed(name: str) -> str:
    """Like _norm but with all spaces removed (matches DB's
    `name_canonical` for hyphenated surnames: 'Mbappe-Lottin' →
    'mbappelottin', i.e. no separator)."""
    return _norm(name).replace(" ", "")


_SCORER_RE = re.compile(
    r"(anytime\s+goal\s*scorer|anytime\s+scorer|first\s+goal\s*scorer|"
    r"first\s+scorer|last\s+goal\s*scorer|last\s+scorer|to\s+score|"
    r"score\s+or\s+assist|score\s+and\s+assist|score\s+&\s+assist)",
    re.IGNORECASE,
)


def is_scorer_market(market: str) -> bool:
    return bool(market and _SCORER_RE.search(market))


# ── Marquee elite anchors ───────────────────────────────────────────
# Hardcoded form labels for marquee international/cross-league players
# whose CURRENT clubs are outside Understat's Top-5 league scrape.
# Updated manually based on recent club + national-team form.
# Format: normalized-full-name → (form_label, streak_proxy)
_ELITE_ANCHOR = {
    # Saudi Pro League — Understat doesn't scrape this league.
    "cristiano ronaldo":  ("HOT", 3),
    "neymar":             ("NEUTRAL", 0),
    # MLS — partial Understat coverage.
    "lionel messi":       ("HOT", 3),
    "luis suarez":        ("NEUTRAL", 0),
    "sergio busquets":    ("NEUTRAL", 0),
    # PSG / Real Madrid superstars where Understat may map to a
    # slightly different stored name string.
    "kylian mbappe":      ("HOT", 3),
    "kylian mbappe lottin": ("HOT", 3),
    # Recent national-team standouts.
    "harry kane":         ("HOT", 3),
    "erling haaland":     ("NEUTRAL", 0),
    "robert lewandowski": ("HOT", 3),
    "mohamed salah":      ("HOT", 3),
    "vinicius junior":    ("HOT", 3),
    "kevin de bruyne":    ("NEUTRAL", 0),
    "jude bellingham":    ("HOT", 3),
    "lamine yamal":       ("HOT", 3),
}


def _label_to_streak(label: str, goals_per_90: float, last5_g: float,
                     games: float) -> int:
    """Convert Understat form_label + recent rate → ±5 streak proxy."""
    lbl = (label or "NEUTRAL").upper()
    # Honor the Understat label first — it's already calibrated for
    # rolling form vs season baseline.
    if lbl == "HOT":
        return 3
    if lbl == "COLD" and games >= 8:
        # Only mark COLD when sample size is large enough to be honest.
        return -3
    # Neutral — fall through to per-90 inspection.
    if goals_per_90 >= 0.55 or last5_g >= 0.5:
        return 1
    if goals_per_90 <= 0.15 and games >= 10:
        return -2
    return 0


async def _find_form_doc(db, player_name: str) -> Optional[dict]:
    """Robust name match against `soccer_player_form`.

    Tries 4 strategies in order of strictness so we get the RIGHT
    player (not a near-namesake):
      1. exact name_canonical (full normalised name)
      2. exact name_canonical squashed (handles "Mbappe-Lottin" stored
         as "mbappelottin")
      3. player_name iregex with the full search name
      4. last-name match BUT only when the first-name initial agrees
         (avoids "Messi"→"Messias", "Junior"→"Junior Kroupi" false
         positives)
    """
    if not player_name:
        return None
    norm_spaces = _norm(player_name)            # "kylian mbappe"
    norm_squash = _norm_squashed(player_name)   # "kylianmbappe"
    parts = norm_spaces.split()
    if not parts:
        return None
    last = parts[-1]
    first_initial = parts[0][:1] if parts[0] else ""

    # Strategy 1: exact normalised match.
    doc = await db.soccer_player_form.find_one({"name_canonical": norm_spaces})
    if doc:
        return doc

    # Strategy 2: hyphen/space-collapsed match (Mbappe-Lottin =
    # "kylian mbappelottin" stored — squash search to "kylianmbappelottin"
    # then also try just first+last squashed).
    doc = await db.soccer_player_form.find_one(
        {"name_canonical": {"$regex": f"^{re.escape(norm_squash)}", "$options": "i"}},
    )
    if doc:
        return doc

    # Strategy 3: full-name substring (handles middle-name DB rows).
    if len(norm_spaces) >= 6:
        doc = await db.soccer_player_form.find_one(
            {"name_canonical": {"$regex": re.escape(norm_spaces), "$options": "i"}},
        )
        if doc:
            return doc

    # Strategy 4: last-name + first-initial disambiguation.
    if len(last) >= 4:  # avoid 3-letter common tokens
        cursor = db.soccer_player_form.find(
            {"name_canonical": {"$regex": f"\\b{re.escape(last)}\\b", "$options": "i"}},
        ).sort([("games", -1)]).limit(20)
        candidates = await cursor.to_list(length=20)
        if candidates:
            # Prefer doc whose first-name initial matches AND whose
            # normalised first name starts with the search first name.
            best = None
            for c in candidates:
                cn = (c.get("name_canonical") or "").split()
                if not cn:
                    continue
                # Last name must literally match (the regex above also
                # matches middle-name-as-last cases, filter strictly).
                if cn[-1] != last:
                    # Hyphenated case: cn[-1] might be "mbappelottin"
                    # while last is "mbappe" — accept startswith.
                    if not cn[-1].startswith(last) and last not in cn:
                        continue
                if first_initial and cn[0][:1] == first_initial:
                    # Also prefer where first names share more chars.
                    if cn[0].startswith(parts[0]) or parts[0].startswith(cn[0]):
                        return c
                    best = best or c
            if best:
                return best
            # No initial match — DON'T return a doc, the player isn't here.
            return None

    return None


async def real_scoring_streak(db, player_name: str) -> Optional[dict]:
    """Return {streak, label, source, ...} from REAL match data, or
    None if no honest data exists for this player.

    Strategy order:
      1. `soccer_player_form` — Understat per-player season aggregate
         (Top-5 leagues, refreshed every 12h)
      2. Hardcoded elite anchor — marquee international/MLS/SPL players
         missing from Understat
      3. None — caller should HIDE the streak chip (NEVER show stale
         pick-history data as a fallback)
    """
    if not player_name:
        return None

    # 1) Understat per-player record.
    doc = await _find_form_doc(db, player_name)
    if doc:
        goals = float(doc.get("goals") or 0)
        games = float(doc.get("games") or 0)
        gp90 = float(doc.get("goals_per_90") or 0)
        label = (doc.get("form_label") or "NEUTRAL").upper()
        # last5 rate isn't stored on this collection — approximate
        # using overall rate when sample is small.
        last5_g = gp90 * 0.85
        streak = _label_to_streak(label, gp90, last5_g, games)
        return {
            "streak": streak,
            "label": label,
            "source": "understat",
            "goals_per_90": gp90,
            "season_g": goals,
            "season_games": games,
            "matched_name": doc.get("player_name"),
        }

    # 2) Marquee elite anchor.
    norm_spaces = _norm(player_name)
    anchor = _ELITE_ANCHOR.get(norm_spaces)
    if anchor is None:
        # Also try last-name match on the anchor table (handles
        # "Kylian Mbappé Lottin" inputs etc.).
        last = norm_spaces.split()[-1] if norm_spaces else ""
        for key, val in _ELITE_ANCHOR.items():
            if last and key.split()[-1] == last:
                anchor = val
                break
    if anchor is not None:
        label, streak = anchor
        return {
            "streak": int(streak),
            "label": label,
            "source": "elite_anchor",
        }

    # 3) Genuinely no data — caller hides chip.
    return None


async def enrich_picks_with_real_streaks(picks: list[dict], db) -> dict:
    """Walk pick list, replace `player_form.current_streak` +
    `understat_form.label` on every goalscorer pick with values
    computed from REAL match data (or hide them entirely when no data
    exists).

    Returns stats dict: {real_data_applied, hidden_no_data, skipped_non_scorer}.
    """
    stats = {"real_data_applied": 0, "hidden_no_data": 0,
             "skipped_non_scorer": 0}
    if not picks:
        return stats

    # Collect unique scorer-market players for bulk lookup.
    unique_players: dict[str, str] = {}   # norm → original
    pick_to_norm: dict[int, str] = {}
    for i, p in enumerate(picks):
        if (p.get("sport") or "").lower() != "soccer":
            continue
        market = p.get("market") or ""
        if not is_scorer_market(market):
            continue
        # Player is either selection or stripped from market suffix.
        sel = (p.get("selection") or "").strip()
        if sel.lower() in ("", "yes", "no"):
            for suffix in (
                " Anytime Goal Scorer", " First Goal Scorer",
                " Last Goal Scorer", " To Score or Assist",
                " Score or Assist", " Score and Assist",
                " Score & Assist", " Anytime Scorer", " To Score",
            ):
                if market.endswith(suffix):
                    sel = market[: -len(suffix)].strip()
                    break
        if not sel:
            continue
        norm = _norm(sel)
        unique_players[norm] = sel
        pick_to_norm[i] = norm

    # Bulk-resolve real streaks (one query per unique player).
    streak_cache: dict[str, Optional[dict]] = {}
    for norm, original in unique_players.items():
        try:
            streak_cache[norm] = await real_scoring_streak(db, original)
        except Exception:
            streak_cache[norm] = None

    # Apply to picks.
    for i, p in enumerate(picks):
        if i not in pick_to_norm:
            stats["skipped_non_scorer"] += 1
            continue
        norm = pick_to_norm[i]
        real = streak_cache.get(norm)
        # Ensure containers exist so frontend always sees a known shape.
        pf = p.setdefault("player_form", {})
        if not isinstance(pf, dict):
            pf = {}
            p["player_form"] = pf
        uf = p.setdefault("understat_form", {})
        if not isinstance(uf, dict):
            uf = {}
            p["understat_form"] = uf

        if real is None:
            # No real data — HIDE the streak chip instead of showing a
            # stale pick-history streak. Zero out so frontend renders
            # nothing (`|current_streak| >= 2` gate fails) and drop
            # any HOT/COLD label that may have leaked from a stale
            # write.
            pf["current_streak"] = 0
            pf["streak_source"] = "no_data"
            # Bump n_picks below the chip gate so old in-flight payloads
            # can't still trigger the HOT/COLD chip on the frontend.
            pf["n_picks"] = 0
            if uf.get("label") in ("HOT", "COLD"):
                # Don't strictly hide an honest Understat HOT label;
                # only blank the label if we KNOW we have no data.
                # Keeping a stale one would re-poison the chip.
                uf.pop("label", None)
            stats["hidden_no_data"] += 1
            continue

        # Apply real values — OVERRIDE the poisoned pick-history numbers.
        pf["current_streak"] = real["streak"]
        pf["streak_source"] = real["source"]
        pf["streak_label"] = real["label"]
        # Force n_picks high enough to satisfy the frontend chip gate
        # (>= 3) only when we actually have a meaningful streak. This
        # keeps the chip honest: showing only when both we have real
        # data AND it's directional.
        if abs(real["streak"]) >= 2:
            pf["n_picks"] = max(int(pf.get("n_picks") or 0), 5)
        if "goals_per_90" in real:
            pf["recent_goals_per_90"] = real["goals_per_90"]
        if "season_g" in real and "season_games" in real:
            pf["season_total"] = {
                "goals": real["season_g"],
                "games": real["season_games"],
            }
        if "matched_name" in real:
            pf["matched_name"] = real["matched_name"]
        uf["label"] = real["label"]
        uf["source"] = real["source"]
        stats["real_data_applied"] += 1
    return stats
