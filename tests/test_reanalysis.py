"""Re-analysis: when a setting, the filing or the AI changes, decisions she made
earlier are checked against the new recommendations. Nothing changes on its own."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import decisions  # noqa: E402
import llm  # noqa: E402
import policy  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import POLICY, SETTINGS  # noqa: E402

FIDS = ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a")
RULE_NAMES = {r["id"]: r["section"] for r in POLICY["rules"]}
RAW = {f: (ROOT / "data" / "filings" / f"{f}.htm").read_text(encoding="utf-8", errors="ignore") for f in FIDS}
BASE = {}


def run(fid, raw=None, **settings):
    with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}):
        return analyze(fid, raw or RAW[fid], dict(SETTINGS, **settings))[0]


def base(fid):
    if fid not in BASE:
        BASE[fid] = run(fid)
    return BASE[fid]


def item(f, n):
    return next(i for i in f["items"] if i["number"] == n)


def auditor_item(f):
    return next(i for i in f["items"] if i["recommendation"]["rule"] == "AUDITOR")["number"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def decide_all(self, f):
        """Accept every policy vote; vote AGAINST on every flagged item."""
        data = {}
        for it in f["items"]:
            if it["recommendation"]["vote"] == "FLAG":
                data = decisions.apply(self.root, f, [it["number"]], "vote", "AGAINST", "House view")
            else:
                data = decisions.apply(self.root, f, [it["number"]], "accept")
        return data


class NothingChanged(Base):
    def test_rerun_with_same_inputs_flags_nothing(self):
        # A false alarm here would make the whole feature noise.
        for fid in FIDS:
            data = self.decide_all(base(fid))
            again = run(fid)
            st = decisions.states(again, data)
            self.assertEqual(st["decided"], st["total"], fid)
            self.assertFalse(any(st["review"].values()), (fid, st["review"]))
            self.assertEqual(st["causes"], [], fid)
            rep = decisions.reanalysis_report(base(fid), again, data)
            self.assertEqual((rep["causes"], rep["recommendations_changed"]), ([], []), fid)


class SettingChanged(Base):
    def test_vote_change_conflicts_with_her_vote(self):
        f = base("starbucks-2026-def14a")
        data = decisions.apply(self.root, f, ["4"], "accept")                       # FOR
        after = run("starbucks-2026-def14a", supermajority_is_majority_voting=False)
        s = decisions.states(after, data)
        self.assertEqual(item(after, "4")["recommendation"]["vote"], "FLAG")
        self.assertEqual((s["items"]["4"]["state"], s["items"]["4"]["change"]), ("stale", "conflicts"))
        self.assertIn("4", s["undecided"])
        self.assertEqual(s["review"]["conflicts"], ["4"])
        self.assertTrue(any("majority voting" in c and "Yes → No" in c for c in s["causes"]))
        self.assertEqual(s["items"]["4"]["causes"], [])                         # setting is in its own changes
        self.assertTrue(any(c.startswith("Recommendation: FOR → REVIEW") for c in s["items"]["4"]["changes"]))
        rep = decisions.reanalysis_report(f, after, data)
        self.assertEqual(rep["recommendations_changed"],
                         [{"number": "4", "title": item(after, "4")["title"], "before": "FOR", "after": "REVIEW"}])

    def test_now_agrees_needs_a_click_and_keeps_her_reason(self):
        f = base("starbucks-2026-def14a")
        self.assertEqual(item(f, "2")["recommendation"]["vote"], "FLAG")           # CEO changed
        data = decisions.apply(self.root, f, ["2"], "vote", "FOR", "Niccol's own pay fell")
        after = run("starbucks-2026-def14a", on_ceo_change="current_ceo")
        self.assertEqual(item(after, "2")["recommendation"]["vote"], "FOR")
        s = decisions.states(after, data)["items"]["2"]
        self.assertEqual((s["state"], s["change"], s["final_vote"]), ("stale", "now_agrees", None))
        data = decisions.apply(self.root, after, ["2"], "confirm")
        s = decisions.states(after, data)["items"]["2"]
        self.assertEqual((s["state"], s["final_vote"], s["reason"]), ("accepted", "FOR", "Niccol's own pay fell"))
        self.assertTrue(s["reconfirmed_after"])
        self.assertEqual([h["vote"] for h in s["history"]], ["FOR"])

    def test_same_vote_new_basis_notifies_but_still_counts(self):
        for fid in FIDS:
            f = base(fid)
            n = auditor_item(f)
            data = decisions.apply(self.root, f, [n], "accept")
            after = run(fid, non_audit_includes_audit_related=False)
            st = decisions.states(after, data)
            s = st["items"][n]
            self.assertEqual((s["state"], s["final_vote"]), ("accepted", "FOR"), fid)
            self.assertTrue(s["basis_changed"], fid)
            self.assertIn(n, st["review"]["basis_changed"], fid)
            self.assertNotIn(n, st["undecided"], fid)
            row = next(r for r in decisions.export_rows(after, data, RULE_NAMES) if r["item"] == n)
            self.assertIn("(setting): Yes: audit-related + tax + all other → No: tax + all other only",
                          row["changed_since_decided"], fid)
            # Re-confirming clears the notice.
            data = decisions.apply(self.root, after, [n], "confirm")
            self.assertFalse(decisions.states(after, data)["items"][n].get("basis_changed"), fid)

    def test_confirm_refuses_a_different_vote_without_reason(self):
        f = base("starbucks-2026-def14a")
        data = {"4": {"vote": "AGAINST", "reason": "", "policy_vote": "AGAINST", "policy_rule": "X",
                      "decided_at": "2026-10-01T00:00:00+00:00"}}
        (self.root / f"{f['id']}.json").write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            decisions.apply(self.root, f, ["4"], "confirm")
        with self.assertRaises(ValueError):
            decisions.apply(self.root, f, ["5"], "confirm")                        # nothing to keep


class FilingUpdated(Base):
    FEE = "47,260,000"

    def test_corrected_figure_flips_the_vote(self):
        fid = "amazon-2026-def14a"
        f = base(fid)
        n = auditor_item(f)
        data = decisions.apply(self.root, f, [n], "accept")
        # Corrected audit fee, and the total with it (otherwise the fee table doesn't add up: FLAG).
        raw = RAW[fid].replace(self.FEE, "1,000,000", 1).replace("59,256,965", "12,996,965", 1)
        after = run(fid, raw)
        self.assertEqual(item(after, n)["recommendation"]["vote"], "AGAINST")
        st = decisions.states(after, data)
        self.assertEqual(st["items"][n]["change"], "conflicts")
        self.assertIn("The filing was updated", st["causes"])
        self.assertTrue(any("Auditor fees, audit: 47,260,000 → 1,000,000" in c for c in st["items"][n]["changes"]))

    def test_small_correction_is_caught_even_when_rounding_hides_it(self):
        fid = "amazon-2026-def14a"
        f = base(fid)
        n = auditor_item(f)
        data = decisions.apply(self.root, f, [n], "accept")
        raw = RAW[fid].replace(self.FEE, "47,260,400", 1).replace("59,256,965", "59,257,365", 1)
        after = run(fid, raw)                                                     # still "$47.26M"
        s = decisions.states(after, data)["items"][n]
        self.assertEqual(s["state"], "accepted")
        self.assertEqual(s["changes"], ["Auditor fees, audit: 47,260,000 → 47,260,400"])

    def test_renumbered_or_removed_items_dont_carry_her_vote(self):
        for fid in FIDS:
            f = base(fid)
            data = self.decide_all(f)
            # A nominee replaced at 1b, and the last item dropped from the ballot.
            items = [dict(i, title="Elect Someone Else") if i["number"] == "1b" else i for i in f["items"][:-1]]
            after = {**f, "items": items}
            st = decisions.states(after, data)
            self.assertEqual(st["items"]["1b"]["change"], "item_changed", fid)
            self.assertIsNone(st["items"]["1b"]["final_vote"], fid)
            self.assertEqual([r["number"] for r in st["review"]["removed"]], [f["items"][-1]["number"]], fid)
            rows = decisions.export_rows(after, data, RULE_NAMES)
            self.assertEqual(len(rows), len(items), fid)
            rep = decisions.reanalysis_report(f, after, data)
            self.assertEqual(rep["items_added"], ["1b"], fid)
            self.assertEqual(rep["items_removed"], ["1b", f["items"][-1]["number"]], fid)


class AIChanged(Base):
    def test_new_ai_answer_moves_an_item_out_of_its_rule(self):
        fid = "amazon-2026-def14a"
        f = base(fid)
        n = auditor_item(f)
        data = decisions.apply(self.root, f, [n], "accept")
        with tempfile.TemporaryDirectory() as stub_dir:
            stub = json.loads((llm.STUB_DIR / f"{fid}.json").read_text(encoding="utf-8"))
            for ans in stub["classify"]["items"]:
                if ans["number"] == n:
                    ans["category"] = "other"
            Path(stub_dir, f"{fid}.json").write_text(json.dumps(stub), encoding="utf-8")
            with mock.patch.object(llm, "STUB_DIR", Path(stub_dir)):
                after = run(fid)
        self.assertEqual(item(after, n)["recommendation"]["vote"], "FLAG")         # keywords and AI disagree
        st = decisions.states(after, data)
        self.assertEqual(st["items"][n]["change"], "conflicts")
        self.assertIn("The AI stub answers were updated", st["causes"])

    def test_model_change_is_named(self):
        old = {"llm_mode": "stub", "llm_model": None, "settings": {}}
        new = {"llm_mode": "live", "llm_model": "claude-sonnet-5-5", "settings": {}}
        self.assertEqual(decisions.run_changes(old, new), ["AI: stub → claude-sonnet-5-5"])
        self.assertEqual(decisions.run_changes(new, {**new, "llm_model": "claude-opus-5-5"}),
                         ["AI: claude-sonnet-5-5 → claude-opus-5-5"])


class OlderDecisions(Base):
    def test_decisions_saved_before_this_feature_still_work(self):
        f = base("starbucks-2026-def14a")
        data = {"3": {"vote": "FOR", "reason": "", "policy_vote": "FOR", "policy_rule": "AUDITOR",
                      "decided_at": "2026-10-01T00:00:00+00:00"}}
        s = decisions.states(f, data)
        self.assertEqual(s["items"]["3"]["state"], "accepted")
        self.assertFalse(any(s["review"].values()))


class Settings(unittest.TestCase):
    def test_saved_settings_override_defaults_and_are_validated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "policy_settings.json"
            self.assertEqual(policy.current_settings(p), SETTINGS)
            policy.set_setting("non_audit_includes_audit_related", "false", p)
            policy.set_setting("ceo_pay_measure", "cap", p)
            cur = policy.current_settings(p)
            self.assertIs(cur["non_audit_includes_audit_related"], False)
            self.assertEqual(cur["ceo_pay_measure"], "cap")
            policy.set_setting("ceo_pay_measure", "sct", p)                         # back to default
            self.assertEqual(json.loads(p.read_text()), {"non_audit_includes_audit_related": False})
            for bad in (("nope", "x"), ("ceo_pay_measure", "salary")):
                with self.assertRaises(ValueError):
                    policy.set_setting(*bad, p)
            view = policy.policy_view(policy.current_settings(p))
            s = next(x for x in view["settings"] if x["id"] == "non_audit_includes_audit_related")
            self.assertEqual((s["value"], s["default"]), (False, True))


if __name__ == "__main__":
    unittest.main()
