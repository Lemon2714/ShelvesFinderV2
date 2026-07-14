"""
workflow_v2.py — ShelvesFinder v2

Entry point for the agentic analysis loop.
Called by the /v2/analyze/stream FastAPI endpoint.

Responsibilities:
  1. Build a fresh SessionState for each request
  2. Run the initial scrape + keyword generation (pre-loop setup)
  3. Hand off to orchestrator.run_react_loop()
  4. Yield SSE events back to the frontend throughout
  5. Persist results on completion
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from app.models.session_state import SessionState, ProductInfo
from app.agents.orchestrator import run_react_loop
from app.agents.keyword_expander import get_initial_keywords

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_v2_workflow(
    url: str,
    max_rounds: int = 5,
    target_missing_count: int = 3,
    budget_limit: float = 0.50,
    user_instructions: str = "",
    include_branded: bool = False,
    llm_provider: str = "",
) -> AsyncGenerator[dict, None]:
    """
    Full agentic analysis workflow for a Walmart product URL.

    Yields SSE-ready event dicts throughout the run:

      setup_*          — scraping and initial keyword generation
      agent_reasoning  — LLM choosing next tool
      tool_selected    — which tool was picked and why
      tool_result      — what the tool returned
      goal_check       — stopping condition evaluation
      complete         — final report with all shelf results
      error            — if a fatal error occurs

    Usage in main.py:
        async for event in run_v2_workflow(url):
            yield json.dumps(event)
    """

    # ------------------------------------------------------------------
    # 0. Create session state
    # ------------------------------------------------------------------
    state = SessionState(
        product_url=url,
        max_rounds=max_rounds,
        target_missing_count=target_missing_count,
        budget_limit=budget_limit,
        user_instructions=user_instructions.strip() if user_instructions else "",
        include_branded=include_branded,
        llm_provider=llm_provider.strip() if llm_provider else "",
    )
    logger.info(f"[WorkflowV2] Session {state.session_id} started for {url}")

    yield {
        "event": "setup_start",
        "session_id": state.session_id,
        "message": "Starting v2 agentic analysis...",
    }

    # ------------------------------------------------------------------
    # 1. Scrape product data
    # ------------------------------------------------------------------
    yield {"event": "setup_scraping", "message": "Scraping product data from Walmart..."}

    try:
        from app.tools.scraper import fetch_product_content

        product_raw = await asyncio.to_thread(fetch_product_content, url)

        # Apply fallback if scraping was blocked
        if not product_raw.get("title") or "robot or human" in product_raw.get("title", "").lower():
            yield {"event": "setup_scraping", "status": "warning",
                   "message": "Anti-bot detected. Falling back to URL slug parsing."}
            parts = url.rstrip("/").split("/")
            slug = parts[-2] if len(parts) >= 2 and parts[-2] != "ip" else parts[-1]
            product_raw["title"] = slug.replace("-", " ").title()
            product_raw["description"] = ""
            product_raw["features"] = []
            product_raw["id"] = product_raw.get("id") or parts[-1].split("?")[0]
            product_raw["brand"] = product_raw.get("brand") or ""

        state.product = ProductInfo(
            title=product_raw.get("title", ""),
            brand=product_raw.get("brand", ""),
            product_id=product_raw.get("id", ""),
            description=product_raw.get("description", ""),
            features=product_raw.get("features", []),
            image=product_raw.get("image", ""),
            price=product_raw.get("price", ""),
        )

        logger.info(
            f"[WorkflowV2] Scraped: '{state.product.title}' "
            f"(brand={state.product.brand}, id={state.product.product_id})"
        )
        yield {
            "event": "setup_scraping",
            "status": "complete",
            "message": f"Product found: '{state.product.title}'",
            "data": {
                "title": state.product.title,
                "brand": state.product.brand,
                "product_id": state.product.product_id,
            },
        }

    except Exception as e:
        logger.error(f"[WorkflowV2] Scraping failed: {e}", exc_info=True)
        yield {"event": "error", "message": f"Scraping failed: {e}"}
        return

    # ------------------------------------------------------------------
    # 2. Generate initial keywords (level 0 — specific)
    # ------------------------------------------------------------------
    yield {"event": "setup_keywords", "message": "Generating initial search keywords (level 0: specific)..."}

    try:
        initial_kws, kw_cost = await asyncio.to_thread(get_initial_keywords, state, state.llm_provider or None)
        logger.info(f"[WorkflowV2] Initial keywords: {initial_kws}")
        yield {
            "event": "setup_keywords",
            "status": "complete",
            "message": f"Generated {len(initial_kws)} initial keywords",
            "data": {"keywords": initial_kws, "cost_usd": kw_cost},
        }
    except Exception as e:
        logger.error(f"[WorkflowV2] Keyword generation failed: {e}", exc_info=True)
        # Non-fatal: use title words as fallback
        fallback = [state.product.title] if state.product.title else ["product"]
        state.keywords_pending = fallback
        yield {
            "event": "setup_keywords",
            "status": "warning",
            "message": f"Keyword generation failed; using fallback: {fallback}",
        }

    # ------------------------------------------------------------------
    # 3. Run the ReAct agent loop
    # ------------------------------------------------------------------
    loop_start_config = {
        "max_rounds": state.max_rounds,
        "target_missing_count": state.target_missing_count,
        "budget_limit_usd": state.budget_limit,
        "include_branded": state.include_branded,
    }
    if state.user_instructions:
        loop_start_config["user_instructions"] = state.user_instructions
        logger.info(f"[WorkflowV2] User instructions active: {state.user_instructions[:100]}")
    if state.include_branded:
        logger.info("[WorkflowV2] Branded keywords enabled")

    yield {
        "event": "loop_start",
        "session_id": state.session_id,
        "config": loop_start_config,
        "message": "Entering ReAct agent loop...",
    }

    try:
        async for event in run_react_loop(state):
            yield event
            # If the loop emitted 'complete', we also want to persist
            if event.get("event") == "complete":
                await _persist_results(state)

    except Exception as e:
        logger.error(f"[WorkflowV2] React loop error: {e}", exc_info=True)
        yield {
            "event": "error",
            "message": f"Agent loop error: {e}",
            "data": state.to_final_report(),
        }


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------

async def _persist_results(state: SessionState) -> None:
    """Save results to CSV and/or Google Sheets (mirrors v1 persistence)."""
    try:
        from app.config import settings
        from app.services.persistence import (
            save_result_to_csv, append_result_to_sheet, upload_csv_to_drive
        )

        report = state.to_final_report()
        browse_pages_urls = [sr["url"] for sr in report["shelf_results"]]

        if settings.use_google_sheets:
            await asyncio.to_thread(
                append_result_to_sheet,
                url=state.product_url,
                title=state.product.title,
                brand=state.product.brand,
                product_id=state.product.product_id,
                keywords=state.keywords_tried,
                branded_keywords=[],
                unbranded_keywords=state.keywords_tried,
                browse_pages=browse_pages_urls,
                openai_cost=state.total_openai_cost,
            )
            logger.info(f"[WorkflowV2] Results saved to Google Sheets")
        else:
            await asyncio.to_thread(
                save_result_to_csv,
                url=state.product_url,
                title=state.product.title,
                brand=state.product.brand,
                product_id=state.product.product_id,
                keywords=state.keywords_tried,
                branded_keywords=[],
                unbranded_keywords=state.keywords_tried,
                browse_pages=browse_pages_urls,
                openai_cost=state.total_openai_cost,
            )
            await asyncio.to_thread(upload_csv_to_drive)
            logger.info(f"[WorkflowV2] Results saved to CSV")

    except Exception as e:
        logger.error(f"[WorkflowV2] Persistence failed (non-fatal): {e}")
