"""Check every point the model returns against the letter itself.

A point is shown to the reader only if:

1. its ``quote`` is found in the letter: exactly after normalising spaces, quote marks, dashes
   and case; or as a near match of at least ``NEAR_MATCH`` similarity; or partly, when a
   verbatim stretch of it covers at least ``PARTIAL_SHARE`` of the quote (paraphrased
   openings), in which case only that stretch is shown as evidence, and
2. for a deadline, the date it states is written in the matched words of the letter
   (a deadline with no date, like "within 30 days", is kept as a relative deadline) and is
   not earlier than the letter's own date (an expired date is not a deadline), and
3. for an amount, the CHF value it states is written in the matched words of the letter.

What is displayed as evidence is always the letter's own words at the matched position, never
the model's copy of them. Points that fail go to ``unverified`` with the reason, and the UI
shows them apart, labelled as not checked.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from . import parse

NEAR_MATCH = 0.90
MIN_QUOTE_CHARS = 4
PARTIAL_SHARE = 0.60      # a verbatim stretch covering at least 60% of the quote ...
PARTIAL_MIN_CHARS = 20    # ... and at least 20 characters long

_TRANSLATE = str.maketrans({
    "’": "'", "‘": "'", "‚": "'", "`": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"', "‹": "'", "›": "'",
    "–": "-", "—": "-", "‑": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
})


def normalise_with_map(text: str) -> tuple[str, list[int]]:
    """Normalised text plus, for each output char, its index in the original text."""
    out_chars: list[str] = []
    index: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch == "­":  # soft hyphen
            continue
        ch = unicodedata.normalize("NFKC", ch).translate(_TRANSLATE).casefold()
        for c in ch:
            if c.isspace():
                if prev_space:
                    continue
                c, prev_space = " ", True
            else:
                prev_space = False
            out_chars.append(c)
            index.append(i)
    while out_chars and out_chars[-1] == " ":
        out_chars.pop()
        index.pop()
    return "".join(out_chars), index


def normalise(text: str) -> str:
    return normalise_with_map(text)[0]


_WRAPPERS = {'"': '"', "'": "'", "“": "”", "„": "“", "«": "»", "»": "«", "‚": "‘", "‘": "’", "”": "”"}


def _strip_quote(q: str) -> str:
    """Remove quotation marks the model wrapped around the whole quote (only as a pair, so a
    closing » that belongs to the letter's own text survives) and leading/trailing ellipses."""
    q = q.strip()
    while len(q) >= 2 and _WRAPPERS.get(q[0]) == q[-1]:
        q = q[1:-1].strip()
    q = re.sub(r"^(\.\.\.|…)\s*|\s*(\.\.\.|…)$", "", q)
    return q.strip()


@dataclass
class Match:
    start: int          # offsets into the ORIGINAL letter text
    end: int
    kind: str           # exact | near | partial
    score: float

    def span(self, letter: str) -> str:
        return letter[self.start:self.end]


def locate(quote: str, letter: str, letter_norm: tuple[str, list[int]] | None = None) -> Match | None:
    """Find ``quote`` in ``letter``. Returns offsets in the original letter, or None."""
    q = normalise(_strip_quote(quote))
    if len(q) < MIN_QUOTE_CHARS:
        return None
    norm, index = letter_norm or normalise_with_map(letter)
    pos = norm.find(q)
    if pos >= 0:
        return Match(index[pos], index[pos + len(q) - 1] + 1, "exact", 1.0)
    # Near match: best window of the same length (±15%) over the normalised letter.
    best = (0.0, -1, -1)
    n = len(q)
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(q)
    step = max(1, n // 8)
    for size in {n, int(n * 0.9), int(n * 1.1)}:
        if size < MIN_QUOTE_CHARS or size > len(norm):
            continue
        for start in range(0, len(norm) - size + 1, step):
            sm.set_seq1(norm[start:start + size])
            if sm.real_quick_ratio() < NEAR_MATCH or sm.quick_ratio() < NEAR_MATCH:
                continue
            r = sm.ratio()
            if r > best[0]:
                best = (r, start, start + size)
    if best[0] >= NEAR_MATCH:
        s0, e0 = _align(q, norm, best[1], best[2])
        ratio = difflib.SequenceMatcher(None, norm[s0:e0], q, autojunk=False).ratio()
        return Match(index[s0], index[e0 - 1] + 1, "near", round(max(ratio, best[0]), 3))
    # Partial: the model paraphrased part of the quote, but a long stretch of it is verbatim.
    lm = difflib.SequenceMatcher(None, norm, q, autojunk=False).find_longest_match(0, len(norm), 0, n)
    if lm.size >= max(PARTIAL_MIN_CHARS, PARTIAL_SHARE * n):
        s0, e0 = lm.a, lm.a + lm.size
        while s0 < e0 and not norm[s0].isalnum():   # trim to whole words
            s0 += 1
        if s0 > 0 and norm[s0 - 1].isalnum():
            while s0 < e0 and norm[s0].isalnum():
                s0 += 1
            while s0 < e0 and not norm[s0].isalnum():
                s0 += 1
        while e0 < len(norm) and norm[e0].isalnum():
            e0 += 1
        if e0 - s0 >= PARTIAL_MIN_CHARS:
            return Match(index[s0], index[e0 - 1] + 1, "partial", round(lm.size / n, 3))
    return None


def _align(q: str, norm: str, s0: int, e0: int) -> tuple[int, int]:
    """Stretch a coarse window to the characters that actually match the quote, then snap
    both ends to whole words, so the evidence shown is a readable piece of the letter."""
    pad = max(8, len(q) // 5)
    ws, we = max(0, s0 - pad), min(len(norm), e0 + pad)
    sm = difflib.SequenceMatcher(None, norm[ws:we], q, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 3]
    if blocks:
        s0, e0 = ws + blocks[0].a, ws + blocks[-1].a + blocks[-1].size
    while s0 > 0 and norm[s0 - 1].isalnum():
        s0 -= 1
    while e0 < len(norm) and norm[e0].isalnum():
        e0 += 1
    return s0, e0


@dataclass
class Point:
    kind: str                     # sender | topic | action | deadline | amount | consequence | contact
    text: str                     # explanation in the reader's language
    quote: str                    # what the model said it quoted
    evidence: str | None = None   # the letter's own words at the matched position
    start: int | None = None
    end: int | None = None
    match: str | None = None      # exact | near | partial
    date: str | None = None       # deadlines: ISO date, or None for a relative deadline
    chf: float | None = None      # amounts
    verified: bool = False
    reason: str | None = None     # why it is not verified

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Explanation:
    summary: str
    target_language: str
    letter_language: str | None
    points: list[Point] = field(default_factory=list)

    @property
    def shown(self) -> list[Point]:
        return [p for p in self.points if p.verified]

    @property
    def unverified(self) -> list[Point]:
        return [p for p in self.points if not p.verified]

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "target_language": self.target_language,
            "letter_language": self.letter_language,
            "shown": [p.to_dict() for p in self.shown],
            "unverified": [p.to_dict() for p in self.unverified],
        }


# Field name in the model's JSON -> point kind
LIST_FIELDS = {
    "actions": "action", "deadlines": "deadline", "amounts": "amount",
    "consequences": "consequence", "contact": "contact", "contacts": "contact",
}
SINGLE_FIELDS = {"sender": "sender", "topic": "topic"}
CONTEXT_CHARS = 25  # a date or amount may sit just outside a tight quote


def points_from_model(data: dict) -> list[Point]:
    """Turn the model's JSON into Points, tolerating small shape deviations."""
    pts: list[Point] = []

    def _one(kind: str, item: object) -> None:
        if isinstance(item, str):
            pts.append(Point(kind=kind, text=item, quote=""))
            return
        if not isinstance(item, dict):
            return
        text = str(item.get("text") or item.get("explanation") or item.get("description") or "").strip()
        quote = str(item.get("quote") or item.get("source") or item.get("evidence") or "").strip()
        p = Point(kind=kind, text=text, quote=quote)
        if kind == "deadline":
            raw = item.get("date")
            d = parse.parse_iso(raw)
            if d is None and isinstance(raw, str) and raw.strip():
                ds = parse.find_dates(raw)
                d = ds[0] if ds else None
            p.date = d.isoformat() if d else None
        if kind == "amount":
            p.chf = parse.to_amount(item.get("chf", item.get("amount")))
        if text or quote:
            pts.append(p)

    for key, kind in SINGLE_FIELDS.items():
        if data.get(key):
            _one(kind, data[key])
    for key, kind in LIST_FIELDS.items():
        value = data.get(key)
        if isinstance(value, dict):
            value = [value]
        for item in value or []:
            _one(kind, item)
    return pts


def verify(points: list[Point], letter: str) -> list[Point]:
    letter_norm = normalise_with_map(letter)
    written_on = parse.letter_date(letter)
    for p in points:
        if not p.quote:
            p.reason = "no quote given"
            continue
        m = locate(p.quote, letter, letter_norm)
        if m is None:
            p.reason = "quote not found in the letter"
            continue
        p.start, p.end, p.match = m.start, m.end, m.kind
        p.evidence = m.span(letter)
        window = letter[max(0, m.start - CONTEXT_CHARS):m.end + CONTEXT_CHARS]
        if p.kind == "deadline":
            if p.date:
                if p.date not in {d.isoformat() for d in parse.find_dates(window)}:
                    p.reason = f"date {p.date} is not written in the quoted words"
                    continue
                if written_on and p.date < written_on.isoformat():
                    p.reason = (f"date {p.date} is before the letter's own date "
                                f"({written_on.isoformat()}), so it has already passed")
                    continue
        if p.kind == "amount":
            if p.chf is None:
                p.reason = "amount has no CHF value"
                continue
            if p.chf not in parse.find_amounts(window):
                p.reason = f"CHF {p.chf:.2f} is not written in the quoted words"
                continue
        p.verified = True
    return points
