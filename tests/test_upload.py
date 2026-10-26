"""Uploading a filing: it's analysed right away, saved only if a ballot is found, and can't
reach outside data/filings/ or silently replace a filing she has decisions on."""
import http.client
import json
import os
import shutil
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

AMAZON = (ROOT / "data" / "filings" / "amazon-2026-def14a.htm").read_text(encoding="utf-8", errors="ignore")
URL = "https://www.sec.gov/Archives/edgar/data/1018724/000110465926041026/tm261382-1_def14a.htm"


class ServerCase(unittest.TestCase):
    """A server on a temporary data folder holding only the Starbucks filing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.filings = root / "filings"
        self.filings.mkdir()
        shutil.copy(ROOT / "data" / "filings" / "starbucks-2026-def14a.htm", self.filings)
        self.patches = [mock.patch.dict(os.environ, {"LLM_MODE": "stub"}),
                        mock.patch.object(server, "FILINGS_DIR", self.filings),
                        mock.patch.object(server, "SOURCES_FILE", self.filings / "sources.json"),
                        mock.patch.object(server, "DECISIONS_DIR", root / "decisions"),
                        mock.patch.object(server, "EXPORTS_DIR", root / "exports"),
                        mock.patch.object(server, "STORE", server.Store())]
        for p in self.patches:
            p.start()
        server.STORE.load()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        res = conn.getresponse()
        data = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, data

    def upload(self, **body):
        return self.request("POST", "/api/filings", body)


class Upload(ServerCase):
    def test_new_filing_is_analysed_and_served_right_away(self):
        status, res = self.upload(filename="Amazon 2026 DEF14A.htm", html=AMAZON, source_url=URL)
        self.assertEqual(status, 201)
        self.assertEqual(res["filing"]["id"], "amazon-2026-def14a")
        self.assertEqual(res["filing"]["item_count"], 17)
        self.assertEqual(res["reports"], [])
        self.assertIn("amazon-2026-def14a", [f["id"] for f in res["filings"]])
        self.assertTrue((self.filings / "amazon-2026-def14a.htm").exists())
        status, f = self.request("GET", "/api/filings/amazon-2026-def14a")
        self.assertEqual(status, 200)
        self.assertEqual(f["source_url"], URL)
        steps = next(i for i in f["items"] if i["number"] == "2")["recommendation"]["steps"]
        self.assertTrue(steps[0]["link"]["url"].startswith(URL + "#:~:text="))
        self.assertEqual(json.loads((self.filings / "sources.json").read_text())["amazon-2026-def14a"], URL)
        server.STORE.reanalyze()          # saved to disk, so a full re-analysis keeps it
        self.assertIn("amazon-2026-def14a", server.STORE.filings)

    def test_same_name_needs_replace_and_reports_what_changed(self):
        self.upload(filename="amazon.htm", html=AMAZON)
        status, res = self.upload(filename="amazon.htm", html=AMAZON)
        self.assertEqual(status, 409)
        self.assertTrue(res["conflict"])
        status, res = self.upload(filename="amazon.htm", html=AMAZON, replace=True)
        self.assertEqual(status, 200)
        self.assertEqual(res["reports"][0]["filing"], "amazon")

    def test_replacing_a_starter_filing_rechecks_her_decisions(self):
        self.request("POST", "/api/filings/starbucks-2026-def14a/decisions",
                     {"items": ["3"], "action": "accept", "expect": {"3": "FOR"}})
        sbux = (ROOT / "data" / "filings" / "starbucks-2026-def14a.htm").read_text(encoding="utf-8", errors="ignore")
        status, res = self.upload(filename="starbucks-2026-def14a.htm", html=sbux + "<!-- amended -->", replace=True)
        self.assertEqual(status, 200)
        self.assertIn("The filing was updated", res["reports"][0]["causes"])

    def test_a_different_company_never_replaces(self):
        status, res = self.upload(filename="starbucks-2026-def14a.htm", html=AMAZON, replace=True)
        self.assertEqual(status, 400)
        self.assertIn("not Starbucks Corporation", res["error"])
        self.assertEqual(server.STORE.filings["starbucks-2026-def14a"]["company"], "Starbucks Corporation")

    def test_not_a_proxy_statement_is_refused_and_nothing_saved(self):
        status, res = self.upload(filename="notes.htm", html="<html><body><p>Quarterly newsletter</p></body></html>")
        self.assertEqual(status, 400)
        self.assertIn("Nothing was saved", res["error"])
        self.assertFalse((self.filings / "notes.htm").exists())
        self.assertNotIn("notes", server.STORE.filings)

    def test_names_never_reach_outside_the_filings_folder(self):
        status, res = self.upload(filename="../../Evil Name.HTM", html=AMAZON)
        self.assertEqual(status, 201)
        self.assertEqual(res["filing"]["id"], "evil-name")
        self.assertTrue((self.filings / "evil-name.htm").exists())
        self.assertEqual({p.name for p in Path(self.tmp.name).iterdir()} - {"decisions", "exports"}, {"filings"})

    def test_bad_requests(self):
        for body in ({"filename": "x.pdf", "html": AMAZON}, {"filename": "...htm", "html": AMAZON},
                     {"filename": 5, "html": AMAZON}, {"filename": "x.htm", "html": ""},
                     {"filename": "x.htm", "html": 12},
                     {"filename": "x.htm", "html": AMAZON, "source_url": "https://evil.example/x.htm"}):
            status, _ = self.upload(**body)
            self.assertEqual(status, 400, body)
        self.assertEqual(sorted(p.name for p in self.filings.iterdir()), ["starbucks-2026-def14a.htm"])

    def _status_for_length(self, length):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/filings", skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json",
                     "Content-Length": length}.items():
            conn.putheader(k, v)
        conn.endheaders()
        status = conn.getresponse().status
        conn.close()
        return status

    def test_size_limit_checked_before_reading(self):
        self.assertEqual(self._status_for_length(str(server.MAX_UPLOAD + 1)), 413)

    def test_negative_or_garbled_length_is_refused_without_reading(self):
        self.assertEqual(self._status_for_length("-1"), 400)
        self.assertEqual(self._status_for_length("lots"), 400)

    def test_only_this_app_may_upload(self):
        small = {"filename": "a.htm", "html": "<p>x</p>"}     # refused before the body is read
        status, _ = self.request("POST", "/api/filings", small, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/filings", small, {"Content-Type": "text/html"})
        self.assertEqual(status, 403)
        self.assertNotIn("a", server.STORE.filings)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# Adding by SEC.gov link (the network is always mocked)

import email.message  # noqa: E402
import time as _time  # noqa: E402
import urllib.error  # noqa: E402

import edgar  # noqa: E402

STARBUCKS = (ROOT / "data" / "filings" / "starbucks-2026-def14a.htm").read_text(encoding="utf-8", errors="ignore")


class FakeResponse:
    def __init__(self, body: bytes, ctype="text/html; charset=utf-8", drip=0.0):
        self.body, self.drip, self.pos = body, drip, 0
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype

    def read(self, n):
        # Like a real HTTPResponse: blocks until it has all n bytes (or the end).
        if self.drip:
            _time.sleep(self.drip * min(n, len(self.body) - self.pos))
        out = self.body[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def read1(self, n):
        # At most one socket read: a dripping server hands over one byte per call.
        if self.drip:
            _time.sleep(self.drip)
            n = 1
        out = self.body[self.pos:self.pos + n]
        self.pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(body, **kw):
    """Patch the EDGAR opener to answer with `body` and record what was requested."""
    seen = []

    def open_(req, timeout=None):
        seen.append(req)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body.encode() if isinstance(body, str) else body, **kw)
    return mock.patch.object(edgar._opener, "open", side_effect=open_), seen


class Links(unittest.TestCase):
    def test_canonical_accepts_only_edgar_documents(self):
        self.assertEqual(edgar.canonical(" " + URL + "?x=1#frag "), URL)
        self.assertEqual(edgar.canonical(URL.replace("https://www.sec.gov", "HTTPS://WWW.SEC.GOV:443")), URL)
        for bad in (URL.replace("https", "http"), URL.replace("www.sec.gov", "www.sec.gov@evil.example"),
                    URL.replace("www.sec.gov", "user:pw@www.sec.gov"), URL.replace("www.sec.gov", "evil.example"),
                    URL.replace("www.sec.gov", "www.sec.gov:8443"), URL.replace("/Archives", "/archives"),
                    URL.replace("1018724/", "1018724/%2e%2e/"), URL.replace("1018724/", "1018724/../"),
                    URL[:-4] + ".pdf", "", None, 5):
            with self.assertRaises(ValueError, msg=bad):
                edgar.canonical(bad)
        with self.assertRaisesRegex(ValueError, "index page"):
            edgar.canonical("https://www.sec.gov/Archives/edgar/data/1018724/000110465926041026/0001104659-26-041026-index.htm")

    def test_redirects_are_checked_before_they_are_followed(self):
        h = edgar._OnlyEdgar()
        req = urllib.request.Request(URL)
        with self.assertRaisesRegex(ValueError, "redirected"):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x.htm")
        ok = h.redirect_request(req, None, 301, "Moved", {}, URL.replace("tm261382-1", "tm261382-2"))
        self.assertTrue(ok.full_url.startswith("https://www.sec.gov/Archives/"))

    def test_needs_a_contact_user_agent(self):
        with mock.patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            with self.assertRaisesRegex(ValueError, "SEC_USER_AGENT"):
                edgar.fetch(URL, 1000)

    def test_sends_her_user_agent_and_no_compression(self):
        patch, seen = serve("<html>ok</html>")
        with patch, mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@example.com"}):
            self.assertEqual(edgar.fetch(URL, 1000), (URL, "<html>ok</html>"))
        self.assertEqual(seen[0].get_header("User-agent"), "Jane Doe jane@example.com")
        self.assertIsNone(seen[0].get_header("Accept-encoding"))

    def test_size_deadline_type_and_errors(self):
        env = mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@example.com"})
        cases = [(serve("x" * 5000)[0], "over"),
                 (serve("%PDF", ctype="application/pdf")[0], "isn't an HTML"),
                 (serve(urllib.error.HTTPError(URL, 403, "Forbidden", {}, None))[0], "403.*SEC_USER_AGENT"),
                 (serve(urllib.error.URLError(OSError("down")))[0], "Couldn't reach")]
        for patch, msg in cases:
            with patch, env:
                with self.assertRaisesRegex(ValueError, msg):
                    edgar.fetch(URL, 1000)
        patch, _ = serve("<html>" + "x" * 2000 + "</html>", drip=0.01)    # read() of it would take 20 s
        started = _time.monotonic()
        with patch, env, mock.patch.object(edgar, "DEADLINE", 0.2):
            with self.assertRaisesRegex(ValueError, "took over"):
                edgar.fetch(URL, 10_000)
        self.assertLess(_time.monotonic() - started, 1.0)


class AddByLink(ServerCase):
    def test_added_by_link_analysed_named_and_linked(self):
        patch, _ = serve(AMAZON)
        with patch, mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@example.com"}):
            status, res = self.upload(url=URL + "?utm=x")
        self.assertEqual(status, 201)
        self.assertEqual(res["filing"]["id"], "amazon-2026-def14a")     # company + meeting year
        self.assertEqual(res["filing"]["item_count"], 17)
        self.assertEqual(json.loads((self.filings / "sources.json").read_text())["amazon-2026-def14a"], URL)
        self.assertTrue((self.filings / "amazon-2026-def14a.htm").exists())

    def test_newer_copy_of_a_loaded_proxy_is_offered_as_a_replacement(self):
        sbux = "https://www.sec.gov/Archives/edgar/data/829224/000121390026007780/ea0270973-01.htm"
        patch, _ = serve(STARBUCKS)
        with patch, mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@example.com"}):
            status, res = self.upload(url=sbux)
            self.assertEqual(status, 409)
            self.assertIn("Starbucks Corporation", res["error"])
            self.assertIn("Replace it?", res["error"])
            status, res = self.upload(url=sbux, replace=True)
        self.assertEqual(status, 200)
        self.assertEqual(res["reports"][0]["filing"], "starbucks-2026-def14a")
        self.assertEqual(sorted(p.name for p in self.filings.iterdir() if p.suffix == ".htm"),
                         ["starbucks-2026-def14a.htm"])

    def test_same_first_word_different_company_gets_its_own_id(self):
        # A loaded "Amazon Air" 2026 proxy under another company number must not be matched.
        fake = {"id": "amazon-2026-def14a", "company": "Amazon Air Holdings Inc.", "meeting_date": "May 1, 2026",
                "source_url": "https://www.sec.gov/Archives/edgar/data/999/0001/a.htm"}
        with mock.patch.dict(server.STORE.filings, {"amazon-2026-def14a": fake}):
            self.assertEqual(server.STORE.id_for(AMAZON, URL), "amazon-1018724-2026-def14a")
            fake2 = dict(fake, company="Amazon.com, Inc.", source_url=None)       # same name, same year
            with mock.patch.dict(server.STORE.filings, {"amazon-2026-def14a": fake2}):
                self.assertEqual(server.STORE.id_for(AMAZON, URL), "amazon-2026-def14a")
            fake3 = dict(fake, company="Something Else", source_url=URL)         # same company number
            with mock.patch.dict(server.STORE.filings, {"amazon-2026-def14a": fake3}):
                self.assertEqual(server.STORE.id_for(AMAZON, URL), "amazon-2026-def14a")

    def test_bad_link_never_fetches(self):
        patch, seen = serve(AMAZON)
        with patch, mock.patch.dict(os.environ, {"SEC_USER_AGENT": "Jane Doe jane@example.com"}):
            for url in ("https://www.sec.gov@evil.example/Archives/edgar/data/1/2/x.htm", "http://localhost:8000/x.htm"):
                status, _ = self.upload(url=url)
                self.assertEqual(status, 400, url)
        self.assertEqual(seen, [])
