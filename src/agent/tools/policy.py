"""Confirmation policy for destructive tool calls.

A heuristic substring match against (a) the label of the element being
acted on and (b) the text being typed, in both English and Russian.
The agent's own system prompt already pushes it to stop before
irreversible mutations, but a substring guard is a cheap second line
of defence: if the user asked the agent to "find spam", a misclick on
'Reply All' or 'Send' should still surface to the human.
"""
from __future__ import annotations

# Lower-case substrings. Russian is included as form-prefixes so we catch
# inflections (удалить / удалю / удалён). Tune as needed.
_DESTRUCTIVE_SUBSTRINGS: tuple[str, ...] = (
    # English actions
    "delete", "remove", "discard", "trash",
    "send", "submit", "post comment", "post reply",
    "pay", "purchase", "checkout", "buy", "place order", "place bid",
    "confirm", "approve",
    "archive", "unsubscribe", "withdraw",
    "transfer", "wire",
    # Russian
    "удал",        # удалить, удалю, удалит, удаление
    "отправ",      # отправить, отправлю, отправка
    "оплат",       # оплатить, оплачу, оплата
    "купить", "купи",
    "подтверд",    # подтвердить, подтверждаю, подтверждение
    "архив",
    "оформить",    # оформить заказ
    "перевод", "перевест",
    "списат",      # списать средства
)


def detect_destructive(
    *, tool_name: str, args: dict, ref_label: str
) -> str | None:
    """Return the matched substring if this tool call looks destructive,
    else None. Only `click`, `type` (with submit=True), `press_key`, and
    `select` are checked — pure observation/scrolling never triggers."""
    if tool_name not in {"click", "type", "press_key", "select"}:
        return None

    haystack_parts: list[str] = [ref_label.lower()] if ref_label else []
    if tool_name == "type":
        if not args.get("submit"):
            # Typing without submit hasn't done anything irreversible yet.
            return None
        haystack_parts.append((args.get("text") or "").lower())
    elif tool_name == "select":
        haystack_parts.append((args.get("value") or "").lower())
    elif tool_name == "press_key":
        # Pressing Enter / Space on a focused element behaves like clicking
        # it, so we keep the label-based check active.
        pass

    if not haystack_parts:
        return None
    haystack = " ".join(haystack_parts)
    for needle in _DESTRUCTIVE_SUBSTRINGS:
        if needle in haystack:
            return needle
    return None


# Reply-token sets. Lower-case. Empty string treated as "no".
_YES_TOKENS = frozenset({"yes", "y", "да", "ok", "ок", "approve", "одобрить"})
_ALWAYS_TOKENS = frozenset({"always", "all", "все", "всегда", "always-pattern"})


def parse_confirmation(answer: str) -> str:
    """Return one of: 'yes' (allow once), 'always' (allow this pattern for
    the rest of the run), 'no' (anything else)."""
    norm = (answer or "").strip().lower()
    if norm in _ALWAYS_TOKENS:
        return "always"
    if norm in _YES_TOKENS:
        return "yes"
    return "no"
