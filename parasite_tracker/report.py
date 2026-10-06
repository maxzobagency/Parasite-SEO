"""Self-contained HTML dashboard (open the file in a browser; no server needed)."""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("report_template.html")


def render_html(data: dict) -> str:
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text().replace("__DATA__", blob)


def render(data: dict, out: str | Path) -> None:
    Path(out).write_text(render_html(data), encoding="utf-8")
