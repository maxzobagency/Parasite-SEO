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
        self.failed: list[tuple[int, dict]] = []
        self.det = None
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


def _is_auth_error(ex: Exception) -> bool:
    return "HTTP 401" in str(ex) or "rejected the login" in str(ex)


def _retryable(code) -> bool:
    # 40101 = "Internal SE Server Error" (Google side, temporary); 5xxxx = DataForSEO side
    return code == 40101 or (isinstance(code, int) and code >= 50000)


def _fetch(api: DataForSEO, cfg: Config, k: dict, attempts: int = 3) -> dict:
    """One live SERP with automatic retries for temporary errors."""
    last = ""
    for n in range(attempts):
        try:
            task = api.live({"keyword": k["keyword"], "location_code": k["location_code"],
                             "language_code": k["language_code"], "device": k["device"],
                             "depth": cfg.depth})
        except DataForSEOError as ex:
            if _is_auth_error(ex):
                raise
            last = str(ex)
        else:
            code = task.get("status_code")
            if code == OK:
                return task
            last = f"{code} {task.get('status_message')}"
            if not _retryable(code):
                raise DataForSEOError(last)
        time.sleep(1.5 * (n + 1))
    raise DataForSEOError(f"{last} (gave up after {attempts} tries)")


def _run(job: LiveJob, api: DataForSEO, items: list[tuple[int, dict]]) -> None:
    def one(item):
        kid, k = item
        if job.fatal:
            return
        try:
            task = _fetch(api, job.cfg, k)
            res = (task.get("result") or [{}])[0].get("items") or []
            with job.lock:
                tracker.store_items(job.conn, job.det, 1, kid, res)
                job.conn.commit()
                job.cost += task.get("cost") or 0
        except Exception as ex:  # noqa: BLE001
            with job.lock:
                if isinstance(ex, DataForSEOError) and _is_auth_error(ex):
                    job.fatal = ("DataForSEO rejected the login/API password. Check them at "
                                 "app.dataforseo.com/api-access (use the API password).")
                job.failed.append((kid, k))
                job.errors.append(f"{k['keyword']}: {ex}")
        finally:
            with job.lock:
                job.done += 1

    def work():
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(one, items))
        with job.lock:
            job.finished = True

    threading.Thread(target=work, daemon=True).start()


def start(keywords: list[dict], cfg: Config, api: DataForSEO, overrides: dict[str, str]) -> LiveJob:
    job = LiveJob(keywords, cfg)
    job.conn.executemany("INSERT OR REPLACE INTO overrides VALUES(?,?)", list(overrides.items()))
    job.conn.commit()
    job.det = Detector(cfg.enabled_categories, overrides)
    with _LOCK:
        for jid in [j for j, v in _JOBS.items() if time.time() - v.created > 3600]:
            del _JOBS[jid]
        while len(_JOBS) >= 5:
            del _JOBS[min(_JOBS, key=lambda j: _JOBS[j].created)]
        _JOBS[job.id] = job
    _run(job, api, list(job.kw_ids.items()))
    return job


def retry(job: LiveJob, api: DataForSEO) -> int:
    """Re-run only the keywords that failed. Returns how many were re-queued."""
    with job.lock:
        if not job.finished or not job.failed:
            return 0
        items, job.failed = job.failed, []
        job.errors, job.fatal, job.finished = [], "", False
        job.done -= len(items)
    _run(job, api, items)
    return len(items)


def get(job_id: str) -> LiveJob | None:
    return _JOBS.get(job_id)


def apply_override(job: LiveJob, domain: str, kind: str) -> None:
    """User confirmed/dismissed a domain: re-classify this job's results straight away."""
    with job.lock:
        job.conn.execute("INSERT OR REPLACE INTO overrides VALUES(?,?)", (domain, kind))
        job.conn.commit()
        tracker.reclassify(job.conn, job.cfg)
