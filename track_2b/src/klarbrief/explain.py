"""Explain a letter with Apertus, then verify every point against the letter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import verify as V
from .llm import LLM, Reply

LANGUAGES = {
    "en": "English", "de": "German (Deutsch)", "fr": "French (Français)",
    "it": "Italian (Italiano)", "rm": "Romansh (Rumantsch Grischun)",
    "pt": "Portuguese (Português)", "es": "Spanish (Español)", "sq": "Albanian (Shqip)",
    "sr": "Serbian/Croatian/Bosnian (latinica)", "tr": "Turkish (Türkçe)",
    "uk": "Ukrainian (Українська)", "ti": "Tigrinya (ትግርኛ)", "ta": "Tamil (தமிழ்)",
    "ar": "Arabic (العربية)", "fa": "Persian (فارسی)",
}

SYSTEM_PROMPT = """You help people in Switzerland understand official letters (from a commune, canton, tax office, migration office, health insurer, school, court or similar).

You receive the letter and the reader's language. Reply with ONE JSON object and nothing else:

{
  "letter_language": "de|fr|it|en|rm|other",
  "summary": "2-3 short sentences in the READER'S LANGUAGE: who wrote, why, what it means for the reader. Plain words.",
  "sender": {"text": "who sent it, in the reader's language", "quote": "exact words from the letter"},
  "topic": {"text": "what it is about, in the reader's language", "quote": "exact words from the letter"},
  "actions": [{"text": "something the reader must do, in the reader's language", "quote": "exact words from the letter"}],
  "deadlines": [{"date": "YYYY-MM-DD, or null if the letter gives no calendar date", "text": "what must happen by then, in the reader's language", "quote": "exact words from the letter that state this deadline"}],
  "amounts": [{"chf": 0.00, "text": "what this amount is for, in the reader's language", "quote": "exact words from the letter that state this amount"}],
  "consequences": [{"text": "what happens if the reader does nothing, in the reader's language", "quote": "exact words from the letter"}],
  "contact": [{"text": "who to contact and how, in the reader's language", "quote": "exact words from the letter"}]
}

Rules:
- Every "quote" is copied character for character from the letter, in the letter's original language. Short is fine: the few words that prove the point.
- Only include what the letter actually says. If the letter has no deadline, no amount or no consequence, use an empty list. Never guess or calculate a date that is not written in the letter.
- The date of the letter itself is not a deadline.
- "chf" is a number, e.g. 1250.5 for CHF 1'250.50.
- All "text" and "summary" values are in the reader's language. Use simple words, no legal jargon."""


def _item(extra: dict | None = None, required: tuple = ()) -> dict:
    props = {"text": {"type": "string", "maxLength": 400},
             "quote": {"type": "string", "maxLength": 400}}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": [*required, "text", "quote"],
            "additionalProperties": False}


def _list(item: dict, max_items: int) -> dict:
    return {"type": "array", "items": item, "maxItems": max_items}


# The reply's exact shape. Sent as response_format=json_schema so the server constrains
# generation to it (llama.cpp turns it into a grammar; vLLM uses guided decoding).
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "letter_language": {"type": "string", "enum": ["de", "fr", "it", "en", "rm", "other"]},
        "summary": {"type": "string", "maxLength": 900},
        "sender": _item(),
        "topic": _item(),
        "actions": _list(_item(), 5),
        "deadlines": _list(_item({"date": {"anyOf": [
            {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}, {"type": "null"}]}},
            ("date",)), 4),
        "amounts": _list(_item({"chf": {"type": "number"}}, ("chf",)), 5),
        "consequences": _list(_item(), 4),
        "contact": _list(_item(), 3),
    },
    "required": ["letter_language", "summary", "sender", "topic", "actions", "deadlines",
                 "amounts", "consequences", "contact"],
    "additionalProperties": False,
}


def build_messages(letter: str, target_lang: str) -> list[dict]:
    lang = LANGUAGES.get(target_lang, target_lang)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Reader's language: {lang}\n\nLetter:\n<<<\n{letter.strip()}\n>>>"},
    ]


def parse_json(text: str) -> dict | None:
    """The first JSON object in ``text`` (tolerates code fences and chatter around it)."""
    text = re.sub(r"<\|[a-z_]+\|>", "", text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


@dataclass
class Result:
    explanation: V.Explanation | None
    raw_text: str
    json_ok: bool
    reply: Reply | None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "json_ok": self.json_ok,
            "error": self.error,
            "explanation": self.explanation.to_dict() if self.explanation else None,
            "raw_text": self.raw_text,
            "prompt_tokens": self.reply.prompt_tokens if self.reply else 0,
            "completion_tokens": self.reply.completion_tokens if self.reply else 0,
            "seconds": self.reply.seconds if self.reply else 0,
            "output_mode": self.reply.output_mode if self.reply else None,
        }


def from_model_text(raw: str, letter: str, target_lang: str) -> tuple[V.Explanation | None, bool]:
    data = parse_json(raw)
    if data is None:
        return None, False
    points = V.verify(V.points_from_model(data), letter)
    summary = data.get("summary") or ""
    if not isinstance(summary, str):
        summary = json.dumps(summary, ensure_ascii=False)
    lang = data.get("letter_language")
    return V.Explanation(summary=summary.strip(), target_language=target_lang,
                         letter_language=lang if isinstance(lang, str) else None,
                         points=points), True


def explain(letter: str, target_lang: str, llm: LLM, *, seed: int | None = 0) -> Result:
    reply = llm.chat(build_messages(letter, target_lang), schema=OUTPUT_SCHEMA, seed=seed)
    expl, ok = from_model_text(reply.text, letter, target_lang)
    return Result(expl, reply.text, ok, reply, None if ok else "reply was not valid JSON")
