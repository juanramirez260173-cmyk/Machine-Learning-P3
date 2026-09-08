"""LLM provider abstraction.

Two providers share one interface so the whole pipeline runs with or without an
API key:

* AnthropicProvider - Claude via the official SDK with structured (JSON-schema)
  outputs, adaptive thinking and server-side refusal fallbacks.
* RuleBasedProvider - deterministic heuristics (regex / keyword) used for unit
  tests, golden-set evaluation and offline demos. It never invents facts: it only
  copies fields that exist in the supplied evidence.

Every call is recorded as an AgentRun (model, prompt version, tokens, latency,
cost) for the audit log and the agent-quality cockpit view.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Type, TypeVar

from pydantic import BaseModel

from .config import Settings
from .models import AgentRun

T = TypeVar("T", bound=BaseModel)
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


class LLMProvider:
    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.get("llm.model", "claude-opus-5")
        self.prompt_version = settings.get("llm.prompt_version", "1.0.0")
        self.last_run: Optional[AgentRun] = None

    def structured(self, agent: str, system: str, user: str, schema: Type[T],
                   case_id: Optional[str] = None, rule_fallback=None) -> T:
        raise NotImplementedError

    def _record(self, agent: str, case_id: Optional[str], started: float, in_tok: int = 0,
                out_tok: int = 0, error: Optional[str] = None) -> AgentRun:
        price = self.settings.get("llm.pricing_usd_per_mtok", {"input": 0, "output": 0})
        run = AgentRun(case_id=case_id, agent=agent, model=self.model, prompt_version=self.prompt_version,
                       input_tokens=in_tok, output_tokens=out_tok,
                       latency_ms=int((time.time() - started) * 1000),
                       cost_usd=(in_tok * price.get("input", 0) + out_tok * price.get("output", 0)) / 1_000_000,
                       error=error)
        self.last_run = run
        return run


class RuleBasedProvider(LLMProvider):
    """Deterministic provider: each agent passes a `rule_fallback` callable that
    builds the schema object from the structured evidence only."""

    name = "rules"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.model = "rules-deterministic"

    def structured(self, agent: str, system: str, user: str, schema: Type[T],
                   case_id: Optional[str] = None, rule_fallback=None) -> T:
        started = time.time()
        if rule_fallback is None:
            raise RuntimeError(f"{agent}: no deterministic fallback available and no LLM configured")
        result = rule_fallback()
        obj = result if isinstance(result, schema) else schema.model_validate(result)
        run = self._record(agent, case_id, started, in_tok=len(user) // 4, out_tok=len(obj.model_dump_json()) // 4)
        run.cost_usd = 0.0  # deterministic rules cost nothing
        return obj


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        import anthropic  # lazy import: optional dependency

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.effort = settings.get("llm.effort", "medium")
        self.max_tokens = int(settings.get("llm.max_tokens", 16000))
        self.fallbacks = settings.get("llm.fallbacks", "default")

    def structured(self, agent: str, system: str, user: str, schema: Type[T],
                   case_id: Optional[str] = None, rule_fallback=None) -> T:
        started = time.time()
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )
        if self.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = self.fallbacks
        try:
            response = self.client.beta.messages.parse(**kwargs)
        except self._anthropic.RateLimitError as exc:
            self._record(agent, case_id, started, error=f"rate_limited: {exc}")
            raise
        except self._anthropic.APIStatusError as exc:
            self._record(agent, case_id, started, error=f"api_error {exc.status_code}: {exc.message}")
            raise
        except self._anthropic.APIConnectionError as exc:
            self._record(agent, case_id, started, error=f"connection_error: {exc}")
            raise
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        if response.stop_reason == "refusal":
            self._record(agent, case_id, started, in_tok, out_tok, error="refusal")
            if rule_fallback is not None:
                result = rule_fallback()
                return result if isinstance(result, schema) else schema.model_validate(result)
            raise RuntimeError(f"{agent}: model refused the request and no deterministic fallback exists")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:  # defensive: parse the text block
            text = next((b.text for b in response.content if b.type == "text"), "{}")
            parsed = schema.model_validate(json.loads(text))
        self._record(agent, case_id, started, in_tok, out_tok)
        return parsed


def make_provider(settings: Settings) -> LLMProvider:
    mode = (os.environ.get("PQM_AGENT_LLM") or settings.get("llm.provider", "auto")).lower()
    if mode == "rules":
        return RuleBasedProvider(settings)
    if mode == "anthropic":
        return AnthropicProvider(settings)
    # auto: use Claude when credentials are present, otherwise deterministic rules
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            return AnthropicProvider(settings)
        except Exception:  # pragma: no cover - SDK missing or misconfigured
            return RuleBasedProvider(settings)
    return RuleBasedProvider(settings)


# --------------------------------------------------------------------------- #
# Small text utilities shared by rule-based extraction
# --------------------------------------------------------------------------- #
def find_first(pattern: str, text: str, flags=re.IGNORECASE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None
