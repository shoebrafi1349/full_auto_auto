"""
core/llm.py — Pluggable LLM backends
======================================
One backend class per provider. Each agent is assigned its own backend
in main.py — swapping providers never touches agent code.

Backends:
  OpenAIBackend    — GPT-4o / GPT-4-turbo  (Planner)
  OpenAICodexBackend — codex-mini / o3-mini  (Coder)  — uses Responses API
  GeminiBackend    — Gemini 2.0 Flash        (Fixer, Supervisor temporarily)
  AnthropicBackend — Claude                  (Supervisor, future)
  OllamaBackend    — local models            (fallback / Tester)
  MockBackend      — canned responses        (tests / dry runs)

Required environment variables:
  OPENAI_API_KEY   — for OpenAIBackend and OpenAICodexBackend
  GEMINI_API_KEY   — for GeminiBackend
  ANTHROPIC_API_KEY — for AnthropicBackend (future)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("llm")

CALL_TIMEOUT_SEC = 120   # 2 min — cloud APIs are much faster than local
MAX_RETRIES      = 2


@dataclass
class LLMResponse:
    text:         str
    model:        str
    input_chars:  int
    output_chars: int
    duration_s:   float
    ok:           bool
    error:        Optional[str] = None


class BaseLLMBackend(ABC):
    model: str

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.15,
    ) -> LLMResponse:
        ...

    async def complete_with_retry(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.15,
    ) -> LLMResponse:
        last_err = ""
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                log.info("[%s] retry %d/%d", self.model, attempt, MAX_RETRIES)
                await asyncio.sleep(5)
            resp = await self.complete(prompt, system, max_tokens, temperature)
            if resp.ok:
                return resp
            last_err = resp.error or "unknown"
            log.warning("[%s] attempt %d failed: %s", self.model, attempt + 1, last_err)
        return LLMResponse(
            text="", model=self.model,
            input_chars=len(prompt), output_chars=0,
            duration_s=0, ok=False, error=last_err,
        )

    def _thread_call(self, fn, timeout: float = CALL_TIMEOUT_SEC) -> dict:
        """Run a blocking function in a daemon thread. Returns {"result": ..., "error": ...}."""
        holder: dict = {"result": None, "error": None}
        def _run():
            try:
                holder["result"] = fn()
            except Exception as e:
                holder["error"] = str(e)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            holder["error"] = f"timeout after {timeout}s"
        return holder


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI — Chat Completions API  (Planner: GPT-4o)
# ══════════════════════════════════════════════════════════════════════════════

class OpenAIBackend(BaseLLMBackend):
    """
    Standard OpenAI Chat Completions API.
    Used by: Planner
    Default model: gpt-4o
    Requires: OPENAI_API_KEY
    """

    def __init__(self, model: str = "gpt-4o"):
        self.model    = model
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            log.warning("OPENAI_API_KEY not set — OpenAIBackend will fail")

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if not self._api_key:
            return LLMResponse("", self.model, len(prompt), 0, 0,
                               False, "OPENAI_API_KEY not set")

        t0 = time.monotonic()
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _call():
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        holder = self._thread_call(_call)
        dur = time.monotonic() - t0

        if holder["error"]:
            return LLMResponse("", self.model, len(prompt), 0, dur,
                               False, holder["error"])

        resp = holder["result"]
        text = resp.choices[0].message.content or ""
        return LLMResponse(text, self.model, len(prompt), len(text), dur, bool(text))


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI Codex — Responses API  (Coder: o3-mini / codex-mini-latest)
# ══════════════════════════════════════════════════════════════════════════════

class OpenAICodexBackend(BaseLLMBackend):
    """
    OpenAI Responses API — used for reasoning/coding models (o3-mini, codex-mini).
    These models use the Responses API, not Chat Completions.
    Used by: Coder
    Default model: o4-mini
    Requires: OPENAI_API_KEY
    """

    def __init__(self, model: str = "o4-mini"):
        self.model    = model
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            log.warning("OPENAI_API_KEY not set — OpenAICodexBackend will fail")

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 1.0,   # reasoning models require temperature=1
    ) -> LLMResponse:
        if not self._api_key:
            return LLMResponse("", self.model, len(prompt), 0, 0,
                               False, "OPENAI_API_KEY not set")

        t0 = time.monotonic()
        full_input = f"{system}\n\n{prompt}" if system else prompt

        def _call():
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            # Responses API — used by o-series and codex models
            response = client.responses.create(
                model=self.model,
                input=full_input,
                max_output_tokens=max_tokens,
            )
            return response

        holder = self._thread_call(_call)
        dur = time.monotonic() - t0

        if holder["error"]:
            # Fallback: if Responses API not available, try Chat Completions
            log.warning("[%s] Responses API failed (%s) — falling back to Chat Completions",
                        self.model, holder["error"])
            return await self._fallback_chat(prompt, system, max_tokens, t0)

        resp   = holder["result"]
        # Extract text from Responses API output
        text = ""
        if hasattr(resp, "output"):
            for item in resp.output:
                if hasattr(item, "content"):
                    for block in item.content:
                        if hasattr(block, "text"):
                            text += block.text
                elif hasattr(item, "text"):
                    text += item.text
        if not text and hasattr(resp, "output_text"):
            text = resp.output_text or ""

        return LLMResponse(text, self.model, len(prompt), len(text), dur, bool(text))

    async def _fallback_chat(
        self, prompt: str, system: str, max_tokens: int, t0: float
    ) -> LLMResponse:
        """Fall back to Chat Completions if Responses API is unavailable."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        def _call():
            from openai import OpenAI
            client = OpenAI(api_key=self._api_key)
            return client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
            )

        holder = self._thread_call(_call)
        dur = time.monotonic() - t0

        if holder["error"]:
            return LLMResponse("", self.model, len(prompt), 0, dur,
                               False, holder["error"])
        text = holder["result"].choices[0].message.content or ""
        return LLMResponse(text, self.model, len(prompt), len(text), dur, bool(text))


