"""Build Klarbrief_Report.pdf from technical_report.md (development tool, not needed at runtime).

Needs: pip install markdown, and Node with Playwright + Chromium (preinstalled in many CI
images). Usage: python docs/build_report.py [chromium_path]
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[1]
MD = ROOT / "technical_report.md"
PDF = ROOT / "Klarbrief_Report.pdf"
CHROMIUM = sys.argv[1] if len(sys.argv) > 1 else "/opt/pw-browsers/chromium"

CSS = """
@page { size: A4; margin: 15mm 16mm 15mm 16mm; }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 9.6pt; line-height: 1.38; color: #1d1d1f; }
h1 { font-size: 17pt; margin: 0 0 4pt; letter-spacing: -0.3px; }
h2 { font-size: 12pt; margin: 11pt 0 4pt; color: #c8261b; border-bottom: 1px solid #eee; padding-bottom: 2pt; }
h3 { font-size: 10.5pt; margin: 8pt 0 3pt; }
p, li { margin: 3pt 0; }
ul { padding-left: 16pt; margin: 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0; font-size: 8.4pt; }
th, td { border: 1px solid #ddd; padding: 3pt 5pt; vertical-align: top; text-align: left; }
th { background: #f4f1ec; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.3pt; background: #f6f6f6; padding: 0 2px; }
pre { background: #f6f6f6; padding: 6pt; font-size: 8pt; white-space: pre-wrap; }
img { max-width: 100%; }
.fig { text-align: center; margin: 6pt 0; }
.fig img { max-height: 210pt; }
.caption { font-size: 8.2pt; color: #555; text-align: center; margin-top: 2pt; }
blockquote { margin: 4pt 0; padding: 2pt 8pt; border-left: 3px solid #c8261b; color: #333; }
a { color: #1d1d1f; }
.pb { page-break-before: always; }
"""

NODE = r"""
import { chromium } from '%(pw)s';
const b = await chromium.launch({ executablePath: '%(chromium)s', args: ['--no-sandbox'] });
const p = await (await b.newContext()).newPage();
await p.goto('file://%(html)s', { waitUntil: 'networkidle' });
await p.pdf({ path: '%(pdf)s', format: 'A4', printBackground: true, preferCSSPageSize: true,
  displayHeaderFooter: true, headerTemplate: '<span></span>',
  footerTemplate: '<div style="font-size:7pt;width:100%%;text-align:center;color:#888">Klarbrief · Hack Apertus 2026 · Track 2B · <span class="pageNumber"></span>/<span class="totalPages"></span></div>' });
await b.close();
"""


def main() -> None:
    text = MD.read_text(encoding="utf-8")
    # ![caption](path) on its own line -> centred figure with caption
    text = re.sub(r"^!\[(.+?)\]\((.+?)\)$",
                  lambda m: f'<div class="fig"><img src="{m.group(2)}"><div class="caption">{m.group(1)}</div></div>',
                  text, flags=re.M)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "md_in_html"])
    html = f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{body}</body></html>'
    with tempfile.TemporaryDirectory() as tmp:
        page = ROOT / "_report.html"
        page.write_text(html, encoding="utf-8")
        script = Path(tmp) / "pdf.mjs"
        script.write_text(NODE % {"pw": "/opt/node-tools/node_modules/playwright/index.mjs",
                                  "chromium": CHROMIUM, "html": page, "pdf": PDF})
        try:
            subprocess.run(["node", str(script)], check=True)
        finally:
            page.unlink(missing_ok=True)
    data = PDF.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    print(f"wrote {PDF} ({pages} pages)")


if __name__ == "__main__":
    main()
