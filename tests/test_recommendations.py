"""End-to-end: facts and recommendations for the three sample filings,
plus the failure paths (LLM off, disagreement, bad quote, keywords missing)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import ai  # noqa: E402
import facts as facts_mod  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
import rules  # noqa: E402
from classify import merge  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import SETTINGS  # noqa: E402

RAW = {f: (ROOT / "data" / "filings" / f"{f}.htm").read_text(encoding="utf-8", errors="ignore")
       for f in ("starbucks-2026-def14a", "alphabet-2026-def14a", "amazon-2026-def14a")}


def run(fid, **env):
    with mock.patch.dict(os.environ, {"LLM_MODE": "stub", **env}):
        filing, pages = analyze(fid, RAW[fid])
    return filing, {i["number"]: i for i in filing["items"]}, pages


class Facts(unittest.TestCase):
    def test_fees_use_latest_year_even_when_listed_second(self):
        f, _, _ = run("alphabet-2026-def14a")
        fees = f["facts"]["fees"]
        self.assertEqual(fees["year"], 2025)
        self.assertEqual(fees["audit"], 37900)           # not 31,130 (2024)
        self.assertEqual(fees["other"], 19400)

    def test_fees_dollars_vs_thousands(self):
        f, _, _ = run("amazon-2026-def14a")
        self.assertEqual(f["facts"]["fees"]["units"], "dollars")
        self.assertEqual(f["facts"]["fees"]["audit"], 47_260_000)
        f, _, _ = run("starbucks-2026-def14a")
        self.assertEqual(f["facts"]["fees"]["units"], "thousands")

    def test_pay_table(self):
        f, _, _ = run("starbucks-2026-def14a")
        pay = f["facts"]["pay"]
        self.assertEqual(pay["ceos_by_year"][2024], ["Laxman Narasimhan", "Rachel Ruggeri", "Brian Niccol"])
        self.assertEqual(pay["tsr_basis"], "rolling 3-year")
        f, _, _ = run("amazon-2026-def14a")
        self.assertEqual(f["facts"]["pay"]["sct"]["Jassy"][2025], 2_069_861)
        self.assertEqual(f["facts"]["pay"]["tsr_value"][2022], 52)

    def test_attendance(self):
        for fid in RAW:
            f, _, _ = run(fid)
            self.assertTrue(f["facts"]["attendance"]["all_at_least_75"], fid)


class Recommendations(unittest.TestCase):
    def test_starbucks(self):
        _, it, _ = run("starbucks-2026-def14a")
        self.assertEqual(it["1a"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["2"]["recommendation"]["vote"], "FLAG")      # CEO changed
        self.assertEqual(it["3"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["4"]["recommendation"]["vote"], "FOR")       # majority voting; board: no rec.
        for n in "56789":
            self.assertEqual(it[n]["recommendation"]["vote"], "FLAG")

    def test_alphabet(self):
        _, it, _ = run("alphabet-2026-def14a")
        self.assertEqual(it["2"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["3"]["recommendation"]["vote"], "FLAG")      # equity plan: not covered
        self.assertEqual(it["4"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["7"]["recommendation"]["vote"], "FLAG")      # "equal voting" is not majority voting

    def test_amazon(self):
        _, it, _ = run("amazon-2026-def14a")
        self.assertEqual(it["1k"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["2"]["recommendation"]["vote"], "FOR")
        self.assertEqual(it["3"]["recommendation"]["vote"], "FOR")
        self.assertIn("rose", it["3"]["recommendation"]["steps"][0]["text"])

    def test_settings_change_outcome(self):
        f, _, _ = run("starbucks-2026-def14a")
        s = dict(SETTINGS, on_ceo_change="current_ceo")
        r = rules.say_on_pay(f["facts"]["pay"], s)
        self.assertEqual(r["vote"], "FOR")               # Niccol's own pay fell
        f, _, _ = run("alphabet-2026-def14a")
        r = rules.auditor(f["facts"]["fees"], dict(SETTINGS, non_audit_includes_audit_related=False))
        self.assertIn("21.29M", r["headline"])           # tax + other only

    def test_pay_up_tsr_down_is_against(self):
        fact = {"status": "found", "fiscal_year": 2025, "ceos_by_year": {2025: ["X"], 2024: ["X"]},
                "sct": {"X": {2025: 12e6, 2024: 10e6}}, "cap": {},
                "tsr_value": {2025: 90, 2024: 120}, "tsr_basis": "rolling 3-year", "evidence": []}
        self.assertEqual(rules.say_on_pay(fact, SETTINGS)["vote"], "AGAINST")

    def test_auditor_against(self):
        fact = {"status": "found", "year": 2025, "units": "dollars", "audit": 100,
                "audit_related": 20, "tax": 50, "other": 40, "evidence": []}
        self.assertEqual(rules.auditor(fact, SETTINGS)["vote"], "AGAINST")


class FailurePaths(unittest.TestCase):
    def test_llm_off_uses_keywords_and_says_so(self):
        _, it, _ = run("starbucks-2026-def14a", LLM_MODE="off")
        self.assertEqual(it["3"]["classification"]["status"], "keywords_only")
        self.assertEqual(it["3"]["recommendation"]["vote"], "FOR")

    def test_disagreement_flags(self):
        bad = {"classify": {"items": [{"number": "3", "category": "other", "direction": "n/a", "reason": "x",
               "quote": "The board of directors recommends a vote FOR the ratification of the selection of Deloitte"}]},
               "locate": {"attendance": ["29"], "fees": ["84"], "pay": ["80"]}}
        with mock.patch("llm.stub_answer", lambda fid, task: bad.get(task)):
            _, it, _ = run("starbucks-2026-def14a")
        self.assertEqual(it["3"]["classification"]["status"], "disagree")
        self.assertEqual(it["3"]["recommendation"]["vote"], "FLAG")

    def test_made_up_quote_is_not_trusted(self):
        bad = {"classify": {"items": [{"number": "5", "category": "majority_voting", "direction": "toward",
               "reason": "x", "quote": "Shareholders request majority voting for all matters immediately."}]}}
        with mock.patch("llm.stub_answer", lambda fid, task: bad.get(task)):
            _, it, _ = run("starbucks-2026-def14a")
        self.assertEqual(it["5"]["classification"]["status"], "unverified")
        self.assertEqual(it["5"]["recommendation"]["vote"], "FLAG")

    def test_locator_disagreement_flags_the_rule(self):
        bad = {"locate": {"attendance": ["29"], "fees": ["12"], "pay": ["80"]}}
        with mock.patch("llm.stub_answer", lambda fid, task: bad.get(task)):
            _, it, _ = run("starbucks-2026-def14a")
        self.assertEqual(it["3"]["recommendation"]["vote"], "FLAG")

    def test_keywords_miss_llm_finds_page_code_reads_it(self):
        with mock.patch.dict(facts_mod.LOCATOR_KEYWORDS, {"fees": [r"no such phrase anywhere"]}):
            f, it, _ = run("alphabet-2026-def14a")
        fees = f["facts"]["fees"]
        self.assertEqual(fees["locator"], "llm_only")
        self.assertEqual(fees["audit"], 37900)

    def test_attendance_with_exceptions_goes_to_llm_and_is_verified(self):
        f, items, pages = run("starbucks-2026-def14a")
        page = pages[f["facts"]["attendance"]["evidence"][0]["page_index"]]
        page.lines.append("Each director attended at least 75% of meetings, except Jane Roe, who attended 60% of meetings.")
        fact = facts_mod.find_attendance([page])
        self.assertEqual(fact["status"], "needs_review")
        answer = {"all_at_least_75": False, "below_75": ["Jane Roe"],
                  "quote": "except Jane Roe, who attended 60% of meetings."}
        with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}), \
                mock.patch("llm.stub_answer", lambda fid, task: answer if task == "attendance" else None):
            got, _ = ai.read_attendance("starbucks-2026-def14a", [page])
        self.assertTrue(got["quote_verified"])
        fact.update(status="found", all_at_least_75=False, below_75=got["below_75"])
        noms = [rules.nominee_name(i) for i in items.values() if i["policy_rule"] == "DIRECTORS"] + ["Jane Roe"]
        self.assertEqual(rules.directors("Jane Roe", fact, SETTINGS, noms)["vote"], "AGAINST")
        self.assertEqual(rules.directors("Ritch Allison", fact, SETTINGS, noms)["vote"], "FOR")
        # Jane Roe isn't on the real ballot: a name that matches no nominee is flagged, not ignored.
        self.assertEqual(rules.directors("Ritch Allison", fact, SETTINGS, noms[:-1])["vote"], "FLAG")

    def test_missing_fact_never_defaults_to_for(self):
        r = rules.auditor({"status": "missing", "evidence": [], "note": "No auditor fee table found."}, SETTINGS)
        self.assertEqual(r["vote"], "FLAG")

    def test_llm_names_not_in_its_quote_are_not_trusted(self):
        f, _, pages = run("starbucks-2026-def14a")
        page = pages[f["facts"]["attendance"]["evidence"][0]["page_index"]]
        page.lines.append("Each director attended at least 75% of meetings, except Jane Roe, who attended 60% of meetings.")
        answer = {"all_at_least_75": False, "below_75": ["Ritch Allison"],          # not who the quote names
                  "quote": "except Jane Roe, who attended 60% of meetings."}
        with mock.patch.dict(os.environ, {"LLM_MODE": "stub"}), \
                mock.patch("llm.stub_answer", lambda fid, task: answer if task == "attendance" else None):
            facts = pipeline._cross_check_facts("starbucks-2026-def14a", pages, facts_mod.find_all(pages))
        self.assertEqual(facts["attendance"]["status"], "needs_review")
        self.assertEqual(rules.directors("Ritch Allison", facts["attendance"], SETTINGS)["vote"], "FLAG")


def fact_naming(*below):
    return {"status": "found", "locator": "agree", "all_at_least_75": False, "below_75": list(below),
            "evidence": [{"page": "1", "page_index": 0, "quote": "attendance sentence", "find": "x"}]}


class DirectorNames(unittest.TestCase):
    """A name the filing gives must match exactly one nominee, or every nominee is flagged."""

    def vote(self, name, nominees, *below):
        return rules.directors(name, fact_naming(*below), SETTINGS, nominees)["vote"]

    def test_real_nominees_with_title_or_suffix(self):
        for fid in RAW:
            _, items, _ = run(fid)
            noms = [rules.nominee_name(i) for i in items.values() if i["policy_rule"] == "DIRECTORS"]
            self.assertGreater(len(noms), 5, fid)
            target = noms[0]
            for given in (target, f"Mr. {target.split()[-1]}", f"{target}, Jr."):
                with self.subTest(fid=fid, given=given):
                    self.assertEqual(self.vote(target, noms, given), "AGAINST")
                    for other in noms[1:]:
                        self.assertEqual(self.vote(other, noms, given),
                                         "FLAG" if rules.surname(other) == rules.surname(target) else "FOR")

    def test_suffix_on_the_ballot_name(self):
        noms = ["Robert Smith, Jr.", "Ann Lee"]
        self.assertEqual(self.vote("Robert Smith, Jr.", noms, "Robert Smith"), "AGAINST")
        self.assertEqual(self.vote("Ann Lee", noms, "Robert Smith"), "FOR")

    def test_no_substring_matches(self):
        noms = ["Jon Lee", "Ann Fleet"]
        self.assertEqual(self.vote("Jon Lee", noms, "Ann Fleet"), "FOR")
        self.assertEqual(self.vote("Ann Fleet", noms, "Ann Fleet"), "AGAINST")

    def test_unmatched_or_ambiguous_name_flags_everyone(self):
        noms = ["Jon Lee", "Ann Fleet", "Mary Smith", "Tom Smith"]
        for n in noms:
            self.assertEqual(self.vote(n, noms, "Kathleen Moore"), "FLAG")     # matches no nominee
            self.assertEqual(self.vote(n, noms, "Ms. Smith"), "FLAG")          # matches two


class KeywordOnlyClassification(unittest.TestCase):
    def test_shareholder_proposal_is_not_voted_on_keywords_alone(self):
        for title in ("Shareholder Proposal Requesting Shareholder Advisory Vote on Golden Parachute Pay",
                      "Shareholder Proposal to Require Ratification of Auditor Rotation by the Accounting Firm"):
            m = merge(title, None, "LLM off", shareholder=True)
            self.assertEqual((m["status"], m["rule"]), ("unconfirmed", "FLAG"), title)

    def test_item_the_llm_skipped_is_flagged(self):
        m = merge("Ratification of Appointment of Independent Registered Public Accounting Firm", None,
                  "LLM gave no answer for this item", skipped=True)
        self.assertEqual((m["status"], m["rule"]), ("unconfirmed", "FLAG"))

    def test_llm_off_all_filings(self):
        for fid in RAW:
            _, items, _ = run(fid, LLM_MODE="off")
            for n, it in items.items():
                with self.subTest(fid=fid, item=n):
                    if it["proponent"] == "shareholder":
                        self.assertEqual(it["recommendation"]["vote"], "FLAG")
                    elif it["classification"]["category"] in ("director_election", "auditor_ratification"):
                        self.assertEqual(it["classification"]["status"], "keywords_only")
                        self.assertEqual(it["recommendation"]["vote"], "FOR")

    def test_llm_skipping_an_item_flags_it(self):
        for fid in RAW:
            stub = llm.stub_answer(fid, "classify")
            _, before, _ = run(fid)
            auditor = next(n for n, i in before.items() if i["classification"]["category"] == "auditor_ratification")
            trimmed = {"items": [a for a in stub["items"] if str(a["number"]) != auditor]}
            real = llm.stub_answer
            with mock.patch("llm.stub_answer", lambda f, task: trimmed if task == "classify" else real(f, task)):
                _, items, _ = run(fid)
            with self.subTest(fid=fid):
                self.assertEqual(before[auditor]["recommendation"]["vote"], "FOR")
                self.assertEqual(items[auditor]["classification"]["status"], "unconfirmed")
                self.assertEqual(items[auditor]["recommendation"]["vote"], "FLAG")


if __name__ == "__main__":
    unittest.main()
