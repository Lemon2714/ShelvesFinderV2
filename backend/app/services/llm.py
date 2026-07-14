"""
llm.py — ShelvesFinder v2

Centralised LLM access layer supporting both OpenAI and Anthropic Claude.
All agents go through `call_llm()` — never call provider SDKs directly.

Provider is controlled by LLM_PROVIDER in .env:
  LLM_PROVIDER=openai   (default)
  LLM_PROVIDER=claude

NOTE: Embeddings always use OpenAI regardless of LLM_PROVIDER because
      Claude does not offer an embeddings API.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON response cleanup
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def clean_json_text(text: str) -> str:
    """
    Strip markdown code fences / surrounding prose from an LLM response so the
    result is parseable JSON.

    Some models (notably Claude) wrap JSON in ```json ... ``` fences even when
    explicitly told not to, which breaks json.loads(). We unwrap the fence and,
    as a fallback, extract the outermost {...} / [...] span.
    """
    if not text:
        return text
    s = text.strip()

    m = _JSON_FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()

    # Fallback: if there's still leading/trailing prose, isolate the JSON span.
    if s and s[0] not in "{[":
        starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
        if starts:
            start = min(starts)
            end = max(s.rfind("}"), s.rfind("]"))
            if end > start:
                s = s[start:end + 1]

    return s.strip()


# ---------------------------------------------------------------------------
# Lazy singleton clients
# ---------------------------------------------------------------------------

_openai_client = None
_openai_initialized = False

_anthropic_client = None
_anthropic_initialized = False


def get_openai_client():
    """
    Returns a configured OpenAI client, or None if unavailable.
    Used directly by evaluation_agent (embeddings) and as the OpenAI
    chat/tool path inside call_llm().
    """
    global _openai_client, _openai_initialized
    if _openai_initialized:
        return _openai_client
    _openai_initialized = True

    if not settings.openai_api_key:
        logger.warning("[LLM] OPENAI_API_KEY not set — OpenAI features unavailable.")
        return None
    try:
        from openai import OpenAI
        logger.info("[LLM] OpenAI client initialised.")
        _openai_client = OpenAI(api_key=settings.openai_api_key)
        return _openai_client
    except ImportError:
        logger.warning("[LLM] openai package not installed.")
        return None


def get_anthropic_client():
    """
    Returns a configured Anthropic client, or None if unavailable.
    Failure is NOT permanently cached — a bad init can be retried on next request.
    """
    global _anthropic_client, _anthropic_initialized
    if _anthropic_initialized and _anthropic_client is not None:
        return _anthropic_client

    if not settings.anthropic_api_key:
        logger.warning("[LLM] ANTHROPIC_API_KEY not set — Claude unavailable.")
        return None
    try:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        _anthropic_initialized = True
        logger.info(f"[LLM] Anthropic client initialised (model: {settings.claude_chat_model}).")
        return _anthropic_client
    except ImportError:
        logger.warning("[LLM] anthropic package not installed — run: pip install anthropic")
        _anthropic_initialized = True   # no point retrying a missing package
        return None
    except Exception as e:
        logger.error(f"[LLM] Anthropic client init failed: {e}", exc_info=True)
        # Do NOT set _anthropic_initialized so the next request retries
        _anthropic_client = None
        return None


# ---------------------------------------------------------------------------
# Provider resolution helpers
# ---------------------------------------------------------------------------

def _resolve_provider(provider: str | None) -> bool:
    """Return True if the effective provider is Claude."""
    if provider and provider.strip():
        return provider.strip().lower() == "claude"
    return settings.using_claude


def _resolve_default_model(provider: str | None, role: str = "chat") -> str:
    """Return the correct default model name for the effective provider."""
    use_claude = _resolve_provider(provider)
    if role == "orchestrator":
        return settings.claude_orchestrator_model if use_claude else settings.openai_orchestrator_model
    return settings.claude_chat_model if use_claude else settings.openai_chat_model


def get_active_client(provider: str | None = None):
    """
    Returns the client for the currently configured LLM provider.
    Used by agents to check whether any LLM is available.
    """
    if _resolve_provider(provider):
        return get_anthropic_client()
    return get_openai_client()


# ---------------------------------------------------------------------------
# Normalised response type
# ---------------------------------------------------------------------------

class LLMResponse:
    """
    Provider-agnostic response object returned by call_llm().

    Attributes:
        content      — raw text content (JSON string for json_mode calls)
        tool_name    — name of the tool selected (tool calls only, else None)
        tool_args    — parsed dict of tool arguments (tool calls only, else None)
        prompt_tokens, completion_tokens — for cost tracking
    """
    __slots__ = ("content", "tool_name", "tool_args", "prompt_tokens", "completion_tokens", "model")

    def __init__(
        self,
        content: str = "",
        tool_name: str | None = None,
        tool_args: dict | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str = "",
    ):
        self.content = content
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model

    def cost_usd(self) -> float:
        """Estimate cost in USD based on the model name stored on this response."""
        model = (self.model or "").lower()
        if "claude" in model:
            if "opus" in model:
                # claude-opus-4-5 / claude-3-opus: $15/1M in, $75/1M out
                return (self.prompt_tokens * 0.000015) + (self.completion_tokens * 0.000075)
            elif "sonnet" in model:
                # claude-sonnet-4-5 / claude-3-5-sonnet: $3/1M in, $15/1M out
                return (self.prompt_tokens * 0.000003) + (self.completion_tokens * 0.000015)
            else:
                # claude-haiku-4-5 / claude-3-haiku / claude-3-5-haiku: $0.80/1M in, $4/1M out
                return (self.prompt_tokens * 0.0000008) + (self.completion_tokens * 0.000004)
        else:
            # gpt-4o-mini: $0.15/1M in, $0.60/1M out
            return (self.prompt_tokens * 0.00000015) + (self.completion_tokens * 0.0000006)


# ---------------------------------------------------------------------------
# Main unified entry point
# ---------------------------------------------------------------------------

def call_llm(
    messages: list[dict],
    model: str | None = None,
    system: str | None = None,
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    json_mode: bool = False,
    provider: str | None = None,
) -> LLMResponse | None:
    """
    Send a chat/tool-call request to the active LLM provider.

    Args:
        messages    — list of {"role": "user"|"assistant", "content": "..."} dicts
                      (do NOT include a system message here; pass it via `system=`)
        model       — override the model name; defaults to settings.active_chat_model
        system      — system prompt string (handled natively by both providers)
        tools       — OpenAI-format tool schemas; automatically converted for Claude
        temperature — sampling temperature (0 = deterministic)
        json_mode   — when True, instructs the model to return only valid JSON
        provider    — per-request override: "openai" or "claude" (falls back to .env)

    Returns:
        LLMResponse  on success
        None         if no provider is available (callers fall back to rule-based logic)
    """
    resolved_model = model or _resolve_default_model(provider)
    if _resolve_provider(provider):
        return _call_claude(messages, resolved_model, system, tools, temperature, json_mode)
    return _call_openai(messages, resolved_model, system, tools, temperature, json_mode)


# ---------------------------------------------------------------------------
# OpenAI implementation
# ---------------------------------------------------------------------------

def _call_openai(
    messages: list[dict],
    model: str | None,
    system: str | None,
    tools: list[dict] | None,
    temperature: float,
    json_mode: bool,
) -> LLMResponse | None:
    client = get_openai_client()
    if not client:
        return None

    resolved_model = model or settings.openai_chat_model

    # OpenAI expects the system message inside the messages list
    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": full_messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "required"
    if json_mode and not tools:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        response = client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        usage = response.usage

        # Tool call response
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            return LLMResponse(
                content=msg.content or "",
                tool_name=tc.function.name,
                tool_args=json.loads(tc.function.arguments or "{}"),
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                model=resolved_model,
            )

        # Plain text / JSON response
        content = msg.content or ""
        if json_mode and not tools:
            content = clean_json_text(content)
        return LLMResponse(
            content=content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            model=resolved_model,
        )

    except Exception as e:
        logger.error(f"[LLM/OpenAI] call failed: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Claude implementation
# ---------------------------------------------------------------------------

def _openai_tools_to_claude(tools: list[dict]) -> list[dict]:
    """
    Convert OpenAI tool schemas to Anthropic format.

    OpenAI:   {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    Anthropic: {"name": ..., "description": ..., "input_schema": {...}}
    """
    claude_tools = []
    for t in tools:
        fn = t.get("function", t)          # handle both wrapped and bare dicts
        claude_tools.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return claude_tools


def _call_claude(
    messages: list[dict],
    model: str | None,
    system: str | None,
    tools: list[dict] | None,
    temperature: float,
    json_mode: bool,
) -> LLMResponse | None:
    client = get_anthropic_client()
    if not client:
        return None

    resolved_model = model or settings.claude_chat_model

    # Claude requires system to be a top-level param, not inside messages
    # If json_mode, append a JSON reminder to the system prompt
    system_parts = []
    if system:
        system_parts.append(system)
    if json_mode and not tools:
        system_parts.append(
            "You must respond with valid JSON only. "
            "Do not include any text, markdown, or code fences outside the JSON object."
        )
    resolved_system = "\n\n".join(system_parts) if system_parts else None

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "max_tokens": 1024,
        "temperature": temperature,
        "messages": messages,
    }
    if resolved_system:
        kwargs["system"] = resolved_system
    if tools:
        kwargs["tools"] = _openai_tools_to_claude(tools)
        kwargs["tool_choice"] = {"type": "any"}   # Claude equivalent of "required"

    try:
        response = client.messages.create(**kwargs)
        usage = response.usage

        used_model = kwargs.get("model", "")

        # Find tool_use block (tool call response)
        for block in response.content:
            if block.type == "tool_use":
                return LLMResponse(
                    content="",
                    tool_name=block.name,
                    tool_args=block.input,          # already a dict — no JSON parsing needed
                    prompt_tokens=usage.input_tokens if usage else 0,
                    completion_tokens=usage.output_tokens if usage else 0,
                    model=used_model,
                )

        # Plain text / JSON response — extract text block
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
                break

        if json_mode and not tools:
            text = clean_json_text(text)

        return LLMResponse(
            content=text,
            prompt_tokens=usage.input_tokens if usage else 0,
            completion_tokens=usage.output_tokens if usage else 0,
            model=used_model,
        )

    except Exception as e:
        logger.error(f"[LLM/Claude] call failed: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Provider info (for health checks / logging)
# ---------------------------------------------------------------------------

def get_provider_info() -> dict:
    """Returns current provider config — used by /v2/health endpoint."""
    return {
        "llm_provider": settings.llm_provider,
        "chat_model": settings.active_chat_model,
        "orchestrator_model": settings.active_orchestrator_model,
        "openai_available": bool(settings.openai_api_key),
        "claude_available": bool(settings.anthropic_api_key),
        "embeddings_provider": "openai",   # always
    }
