from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY,
  keyword TEXT NOT NULL,
  niche TEXT NOT NULL DEFAULT '',
  location_code INTEGER NOT NULL,
  language_code TEXT NOT NULL,
  device TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(keyword, location_code, language_code, device)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  depth INTEGER NOT NULL,
  cost REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  run_id INTEGER NOT NULL,
  keyword_id INTEGER NOT NULL,
  task_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
  error TEXT,
  PRIMARY KEY (run_id, keyword_id)
);
CREATE TABLE IF NOT EXISTS results (
  run_id INTEGER NOT NULL,
  keyword_id INTEGER NOT NULL,
  rank_group INTEGER,
  rank_absolute INTEGER,
  type TEXT,
  host TEXT,
  root TEXT,
  url TEXT,
  title TEXT,
  is_parasite INTEGER NOT NULL DEFAULT 0,
  platform TEXT,
  category TEXT,
  confidence TEXT,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id, keyword_id);
CREATE TABLE IF NOT EXISTS overrides (
  domain TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('parasite','ignore'))
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
