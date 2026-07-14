"""
orchestrator.py — ShelvesFinder v2

Implements the ReAct (Reason → Act → Observe) agent loop.

Each iteration:
  1. REASON  — Build a state summary and ask GPT to pick the next tool
  2. ACT     — Execute the chosen tool via the ToolRegistry
  3. OBSERVE — Update SessionState with the result; yield SSE event
  4. CHECK   — Evaluate stopping conditions; stop or loop again
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Dict, Any

from app.config import settings
from app.models.session_state import (
    SessionState, AgentAction, BrowsePage, ShelfResult, KEYWORD_LEVELS
)
from app.tools.tool_registry import registry, ToolResult
from app.services.llm import call_llm, get_active_client

logger = logging.getLogger(__name__)

# System prompt: tells the LLM its role and decision criteria
_SYSTEM_PROMPT = """You are the orchestrator for ShelvesFinder v2, an agentic AI system that finds
which Walmart browse/category pages are MISSING a specific product.

Your goal: Find browse pages where the product does NOT appear. Stop when you have found
enough missing pages (target_missing) OR when search options are exhausted.

Decision rules:
- If keywords_pending > 0: call 'search' with some or all pending keywords
- If unranked pages exist after a search: call 'evaluate' to rank them by relevance
- If ranked unchecked pages exist: call 'check_shelf' to verify product presence
- If keywords_pending = 0 AND pages are checked AND target not met: call 'expand_keywords'
- If target met OR all levels exhausted OR round/budget limit reached: call 'stop'

