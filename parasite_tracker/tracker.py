"""Run orchestration: sync keywords, submit SERP tasks, collect + classify results."""
from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Config
from .dataforseo import IN_QUEUE, TASK_CREATED, OK, DataForSEO
from .detector import Detector, host_of, root_domain


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get_detector(conn: sqlite3.Connection, cfg: Config) -> Detector:
    ov = {r["domain"]: r["kind"] for r in conn.execute("SELECT domain, kind FROM overrides")}
    return Detector(cfg.enabled_categories, ov)


def sync_keywords(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert keywords.csv into the DB; keywords no longer in the CSV are deactivated."""
    conn.execute("UPDATE keywords SET active = 0")
    for r in rows:
        conn.execute(
            """INSERT INTO keywords(keyword, niche, location_code, language_code, device, active)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(keyword, location_code, language_code, device)
               DO UPDATE SET niche=excluded.niche, active=1""",
            (r["keyword"], r["niche"], r["location_code"], r["language_code"], r["device"]))
    conn.commit()
    return len(rows)


def add_keywords(conn: sqlite3.Connection, rows: list[dict], replace: bool = False) -> int:
    if replace:
        conn.execute("UPDATE keywords SET active = 0")
    for r in rows:
        conn.execute(
            """INSERT INTO keywords(keyword, niche, location_code, language_code, device, active)
               VALUES(?,?,?,?,?,1)
               ON CONFLICT(keyword, location_code, language_code, device)
               DO UPDATE SET niche=excluded.niche, active=1""",
            (r["keyword"], r["niche"], r["location_code"], r["language_code"], r["device"]))
    conn.commit()
    return len(rows)


def estimate_cost(n_keywords: int, depth: int, priority: int) -> float:
    """Rough USD estimate. Standard queue ~ $0.0006 per 10 results, high priority ~ 2x.
    Check dataforseo.com/pricing - rates change."""
    pages = -(-depth // 10)
    return n_keywords * pages * (0.0006 if priority == 1 else 0.0012)


def start_run(conn, cfg: Config) -> int:
    cur = conn.execute("INSERT INTO runs(depth) VALUES(?)", (cfg.depth,))
    run_id = cur.lastrowid
    conn.execute("INSERT INTO tasks(run_id, keyword_id) SELECT ?, id FROM keywords WHERE active=1",
                 (run_id,))
    conn.commit()
    return run_id


def latest_open_run(conn) -> int | None:
    """The newest run, if any of its keywords is still pending/errored (so it can be resumed)."""
    r = conn.execute("""SELECT id FROM runs ORDER BY id DESC LIMIT 1""").fetchone()
    if not r:
        return None
    left = conn.execute("SELECT COUNT(*) FROM tasks WHERE run_id=? AND status != 'done'", (r["id"],)).fetchone()[0]
    return r["id"] if left else None


def submit(conn, api: DataForSEO, cfg: Config, run_id: int) -> int:
    """Post every task of the run that has no task_id yet. Returns number submitted."""
    todo = conn.execute(
        """SELECT k.* FROM tasks t JOIN keywords k ON k.id = t.keyword_id
           WHERE t.run_id=? AND t.task_id IS NULL AND t.status != 'done'""", (run_id,)).fetchall()
    sent = 0
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        payload = [{"keyword": k["keyword"], "location_code": k["location_code"],
                    "language_code": k["language_code"], "device": k["device"],
                    "depth": cfg.depth, "priority": cfg.priority, "tag": str(k["id"])}
                   for k in batch]
        for k, t in zip(batch, api.post_tasks(payload)):
            if t.get("status_code") == TASK_CREATED:
                conn.execute("UPDATE tasks SET task_id=?, status='pending', error=NULL "
                             "WHERE run_id=? AND keyword_id=?", (t["id"], run_id, k["id"]))
                sent += 1
            else:
                conn.execute("UPDATE tasks SET status='error', error=? WHERE run_id=? AND keyword_id=?",
                             (f"{t.get('status_code')} {t.get('status_message')}", run_id, k["id"]))
        conn.commit()
        log(f"  submitted {min(i + 100, len(todo))}/{len(todo)}")
    return sent


def store_items(conn, det: Detector, run_id: int, keyword_id: int, items: list[dict]) -> None:
    conn.execute("DELETE FROM results WHERE run_id=? AND keyword_id=?", (run_id, keyword_id))
    for it in items:
        if it.get("type") != "organic" or not it.get("url"):
            continue
        host = host_of(it["url"])
        v = det.classify(it["url"], it.get("title") or "")
        conn.execute(
            """INSERT INTO results(run_id, keyword_id, rank_group, rank_absolute, type, host, root,
               url, title, is_parasite, platform, category, confidence, reason)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, keyword_id, it.get("rank_group"), it.get("rank_absolute"), "organic", host,
             root_domain(host), it["url"], it.get("title"), int(v.is_parasite), v.platform,
             v.category, v.confidence, v.reason))


