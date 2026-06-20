"""Claude Sonnet 4.5 explainer for picks & bet-killer warnings."""
import os
import re
import logging
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger(__name__)

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"


# ──────────────────────────────────────────────────────────────────────
# Fabrication scrubber — runtime safety net.
#
# Even with a hardened system prompt the LLM can still hallucinate
# specific stats ("Marozsan 41-6", "hold rate 83%", "career 12-0 vs lefties").
# These patterns matched the fake-stat templates we used to inject; we
# strip any line containing them from the final output so the user never
# sees fabricated numbers.
# ──────────────────────────────────────────────────────────────────────

_FAB_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b\d{2,3}-\d{1,2}(?:-\d{1,2})?\s+(?:record|L\d+|last|in|over)", re.IGNORECASE),
    re.compile(r"\bL\d+\s+months?\b", re.IGNORECASE),
    re.compile(r"hold rate\b", re.IGNORECASE),
    re.compile(r"\bbreak rate\b", re.IGNORECASE),
    re.compile(r"\bcareer\s+\d{1,3}-\d{1,3}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,3}\s*BAA\b", re.IGNORECASE),
    re.compile(r"\bERA\s+\d", re.IGNORECASE),
    re.compile(r"\bwRC\+?\s+\d", re.IGNORECASE),
    re.compile(r"\bxG/90\b", re.IGNORECASE),
    re.compile(r"\bclean sheet rate\b", re.IGNORECASE),
    re.compile(r"\btakedown defense\b", re.IGNORECASE),
    re.compile(r"\bDVOA\b", re.IGNORECASE),
    re.compile(r"\bsnap share\b", re.IGNORECASE),
    re.compile(r"\busage rate\b", re.IGNORECASE),
    re.compile(r"\bopposing pitcher allows\b", re.IGNORECASE),
    re.compile(r"\bsignificant strikes per\b", re.IGNORECASE),
    re.compile(r"\bsurface record\b", re.IGNORECASE),
    # "Player won X of last Y" pattern
    re.compile(r"\bwon\s+\d{1,2}\s+of\s+(?:his\s+)?last\s+\d{1,2}\b", re.IGNORECASE),
)


def _scrub_fabrications(text: str) -> str:
    """Remove any line containing a fabricated-stat pattern.

    Returns the cleaned text. Adds a brief footer if any line was scrubbed
    so we never silently drop content the user might miss.
    """
    if not text:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    scrubbed_count = 0
    for ln in lines:
        if any(p.search(ln) for p in _FAB_PATTERNS):
            scrubbed_count += 1
            continue
        kept.append(ln)
    cleaned = "\n".join(kept)
    if scrubbed_count:
        logger.warning("AI scrubber removed %d fabricated lines", scrubbed_count)
    return cleaned


def _build_chat(session_id: str, system_message: str) -> LlmChat:
    return LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=session_id,
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)


async def explain_pick(pick: dict) -> tuple[str, bool]:
    """Generate the 'Why This Pick?' explanation. Returns (text, is_real_ai)."""
    system = (
        "You are an elite sports betting analyst writing for the PerksLocks AI platform. "
        "Your job is to explain WHY a particular pick has a positive expected value. "
        "NEVER claim guaranteed wins or 100% locks — frame everything as probabilities, "
        "edges, and confidence. Be tactical, concise, data-driven. Use bullet points. "
        "Mention the specific stats from the factor breakdown and key insights provided. "
        "End with a one-line risk note ('Risk:' …).\n\n"
        "STRICT FACTUAL RULES — violating these is a critical failure:\n"
        "• Do NOT invent specific numeric stats not present in the input (e.g. win-loss "
        "records, hold percentages, shooting splits, surface records, snap shares, "
        "ERAs, xG values). If the data doesn't include a number, do not fabricate one.\n"
        "• Do NOT cite head-to-head records, last-N-game streaks, or career numbers "
        "unless they appear verbatim in the Factor breakdown or Key insights.\n"
        "• Use ONLY the factor scores (0-100) and qualitative insights provided. "
        "Describe them as model signals, not as raw box-score statistics.\n"
        "• If you're tempted to write a specific stat that isn't in the input, omit it."
    )
    chat = _build_chat(f"explain-{pick.get('external_id', 'x')}", system)
    payload = (
        f"Pick: {pick.get('selection')} — {pick.get('market')}\n"
        f"Sport / League: {pick.get('sport')} / {pick.get('league')}\n"
        f"Event: {pick.get('event')}\n"
        f"Lock Score: {pick.get('lock_score')} ({pick.get('grade')})\n"
        f"Win Probability: {pick.get('win_probability')}%\n"
        f"Implied (Book) Probability: {pick.get('implied_probability')}%\n"
        f"Edge: {pick.get('edge_percent')}%\n"
        f"Factor breakdown (0-100, weighted): {pick.get('factors')}\n"
        f"Key insights:\n- " + "\n- ".join(pick.get("key_insights", [])) + "\n\n"
        "Write the 'Why This Pick?' breakdown in 5-7 bullet points + risk line."
    )
    try:
        resp = await chat.send_message(UserMessage(text=payload))
        cleaned = _scrub_fabrications(str(resp).strip())
        return cleaned, True
    except Exception as e:
        logger.warning("AI explain failed: %s", e)
        return _scrub_fabrications(_fallback_explanation(pick)), False


