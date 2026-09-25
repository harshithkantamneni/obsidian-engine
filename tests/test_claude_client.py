"""Tests for claude_client.py — .env parsing, cost tracking, JSON handling."""
import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from clients import claude_client


class TestEnvParser:
    """Test claude_client's .env loader (python-dotenv, no ${VAR} expansion)."""

    def _parse_env(self, content, preset=None):
        """Run claude_client._load_env_file on content; return the environment it leaves."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".env"
            path.write_text(content)
            with patch.dict(os.environ, preset or {}, clear=True):
                claude_client._load_env_file(path)
                return dict(os.environ)

    def test_simple_key_value(self):
        result = self._parse_env("FOO=bar")
        assert result["FOO"] == "bar"

    def test_double_quoted_value(self):
        result = self._parse_env('API_KEY="sk-12345"')
        assert result["API_KEY"] == "sk-12345"

    def test_single_quoted_value(self):
        result = self._parse_env("SECRET='my-secret'")
        assert result["SECRET"] == "my-secret"

    def test_value_with_equals(self):
        result = self._parse_env("URL=https://example.com?key=value")
        assert result["URL"] == "https://example.com?key=value"

    def test_comments_skipped(self):
        result = self._parse_env("# This is a comment\nKEY=val")
        assert "KEY" in result
        assert len(result) == 1

    def test_empty_lines_skipped(self):
        result = self._parse_env("\n\nKEY=val\n\n")
        assert result == {"KEY": "val"}

    def test_whitespace_stripped(self):
        result = self._parse_env("  KEY  =  value  ")
        assert result["KEY"] == "value"

    def test_empty_value(self):
        result = self._parse_env("KEY=")
        assert result["KEY"] == ""

    def test_quoted_empty_value(self):
        result = self._parse_env('KEY=""')
        assert result["KEY"] == ""

    def test_value_with_spaces(self):
        result = self._parse_env('MSG="hello world"')
        assert result["MSG"] == "hello world"

    def test_unquoted_value_with_hash(self):
        """A # with no whitespace before it is part of the value, not a comment."""
        result = self._parse_env("COLOR=#FF0000")
        assert result["COLOR"] == "#FF0000"

    def test_dollar_references_load_literally(self):
        result = self._parse_env("A=one\nB=${A}\nC=pre${A}post\nD=${UNSET}\n")
        assert result["B"] == "${A}"
        assert result["C"] == "pre${A}post"
        assert result["D"] == "${UNSET}"

    def test_existing_environment_wins(self):
        result = self._parse_env("KEY=from_file\n", preset={"KEY": "from_env"})
        assert result["KEY"] == "from_env"

    def test_missing_file_is_a_noop(self, tmp_path):
        with patch.dict(os.environ, {}, clear=True):
            claude_client._load_env_file(tmp_path / "absent.env")
            assert dict(os.environ) == {}


class TestCostTracking:
    def test_track_and_get_costs(self):
        claude_client.reset_session_costs()

        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 500
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0

        claude_client._track_usage("claude-sonnet-4-6", usage)
        costs = claude_client.get_session_costs()

        assert costs["tokens"]["claude-sonnet-4-6"]["input"] == 1000
        assert costs["tokens"]["claude-sonnet-4-6"]["output"] == 500
        assert costs["usd_total"] > 0

    def test_reset_clears_costs(self):
        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 100
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0
        claude_client._track_usage("claude-sonnet-4-6", usage)
        claude_client.reset_session_costs()
        costs = claude_client.get_session_costs()
        assert costs["usd_total"] == 0.0
        assert costs["tokens"] == {}

    def test_cache_read_discount(self):
        claude_client.reset_session_costs()

        usage = MagicMock()
        usage.input_tokens = 1000
        usage.output_tokens = 0
        usage.cache_read_input_tokens = 800  # 80% cache hit
        usage.cache_creation_input_tokens = 0

        claude_client._track_usage("claude-sonnet-4-6", usage)
        costs = claude_client.get_session_costs()
        # 200 normal input + 800 cache read at 10% = 200 + 80 = 280 effective tokens
        # Much cheaper than 1000 full-price tokens
        full_price = 1000 * 3.00 / 1_000_000
        assert costs["usd_total"] < full_price

    def test_multiple_models_tracked(self):
        claude_client.reset_session_costs()

        for model in ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]:
            usage = MagicMock()
            usage.input_tokens = 100
            usage.output_tokens = 50
            usage.cache_read_input_tokens = 0
            usage.cache_creation_input_tokens = 0
            claude_client._track_usage(model, usage)

        costs = claude_client.get_session_costs()
        assert len(costs["tokens"]) == 2

    def test_unknown_model_uses_default_pricing(self):
        claude_client.reset_session_costs()

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 100
        usage.cache_read_input_tokens = 0
        usage.cache_creation_input_tokens = 0

        claude_client._track_usage("unknown-model", usage)
        costs = claude_client.get_session_costs()
        assert costs["usd_total"] > 0  # Uses default $3/$15 pricing


class TestJsonParsing:
    """Test the JSON extraction logic used in call_claude."""

    def _strip_fences(self, raw):
        """Replicate the fence-stripping logic from call_claude."""
        import re
        clean = re.sub(r"^```(?:json)?\s*", "", raw)
        clean = re.sub(r"\s*```$", "", clean).strip()
        return clean

    def test_plain_json(self):
        raw = '{"key": "value"}'
        assert json.loads(self._strip_fences(raw)) == {"key": "value"}

    def test_json_with_fences(self):
        raw = '```json\n{"key": "value"}\n```'
        assert json.loads(self._strip_fences(raw)) == {"key": "value"}

    def test_json_with_bare_fences(self):
        raw = '```\n{"key": "value"}\n```'
        assert json.loads(self._strip_fences(raw)) == {"key": "value"}

    def test_json_with_unicode(self):
        """Verify Unicode replacement fallback logic."""
        raw = '{"title": "Caesar\u2019s Fall"}'
        parsed = json.loads(raw)
        assert "Fall" in parsed["title"]


class TestModelConstants:
    def test_model_ids_defined(self):
        assert claude_client.OPUS == "claude-opus-4-6"
        assert claude_client.SONNET == "claude-sonnet-4-6"
        assert claude_client.HAIKU == "claude-haiku-4-5-20251001"

    def test_all_models_have_prices(self):
        for model in [claude_client.OPUS, claude_client.SONNET, claude_client.HAIKU]:
            assert model in claude_client._PRICES
            assert "input" in claude_client._PRICES[model]
            assert "output" in claude_client._PRICES[model]
