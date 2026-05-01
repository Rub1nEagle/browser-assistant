from __future__ import annotations

from dataclasses import asdict
from typing import Any

from anthropic import AsyncAnthropic

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

# USD per million tokens. Patterns matched in order; first prefix wins.
# Numbers are list prices for the Claude 4 family as of the knowledge cutoff;
# update if the API responds with different billing.
_PRICING: list[tuple[str, dict[str, float]]] = [
    ("claude-opus-4", {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50}),
    ("claude-sonnet-4", {"in": 3.0, "out": 15.0, "cache_write": 3.75, "cache_read": 0.30}),
    ("claude-haiku-4", {"in": 1.0, "out": 5.0, "cache_write": 1.25, "cache_read": 0.10}),
]


def _block_to_dict(block: Block) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        d: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error:
            d["is_error"] = True
        return d
    raise TypeError(f"unknown block: {block!r}")


def _decode_content(content: list[Any]) -> list[Block]:
    out: list[Block] = []
    for item in content:
        # SDK returns pydantic-ish models; access fields by attribute, fall back to dict.
        t = getattr(item, "type", None) or item.get("type")  # type: ignore[union-attr]
        if t == "text":
            out.append(TextBlock(text=getattr(item, "text", None) or item["text"]))
        elif t == "tool_use":
            out.append(ToolUseBlock(
                id=getattr(item, "id", None) or item["id"],
                name=getattr(item, "name", None) or item["name"],
                input=dict(getattr(item, "input", None) or item["input"]),
            ))
        # Other block types (thinking, etc.) are ignored at this layer.
    return out


class AnthropicClient(LLMClient):
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
        # `base_url` lets you route through a proxy/reseller that speaks
        # the Anthropic API (e.g. Russian payment proxies) without code changes.
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**kwargs)
        self.model = model

    async def step(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[Tool],
        max_tokens: int = 4096,
    ) -> StepResponse:
        # Cache the static system prompt and the (also static) tool list.
        system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        tool_params: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        if tool_params:
            tool_params[-1]["cache_control"] = {"type": "ephemeral"}

        message_params = [
            {"role": m.role, "content": [_block_to_dict(b) for b in m.blocks]}
            for m in messages
        ]

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_param,
            tools=tool_params,
            messages=message_params,
        )

        usage = Usage(
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return StepResponse(
            blocks=_decode_content(response.content),
            stop_reason=response.stop_reason or "",
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
        rates = next((r for prefix, r in _PRICING if self.model.startswith(prefix)), None)
        if rates is None:
            return None
        # Anthropic counts cached tokens separately — `input_tokens` is the
        # uncached portion already. So we sum the four buckets independently.
        return (
            input_tokens * rates["in"]
            + output_tokens * rates["out"]
            + cache_creation_tokens * rates["cache_write"]
            + cache_read_tokens * rates["cache_read"]
        ) / 1_000_000
