"""
similarity.py — ShelvesFinder v2

Ranks Walmart browse page titles against a product description using
OpenAI text embeddings + cosine similarity.

IMPORTANT — embeddings always use OpenAI regardless of LLM_PROVIDER:
  Claude does not provide an embeddings API.  If OPENAI_API_KEY is not
  set (e.g. the user switched to LLM_PROVIDER=claude without an OpenAI
  key), score_relevance() automatically falls back to keyword/Jaccard
  matching so the pipeline never hard-fails.
"""

import logging
import math
from typing import List, Tuple

from app.config import settings
from app.services.llm import get_openai_client   # always OpenAI — intentional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------

def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)


def keyword_match_score(text1: str, text2: str) -> float:
    """Jaccard-like term overlap — used when embeddings are unavailable."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / float(len(words1 | words2))


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------

def get_embedding(client, text: str) -> Tuple[List[float], float]:
    """Fetch an OpenAI embedding vector for `text`. Returns (vector, cost_usd)."""
    try:
        response = client.embeddings.create(
            input=text,
            model=settings.openai_embedding_model,
        )
        usage = response.usage.prompt_tokens if response.usage else 0
        cost  = usage * 0.00000002   # text-embedding-3-small: $0.02 / 1M tokens
        return response.data[0].embedding, cost
    except Exception as e:
        logger.error(f"[Similarity] Embedding request failed: {e}")
        return [], 0.0


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_relevance(
    product_text: str,
    page_titles: List[str],
) -> Tuple[List[Tuple[str, float]], float]:
    """
    Score and rank `page_titles` by relevance to `product_text`.

    Strategy (in priority order):
      1. OpenAI embeddings + cosine similarity  (best quality)
      2. Jaccard keyword overlap                (fallback — no API needed)

    The fallback activates automatically when:
      - OPENAI_API_KEY is not set  (e.g. LLM_PROVIDER=claude only)
      - The embedding API call fails for any reason

    Returns:
        (ranked_list, total_cost_usd)
        ranked_list: [(page_title, score), ...] sorted descending by score
    """
    # Embeddings always use OpenAI — Claude has no embeddings API.
    openai_client = get_openai_client()
    use_embeddings = openai_client is not None

    if not use_embeddings:
        logger.warning(
            "[Similarity] OpenAI client unavailable — embeddings disabled. "
            "This is expected when LLM_PROVIDER=claude and no OPENAI_API_KEY is set. "
            "Falling back to keyword/Jaccard scoring (lower accuracy)."
        )

    total_cost      = 0.0
    product_embedding: List[float] = []
    results: List[Tuple[str, float]] = []

    # Fetch product embedding once (reused for all page titles)
    if use_embeddings:
        product_embedding, p_cost = get_embedding(openai_client, product_text)
        total_cost += p_cost
        if not product_embedding:
            logger.warning("[Similarity] Product embedding failed — switching to keyword matching.")
            use_embeddings = False

    for title in page_titles:
        if use_embeddings:
            title_embedding, t_cost = get_embedding(openai_client, title)
            total_cost += t_cost
            score = (
                cosine_similarity(product_embedding, title_embedding)
                if title_embedding
                else keyword_match_score(product_text, title)
            )
        else:
            score = keyword_match_score(product_text, title)

        results.append((title, score))

    results.sort(key=lambda x: x[1], reverse=True)

    logger.info(
        f"[Similarity] Scored {len(results)} pages using "
        f"{'embeddings' if use_embeddings else 'keyword matching'} | "
        f"cost=${total_cost:.6f}"
    )
    return results, total_cost
