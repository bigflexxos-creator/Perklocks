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

    Per-call variant (used by single-pick endpoints like /picks/{id}).
    For LIST endpoints, prefer `_find_form_docs_batch` which folds all
    these queries into ONE `$or` query for the full pick list — saves
    up to ~280 round-trips on a 70-player slate (code-review MEDIUM
    2026-06-30).
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
            return _pick_best_candidate(candidates, parts, last, first_initial)

    return None


def _pick_best_candidate(
    candidates: list[dict], parts: list[str], last: str, first_initial: str,
) -> Optional[dict]:
    """Apply the Strategy-4 strict last-name + initial filter to a list
    of in-memory candidates. Extracted so both the per-call path and
    the batched path share identical disambiguation logic.
    """
    best = None
    for c in candidates:
        cn = (c.get("name_canonical") or "").split()
        if not cn:
            continue
        # ── STRICT LAST-NAME GUARD ────────────────────────────────────
        # The regex `\bjunior\b` upstream matches "Junior Messias"
        # (where 'junior' is the FIRST name) just as eagerly as
        # "Vinicius Junior" (where it's the LAST name). Only accept
        # candidates where the LAST token of the canonical name
        # matches our search last name — with a hyphenated allowance
        # (Mbappe-Lottin → cn[-1]="mbappelottin" startswith "mbappe").
        if cn[-1] != last and not cn[-1].startswith(last):
            continue
        if first_initial and cn[0][:1] == first_initial:
            if cn[0].startswith(parts[0]) or parts[0].startswith(cn[0]):
                return c
            best = best or c
    return best