# ══════════════════════════════════════════════════════════════════════════════
# Google Gemini  (Fixer + Supervisor temporarily)
# Uses the new google-genai SDK (pip install google-genai)
# NOT the deprecated google-generativeai package
# ══════════════════════════════════════════════════════════════════════════════

class GeminiBackend(BaseLLMBackend):
    """
    Google Gemini via the new google-genai SDK.
    Install: pip install google-genai

    Used by: Fixer, Supervisor (temporary until Anthropic key is ready)
    Default model: gemini-2.0-flash
    Requires: GEMINI_API_KEY environment variable
    """

    def __init__(self, model: str = "gemini-2.0-flash"):
        self.model    = model
        self._api_key = os.environ.get("GEMINI_API_KEY", "")
        if not self._api_key:
            log.warning("GEMINI_API_KEY not set — GeminiBackend will fail")

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.15,
    ) -> LLMResponse:
        if not self._api_key:
            return LLMResponse("", self.model, len(prompt), 0, 0,
                               False, "GEMINI_API_KEY not set")

        t0 = time.monotonic()
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        def _call():
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self._api_key)
            response = client.models.generate_content(
                model=self.model,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
            client.close()
            return response

        holder = self._thread_call(_call)
        dur = time.monotonic() - t0

        if holder["error"]:
            return LLMResponse("", self.model, len(prompt), 0, dur,
                               False, holder["error"])

        resp = holder["result"]
        try:
            text = resp.text or ""
        except Exception as e:
            return LLMResponse("", self.model, len(prompt), 0, dur,
                               False, f"Could not extract text: {e}")

        return LLMResponse(text, self.model, len(prompt), len(text), dur, bool(text))


# ══════════════════════════════════════════════════════════════════════════════
# Anthropic / Claude — future Supervisor (currently disabled)
# ══════════════════════════════════════════════════════════════════════════════

class AnthropicBackend(BaseLLMBackend):
    """
    Claude backend — for when you switch the Supervisor back to Anthropic.
    Requires: ANTHROPIC_API_KEY
    """

    def __init__(self, model: str = "claude-opus-4-5"):
        self.model    = model
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not self._api_key:
            log.warning("ANTHROPIC_API_KEY not set — AnthropicBackend will fail")

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> LLMResponse:
        if not self._api_key:
            return LLMResponse("", self.model, len(prompt), 0, 0,
                               False, "ANTHROPIC_API_KEY not set")

        t0 = time.monotonic()

        def _call():
            import anthropic
            client = anthropic.Anthropic(api_key=self._api_key)
            kwargs: dict = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                kwargs["system"] = system
            return client.messages.create(**kwargs)

        holder = self._thread_call(_call)
        dur = time.monotonic() - t0

        if holder["error"]:
            return LLMResponse("", self.model, len(prompt), 0, dur,
                               False, holder["error"])

        msg  = holder["result"]
        text = msg.content[0].text if msg.content else ""
        return LLMResponse(text, self.model, len(prompt), len(text), dur, bool(text))


# ══════════════════════════════════════════════════════════════════════════════
# Ollama — local fallback (Tester uses this; also available as override)
# ══════════════════════════════════════════════════════════════════════════════

class OllamaBackend(BaseLLMBackend):
    """Local Ollama — free, no API key needed. Used by Tester."""

    def __init__(self, model: str = "qwen2.5-coder:7b"):
        self.model = model

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.15,
    ) -> LLMResponse:
        t0 = time.monotonic()
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        def _call():
            import ollama
            r = ollama.generate(
                model=self.model,
                prompt=full_prompt,
                options={"temperature": temperature, "num_predict": max_tokens},
            )
            return r.response.strip() if hasattr(r, "response") else str(r).strip()

        holder = self._thread_call(_call, timeout=480)
        dur    = time.monotonic() - t0

        if holder["error"]:
            return LLMResponse("", self.model, len(full_prompt), 0, dur,
                               False, holder["error"])

        text = holder["result"] or ""
        return LLMResponse(text, self.model, len(full_prompt), len(text), dur, bool(text))


