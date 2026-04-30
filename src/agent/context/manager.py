from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm.types import (
    Block,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)


# Regex over the URL/title preamble that observe() emits.
_URL_LINE = re.compile(r"^URL:\s*(.+)$", re.MULTILINE)
_REF_COUNT = re.compile(r"\[ref=e\d+\]")


def _summarise_observe(content: str) -> str:
    """Replace a full observe snapshot with a one-liner. Older observes
    eat tens of thousands of tokens; the agent rarely needs them once
    fresher state has overwritten the page."""
    url_match = _URL_LINE.search(content)
    url = url_match.group(1).strip() if url_match else "(unknown URL)"
    refs = len(_REF_COUNT.findall(content))
    return (
        f"[earlier observe — collapsed to save tokens] "
        f"URL was {url}; {refs} interactive refs at the time. "
        f"Refs from this observe are stale; call observe() again to re-fetch."
    )


@dataclass
class ContextManager:
    """Owns the conversation history and the scratchpad.

    Compression policy: every observe() tool_result older than the most
    recent one is collapsed to a one-line summary at message-build time.
    Other tool_results pass through verbatim. The scratchpad is appended
    to the system prompt as a non-cached suffix.
    """

    system_base: str
    _messages: list[Message] = field(default_factory=list)
    # tool_use_id → tool name, so we know which ToolResultBlocks came from observe().
    _tool_origin: dict[str, str] = field(default_factory=dict)
    _scratchpad: dict[str, str] = field(default_factory=dict)

    # --- mutation --------------------------------------------------------

    def add_user_text(self, text: str) -> None:
        self._messages.append(Message(role="user", blocks=[TextBlock(text=text)]))

    def record_assistant(self, blocks: list[Block]) -> None:
        for b in blocks:
            if isinstance(b, ToolUseBlock):
                self._tool_origin[b.id] = b.name
        self._messages.append(Message(role="assistant", blocks=list(blocks)))

    def record_tool_results(self, results: list[ToolResultBlock]) -> None:
        if not results:
            return
        # OpenAI flattens these out at the adapter layer; we keep the
        # Anthropic-shaped grouping internally.
        self._messages.append(Message(role="user", blocks=list(results)))

    # --- scratchpad ------------------------------------------------------

    def remember(self, key: str, value: str) -> None:
        self._scratchpad[str(key)] = str(value)

    def recall(self, key: str) -> str | None:
        return self._scratchpad.get(str(key))

    def list_scratchpad(self) -> dict[str, str]:
        return dict(self._scratchpad)

    # --- export to LLM --------------------------------------------------

    def build_system(self) -> str:
        if not self._scratchpad:
            return self.system_base
        lines = ["", "# Scratchpad (your persistent notes for this task)", ""]
        for k, v in self._scratchpad.items():
            v_one = " ".join(v.splitlines())
            v_one = v_one if len(v_one) <= 240 else v_one[:237] + "…"
            lines.append(f"- **{k}**: {v_one}")
        return self.system_base + "\n" + "\n".join(lines)

    def build_messages(self) -> list[Message]:
        latest_observe_id = self._latest_observe_result_id()
        out: list[Message] = []
        for m in self._messages:
            if m.role != "user":
                out.append(m)
                continue
            new_blocks: list[Block] = []
            for b in m.blocks:
                if (
                    isinstance(b, ToolResultBlock)
                    and self._tool_origin.get(b.tool_use_id) == "observe"
                    and b.tool_use_id != latest_observe_id
                ):
                    new_blocks.append(ToolResultBlock(
                        tool_use_id=b.tool_use_id,
                        content=_summarise_observe(b.content),
                        is_error=b.is_error,
                    ))
                else:
                    new_blocks.append(b)
            out.append(Message(role="user", blocks=new_blocks))
        return out

    # --- helpers ---------------------------------------------------------

    def _latest_observe_result_id(self) -> str | None:
        for m in reversed(self._messages):
            if m.role != "user":
                continue
            for b in reversed(m.blocks):
                if (
                    isinstance(b, ToolResultBlock)
                    and self._tool_origin.get(b.tool_use_id) == "observe"
                ):
                    return b.tool_use_id
        return None
