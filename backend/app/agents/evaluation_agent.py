import logging
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple
from app.tools.similarity import score_relevance

logger = logging.getLogger(__name__)

# Key under which each ranked page carries its relevance score. This is the
# single documented name for the score across the backend: evaluate_and_rank
# emits it, orchestrator._call_evaluate reads it onto BrowsePage.relevance_score,
# and it flows through to ShelfResult and the final report. Test doubles that
# stand in for evaluate_and_rank must emit this key too — see
# tests/test_evaluation_agent.py::TestReturnContract, which pins the shape so a
# double can never silently diverge from the real function again.
RELEVANCE_SCORE_KEY = "relevance_score"


def _title_from_url(url: str) -> str:
    """Derive a readable page title from a browse URL slug (fallback only)."""
    parts = url.rstrip("/").split("/")
    if "browse" in parts:
        idx         = parts.index("browse")
        title_parts = parts[idx + 1:]
    else:
        title_parts = parts[-2:]
    return " ".join(title_parts).replace("-", " ")


def evaluate_and_rank(
    product_info: dict,
    candidate_urls: List[dict],
) -> Tuple[List[dict], float, float]:
    """
    Ranks candidate browse-page URLs by relevance to the product.

    Scoring is done via similarity.score_relevance() which uses:
      - OpenAI embeddings + cosine similarity when OPENAI_API_KEY is set
      - Jaccard keyword matching as a fallback (no API key required)

    Each candidate is scored on its real search-result title when one is
    supplied ("title" on the input dict), falling back to a title derived from
    the URL slug. The real title is the text a shopper actually sees, so it is
    a far better relevance signal than a slug.

    NOTE: This function always uses OpenAI for embeddings regardless of
    LLM_PROVIDER, because Claude does not provide an embeddings API.

    Returns:
        (ranked_pages, confidence_score, embedding_cost_usd)
        ranked_pages: list of
            {"url", "keyword", "position", "title", "relevance_score"}
            sorted by relevance_score descending. Every input candidate appears
            exactly once — ranking never drops candidates. "relevance_score" is
            None only when the scorer failed to return a score for that
            candidate; callers must treat None as "not scored" rather than
            substituting a numeric default.
        confidence_score: average similarity of the top-3 scored results (0–1)
        embedding_cost_usd: OpenAI embedding cost (0.0 when keyword fallback used)
    """
    title        = product_info.get("title", "")
    description  = product_info.get("description", "")
    product_text = f"{title} {description}".strip()

    if not candidate_urls:
        return [], 0.0, 0.0

    # Prefer the real search-result title captured at search time; fall back to
    # the URL slug when the caller did not supply one.
    candidate_data = []
    for item in candidate_urls:
        url            = item.get("url", "")
        supplied_title = (item.get("title") or "").strip()
        page_title     = supplied_title or _title_from_url(url)
        candidate_data.append({**item, "title": page_title})

    page_titles = [item["title"] for item in candidate_data]

    ranked_titles_with_scores, total_cost = score_relevance(product_text, page_titles)

    # Re-attach scores to candidates by INDEX, not by title string. Two distinct
    # URLs can derive the same title (trailing slash, differing scheme, or two
    # shelves that genuinely share a search-result title); matching on the title
    # alone pairs a score to whichever candidate happened to be found first and
    # silently drops any candidate the scorer did not echo back verbatim. A
    # dropped candidate keeps relevance_score=None forever, so the agent would
    # re-evaluate it every round until the round limit.
    pending_by_title: Dict[str, Deque[int]] = defaultdict(deque)
    for index, page_title in enumerate(page_titles):
        pending_by_title[page_title].append(index)

    score_by_index: Dict[int, float] = {}
    for ranked_title, score in ranked_titles_with_scores:
        queue = pending_by_title.get(ranked_title)
        if not queue:
            logger.warning(
                f"[EvaluationAgent] Scorer returned title with no matching "
                f"candidate: {ranked_title[:60]!r}"
            )
            continue
        score_by_index[queue.popleft()] = score

    unscored = len(candidate_data) - len(score_by_index)
    if unscored:
        logger.warning(
            f"[EvaluationAgent] {unscored} of {len(candidate_data)} candidates "
            f"received no score; they are returned unscored (relevance_score=None)"
        )

    # Sort by score descending, with unscored candidates last. Python's sort is
    # stable, so equal scores keep candidate input order.
    ranked_order = sorted(
        range(len(candidate_data)),
        key=lambda i: (
            score_by_index.get(i) is not None,
            score_by_index.get(i, 0.0),
        ),
        reverse=True,
    )

    top_urls: List[dict] = []
    scores:   List[float] = []
    for index in ranked_order:
        item  = candidate_data[index]
        score: Optional[float] = score_by_index.get(index)
        top_urls.append({
            "url":               item["url"],
            "keyword":           item.get("keyword", ""),
            "position":          item.get("position"),
            "title":             item["title"],
            RELEVANCE_SCORE_KEY: score,
        })
        if score is not None:
            scores.append(score)

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
