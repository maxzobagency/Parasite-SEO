from __future__ import annotations

import csv
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .detector import DEFAULT_ENABLED


@dataclass
class Config:
    location_code: int = 2840          # 2840 = United States
    language_code: str = "en"
    device: str = "desktop"            # desktop | mobile
    depth: int = 20                    # results per keyword (billed per 10)
    priority: int = 1                  # 1 = normal (cheapest), 2 = high (2x cost, faster)
    top_n: int = 10                    # "visible" threshold used in the report
    enabled_categories: list[str] = field(default_factory=lambda: list(DEFAULT_ENABLED))
    db: str = "parasite.db"
    keywords: str = "keywords.csv"
    report: str = "report.html"
    login: str = ""
    password: str = ""


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_config(path: str | Path = "config.toml") -> Config:
    _load_dotenv()
    cfg = Config()
    p = Path(path)
    if p.exists():
        data = tomllib.loads(p.read_text())
        for section in ("serp", "detection", "paths"):
            for k, v in data.get(section, {}).items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
    cfg.login = os.environ.get("DATAFORSEO_LOGIN", "")
    cfg.password = os.environ.get("DATAFORSEO_PASSWORD", "")
    return cfg


def read_keywords(path: str | Path, cfg: Config) -> list[dict]:
    """keywords.csv columns: keyword (required), niche, location_code, language_code, device."""
    rows, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            kw = r.get("keyword", "")
            if not kw:
                continue
            row = {
                "keyword": kw,
                "niche": r.get("niche", ""),
                "location_code": int(r.get("location_code") or cfg.location_code),
                "language_code": r.get("language_code") or cfg.language_code,
                "device": r.get("device") or cfg.device,
            }
            key = (kw.lower(), row["location_code"], row["language_code"], row["device"])
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows
