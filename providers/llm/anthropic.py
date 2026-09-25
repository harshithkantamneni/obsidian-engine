"""
Anthropic Claude LLM provider — default implementation.

The pipeline's call_claude() talks to Anthropic directly when this provider is
configured; this class exposes the same behaviour through the LLMProvider
interface (it calls the internal Anthropic functions, never call_claude(), so
there is no recursion).
"""

from __future__ import annotations

from typing import Any

from providers.base import LLMProvider


class AnthropicProvider(LLMProvider):
    """Claude LLM provider using the Anthropic API."""

    def __init__(self, models: dict | None = None):
        from clients.claude_client import HAIKU, OPUS, SONNET
        self._default_model = SONNET
        self.models = {"premium": OPUS, "full": SONNET, "light": HAIKU}
        if models:
            self.models.update(models)

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        expect_json: bool = True,
        output_schema: dict | None = None,
    ) -> Any:
        from clients.claude_client import _anthropic_call
        return _anthropic_call(
            system_prompt,
            user_prompt,
            model or self._default_model,
            max_tokens=max_tokens,
            expect_json=expect_json,
            output_schema=output_schema,
        )

    def generate_with_search(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        output_schema: dict | None = None,
    ) -> str:
        from clients.claude_client import _anthropic_call_with_search
        return _anthropic_call_with_search(
            system_prompt,
            user_prompt,
            model or self._default_model,
            max_tokens=max_tokens,
            output_schema=output_schema,
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        from clients.claude_client import _PRICES
        model = model or self._default_model
        prices = _PRICES.get(model, {"input": 3.00, "output": 15.00})
        return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000

    @property
    def name(self) -> str:
        return "Anthropic Claude"
