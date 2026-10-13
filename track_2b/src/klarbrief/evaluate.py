"""Run Klarbrief over the labelled letters, then score what it showed.

Running and scoring are separate on purpose: ``run`` stores every raw model reply, and
``score`` re-verifies and re-scores those replies offline. Anyone can recompute every number in
the report from ``data/results/*.jsonl`` without access to a model.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path

from . import parse
from .explain import OUTPUT_SCHEMA, build_messages, from_model_text
from .llm import LLM

DATA = Path(__file__).resolve().parents[2] / "data"
LETTERS = DATA / "letters" / "letters.json"
RESULTS = DATA / "results"


def load_letters(path: Path = LETTERS) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def run(llm: LLM, out: Path, langs: list[str], ids: list[str] | None = None,
        seed: int = 0, workers: int = 1) -> Path:
    """Explain every letter in every reader language; append one JSON line per reply.
    Already-recorded (letter, language) pairs are skipped, so an interrupted run resumes."""
    letters = [l for l in load_letters() if not ids or l["id"] in ids]
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done.add((r["letter_id"], r["target_lang"]))
    jobs = [(l, lang) for lang in langs for l in letters if (l["id"], lang) not in done]
    total, lock, counter = len(jobs), threading.Lock(), [0]

    def _one(job: tuple[dict, str]) -> None:
        letter, lang = job
        t0 = time.monotonic()
        reply = llm.chat(build_messages(letter["text"], lang), schema=OUTPUT_SCHEMA, seed=seed)
        rec = {
            "letter_id": letter["id"], "target_lang": lang, "model": llm.s.model,
            "endpoint": llm.s.base_url, "seed": seed, "temperature": 0.0,
            "raw_text": reply.text, "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens, "seconds": reply.seconds,
            "output_mode": reply.output_mode, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with lock:
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            counter[0] += 1
            print(f"[{counter[0]}/{total}] {letter['id']} -> {lang}: {reply.completion_tokens} tokens, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for f in [pool.submit(_one, j) for j in jobs]:
            f.result()
    return out


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": round(p, 3), "recall": round(r, 3), "tp": tp, "fp": fp, "fn": fn}


def score_records(records: list[dict]) -> dict:
    letters = {l["id"]: l for l in load_letters()}
    c = Counter()
    kinds = Counter()
    reasons = Counter()
    per_letter = []
    for rec in records:
        letter = letters[rec["letter_id"]]
        gold_d = set(letter["gold"]["deadlines"])
        gold_a = set(letter["gold"]["amounts"])
        letter_dates = {d.isoformat() for d in parse.find_dates(letter["text"])}
        expl, ok = from_model_text(rec["raw_text"], letter["text"], rec["target_lang"])
        c["replies"] += 1
        c["json_ok"] += ok
        if not ok:
            c["fn_d_raw"] += len(gold_d); c["fn_d_shown"] += len(gold_d)
            c["fn_a_raw"] += len(gold_a); c["fn_a_shown"] += len(gold_a)
            per_letter.append({"letter_id": rec["letter_id"], "target_lang": rec["target_lang"], "json_ok": False})
            continue
        pts = expl.points
        c["points"] += len(pts)
        c["points_shown"] += len(expl.shown)
        for p in pts:
            kinds[(p.kind, p.verified)] += 1
            if p.match:
                c[f"match_{p.match}"] += 1
            if not p.verified:
                reasons[p.reason] += 1
        for stage, chosen in (("raw", pts), ("shown", expl.shown)):
            dates = {p.date for p in chosen if p.kind == "deadline" and p.date}
            chf = {p.chf for p in chosen if p.kind == "amount" and p.chf is not None}
            c[f"tp_d_{stage}"] += len(dates & gold_d)
            c[f"fp_d_{stage}"] += len(dates - gold_d)
            c[f"fn_d_{stage}"] += len(gold_d - dates)
            c[f"tp_a_{stage}"] += len(chf & gold_a)
            c[f"fp_a_{stage}"] += len(chf - gold_a)
            c[f"fn_a_{stage}"] += len(gold_a - chf)
            wrong = dates - gold_d
            c[f"wrong_date_in_letter_{stage}"] += len(wrong & letter_dates)
            c[f"wrong_date_not_in_letter_{stage}"] += len(wrong - letter_dates)
            if not gold_d and dates:
                c[f"invented_deadline_letters_{stage}"] += 1
        # A gold amount is "surfaced" if the reader sees it in any shown point's letter words.
        seen_amounts = set()
        for p in expl.shown:
            seen_amounts |= set(parse.find_amounts(p.evidence or ""))
        c["amounts_surfaced"] += len(gold_a & seen_amounts)
        c["amounts_gold"] += len(gold_a)
        if not gold_d:
            c["no_deadline_letters"] += 1
        per_letter.append({
            "letter_id": rec["letter_id"], "target_lang": rec["target_lang"], "json_ok": True,
            "shown_deadlines": sorted(p.date for p in expl.shown if p.kind == "deadline" and p.date),
            "shown_amounts": sorted(p.chf for p in expl.shown if p.kind == "amount" and p.chf is not None),
            "gold_deadlines": sorted(gold_d), "gold_amounts": sorted(gold_a),
            "unverified": [{"kind": p.kind, "reason": p.reason, "quote": p.quote} for p in expl.unverified],
        })
        c["prompt_tokens"] += rec.get("prompt_tokens", 0)
        c["completion_tokens"] += rec.get("completion_tokens", 0)
        c["seconds"] += rec.get("seconds", 0)

    n = c["replies"] or 1
    summary = {
        "replies": c["replies"],
        "json_valid": round(c["json_ok"] / n, 3),
        "points": c["points"],
        "grounded_share": round(c["points_shown"] / c["points"], 3) if c["points"] else None,
        "exact_matches": c["match_exact"], "near_matches": c["match_near"], "partial_matches": c["match_partial"],
        "deadlines_raw": _prf(c["tp_d_raw"], c["fp_d_raw"], c["fn_d_raw"]),
        "deadlines_shown": _prf(c["tp_d_shown"], c["fp_d_shown"], c["fn_d_shown"]),
        "amounts_raw": _prf(c["tp_a_raw"], c["fp_a_raw"], c["fn_a_raw"]),
        "amounts_shown": _prf(c["tp_a_shown"], c["fp_a_shown"], c["fn_a_shown"]),
        "amounts_surfaced": f"{c['amounts_surfaced']}/{c['amounts_gold']}",
        "wrong_dates_raw": {"misread_from_letter": c["wrong_date_in_letter_raw"],
                            "not_in_letter": c["wrong_date_not_in_letter_raw"]},
        "wrong_dates_shown": {"misread_from_letter": c["wrong_date_in_letter_shown"],
                              "not_in_letter": c["wrong_date_not_in_letter_shown"]},
        "invented_deadline_on_no_deadline_letters": {
            "raw": c["invented_deadline_letters_raw"], "shown": c["invented_deadline_letters_shown"],
            "of": c["no_deadline_letters"]},
        "unverified_reasons": dict(reasons.most_common()),
        "mean_prompt_tokens": round(c["prompt_tokens"] / n),
        "mean_completion_tokens": round(c["completion_tokens"] / n),
        "mean_seconds": round(c["seconds"] / n, 1),
    }
    return {"summary": summary, "per_letter": per_letter,
            "points_by_kind": {f"{k}:{'shown' if v else 'unverified'}": n_ for (k, v), n_ in sorted(kinds.items())}}


def consistency(records: list[dict]) -> dict:
    """Do two reader languages get the same facts? Compares shown deadlines and amounts per letter."""
    letters = {l["id"]: l for l in load_letters()}
    by_letter: dict[str, dict[str, tuple]] = {}
    for rec in records:
        expl, ok = from_model_text(rec["raw_text"], letters[rec["letter_id"]]["text"], rec["target_lang"])
        if not ok:
            continue
        facts = (tuple(sorted(p.date for p in expl.shown if p.kind == "deadline" and p.date)),
                 tuple(sorted(p.chf for p in expl.shown if p.kind == "amount" and p.chf is not None)))
        by_letter.setdefault(rec["letter_id"], {})[rec["target_lang"]] = facts
    pairs = [v for v in by_letter.values() if len(v) >= 2]
    same_d = sum(len({f[0] for f in v.values()}) == 1 for v in pairs)
    same_a = sum(len({f[1] for f in v.values()}) == 1 for v in pairs)
    return {"letters_compared": len(pairs), "same_deadlines": same_d, "same_amounts": same_a}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
