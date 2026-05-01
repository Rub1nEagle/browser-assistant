from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .base import LLMClient
from .types import (
    Block,
    Message,
    StepResponse,
    TextBlock,
    Tool,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

# USD per million tokens. First-prefix-wins, longer prefixes earlier.
# Numbers are list prices on the OpenAI API as of the knowledge cutoff;
# alternative endpoints (OpenRouter, DeepSeek, Ollama, etc.) are charged
# upstream — we leave their pricing at 0.
_PRICING: list[tuple[str, dict[str, float]]] = [
    ("gpt-4o-mini",       {"in": 0.15, "out": 0.60, "cache_read": 0.075}),
    ("gpt-4o",            {"in": 2.50, "out": 10.00, "cache_read": 1.25}),
    ("gpt-4.1-mini",      {"in": 0.40, "out": 1.60, "cache_read": 0.10}),
    ("gpt-4.1",           {"in": 2.00, "out": 8.00, "cache_read": 0.50}),
    ("o4-mini",           {"in": 1.10, "out": 4.40, "cache_read": 0.275}),
    ("o3-mini",           {"in": 1.10, "out": 4.40, "cache_read": 0.55}),
    ("deepseek-reasoner", {"in": 0.55, "out": 2.19, "cache_read": 0.14}),
    ("deepseek-chat",     {"in": 0.27, "out": 1.10, "cache_read": 0.07}),
]


def _messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate our Block-based history to OpenAI's flat message format.

    Differences worth keeping in mind:
    - Anthropic puts tool_result blocks inside a user message; OpenAI
      uses a separate `role: "tool"` message per result.
    - OpenAI assistant messages carry tool_calls as a sibling field of
      `content`, not as inline blocks.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            tool_results = [b for b in m.blocks if isinstance(b, ToolResultBlock)]
            text_blocks = [b for b in m.blocks if isinstance(b, TextBlock)]
            for tr in tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": tr.content,
                })
            if text_blocks or not tool_results:
                text = "\n".join(b.text for b in text_blocks)
                out.append({"role": "user", "content": text})
        elif m.role == "assistant":
            text_blocks = [b for b in m.blocks if isinstance(b, TextBlock)]
            tool_uses = [b for b in m.blocks if isinstance(b, ToolUseBlock)]
            msg: dict[str, Any] = {"role": "assistant"}
            text_content = "\n".join(b.text for b in text_blocks)
            # OpenAI accepts null content when tool_calls is present.
            msg["content"] = text_content if text_content else None
            if tool_uses:
                msg["tool_calls"] = [
                    {
                        "id": b.id,
                        "type": "function",
                        "function": {
                            "name": b.name,
                            "arguments": json.dumps(b.input, ensure_ascii=False),
                        },
                    }
                    for b in tool_uses
                ]
            out.append(msg)
    return out


def _decode_message(message: Any) -> list[Block]:
    blocks: list[Block] = []
    text = getattr(message, "content", None)
    if text:
        blocks.append(TextBlock(text=text))
    tool_calls = getattr(message, "tool_calls", None) or []
    for tc in tool_calls:
        raw = getattr(tc.function, "arguments", "") or ""
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # Some backends occasionally return malformed JSON; surface it
            # so the agent can recover rather than crashing the loop.
            args = {"_unparsed_arguments": raw}
        blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
    return blocks


class OpenAIClient(LLMClient):
    """Adapter for OpenAI Chat Completions and any OpenAI-compatible endpoint
    (OpenRouter, DeepSeek, Together, local Ollama, …) — pass `base_url`."""

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def step(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[Tool],
        max_tokens: int = 4096,
    ) -> StepResponse:
        oai_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        oai_messages.extend(_messages_to_openai(messages))

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        blocks = _decode_message(choice.message)

        # OpenAI auto-caches prompts ≥1024 tokens with shared prefix.
        # Cached portion is reported under prompt_tokens_details.cached_tokens
        # and is *included* in prompt_tokens — we split it out so our
        # estimate_cost_usd can apply the discounted rate.
        cached = 0
        prompt_tokens = 0
        completion_tokens = 0
        if response.usage:
            prompt_tokens = response.usage.prompt_tokens or 0
            completion_tokens = response.usage.completion_tokens or 0
            details = getattr(response.usage, "prompt_tokens_details", None)
            if details is not None:
                cached = getattr(details, "cached_tokens", 0) or 0

        usage = Usage(
            input_tokens=max(prompt_tokens - cached, 0),
            output_tokens=completion_tokens,
            cache_read_tokens=cached,
            cache_creation_tokens=0,
        )
        return StepResponse(
            blocks=blocks,
            stop_reason=choice.finish_reason or "",
            usage=usage,
        )

    def estimate_cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float | None:
        model_lc = self.model.lower()
        # OpenRouter and similar prefix the model with `<provider>/`. Strip it
        # so our pricing table (keyed by raw model name) still matches.
        # Note: OpenRouter adds a small markup over upstream — figures here
        # under-report by a few percent.
        if "/" in model_lc:
            model_lc = model_lc.rsplit("/", 1)[-1]
        rates = next((r for prefix, r in _PRICING if model_lc.startswith(prefix)), None)
        if rates is None:
            return None
        cache_rate = rates.get("cache_read", rates["in"])
        return (
            input_tokens * rates["in"]
            + output_tokens * rates["out"]
            + cache_read_tokens * cache_rate
        ) / 1_000_000