Always provide clear reasoning for your choice. Be efficient — prefer checking pages
over searching more when you already have good candidates."""


def _build_state_message(state: SessionState) -> str:
    """Formats the current session state as a user message for the LLM."""
    summary = state.to_summary_dict()
    msg = (
        f"## Current Session State\n\n"
        f"Round: {summary['round']} / {summary['max_rounds']}\n"
        f"Product: {summary['product_title']} (ID: {summary['product_id']})\n\n"
        f"Keywords:\n"
        f"  - Tried: {summary['keywords_tried'] or 'none'}\n"
        f"  - Pending (not yet searched): {summary['keywords_pending'] or 'none'}\n"
        f"  - Expansion level: {summary['keyword_expansion_level']}\n\n"
        f"Pages:\n"
        f"  - Discovered: {summary['pages_discovered']}\n"
        f"  - Unchecked: {summary['pages_unchecked']}\n\n"
        f"Results so far:\n"
        f"  - Missing pages found: {summary['missing_count']} (target: {summary['target_missing']})\n"
        f"  - Found pages: {summary['found_count']}\n\n"
        f"Cost: ${summary['total_cost_usd']} / ${summary['budget_limit_usd']} budget\n\n"
        f"Recent actions:\n{summary['recent_actions']}\n"
    )
    if state.user_instructions:
        msg += (
            f"\n## User Context (use this to guide keyword and shelf decisions)\n"
            f"{state.user_instructions}\n"
        )
    msg += "\nWhat tool should be called next? Choose wisely."
    return msg


def _check_stopping_conditions(state: SessionState) -> tuple[bool, str]:
    """
    Returns (should_stop, reason).
    Called after each Observe phase.
    """
    if len(state.missing_pages) >= state.target_missing_count:
        return True, f"goal_achieved: found {len(state.missing_pages)} missing pages (target={state.target_missing_count})"

    if state.round_number >= state.max_rounds:
        return True, f"round_limit: reached max_rounds={state.max_rounds}"

    if state.keywords_exhausted and len(state.unchecked_pages) == 0:
        return True, "keywords_exhausted: all 4 expansion levels searched and all pages checked"

    if state.total_openai_cost >= state.budget_limit:
        return True, f"budget_limit: cost ${state.total_openai_cost:.4f} reached limit ${state.budget_limit} ({settings.llm_provider})"

    return False, ""


async def _call_search(state: SessionState, args: dict) -> ToolResult:
    """Execute the search tool."""
    from app.agents.search_agent import find_browse_pages

    keywords: list[str] = args.get("keywords", [])
    if not keywords and state.keywords_pending:
        keywords = state.keywords_pending[:5]

    logger.info(f"[Orchestrator] SEARCH: {keywords}")

    try:
        raw_pages = await asyncio.to_thread(find_browse_pages, keywords)

        logger.info(f"[Search] Raw pages from search_agent ({len(raw_pages)}): {raw_pages[:2]}")

        new_pages: list[BrowsePage] = []
        existing_urls = {p.url for p in state.pages_discovered}
        for rp in raw_pages:
            url = rp.get("url") if isinstance(rp, dict) else str(rp)
            if url and url not in existing_urls:
                # Use `or` fallback — rp.get(key, default) won't use default when key exists with None value
                kw = (rp.get("keyword") or (keywords[0] if keywords else "")) if isinstance(rp, dict) else ""
                pos = int(rp.get("position") or 0) if isinstance(rp, dict) else 0
                new_pages.append(BrowsePage(url=url, keyword=kw, position=pos))
                logger.info(f"[Search] BrowsePage created: keyword='{kw}' position={pos} url={url[:60]}")
                existing_urls.add(url)

        # Move searched keywords from pending → tried
        for kw in keywords:
            if kw in state.keywords_pending:
                state.keywords_pending.remove(kw)
            if kw not in state.keywords_tried:
                state.keywords_tried.append(kw)

        state.pages_discovered.extend(new_pages)

        return ToolResult(
            success=True,
            data={"new_pages": len(new_pages), "total_discovered": len(state.pages_discovered)},
            message=f"Searched {len(keywords)} keywords, discovered {len(new_pages)} new pages",
        )
    except Exception as e:
        logger.error(f"[Orchestrator] Search failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Search failed")


async def _call_evaluate(state: SessionState, args: dict) -> ToolResult:
    """Execute the evaluation (embedding ranking) tool."""
    from app.agents.evaluation_agent import evaluate_and_rank

    unranked = state.unranked_pages
    if not unranked:
        return ToolResult(success=True, message="No unranked pages to evaluate", data={})

    logger.info(f"[Orchestrator] EVALUATE: {len(unranked)} pages")

    product_dict = {
        "title": state.product.title,
        "brand": state.product.brand,
        "description": state.product.description,
        "features": state.product.features,
    }
    raw_pages = [{"url": p.url, "keyword": p.keyword, "position": p.position} for p in unranked]

    try:
        ranked, confidence, cost = await asyncio.to_thread(
            evaluate_and_rank, product_dict, raw_pages
        )

        # Update relevance scores on BrowsePage objects
        url_to_score: dict = {}
        for rp in ranked:
            if isinstance(rp, dict):
                url_to_score[rp.get("url", "")] = rp.get("relevance_score", rp.get("score", 0.5))

        for page in state.pages_discovered:
            if page.url in url_to_score:
                page.relevance_score = url_to_score[page.url]

        # Sort discovered pages by relevance descending
        state.pages_discovered.sort(key=lambda p: p.relevance_score, reverse=True)

        state.record_cost(cost)
        return ToolResult(
            success=True,
            data={"confidence": confidence, "ranked_count": len(ranked)},
            message=f"Ranked {len(ranked)} pages (confidence={confidence:.1%})",
            cost_usd=cost,
        )
    except Exception as e:
        logger.error(f"[Orchestrator] Evaluate failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Evaluation failed")


async def _call_check_shelf(state: SessionState, args: dict) -> ToolResult:
    """Execute shelf visibility check on top unchecked pages."""
    from app.tools.shelf_checker import check_shelf_visibility

    max_pages: int = min(args.get("max_pages", 5), 5)
    # Pick top-ranked unchecked pages
    candidates = sorted(
        [p for p in state.pages_discovered if not p.checked],
        key=lambda p: p.relevance_score,
        reverse=True,
    )[:max_pages]

    if not candidates:
        return ToolResult(success=True, message="No unchecked pages available", data={})

    logger.info(f"[Orchestrator] CHECK_SHELF: {len(candidates)} pages")

    raw_pages = [{"url": p.url, "keyword": p.keyword, "position": p.position} for p in candidates]

    try:
        stats = await check_shelf_visibility(
            raw_pages, state.product.product_id, state.product.brand
        )

        details:       dict = stats.get("details", {})
        invalid_count: int  = 0

        logger.info(f"[ShelfCheck] details keys: {list(details.keys())[:3]}")
        for page in candidates:
            page.checked = True
            state.pages_checked.append(page)

            result = details.get(page.url)   # {"brand":..,"product":..} | None

            if result is None:
                # Page doesn't exist — exclude from found/missing entirely
                invalid_count += 1
                logger.warning(
                    f"[ShelfCheck] Skipping invalid page (not found on Walmart): "
                    f"{page.url[:80]}"
                )
                continue

            found = bool(result.get("product"))
            brand_found = result.get("brand")
            page_found = result.get("page", 0) or 0
            visibility = bool(result.get("visibility"))
            discoverability = bool(result.get("discoverability"))
            organic = visibility and discoverability
            sponsored = visibility and not discoverability
            logger.info(
                f"[ShelfCheck] keyword='{page.keyword}' pos={page.position} "
                f"brand={brand_found} found={found} page={page_found} "
                f"visibility={visibility} discoverability={discoverability} "
                f"sponsored={sponsored} organic={organic} url={page.url[:60]}"
            )
            sr = ShelfResult(
                page_url=page.url,
                product_found=found,
                brand_found=brand_found,
                page_number_found=page_found,
                sponsored=sponsored,
                organic=organic,
                visibility=visibility,
                discoverability=discoverability,
                confidence=1.0 if found else 0.0,
                checked_at_round=state.round_number,
                keyword=page.keyword,
                position=page.position,
            )
            if found:
                state.found_pages.append(sr)
            else:
                state.missing_pages.append(sr)

        return ToolResult(
            success=True,
            data={
                "checked": len(candidates),
                "found":   stats.get("found", 0),
                "missing": stats.get("missing", 0),
                "invalid": invalid_count,
            },
            message=(
                f"Checked {len(candidates)} pages: "
                f"{stats.get('found', 0)} found, {stats.get('missing', 0)} missing"
                + (f", {invalid_count} invalid (skipped)" if invalid_count else "")
            ),
        )
    except Exception as e:
        logger.error(f"[Orchestrator] ShelfCheck failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Shelf check failed")


async def _call_expand_keywords(state: SessionState, args: dict) -> ToolResult:
    """Expand keywords to the next level."""
    from app.agents.keyword_expander import expand_keywords

    try:
        new_kws, cost = await asyncio.to_thread(expand_keywords, state, state.llm_provider or None)
        return ToolResult(
            success=True,
            data={"new_keywords": new_kws, "level": state.keyword_expansion_level},
            message=(
                f"Expanded to level {state.keyword_expansion_level} "
                f"({KEYWORD_LEVELS[min(state.keyword_expansion_level, len(KEYWORD_LEVELS)-1)]}): "
                f"{len(new_kws)} new keywords"
            ),
            cost_usd=cost,
        )
    except Exception as e:
        logger.error(f"[Orchestrator] ExpandKeywords failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Keyword expansion failed")


async def _call_stop(state: SessionState, args: dict) -> ToolResult:
    reason: str = args.get("reason", "Agent decided to stop")
    state.stop_reason = reason
    state.is_done = True
    return ToolResult(success=True, message=reason, data={"stop_reason": reason})


# Map tool names to async executor functions
_TOOL_EXECUTORS: Dict[str, Any] = {
    "search": _call_search,
    "evaluate": _call_evaluate,
    "check_shelf": _call_check_shelf,
    "expand_keywords": _call_expand_keywords,
    "stop": _call_stop,
}


# ---------------------------------------------------------------------------
# Main ReAct loop — yields SSE events
# ---------------------------------------------------------------------------

async def run_react_loop(state: SessionState) -> AsyncGenerator[dict, None]:
    """
    Core ReAct agent loop. Yields SSE-ready event dicts.

    Caller (workflow_v2.py) is responsible for:
      - Initial scrape and keyword generation (pre-loop)
      - Streaming events to the frontend
      - Final report assembly
    """
    while not state.is_done:
        state.round_number += 1
        logger.info(f"\n{'='*60}\n[Orchestrator] === ROUND {state.round_number} ===\n{'='*60}")

        # ---------------------------------------------------------------
        # 1. REASON — ask LLM to pick next tool
        # ---------------------------------------------------------------
        state_msg = _build_state_message(state)

        yield {
            "event": "agent_reasoning",
            "round": state.round_number,
            "state_summary": state.to_summary_dict(),
            "message": f"Round {state.round_number}: Agent is reasoning about next action...",
        }

        # Build system prompt — append user instructions when present
        system_prompt = _SYSTEM_PROMPT
        if state.user_instructions:
            system_prompt = (
                _SYSTEM_PROMPT
                + f"\n\nUser-provided context for this analysis:\n{state.user_instructions}"
            )

        from app.tools.tool_registry import TOOL_SCHEMAS
        from app.services.llm import _resolve_default_model
        provider = state.llm_provider or None
        orch_model = _resolve_default_model(provider, role="orchestrator")
        llm_result = await asyncio.to_thread(
            call_llm,
            messages=[{"role": "user", "content": state_msg}],
            model=orch_model,
            system=system_prompt,
            tools=TOOL_SCHEMAS,
            temperature=0.1,
            provider=provider,
        )

        if llm_result is None or llm_result.tool_name is None:
            # No LLM available or call failed — deterministic fallback
            reason = "unavailable" if llm_result is None else "no tool call returned"
            logger.warning(f"[Orchestrator] LLM {reason}; using rule-based fallback")
            tool_name, tool_args = _rule_based_decision(state)
            reasoning = f"[Rule-based fallback] {tool_name}"
        else:
            tool_name = llm_result.tool_name
            tool_args  = llm_result.tool_args or {}
            reasoning  = tool_args.get("reasoning", tool_args.get("reason", ""))
            llm_cost   = llm_result.cost_usd()
            state.record_cost(llm_cost)
            effective_provider = state.llm_provider or settings.llm_provider
            logger.info(
                f"[Orchestrator] {effective_provider.upper()} chose: {tool_name} | "
                f"reason: {reasoning[:100]} | cost=${llm_cost:.6f}"
            )

        logger.info(f"[Orchestrator] tool={tool_name} | reasoning={reasoning[:80]}")

        yield {
            "event": "tool_selected",
            "round": state.round_number,
            "tool": tool_name,
            "reasoning": reasoning,
            "message": f"Tool selected: {tool_name}",
        }

        # Record action
        state.history.add(AgentAction(
            tool_name=tool_name,
            tool_input=tool_args,
            reasoning=reasoning,
            round_number=state.round_number,
        ))

        # ---------------------------------------------------------------
        # 2. ACT — execute chosen tool
        # ---------------------------------------------------------------
        executor = _TOOL_EXECUTORS.get(tool_name)
        if not executor:
            logger.error(f"[Orchestrator] Unknown tool: {tool_name}")
            result = ToolResult(success=False, error=f"Unknown tool: {tool_name}")
        else:
            result = await executor(state, tool_args)

        logger.info(f"[Orchestrator] Tool result: {result.message}")

        yield {
            "event": "tool_result",
            "round": state.round_number,
            "tool": tool_name,
            "success": result.success,
            "message": result.message,
            "data": result.data,
        }

        # ---------------------------------------------------------------
        # 3. OBSERVE — if stop was called, we're done
        # ---------------------------------------------------------------
        if tool_name == "stop" or state.is_done:
            final_report = state.to_final_report()
            logger.info(f"[Orchestrator] FINAL shelf_results sample: {final_report.get('shelf_results', [])[:2]}")
            yield {
                "event": "complete",
                "round": state.round_number,
                "stop_reason": state.stop_reason,
                "message": f"Agent stopped: {state.stop_reason}",
                "data": final_report,
            }
            return

        # ---------------------------------------------------------------
        # 4. CHECK stopping conditions after each non-stop action
        # ---------------------------------------------------------------
        should_stop, stop_reason = _check_stopping_conditions(state)

        yield {
            "event": "goal_check",
            "round": state.round_number,
            "missing_found": len(state.missing_pages),
            "target": state.target_missing_count,
            "should_stop": should_stop,
            "message": (
                f"Goal check: {len(state.missing_pages)}/{state.target_missing_count} missing pages found"
                + (f" → STOPPING ({stop_reason})" if should_stop else " → continuing")
            ),
        }

        if should_stop:
            state.stop_reason = stop_reason
            state.is_done = True
            final_report = state.to_final_report()
            logger.info(f"[Orchestrator] FINAL shelf_results sample: {final_report.get('shelf_results', [])[:2]}")
            yield {
                "event": "complete",
                "round": state.round_number,
                "stop_reason": stop_reason,
                "message": f"Stopping: {stop_reason}",
                "data": final_report,
            }
            return

        # Small yield to let async event loop breathe between rounds
        await asyncio.sleep(0.1)

    # Should not reach here, but guard
    yield {
        "event": "complete",
        "round": state.round_number,
        "stop_reason": state.stop_reason or "loop_ended",
        "message": "Agent loop completed",
        "data": state.to_final_report(),
    }


# ---------------------------------------------------------------------------
# Rule-based fallback (used when OpenAI is unavailable)
# ---------------------------------------------------------------------------

def _rule_based_decision(state: SessionState) -> tuple[str, dict]:
    """Simple deterministic fallback that mirrors the ReAct decision rules."""
    if state.keywords_pending:
        kws = state.keywords_pending[:5]
        return "search", {"keywords": kws, "reasoning": "Pending keywords available"}

    if state.unranked_pages:
        return "evaluate", {"reasoning": "Unranked pages need scoring"}

    if state.unchecked_pages:
        return "check_shelf", {"max_pages": 5, "reasoning": "Ranked unchecked pages available"}

    if not state.keywords_exhausted:
        return "expand_keywords", {"reasoning": "Current keywords exhausted, expanding"}

    return "stop", {"reason": "All options exhausted (rule-based fallback)"}
