import tempfile
import unittest
from pathlib import Path

from parasite_tracker import analysis, db, tracker
from parasite_tracker.config import Config
from parasite_tracker.detector import Detector, root_domain
from parasite_tracker.report import render


class FakeAPI:
    """Mimics DataForSEO: every task is immediately ready."""
    def __init__(self, serps):
        self.serps, self.tasks = serps, {}

    def post_tasks(self, tasks):
        out = []
        for i, t in enumerate(tasks):
            tid = f"t{len(self.tasks)}"
            self.tasks[tid] = t["keyword"]
            out.append({"id": tid, "status_code": 20100})
        return out

    def ready_ids(self):
        return set(self.tasks)

    def get_task(self, tid):
        urls = self.serps[self.tasks[tid]]
        items = [{"type": "organic", "url": u, "title": u, "rank_group": i + 1, "rank_absolute": i + 1}
                 for i, u in enumerate(urls)]
        return {"status_code": 20000, "cost": 0.0012, "result": [{"items": items}]}


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.d = Detector()

    def test_hosts_subdomains_paths(self):
        c = self.d.classify
        self.assertTrue(c("https://medium.com/@x/best-vpn").is_parasite)
        self.assertTrue(c("https://someone.github.io/vpn").is_parasite)
        self.assertTrue(c("https://www.linkedin.com/pulse/best-crm").is_parasite)
        self.assertFalse(c("https://www.linkedin.com/jobs/x").is_parasite)  # path rules are specific
        self.assertTrue(c("https://www.forbes.com/sites/x/2025/y").is_parasite)
        self.assertFalse(c("https://www.forbes.com/advisor/best-crm").is_parasite)
        self.assertTrue(c("https://forums.example.com/t/1").is_parasite)
        self.assertFalse(c("https://nytimes.com/wirecutter/x").is_parasite)
        self.assertFalse(c("https://notmedium.com/x").is_parasite)  # no substring false positives

    def test_overrides_and_optional_categories(self):
        self.assertTrue(Detector(overrides={"foo.com": "parasite"}).classify("https://a.foo.com/x").is_parasite)
        self.assertFalse(Detector(overrides={"medium.com": "ignore"}).classify("https://medium.com/x").is_parasite)
        self.assertFalse(self.d.classify("https://youtube.com/watch?v=1").is_parasite)
        self.assertTrue(Detector(["social_video"]).classify("https://youtube.com/watch?v=1").is_parasite)
        self.assertTrue(Detector(["edu_gov"]).classify("https://cs.mit.edu/x").is_parasite)

    def test_root_domain(self):
        self.assertEqual(root_domain("a.b.example.co.uk"), "example.co.uk")
        self.assertEqual(root_domain("a.b.example.com"), "example.com")


class PipelineTests(unittest.TestCase):
    def test_two_runs_and_report(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(db=str(tmp / "t.db"), report=str(tmp / "r.html"))
        conn = db.connect(cfg.db)
        tracker.sync_keywords(conn, [
            {"keyword": "a", "niche": "n1", "location_code": 2840, "language_code": "en", "device": "desktop"},
            {"keyword": "b", "niche": "n2", "location_code": 2840, "language_code": "en", "device": "desktop"}])
        serp1 = {"a": ["https://real.com/1", "https://medium.com/a", "https://x.com/2"],
                 "b": ["https://real.com/3", "https://real.com/4"]}
        serp2 = {"a": ["https://medium.com/a", "https://real.com/1", "https://u.blogspot.com/z"],
                 "b": ["https://reddit.com/r/b", "https://real.com/4"]}
        for serps in (serp1, serp2):
            api = FakeAPI(serps)
            rid = tracker.start_run(conn, cfg)
            tracker.submit(conn, api, cfg, rid)
            self.assertTrue(tracker.collect(conn, api, cfg, rid, poll=0))
        data = analysis.build(conn)
        self.assertEqual(data["summary"]["keywords"], 2)
        self.assertEqual(data["summary"]["with_parasite"], 2)
        self.assertEqual(data["summary"]["prev_pct"], 50.0)
        kinds = {(c["kind"], c["platform"]) for c in data["changes"]}
        self.assertIn(("new", "reddit.com"), kinds)
        self.assertIn(("new", "blogspot.com"), kinds)
        self.assertEqual(data["keywords"][0]["best"], 1)
        render(data, cfg.report)
        self.assertIn("medium.com", Path(cfg.report).read_text())

    def test_mark_reclassifies(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(db=str(tmp / "t.db"))
        conn = db.connect(cfg.db)
        tracker.sync_keywords(conn, [{"keyword": "a", "niche": "", "location_code": 1, "language_code": "en", "device": "desktop"}])
        api = FakeAPI({"a": ["https://spamhost.com/x"]})
        rid = tracker.start_run(conn, cfg)
        tracker.submit(conn, api, cfg, rid); tracker.collect(conn, api, cfg, rid, poll=0)
        self.assertEqual(conn.execute("SELECT SUM(is_parasite) FROM results").fetchone()[0], 0)
        conn.execute("INSERT INTO overrides VALUES('spamhost.com','parasite')")
        tracker.reclassify(conn, cfg)
        self.assertEqual(conn.execute("SELECT SUM(is_parasite) FROM results").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
