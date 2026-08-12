"""Magic adapter package."""
from services.magic.adapters.soccer import build_soccer_evidence
from services.magic.adapters.tennis import build_tennis_evidence
from services.magic.adapters.playerprop import build_playerprop_evidence


async def build_evidence(db, pick: dict):
    """Sport dispatcher — routes to the right adapter."""
    sport = (pick.get("sport") or "").strip()
    if sport == "Soccer":
        return await build_soccer_evidence(db, pick)
    if sport == "Tennis":
        return await build_tennis_evidence(db, pick)
    if sport in ("MLB", "NBA", "NFL"):
        return await build_playerprop_evidence(db, pick, sport=sport)
    # Unsupported sport → return a MagicOutput with INSUFFICIENT tier.
    from services.magic.contract import MagicOutput, MagicTier
    return MagicOutput(
        pick_id=pick.get("id") or "", sport=sport,
        market=pick.get("market"),
        magic_tier=MagicTier.INSUFFICIENT_EVIDENCE,
    )


__all__ = ["build_evidence", "build_soccer_evidence",
            "build_tennis_evidence", "build_playerprop_evidence"]
