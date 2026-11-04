"""Render the README images headless with Playwright (no browser window opens).

  python scripts/render_media.py      (needs `pip install playwright` and `playwright install chromium`)

1. media/demo-terminal.png   output of scripts/demo_local.sh, saved in docs/demo-local-2026-11-04.txt
2. media/n8n-live-run.png    the n8n workflow run, saved in docs/n8n-live-run-2026-11-04.txt
3. media/order-email.png     the order_confirmation template rendered with samples/order.json
"""
import html
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from resend_notifier.templates import TemplateRenderer  # noqa: E402

MEDIA, DOCS = ROOT / "media", ROOT / "docs"


def terminal(title: str, text: str) -> str:
    return f"""<html><body style="margin:0;background:#fff"><div id="wrap" style="display:inline-block;padding:24px">
<div style="background:#1e1e1e;border-radius:10px;padding:18px 22px;width:1150px">
<div style="color:#9cdcfe;font:13px Menlo,monospace;margin-bottom:10px">$ {html.escape(title)}</div>
<pre style="margin:0;color:#e6e6e6;font:12.5px/1.5 Menlo,monospace;white-space:pre-wrap">{html.escape(text)}</pre></div></div></body></html>"""


def main():
    MEDIA.mkdir(exist_ok=True)
    demo = (DOCS / "demo-local-2026-11-04.txt").read_text().split("== What the fake Resend API received")[0].strip()
    n8n = (DOCS / "n8n-live-run-2026-11-04.txt").read_text().split("What the fake Resend API received")[0].strip()
    email = TemplateRenderer(ROOT / "templates").render("order_confirmation", json.loads((ROOT / "samples" / "order.json").read_text()))
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(device_scale_factor=2)
        page.set_content(terminal("./scripts/demo_local.sh", demo))
        page.locator("#wrap").screenshot(path=str(MEDIA / "demo-terminal.png"))
        page.set_content(terminal("curl ... against the two workflows on self-hosted n8n 2.45.7", n8n))
        page.locator("#wrap").screenshot(path=str(MEDIA / "n8n-live-run.png"))
        page.set_viewport_size({"width": 720, "height": 600})
        page.set_content(f'<html><body style="margin:0;background:#f3f4f6"><div id="wrap" style="padding:24px">'
                         f'<div style="font:13px sans-serif;color:#555;margin-bottom:8px">Subject: {html.escape(email.subject)}</div>'
                         f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:8px">{email.html}</div></div></body></html>')
        page.locator("#wrap").screenshot(path=str(MEDIA / "order-email.png"))
        b.close()
    print("wrote", ", ".join(sorted(x.name for x in MEDIA.glob("*.png"))))


if __name__ == "__main__":
    sys.exit(main())