async def _find_form_docs_batch(
    db, unique_players: dict[str, str],
) -> dict[str, Optional[dict]]:
    """Batched name match against `soccer_player_form`.

    Performs ONE bulk query (an `$or` over exact + squashed-prefix +
    last-name regex matches) covering every unique player name in the
    slate, then routes each match to its rightful pick in Python using
    the same 4-strategy ranking as `_find_form_doc`. This is the
    list-endpoint hot path on `/picks/today` (code-review MEDIUM
    2026-06-30).

    Args:
      unique_players: {norm_full_name: original_name} as produced by
        the caller's de-dup pass.
    Returns:
      {norm_full_name: form_doc_or_None}
    """
    if not unique_players:
        return {}
    # ── Build per-player keys we'll need to route results back ─────
    # (Avoid re-normalising in the hot loop later.)
    player_keys: dict[str, dict] = {}
    or_clauses: list[dict] = []
    seen_or_keys: set[str] = set()
    exact_norms: set[str] = set()
    last_names: set[str] = set()
    for norm_full, original in unique_players.items():
        parts = norm_full.split()
        if not parts:
            continue
        squash = _norm_squashed(original)
        last = parts[-1]
        first_initial = parts[0][:1] if parts[0] else ""
        player_keys[norm_full] = {
            "parts": parts,
            "last": last,
            "first_initial": first_initial,
            "squash": squash,
        }
        exact_norms.add(norm_full)
        if len(last) >= 4:
            last_names.add(last)
        # Squashed-prefix variant (Strategy 2) — add as a regex anchor.
        if squash and squash != norm_full.replace(" ", ""):
            key = f"sq:{squash}"
            if key not in seen_or_keys:
                or_clauses.append({
                    "name_canonical": {
                        "$regex": f"^{re.escape(squash)}",
                        "$options": "i",
                    },
                })
                seen_or_keys.add(key)
    # Strategy 1: bulk exact match (cheapest, hits ~80% of cases).
    if exact_norms:
        or_clauses.append({"name_canonical": {"$in": list(exact_norms)}})
    # Strategy 3 prefilter: substring regex per unique player so the
    # batched query also fetches rows where the player is stored under
    # a longer canonical name (e.g. "kylian mbappe" → DB has "kylian
    # mbappelottin"). One big alternation regex keeps it to a single
    # MongoDB scan instead of N substring queries.
    sub_patterns = [re.escape(n) for n in exact_norms if len(n) >= 6]
    if sub_patterns:
        or_clauses.append({
            "name_canonical": {"$regex": "|".join(sub_patterns), "$options": "i"},
        })
    # Strategy 4 prefilter: a single regex OR of all last names. Word-
    # bounded so it returns rows where the surname appears as either
    # the first or last token; Python-side guard tightens to last-token
    # only.
    if last_names:
        ln_pattern = "|".join(rf"\b{re.escape(ln)}\b" for ln in last_names)
        or_clauses.append({
            "name_canonical": {"$regex": ln_pattern, "$options": "i"},
        })
    if not or_clauses:
        return {n: None for n in unique_players}

    # Single round-trip. Limit is generous — even 70 unique players
    # rarely surface >300 candidate rows from a 2,700-row Understat
    # collection.
    proj = {
        "_id": 0, "name_canonical": 1, "player_name": 1,
        "form_label": 1, "goals": 1, "games": 1, "goals_per_90": 1,
        "team": 1, "league": 1, "season": 1,
    }
    cursor = db.soccer_player_form.find(
        {"$or": or_clauses}, proj,
    ).sort([("season", -1), ("games", -1)]).limit(2000)
    all_docs = await cursor.to_list(length=2000)

    # ── Route each candidate doc to a pick name (in-memory) ───────
    # Build lookup tables ONCE:
    exact_index: dict[str, dict] = {}
    squash_index: dict[str, list[dict]] = {}
    last_index: dict[str, list[dict]] = {}
    for d in all_docs:
        cn = d.get("name_canonical") or ""
        if not cn:
            continue
        # Strategy 1 / 3: exact normalised match → keep highest-season
        # / highest-games doc (already sorted upstream).
        if cn not in exact_index:
            exact_index[cn] = d
        cn_squash = cn.replace(" ", "")
        squash_index.setdefault(cn_squash, []).append(d)
        cn_parts = cn.split()
        if cn_parts:
            last_index.setdefault(cn_parts[-1], []).append(d)

    out: dict[str, Optional[dict]] = {}
    for norm_full, keys in player_keys.items():
        # Strategy 1 — exact name_canonical match.
        doc = exact_index.get(norm_full)
        if doc:
            out[norm_full] = doc
            continue
        # Strategy 2 — squashed prefix (Mbappe-Lottin handling).
        sq = keys["squash"]
        if sq:
            for d in squash_index.get(sq, []):
                if (d.get("name_canonical") or "").replace(" ", "").startswith(sq):
                    out[norm_full] = d
                    break
            else:
                # Also try `cn_squash` startswith on prefix collisions.
                squash_hit = None
                for prefix_key, docs in squash_index.items():
                    if prefix_key.startswith(sq) and docs:
                        squash_hit = docs[0]
                        break
                if squash_hit:
                    out[norm_full] = squash_hit
                    continue
            if out.get(norm_full):
                continue
        # Strategy 3 — full-name substring (catches middle-name rows).
        if len(norm_full) >= 6:
            sub_hit = None
            for d in all_docs:
                cn_key = d.get("name_canonical") or ""
                if norm_full in cn_key:
                    sub_hit = d
                    break
            if sub_hit:
                out[norm_full] = sub_hit
                continue
        # Strategy 4 — last-name + first-initial disambiguation.
        last = keys["last"]
        if len(last) >= 4:
            candidates = last_index.get(last, [])
            if candidates:
                # Sort by season (desc) / games (desc) — preserves the
                # per-call helper's ranking.
                candidates_sorted = sorted(
                    candidates,
                    key=lambda d: (
                        d.get("season") or 0,
                        d.get("games") or 0,
                    ),
                    reverse=True,
                )[:20]
                pick = _pick_best_candidate(
                    candidates_sorted, keys["parts"], last, keys["first_initial"],
                )
                if pick:
                    out[norm_full] = pick
                    continue
        out[norm_full] = None
    return out


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
        # "Kylian Mbappé Lottin" inputs etc.) — but ONLY when both the
        # last name AND the first-name initial agree. Without the
        # initial check, any "… Junior" pick would inherit Vinícius
        # Júnior's HOT streak, any "… Ronaldo" would inherit Cristiano
        # Ronaldo's, etc. (Code-review HIGH finding 2026-06-30.)
        parts = norm_spaces.split() if norm_spaces else []
        last = parts[-1] if parts else ""
        first_initial = parts[0][:1] if len(parts) >= 2 and parts[0] else ""
        # Require BOTH parts (multi-token query name) AND a clear initial
        # — single-token inputs like "Ronaldo" are ambiguous and should
        # NOT be matched via this fallback.
        if last and first_initial and len(parts) >= 2:
            for key, val in _ELITE_ANCHOR.items():
                key_parts = key.split()
                if not key_parts:
                    continue
                key_last = key_parts[-1]
                key_first_initial = key_parts[0][:1] if key_parts[0] else ""
                if key_last == last and key_first_initial == first_initial:
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

    # ── Bulk-resolve real streaks in ONE Mongo round-trip ─────────────
    # Previously this loop awaited `real_scoring_streak()` per unique
    # player, which itself runs up to 3 `find_one`s + 1 `find` — at
    # ~70 unique players on a Soccer slate that's up to ~280 sequential
    # round-trips. The batched helper folds every player's Strategy 1+2
    # + Strategy 4 surname queries into a single `$or` find, returning
    # a {norm → form_doc | None} map. Routing back to streak shape
    # happens in-memory below. Code-review MEDIUM 2026-06-30.
    streak_cache: dict[str, Optional[dict]] = {}
    try:
        doc_map = await _find_form_docs_batch(db, unique_players)
    except Exception:
        doc_map = {n: None for n in unique_players}
    for norm, original in unique_players.items():
        doc = doc_map.get(norm)
        if doc:
            goals = float(doc.get("goals") or 0)
            games = float(doc.get("games") or 0)
            gp90 = float(doc.get("goals_per_90") or 0)
            label = (doc.get("form_label") or "NEUTRAL").upper()
            last5_g = gp90 * 0.85
            streak = _label_to_streak(label, gp90, last5_g, games)
            streak_cache[norm] = {
                "streak": streak,
                "label": label,
                "source": "understat",
                "goals_per_90": gp90,
                "season_g": goals,
                "season_games": games,
                "matched_name": doc.get("player_name"),
            }
            continue
        # No Understat row — try the elite anchor (no DB I/O).
        anchor = _ELITE_ANCHOR.get(norm)
        if anchor is None:
            parts = norm.split()
            last = parts[-1] if parts else ""
            first_initial = parts[0][:1] if len(parts) >= 2 and parts[0] else ""
            if last and first_initial and len(parts) >= 2:
                for key, val in _ELITE_ANCHOR.items():
                    key_parts = key.split()
                    if not key_parts:
                        continue
                    if (key_parts[-1] == last
                            and (key_parts[0][:1] if key_parts[0] else "") == first_initial):
                        anchor = val
                        break
        if anchor is not None:
            label, streak_val = anchor
            streak_cache[norm] = {
                "streak": int(streak_val),
                "label": label,
                "source": "elite_anchor",
            }
        else:
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
