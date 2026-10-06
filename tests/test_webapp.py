import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

os.environ["APP_PASSWORD"] = "pw123"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from parasite_tracker import webapp  # noqa: E402
from tests.test_pipeline import FakeAPI  # noqa: E402


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeAPI({"best vpn": ["https://medium.com/a", "https://real.com/1"],
                            "best crm": ["https://real.com/2", "https://quora.com/q"]})
        webapp.DataForSEO = lambda login, password: cls.fake
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cookie = ""

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, form=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": self.cookie} if self.cookie else {}
        body = urlencode(form, doseq=True) if form is not None else None
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, h)
        r = c.getresponse()
        return r.status, r.read().decode(), r

    def test_flow(self):
        self.assertEqual(self.req("GET", "/")[0], 303)                   # needs login
        self.assertEqual(self.req("POST", "/login", {"password": "bad"})[0], 401)
        st, _, r = self.req("POST", "/login", {"password": "pw123"})
        self.assertEqual(st, 303)
        type(self).cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.req("GET", "/")[0], 200)

        st, body, _ = self.req("POST", "/search", {"text": "best vpn", "login": "", "password": ""})
        self.assertIn("login", json.loads(body)["error"])

        text = "best vpn, vpn\nbest crm\tsaas\n"
        st, body, _ = self.req("POST", "/search", {"text": text, "login": "a@b.c", "password": "x", "depth": "10"})
        job = json.loads(body)["job"]
        for _ in range(100):
            time.sleep(0.1)
            s = json.loads(self.req("GET", f"/job/{job}.json")[1])
            if s["finished"]:
                break
        self.assertEqual((s["done"], s["total"], s["n_errors"]), (2, 2, 0), s)
        st, rep, _ = self.req("GET", f"/job/{job}/report")
        self.assertIn("quora.com", rep)
        self.assertIn("best crm", self.req("GET", f"/job/{job}/export.csv")[1])
        self.assertEqual(self.req("GET", "/job/nope.json")[0], 404)

        # nothing about the keywords or the password was persisted
        import sqlite3
        raw = "".join(str(r) for t in ("keywords", "settings", "results") for r in
                      sqlite3.connect(os.environ["DB_PATH"]).execute(f"SELECT * FROM {t}"))
        self.assertNotIn("best vpn", raw)

        self.req("POST", "/domains", {"action": "add", "domain": "real.com", "kind": "parasite"})
        self.assertIn("real.com", self.req("GET", "/domains")[1])
        self.assertEqual(self.req("GET", "/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
