"""Self-contained HTML dashboard (open the file in a browser; no server needed)."""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("report_template.html")


def render(data: dict, out: str | Path) -> None:
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(out).write_text(TEMPLATE.read_text().replace("__DATA__", blob), encoding="utf-8")
