"""Turns raw results into the numbers the report/CLI show."""
from __future__ import annotations

from collections import Counter, defaultdict

from .detector import discover_candidates


def finished_runs(conn, limit: int = 12) -> list[dict]:
    rows = conn.execute("""SELECT r.id, r.started_at, r.finished_at, r.cost, r.depth,
                           (SELECT COUNT(DISTINCT keyword_id) FROM results WHERE run_id = r.id) AS kws
                           FROM runs r WHERE r.finished_at IS NOT NULL
                           ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows][::-1]


def _load(conn, run_id: int) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM results WHERE run_id=? ORDER BY rank_group", (run_id,)):
        out[r["keyword_id"]].append(dict(r))
    return out


def _share(conn, run_id: int, top_n: int) -> float:
    r = conn.execute("""SELECT COUNT(DISTINCT keyword_id) FROM results
                        WHERE run_id=? AND is_parasite=1 AND rank_group<=?""", (run_id, top_n)).fetchone()[0]
    t = conn.execute("SELECT COUNT(DISTINCT keyword_id) FROM results WHERE run_id=?", (run_id,)).fetchone()[0]
    return round(100 * r / t, 1) if t else 0.0


def build(conn, top_n: int = 10, run_id: int | None = None) -> dict | None:
    runs = finished_runs(conn)
    if run_id is None and not runs:
        return None
    cur = next((r for r in runs if r["id"] == run_id), None) if run_id else runs[-1]
    if cur is None:
        return None
    prev = next((r for r in reversed(runs) if r["id"] < cur["id"]), None)

    kws = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM keywords")}
    cur_res = _load(conn, cur["id"])
    prev_res = _load(conn, prev["id"]) if prev else {}

    keyword_rows, plat = [], defaultdict(lambda: {"top3": 0, "topn": 0, "kws": set(), "cat": "",
                                                  "prev_topn": 0})
    niche = defaultdict(lambda: {"kws": 0, "with_parasite": 0, "plat": Counter(), "share_sum": 0.0})
    changes = []

    for kid, items in cur_res.items():
        k = kws.get(kid)
        if not k:
            continue
        top = [i for i in items if i["rank_group"] <= top_n]
        paras = [i for i in top if i["is_parasite"]]
        prev_items = prev_res.get(kid, [])
        prev_par = {i["url"]: i for i in prev_items if i["is_parasite"] and i["rank_group"] <= top_n}
        cur_par = {i["url"]: i for i in paras}

        for u, i in cur_par.items():
            if u not in prev_par and prev:
                changes.append({"kind": "new", "keyword": k["keyword"], "niche": k["niche"],
                                "platform": i["platform"], "url": u, "rank": i["rank_group"],
                                "prev_rank": None})
            elif u in prev_par and abs(prev_par[u]["rank_group"] - i["rank_group"]) >= 3:
                changes.append({"kind": "moved", "keyword": k["keyword"], "niche": k["niche"],
                                "platform": i["platform"], "url": u, "rank": i["rank_group"],
                                "prev_rank": prev_par[u]["rank_group"]})
        if prev:
            for u, i in prev_par.items():
                if u not in cur_par:
                    now = next((x["rank_group"] for x in items if x["url"] == u), None)
                    changes.append({"kind": "lost", "keyword": k["keyword"], "niche": k["niche"],
                                    "platform": i["platform"], "url": u, "rank": now,
                                    "prev_rank": i["rank_group"]})

        for i in paras:
            p = plat[i["platform"]]
            p["topn"] += 1
            p["top3"] += i["rank_group"] <= 3
            p["kws"].add(kid)
            p["cat"] = i["category"]
            niche[k["niche"] or "(none)"]["plat"][i["platform"]] += 1
        for i in prev_par.values():
            plat[i["platform"]]["prev_topn"] += 1

        n = niche[k["niche"] or "(none)"]
        n["kws"] += 1
        n["with_parasite"] += bool(paras)
        n["share_sum"] += len(paras) / max(len(top), 1)

        keyword_rows.append({
            "id": kid, "keyword": k["keyword"], "niche": k["niche"],
            "best": min((i["rank_group"] for i in paras), default=None),
            "prev_best": min((i["rank_group"] for i in prev_par.values()), default=None) if prev else None,
            "count": len(paras), "share": round(100 * len(paras) / max(len(top), 1)),
            "parasites": [{"rank": i["rank_group"], "platform": i["platform"], "url": i["url"],
                           "title": i["title"], "conf": i["confidence"]} for i in paras],
            "top": [{"rank": i["rank_group"], "host": i["host"], "p": i["is_parasite"]} for i in top],
        })

    keyword_rows.sort(key=lambda r: (r["best"] is None, r["best"] or 99, -r["count"]))

    known_roots = {i["root"] for items in cur_res.values() for i in items
                   if i["is_parasite"] and i["category"] != "suspected"}
    flat = [{"keyword": kws[kid]["keyword"], "niche": kws[kid]["niche"], "url": i["url"],
             "host": i["host"], "root": i["root"], "title": i["title"] or ""}
            for kid, items in cur_res.items() if kid in kws for i in items if i["rank_group"] <= 30]
    ignored = {r["domain"] for r in conn.execute("SELECT domain FROM overrides WHERE kind='ignore'")}
    candidates = [c for c in discover_candidates(flat, known_roots) if c["domain"] not in ignored][:25]

    total = len(keyword_rows)
    with_p = sum(1 for r in keyword_rows if r["best"] is not None)
    return {
        "top_n": top_n,
        "run": cur, "prev_run": prev,
        "summary": {"keywords": total, "with_parasite": with_p,
                    "pct": round(100 * with_p / total, 1) if total else 0,
                    "prev_pct": _share(conn, prev["id"], top_n) if prev else None,
                    "top3": sum(1 for r in keyword_rows if r["best"] is not None and r["best"] <= 3)},
        "platforms": sorted(
            [{"platform": k, "category": v["cat"], "top3": v["top3"], "topn": v["topn"],
              "keywords": len(v["kws"]), "prev_topn": v["prev_topn"] if prev else None}
             for k, v in plat.items()], key=lambda x: -x["topn"]),
        "niches": sorted(
            [{"niche": k, "keywords": v["kws"], "with_parasite": v["with_parasite"],
              "pct": round(100 * v["with_parasite"] / v["kws"]) if v["kws"] else 0,
              "avg_share": round(100 * v["share_sum"] / v["kws"]) if v["kws"] else 0,
              "top_platforms": [p for p, _ in v["plat"].most_common(3)]}
             for k, v in niche.items()], key=lambda x: -x["pct"]),
        "keywords": keyword_rows,
        "changes": sorted(changes, key=lambda c: ({"new": 0, "lost": 1, "moved": 2}[c["kind"]], c["rank"] or 99)),
        "candidates": candidates,
        "trend": [{"run": r["id"], "date": (r["finished_at"] or r["started_at"])[:10],
                   "pct": _share(conn, r["id"], top_n)} for r in runs],
    }
