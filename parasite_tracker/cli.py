from __future__ import annotations

import argparse
import csv
import sys

from . import analysis, db, tracker
from .config import load_config, read_keywords
from .dataforseo import DataForSEO, DataForSEOError
from .detector import root_domain, host_of
from .report import render


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parasite_tracker",
                                 description="Track which parasite sites rank for your keywords.")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="check all keywords now (submit + collect + report)")
    r.add_argument("--resume", action="store_true", help="continue / retry the unfinished run")
    r.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    r.add_argument("--limit", type=int, help="only the first N keywords (cheap test)")
    sub.add_parser("collect", help="fetch results of an unfinished run")
    sub.add_parser("report", help="rebuild the HTML report from stored data")
    sub.add_parser("estimate", help="show expected cost of a run")
    sub.add_parser("reclassify", help="re-apply parasite rules to stored results")
    m = sub.add_parser("mark", help="teach the tool: mark a domain as parasite or ignore")
    m.add_argument("domain"); m.add_argument("kind", choices=["parasite", "ignore"])
    e = sub.add_parser("export", help="write the latest run to CSV")
    e.add_argument("out", nargs="?", default="latest.csv")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    conn = db.connect(cfg.db)

    try:
        if a.cmd == "mark":
            d = root_domain(host_of(a.domain)) if "/" not in a.domain else a.domain.lower()
            conn.execute("INSERT OR REPLACE INTO overrides VALUES(?,?)", (d, a.kind))
            conn.commit()
            n = tracker.reclassify(conn, cfg)
            print(f"{d} -> {a.kind}; re-checked {n} stored results. Run `report` to refresh.")
        elif a.cmd == "reclassify":
            print(f"re-checked {tracker.reclassify(conn, cfg)} results")
        elif a.cmd == "report":
            return _report(conn, cfg)
        elif a.cmd == "export":
            return _export(conn, cfg, a.out)
        elif a.cmd == "estimate":
            n = tracker.sync_keywords(conn, read_keywords(cfg.keywords, cfg))
            print(f"{n} keywords x depth {cfg.depth} -> ~${tracker.estimate_cost(n, cfg.depth, cfg.priority):.2f} per run")
        elif a.cmd == "collect":
            run_id = tracker.latest_open_run(conn)
            if not run_id:
                print("no unfinished run"); return 1
            tracker.collect(conn, DataForSEO(cfg.login, cfg.password), cfg, run_id)
            return _report(conn, cfg)
        elif a.cmd == "run":
            return _run(conn, cfg, a)
    except DataForSEOError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    return 0


def _run(conn, cfg, a) -> int:
    kws = read_keywords(cfg.keywords, cfg)
    if a.limit:
        kws = kws[:a.limit]
    n = tracker.sync_keywords(conn, kws)
    est = tracker.estimate_cost(n, cfg.depth, cfg.priority)
    api = DataForSEO(cfg.login, cfg.password)
    run_id = tracker.latest_open_run(conn) if a.resume else None
    if a.resume and run_id:
        conn.execute("UPDATE tasks SET task_id=NULL, status='pending', error=NULL "
                     "WHERE run_id=? AND status='error'", (run_id,))
        conn.commit()
    else:
        if not a.yes and sys.stdin.isatty():
            if input(f"{n} keywords, depth {cfg.depth}: about ${est:.2f}. Continue? [y/N] ").lower() != "y":
                return 1
        run_id = tracker.start_run(conn, cfg)
    print(f"run #{run_id}: {n} keywords (est. ${est:.2f})", file=sys.stderr)
    tracker.submit(conn, api, cfg, run_id)
    ok = tracker.collect(conn, api, cfg, run_id)
    _report(conn, cfg)
    return 0 if ok else 3


def _report(conn, cfg) -> int:
    data = analysis.build(conn, cfg.top_n)
    if not data:
        print("no finished run yet - run `python -m parasite_tracker run` first"); return 1
    render(data, cfg.report)
    s = data["summary"]
    print(f"{s['with_parasite']}/{s['keywords']} keywords ({s['pct']}%) have a parasite in the top "
          f"{cfg.top_n}. Top platforms: " + ", ".join(f"{p['platform']} ({p['topn']})" for p in data["platforms"][:5]))
    if data["changes"]:
        c = {k: sum(1 for x in data["changes"] if x["kind"] == k) for k in ("new", "lost", "moved")}
        print(f"changes: +{c['new']} new, -{c['lost']} lost, {c['moved']} moved")
    print(f"report written to {cfg.report}")
    return 0


def _export(conn, cfg, out) -> int:
    run = analysis.finished_runs(conn, 1)
    if not run:
        print("no finished run"); return 1
    rows = conn.execute("""SELECT k.keyword, k.niche, r.rank_group, r.host, r.url, r.title,
                           r.is_parasite, r.platform, r.category, r.confidence
                           FROM results r JOIN keywords k ON k.id = r.keyword_id
                           WHERE r.run_id=? ORDER BY k.keyword, r.rank_group""", (run[-1]["id"],))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["keyword", "niche", "rank", "host", "url", "title", "is_parasite", "platform", "category", "confidence"])
        w.writerows(tuple(r) for r in rows)
    print(f"wrote {out}")
    return 0
