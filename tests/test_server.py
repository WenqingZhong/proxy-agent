"""The HTTP API: only the app's own page may use it, and a save must name the
recommendation she was shown."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import server  # noqa: E402

FIDS = ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.patches = [mock.patch.dict(os.environ, {"LLM_MODE": "stub"}),
                       mock.patch.object(server, "DECISIONS_DIR", Path(cls.tmp.name) / "decisions"),
                       mock.patch.object(server, "EXPORTS_DIR", Path(cls.tmp.name) / "exports")]
        for p in cls.patches:
            p.start()
        server.STORE.load()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        res = conn.getresponse()
        data = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, data

    def auditor(self, fid):
        it = next(i for i in server.STORE.filings[fid]["items"]
                  if i["classification"]["category"] == "auditor_ratification")
        return it["number"], it["recommendation"]["vote"]

    def saved(self, fid):
        return self.request("GET", f"/api/filings/{fid}/decisions")[1]["decided"]

    def test_save_with_the_vote_she_saw(self):
        for fid in FIDS:
            n, vote = self.auditor(fid)
            status, _ = self.request("POST", f"/api/filings/{fid}/decisions",
                                     {"items": [n], "action": "accept", "expect": {n: vote}})
            self.assertEqual(status, 200, fid)
            self.request("POST", f"/api/filings/{fid}/decisions", {"items": [n], "action": "clear"})

    def test_changed_recommendation_is_a_conflict(self):
        for fid in FIDS:
            n, vote = self.auditor(fid)
            status, data = self.request("POST", f"/api/filings/{fid}/decisions",
                                        {"items": [n], "action": "accept", "expect": {n: "AGAINST" if vote == "FOR" else "FOR"}})
            self.assertEqual((status, data.get("conflict")), (409, True), fid)
            self.assertEqual(self.saved(fid), 0, fid)

    def test_save_without_expect_is_refused(self):
        n, _ = self.auditor(FIDS[0])
        status, _ = self.request("POST", f"/api/filings/{FIDS[0]}/decisions", {"items": [n], "action": "accept"})
        self.assertEqual(status, 400)
        self.assertEqual(self.saved(FIDS[0]), 0)

    def test_other_sites_cannot_post(self):
        n, vote = self.auditor(FIDS[0])
        body = {"items": [n], "action": "accept", "expect": {n: vote}}
        path = f"/api/filings/{FIDS[0]}/decisions"
        for headers in ({"Origin": "https://evil.example"},          # cross-site fetch
                        {"Content-Type": "text/plain"},              # no-cors "simple" request
                        {"Host": f"evil.example:{self.port}"}):      # DNS rebinding
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", path, body, headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/policy/settings", {"id": "on_ceo_change", "value": "current_ceo"},
                                      {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/filings", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.saved(FIDS[0]), 0)

    def test_own_page_can_post(self):
        n, vote = self.auditor(FIDS[1])
        status, _ = self.request("POST", f"/api/filings/{FIDS[1]}/decisions",
                                 {"items": [n], "action": "accept", "expect": {n: vote}},
                                 {"Origin": f"http://localhost:{self.port}", "Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)
        self.request("POST", f"/api/filings/{FIDS[1]}/decisions", {"items": [n], "action": "clear"})


if __name__ == "__main__":
    unittest.main()
