import threading
import unittest
from unittest import mock

from parasite_tracker import live
from parasite_tracker.config import Config
from parasite_tracker.dataforseo import DataForSEOError


class FlakyAPI:
    """'flaky' fails twice with 40101 then works; 'dead' always 40101 until healed; 'denied' = bad login."""
    def __init__(self):
        self.calls, self.healed = {}, False

    def live(self, task):
        kw = task["keyword"]
        self.calls[kw] = self.calls.get(kw, 0) + 1
        if kw == "denied":
            raise DataForSEOError("DataForSEO rejected the login/password (HTTP 401).")
        if (kw == "flaky" and self.calls[kw] <= 2) or (kw == "dead" and not self.healed):
            return {"status_code": 40101, "status_message": "Internal SE Server Error."}
        return {"status_code": 20000, "cost": 0.002, "result": [{"items": [
            {"type": "organic", "url": "https://medium.com/x", "title": "t", "rank_group": 1, "rank_absolute": 1}]}]}


def kws(*names):
    return [{"keyword": n, "niche": "", "location_code": 2840, "language_code": "en", "device": "desktop"} for n in names]


def wait(job):
    for _ in range(300):
        if job.snapshot()["finished"]:
            return job.snapshot()
        threading.Event().wait(0.02)
    raise AssertionError("job did not finish")


@mock.patch("parasite_tracker.live.time.sleep", lambda s: None)
class LiveTests(unittest.TestCase):
    def test_transient_errors_are_retried_and_failures_can_be_retried(self):
        api = FlakyAPI()
        job = live.start(kws("ok", "flaky", "dead"), Config(), api, {})
        s = wait(job)
        self.assertEqual(api.calls["flaky"], 3)             # recovered by itself
        self.assertEqual((s["done"], s["n_errors"], s["fatal"]), (3, 1, ""))   # only 'dead' failed
        self.assertIn("40101", s["errors"][0])
        api.healed = True
        self.assertEqual(live.retry(job, api), 1)
        s = wait(job)
        self.assertEqual((s["done"], s["total"], s["n_errors"]), (3, 3, 0))

    def test_only_a_real_auth_error_is_fatal(self):
        job = live.start(kws("denied"), Config(), FlakyAPI(), {})
        self.assertIn("rejected the login", wait(job)["fatal"])


if __name__ == "__main__":
    unittest.main()
