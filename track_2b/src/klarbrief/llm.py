"""Minimal OpenAI-compatible chat client for Apertus (Python standard library only).

Configured with the three variables the Hack Apertus template prescribes:

- ``LLM_BASE_URL``  e.g. ``https://api.publicai.co/v1`` (Swiss-hosted Apertus), a CSCS endpoint,
  or ``http://llm:8080/v1`` for the on-premise llama.cpp container
- ``LLM_API_KEY``   not needed for a local llama.cpp server
- ``LLM_NAME``      e.g. ``swiss-ai/apertus-v1.5-8b``

``PUBLICAI_API_KEY`` is accepted when ``LLM_API_KEY`` is unset, and the run says so.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

PUBLICAI_BASE_URL = "https://api.publicai.co/v1"
DEFAULT_MODEL = "swiss-ai/apertus-v1.5-8b"
USER_AGENT = "klarbrief/0.1 (Hack Apertus 2026)"


@dataclass
class Settings:
    base_url: str | None
    api_key: str | None
    model: str
    note: str

    @classmethod
    def from_env(cls) -> "Settings":
        base = os.environ.get("LLM_BASE_URL") or None
        key = os.environ.get("LLM_API_KEY") or None
        note = ""
        if not key and os.environ.get("PUBLICAI_API_KEY"):
            key = os.environ["PUBLICAI_API_KEY"]
            note = "key from PUBLICAI_API_KEY (LLM_API_KEY unset)"
            base = base or PUBLICAI_BASE_URL
        model = os.environ.get("LLM_NAME") or DEFAULT_MODEL
        return cls(base.rstrip("/") if base else None, key, model, note)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def describe(self) -> str:
        if not self.base_url:
            return "no LLM endpoint configured (set LLM_BASE_URL)"
        key = "with API key" if self.api_key else "without API key"
        extra = f"; {self.note}" if self.note else ""
        return f"{self.model} at {self.base_url} ({key}{extra})"


class LLMError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    output_mode: str          # json_schema | json_object | text

    @property
    def json_mode(self) -> bool:
        return self.output_mode != "text"


class LLM:
    # Structured-output modes, strongest first. The client falls back one step when the
    # endpoint rejects a mode (HTTP 400/422), says so on stderr, and records the mode used.
    MODES = ("json_schema", "json_object", "text")

    def __init__(self, settings: Settings, timeout: float = 600.0, mode: str | None = None):
        if not settings.base_url:
            raise LLMError("No LLM endpoint: set LLM_BASE_URL (and LLM_API_KEY if it needs one).")
        self.s = settings
        self.timeout = timeout
        self.mode = mode or os.environ.get("KLARBRIEF_OUTPUT_MODE") or "json_schema"
        if self.mode not in self.MODES:
            raise LLMError(f"unknown output mode {self.mode!r}; use one of {self.MODES}")

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.s.base_url + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                     **({"Authorization": f"Bearer {self.s.api_key}"} if self.s.api_key else {})})
        last = ""
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:400]
                last = f"HTTP {e.code}: {body}"
                if e.code in (429, 500, 502, 503, 504):
                    wait = min(60, 3 * 2 ** attempt)
                    print(f"[klarbrief] {last.splitlines()[0][:100]}; retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise LLMError(last) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(min(30, 2 * 2 ** attempt))
        raise LLMError(f"gave up after retries: {last}")

    def chat(self, messages: list[dict], *, schema: dict | None = None, temperature: float = 0.0,
             max_tokens: int = 1200, seed: int | None = 0) -> Reply:
        payload: dict = {"model": self.s.model, "messages": messages,
                         "temperature": temperature, "max_tokens": max_tokens}
        if seed is not None:
            payload["seed"] = seed
        t0 = time.monotonic()
        while True:
            mode = self.mode if (schema or self.mode != "json_schema") else "json_object"
            if mode == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "explanation", "strict": True, "schema": schema}}
            elif mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            else:
                payload.pop("response_format", None)
            try:
                data = self._post("/chat/completions", payload)
                break
            except LLMError as e:
                if mode != "text" and str(e).startswith(("HTTP 400", "HTTP 422")):
                    nxt = self.MODES[self.MODES.index(mode) + 1]
                    print(f"[klarbrief] endpoint rejected response_format={mode} ({str(e)[:120]}); "
                          f"falling back to {nxt}", file=sys.stderr)
                    self.mode = nxt
                    continue
                raise
        msg = data["choices"][0].get("message") or {}
        usage = data.get("usage") or {}
        return Reply(text=msg.get("content") or "",
                     prompt_tokens=int(usage.get("prompt_tokens") or 0),
                     completion_tokens=int(usage.get("completion_tokens") or 0),
                     seconds=round(time.monotonic() - t0, 2), output_mode=mode)
