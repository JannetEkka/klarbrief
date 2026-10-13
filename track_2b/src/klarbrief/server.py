"""Klarbrief web UI: one page, standard library HTTP server.

When no LLM endpoint is configured the server runs in REPLAY mode: for the bundled sample
letters it returns the replies recorded in ``data/results`` (labelled as replayed, with the
model that produced them), and it refuses other letters instead of pretending.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import evaluate as E
from .explain import LANGUAGES, explain, from_model_text
from .llm import LLM, LLMError, Settings

MAX_LETTER_CHARS = 12000


class Recorded:
    """Recorded model replies for the sample letters, used in replay mode."""

    def __init__(self, results_dir: Path = E.RESULTS):
        self.by_key: dict[tuple[str, str], dict] = {}
        for path in sorted(results_dir.glob("*.jsonl")):
            if path.name.startswith("ablation-"):   # comparison runs, not what the app ships with
                continue
            for rec in E.read_jsonl(path):
                self.by_key.setdefault((rec["letter_id"], rec["target_lang"]), rec)

    def get(self, letter_id: str, lang: str) -> dict | None:
        return self.by_key.get((letter_id, lang))


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = LLM(settings) if settings.configured else None
        self.letters = E.load_letters()
        self.by_text = {l["text"].strip(): l for l in self.letters}
        self.recorded = Recorded()

    def config(self) -> dict:
        return {"mode": "live" if self.llm else "replay", "model": self.settings.describe(),
                "languages": LANGUAGES,
                "replay_languages": sorted({k[1] for k in self.recorded.by_key})}

    def explain(self, letter: str, lang: str) -> tuple[int, dict]:
        letter = letter.strip()
        if not letter:
            return 400, {"error": "Paste a letter first."}
        if len(letter) > MAX_LETTER_CHARS:
            return 400, {"error": f"Letter is longer than {MAX_LETTER_CHARS} characters."}
        if self.llm:
            try:
                res = explain(letter, lang, self.llm)
            except LLMError as e:
                return 502, {"error": f"Apertus endpoint error: {e}"}
            body = res.to_dict()
            body.update(mode="live", model=self.settings.model)
            return 200, body
        sample = self.by_text.get(letter)
        rec = self.recorded.get(sample["id"], lang) if sample else None
        if not rec:
            return 503, {"error": "Replay mode (no LLM endpoint configured): only the sample "
                                  "letters in the recorded languages can be shown. Set "
                                  "LLM_BASE_URL to explain any letter."}
        expl, ok = from_model_text(rec["raw_text"], letter, lang)
        return 200, {"mode": "replay", "model": rec["model"], "recorded_at": rec.get("recorded_at"),
                     "json_ok": ok, "explanation": expl.to_dict() if expl else None,
                     "raw_text": rec["raw_text"], "seconds": rec.get("seconds"),
                     "prompt_tokens": rec.get("prompt_tokens"),
                     "completion_tokens": rec.get("completion_tokens")}


def make_handler(app: App):
    page = resources.files("klarbrief.static").joinpath("index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Klarbrief/0.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict | list) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/config":
                self._json(200, app.config())
            elif self.path == "/api/letters":
                self._json(200, [{k: l[k] for k in ("id", "lang", "kind", "text")} for l in app.letters])
            elif self.path == "/healthz":
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/explain":
                self._json(404, {"error": "not found"})
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 200_000)
                req = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "bad request"})
                return
            code, body = app.explain(str(req.get("letter", "")), str(req.get("lang", "en")))
            self._json(code, body)

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("[klarbrief] " + fmt % args + "\n")

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    app = App(Settings.from_env())
    print(f"[klarbrief] mode={app.config()['mode']} · {app.settings.describe()}", flush=True)
    print(f"[klarbrief] open http://localhost:{port}", flush=True)
    ThreadingHTTPServer((host, port), make_handler(app)).serve_forever()
