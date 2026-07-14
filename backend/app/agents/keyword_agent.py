import json
import logging
from typing import List, Tuple
from app.services.llm import call_llm, get_active_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — Walmart shelf-navigation context
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert in Walmart's product catalog and browse/category page structure.
Your job is to generate search keywords that match how Walmart organises its \
browse pages — not general Google SEO keywords.

Walmart browse pages follow a hierarchy:
  Department → Category → Subcategory
  e.g.  Health → Vitamins & Supplements → Probiotics & Digestive Health

Good keywords map directly to real Walmart shelf names:
  ✅  "vitamins & supplements", "digestive health", "personal care", "baby formula"
  ✅  "protein shakes", "meal replacement", "heart health supplements"

Bad keywords are long product descriptions that would never be a shelf name:
  ❌  "best probiotic 50 billion CFU lactobacillus acidophilus"
  ❌  "high potency omega-3 fish oil softgels with EPA and DHA"

Think like a Walmart category manager naming a shelf, not a shopper typing into Google.\
"""

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_prompt(
    brand: str,
    text_to_analyze: str,
    include_branded: bool,
    user_instructions: str,
) -> str:
    """
    Constructs the user-turn prompt.

    When include_branded=False  → asks for unbranded shelf terms only.
    When include_branded=True   → asks for both branded and unbranded terms.
    """
    # --- Example (few-shot, always unbranded style) ---
    example = (
        "Example — for a Nordic Naturals Omega-3 Fish Oil 1000mg supplement:\n"
        "  Good: [\"fish oil\", \"omega 3 supplements\", \"vitamins & supplements\",\n"
        "          \"heart health supplements\", \"dietary supplements\", \"grocery vitamins\"]\n"
        "  Bad:  [\"nordic naturals\", \"1000mg epa dha\", "
        "\"high potency fish oil capsules\"]\n\n"
    )

    # --- Task description ---
    if include_branded:
        task = (
            f"Generate keywords for the Walmart product below. "
            f"The brand is '{brand}'.\n\n"
            "Produce TWO lists based on following criteria:\n"
            "  1. Generic keywords that a shopper likely to search when they are looking for this type of product.\n"
            "  2. unbranded_keywords — 6-8 shelf-style terms with NO brand name.\n"
            "     Think: what Walmart category/subcategory shelves would this live on?\n"
            "     Each keyword should be 1-4 words, short enough to be a shelf name.\n\n"
            "  3. branded_keywords — 3-5 terms that INCLUDE the brand name of this brand and competition brand.\n"
            f"    Format: '{brand} <product type>' e.g. '{brand} protein shake'\n"
            "     These help find brand-specific sections or sponsored shelf placements.\n\n"
        )
        json_format = (
            '{\n'
            '  "unbranded_keywords": ["shelf term 1", "shelf term 2", ...],\n'
            '  "branded_keywords":   ["brand term 1", "brand term 2", ...]\n'
            '}'
        )
    else:
        task = (
            f"Generate keywords for the Walmart product below. "
            f"The brand is '{brand}'.\n\n"
            "Produce ONE list:\n"
            "  unbranded_keywords — 6-8 shelf-style terms with NO brand name.\n"
            "  Think: what Walmart category/subcategory shelves would this live on?\n"
            "  Include a mix of:\n"
            "    - Specific subcategory shelves  (e.g. 'probiotics', 'prenatal vitamins')\n"
            "    - Broader category shelves      (e.g. 'vitamins & supplements')\n"
            "    - Adjacent shelves it might appear on\n"
            "  Each keyword should be 1-4 words — short enough to be a shelf name.\n\n"
        )
        json_format = '{"unbranded_keywords": ["shelf term 1", "shelf term 2", ...]}'

    # --- Rules ---
    rules = (
        "Rules:\n"
        f"  • Do NOT include the brand name '{brand}' in unbranded_keywords\n"
        "  • Do NOT generate long product descriptions\n"
        "  • Keywords must be realistic Walmart shelf/category names\n"
        "  • Do NOT repeat keywords\n\n"
    )

    # --- User instructions (optional) ---
    context = ""
    if user_instructions:
        context = f"Additional context from user:\n  {user_instructions}\n\n"

    return (
        f"{example}"
        f"Product to analyse:\n{text_to_analyze}\n\n"
        f"{task}"
        f"{rules}"
        f"{context}"
        f"Return JSON:\n{json_format}"
    )


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_keywords(
    product_info: dict,
    user_instructions: str = "",
    include_branded: bool = False,
    llm_provider: str | None = None,
) -> Tuple[List[str], List[str], List[str], float]:
    """
    Extracts Walmart-shelf-focused keywords from product info.

    Args:
        product_info:      dict with title, brand, description, features
        user_instructions: optional free-text context to guide keyword focus
        include_branded:   when True, also generates brand-name keywords

    Returns:
        (all_keywords, branded_keywords, unbranded_keywords, cost_usd)
    """
    title         = product_info.get("title", "")
    description   = product_info.get("description", "")
    features_list = product_info.get("features", [])
    features      = "\n".join(features_list) if isinstance(features_list, list) else str(features_list)
    brand         = product_info.get("brand", "")

    text_to_analyze = (
        f"  Brand:       {brand}\n"
        f"  Title:       {title}\n"
        f"  Description: {description}\n"
        f"  Features:\n"
        + "\n".join(f"    - {f}" for f in (features_list or [])[:8])
    )

    client = get_active_client(provider=llm_provider)
    if not client:
        effective = llm_provider or __import__('app.config', fromlist=['settings']).settings.llm_provider
        logger.warning(
            f"[KeywordAgent] No LLM client available for provider='{effective}'. "
            f"Check API key and package installation. Falling back to rule-based extraction."
        )
    else:
        try:
            prompt = _build_prompt(brand, text_to_analyze, include_branded, user_instructions)

            effective = llm_provider or __import__('app.config', fromlist=['settings']).settings.llm_provider
            logger.info(
                f"[KeywordAgent] Calling LLM — include_branded={include_branded} "
                f"provider={effective}"
            )

            result = call_llm(
                messages=[{"role": "user", "content": prompt}],
                system=_SYSTEM_PROMPT,
                json_mode=True,
                temperature=0.1,
                provider=llm_provider,
            )

            if result is None:
                raise RuntimeError(
                    f"call_llm returned None for provider='{effective}'. "
                    "Check model name in config and API key validity."
                )

            logger.info(f"[KeywordAgent] Response: {result.content[:300]}")

            data      = json.loads(result.content)
            branded   = data.get("branded_keywords", [])   if include_branded else []
            unbranded = data.get("unbranded_keywords", [])

            if not isinstance(branded, list):   branded   = []
            if not isinstance(unbranded, list): unbranded = []

            all_keywords = unbranded + branded
            cost = result.cost_usd()

            logger.info(
                f"[KeywordAgent] {len(unbranded)} unbranded + {len(branded)} branded keywords | "
                f"tokens={result.prompt_tokens}p+{result.completion_tokens}c | cost=${cost:.6f}"
            )

            if all_keywords:
                return all_keywords, branded, unbranded, cost

        except Exception as e:
            logger.error(f"[KeywordAgent] LLM extraction failed: {e}", exc_info=True)

    # Fallback — no LLM client or call failed
    logger.warning("[KeywordAgent] Falling back to rule-based keyword extraction.")
    fallback_kw = _fallback_extract_keywords(title)
    return fallback_kw, [], fallback_kw, 0.0


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------

def _fallback_extract_keywords(title: str) -> List[str]:
    """Simple noun-phrase extraction when no LLM is available."""
    words = title.split()
    if len(words) <= 3:
        return [title]
    keywords = [" ".join(words[:min(4, len(words))])]
    if len(words) >= 6:
        keywords.append(" ".join(words[2:5]))
    keywords.append(title)
    return keywords[:3]


# Backward-compat alias
fallback_extract_keywords = _fallback_extract_keywords
