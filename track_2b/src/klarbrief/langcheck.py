"""Which language is a text in? Small, deterministic, standard library only.

Used twice: to tell the model which language the letter is in, and to check that the
explanation came back in the reader's language (small models drift into the letter's
language). Non-Latin scripts are recognised by Unicode block; Latin-script languages by
their most common short words. The answer can be "unknown"; callers treat that as
"cannot tell", never as a pass or a fail.
"""

from __future__ import annotations

import re
from collections import Counter

SCRIPTS = {  # reader languages written in their own script
    "uk": [(0x0400, 0x04FF)],
    "ar": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "fa": [(0x0600, 0x06FF), (0x0750, 0x077F)],
    "ta": [(0x0B80, 0x0BFF)],
    "ti": [(0x1200, 0x139F)],
}

STOPWORDS = {
    "en": "the and you your to of is by for this must will if with are be have that not or it from".split(),
    "de": "der die das und sie ihr ihre ist nicht mit bis zu den dem ein eine wird werden für auf sind oder wenn sich".split(),
    "fr": "le la les et vous votre est des du un une pour dans avec pas qui que sur au ce vos".split(),
    "it": "il lo gli e di che per un una è del della non con sono al entro deve lei suo sua".split(),
    "pt": "o os e de do da que para um uma você seu sua não com deve é pelo pela".split(),
    "es": "el los y de que para un una usted su no con es debe por del las".split(),
    "sq": "dhe të në për një është me nga ju juaj duhet që nuk deri ose".split(),
    "tr": "ve bir bu için ile olarak size sizin gerekir kadar değil olan veya".split(),
    "sr": "i je da se na u za od ne su ili kao morate vaš vam do".split(),
    "rm": "il la da per cun che betg vus vossa ston fin ina in las ils".split(),
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _script_share(text: str, ranges: list[tuple[int, int]]) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    hit = sum(any(lo <= ord(c) <= hi for lo, hi in ranges) for c in letters)
    return hit / len(letters)


def word_scores(text: str) -> Counter:
    words = [w.casefold() for w in _WORD.findall(text)]
    scores: Counter = Counter()
    for lang, stops in STOPWORDS.items():
        s = set(stops)
        scores[lang] = sum(w in s for w in words)
    return scores


def detect(text: str) -> str:
    """Best guess: a language code, or "unknown"."""
    for lang in ("uk", "ta", "ti"):
        if _script_share(text, SCRIPTS[lang]) >= 0.5:
            return lang
    if _script_share(text, SCRIPTS["ar"]) >= 0.5:
        return "ar"   # Arabic script: Arabic or Persian, not told apart here
    scores = word_scores(text)
    best, n = scores.most_common(1)[0] if scores else ("unknown", 0)
    return best if n >= 3 else "unknown"


def in_language(text: str, target: str) -> bool | None:
    """True / False when the text clearly is / is not in ``target``; None when unsure."""
    if not text.strip():
        return None
    if target in SCRIPTS:
        return _script_share(text, SCRIPTS[target]) >= 0.5
    if target not in STOPWORDS:
        return None
    scores = word_scores(text)
    top = max(scores.values()) if scores else 0
    if top < 3:
        return None
    return scores[target] >= 0.6 * top


def explanation_text(data: dict) -> str:
    """The reader-facing text of a model reply: summary plus every point's text."""
    parts = [data.get("summary") or ""]
    for key in ("sender", "topic"):
        if isinstance(data.get(key), dict):
            parts.append(str(data[key].get("text") or ""))
    for key in ("actions", "deadlines", "amounts", "consequences", "contact"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
    return " ".join(p for p in parts if isinstance(p, str))