def collect(conn, api: DataForSEO, cfg: Config, run_id: int, poll: int = 15,
            timeout_min: int = 45) -> bool:
    """Poll until all tasks of the run are fetched. True if every task finished."""
    det = get_detector(conn, cfg)
    deadline = time.time() + timeout_min * 60
    while True:
        pending = {r["task_id"]: r["keyword_id"] for r in conn.execute(
            "SELECT task_id, keyword_id FROM tasks WHERE run_id=? AND status='pending' "
            "AND task_id IS NOT NULL", (run_id,))}
        if not pending:
            break
        ready = [t for t in api.ready_ids() if t in pending]
        if ready:
            with ThreadPoolExecutor(max_workers=8) as ex:
                fetched = list(ex.map(lambda tid: (tid, api.get_task(tid)), ready))
            cost = 0.0
            for tid, task in fetched:
                kid, code = pending[tid], task.get("status_code")
                if code == OK:
                    res = (task.get("result") or [{}])[0]
                    store_items(conn, det, run_id, kid, res.get("items") or [])
                    conn.execute("UPDATE tasks SET status='done' WHERE run_id=? AND keyword_id=?",
                                 (run_id, kid))
                    cost += task.get("cost") or 0
                elif code in IN_QUEUE:
                    continue
                else:
                    conn.execute("UPDATE tasks SET status='error', error=? WHERE run_id=? AND keyword_id=?",
                                 (f"{code} {task.get('status_message')}", run_id, kid))
            conn.execute("UPDATE runs SET cost = cost + ? WHERE id=?", (cost, run_id))
            conn.commit()
        done, total = conn.execute(
            "SELECT SUM(status='done'), COUNT(*) FROM tasks WHERE run_id=?", (run_id,)).fetchone()
        log(f"  collected {done or 0}/{total}")
        if time.time() > deadline:
            log("  timed out waiting; re-run with `collect` later (tasks are kept for 30 days)")
            return False
        if not ready:
            time.sleep(poll)
    errors = conn.execute("SELECT COUNT(*) FROM tasks WHERE run_id=? AND status='error'",
                          (run_id,)).fetchone()[0]
    if errors:
        log(f"  {errors} keyword(s) errored - run `run --resume` to retry them")
    # finish even when a few keywords failed, so the report shows everything that worked
    conn.execute("UPDATE runs SET finished_at=datetime('now') WHERE id=?", (run_id,))
    conn.commit()
    return errors == 0


def reclassify(conn, cfg: Config) -> int:
    """Re-apply the detector to every stored result (after `mark` or list edits)."""
    det = get_detector(conn, cfg)
    rows = conn.execute("SELECT rowid, url, title FROM results").fetchall()
    for r in rows:
        v = det.classify(r["url"], r["title"] or "")
        conn.execute("UPDATE results SET is_parasite=?, platform=?, category=?, confidence=?, "
                     "reason=? WHERE rowid=?",
                     (int(v.is_parasite), v.platform, v.category, v.confidence, v.reason, r["rowid"]))
    conn.commit()
    return len(rows)
