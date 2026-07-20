"""
tool_registry.py — ShelvesFinder v2

Maps LLM-callable tool names to Python functions and provides the
OpenAI function-calling schema the orchestrator sends to GPT.

Each tool receives a SessionState and keyword/page arguments, performs
its work, mutates state, and returns a ToolResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolResult — standardised return from every tool
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    success: bool
    data: Any = None
    message: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# OpenAI function-call schemas — what the LLM sees as available tools
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search Walmart and Google for browse/category pages using the provided keywords. "
                "Use this when you have pending keywords that have not been searched yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of search keywords (1-5 at a time)",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Why these keywords were chosen for this search round",
                    },
                },
                "required": ["keywords", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate",
            "description": (
                "Rank discovered browse pages by relevance to the product using embedding similarity. "
                "Use this when there are unranked pages in the discovered list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Why evaluation is needed now",
                    },
                },
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_shelf",
            "description": (
                "Check whether the product appears on specific browse/category pages by scraping them. "
                "Use this when there are ranked but unchecked pages. Checks up to 10 pages per call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_pages": {
                        "type": "integer",
                        "description": (
                            "Maximum number of pages to check in this call (1-10). "
                            "When omitted, defaults to the number of rows still needed "
                            "to reach the recommended result count."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Why shelf check is the right next action",
                    },
                },
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_keywords",
            "description": (
                "Generate a broader set of keywords when the current keyword level has been exhausted. "
                "Moves through 4 levels: specific → broader → category → department. "
                "Use this when all current keywords have been searched and there are still unchecked pages "
                "or the target has not been met."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reasoning": {
                        "type": "string",
                        "description": "Why keyword expansion is needed",
                    },
                },
                "required": ["reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop",
            "description": (
                "Stop the agent loop and produce the final report. "
                "Use when: (1) the requested number of category-page rows has been collected, "
                "(2) all keyword levels exhausted, "
                "(3) round limit reached, or "
                "(4) budget limit approached."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Human-readable explanation of why the agent is stopping",
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Registry: maps tool name → async executor function
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Holds references to the actual tool implementations.
    Populated at startup by workflow_v2.py after all imports resolve.
    """

    def __init__(self):
        self._registry: Dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._registry[name] = fn
        logger.debug(f"[ToolRegistry] Registered tool: {name}")

    def get(self, name: str) -> Optional[Callable]:
        return self._registry.get(name)

    def list_names(self) -> List[str]:
        return list(self._registry.keys())

    def schemas(self) -> List[dict]:
        """Return OpenAI tool schemas for registered tools only."""
        return [s for s in TOOL_SCHEMAS if s["function"]["name"] in self._registry]


# Singleton instance imported by orchestrator and workflow_v2
registry = ToolRegistry()
