from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
}


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    api_key: str
    base_url: str | None
    max_steps: int
    max_cost_usd: float
    profile_dir: Path
    # When True, the tool registry intercepts plausibly-destructive actions
    # (delete/send/pay/confirm/...) and asks the user before executing.
    # Set to False for fully unattended runs in trusted environments.
    confirm_destructive: bool = True

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
        if provider not in _DEFAULT_MODELS:
            raise RuntimeError(
                f"LLM_PROVIDER={provider!r} not supported; expected one of {list(_DEFAULT_MODELS)}"
            )

        if provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
            base_url = os.getenv("ANTHROPIC_BASE_URL") or None
            api_key_var = "ANTHROPIC_API_KEY"
        else:
            api_key = os.getenv("OPENAI_API_KEY", "")
            base_url = os.getenv("OPENAI_BASE_URL") or None
            api_key_var = "OPENAI_API_KEY"

        if not api_key:
            raise RuntimeError(
                f"{api_key_var} is not set. Copy .env.example to .env and fill it in."
            )

        model = os.getenv("LLM_MODEL") or _DEFAULT_MODELS[provider]
        return cls(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_steps=int(os.getenv("MAX_STEPS", "60")),
            max_cost_usd=float(os.getenv("MAX_COST_USD", "2.0")),
            profile_dir=Path(os.getenv("BROWSER_PROFILE_DIR", "./.browser-profile")).resolve(),
            confirm_destructive=_parse_bool(os.getenv("CONFIRM_DESTRUCTIVE"), default=True),
        )


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}
