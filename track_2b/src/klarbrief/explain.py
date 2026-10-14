"""Explain a letter with Apertus, then verify every point against the letter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import langcheck as LC
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


LETTER_LANGUAGE_NAMES = {"de": "German", "fr": "French", "it": "Italian", "en": "English", "rm": "Romansh"}


def build_messages(letter: str, target_lang: str, letter_lang: str | None = None) -> list[dict]:
    """The reader's language is stated before the letter and again after it: small models
    otherwise drift into the letter's own language once they have read it."""
    lang = LANGUAGES.get(target_lang, target_lang)
    source = LETTER_LANGUAGE_NAMES.get(letter_lang or "", "")
    even = f" The letter is in {source}, but" if source and source.lower() not in lang.lower() else ""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Reader's language: {lang}\n\nLetter:\n<<<\n{letter.strip()}\n>>>\n\n"
            f"Reminder:{even} write the summary and every \"text\" value in {lang}. "
            f"Keep every \"quote\" exactly as written in the letter.")},
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
    language_ok: bool | None = None      # reply in the reader's language? None = cannot tell
    retried: bool = False                # a second call asked for the reader's language
    first_raw_text: str | None = None    # the first reply, when it was retried
    seconds: float = 0.0                 # all calls together
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "json_ok": self.json_ok,
            "error": self.error,
            "explanation": self.explanation.to_dict() if self.explanation else None,
            "raw_text": self.raw_text,
            "language_ok": self.language_ok,
            "retried": self.retried,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": round(self.seconds, 2),
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


def language_of_reply(raw: str, target_lang: str) -> bool | None:
    data = parse_json(raw)
    return LC.in_language(LC.explanation_text(data), target_lang) if data else None


def explain(letter: str, target_lang: str, llm: LLM, *, seed: int | None = 0) -> Result:
    """One call; if the reply is clearly not in the reader's language, one more call that asks
    for a rewrite (same quotes, dates and amounts). The result says whether the language is
    right, so the UI can tell the reader instead of hiding it."""
    messages = build_messages(letter, target_lang, LC.detect(letter))
    reply = llm.chat(messages, schema=OUTPUT_SCHEMA, seed=seed)
    raw, first, lang_ok, retried = reply.text, None, language_of_reply(reply.text, target_lang), False
    seconds, p_tok, c_tok = reply.seconds, reply.prompt_tokens, reply.completion_tokens
    if lang_ok is False:
        name = LANGUAGES.get(target_lang, target_lang)
        retry = messages + [
            {"role": "assistant", "content": reply.text},
            {"role": "user", "content": (
                f"Your reply is not in {name}. Rewrite the same JSON with the summary and every "
                f"\"text\" value in {name}. Keep every \"quote\", date and amount exactly as they are.")}]
        second = llm.chat(retry, schema=OUTPUT_SCHEMA, seed=seed)
        seconds += second.seconds
        p_tok += second.prompt_tokens
        c_tok += second.completion_tokens
        retried = True
        if parse_json(second.text) is not None:
            first, raw, reply = reply.text, second.text, second
            lang_ok = language_of_reply(second.text, target_lang)
    expl, ok = from_model_text(raw, letter, target_lang)
    return Result(expl, raw, ok, reply, None if ok else "reply was not valid JSON",
                  language_ok=lang_ok, retried=retried, first_raw_text=first,
                  seconds=seconds, prompt_tokens=p_tok, completion_tokens=c_tok)
