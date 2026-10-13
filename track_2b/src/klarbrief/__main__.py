"""Command line: ``python -m klarbrief <command>``.

  explain FILE [--lang en]     explain one letter (text file) and print what is shown
  serve [--port 8000]          start the web UI
  eval [--langs en,uk]         run the labelled letters through the configured model
  score [FILE ...]             re-score recorded replies offline (no model needed)
  run                          what `make run` does: score, a live check if a model is set, then serve
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import evaluate as E
from .explain import LANGUAGES, explain
from .llm import LLM, LLMError, Settings

KIND_LABEL = {"sender": "From", "topic": "About", "action": "What to do", "deadline": "By when",
              "amount": "How much", "consequence": "If you don't", "contact": "Contact"}


def _print_explanation(d: dict) -> None:
    e = d.get("explanation")
    if not e:
        print(f"  ! no explanation: {d.get('error')}")
        return
    print(f"  Summary: {e['summary']}")
    for p in e["shown"]:
        extra = f" [{p['date']}]" if p.get("date") else (f" [CHF {p['chf']:.2f}]" if "chf" in p else "")
        print(f"  ✓ {KIND_LABEL.get(p['kind'], p['kind'])}{extra}: {p['text']}")
        print(f"      letter says: “{p['evidence']}” ({p['match']})")
    for p in e["unverified"]:
        print(f"  ✗ not shown ({p['reason']}): {p['kind']}: {p['text']}")


def _scores_table(paths: list[Path]) -> str:
    rows = ["| Model · reader languages | Replies | JSON valid | Points grounded | Deadlines shown (P / R) "
            "| Amounts shown (P / R) | Invented deadlines raw → shown | Wrong dates raw → shown | s / letter |",
            "|---|---|---|---|---|---|---|---|---|"]
    for path in paths:
        recs = E.read_jsonl(path)
        if not recs:
            continue
        s = E.score_records(recs)["summary"]
        langs = ",".join(sorted({r["target_lang"] for r in recs}))
        inv = s["invented_deadline_on_no_deadline_letters"]
        wr, ws = s["wrong_dates_raw"], s["wrong_dates_shown"]
        rows.append(
            f"| {recs[0]['model']} · {langs} | {s['replies']} | {s['json_valid']:.0%} | {s['grounded_share']:.0%} "
            f"| {s['deadlines_shown']['precision']:.2f} / {s['deadlines_shown']['recall']:.2f} "
            f"| {s['amounts_shown']['precision']:.2f} / {s['amounts_shown']['recall']:.2f} "
            f"| {inv['raw']} → {inv['shown']} of {inv['of']} "
            f"| {wr['misread_from_letter'] + wr['not_in_letter']} → {ws['misread_from_letter'] + ws['not_in_letter']} "
            f"| {s['mean_seconds']} |")
    return "\n".join(rows)


def cmd_score(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files] or sorted(E.RESULTS.glob("*.jsonl"))
    if not paths:
        print("no recorded results in data/results/")
        return 1
    print(_scores_table(paths))
    out = {}
    for p in paths:
        recs = E.read_jsonl(p)
        out[p.name] = {**E.score_records(recs), "consistency": E.consistency(recs)}
    if args.write:
        target = E.RESULTS / "scores.json"
        target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {target}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    s = Settings.from_env()
    letter = Path(args.file).read_text(encoding="utf-8")
    res = explain(letter, args.lang, LLM(s))
    d = res.to_dict()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(f"Model: {s.describe()} · {d['seconds']}s")
        _print_explanation(d)
    return 0 if res.json_ok else 2


def cmd_eval(args: argparse.Namespace) -> int:
    s = Settings.from_env()
    llm = LLM(s)
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", s.model).strip("-")
    out = Path(args.out) if args.out else E.RESULTS / f"{name}.jsonl"
    print(f"Evaluating {s.describe()} → {out}")
    E.run(llm, out, args.langs.split(","), ids=args.ids.split(",") if args.ids else None,
          workers=args.workers)
    print(_scores_table([out]))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """make run: show the recorded numbers, check the live model on three letters, serve the UI."""
    s = Settings.from_env()
    print("=" * 78)
    print("Klarbrief · official letters explained in your language, every point checked")
    print("=" * 78)
    print("\n1) Recorded evaluation, re-scored now from data/results (no model needed):\n")
    cmd_score(argparse.Namespace(files=[], write=False))
    if s.configured:
        print(f"\n2) Live check on {s.describe()}:\n")
        letters = {l["id"]: l for l in E.load_letters()}
        llm = LLM(s)
        for lid, lang in (("de-01", "en"), ("fr-01", "pt"), ("it-03", "sq")):
            t0 = time.monotonic()
            try:
                res = explain(letters[lid]["text"], lang, llm)
            except LLMError as e:
                print(f"  ! {lid}: endpoint error: {e}")
                continue
            print(f"- {lid} ({letters[lid]['kind']}) explained in {LANGUAGES[lang]}, "
                  f"{time.monotonic() - t0:.0f}s:")
            _print_explanation(res.to_dict())
            print()
    else:
        print("\n2) Live check skipped: no LLM endpoint configured.\n"
              "   Set LLM_BASE_URL (+ LLM_API_KEY, LLM_NAME), or run `make onprem` for the local\n"
              "   llama.cpp container. The UI below runs in REPLAY mode: sample letters only,\n"
              "   showing recorded replies labelled with the model that produced them.")
    if args.no_serve:
        return 0
    print("\n3) Web UI")
    from .server import serve
    serve(port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="klarbrief", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("explain"); p.add_argument("file"); p.add_argument("--lang", default="en")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_explain)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=lambda a: __import__("klarbrief.server", fromlist=["serve"]).serve(port=a.port) or 0)
    p = sub.add_parser("eval"); p.add_argument("--langs", default="en"); p.add_argument("--ids")
    p.add_argument("--out"); p.add_argument("--workers", type=int, default=4); p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("score"); p.add_argument("files", nargs="*")
    p.add_argument("--write", action="store_true"); p.set_defaults(fn=cmd_score)
    p = sub.add_parser("run"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-serve", action="store_true"); p.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except LLMError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
