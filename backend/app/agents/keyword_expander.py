"""
keyword_expander.py — ShelvesFinder v2

Dynamically broadens keyword search across 4 levels when all current
keywords have been searched but the target hasn't been met:

  Level 0 — specific      e.g. "ergonomic mesh office chair with lumbar"
  Level 1 — broader       e.g. "office chair", "desk chair"
  Level 2 — category      e.g. "home office furniture", "office seating"
  Level 3 — department    e.g. "furniture", "home office"

Works with both OpenAI and Claude via call_llm().
Provider is controlled by LLM_PROVIDER in .env.
"""

from __future__ import annotations

import json
import logging
from typing import List, Tuple

from app.models.session_state import SessionState, KEYWORD_LEVELS
from app.services.llm import call_llm, get_active_client

logger = logging.getLogger(__name__)

# How many keywords to generate per expansion level
_KEYWORDS_PER_LEVEL = {
    0: 6,   # specific
    1: 5,   # broader
    2: 4,   # category
    3: 3,   # department
}

_SYSTEM_PROMPT = (
    "You are an e-commerce search expert. Given a product and the keywords "
    "already tried, generate NEW search keywords at the requested specificity level. "
    "Never repeat keywords already tried. Return only a JSON object."
)


def _build_expansion_prompt(state: SessionState, level: int) -> str:
    level_name    = KEYWORD_LEVELS[level]
    n             = _KEYWORDS_PER_LEVEL.get(level, 5)
    already_tried = ", ".join(state.keywords_tried) if state.keywords_tried else "none"

    level_guidance = {
        0: "Very specific, multi-word phrases a shopper would use to find exactly this product.",
        1: "Moderately specific — 1-3 word phrases for the product type, no brand.",
        2: "Category-level terms — the broader section of a store this lives in.",
        3: "Department-level terms — the top-level store department (e.g. 'furniture', 'electronics').",
    }

    prompt = (
        f"Product:\n{state.product.to_prompt_str()}\n\n"
        f"Keywords already tried: {already_tried}\n\n"
        f"Task: Generate {n} NEW keywords at the '{level_name}' level.\n"
        f"Level guidance: {level_guidance.get(level, '')}\n\n"
        "Rules:\n"
        "- Do NOT include the brand name\n"
        "- Do NOT repeat any already-tried keyword\n"
        "- Make keywords suitable for Walmart category/browse page search\n"
    )
    if state.user_instructions:
        prompt += f"\nAdditional context from user: {state.user_instructions}\n"
    prompt += f'\nReturn JSON: {{"keywords": ["kw1", "kw2", ...]}}'
    return prompt


def expand_keywords(state: SessionState, llm_provider: str | None = None) -> Tuple[List[str], float]:
    """
    Advance state.keyword_expansion_level by 1 and generate new keywords.

    Returns:
        (new_keywords, cost_usd)
        new_keywords are also appended to state.keywords_pending.
    """
    next_level = state.keyword_expansion_level + 1

    if next_level >= len(KEYWORD_LEVELS):
        logger.info("[KeywordExpander] All expansion levels exhausted.")
        return [], 0.0

    state.keyword_expansion_level = next_level
    level_name = KEYWORD_LEVELS[next_level]
    logger.info(f"[KeywordExpander] Expanding to level {next_level} ({level_name})")

    if not get_active_client(provider=llm_provider):
        words    = state.product.title.split()
        fallback = [" ".join(words[:i]) for i in range(2, min(4, len(words) + 1))]
        fallback = [kw for kw in fallback if kw not in state.keywords_tried][:3]
        state.keywords_pending.extend(fallback)
        state.keywords_unbranded.extend(
            kw for kw in fallback if kw not in state.keywords_unbranded
        )
        logger.warning(f"[KeywordExpander] No LLM client; fallback keywords: {fallback}")
        return fallback, 0.0

    prompt = _build_expansion_prompt(state, next_level)

    result = call_llm(
        messages=[{"role": "user", "content": prompt}],
        system=_SYSTEM_PROMPT,
        json_mode=True,
        temperature=0.3,
        provider=llm_provider,
    )

    if result is None:
        logger.error("[KeywordExpander] call_llm returned None during expansion.")
        return [], 0.0

    try:
        data = json.loads(result.content)
        new_keywords: List[str] = data.get("keywords", [])

        # Deduplicate against already-tried and already-pending
        new_keywords = [
            kw for kw in new_keywords
            if kw and kw not in state.keywords_tried and kw not in state.keywords_pending
        ]

        cost = result.cost_usd()
        logger.info(
            f"[KeywordExpander] Level {next_level} ({level_name}): "
            f"{len(new_keywords)} new keywords | cost=${cost:.6f}"
        )
        logger.debug(f"[KeywordExpander] New keywords: {new_keywords}")

        state.keywords_pending.extend(new_keywords)
        # Expansion keywords are generated brand-free by prompt design.
        state.keywords_unbranded.extend(
            kw for kw in new_keywords if kw not in state.keywords_unbranded
        )
        state.record_cost(cost)
        return new_keywords, cost

    except Exception as e:
        logger.error(f"[KeywordExpander] Failed to parse expansion response: {e}", exc_info=True)
        return [], 0.0


def get_initial_keywords(state: SessionState, llm_provider: str | None = None) -> Tuple[List[str], float]:
    """
    Generate level-0 (specific) keywords at the start of the session.
    Delegates to keyword_agent which also uses call_llm().
    """
    from app.agents.keyword_agent import extract_keywords

    product_dict = {
        "title":       state.product.title,
        "brand":       state.product.brand,
        "description": state.product.description,
        "features":    state.product.features,
    }

    all_kw, branded, unbranded, cost = extract_keywords(
        product_dict,
        user_instructions=state.user_instructions,
        include_branded=state.include_branded,
        llm_provider=llm_provider,
    )
    unbranded_kws = [kw for kw in (unbranded or all_kw) if kw]
    # Branded keywords are queued ONLY when the "Include Branded Results"
    # setting is on; the classification is preserved on the session so the
    # setting stays enforceable downstream and provenance can be reported.
    branded_kws = [kw for kw in branded if kw] if state.include_branded else []
    branded_kws = [kw for kw in branded_kws if kw not in unbranded_kws]

    state.keywords_unbranded.extend(
        kw for kw in unbranded_kws if kw not in state.keywords_unbranded
    )
    state.keywords_branded.extend(
        kw for kw in branded_kws if kw not in state.keywords_branded
    )

    new_keywords = unbranded_kws + branded_kws
    state.keywords_pending.extend(new_keywords)
    state.record_cost(cost)

    logger.info(
        f"[KeywordExpander] Initial keywords ({len(new_keywords)}): "
        f"{len(unbranded_kws)} generic + {len(branded_kws)} branded "
        f"{new_keywords} | cost=${cost:.6f}"
    )
    return new_keywords, cost
