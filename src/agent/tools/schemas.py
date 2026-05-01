"""Pydantic models for tool arguments.

Each tool gets a model with `extra='forbid'` so an unexpected field is
caught before we try to call into Playwright. The JSON-Schema we send to
the LLM is generated from these models — no more drift between the
LLM-visible schema and what the handler actually accepts.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _BaseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavigateArgs(_BaseArgs):
    url: str = Field(..., description="Absolute URL, including https://.")


class ObserveArgs(_BaseArgs):
    pass


class ClickArgs(_BaseArgs):
    element_id: str = Field(..., description="Ref like `e6` from the latest observe().")


class TypeArgs(_BaseArgs):
    element_id: str
    text: str
    submit: bool = Field(False, description="Press Enter after typing.")


class PressKeyArgs(_BaseArgs):
    element_id: str
    key: str = Field(..., description="Playwright key syntax (Enter, Escape, Control+A, …).")


class SelectArgs(_BaseArgs):
    element_id: str
    value: str


class ScrollArgs(_BaseArgs):
    direction: Literal["down", "up", "top", "bottom"]
    amount: int = Field(800, description="Pixels for up/down. Ignored for top/bottom.")


class WaitForArgs(_BaseArgs):
    condition: Literal["network_idle", "load", "url_contains", "text_visible"]
    value: str | None = Field(None, description="Required for url_contains and text_visible.")
    timeout_seconds: float = 10.0


class GoBackArgs(_BaseArgs):
    pass


class GoForwardArgs(_BaseArgs):
    pass


class ExtractArgs(_BaseArgs):
    instruction: str = Field(
        ...,
        description=(
            "What to extract. E.g. 'list the top 10 emails: subject, sender, "
            "snippet, is_unread (boolean)'."
        ),
    )


class RememberArgs(_BaseArgs):
    key: str
    value: str


class RecallArgs(_BaseArgs):
    key: str


class AskUserArgs(_BaseArgs):
    question: str


class DoneArgs(_BaseArgs):
    report: str


def to_tool_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic → the trimmed JSON-Schema shape both Anthropic and OpenAI
    accept for tool/function definitions.

    Trim:
    - Top-level and per-field `title` (auto-generated from the class/field
      name, just noise for the model).
    - `anyOf: [str, null]` (from Optional[str]) → flat `type: string`. Some
      providers reject the null variant in tool schemas.
    - Pydantic-internal `$defs` block.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.pop("$defs", None)

    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
        any_of = prop.get("anyOf")
        if any_of:
            non_null = [t for t in any_of if t.get("type") != "null"]
            if len(non_null) == 1:
                prop.update(non_null[0])
                prop.pop("anyOf", None)

    return schema
