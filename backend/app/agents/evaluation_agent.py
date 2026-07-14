import logging
from typing import List, Tuple
from app.tools.similarity import score_relevance

logger = logging.getLogger(__name__)


def evaluate_and_rank(
    product_info: dict,
    candidate_urls: List[dict],
) -> Tuple[List[dict], float, float]:
    """
    Ranks candidate browse-page URLs by relevance to the product.

    Scoring is done via similarity.score_relevance() which uses:
      - OpenAI embeddings + cosine similarity when OPENAI_API_KEY is set
      - Jaccard keyword matching as a fallback (no API key required)

    NOTE: This function always uses OpenAI for embeddings regardless of
    LLM_PROVIDER, because Claude does not provide an embeddings API.

    Returns:
        (ranked_pages, confidence_score, embedding_cost_usd)
        ranked_pages: list of {"url": ..., "keyword": ..., "position": ...}
        confidence_score: average cosine similarity of top-3 results (0–1)
        embedding_cost_usd: OpenAI embedding cost (0.0 when keyword fallback used)
    """
    title        = product_info.get("title", "")
    description  = product_info.get("description", "")
    product_text = f"{title} {description}".strip()

    if not candidate_urls:
        return [], 0.0, 0.0

    # Extract a readable page title from each URL for scoring
    candidate_data = []
    for item in candidate_urls:
        url   = item.get("url", "")
        parts = url.rstrip("/").split("/")
        if "browse" in parts:
            idx         = parts.index("browse")
            title_parts = parts[idx + 1:]
        else:
            title_parts = parts[-2:]
        page_title = " ".join(title_parts).replace("-", " ")
        candidate_data.append({**item, "title": page_title})

    page_titles = [item["title"] for item in candidate_data]

    ranked_titles_with_scores, total_cost = score_relevance(product_text, page_titles)

    # Re-attach original URL / keyword / position to ranked results
    url_pool = list(candidate_data)
    top_urls: List[dict] = []
    scores:   List[float] = []

    for ranked_title, score in ranked_titles_with_scores:
        for item in url_pool:
            if item["title"] == ranked_title:
                top_urls.append({
                    "url":      item["url"],
                    "keyword":  item.get("keyword", ""),
                    "position": item.get("position"),
                })
                scores.append(score)
                url_pool.remove(item)
                break

    # Confidence = average of top-3 similarity scores
    confidence_score = (
        sum(scores[:3]) / min(3, len(scores))
        if scores else 0.0
    )

    logger.info(
        f"[EvaluationAgent] Ranked {len(top_urls)} pages | "
        f"confidence={confidence_score:.2f} | cost=${total_cost:.6f}"
    )
    return top_urls, round(confidence_score, 2), total_cost