# ══════════════════════════════════════════════════════════════════════════════
# Mock backend — for dry runs and tests
# ══════════════════════════════════════════════════════════════════════════════

class MockBackend(BaseLLMBackend):
    """Returns canned responses — no network calls. Used for testing."""

    def __init__(self, model: str = "mock", responses: Optional[list[str]] = None):
        self.model  = model
        self._queue = list(responses or [])
        self._default = (
            '{"goals": [{"id": "mock_goal", "title": "Mock goal", '
            '"target_files": ["mock.py"], "description": "Mock task", "mode": "feature"}]}'
        )

    async def complete(self, prompt, system="", max_tokens=4096, temperature=0.15):
        await asyncio.sleep(0.05)
        text = self._queue.pop(0) if self._queue else self._default
        return LLMResponse(text, self.model, len(prompt), len(text), 0.05, True)


# ══════════════════════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════════════════════

def make_backend(
    backend_type: str,
    model: str = "",
    mock_responses: Optional[list[str]] = None,
) -> BaseLLMBackend:
    """
    backend_type:
      "openai"        → OpenAIBackend          (Chat Completions, GPT-4o)
      "openai-codex"  → OpenAICodexBackend     (Responses API, o4-mini)
      "gemini"        → GeminiBackend          (Gemini 2.0 Flash)
      "anthropic"     → AnthropicBackend       (Claude, future Supervisor)
      "ollama"        → OllamaBackend          (local, Tester / fallback)
      "mock"          → MockBackend            (tests / dry runs)
    """
    if backend_type == "openai":
        return OpenAIBackend(model or "gpt-4o")
    if backend_type == "openai-codex":
        return OpenAICodexBackend(model or "o4-mini")
    if backend_type == "gemini":
        return GeminiBackend(model or "gemini-2.0-flash")
    if backend_type == "anthropic":
        return AnthropicBackend(model or "claude-opus-4-5")
    if backend_type == "ollama":
        return OllamaBackend(model or "qwen2.5-coder:7b")
    if backend_type == "mock":
        return MockBackend(model or "mock", mock_responses)
    raise ValueError(f"Unknown backend: {backend_type!r}")