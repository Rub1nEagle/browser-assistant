"""Cost calc — known models price out, unknowns return None."""
from __future__ import annotations

from agent.llm.anthropic_client import AnthropicClient
from agent.llm.openai_client import OpenAIClient


def test_anthropic_known_model_is_priced():
    client = AnthropicClient(api_key="x", model="claude-sonnet-4-6")
    cost = client.estimate_cost_usd(input_tokens=1_000, output_tokens=1_000)
    assert cost is not None and cost > 0


def test_anthropic_unknown_model_returns_none():
    client = AnthropicClient(api_key="x", model="claude-magic-7-2050")
    assert client.estimate_cost_usd(input_tokens=1_000, output_tokens=1_000) is None


def test_openai_known_model_is_priced():
    client = OpenAIClient(api_key="x", model="gpt-4o-mini")
    cost = client.estimate_cost_usd(input_tokens=1_000, output_tokens=1_000)
    # gpt-4o-mini: 0.15/M input + 0.60/M output → 0.00075 for 1k+1k.
    assert cost is not None
    assert abs(cost - 0.00075) < 1e-9


def test_openai_openrouter_prefix_is_stripped():
    """OpenRouter uses `provider/model`; pricing should still match the
    underlying model name."""
    a = OpenAIClient(api_key="x", model="openai/gpt-4o-mini").estimate_cost_usd(
        input_tokens=1_000, output_tokens=1_000,
    )
    b = OpenAIClient(api_key="x", model="gpt-4o-mini").estimate_cost_usd(
        input_tokens=1_000, output_tokens=1_000,
    )
    assert a == b


def test_openai_unknown_model_returns_none():
    client = OpenAIClient(api_key="x", model="custom/llama-7b")
    assert client.estimate_cost_usd(input_tokens=1_000, output_tokens=1_000) is None


def test_cached_tokens_are_cheaper_than_fresh_input():
    client = OpenAIClient(api_key="x", model="gpt-4o-mini")
    fresh = client.estimate_cost_usd(input_tokens=10_000, output_tokens=0)
    cached = client.estimate_cost_usd(
        input_tokens=0, output_tokens=0, cache_read_tokens=10_000,
    )
    assert fresh is not None and cached is not None
    assert cached < fresh
