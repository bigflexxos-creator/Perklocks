"""Tennis Deep Signals — Signal Engine Phase B.5.

Same additive pattern as `mlb_deep.py` and `soccer_deep.py` — reads
from tennis-specific enrichment ALREADY on the pick document
(`tennis_components` computed by `tennis_engine.apply_tennis_engine`)
plus optional lookups from the `tennis_players` collection (surface
Elo delta + recent match load). Non-tennis picks fast no-op.

What we award points for:

  1. `surface_fit`     — tennis_components.surface score (0-100). Elite
                         surface specialists (≥80) get a durable-edge
                         bump; poor surface fit (≤35) is a fade signal.
  2. `serve_return`    — tennis_components.serve_return. Dominant
                         hold/break profiles (≥80) are the highest-
                         signal predictor in tennis.
  3. `motivation`      — tennis_components.motivation. Low (<50)
                         penalises the pick (e.g. round-of-32 tune-ups
                         where the fav mails it in).
  4. `variance_risk`   — tennis_components.variance. High (>60) means
                         history of upsets / retirements / walkovers.
  5. `elo_edge`        — from tennis_players collection: surface-specific
                         Elo delta between pick_side and opponent. This
                         is orthogonal to book-odds and captures structural
                         quality gaps books sometimes miss (esp. on 250s).
  6. `recent_load`     — matches_7d count from tennis_players. Heavy
                         schedule (>4 matches in 7 days) → fade signal
                         for the tired side.

All fields except tennis_components are optional — the calculator
degrades gracefully to component-only signal when db is unavailable.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("lockscore.services.signal_engine.tennis_deep")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_opponent(pick: dict) -> Optional[str]:
    """From an event 'Giron M. vs Choinski J.' + selection 'Giron M.',
    return the opposing player's name. Returns None when we can't
    parse cleanly (rare — tennis_extra events always use ` vs `)."""
    event = pick.get("event") or ""
    sel = (pick.get("pick_side") or pick.get("selection") or "").strip()
    if not event or " vs " not in event or not sel:
        return None
    left, right = event.split(" vs ", 1)
    left = left.strip()
    right = right.strip()
    # Cheap containment match — tennis_extra names look like "Giron M."
    if sel in left or left in sel:
        return right
    if sel in right or right in sel:
        return left
    # Fallback: last-name match
    sel_last = sel.split()[0].rstrip(".").lower()
    if sel_last and sel_last in left.lower():
        return right
    if sel_last and sel_last in right.lower():
        return left
    return None


_NAME_NORM_RE = re.compile(r"[^a-z\s]")
_TE_NAME_RE = re.compile(r"^([A-Za-z\-']+)\s+([A-Za-z])\.?$")


def _norm_name(name: str) -> str:
    """Match `tennis_players.name_norm` format: lowercase, strip
    punctuation and honorifics."""
    if not name:
        return ""
    n = name.strip().lower()
    n = _NAME_NORM_RE.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _te_name_parts(name: str) -> Optional[tuple[str, str]]:
    """Detect TennisExplorer-style 'Lastname X.' names and return
    ``(lastname_lower, initial_lower)``. None when the name isn't in
    that format (already full name)."""
    if not name:
        return None
    m = _TE_NAME_RE.match(name.strip())
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower()


async def _lookup_tennis_player(db, name: str, surface: str) -> Optional[dict]:
    """Best-effort lookup that handles both full names ('Marcos Giron')
    and TennisExplorer initial format ('Giron M.'). Returns the raw
    tennis_players doc or None. Never raises."""
    if not name:
        return None
    try:
        # Case 1: TennisExplorer 'Lastname X.' — hunt by last-name suffix
        # match. Multiple candidates possible (e.g., 'Nadal R.' could match
        # 'Rafael Nadal'); we pick the initial that starts a first-name.
        parts = _te_name_parts(name)
        if parts:
            last, initial = parts
            # First try: full regex 'firstname startswith initial AND lastname'
            candidates = await db.tennis_players.find(
                {"name_norm": {
                    "$regex": rf"^{initial}\S*\s+{re.escape(last)}$",
                    "$options": "i",
                }},
                {"elo_overall": 1, f"elo_{surface}": 1, "matches_7d": 1, "name_norm": 1},
            ).limit(3).to_list(length=3)
            if candidates:
                return candidates[0]
            # Fallback: any lastname match — returns highest-Elo player
            candidates = await db.tennis_players.find(
                {"name_norm": {"$regex": rf"\s+{re.escape(last)}$", "$options": "i"}},
                {"elo_overall": 1, f"elo_{surface}": 1, "matches_7d": 1, "name_norm": 1},
            ).sort("elo_overall", -1).limit(1).to_list(length=1)
            return candidates[0] if candidates else None

        # Case 2: full name — direct prefix match on name_norm.
        nn = _norm_name(name)
        if not nn:
            return None
        doc = await db.tennis_players.find_one(
            {"name_norm": {"$regex": f"^{re.escape(nn)}"}},
            {"elo_overall": 1, f"elo_{surface}": 1, "matches_7d": 1, "name_norm": 1},
        )
        return doc
    except Exception as e:
        logger.debug("tennis player lookup failed for %r: %s", name, e)
        return None


def _surface_from_components(comp: dict) -> str:
    return (comp.get("surface_name") or "Hard").lower()


def _elo_for_surface(player_doc: dict, surface: str) -> float:
    """Return the appropriate Elo for the given surface, falling back
    to overall Elo when the surface-specific rating is missing."""
    key = f"elo_{surface}"
    val = player_doc.get(key)
    if isinstance(val, (int, float)) and val > 0:
        return float(val)
    return float(player_doc.get("elo_overall") or 1500.0)


def _matches_last_7d(player_doc: dict) -> int:
    """Count entries in `matches_7d` whose ISO timestamp is within the
    last 7 days of NOW. The field name is aspirational — the array
    accumulates historical matches without being pruned, so we do the
    date filter here (2026-07 finding)."""
    from datetime import datetime, timezone, timedelta
    matches = player_doc.get("matches_7d") or []
    if not matches:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    n = 0
    for m in matches:
        iso = m.get("iso") if isinstance(m, dict) else None
        if not iso:
            continue
        try:
            # Handle both '+00:00' and 'Z' suffixes.
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                n += 1
        except Exception:
            continue
    return n


async def enrich_tennis_pick(db, pick: dict) -> dict:
    """Attach `tennis_deep` block to a single tennis pick. Async because
    it does two point-lookups into `tennis_players`. Idempotent.

    Attaches:
      pick['tennis_deep'] = {
        'surface':          'hard' | 'clay' | 'grass' | ...,
        'tour_tier':        int | None,   # 1 = Grand Slam, 5 = Challenger
        'surface_fit':      float | None,     # 0-100 from tennis_components
        'serve_return':     float | None,
        'motivation':       float | None,
        'variance':         float | None,
        'is_99_lock_elig':  bool,
        'elo_edge':         float | None,   # + = pick side is stronger
        'pick_matches_7d':  int | None,
        'opp_matches_7d':   int | None,
      }
    """
    if (pick.get("sport") or "").lower() != "tennis":
        return pick

    comp = pick.get("tennis_components") or {}
    if not isinstance(comp, dict):
        comp = {}

    surface = _surface_from_components(comp)
    tier = comp.get("tier") if isinstance(comp.get("tier"), int) else None

    surface_fit  = comp.get("surface")     if isinstance(comp.get("surface"),     (int, float)) else None
    serve_return = comp.get("serve_return") if isinstance(comp.get("serve_return"), (int, float)) else None
    motivation   = comp.get("motivation")  if isinstance(comp.get("motivation"),  (int, float)) else None
    variance     = comp.get("variance")    if isinstance(comp.get("variance"),    (int, float)) else None
    is_99_elig   = bool(comp.get("is_99_lock_eligible"))

    # Best-effort Elo lookup — never let a DB miss break the pipeline.
    elo_edge: Optional[float] = None
    pick_load: Optional[int] = None
    opp_load: Optional[int] = None
    pick_name = (pick.get("pick_side") or pick.get("selection") or "").strip()
    opp_name = _extract_opponent(pick) or ""
    if db is not None and pick_name:
        try:
            pdoc = await _lookup_tennis_player(db, pick_name, surface)
            odoc = await _lookup_tennis_player(db, opp_name, surface) if opp_name else None
            if pdoc:
                p_elo = _elo_for_surface(pdoc, surface)
                pick_load = _matches_last_7d(pdoc)
                if odoc:
                    o_elo = _elo_for_surface(odoc, surface)
                    opp_load = _matches_last_7d(odoc)
                    elo_edge = round(p_elo - o_elo, 1)
        except Exception as e:
            logger.debug("tennis_deep Elo lookup failed for %s: %s", pick_name, e)

    pick["tennis_deep"] = {
        "surface":          surface,
        "tour_tier":        tier,
        "surface_fit":      round(_f(surface_fit),  1) if surface_fit  is not None else None,
        "serve_return":     round(_f(serve_return), 1) if serve_return is not None else None,
        "motivation":       round(_f(motivation),   1) if motivation   is not None else None,
        "variance":         round(_f(variance),     1) if variance     is not None else None,
        "is_99_lock_elig":  is_99_elig,
        "elo_edge":         elo_edge,
        "pick_matches_7d":  pick_load,
        "opp_matches_7d":   opp_load,
    }
    return pick
