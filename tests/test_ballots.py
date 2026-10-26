"""Regression tests: the ballot we expect from each sample filing.

Run:  python3 -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from pages import split_pages  # noqa: E402
from extract import extract_ballot  # noqa: E402
from classify import keyword_category, RULE_FOR_CATEGORY  # noqa: E402


def rule_for(title):
    return RULE_FOR_CATEGORY[keyword_category(title)["category"]]


def load(fid):
    raw = (ROOT / "data" / "filings" / f"{fid}.htm").read_text(encoding="utf-8", errors="ignore")
    pages = split_pages(raw)
    filing = extract_ballot(fid, pages)
    return filing, {i.number: i for i in filing.items}


class Starbucks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.items = load("starbucks-2026-def14a")

    def test_header(self):
        self.assertEqual(self.f.company, "Starbucks Corporation")
        self.assertEqual(self.f.meeting_date, "March 25, 2026")

    def test_ballot(self):
        self.assertEqual(len(self.f.items), 19)          # 11 nominees + proposals 2-9
        self.assertEqual(self.items["1a"].title, "Elect Ritch Allison")
        self.assertEqual(self.items["1k"].title, "Elect Wei Zhang")
        self.assertEqual(self.items["2"].kind, "say_on_pay")
        self.assertEqual(self.items["3"].kind, "auditor")

    def test_board_makes_no_recommendation_on_prop_4(self):
        self.assertEqual(self.items["4"].board_rec, "NONE")
        self.assertEqual(self.items["4"].kind, "board_structure")   # majority voting
        self.assertEqual(self.items["4"].proponent, "shareholder")

    def test_pages(self):
        self.assertEqual(self.items["3"].board_rec_evidence.page, "85")
        self.assertEqual(self.items["9"].section.page, "97")


class Alphabet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.items = load("alphabet-2026-def14a")

    def test_header(self):
        self.assertEqual(self.f.company, "Alphabet Inc.")
        self.assertEqual(self.f.meeting_date, "June 5, 2026")

    def test_ballot(self):
        self.assertEqual(len(self.f.items), 23)          # 10 nominees + proposals 2-14
        self.assertEqual(self.items["1h"].title, "Elect Roger W. Ferguson Jr.")
        self.assertEqual(self.items["2"].kind, "auditor")
        self.assertEqual(self.items["4"].kind, "say_on_pay")

    def test_stock_plan_and_equal_voting_are_flagged(self):
        self.assertEqual(rule_for(self.items["3"].title), "FLAG")
        # "Equal shareholder voting" is not declassification or majority voting.
        self.assertEqual(rule_for(self.items["7"].title), "FLAG")

    def test_board_recs(self):
        self.assertEqual(self.items["2"].board_rec, "FOR")
        for n in range(5, 15):
            self.assertEqual(self.items[str(n)].board_rec, "AGAINST", n)


class Amazon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.items = load("amazon-2026-def14a")

    def test_header(self):
        self.assertEqual(self.f.company, "Amazon.com, Inc.")
        self.assertEqual(self.f.meeting_date, "May 20, 2026")

    def test_ballot(self):
        self.assertEqual(len(self.f.items), 17)          # 11 nominees + items 2-7
        self.assertEqual(self.items["1b"].title, "Elect Andrew R. Jassy")
        self.assertEqual(self.items["1k"].title, "Elect Wendell P. Weeks")
        self.assertEqual(self.items["3"].kind, "say_on_pay")

    def test_pages(self):
        self.assertEqual(self.items["2"].section.page, "25")
        self.assertEqual(self.items["7"].section.page, "42")
        self.assertEqual(self.items["7"].board_rec, "AGAINST")


if __name__ == "__main__":
    unittest.main()


class VoteSummary(unittest.TestCase):
    """What is being voted on: copied verbatim from the item's own section."""

    def test_every_item_has_a_verbatim_summary(self):
        def squash(t):
            return " ".join(t.split())
        for fid in ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a"):
            pages = split_pages((ROOT / "data" / "filings" / f"{fid}.htm").read_text(encoding="utf-8", errors="ignore"))
            items = extract_ballot(fid, pages).to_dict()["items"]
            for it in items:
                s = it["summary"]
                self.assertIsNotNone(s, f"{fid} {it['number']}")
                page_text = squash(" ".join(pages[s["page_index"]].lines + pages[min(s["page_index"] + 1, len(pages) - 1)].lines))
                first = s["quote"].split(" … ")[0].split(" • ")[0]
                self.assertIn(squash(first), page_text, f"{fid} {it['number']}")
                if it["proponent"] == "shareholder":
                    self.assertEqual(it["summary_kind"], "resolution", f"{fid} {it['number']}")
                    self.assertRegex(s["quote"], r"(?i)^resolved")

    def test_summary_stays_inside_its_own_proposal(self):
        pages = split_pages((ROOT / "data" / "filings" / "starbucks-2026-def14a.htm").read_text(encoding="utf-8", errors="ignore"))
        items = {i["number"]: i for i in extract_ballot("starbucks-2026-def14a", pages).to_dict()["items"]}
        self.assertIn("greater than majority vote", items["4"]["summary"]["quote"])
        self.assertIn("Chairman", items["5"]["summary"]["quote"])
        self.assertTrue(items["2"]["summary"]["quote"].startswith("RESOLVED, that the shareholders of Starbucks"))
