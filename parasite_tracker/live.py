"""Realtime search: results live only in memory for the lifetime of the job (nothing is saved)."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import db, tracker
from .config import Config
from .dataforseo import OK, DataForSEO, DataForSEOError
from .detector import Detector

MAX_KEYWORDS = 1000
WORKERS = 8
_JOBS: dict[str, "LiveJob"] = {}
_LOCK = threading.Lock()


def estimate_cost(n_keywords: int, depth: int) -> float:
    """Live mode ~ $0.002 per 10 results per keyword (check dataforseo.com/pricing)."""
    return n_keywords * -(-depth // 10) * 0.002


class LiveJob:
    def __init__(self, keywords: list[dict], cfg: Config):
        self.id = uuid.uuid4().hex[:12]
        self.created = time.time()
        self.cfg = cfg
        self.total = len(keywords)
        self.done = 0
        self.cost = 0.0
        self.errors: list[str] = []
        self.finished = False
        self.fatal = ""
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.conn.execute("INSERT INTO runs(id, depth, finished_at) VALUES(1, ?, datetime('now'))", (cfg.depth,))
        self.kw_ids = {}
        for k in keywords:
            cur = self.conn.execute(
                "INSERT INTO keywords(keyword, niche, location_code, language_code, device) VALUES(?,?,?,?,?)",
                (k["keyword"], k["niche"], k["location_code"], k["language_code"], k["device"]))
            self.kw_ids[cur.lastrowid] = k
        self.conn.commit()

    def snapshot(self) -> dict:
        with self.lock:
            return {"id": self.id, "total": self.total, "done": self.done, "finished": self.finished,
                    "cost": round(self.cost, 4), "errors": self.errors[:5], "n_errors": len(self.errors),
                    "fatal": self.fatal}


def start(keywords: list[dict], cfg: Config, api: DataForSEO, overrides: dict[str, str]) -> LiveJob:
    job = LiveJob(keywords, cfg)
    det = Detector(cfg.enabled_categories, overrides)
    with _LOCK:
        for jid in [j for j, v in _JOBS.items() if time.time() - v.created > 3600]:
            del _JOBS[jid]
        while len(_JOBS) >= 5:
            del _JOBS[min(_JOBS, key=lambda j: _JOBS[j].created)]
        _JOBS[job.id] = job

    def one(item):
        kid, k = item
        if job.fatal:
            return
        try:
            task = api.live({"keyword": k["keyword"], "location_code": k["location_code"],
                             "language_code": k["language_code"], "device": k["device"],
                             "depth": cfg.depth})
            code = task.get("status_code")
            if code != OK:
                raise DataForSEOError(f"{code} {task.get('status_message')}")
            items = ((task.get("result") or [{}])[0]).get("items") or []
            with job.lock:
                tracker.store_items(job.conn, det, 1, kid, items)
                job.conn.commit()
                job.cost += task.get("cost") or 0
        except DataForSEOError as ex:
            with job.lock:
                if "401" in str(ex) or "credentials" in str(ex).lower():
                    job.fatal = str(ex)
                job.errors.append(f"{k['keyword']}: {ex}")
        except Exception as ex:  # noqa: BLE001
            with job.lock:
                job.errors.append(f"{k['keyword']}: {ex}")
        finally:
            with job.lock:
                job.done += 1

    def work():
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(one, job.kw_ids.items()))
        with job.lock:
            job.finished = True

    threading.Thread(target=work, daemon=True).start()
    return job


def get(job_id: str) -> LiveJob | None:
    return _JOBS.get(job_id)
