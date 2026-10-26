"""Saved exports: each export is kept with its time, and the app says when the
latest one no longer matches her votes."""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import decisions  # noqa: E402
import exports  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import POLICY, SETTINGS, policy_view  # noqa: E402

FIDS = ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a")
RULE_NAMES = {r["id"]: r["section"] for r in POLICY["rules"]}
RAW = {f: (ROOT / "data" / "filings" / f"{f}.htm").read_text(encoding="utf-8", errors="ignore") for f in FIDS}
BASE = {}


def run(fid, **settings):
    with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}):
        return analyze(fid, RAW[fid], dict(SETTINGS, **settings))[0]


def base(fid):
    if fid not in BASE:
        BASE[fid] = run(fid)
    return BASE[fid]


def first_policy_item(f):
    return next(i["number"] for i in f["items"] if i["recommendation"]["vote"] == "FOR")


class Exports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dec = Path(self.tmp.name) / "decisions"
        self.exp = Path(self.tmp.name) / "exports"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, f, data, fmt="csv"):
        return exports.record(self.exp, f, data, RULE_NAMES, policy_view(f["settings"]), fmt)

    def status(self, f, data):
        return exports.status(self.exp, f, data, RULE_NAMES)

    def test_no_export_yet(self):
        for fid in FIDS:
            st = self.status(base(fid), {})
            self.assertEqual((st["last"], st["outdated"], st["history"]), (None, False, []), fid)

    def test_export_is_current_until_a_vote_changes(self):
        for fid in FIDS:
            f = base(fid)
            n = first_policy_item(f)
            data = decisions.apply(self.dec, f, [n], "accept")
            self.export(f, data)
            self.assertFalse(self.status(f, data)["outdated"], fid)

            data = decisions.apply(self.dec, f, [n], "vote", "AGAINST", "House view")
            st = self.status(f, data)
            self.assertTrue(st["outdated"], fid)
            self.assertEqual(st["changes"], [{"item": n, "title": next(i["title"] for i in f["items"] if i["number"] == n),
                                              "changes": ["vote: FOR → AGAINST"]}], fid)

            # Changing it back makes the saved export match again.
            data = decisions.apply(self.dec, f, [n], "accept")
            self.assertFalse(self.status(f, data)["outdated"], fid)

    def test_reconfirming_the_same_vote_is_not_a_change(self):
        f = base("amazon-2026-def14a")
        n = first_policy_item(f)
        data = decisions.apply(self.dec, f, [n], "accept")
        self.export(f, data)
        data = decisions.apply(self.dec, f, [n], "confirm")       # new timestamp, same vote
        self.assertFalse(self.status(f, data)["outdated"])

    def test_renamed_decision_labels_dont_outdate_old_exports(self):
        for fid in FIDS:
            f = base(fid)
            n = first_policy_item(f)
            data = decisions.apply(self.dec, f, [n], "vote", "AGAINST", "House view")
            snap = self.export(f, data)
            for r in snap["votes"]:                                # an export saved under older wording
                r["decision"] = {"override": "overrode policy"}.get(r["decision"], r["decision"])
            (self.exp / fid / f"{snap['_export']['id']}.json").write_text(json.dumps(snap), encoding="utf-8")
            self.assertFalse(self.status(f, data)["outdated"], fid)

    def test_reason_and_new_decisions_count(self):
        f = base("starbucks-2026-def14a")
        data = decisions.apply(self.dec, f, ["5"], "vote", "AGAINST", "First reason")
        self.export(f, data)
        data = decisions.apply(self.dec, f, ["5"], "vote", "AGAINST", "Better reason")
        data = decisions.apply(self.dec, f, ["3"], "accept")
        ch = {c["item"]: c["changes"] for c in self.status(f, data)["changes"]}
        self.assertEqual(ch, {"5": ["reason changed"], "3": ["vote: none → FOR"]})

    def test_reanalysis_outdates_the_export(self):
        f = base("starbucks-2026-def14a")
        data = decisions.apply(self.dec, f, ["4"], "accept")
        self.export(f, data)
        after = run("starbucks-2026-def14a", supermajority_is_majority_voting=False)
        ch = {c["item"]: c["changes"] for c in exports.status(self.exp, after, data, RULE_NAMES)["changes"]}
        self.assertEqual(ch["4"][0], "vote: FOR → none (needs re-deciding: recommendation changed)")
        self.assertIn("policy recommendation: FOR → REVIEW", ch["4"])

    def test_history_keeps_every_export_newest_first(self):
        f = base("alphabet-2026-def14a")
        a = self.export(f, {}, "csv")
        data = decisions.apply(self.dec, f, ["2"], "accept")
        b = self.export(f, data, "json")                           # likely the same second: unique id
        hist = self.status(f, data)["history"]
        self.assertEqual([h["id"] for h in hist], [b["_export"]["id"], a["_export"]["id"]])
        self.assertNotEqual(a["_export"]["id"], b["_export"]["id"])
        self.assertEqual((hist[0]["format"], hist[0]["decided"], hist[0]["total"]), ("json", 1, len(f["items"])))
        self.assertFalse(hist[1]["complete"])
        self.assertTrue(hist[0]["exported_at"])

    def test_past_export_downloads_as_it_was(self):
        for fid in FIDS:
            f = base(fid)
            n = first_policy_item(f)
            data = decisions.apply(self.dec, f, [n], "accept")
            snap = self.export(f, data)
            then_csv = decisions.to_csv(decisions.export_rows(f, data, RULE_NAMES))
            decisions.apply(self.dec, f, [n], "vote", "AGAINST", "Changed my mind")
            saved = exports.get(self.exp, fid, snap["_export"]["id"])
            body, ctype = exports.as_file(saved, "csv")
            self.assertEqual(body.decode("utf-8-sig"), then_csv, fid)
            self.assertIn("text/csv", ctype)
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
            self.assertEqual(next(r for r in rows if r["item"] == n)["final_vote"], "FOR", fid)
            js = json.loads(exports.as_file(saved, "json")[0])
            self.assertNotIn("_export", js)
            self.assertEqual(js["exported_at"], snap["exported_at"])

    def test_bad_requests(self):
        f = base("amazon-2026-def14a")
        with self.assertRaises(ValueError):
            self.export(f, {}, "xlsx")
        self.assertIsNone(exports.get(self.exp, f["id"], "../decisions"))
        self.assertIsNone(exports.get(self.exp, f["id"], "nope"))

    def test_export_id_from_time(self):
        f = base("amazon-2026-def14a")
        with mock.patch.object(decisions, "_now", return_value="2026-10-26T21:00:00+00:00"):
            ids = [self.export(f, {})["_export"]["id"] for _ in range(2)]
        self.assertEqual(ids, ["2026-10-26T21-00-00Z", "2026-10-26T21-00-00Z-2"])


if __name__ == "__main__":
    unittest.main()
