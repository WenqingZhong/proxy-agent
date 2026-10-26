"""Evidence links: every cited claim points at the exact words in the live filing on SEC.gov."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from links import Linker, render_blocks  # noqa: E402
from pages import split_pages  # noqa: E402
from pipeline import analyze  # noqa: E402

SOURCES = json.loads((ROOT / "data" / "filings" / "sources.json").read_text())
RUNS = {}


def run(fid):
    if fid not in RUNS:
        raw = (ROOT / "data" / "filings" / f"{fid}.htm").read_text(encoding="utf-8", errors="ignore")
        with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}):
            RUNS[fid] = analyze(fid, raw, source_url=SOURCES[fid])
    return RUNS[fid]


def all_links(filing):
    for it in filing["items"]:
        for st in it["recommendation"]["steps"]:
            if st.get("cite"):
                yield it["number"], st["text"], st["link"]
        for key in ("summary", "board_rec_evidence"):
            if it.get(key):
                yield it["number"], key, it[key]["link"]


class Rendering(unittest.TestCase):
    def test_inline_tags_add_no_space_and_blocks_split(self):
        html = '<p><span>A</span><span>pproval</span> of the <b>Plan</b></p><table><tr><td>$</td><td>9,765</td></tr></table>'
        self.assertEqual(render_blocks(html), ["approval of the plan", "$", "9,765"])


class Links(unittest.TestCase):
    def test_every_cited_claim_has_a_link_into_its_sec_filing(self):
        for fid, url in SOURCES.items():
            filing, _ = run(fid)
            self.assertEqual(filing["source_url"], url)
            links = list(all_links(filing))
            self.assertGreater(len(links), 20, fid)
            for n, what, link in links:
                self.assertTrue(link["url"].startswith(url + "#:~:text="), f"{fid} {n} {what}")
                self.assertTrue(link["page"], f"{fid} {n} {what}")
            exact = sum(link["exact"] for _, _, link in links)
            self.assertGreaterEqual(exact / len(links), 0.95, fid)

    def test_vote_summaries_and_attendance_link_exactly(self):
        for fid in SOURCES:
            filing, _ = run(fid)
            for it in filing["items"]:
                self.assertTrue(it["summary"]["link"]["exact"], f"{fid} {it['number']}")
            att = next(i for i in filing["items"] if i["number"].startswith("1"))
            self.assertTrue(att["recommendation"]["steps"][0]["link"]["exact"], fid)

    def test_fee_reasons_cite_their_own_cells(self):
        filing, _ = run("starbucks-2026-def14a")
        aud = next(i for i in filing["items"] if i["recommendation"]["rule"] == "AUDITOR")
        steps = aud["recommendation"]["steps"]
        self.assertTrue(steps[0]["text"].startswith("Audit fees, FY2025: $9.77M"))
        self.assertEqual(unquote(steps[0]["link"]["url"].split("#:~:text=")[1]), "9,765")
        self.assertEqual(steps[0]["link"]["page"], "84")
        self.assertIsNone(steps[-1]["cite"])          # the arithmetic is ours, not the filing's

    def test_first_match_rule(self):
        """A term is only used if the browser's first match is the passage we mean."""
        pages = split_pages((ROOT / "data" / "filings" / "alphabet-2026-def14a.htm").read_text(errors="ignore"))
        linker = Linker("U", pages)
        filing, _ = run("alphabet-2026-def14a")
        for n, what, link in all_links(filing):
            if not link["exact"] or "-," in link["url"] or ",-" in link["url"]:
                continue
            start = unquote(link["url"].split("#:~:text=")[1].split(",")[0])
            bi, _ = linker._first(start)
            self.assertIn(linker.blocks[bi][0], (link["page_index"], link["page_index"] - 1, link["page_index"] + 1),
                          f"{n} {what}")

    def test_no_source_url_means_no_links(self):
        raw = (ROOT / "data" / "filings" / "amazon-2026-def14a.htm").read_text(encoding="utf-8", errors="ignore")
        with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}):
            filing, _ = analyze("amazon-2026-def14a", raw)
        self.assertTrue(all(link["url"] is None for _, _, link in all_links(filing)))


if __name__ == "__main__":
    unittest.main()
