"""Claude Sonnet 4.5 explainer for picks & bet-killer warnings."""
import os
import logging
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger(__name__)

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-5-20250929"


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
        "End with a one-line risk note ('Risk:' …)."
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
        return str(resp).strip(), True
    except Exception as e:
        logger.warning("AI explain failed: %s", e)
        return _fallback_explanation(pick), False


async def bet_killer_warning(pick: dict) -> tuple[str, bool]:
    """Generate a 'Why To Avoid' warning. Returns (text, is_real_ai)."""
    system = (
        "You are a sharp sports betting risk analyst for PerksLocks AI. "
        "Your job is to warn users away from dangerous bets. Be blunt, specific, "
        "stat-driven. NEVER guarantee outcomes — frame as probability vs implied. "
        "Use bullet points. End with 'Recommendation: PASS' on its own line."
    )
    chat = _build_chat(f"killer-{pick.get('external_id', 'x')}", system)
    payload = (
        f"Pick under scrutiny: {pick.get('selection')} — {pick.get('market')}\n"
        f"Sport: {pick.get('sport')} | Event: {pick.get('event')}\n"
        f"Lock Score: {pick.get('lock_score')} (BELOW 85 — Pass tier)\n"
        f"Win Probability: {pick.get('win_probability')}% vs Implied {pick.get('implied_probability')}%\n"
        f"Edge: {pick.get('edge_percent')}%\n"
        f"Factor breakdown: {pick.get('factors')}\n"
        f"Insights:\n- " + "\n- ".join(pick.get("key_insights", [])) + "\n\n"
        "Write the 'Why To Avoid' bullet list (4-6 points) explaining the risk factors."
    )
    try:
        resp = await chat.send_message(UserMessage(text=payload))
        return str(resp).strip(), True
    except Exception as e:
        logger.warning("AI killer failed: %s", e)
        return _fallback_killer(pick), False


def _fallback_explanation(pick: dict) -> str:
    bullets = "\n".join(f"• {k}" for k in pick.get("key_insights", []))
    return (
        f"Why This Pick — {pick.get('selection')} {pick.get('market')}\n\n"
        f"{bullets}\n"
        f"• Model edge: +{pick.get('edge_percent')}%\n"
        f"• Lock Score: {pick.get('lock_score')} ({pick.get('grade')})\n\n"
        f"Risk: All bets carry variance — manage bankroll accordingly."
    )


def _fallback_killer(pick: dict) -> str:
    bullets = "\n".join(f"• {k}" for k in pick.get("key_insights", []))
    return (
        f"Why To Avoid — {pick.get('selection')} {pick.get('market')}\n\n"
        f"{bullets}\n"
        f"• Lock Score only {pick.get('lock_score')} (below 85 threshold)\n"
        f"• Edge: {pick.get('edge_percent')}%\n\n"
        f"Recommendation: PASS"
    )
