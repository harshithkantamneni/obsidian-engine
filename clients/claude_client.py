"""
Shared LLM entry point with model routing.
- Opus / "premium": creative writing
- Sonnet / "full":  analytical, judgment-based agents
- Haiku / "light":  mechanical, formatting, classification agents

call_claude() / call_claude_with_search() are the single entry point every
agent uses. With the default ``providers.llm.name: anthropic`` they call the
Anthropic API directly (see _anthropic_call). With any other LLM provider
(built-in "openai" or a custom ``module.ClassName``) they dispatch to
``provider.generate()`` / ``provider.generate_with_search()``, mapping the
Claude model id to a tier ("premium" / "full" / "light") that the provider
resolves via ``resolve_model()``.
"""

import copy
import random
import threading
import time
import json
import re
import os
from pathlib import Path

# Load .env if present
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _v = _v.strip()
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                _v = _v[1:-1]
            os.environ.setdefault(_k.strip(), _v)

# Model constants (from obsidian.yaml → models.*)
try:
    from core.config import cfg as _cfg
    OPUS   = _cfg.models.premium
    SONNET = _cfg.models.full
    HAIKU  = _cfg.models.light
except Exception:
    OPUS   = "claude-opus-4-6"
    SONNET = "claude-sonnet-4-6"
    HAIKU  = "claude-haiku-4-5-20251001"


class _LazyAnthropicClient:
    """Module-level ``client`` that only builds anthropic.Anthropic on first use.

    Users who bring their own LLM provider don't need ANTHROPIC_API_KEY (or
    even the anthropic package) just to import this module. Existing code
    that does ``from clients.claude_client import client`` keeps working, and
    tests can still patch ``clients.claude_client.client``.
    """

    _lock = threading.Lock()

    def __init__(self):
        self._real = None

    def _get(self):
        if self._real is None:
            with self._lock:
                if self._real is None:
                    import anthropic
                    self._real = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        return self._real

    def __getattr__(self, name):
        return getattr(self._get(), name)


client = _LazyAnthropicClient()

# ── Cost tracking (thread-safe) ───────────────────────────────────────────────
_PRICES = {  # USD per 1M tokens
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":          {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input":  0.80, "output":  4.00},
}
_session_costs = {"tokens": {}, "usd_total": 0.0}
_cost_lock = threading.Lock()

def track_usage(model: str, usage):
    """Track token usage and USD cost. Thread-safe."""
    inp          = getattr(usage, "input_tokens",  0) or 0
    out          = getattr(usage, "output_tokens", 0) or 0
    cache_read   = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    prices = _PRICES.get(model, {"input": 3.00, "output": 15.00})
    # Cache reads = 10% of input price; cache creation = 125% of input price
    normal_inp = max(0, inp - cache_read - cache_create)
    usd = (normal_inp * prices["input"]
           + cache_read * prices["input"] * 0.1
           + cache_create * prices["input"] * 1.25
           + out * prices["output"]) / 1_000_000
    with _cost_lock:
        t = _session_costs["tokens"].setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0})
        t["input"]        += inp
        t["output"]       += out
        t["cache_read"]   += cache_read
        t["cache_create"] += cache_create
        _session_costs["usd_total"] = round(_session_costs["usd_total"] + usd, 6)

# Backward compat alias
_track_usage = track_usage

def get_session_costs() -> dict:
    """Return accumulated token usage and USD cost for this process session."""
    with _cost_lock:
        return {
            "tokens":    copy.deepcopy(_session_costs["tokens"]),
            "usd_total": _session_costs["usd_total"],
        }

def reset_session_costs():
    """Reset the session cost accumulator (call at start of each pipeline run)."""
    global _untracked_notice_logged
    _untracked_notice_logged = False
    with _cost_lock:
        _session_costs["tokens"]    = {}
        _session_costs["usd_total"] = 0.0


