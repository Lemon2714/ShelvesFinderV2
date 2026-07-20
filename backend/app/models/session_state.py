"""
session_state.py — ShelvesFinder v2

In-memory state object shared across all ReAct loop iterations.
Each analysis request gets its own SessionState instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime
import uuid


def relevance_sort_key(score: Optional[float]) -> Tuple[int, float]:
    """
    Ascending sort key that orders by relevance, best first, unscored last.

    Shared by every place that ranks on relevance so the None handling has one
    definition. Unscored (None) sorts after every scored row *including* a
    scored 0.0 — a 0.0 is a measured irrelevance, whereas None means the
    pipeline never ranked that row at all, and a measured result outranks an
    absent one. Use directly as `key=`; no `reverse=True`.
    """
    if score is None:
        return (1, 0.0)
    return (0, -score)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

@dataclass
class ProductInfo:
    """Scraped product data from Walmart PDP."""
    title: str = ""
    brand: str = ""
    product_id: str = ""
    breadcrumb: str = ""
    description: str = ""
    features: List[str] = field(default_factory=list)
    image: str = ""
    price: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "brand": self.brand,
            "product_id": self.product_id,
            "breadcrumb": self.breadcrumb,
            "description": self.description,
            "features": self.features,
            "image": self.image,
            "price": self.price,
        }

    def to_prompt_str(self) -> str:
        features_str = "\n".join(f"  - {f}" for f in self.features[:5])
        return (
            f"Title: {self.title}\n"
            f"Brand: {self.brand}\n"
            f"Product ID: {self.product_id}\n"
            f"Breadcrumb: {self.breadcrumb}\n"
            f"Features:\n{features_str}"
        )


@dataclass
class BrowsePage:
    """A discovered Walmart browse/category URL."""
    url: str
    keyword: str = ""
    position: int = 0
    # None = not yet scored by the evaluation agent. A real 0.0 is a valid
    # measured score (the shelf is genuinely irrelevant) and must NOT be
    # confused with "unscored", or the page would be re-evaluated forever.
    relevance_score: Optional[float] = None
    checked: bool = False
    title: str = ""            # search-result title/breadcrumb metadata
    is_branded: bool = False   # inherently brand-specific shelf (classifier)
    keyword_type: str = "generic"  # "generic" | "branded" — origin keyword class


@dataclass
class ProductPlacement:
    """One product placement/impression on a checked shelf page."""
    placement_index: int
    visibility: bool
    discoverability: bool
    sponsored: bool
    organic: bool
    placement_rank: Optional[int] = None
    classification_source: str = "structured"

    @classmethod
    def from_dict(cls, value: dict) -> "ProductPlacement":
        return cls(
            placement_index=int(value.get("placement_index", 0) or 0),
            visibility=bool(value.get("visibility")),
            discoverability=bool(value.get("discoverability")),
            sponsored=bool(value.get("sponsored")),
            organic=bool(value.get("organic")),
            placement_rank=(
                int(value["placement_rank"])
                if value.get("placement_rank") is not None
                else None
            ),
            classification_source=str(
                value.get("classification_source", "structured")
            ),
        )

    def to_dict(self) -> dict:
        return {
            "placement_index": self.placement_index,
            "visibility": self.visibility,
            "discoverability": self.discoverability,
            "sponsored": self.sponsored,
            "organic": self.organic,
            "placement_rank": self.placement_rank,
            "classification_source": self.classification_source,
        }


@dataclass
class ShelfResult:
    """Result of checking whether a product appears on a specific shelf/page."""
    page_url: str
    product_found: bool
    page_number_found: int = 0       # 1 when found on the first page; otherwise 0
    confidence: float = 0.0
    checked_at_round: int = 0
    keyword: str = ""                # search keyword that discovered this page
    keyword_type: str = "generic"    # "generic" | "branded" — origin keyword class
    is_branded_shelf: bool = False   # inherently brand-specific base shelf
    position: int = 0                # rank position in search results
    brand_found: Optional[bool] = None  # True/False if brand carried on shelf; None if not evaluated
    sponsored: bool = False          # True when any placement on this page is sponsored
    organic: bool = False            # True when any placement on this page is organic
    visibility: bool = False         # product present on the base/general shelf ("Walmart Digital Shelf")
    discoverability: bool = False    # product present on the brand-filtered shelf ("Digital Shelf (Brand Filter)")
    placement_rank: Optional[int] = None  # first base-shelf Page 1 product-card rank
    placements: List[ProductPlacement] = field(default_factory=list)
    # Embedding relevance carried from the BrowsePage. None when the page was
    # checked before it was ever scored (see BrowsePage.relevance_score).
    relevance_score: Optional[float] = None
    discovery_index: int = 0         # order this row was admitted (found+missing)


@dataclass
class AgentAction:
    """One recorded tool call in the ReAct loop."""
    tool_name: str
    tool_input: dict
    reasoning: str
    round_number: int
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class ActionHistory:
    """Full log of all tool calls this session."""
    actions: List[AgentAction] = field(default_factory=list)

    def add(self, action: AgentAction) -> None:
        self.actions.append(action)

    def last_n(self, n: int) -> List[AgentAction]:
        return self.actions[-n:]

    def to_summary_str(self, last_n: int = 5) -> str:
        recent = self.last_n(last_n)
        if not recent:
            return "No actions taken yet."
        lines = []
        for a in recent:
            lines.append(
                f"  [Round {a.round_number}] {a.tool_name}: {a.reasoning[:120]}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Keyword expansion levels
# ---------------------------------------------------------------------------

KEYWORD_LEVELS = ["specific", "broader", "category", "department"]


# ---------------------------------------------------------------------------
# Recommended Category Page Results — allowed range for the number of final
# category-page rows returned under "Recommended Category Pages".
# ---------------------------------------------------------------------------

RECOMMENDED_RESULT_COUNT_MIN = 3
RECOMMENDED_RESULT_COUNT_DEFAULT = 5
RECOMMENDED_RESULT_COUNT_MAX = 10


# ---------------------------------------------------------------------------
# Main session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """
    Central state object for one v2 analysis run.
    Passed to every tool call; mutated after each observation.
    """

    # Identity
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    product_url: str = ""

    # Product data (populated after scrape)
    product: ProductInfo = field(default_factory=ProductInfo)

    # Keywords
    keywords_tried: List[str] = field(default_factory=list)
    keywords_pending: List[str] = field(default_factory=list)
    keyword_expansion_level: int = 0          # 0=specific, 1=broader, 2=category, 3=department
    # Classification of every generated keyword, kept separately so the
    # include_branded setting stays enforceable and result provenance is known.
    keywords_branded: List[str] = field(default_factory=list)
    keywords_unbranded: List[str] = field(default_factory=list)
    # Brand names harvested from search-result metadata during the session
    # (e.g. competitor brands revealed by facet URLs). Used by the shelf
    # classifier to reject competitor-branded shelves.
    known_brand_terms: set = field(default_factory=set)

    # Pages
    pages_discovered: List[BrowsePage] = field(default_factory=list)
    pages_checked: List[BrowsePage] = field(default_factory=list)

    # Results
    missing_pages: List[ShelfResult] = field(default_factory=list)
    found_pages: List[ShelfResult] = field(default_factory=list)

    # Agent loop control
    round_number: int = 0
    max_rounds: int = 5
    stop_reason: str = ""
    is_done: bool = False

    # Budget
    total_openai_cost: float = 0.0
    budget_limit: float = 0.50

    # Config (set from /v2/analyze/config)
    target_missing_count: int = 3

    # Number of final category-page rows to return under
    # "Recommended Category Pages" (min 3, max 10)
    recommended_result_count: int = RECOMMENDED_RESULT_COUNT_DEFAULT

    # Optional user-supplied context injected into agent prompts
    user_instructions: str = ""

    # "Include Branded Results" setting. When True the keyword agent also
    # generates and searches brand-name keywords (e.g. "Ensure protein shake")
    # AND inherently brand-specific category shelves (analyzed brand or
    # competitors) are allowed in the final results. When False only generic
    # keywords are searched and only generic base shelves are admitted — the
    # product-brand-filtered view of each generic shelf is still produced.
    include_branded: bool = False

    # Per-request LLM provider override ("openai" or "claude"); empty = use .env default
    llm_provider: str = ""

    # Full action trace
    history: ActionHistory = field(default_factory=ActionHistory)

    # Cache of sponsored/organic placement keyed by search keyword
    # (avoids re-fetching the same search results page across rounds)
    placement_cache: dict = field(default_factory=dict)

    # ---------------------------------------------------------------------------
    # Convenience helpers used by orchestrator
    # ---------------------------------------------------------------------------

    @property
    def unchecked_pages(self) -> List[BrowsePage]:
        return [p for p in self.pages_discovered if not p.checked]

    @property
    def unranked_pages(self) -> List[BrowsePage]:
        """
        Discovered pages the evaluation agent has not scored yet.

        Keyed on `is None`, not `== 0.0`: a genuine 0.0 means "scored, and
        irrelevant", and such a page must never re-enter the evaluate queue.
        """
        return [p for p in self.pages_discovered if p.relevance_score is None and not p.checked]

    @property
    def keywords_exhausted(self) -> bool:
        return (
            len(self.keywords_pending) == 0
            and self.keyword_expansion_level >= len(KEYWORD_LEVELS) - 1
        )

    def record_cost(self, cost: float) -> None:
        self.total_openai_cost += cost

    def keyword_type_for(self, keyword: str) -> str:
        """Classify a keyword by the lists it was generated into."""
        return "branded" if keyword in self.keywords_branded else "generic"

    def _reportable_results(self, results: List[ShelfResult]) -> List[ShelfResult]:
        """
        Server-side enforcement of the "Include Branded Results" contract.

        Inherently branded shelves are already rejected at discovery time and
        after the shelf check when include_branded is False; this is a
        defensive second gate so they can never leak into the final report,
        persistence, email, or SSE output.
        """
        if self.include_branded:
            return list(results)
        return [sr for sr in results if not sr.is_branded_shelf]

    # ------------------------------------------------------------------
    # Final-row selection for "Recommended Category Pages"
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_result_url(url: str) -> str:
        return (url or "").split("#")[0].rstrip("/").lower()

    def _unique_shelf_results(self) -> List[ShelfResult]:
        """
        All eligible rows (found + missing), URL-deduplicated, in discovery order.

        Branded shelves are filtered out first when include_branded is False,
        so they can neither consume a Recommended Category Pages slot nor
        count toward the completion target.
        """
        combined = sorted(
            self._reportable_results(self.found_pages + self.missing_pages),
            key=lambda sr: sr.discovery_index,
        )
        seen: set = set()
        unique: List[ShelfResult] = []
        for sr in combined:
            key = self._normalize_result_url(sr.page_url)
            if key in seen:
                continue
            seen.add(key)
            unique.append(sr)
        return unique

    @property
    def eligible_result_count(self) -> int:
        """Count of final category-page rows collected so far (uncapped)."""
        return len(self._unique_shelf_results())

    def selected_shelf_results(self) -> List[ShelfResult]:
        """
        The rows that make up the final report, capped at
        recommended_result_count. When more eligible rows exist than
        requested, the best rows win under a stable ordering:
        relevance score desc, search position asc, discovery order, URL.
        """
        ranked = sorted(
            self._unique_shelf_results(),
            key=lambda sr: (
                relevance_sort_key(sr.relevance_score),
                sr.position if sr.position and sr.position > 0 else float("inf"),
                sr.discovery_index,
                self._normalize_result_url(sr.page_url),
            ),
        )
        return ranked[: max(self.recommended_result_count, 0)]

    def to_summary_dict(self) -> dict:
        """Compact snapshot sent to LLM each iteration."""
        d = {
            "round": self.round_number,
            "max_rounds": self.max_rounds,
            "product_title": self.product.title,
            "product_id": self.product.product_id,
            "keywords_tried": self.keywords_tried,
            "keywords_pending": self.keywords_pending,
            "keyword_expansion_level": KEYWORD_LEVELS[
                min(self.keyword_expansion_level, len(KEYWORD_LEVELS) - 1)
            ],
            "pages_discovered": len(self.pages_discovered),
            "pages_unchecked": len(self.unchecked_pages),
            "pages_unranked": len(self.unranked_pages),
            "pages_ranked_unchecked": len(self.unchecked_pages) - len(self.unranked_pages),
            "missing_count": len(self.missing_pages),
            "found_count": len(self.found_pages),
            "target_missing": self.target_missing_count,
            "eligible_rows": self.eligible_result_count,
            "recommended_result_count": self.recommended_result_count,
            "total_cost_usd": round(self.total_openai_cost, 4),
            "budget_limit_usd": self.budget_limit,
            "recent_actions": self.history.to_summary_str(last_n=4),
        }
        if self.user_instructions:
            d["user_instructions"] = self.user_instructions
        if self.include_branded:
            d["include_branded"] = True
        return d

    def to_final_report(self) -> dict:
        """
        Full structured output once the loop is done.

        Only the selected rows (branded shelves already excluded per the
        "Include Branded Results" setting, then capped at
        recommended_result_count, in selection order) are reported;
        dashboards, persistence, copy, and email all derive from this same
        collection.
        """
        selected = self.selected_shelf_results()
        all_pages = []
        for sr in selected:
            all_pages.append({
                "url": sr.page_url,
                "found": sr.product_found,
                "brand_found": sr.brand_found,
                "sponsored": sr.sponsored,
                "organic": sr.organic,
                "visibility": sr.visibility,           # base shelf → Visibility Dashboard
                "discoverability": sr.discoverability,  # brand shelf → Discoverability Dashboard
                "placement_rank": sr.placement_rank,
                "placements": [p.to_dict() for p in sr.placements],
                "page_number": sr.page_number_found,
                "confidence": sr.confidence,
                "keyword": sr.keyword,
                "keyword_type": sr.keyword_type,
                "is_branded_shelf": sr.is_branded_shelf,
                "position": sr.position,
                # Embedding relevance this row was ranked on. null = never
                # scored. Surfaced so the ordering above is inspectable.
                "relevance_score": sr.relevance_score,
            })

        # Dashboard aggregates. The shared Discoverability Dashboard is driven by
        # Discoverability; visible/discoverable counts expose both signals.
        all_results = selected
        total_checked = len(all_results)
        visible_count = sum(1 for sr in all_results if sr.visibility)
        discoverable_count = sum(1 for sr in all_results if sr.discoverability)
        placements = [p for sr in all_results for p in sr.placements]
        organic_count = sum(1 for p in placements if p.organic)
        sponsored_count = sum(1 for p in placements if p.sponsored)

        return {
            "session_id": self.session_id,
            "product_title": self.product.title,
            "product_brand": self.product.brand,
            "product_id": self.product.product_id,
            "product_image": self.product.image,
            "product_price": self.product.price,
            "keywords_used": self.keywords_tried,
            "branded_keywords_used": [
                kw for kw in self.keywords_tried
                if self.keyword_type_for(kw) == "branded"
            ],
            "unbranded_keywords_used": [
                kw for kw in self.keywords_tried
                if self.keyword_type_for(kw) == "generic"
            ],
            "include_branded": self.include_branded,
            "rounds_completed": self.round_number,
            "stop_reason": self.stop_reason,
            "recommended_result_count_requested": self.recommended_result_count,
            "recommended_result_count_returned": len(all_pages),
            "shelf_results": all_pages,
            "shelf_stats": {
                "total": total_checked,
                "found": discoverable_count,           # Discoverability-driven
                "missing": total_checked - discoverable_count,
                "score": round(
                    discoverable_count / max(total_checked, 1) * 100, 1
                ),
                "visible": visible_count,
                "discoverable": discoverable_count,
                # Placement/impression-level metrics. Organic and sponsored are
                # intentionally independent, so both can be non-zero for one page.
                "placements": len(placements),
                "organic": organic_count,
                "sponsored": sponsored_count,
                # Explicit page-level compatibility metrics for older consumers.
                "organic_pages": sum(1 for sr in all_results if sr.organic),
                "sponsored_pages": sum(1 for sr in all_results if sr.sponsored),
                "details": {
                    sr.page_url: True for sr in selected if sr.product_found
                },
            },
            "openai_cost_usd": round(self.total_openai_cost, 6),
        }
