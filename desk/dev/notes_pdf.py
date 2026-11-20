"""Print the trading notes to a PDF with headless Chromium.

    pip install playwright && playwright install chromium     # once
    python -m desk.dev.notes_pdf

Writes ``desk/static/notes/trading-notes.pdf``, which the desk serves at ``/notes.pdf``. The PDF is the same
page as ``/notes`` (same Markdown, same figures), printed in the light theme with one chapter per page.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from desk.server import NOTES_PDF, STATIC, notes_page

OUT = STATIC / "notes" / NOTES_PDF


def build(out: Path = OUT) -> Path:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    html = notes_page(for_print=True)
    with tempfile.TemporaryDirectory() as folder:
        # App screenshots are large PNGs; a JPEG at print width keeps the PDF small enough to download on a phone.
        def shrink(match):
            source = Path(match.group(1).removeprefix("file://"))
            target = Path(folder) / (source.stem + ".jpg")
            with Image.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((1400, 1400 * image.height // image.width))
                image.save(target, quality=82, optimize=True)
            return f'src="{target.as_uri()}"'

        html = re.sub(r'src="(file://[^"]*/manual/img/[^"]+)"', shrink, html)
        page_file = Path(folder) / "notes.html"
        page_file.write_text(html, encoding="utf-8")
        with sync_playwright() as pw:
            path = os.environ.get("NOTES_CHROMIUM")
            browser = pw.chromium.launch(**({"executable_path": path} if path else {}))
            page = browser.new_page()
            page.goto(page_file.as_uri(), wait_until="networkidle")
            footer = ('<div style="font-size:8px;color:#888;width:100%;padding:0 14mm;display:flex;'
                      'justify-content:space-between"><span>TradeSetu trading notes. Not investment advice.</span>'
                      '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
            page.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
                     header_template="<span></span>", footer_template=footer,
                     margin={"top": "14mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
            browser.close()
    return out


if __name__ == "__main__":
    print(f"Wrote {build()}")
