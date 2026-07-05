"""Claude wiring for DeepEval (CHO-18).

DeepEval defaults to OpenAI. This module wires Claude (`claude-sonnet-5`) in as the
Synthesizer's generation + quality-scoring model.

Two paths, resolved at import/construction time by :func:`make_claude_model`:

1. **Native** — the installed ``deepeval`` (pinned 4.0.7) ships
   ``deepeval.models.AnthropicModel``, a schema-aware model class. This is the
   preferred path (D5 / task 2.2): DeepEval's Synthesizer leans hard on structured
   (pydantic-schema) generation, and the native class implements that against the
   Anthropic tool API for us.
2. **Wrapper fallback** — :class:`ClaudeModel`, a ``DeepEvalBaseLLM`` subclass over
   the raw Anthropic SDK, used only if a future/older ``deepeval`` lacks the native
   class. It supports schema-structured output via Anthropic forced tool use.

The Anthropic API key is read from the repo ``.env`` (``ANTHROPIC_API_KEY``) and passed
explicitly, so DeepEval's own settings/telemetry files are never relied upon.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from anthropic import Anthropic, AsyncAnthropic
from deepeval.models import DeepEvalBaseLLM

DEFAULT_MODEL = "claude-sonnet-5"

# Per-token pricing for cost bookkeeping. deepeval's pricing table has no entry for
# `claude-sonnet-5`, so its native AnthropicModel.calculate_cost() returns None →
# DeepEval's `synthesis_cost += None` blows up. Supplying nonzero prices (Sonnet's
# published $3 / $15 per 1M input/output tokens) makes cost a real float. Approximate
# and only used for a logged estimate — never affects the dataset.
COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
COST_PER_OUTPUT_TOKEN = 15.0 / 1_000_000


# --------------------------------------------------------------------------- #
# Wrapper fallback — only used if deepeval has no native AnthropicModel.
# --------------------------------------------------------------------------- #
class ClaudeModel(DeepEvalBaseLLM):
    """Minimal DeepEvalBaseLLM over the Anthropic SDK.

    Implements the four abstract methods DeepEval requires: ``load_model``,
    ``generate``, ``a_generate``, ``get_model_name``. When DeepEval passes a pydantic
    ``schema``, we force a single-tool call whose input schema is the model's JSON
    schema and parse the tool input back into the pydantic object — the same contract
    the native class honours.
    """

    def __init__(self, model: str = DEFAULT_MODEL, api_key: Optional[str] = None,
                 max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens
        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (repo .env).")
        self._key = key
        self._client = Anthropic(api_key=key)
        self._aclient = AsyncAnthropic(api_key=key)

    # DeepEvalBaseLLM abstract API ----------------------------------------- #
    def load_model(self):
        return self._client

    def get_model_name(self) -> str:
        return self.model

    def generate(self, prompt: str, schema=None):
        if schema is not None:
            msg = self._client.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                tools=[self._tool_for(schema)],
                tool_choice={"type": "tool", "name": "extract"},
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_tool(msg, schema)
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _text(msg)

    async def a_generate(self, prompt: str, schema=None):
        if schema is not None:
            msg = await self._aclient.messages.create(
                model=self.model, max_tokens=self.max_tokens,
                tools=[self._tool_for(schema)],
                tool_choice={"type": "tool", "name": "extract"},
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_tool(msg, schema)
        msg = await self._aclient.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _text(msg)

    # helpers -------------------------------------------------------------- #
    @staticmethod
    def _tool_for(schema) -> dict:
        return {
            "name": "extract",
            "description": "Return the result strictly matching the schema.",
            "input_schema": schema.model_json_schema(),
        }

    @staticmethod
    def _parse_tool(msg, schema):
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                return schema.model_validate(block.input)
        # Fallback: try to parse any text as JSON into the schema.
        return schema.model_validate(json.loads(_text(msg)))


def _text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


# --------------------------------------------------------------------------- #
# Factory — prefer native, fall back to wrapper (tasks 2.1 / 2.2).
# --------------------------------------------------------------------------- #
def make_claude_model(model: str = DEFAULT_MODEL, api_key: Optional[str] = None):
    """Return a DeepEval-compatible Claude model, preferring the native class.

    Returns a tuple ``(model_obj, kind)`` where ``kind`` is ``"native"`` or
    ``"wrapper"`` for logging/verification.
    """
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set (repo .env).")
    try:
        from deepeval.models import AnthropicModel  # native, schema-aware
        # Disable extended thinking: deepeval's native class reads `content[0].text`,
        # which explodes when Claude emits a leading ThinkingBlock. We only need the
        # structured/text answer, so turn thinking off for deterministic output.
        return AnthropicModel(
            model=model, api_key=key,
            cost_per_input_token=COST_PER_INPUT_TOKEN,
            cost_per_output_token=COST_PER_OUTPUT_TOKEN,
            # thinking off (deepeval reads content[0].text); generous per-request
            # timeout so batched calls don't trip the client's default under load.
            generation_kwargs={"thinking": {"type": "disabled"}, "timeout": 120.0},
        ), "native"
    except Exception:  # noqa: BLE001 — any absence/ctor drift → wrapper
        return ClaudeModel(model=model, api_key=key), "wrapper"


def verify_model(model_obj) -> str:
    """Task 2.3 — a single generate() call to prove wiring before bulk generation.

    Native AnthropicModel.generate returns ``(text, cost)``; the wrapper returns a
    plain string. Normalise to the text so callers can print it.
    """
    out = model_obj.generate("Reply with exactly the word: OK")
    if isinstance(out, tuple):
        out = out[0]
    return str(out).strip()