def _parse_json_robust(raw: str):
    """
    Attempt to parse JSON from a Claude response, handling common issues:
    1. Markdown code fences
    2. Trailing text after valid JSON object
    3. Unicode characters that break json.loads
    4. Truncated JSON (close open brackets/braces)
    Returns parsed dict/list, or None if all attempts fail.
    """
    # Strip markdown code fences
    clean = re.sub(r"^```(?:json)?\s*", "", raw)
    clean = re.sub(r"\s*```$", "", clean).strip()

    # Attempt 1: direct parse
    result = _try_parse(clean)
    if result is not None:
        return result

    # Attempt 2: replace Unicode punctuation
    clean2 = clean.replace("\u2014", "-").replace("\u2013", "-").replace(
        "\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    result = _try_parse(clean2)
    if result is not None:
        return result

    # Attempt 3: extract first complete JSON object/array (handles trailing text)
    result = _extract_first_json(clean2)
    if result is not None:
        return result

    # Attempt 4: repair truncated JSON (close open brackets)
    result = _repair_truncated_json(clean2)
    if result is not None:
        return result

    print(f"[claude_client] All JSON parse attempts failed ({len(raw)} chars)")
    print(f"[claude_client] Response start: {raw[:300]}")
    print(f"[claude_client] Response end:   {raw[-200:]}")
    return None


def _try_parse(text: str):
    """Try json.loads, return parsed value or None."""
    try:
        parsed = json.loads(text, strict=False)
        if not isinstance(parsed, (dict, list)):
            return {"raw": parsed}
        return parsed
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_first_json(text: str):
    """Extract the first complete JSON object or array from text with trailing data."""
    # Find which JSON structure starts first
    obj_idx = text.find('{')
    arr_idx = text.find('[')
    candidates = []
    if obj_idx != -1:
        candidates.append((obj_idx, '{', '}'))
    if arr_idx != -1:
        candidates.append((arr_idx, '[', ']'))
    candidates.sort(key=lambda x: x[0])  # try earliest first

    for idx, start_char, end_char in candidates:
        depth = 0
        in_string = False
        escape = False
        for i in range(idx, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == start_char:
                depth += 1
            elif c == end_char:
                depth -= 1
                if depth == 0:
                    candidate = text[idx:i + 1]
                    result = _try_parse(candidate)
                    if result is not None:
                        if i + 1 < len(text):
                            print(f"[claude_client] Extracted JSON, ignored {len(text) - i - 1} trailing chars")
                        return result
                    break
    return None


def _repair_truncated_json(text: str):
    """Attempt to close truncated JSON by removing the incomplete tail and closing brackets."""
    # Find where JSON starts
    start = -1
    for i, c in enumerate(text):
        if c in ('{', '['):
            start = i
            break
    if start == -1:
        return None

    # Walk through tracking nesting, save snapshots at safe truncation points
    depth_stack = []
    in_string = False
    escape = False
    last_safe = start
    last_safe_stack = []

    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c in ('{', '['):
            depth_stack.append(c)
        elif c == '}' and depth_stack and depth_stack[-1] == '{':
            depth_stack.pop()
            last_safe = i + 1
            last_safe_stack = list(depth_stack)
        elif c == ']' and depth_stack and depth_stack[-1] == '[':
            depth_stack.pop()
            last_safe = i + 1
            last_safe_stack = list(depth_stack)
        elif c == ',' and depth_stack:
            last_safe = i
            last_safe_stack = list(depth_stack)

    if not depth_stack:
        return None  # JSON is complete, not truncated

    # Truncate at last safe point and close remaining brackets using saved stack
    repaired = text[start:last_safe]
    repaired = repaired.rstrip().rstrip(',')
    for bracket in reversed(last_safe_stack):
        repaired += '}' if bracket == '{' else ']'

    result = _try_parse(repaired)
    if result is not None:
        print(f"[claude_client] Repaired truncated JSON (closed {len(last_safe_stack)} brackets)")
    return result


# ── Provider dispatch ─────────────────────────────────────────────────────────

_untracked_notice_logged = False


def model_tier(model: str | None) -> str:
    """Map a Claude model id to a provider-neutral tier.

    OPUS → "premium", SONNET → "full", HAIKU → "light", anything else → "full".
    """
    if not model:
        return "full"
    if model == OPUS:
        return "premium"
    if model == SONNET:
        return "full"
    if model == HAIKU:
        return "light"
    low = str(model).lower()
    if "opus" in low:
        return "premium"
    if "haiku" in low:
        return "light"
    return "full"


def _custom_llm_provider():
    """Return the configured LLM provider, or None for the built-in Anthropic path.

    If the provider config can't be read at all we assume the default
    (Anthropic). A custom provider that fails to load raises — the user asked
    for it, so a silent fallback to Anthropic would be wrong.
    """
    try:
        from providers.registry import get_provider_name
        name = get_provider_name("llm")
    except Exception:
        return None
    if name == "anthropic":
        return None
    from providers.registry import get_provider
    provider = get_provider("llm")
    global _untracked_notice_logged
    if not _untracked_notice_logged:
        _untracked_notice_logged = True
        print(f"[llm] Using LLM provider '{name}' ({getattr(provider, 'name', '?')}) — "
              f"token usage/cost is not tracked for non-Anthropic providers, "
              f"so the per-run budget cap does not apply to LLM spend.")
    return provider


def llm_is_anthropic() -> bool:
    """True when LLM calls go to the built-in Anthropic provider."""
    try:
        from providers.registry import get_provider_name
        return get_provider_name("llm") == "anthropic"
    except Exception:
        return True


def anthropic_vision_available() -> bool:
    """True if Claude vision calls (image scoring) can be made.

    Vision scoring is an Anthropic-specific feature: it runs only when the LLM
    provider is the built-in "anthropic" and ANTHROPIC_API_KEY is set.
    """
    return llm_is_anthropic() and bool(os.environ.get("ANTHROPIC_API_KEY"))


def call_claude(
    system_prompt: str,
    user_prompt: str,
    model: str = SONNET,
    max_tokens: int = 4000,
    expect_json: bool = True,
    output_schema: dict | None = None,
) -> object:
    """
    Call the configured LLM and return parsed JSON or raw text.

    Default (providers.llm.name: anthropic): Claude via the Anthropic API.
    Otherwise: ``provider.generate()`` with model=provider.resolve_model(tier).
    """
    provider = _custom_llm_provider()
    if provider is None:
        return _anthropic_call(system_prompt, user_prompt, model, max_tokens,
                               expect_json, output_schema)
    result = provider.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=provider.resolve_model(model_tier(model)),
        max_tokens=max_tokens,
        expect_json=expect_json,
        output_schema=output_schema,
    )
    if expect_json and isinstance(result, str):
        parsed = _parse_json_robust(result)
        if parsed is None:
            raise json.JSONDecodeError(
                f"Could not parse JSON from LLM provider response ({len(result)} chars)",
                result[:200], 0,
            )
        return parsed
    return result


def call_claude_with_search(
    system_prompt: str,
    user_prompt: str,
    model: str = SONNET,
    max_tokens: int = 4000,
    output_schema: dict | None = None,
) -> str:
    """
    Call the configured LLM with web search enabled. Returns raw text.

    Default: Claude with the web_search tool. Otherwise:
    ``provider.generate_with_search()`` (providers without search may fall
    back to plain generation).
    """
    provider = _custom_llm_provider()
    if provider is None:
        return _anthropic_call_with_search(system_prompt, user_prompt, model,
                                           max_tokens, output_schema)
    return provider.generate_with_search(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=provider.resolve_model(model_tier(model)),
        max_tokens=max_tokens,
        output_schema=output_schema,
    )


# ── Anthropic implementation (built-in "anthropic" provider) ─────────────────

def _anthropic_call(
    system_prompt: str,
    user_prompt: str,
    model: str = SONNET,
    max_tokens: int = 4000,
    expect_json: bool = True,
    output_schema: dict | None = None,
) -> object:
    """
    Call Claude and return parsed JSON or raw text.
    If expect_json=True, strips markdown fences and parses.
    System prompts > 1000 chars are wrapped with prompt caching.
    """
    # Wrap long system prompts with cache_control for prompt caching
    if len(system_prompt) > 1000:
        system_param = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = system_prompt

    # Wrap long user prompts with cache_control too
    if len(user_prompt) > 1000:
        user_content = [{"type": "text", "text": user_prompt, "cache_control": {"type": "ephemeral"}}]
    else:
        user_content = user_prompt

    last_err = None
    for attempt in range(5):
        try:
            create_kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                system=system_param,
                messages=[{"role": "user", "content": user_content}],
            )
            if output_schema is not None:
                create_kwargs["output_config"] = {
                    "format": {
                        "type": "json_schema",
                        "schema": output_schema,
                    }
                }
            response = client.messages.create(**create_kwargs)
            break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if "rate_limit" in err_str and attempt < 4:
                wait = 5 * (attempt + 1) + random.uniform(0, 2.0)  # Tier 3 jittered backoff
                print(f"  [rate limit] waiting {wait:.1f}s before retry {attempt+2}/5...")
                time.sleep(wait)
            elif any(kw in err_str for kw in ["timeout", "connection", "overloaded", "529", "503"]) and attempt < 4:
                wait = 5 * (attempt + 1) + random.uniform(0, 2.0)
                print(f"  [transient error] waiting {wait:.1f}s before retry {attempt+2}/5...")
                time.sleep(wait)
            else:
                raise
    else:
        raise last_err or Exception("Claude API failed after 5 attempts")
    track_usage(model, response.usage)

    # Log cache hits
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    if cache_read > 0:
        print(f"  [cache] hit: {cache_read} tokens read from cache")

    if not response.content:
        raise Exception("Claude returned empty response (no content blocks)")
    raw = response.content[0].text.strip()

    if not expect_json:
        return raw

    # If response was truncated by max_tokens, auto-retry with 2x limit (once)
    if getattr(response, "stop_reason", None) == "max_tokens" and max_tokens < 16000:
        bumped = min(max_tokens * 2, 16000)
        print(f"[claude_client] Response truncated at {max_tokens} tokens — retrying with {bumped}...")
        return _anthropic_call(system_prompt, user_prompt, model, max_tokens=bumped, expect_json=True, output_schema=output_schema)

    # When schema is provided, API guarantees valid JSON — skip _parse_json_robust
    if output_schema is not None:
        return json.loads(raw)

    parsed = _parse_json_robust(raw)
    if parsed is not None:
        return parsed

    raise json.JSONDecodeError(
        f"Could not parse JSON from Claude response ({len(raw)} chars)",
        raw[:200], 0
    )


