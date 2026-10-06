"""Minimal DataForSEO SERP client (Standard queue: task_post -> tasks_ready -> task_get)."""
from __future__ import annotations

import base64
import gzip
import json
import time
import urllib.error
import urllib.request

BASE = "https://api.dataforseo.com/v3"
OK = 20000
TASK_CREATED = 20100
IN_QUEUE = {40601, 40602}   # "task handed" / "task in queue" -> not ready yet


class DataForSEOError(RuntimeError):
    pass


class DataForSEO:
    def __init__(self, login: str, password: str, engine: str = "google", timeout: int = 60):
        if not login or not password:
            raise DataForSEOError(
                "Missing credentials. Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD "
                "(env vars or a .env file). Use the API password from the DataForSEO "
                "dashboard (app.dataforseo.com/api-access), not your account password.")
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json",
                        "Accept-Encoding": "gzip"}
        self.engine = engine
        self.timeout = timeout

    def _request(self, method: str, path: str, payload=None, retries: int = 4) -> dict:
        url = f"{BASE}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=self.headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 401:
                    raise DataForSEOError("DataForSEO rejected the login/password (HTTP 401).") from e
                raise DataForSEOError(f"HTTP {e.code} from {path}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise DataForSEOError("unreachable")

    # -- endpoints ----------------------------------------------------------
    def post_tasks(self, tasks: list[dict]) -> list[dict]:
        """Submit up to 100 keyword tasks. Returns the per-task response objects (same order)."""
        assert len(tasks) <= 100
        resp = self._request("POST", f"/serp/{self.engine}/organic/task_post", tasks)
        if resp.get("status_code") != OK:
            raise DataForSEOError(f"task_post failed: {resp.get('status_message')}")
        return resp["tasks"]

    def ready_ids(self) -> set[str]:
        resp = self._request("GET", f"/serp/{self.engine}/organic/tasks_ready")
        ids: set[str] = set()
        for t in resp.get("tasks") or []:
            for r in t.get("result") or []:
                ids.add(r["id"])
        return ids

    def get_task(self, task_id: str) -> dict:
        """Returns the task object; caller checks status_code (IN_QUEUE => retry later)."""
        resp = self._request("GET", f"/serp/{self.engine}/organic/task_get/advanced/{task_id}")
        return (resp.get("tasks") or [{}])[0]

    def live(self, task: dict) -> dict:
        """Instant SERP for one keyword (billed higher than the queue). Returns the task object."""
        resp = self._request("POST", f"/serp/{self.engine}/organic/live/advanced", [task])
        return (resp.get("tasks") or [{}])[0]
