"""Accept / override / decide each item, stale decisions, and the export."""
import csv
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import decisions  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import POLICY  # noqa: E402

FIDS = ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a")
RULE_NAMES = {r["id"]: r["section"] for r in POLICY["rules"]}
FILINGS = {}


def filing(fid):
    if fid not in FILINGS:
        raw = (ROOT / "data" / "filings" / f"{fid}.htm").read_text(encoding="utf-8", errors="ignore")
        with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}):
            FILINGS[fid] = analyze(fid, raw)[0]
    return FILINGS[fid]


class Decisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_starts_undecided(self):
        for fid in FIDS:
            st = decisions.states(filing(fid), decisions.load(self.root, fid))
            self.assertEqual(st["decided"], 0, fid)
            self.assertEqual(len(st["undecided"]), st["total"], fid)

    def test_accept_and_override(self):
        f = filing("starbucks-2026-def14a")
        decisions.apply(self.root, f, ["3"], "accept")
        data = decisions.apply(self.root, f, ["4"], "vote", "AGAINST", "Prefer to keep protections")
        st = decisions.states(f, data)["items"]
        self.assertEqual(st["3"]["state"], "accepted")
        self.assertEqual(st["3"]["final_vote"], "FOR")
        self.assertEqual(st["4"]["state"], "overridden")
        self.assertEqual(st["4"]["final_vote"], "AGAINST")
        self.assertEqual(st["4"]["reason"], "Prefer to keep protections")

    def test_override_needs_reason(self):
        f = filing("starbucks-2026-def14a")
        with self.assertRaises(ValueError):
            decisions.apply(self.root, f, ["4"], "vote", "AGAINST", "   ")
        self.assertEqual(decisions.load(self.root, f["id"]), {})

    def test_same_vote_as_policy_counts_as_accepted(self):
        f = filing("amazon-2026-def14a")
        data = decisions.apply(self.root, f, ["2"], "vote", "FOR")
        self.assertEqual(decisions.states(f, data)["items"]["2"]["state"], "accepted")

    def test_flagged_items_need_a_vote_and_reason(self):
        for fid, n in (("starbucks-2026-def14a", "5"), ("alphabet-2026-def14a", "7"), ("amazon-2026-def14a", "4")):
            f = filing(fid)
            self.assertEqual(next(i for i in f["items"] if i["number"] == n)["recommendation"]["vote"], "FLAG")
            with self.assertRaises(ValueError):
                decisions.apply(self.root, f, [n], "accept")
            with self.assertRaises(ValueError):
                decisions.apply(self.root, f, [n], "vote", "AGAINST")
            data = decisions.apply(self.root, f, [n], "vote", "ABSTAIN", "Need more info from the proponent")
            self.assertEqual(decisions.states(f, data)["items"][n]["state"], "decided", fid)

    def test_absent_needs_a_reason(self):
        for fid, n in (("starbucks-2026-def14a", "3"), ("alphabet-2026-def14a", "7"), ("amazon-2026-def14a", "1a")):
            f = filing(fid)
            with self.assertRaises(ValueError):
                decisions.apply(self.root, f, [n], "vote", "ABSENT", "")
            data = decisions.apply(self.root, f, [n], "vote", "ABSENT", "Shares on loan; not recalled")
            st = decisions.states(f, data)["items"][n]
            self.assertEqual(st["final_vote"], "ABSENT", fid)
            row = next(r for r in decisions.export_rows(f, data, RULE_NAMES) if r["item"] == n)
            self.assertEqual((row["final_vote"], row["reason"]), ("ABSENT", "Shares on loan; not recalled"))

    def test_bulk_accept_directors(self):
        for fid in FIDS:
            f = filing(fid)
            nominees = [i["number"] for i in f["items"] if i["proposal"] == "1"]
            data = decisions.apply(self.root, f, nominees, "accept")
            st = decisions.states(f, data)
            self.assertEqual(st["decided"], len(nominees), fid)
            self.assertTrue(all(st["items"][n]["final_vote"] == "FOR" for n in nominees))

    def test_bad_requests(self):
        f = filing("amazon-2026-def14a")
        for args in ((["99"], "accept"), ([], "accept"), (["2"], "vote", "MAYBE", "x"), (["2"], "approve")):
            with self.assertRaises(ValueError):
                decisions.apply(self.root, f, *args)

    def test_clear(self):
        f = filing("amazon-2026-def14a")
        decisions.apply(self.root, f, ["2"], "accept")
        data = decisions.apply(self.root, f, ["2"], "clear")
        self.assertEqual(decisions.states(f, data)["items"]["2"]["state"], "undecided")

    def test_decision_goes_stale_when_recommendation_changes(self):
        f = filing("starbucks-2026-def14a")
        data = decisions.apply(self.root, f, ["3"], "accept")
        changed = {**f, "items": [dict(i, recommendation=dict(i["recommendation"], vote="AGAINST"))
                                  if i["number"] == "3" else i for i in f["items"]]}
        st = decisions.states(changed, data)
        self.assertEqual(st["items"]["3"]["state"], "stale")
        self.assertIn("3", st["undecided"])


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_has_every_item_and_records_the_change(self):
        f = filing("starbucks-2026-def14a")
        decisions.apply(self.root, f, ["3"], "accept")
        data = decisions.apply(self.root, f, ["4"], "vote", "AGAINST", "Keep supermajority")
        rows = list(csv.DictReader(io.StringIO(decisions.to_csv(decisions.export_rows(f, data, RULE_NAMES)))))
        self.assertEqual(len(rows), 19)
        r4 = next(r for r in rows if r["item"] == "4")
        self.assertEqual((r4["policy_rec"], r4["final_vote"], r4["changed"], r4["reason"]),
                         ("FOR", "AGAINST", "yes", "Keep supermajority"))
        self.assertEqual(r4["board_rec"], "NONE")
        r3 = next(r for r in rows if r["item"] == "3")
        self.assertEqual((r3["final_vote"], r3["changed"], r3["policy_rule"]), ("FOR", "no", "Auditor"))
        self.assertTrue(r3["source_page"])
        r2 = next(r for r in rows if r["item"] == "2")
        self.assertEqual((r2["policy_rec"], r2["final_vote"], r2["decision"]), ("REVIEW", "", "undecided"))

    def test_json_reports_completeness(self):
        for fid in FIDS:
            f = filing(fid)
            out = decisions.to_json(f, {}, RULE_NAMES, POLICY)
            self.assertFalse(out["complete"], fid)
            self.assertEqual(len(out["votes"]), len(f["items"]))
            self.assertEqual(out["policy"]["settings"]["on_ceo_change"], "flag")
            data = {}
            for it in f["items"]:
                if it["recommendation"]["vote"] == "FLAG":
                    data = decisions.apply(self.root, f, [it["number"]], "vote", "AGAINST", "House view")
                else:
                    data = decisions.apply(self.root, f, [it["number"]], "accept")
            self.assertTrue(decisions.to_json(f, data, RULE_NAMES, POLICY)["complete"], fid)

    def test_source_page_comes_from_the_evidence(self):
        f = filing("alphabet-2026-def14a")
        aud = next(i for i in f["items"] if i["recommendation"]["rule"] == "AUDITOR")
        self.assertEqual(decisions.main_page(aud, f["facts"]), f["facts"]["fees"]["evidence"][0]["page"])


if __name__ == "__main__":
    unittest.main()
