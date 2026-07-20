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
    SessionState, AgentAction, BrowsePage, ShelfResult, ProductPlacement,
    KEYWORD_LEVELS, relevance_sort_key,
)
from app.tools.tool_registry import registry, ToolResult
from app.services.llm import call_llm, get_active_client

logger = logging.getLogger(__name__)

# System prompt: tells the LLM its role and decision criteria
_SYSTEM_PROMPT = """You are the orchestrator for ShelvesFinder v2, an agentic AI system that finds
which Walmart browse/category pages a specific product should appear on.

Your goal: Assemble the requested number of final category-page rows
(recommended_result_count) — valid, checked browse pages for the report. Keep
searching, evaluating, and checking candidates until that many eligible rows
have been collected OR search options are exhausted. Invalid or non-existent
pages do not count toward the target.

Decision rules:
- If keywords_pending > 0: call 'search' with some or all pending keywords
- If unranked pages exist after a search: call 'evaluate' to rank them by relevance
- If ranked unchecked pages exist: call 'check_shelf' to verify product presence
- If keywords_pending = 0 AND pages are checked AND the row target is not met: call 'expand_keywords'
- If the row target is met OR all levels exhausted OR round/budget limit reached: call 'stop'

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
        f"  - Unranked (need 'evaluate'): {summary['pages_unranked']}\n"
        f"  - Ranked & unchecked (ready for 'check_shelf'): {summary['pages_ranked_unchecked']}\n\n"
        f"Results so far:\n"
        f"  - Eligible category-page rows ready: {summary['eligible_rows']} "
        f"(target: {summary['recommended_result_count']})\n"
        f"  - Missing pages found: {summary['missing_count']}\n"
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


def _returned_summary(final_report: dict) -> str:
    """Human-readable 'Returned X of Y requested category pages' line."""
    returned = final_report.get("recommended_result_count_returned", 0)
    requested = final_report.get("recommended_result_count_requested", 0)
    summary = f"Returned {returned} of {requested} requested category pages"
    if returned < requested:
        summary += " (shortfall — fewer valid pages were available)"
    return summary


def _is_noop_choice(state: SessionState, tool_name: str, tool_args: dict) -> bool:
    """
    True when the chosen tool cannot make progress in the current state.
    Used to break LLM decision loops (e.g. calling 'evaluate' again after
    every discovered page has already been ranked).
    """
    if tool_name == "evaluate":
        return not state.unranked_pages
    if tool_name == "check_shelf":
        return not state.unchecked_pages
    if tool_name == "search":
        return not tool_args.get("keywords")
    if tool_name == "expand_keywords":
        return state.keywords_exhausted
    return False


def _check_stopping_conditions(state: SessionState) -> tuple[bool, str]:
    """
    Returns (should_stop, reason).
    Called after each Observe phase.
    """
    # Primary completion target: the requested number of final category-page
    # rows (valid, deduplicated, checked). Raw discovered/unchecked pages and
    # the missing-page count alone never complete the run.
    if state.eligible_result_count >= state.recommended_result_count:
        return True, (
            f"recommended_result_count_reached: collected "
            f"{state.eligible_result_count} of {state.recommended_result_count} "
            f"requested category pages"
        )

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
    from app.tools.shelf_classifier import classify_shelf, harvest_known_brands

    keywords: list[str] = args.get("keywords", [])

    logger.info(f"[Orchestrator] SEARCH: {keywords}")

    try:
        raw_pages = await asyncio.to_thread(find_browse_pages, keywords)

        logger.info(f"[Search] Raw pages from search_agent ({len(raw_pages)}): {raw_pages[:2]}")

        # Mine competitor brand names revealed by this batch's metadata
        # (e.g. brand-facet URLs) before classifying any candidate, so a
        # competitor's unfaceted sibling shelf is also recognized.
        state.known_brand_terms.update(
            harvest_known_brands(rp for rp in raw_pages if isinstance(rp, dict))
        )

        new_pages: list[BrowsePage] = []
        rejected_branded = 0
        existing_urls = {p.url for p in state.pages_discovered}
        for rp in raw_pages:
            url = rp.get("url") if isinstance(rp, dict) else str(rp)
            if url and url not in existing_urls:
                # Use `or` fallback — rp.get(key, default) won't use default when key exists with None value
                kw = (rp.get("keyword") or (keywords[0] if keywords else "")) if isinstance(rp, dict) else ""
                pos = int(rp.get("position") or 0) if isinstance(rp, dict) else 0
                title = (rp.get("title") or "") if isinstance(rp, dict) else ""

                # Centralized branded-shelf gate: classify the discovered BASE
                # shelf (before the display-only brand-filtered URL is built).
                # A generic keyword can still surface a branded category page,
                # so the shelf itself is classified, not just the keyword.
                classification = classify_shelf(
                    url,
                    product_brand=state.product.brand,
                    title=title,
                    known_brands=state.known_brand_terms,
                )
                if classification.brand:
                    state.known_brand_terms.add(classification.brand)
                if classification.is_branded and not state.include_branded:
                    rejected_branded += 1
                    logger.info(
                        f"[Search] Rejected branded shelf ({classification.reason}, "
                        f"brand='{classification.brand}'): {url[:80]}"
                    )
                    existing_urls.add(url)
                    continue

                new_pages.append(BrowsePage(
                    url=url, keyword=kw, position=pos, title=title,
                    is_branded=classification.is_branded,
                    keyword_type=state.keyword_type_for(kw),
                ))
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
            data={
                "new_pages": len(new_pages),
                "total_discovered": len(state.pages_discovered),
                "rejected_branded": rejected_branded,
            },
            message=(
                f"Searched {len(keywords)} keywords, discovered {len(new_pages)} new pages"
                + (f", rejected {rejected_branded} branded shelves" if rejected_branded else "")
            ),
        )
    except Exception as e:
        logger.error(f"[Orchestrator] Search failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Search failed")


async def _call_evaluate(state: SessionState, args: dict) -> ToolResult:
    """Execute the evaluation (embedding ranking) tool."""
    from app.agents.evaluation_agent import evaluate_and_rank, RELEVANCE_SCORE_KEY

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
    # `title` is the real search-result title captured at search time. Passing
    # it lets the evaluator score the text a shopper actually sees instead of a
    # title reconstructed from the URL slug.
    raw_pages = [
        {"url": p.url, "keyword": p.keyword, "position": p.position, "title": p.title}
        for p in unranked
    ]

    try:
        ranked, confidence, cost = await asyncio.to_thread(
            evaluate_and_rank, product_dict, raw_pages
        )

        # The embedding calls were made whether or not the return shape is
        # usable, so record the cost before any early return.
        state.record_cost(cost)

        # Update relevance scores on BrowsePage objects. Read the documented
        # key with NO numeric default: substituting a placeholder here is what
        # silently pinned every page to 0.5 and collapsed the final ranking
        # onto raw search position.
        url_to_score: dict = {}
        unscored_count = 0
        for rp in ranked:
            if not isinstance(rp, dict):
                continue
            url = rp.get("url", "")
            if not url:
                continue
            score = rp.get(RELEVANCE_SCORE_KEY)
            if score is None:
                unscored_count += 1
                logger.warning(
                    f"[Orchestrator] evaluate_and_rank returned no "
                    f"'{RELEVANCE_SCORE_KEY}' for {url[:80]} — leaving the page "
                    f"unscored rather than inventing a score"
                )
                continue
            url_to_score[url] = score

        if ranked and not url_to_score:
            # Every row came back without a score: the evaluator is not
            # honouring its contract. Report the tool as failed instead of
            # leaving the pages unscored, which would make the agent re-pick
            # 'evaluate' every round until the round limit.
            logger.error(
                f"[Orchestrator] evaluate_and_rank returned {len(ranked)} rows, "
                f"none carrying '{RELEVANCE_SCORE_KEY}' — contract violation"
            )
            return ToolResult(
                success=False,
                error=f"evaluate_and_rank returned no '{RELEVANCE_SCORE_KEY}' values",
                message="Evaluation returned no usable relevance scores",
                cost_usd=cost,
            )

        for page in state.pages_discovered:
            if page.url in url_to_score:
                page.relevance_score = url_to_score[page.url]

        # Sort discovered pages by relevance, best first, unscored last.
        state.pages_discovered.sort(key=lambda p: relevance_sort_key(p.relevance_score))

        return ToolResult(
            success=True,
            data={
                "confidence": confidence,
                "ranked_count": len(ranked),
                "unscored_count": unscored_count,
            },
            message=(
                f"Ranked {len(ranked)} pages (confidence={confidence:.1%})"
                + (f", {unscored_count} unscored" if unscored_count else "")
            ),
            cost_usd=cost,
        )
    except Exception as e:
        logger.error(f"[Orchestrator] Evaluate failed: {e}", exc_info=True)
        return ToolResult(success=False, error=str(e), message="Evaluation failed")


async def _call_check_shelf(state: SessionState, args: dict) -> ToolResult:
    """Execute shelf visibility check on top unchecked pages."""
    from app.tools.shelf_checker import ShelfUnavailable, check_shelf_visibility

    # Default batch = rows still needed to reach the requested result count.
    # Hard cap of 10 matches RECOMMENDED_RESULT_COUNT_MAX so a user request
    # for up to 10 final rows is always satisfiable.
    remaining = max(1, state.recommended_result_count - state.eligible_result_count)
    default_batch = min(remaining, 10)
    try:
        requested = int(args.get("max_pages", default_batch))
    except (TypeError, ValueError):
        requested = default_batch
    max_pages = max(1, min(requested, 10))

    # Pick top-ranked unchecked pages — best relevance first, unscored last.
    candidates = sorted(
        state.unchecked_pages,
        key=lambda p: relevance_sort_key(p.relevance_score),
    )[:max_pages]

    if not candidates:
        return ToolResult(success=True, message="No unchecked pages available", data={})

    logger.info(f"[Orchestrator] CHECK_SHELF: {len(candidates)} pages")

    raw_pages = [{"url": p.url, "keyword": p.keyword, "position": p.position} for p in candidates]

    try:
        stats = await check_shelf_visibility(
            raw_pages, state.product.product_id, state.product.brand,
            known_brands=state.known_brand_terms,
        )

        details:          dict = stats.get("details", {})
        invalid_count:    int  = 0
        unavailable_count: int = 0
        rejected_branded: int  = 0
        found_count:      int  = 0
        missing_count:    int  = 0

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

            if isinstance(result, ShelfUnavailable):
                unavailable_count += 1
                logger.warning(
                    f"[ShelfCheck] Skipping unavailable page "
                    f"({result.reason}): {page.url[:80]}"
                )
                continue

            # Harvest every brand the fetched page's own facet metadata
            # revealed, so later candidates are rejected before being fetched.
            state.known_brand_terms.update(
                b for b in (result.get("page_brands") or []) if b
            )
            if result.get("shelf_brand"):
                state.known_brand_terms.add(result["shelf_brand"])

            # Post-fetch verification: the page's own __NEXT_DATA__ proved the
            # base shelf is inherently branded (catches brands the pre-fetch
            # lexical classifier couldn't know about).
            if result.get("branded_shelf"):
                page.is_branded = True
                if not state.include_branded:
                    rejected_branded += 1
                    logger.info(
                        f"[ShelfCheck] Rejected branded shelf post-fetch "
                        f"({result.get('branded_reason')}, "
                        f"brand='{result.get('shelf_brand')}'): {page.url[:80]}"
                    )
                    continue

            # Use the page-1 brand-shelf signal shared with the final dashboard.
            # Deeper shelf pages are never considered found.
            found = bool(result.get("discoverability"))
            brand_found = result.get("brand")
            page_found = 1 if found else 0
            visibility = bool(result.get("visibility"))
            discoverability = bool(result.get("discoverability"))
            placements = [
                ProductPlacement.from_dict(placement)
                for placement in result.get("placements", [])
            ]
            placement_rank = result.get("placement_rank")
            if placement_rank is None:
                ranked_placements = [
                    placement.placement_rank
                    for placement in placements
                    if placement.placement_rank is not None
                ]
                placement_rank = (
                    min(ranked_placements) if ranked_placements else None
                )
            organic = any(placement.organic for placement in placements)
            sponsored = any(placement.sponsored for placement in placements)
            logger.info(
                f"[ShelfCheck] keyword='{page.keyword}' pos={page.position} "
                f"brand={brand_found} found={found} page={page_found} "
                f"visibility={visibility} discoverability={discoverability} "
                f"sponsored={sponsored} organic={organic} url={page.url[:60]}"
            )
            sr = ShelfResult(
                page_url=page.url,
                product_found=found,
                keyword_type=page.keyword_type,
                is_branded_shelf=page.is_branded,
                brand_found=brand_found,
                page_number_found=page_found,
                sponsored=sponsored,
                organic=organic,
                visibility=visibility,
                discoverability=discoverability,
                placement_rank=placement_rank,
                placements=placements,
                confidence=1.0 if found else 0.0,
                checked_at_round=state.round_number,
                keyword=page.keyword,
                position=page.position,
                relevance_score=page.relevance_score,
                discovery_index=len(state.found_pages) + len(state.missing_pages),
            )
            if found:
                found_count += 1
                state.found_pages.append(sr)
            else:
                missing_count += 1
                state.missing_pages.append(sr)

        # With the freshly harvested brand names, already-discovered but
        # unchecked pages may now be recognizable as branded — drop them
        # before they cost a fetch.
        pruned_count = 0
        if not state.include_branded and state.known_brand_terms:
            from app.tools.shelf_classifier import classify_shelf

            for pending_page in list(state.unchecked_pages):
                classification = classify_shelf(
                    pending_page.url,
                    product_brand=state.product.brand,
                    title=pending_page.title,
                    known_brands=state.known_brand_terms,
                )
                if classification.is_branded:
                    state.pages_discovered.remove(pending_page)
                    pruned_count += 1
                    logger.info(
                        f"[ShelfCheck] Pruned unchecked branded shelf "
                        f"({classification.reason}, brand='{classification.brand}'): "
                        f"{pending_page.url[:80]}"
                    )

        return ToolResult(
            success=True,
            data={
                "checked": len(candidates),
                "found":   found_count,
                "missing": missing_count,
                "invalid": invalid_count,
                "unavailable": unavailable_count,
                "rejected_branded": rejected_branded,
                "pruned_unchecked": pruned_count,
            },
            message=(
                f"Checked {len(candidates)} pages: "
                f"{found_count} found, {missing_count} missing"
                + (f", {invalid_count} invalid (skipped)" if invalid_count else "")
                + (f", {unavailable_count} unavailable (skipped)" if unavailable_count else "")
                + (f", {rejected_branded} branded (rejected)" if rejected_branded else "")
                + (f", {pruned_count} pending branded (pruned)" if pruned_count else "")
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

            # No-op guard: if the chosen tool has no work to do in the current
            # state (e.g. 'evaluate' when every page is already ranked), the
            # round would be wasted and the LLM tends to repeat the same choice
            # forever. Override with the deterministic rule-based decision.
            if _is_noop_choice(state, tool_name, tool_args):
                fallback_name, fallback_args = _rule_based_decision(state)
                logger.warning(
                    f"[Orchestrator] '{tool_name}' has no work to do; "
                    f"overriding with rule-based choice '{fallback_name}'"
                )
                reasoning = (
                    f"[No-op override] '{tool_name}' had no work to do; "
                    f"switched to '{fallback_name}'"
                )
                tool_name, tool_args = fallback_name, fallback_args

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
                "rows_returned": final_report["recommended_result_count_returned"],
                "rows_requested": final_report["recommended_result_count_requested"],
                "message": (
                    f"Agent stopped: {state.stop_reason} · "
                    + _returned_summary(final_report)
                ),
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
            "rows_ready": state.eligible_result_count,
            "rows_target": state.recommended_result_count,
            "should_stop": should_stop,
            "message": (
                f"Goal check: {state.eligible_result_count}/{state.recommended_result_count} category pages ready"
                + (f" → STOPPING ({stop_reason})" if should_stop else " → continuing")
            ),
        }

        if should_stop:
            state.stop_reason = stop_reason
            state.is_done = True
            final_report = state.to_final_report()
            logger.info(f"[Orchestrator] FINAL shelf_results sample: {final_report.get('shelf_results', [])[:2]}")
            logger.info(f"[Orchestrator] {_returned_summary(final_report)} (stop_reason={stop_reason})")
            yield {
                "event": "complete",
                "round": state.round_number,
                "stop_reason": stop_reason,
                "rows_returned": final_report["recommended_result_count_returned"],
                "rows_requested": final_report["recommended_result_count_requested"],
                "message": f"Stopping: {stop_reason} · " + _returned_summary(final_report),
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
        remaining = max(1, state.recommended_result_count - state.eligible_result_count)
        return "check_shelf", {
            "max_pages": min(remaining, 10),
            "reasoning": "Ranked unchecked pages available",
        }

    if not state.keywords_exhausted:
        return "expand_keywords", {"reasoning": "Current keywords exhausted, expanding"}

    return "stop", {"reason": "All options exhausted (rule-based fallback)"}
