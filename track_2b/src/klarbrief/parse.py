"""Find dates and CHF amounts in letter text (German, French, Italian, English).

Used twice: by the verifier (a deadline's date or an amount must appear in the quoted words)
and by the evaluation (gold values are compared with what the model extracted).
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {
    # de
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
    # fr
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    # it
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "dicembre": 12,
    # en
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

# 30. November 2026 · 30 novembre 2026 · 1er décembre 2026 · 1° dicembre 2026 · 1º
_DAY_MONTH_YEAR = re.compile(
    rf"\b(\d{{1,2}})(?:\.|er|°|º)?\s+({_MONTH_RE})\s+(\d{{4}})\b", re.IGNORECASE)
# November 30, 2026
_MONTH_DAY_YEAR = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.IGNORECASE)
# 30.11.2026 · 30/11/2026 · 30.11.26
_NUMERIC = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _mk(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str) -> list[date]:
    """All calendar dates written in ``text``, in order of appearance, without duplicates."""
    found: list[tuple[int, date]] = []
    for m in _DAY_MONTH_YEAR.finditer(text):
        d = _mk(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        if d:
            found.append((m.start(), d))
    for m in _MONTH_DAY_YEAR.finditer(text):
        d = _mk(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        if d:
            found.append((m.start(), d))
    for m in _NUMERIC.finditer(text):
        d = _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if d:
            found.append((m.start(), d))
    for m in _ISO.finditer(text):
        d = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    out: list[date] = []
    for _, d in sorted(found, key=lambda x: x[0]):
        if d not in out:
            out.append(d)
    return out


HEADER_CHARS = 600
# "Beispielwil, 2. Oktober 2026" · "Montexemple, le 5 octobre 2026" · "Rechnungsdatum: 1. Oktober 2026"
_DATELINE = re.compile(
    r"^\s*(?:[A-ZÀ-Ý][\w'’\-. ]{1,40},\s*(?:le |den |il |am )?"
    r"|(?:Rechnungsdatum|Datum|Date|Data|Invoice date)\s*:\s*)(.{6,30})$", re.IGNORECASE)


def letter_date(text: str) -> date | None:
    """The letter's own date: a dateline in the header ("Place, 2. Oktober 2026" or
    "Datum: …") within the first 600 characters. None when there is no dated line with a
    full date (e.g. "im Oktober 2026"), so a date from the body is never mistaken for it."""
    for line in text[:HEADER_CHARS].splitlines():
        m = _DATELINE.match(line)
        if m:
            dates = find_dates(m.group(1))
            if dates:
                return dates[0]
    return None


def parse_iso(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    m = _ISO.fullmatch(value.strip())
    return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# CHF 1'250.50 · Fr. 85.– · CHF 120 · 120.00 Franken · 1 250,50 CHF
_NUM = r"\d{1,3}(?:[’'   ]\d{3})*(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?"
_CUR_BEFORE = re.compile(rf"\b(?:CHF|SFr\.?|Fr\.|fr\.)\s?({_NUM})(?:[.,]?[–-]{{1,2}})?", re.IGNORECASE)
_CUR_AFTER = re.compile(rf"({_NUM})(?:[.,]?[–-]{{1,2}})?\s?(?:CHF|Franken|francs|franchi)\b",
                        re.IGNORECASE)


def _to_float(raw: str) -> float | None:
    s = re.sub(r"[’'   ]", "", raw)
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def find_amounts(text: str) -> list[float]:
    """All CHF amounts written in ``text``, in order, without duplicates."""
    hits: list[tuple[int, float]] = []
    for rx in (_CUR_BEFORE, _CUR_AFTER):
        for m in rx.finditer(text):
            v = _to_float(m.group(1))
            if v is not None:
                hits.append((m.start(), v))
    out: list[float] = []
    for _, v in sorted(hits, key=lambda x: x[0]):
        if v not in out:
            out.append(v)
    return out


def to_amount(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    if isinstance(value, str):
        amounts = find_amounts(value) or ([_to_float(value)] if _to_float(value) is not None else [])
        return amounts[0] if amounts else None
    return None