def _anthropic_call_with_search(
    system_prompt: str,
    user_prompt: str,
    model: str = SONNET,
    max_tokens: int = 4000,
    output_schema: dict | None = None,
) -> str:
    """
    Call Claude with web search tool enabled.
    Returns the full text response (search results embedded).
    """
    # Wrap long system prompts with cache_control for prompt caching
    if len(system_prompt) > 1000:
        system_param = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = system_prompt

    last_err = None
    for attempt in range(5):
        try:
            create_kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                system=system_param,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": user_prompt}],
            )
            if output_schema is not None:
                create_kwargs["output_config"] = {
                    "format": {
                        "type": "json_schema",
                        "schema": output_schema,
                    }
                }
            response = client.messages.create(**create_kwargs)
            break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if "rate_limit" in err_str and attempt < 4:
                wait = 5 * (attempt + 1) + random.uniform(0, 2.0)  # Tier 3 jittered backoff
                print(f"  [rate limit] waiting {wait:.1f}s before retry {attempt+2}/5...")
                time.sleep(wait)
            elif any(kw in err_str for kw in ["timeout", "connection", "overloaded", "529", "503"]) and attempt < 4:
                wait = 5 * (attempt + 1) + random.uniform(0, 2.0)
                print(f"  [transient error] waiting {wait:.1f}s before retry {attempt+2}/5...")
                time.sleep(wait)
            else:
                raise
    else:
        raise last_err or Exception("Claude API (search) failed after 5 attempts")
    track_usage(model, response.usage)

    # Collect all text blocks from response
    text_parts = [
        block.text
        for block in response.content
        if hasattr(block, "text")
    ]
    return "\n".join(text_parts).strip()
