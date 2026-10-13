"""Make the report figure and the numbers table from data/results (development tool).

Needs matplotlib. Usage: python docs/make_figures.py  -> docs/results.png, docs/results.md
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from klarbrief import evaluate as E  # noqa: E402

MAIN = sorted(p for p in E.RESULTS.glob("*.jsonl") if not p.name.startswith("ablation-"))
ABLATION = sorted(E.RESULTS.glob("ablation-*.jsonl"))

BLUE, ORANGE = "#2a78d6", "#eb6834"          # validated categorical slots 1 and 2
INK, MUTED, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"


def facts(summary: dict) -> dict:
    d_raw, d_shown = summary["deadlines_raw"], summary["deadlines_shown"]
    a_raw, a_shown = summary["amounts_raw"], summary["amounts_shown"]
    return {
        "wrong_deadlines": (d_raw["fp"], d_shown["fp"]),
        "wrong_amounts": (a_raw["fp"], a_shown["fp"]),
        "right_deadlines": (d_raw["tp"], d_shown["tp"], d_raw["tp"] + d_raw["fn"]),
        "right_amounts": (a_raw["tp"], a_shown["tp"], a_raw["tp"] + a_raw["fn"]),
    }


def figure(summary: dict, out: Path) -> None:
    f = facts(summary)
    cats = ["Wrong deadline\ndates", "Wrong CHF\namounts", "Correct\ndeadlines", "Correct CHF\namounts"]
    raw = [f["wrong_deadlines"][0], f["wrong_amounts"][0], f["right_deadlines"][0], f["right_amounts"][0]]
    shown = [f["wrong_deadlines"][1], f["wrong_amounts"][1], f["right_deadlines"][1], f["right_amounts"][1]]
    fig, ax = plt.subplots(figsize=(7.2, 2.9), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    x = range(len(cats))
    w = 0.36
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], raw, w, color=BLUE, label="Apertus output shown as is")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], shown, w, color=ORANGE, label="Shown by Klarbrief (verified)")
    for bars in (b1, b2):
        for r in bars:
            ax.annotate(f"{int(r.get_height())}", (r.get_x() + r.get_width() / 2, r.get_height()),
                        xytext=(0, 2), textcoords="offset points", ha="center", va="bottom",
                        fontsize=8, color=INK)
    ax.set_ylim(0, max(raw + shown) * 1.28 + 0.5)
    ax.axvline(1.5, color="#e3ded6", lw=1)
    ax.text(0.5, 0.97, "should be 0", transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8, color=MUTED)
    ax.text(2.5, 0.97, f"of {f['right_deadlines'][2]} deadlines · {f['right_amounts'][2]} amounts in the labels",
            transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=8, color=MUTED)
    ax.set_xticks(list(x), cats, fontsize=8.5, color=INK)
    ax.tick_params(axis="y", labelsize=8, colors=MUTED, length=0)
    ax.tick_params(axis="x", length=0)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color("#cfcac2")
    ax.yaxis.grid(True, color="#ece8e1", lw=0.6)
    ax.set_axisbelow(True)
    ax.set_ylabel("count over all replies", fontsize=8, color=MUTED)
    ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0, 1.16), ncol=2)
    fig.tight_layout()
    fig.savefig(out, facecolor=SURFACE)
    print(f"wrote {out}")


def table(paths: list[Path], label) -> list[str]:
    rows = []
    for p in paths:
        recs = E.read_jsonl(p)
        s = E.score_records(recs)["summary"]
        c = E.consistency(recs)
        langs = "+".join(sorted({r["target_lang"] for r in recs}))
        mode = sorted({r.get("output_mode") or ("json_object" if r.get("json_mode") else "text") for r in recs})
        inv = s["invented_deadline_on_no_deadline_letters"]
        rows.append({"label": label(p), "langs": langs, "mode": ",".join(mode), "s": s, "c": c})
    return rows


def main() -> None:
    if not MAIN:
        sys.exit("no results")
    main_recs = [r for p in MAIN for r in E.read_jsonl(p)]
    summary = E.score_records(main_recs)["summary"]
    figure(summary, ROOT / "docs" / "results.png")
    out = {"main": summary, "consistency": E.consistency(main_recs),
           "ablation": {p.name: E.score_records(E.read_jsonl(p))["summary"] for p in ABLATION},
           "by_language": {}}
    for lang in sorted({r["target_lang"] for r in main_recs}):
        out["by_language"][lang] = E.score_records([r for r in main_recs if r["target_lang"] == lang])["summary"]
    for lang_letter in ("de", "fr", "it"):
        out["by_language"]["letters_" + lang_letter] = E.score_records(
            [r for r in main_recs if r["letter_id"].startswith(lang_letter)])["summary"]
    (ROOT / "docs" / "results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(json.dumps({k: out[k] for k in ("main", "consistency")}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
