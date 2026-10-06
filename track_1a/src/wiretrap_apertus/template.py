"""Render conversations in Apertus 1.5's own format, and parse its raw output.

The chat template is the official one from swiss-ai/Apertus-v1.5-8B (identical in the 70B
repo), vendored unchanged in ``assets/``. Rendering with it gives the walkthrough strings the
Track 1A findings schema asks for ("a full conversation as a string in Apertus response
format"), and lets the harness talk to a plain ``/v1/completions`` endpoint when a server does
not parse tool calls itself.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import jinja2

TEMPLATE_SOURCE = "swiss-ai/Apertus-v1.5-8B@a411d838600baf0e3635a3daf66fb7c55fc97bb6"

TOOLS_PREFIX = "<|tools_prefix|>"
TOOLS_SUFFIX = "<|tools_suffix|>"
ASSISTANT_END = "<|assistant_end|>"
INNER_PREFIX = "<|inner_prefix|>"
INNER_SUFFIX = "<|inner_suffix|>"
SPECIAL_TOKENS = (
    "<|system_start|>", "<|system_end|>", "<|developer_start|>", "<|developer_end|>",
    "<|user_start|>", "<|user_end|>", "<|assistant_start|>", ASSISTANT_END,
    INNER_PREFIX, INNER_SUFFIX, TOOLS_PREFIX, TOOLS_SUFFIX,
    "<|tool_output_start|>", "<|tool_output_end|>", "<|image|>", "<|audio|>", "</s>",
)


def _raise(message: str) -> None:
    raise jinja2.TemplateError(message)


def _tojson(value: Any, indent: int | None = None) -> str:
    # Same behaviour as transformers' chat-template tojson: no HTML escaping, keep unicode.
    return json.dumps(value, ensure_ascii=False, indent=indent)


def _load_template() -> jinja2.Template:
    source = resources.files("wiretrap_apertus.assets").joinpath(
        "apertus_v1_5_chat_template.jinja").read_text(encoding="utf-8")
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True,
                             extensions=["jinja2.ext.loopcontrols"])
    env.filters["tojson"] = _tojson
    env.globals["raise_exception"] = _raise
    return env.from_string(source)


_TEMPLATE = None


def render(messages: list[dict], tools: list[dict] | None = None, *,
           add_generation_prompt: bool = False, continue_assistant: bool = False,
           enable_thinking: bool = False, bos_token: str = "<s>") -> str:
    """Render ``messages`` exactly as Apertus 1.5's chat template does."""
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = _load_template()
    kwargs: dict[str, Any] = {
        "messages": messages,
        "bos_token": bos_token,
        "add_generation_prompt": add_generation_prompt,
        "enable_thinking": enable_thinking,
    }
    if tools:
        kwargs["tools"] = tools
    if continue_assistant:
        kwargs["continue_assistant_message"] = True
    return _TEMPLATE.render(**kwargs)


def to_template_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-style chat messages (tool_calls with JSON-string arguments) into the
    shape the Apertus template expects (arguments as objects, ``content`` always present)."""
    out = []
    for m in messages:
        m = dict(m)
        if m["role"] == "assistant":
            m.setdefault("content", "")
            if m["content"] is None:
                m["content"] = ""
            calls = []
            for c in m.get("tool_calls") or []:
                fn = c.get("function", c)
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                calls.append({"type": "function", "function": {"name": fn["name"], "arguments": args}})
            if calls:
                m["tool_calls"] = calls
            else:
                m.pop("tool_calls", None)
        elif m["role"] == "tool":
            m = {"role": "tool", "content": m.get("content", "")}
        out.append(m)
    return out


def strip_special(text: str) -> str:
    for tok in SPECIAL_TOKENS:
        text = text.replace(tok, "")
    return text


def split_thinking(text: str) -> tuple[str, str]:
    """Return (thinking, visible) for a raw assistant segment."""
    if INNER_PREFIX in text:
        before, _, rest = text.partition(INNER_PREFIX)
        thinking, sep, after = rest.partition(INNER_SUFFIX)
        if not sep:  # unterminated deliberation: everything after the prefix is thinking
            return thinking.strip(), before.strip()
        return thinking.strip(), (before + after).strip()
    return "", text


def parse_tool_calls(text: str, known_tools: set[str] | None = None) -> tuple[list[dict], str]:
    """Find tool calls in raw model text.

    Apertus 1.5 emits ``<|tools_prefix|>[{"name": {args}}, ...]<|tools_suffix|>``. Servers that
    skip special tokens leave only the JSON list, so a bare list of single-key objects whose key
    is a known tool name is accepted too. Returns (calls, remaining_text) where each call is
    ``{"name": str, "arguments": dict}``.
    """
    calls: list[dict] = []
    remaining = text
    decoder = json.JSONDecoder()

    def _take(blob: str) -> list[dict] | None:
        try:
            value, _ = decoder.raw_decode(blob.lstrip())
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return None
        found = []
        for item in value:
            if not isinstance(item, dict) or not item:
                return None
            if "name" in item and "arguments" in item and len(item) <= 3:
                name, args = item["name"], item["arguments"]
            elif len(item) == 1:
                name, args = next(iter(item.items()))
            else:
                return None
            if known_tools is not None and name not in known_tools:
                return None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_value": args}
            found.append({"name": name, "arguments": args})
        return found

    if TOOLS_PREFIX in text:
        before, _, rest = text.partition(TOOLS_PREFIX)
        blob, _, after = rest.partition(TOOLS_SUFFIX)
        parsed = _take(blob)
        if parsed is not None:
            return parsed, (before + after)
        return calls, remaining

    # Bare JSON list (special tokens stripped by the server): only accept it when it starts the
    # reply or follows the deliberation, and every key is a declared tool.
    stripped = text.lstrip()
    if known_tools and stripped.startswith(("[", "{")):
        parsed = _take(stripped)
        if parsed:
            try:
                _, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                end = len(stripped)
            return parsed, stripped[end:]
    return calls, remaining
