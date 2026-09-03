"""PublishedPickContract — one immutable accessor for every consumer
======================================================================

PERKLOCKS-MAIN 34 · P0 root closure.

Prior to this module, every consumer (Locks board, Pick Breakdown,
Rollover source, Parlay source, My Bets, History, Analytics, Lab
research references) reached into raw pick documents by hand — mixing
frozen canonical publication truth with mutable legacy aliases.  That
inconsistency is what allowed:

  * `published_grade` and `grade` to disagree
  * `published_line`  and `line`  to disagree
  * `published_odds`  and `odds`  to disagree
  * `published_lock_score` and `lock_score` to disagree post-mutation

`PublishedPickContract` is the ONE authoritative accessor.  Consumers
call `PublishedPickContract.from_pick(pick_doc).as_dict()` and receive
a frozen namedtuple with the exact fields the immutable publication
snapshot recorded.  Any decorator / signal engine that mutates the
raw document AFTER publication CANNOT alter the values returned by
this contract.

Canonical frozen fields (the wager as published, forever):
    canonical_pick_id, event_id, sport, league,
    player_identity, team_identity, opponent_identity,
    canonical_market_family, provider_market_key,
    line_type,               # standard | alternate
    market_class,            # game_market | player_prop
    selection, side, line,
    sportsbook,
    published_odds,
    win_expected,            # aka model_win_prob / win_probability
    published_lock_score, published_grade,
    publication_state, publication_revision, board_version,
    published_at,
    evidence_snapshot_version.

Contract rule:  each field is derived from the frozen `published_*`
value when present, and only falls back to the mutable alias when the
canonical value is missing (legacy rows before publication_barrier
landed).  The `_provenance` map records which value class supplied
each field so consumers / tests can catch the day a mutable alias
outranks the canonical field.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


_CANONICAL_KEYS = (
    "canonical_pick_id", "event_id", "sport", "league",
    "player_identity", "team_identity", "opponent_identity",
    "canonical_market_family", "provider_market_key",
    "line_type", "market_class",
    "selection", "side", "line",
    "sportsbook", "published_odds", "win_expected",
    "published_lock_score", "published_grade",
    "publication_state", "publication_revision", "board_version",
    "published_at", "evidence_snapshot_version",
)


def _first(*vals):
    """Return the first value that is not None (does NOT filter falsy 0)."""
    for v in vals:
        if v is not None:
            return v
    return None


@dataclass(frozen=True)
class PublishedPickContract:
    """Immutable frozen view of a published wager."""
    canonical_pick_id: Optional[str] = None
    event_id: Optional[str] = None
    sport: Optional[str] = None
    league: Optional[str] = None
    player_identity: Optional[str] = None
    team_identity: Optional[str] = None
    opponent_identity: Optional[str] = None
    canonical_market_family: Optional[str] = None
    provider_market_key: Optional[str] = None
    line_type: Optional[str] = None
    market_class: Optional[str] = None
    selection: Optional[str] = None
    side: Optional[str] = None
    line: Optional[float] = None
    sportsbook: Optional[str] = None
    published_odds: Optional[int] = None
    win_expected: Optional[float] = None
    published_lock_score: Optional[float] = None
    published_grade: Optional[str] = None
    publication_state: Optional[str] = None
    publication_revision: Optional[int] = None
    board_version: Optional[str] = None
    published_at: Optional[str] = None
    evidence_snapshot_version: Optional[int] = None
    _provenance: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_provenance", None)
        return d

    def provenance(self) -> Dict[str, str]:
        return dict(self._provenance)

    @classmethod
    def from_pick(cls, pick: Dict[str, Any]) -> "PublishedPickContract":
        if not isinstance(pick, dict):
            raise TypeError("PublishedPickContract.from_pick requires a dict")
        prov: Dict[str, str] = {}

        def _pick(name: str, canonical: Optional[str], *legacy: str) -> Any:
            if canonical is not None and pick.get(canonical) is not None:
                prov[name] = "canonical"
                return pick[canonical]
            for lk in legacy:
                if pick.get(lk) is not None:
                    prov[name] = f"legacy:{lk}"
                    return pick[lk]
            prov[name] = "absent"
            return None

        # Identity
        cpi   = _pick("canonical_pick_id", "canonical_pick_id", "id")
        eid   = _pick("event_id", "canonical_event_id", "event_id", "provider_event_id")
        sport = _pick("sport", "sport")
        lg    = _pick("league", "league")
        pl    = _pick("player_identity", "canonical_player_id", "player_name",
                       "elite_player_name")
        tm    = _pick("team_identity", "canonical_team_id", "home_team_name")
        opp   = _pick("opponent_identity", "canonical_opponent_id",
                       "away_team_name")
        # Market
        cmf   = _pick("canonical_market_family", "canonical_market_family",
                       "market_family")
        pmk   = _pick("provider_market_key", "provider_market_key", "market_key")
        # line_type + market_class are derived when not carried explicitly.
        lt    = pick.get("line_type") or ("alternate" if pick.get("is_alt") else "standard")
        prov["line_type"] = "canonical" if pick.get("line_type") else "derived"
        mkt_cls = pick.get("market_class")
        if not mkt_cls:
            has_player = bool(pl or pick.get("elite_player") or pick.get("player"))
            mkt_cls = "player_prop" if has_player else "game_market"
            prov["market_class"] = "derived"
        else:
            prov["market_class"] = "canonical"
        # Wager terms
        sel   = _pick("selection", "canonical_selection", "provider_selection", "selection")
        side  = _pick("side", "published_side", "side")
        line  = _pick("line", "published_line", "provider_line", "line")
        # Price + book
        bk    = _pick("sportsbook", "sportsbook", "bookmaker")
        odds  = _pick("published_odds", "published_odds", "american_odds",
                        "book_odds", "odds")
        # Lock + grade
        we    = _pick("win_expected", "published_probability",
                        "model_win_prob", "win_probability")
        pls   = _pick("published_lock_score", "published_lock_score", "lock_score")
        pg    = _pick("published_grade", "published_grade", "grade")
        # Publication metadata
        ps    = _pick("publication_state", "publication_state")
        pr    = _pick("publication_revision", "publication_revision")
        bv    = _pick("board_version", "board_version")
        pat   = _pick("published_at", "published_at", "publication_published_at")
        esv   = _pick("evidence_snapshot_version",
                        "evidence_snapshot_version", "snapshot_version")

        return cls(
            canonical_pick_id=cpi, event_id=eid, sport=sport, league=lg,
            player_identity=pl, team_identity=tm, opponent_identity=opp,
            canonical_market_family=cmf, provider_market_key=pmk,
            line_type=lt, market_class=mkt_cls,
            selection=sel, side=side, line=line,
            sportsbook=bk, published_odds=odds, win_expected=we,
            published_lock_score=pls, published_grade=pg,
            publication_state=ps, publication_revision=pr, board_version=bv,
            published_at=pat, evidence_snapshot_version=esv,
            _provenance=prov,
        )


def contract_dict(pick: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience shortcut for consumers that only need the dict form."""
    return PublishedPickContract.from_pick(pick).as_dict()
