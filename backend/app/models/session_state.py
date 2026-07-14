"""
session_state.py — ShelvesFinder v2

In-memory state object shared across all ReAct loop iterations.
Each analysis request gets its own SessionState instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import uuid


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
    relevance_score: float = 0.0
    checked: bool = False


@dataclass
class ShelfResult:
    """Result of checking whether a product appears on a specific shelf/page."""
    page_url: str
    product_found: bool
    page_number_found: int = 0       # which paginated page the product was on
    confidence: float = 0.0
    checked_at_round: int = 0
    keyword: str = ""                # search keyword that discovered this page
    position: int = 0                # rank position in search results
    brand_found: Optional[bool] = None  # True/False if brand carried on shelf; None if not evaluated
    sponsored: bool = False          # product appears as a sponsored listing in the keyword's search results
    organic: bool = False            # product appears as an organic listing in the keyword's search results


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

    # Optional user-supplied context injected into agent prompts
    user_instructions: str = ""

    # When True, keyword agent also generates brand-name keywords
    # (e.g. "Ensure protein shake") in addition to unbranded shelf terms
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
        return [p for p in self.pages_discovered if p.relevance_score == 0.0 and not p.checked]

    @property
    def keywords_exhausted(self) -> bool:
        return (
            len(self.keywords_pending) == 0
            and self.keyword_expansion_level >= len(KEYWORD_LEVELS) - 1
        )

    def record_cost(self, cost: float) -> None:
        self.total_openai_cost += cost

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
            "missing_count": len(self.missing_pages),
            "found_count": len(self.found_pages),
            "target_missing": self.target_missing_count,
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
        """Full structured output once the loop is done."""
        all_pages = []
        for sr in self.found_pages:
            all_pages.append({
                "url": sr.page_url,
                "found": True,
                "brand_found": sr.brand_found,
                "sponsored": sr.sponsored,
                "organic": sr.organic,
                "page_number": sr.page_number_found,
                "confidence": sr.confidence,
                "keyword": sr.keyword,
                "position": sr.position,
            })
        for sr in self.missing_pages:
            all_pages.append({
                "url": sr.page_url,
                "found": False,
                "brand_found": sr.brand_found,
                "sponsored": sr.sponsored,
                "organic": sr.organic,
                "page_number": 0,
                "confidence": sr.confidence,
                "keyword": sr.keyword,
                "position": sr.position,
            })

        return {
            "session_id": self.session_id,
            "product_title": self.product.title,
            "product_brand": self.product.brand,
            "product_id": self.product.product_id,
            "product_image": self.product.image,
            "product_price": self.product.price,
            "keywords_used": self.keywords_tried,
            "rounds_completed": self.round_number,
            "stop_reason": self.stop_reason,
            "shelf_results": all_pages,
            "shelf_stats": {
                "total": len(self.found_pages) + len(self.missing_pages),
                "found": len(self.found_pages),
                "missing": len(self.missing_pages),
                "score": round(
                    len(self.found_pages)
                    / max(len(self.found_pages) + len(self.missing_pages), 1)
                    * 100,
                    1,
                ),
                "details": {
                    sr.page_url: True for sr in self.found_pages
                },
            },
            "openai_cost_usd": round(self.total_openai_cost, 6),
        }
