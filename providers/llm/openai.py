"""
OpenAI (and OpenAI-compatible) LLM provider.

Requires: pip install openai. Set OPENAI_API_KEY in .env.

obsidian.yaml:
    providers:
      llm:
        name: openai
        options:
          default_model: gpt-4o
          models:                 # pipeline tier -> model id
            premium: gpt-4o
            full: gpt-4o
            light: gpt-4o-mini
          # base_url: http://localhost:11434/v1   # any OpenAI-compatible server (e.g. Ollama)
          # api_key_env: OPENAI_API_KEY           # env var holding the key
"""

from __future__ import annotations

import json
import os
from typing import Any

from providers.base import LLMProvider


class OpenAIProvider(LLMProvider):
    """OpenAI GPT provider (chat completions API)."""

    def __init__(
        self,
        default_model: str = "gpt-4o",
        models: dict | None = None,
        base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
    ):
        self._default_model = default_model
        self.models = dict(models or {})
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._client = None
        self._schema_supported = True

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise ImportError(
                    "OpenAI provider requires the openai package. "
                    "Install with: pip install openai"
                )
            api_key = os.getenv(self._api_key_env)
            if not api_key and not self._base_url:
                raise RuntimeError(f"{self._api_key_env} not set — required for the openai LLM provider")
            kwargs = {"api_key": api_key or "not-needed"}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def _create(self, **kwargs):
        """chat.completions.create with a fallback for models that only accept
        max_completion_tokens (o-series / newer models)."""
        client = self._get_client()
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            if "max_completion_tokens" in str(e) and "max_tokens" in kwargs:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                return client.chat.completions.create(**kwargs)
            raise

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        expect_json: bool = True,
        output_schema: dict | None = None,
    ) -> Any:
        model = model or self._default_model
        if (expect_json or output_schema) and "json" not in system_prompt.lower():
            # json_object mode requires the word "json" somewhere in the messages
            system_prompt = system_prompt + "\n\nRespond with valid JSON only."

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )

        response = None
        if output_schema is not None and self._schema_supported:
            try:
                response = self._create(**kwargs, response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": output_schema, "strict": False},
                })
            except Exception as e:
                # Model/server without json_schema support: fall back to json_object
                if "response_format" not in str(e) and "json_schema" not in str(e):
                    raise
                self._schema_supported = False
        if response is None:
            if expect_json or output_schema is not None:
                kwargs["response_format"] = {"type": "json_object"}
            response = self._create(**kwargs)

        raw = (response.choices[0].message.content or "").strip()
        if not expect_json:
            return raw
        try:
            return json.loads(raw, strict=False)
        except (json.JSONDecodeError, ValueError):
            from clients.claude_client import _parse_json_robust
            parsed = _parse_json_robust(raw)
            if parsed is None:
                raise
            return parsed

    def generate_with_search(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_tokens: int = 4000,
        output_schema: dict | None = None,
    ) -> str:
        # Chat completions has no built-in web search — fall back to regular
        # generation with a note in the system prompt.
        enhanced_system = system_prompt + (
            "\n\nNote: Web search is not available with this provider. "
            "Use your training knowledge to provide the best response."
        )
        return self.generate(
            system_prompt=enhanced_system,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            expect_json=False,
        )

    def estimate_cost(self, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
        model = model or self._default_model
        # GPT-4o pricing (as of 2025)
        prices = {
            "gpt-4o": {"input": 2.50, "output": 10.00},
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        }
        p = prices.get(model, {"input": 2.50, "output": 10.00})
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000

    @property
    def name(self) -> str:
        return "OpenAI GPT"