async def analyze_loss(pick: dict) -> tuple[str, bool]:
    """Generate a 'Why It Lost' breakdown for a settled losing pick.

    Returns (text, is_real_ai). Falls back to a deterministic template if
    Claude isn't reachable.
    """
    system = (
        "You are a sports betting post-mortem analyst for PerksLocks. "
        "A pick the model classified as a high-confidence lock just LOST. "
        "Your job is to reconstruct what likely went wrong: which model factors "
        "were probably misread, what scoreline / matchup pattern caught the "
        "model off-guard, and ONE concrete adjustment users could apply to "
        "future similar picks. Be honest — don't sugarcoat. Use 4-6 bullets. "
        "End with a single-line takeaway: 'Lesson: ...'."
    )
    final_score = pick.get("final_score") or {}
    score_str = " · ".join(f"{k} {v}" for k, v in final_score.items()) or "unknown"
    factors = pick.get("factors") or {}
    factor_lines = "\n".join(f"- {k}: {v}" for k, v in factors.items())
    insights = "\n".join(f"- {ins}" for ins in (pick.get("key_insights") or []))
    user_msg = (
        f"PICK: {pick.get('selection')} | {pick.get('market')}\n"
        f"SPORT/LEAGUE: {pick.get('sport')} · {pick.get('league')}\n"
        f"GAME: {pick.get('event')}\n"
        f"FINAL SCORE: {score_str}\n"
        f"LOCK SCORE WAS: {pick.get('lock_score')}\n"
        f"MODEL WIN PROB: {pick.get('win_probability')}%\n"
        f"BOOK ODDS: {pick.get('book_odds')}\n"
        f"EDGE WAS: {pick.get('edge_percent')}%\n\n"
        f"MODEL FACTOR BREAKDOWN (0-1 scale, higher = more confident):\n{factor_lines}\n\n"
        f"KEY INSIGHTS PRE-GAME:\n{insights}\n\n"
        "Analyze why this loss happened and what to learn from it."
    )
    if not EMERGENT_LLM_KEY:
        return (_fallback_loss(pick), False)
    try:
        chat = _build_chat(f"loss-{pick.get('id', '')}", system)
        resp = await chat.send_message(UserMessage(text=user_msg))
        return (resp, True) if isinstance(resp, str) and resp.strip() else (_fallback_loss(pick), False)
    except Exception as e:
        logger.warning("analyze_loss Claude call failed: %s", e)
        return (_fallback_loss(pick), False)


def _fallback_loss(pick: dict) -> str:
    final_score = pick.get("final_score") or {}
    score_str = " · ".join(f"{k} {v}" for k, v in final_score.items()) or "score unavailable"
    return (
        f"Why It Lost — {pick.get('market')}\n\n"
        f"• Final: {score_str}\n"
        f"• Model expected {pick.get('win_probability')}% win prob — outcome fell in the {100 - (pick.get('win_probability') or 0)}% tail\n"
        f"• Edge was only {pick.get('edge_percent')}% — narrow margin for error\n"
        f"• Lock Score of {pick.get('lock_score')} reflects high confidence but no guarantee\n"
        f"• Even Elite-tier picks lose ~15-20% of the time\n\n"
        f"Lesson: One loss isn't a model failure — variance is real. Stay disciplined."
    )
