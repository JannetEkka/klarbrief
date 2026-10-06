"""OpenAI-compatible client for an Apertus 1.5 endpoint.

Configuration comes from the three variables the Hack Apertus template prescribes:

- ``LLM_BASE_URL`` (default: the Public AI gateway, ``https://api.publicai.co/v1``)
- ``LLM_API_KEY``  (``PUBLICAI_API_KEY`` is read when it is unset, and the run says so)
- ``LLM_NAME``     (default: ``swiss-ai/apertus-v1.5-8b``)

Two API modes:

- ``chat``: ``POST /chat/completions`` with ``tools``; tool calls are read from
  ``message.tool_calls`` or, when the server does not parse them, from the reply text.
- ``raw``:  ``POST /completions`` with the prompt rendered by Apertus's own chat template and
  special tokens kept, so tool calls are read straight from ``<|tools_prefix|>…``.

``auto`` uses ``chat`` and switches to ``raw`` only when the server rejects ``tools``; every
transcript records which mode produced it.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import template as T

DEFAULT_BASE_URL = "https://api.publicai.co/v1"
DEFAULT_MODEL = "swiss-ai/apertus-v1.5-8b"
USER_AGENT = "wiretrap-apertus/0.1 (Hack Apertus 2026 red-teaming)"


@dataclass
class Settings:
    base_url: str
    api_key: str | None
    model: str
    key_source: str
    api_mode: str = "auto"
    timeout: float = 120.0
    max_rpm: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        key, source = os.environ.get("LLM_API_KEY"), "LLM_API_KEY"
        if not key and os.environ.get("PUBLICAI_API_KEY"):
            key, source = os.environ["PUBLICAI_API_KEY"], "PUBLICAI_API_KEY (LLM_API_KEY unset)"
        if not key:
            source = "none"
        return cls(
            base_url=(os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            api_key=key or None,
            model=os.environ.get("LLM_NAME") or DEFAULT_MODEL,
            key_source=source,
            api_mode=os.environ.get("WT_API_MODE", "auto"),
            max_rpm=int(os.environ.get("WT_MAX_RPM", "60")),
        )

    def describe(self) -> str:
        return (f"endpoint={self.base_url} model={self.model} key={self.key_source} "
                f"api_mode={self.api_mode}")


class RateLimiter:
    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(per_minute, 1)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


@dataclass
class Completion:
    text: str                       # visible reply text (special tokens and tool JSON removed)
    tool_calls: list[dict]          # [{"name", "arguments", "id"}]
    thinking: str                   # deliberation, when the model produced any
    raw: str                        # exactly what the server returned as text
    mode: str                       # chat | raw
    finish_reason: str | None
    usage: dict = field(default_factory=dict)
    request: dict = field(default_factory=dict)


class ApiError(RuntimeError):
    pass


class ApertusClient:
    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise ApiError("No API key: set LLM_API_KEY (or PUBLICAI_API_KEY).")
        self.s = settings
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })
        self.limiter = RateLimiter(settings.max_rpm)
        self.mode = settings.api_mode if settings.api_mode in ("chat", "raw") else None
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0}
        self._usage_lock = threading.Lock()

    # -- HTTP -------------------------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.s.base_url}{path}"
        last = None
        for attempt in range(6):
            self.limiter.wait()
            try:
                r = self.session.post(url, data=json.dumps(payload), timeout=self.s.timeout)
            except requests.RequestException as exc:  # network blip: back off and retry
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(min(60, 2 ** attempt + random.random()))
                continue
            if r.status_code == 200:
                data = r.json()
                with self._usage_lock:
                    u = data.get("usage") or {}
                    self.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                    self.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
                    self.usage["requests"] += 1
                return data
            last = f"HTTP {r.status_code}: {r.text[:500]}"
            if r.status_code in (429, 500, 502, 503, 504):
                wait = float(r.headers.get("Retry-After") or 0) or min(60, 2 ** attempt * 2)
                print(f"[client] {last.splitlines()[0][:120]} -> retry in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait + random.random())
                continue
            raise ApiError(last)
        raise ApiError(f"gave up after retries: {last}")

    # -- chat mode --------------------------------------------------------------------------
    def _chat(self, messages: list[dict], tools: list[dict], gen: dict) -> Completion:
        payload = {"model": self.s.model, "messages": messages, **gen}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post("/chat/completions", payload)
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        thinking = msg.get("reasoning_content") or msg.get("reasoning") or ""
        calls = []
        for i, c in enumerate(msg.get("tool_calls") or []):
            fn = c.get("function", {})
            args = fn.get("arguments", "{}")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                args = {"_raw": args}
            calls.append({"id": c.get("id") or f"call_{i}", "name": fn.get("name"), "arguments": args})
        known = {t["function"]["name"] for t in tools} if tools else set()
        th2, visible = T.split_thinking(content)
        thinking = thinking or th2
        if not calls:  # server returned the call as text
            parsed, rest = T.parse_tool_calls(visible, known)
            if parsed:
                calls = [{"id": f"call_{i}", **p} for i, p in enumerate(parsed)]
                visible = rest
        return Completion(text=T.strip_special(visible).strip(), tool_calls=calls, thinking=thinking,
                          raw=content, mode="chat", finish_reason=choice.get("finish_reason"),
                          usage=data.get("usage") or {}, request={"path": "/chat/completions", **gen})

    # -- raw mode ---------------------------------------------------------------------------
    def _raw(self, messages: list[dict], tools: list[dict], gen: dict) -> Completion:
        tmsgs = T.to_template_messages(messages)
        continuing = bool(tmsgs) and tmsgs[-1]["role"] == "tool"
        prompt = T.render(tmsgs, tools, add_generation_prompt=not continuing,
                          continue_assistant=continuing)
        payload = {
            "model": self.s.model, "prompt": prompt, **gen,
            "stop": [T.TOOLS_SUFFIX, T.ASSISTANT_END],
            "skip_special_tokens": False, "include_stop_str_in_output": True,
        }
        data = self._post("/completions", payload)
        choice = data["choices"][0]
        raw = choice.get("text") or ""
        known = {t["function"]["name"] for t in tools} if tools else set()
        thinking, visible = T.split_thinking(raw)
        parsed, rest = T.parse_tool_calls(visible, known)
        calls = [{"id": f"call_{i}", **p} for i, p in enumerate(parsed)]
        return Completion(text=T.strip_special(rest).strip(), tool_calls=calls, thinking=thinking,
                          raw=raw, mode="raw", finish_reason=choice.get("finish_reason"),
                          usage=data.get("usage") or {}, request={"path": "/completions", **gen})

    def complete(self, messages: list[dict], tools: list[dict], *, temperature: float,
                 seed: int | None, max_tokens: int = 700, top_p: float | None = None) -> Completion:
        gen: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        if seed is not None:
            gen["seed"] = seed
        if top_p is not None:
            gen["top_p"] = top_p
        if self.mode == "raw":
            return self._raw(messages, tools, gen)
        try:
            return self._chat(messages, tools, gen)
        except ApiError as exc:
            rejected = str(exc).startswith(("HTTP 400", "HTTP 422"))
            if self.mode is None and tools and rejected:
                print(f"[client] chat endpoint rejected tools ({str(exc)[:160]}); "
                      "switching to raw /completions with the Apertus template", file=sys.stderr)
                self.mode = "raw"
                return self._raw(messages, tools, gen)
            raise

    def list_models(self) -> list[str]:
        r = self.session.get(f"{self.s.base_url}/models", timeout=self.s.timeout)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]
