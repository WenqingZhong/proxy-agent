"""filing HTML -> ballot -> facts -> classification -> recommendations."""
from __future__ import annotations

import re

import ai
import llm
from classify import merge
from extract import extract_ballot
from facts import find_all, find_attendance, find_fees, find_pay
from pages import split_pages
from policy import SETTINGS
from rules import recommend

FINDERS = {"attendance": find_attendance, "fees": find_fees, "pay": find_pay}


def _cross_check_facts(filing_id, pages, facts):
    loc, err = ai.locate_facts(filing_id, pages)
    for name, fact in facts.items():
        llm_idx = loc.get(name) if loc else None
        fact["llm_pages"] = [pages[i].label for i in (llm_idx or [])]
        if llm_idx is None:
            fact["locator"] = "keywords_only"
            fact["locator_note"] = f"LLM locator not used ({err})."
        elif fact["status"] == "missing":
            # Keywords failed: let the LLM say where to look, then read it with code.
            retry = FINDERS[name](pages, llm_idx) if llm_idx else None
            if retry and retry["status"] != "missing":
                retry.update(keyword_pages=fact["keyword_pages"], llm_pages=fact["llm_pages"],
                             method="code", locator="llm_only",
                             locator_note="Keywords missed this; the LLM found the page and code read it.")
                facts[name] = fact = retry
            else:
                fact["locator"] = "neither"
                fact["locator_note"] = "Neither keywords nor the LLM found it."
        else:
            ev_idx = fact["evidence"][0]["page_index"]
            fact["locator"] = "agree" if ev_idx in llm_idx else "disagree"
            fact["locator_note"] = ("Keywords and LLM point to the same page." if fact["locator"] == "agree"
                                    else "Keywords and LLM point to different pages.")

    # Prose the code can't settle: let the LLM read it, then verify the quote.
    att = facts["attendance"]
    if att["status"] == "needs_review" and att["evidence"]:
        page = pages[att["evidence"][0]["page_index"]]
        ans, err = ai.read_attendance(filing_id, [page])
        if ans and ans.get("quote_verified") and ans.get("all_at_least_75") is not None:
            att.update(status="found", method="llm", all_at_least_75=ans["all_at_least_75"],
                       below_75=ans.get("below_75") or [],
                       evidence=[{"page": ans["page"], "page_index": ans["page_index"],
                                  "quote": ans["quote"], "find": ans["quote"]}],
                       note="Read by the LLM; quote checked against the filing.")
        elif ans:
            att["note"] += " The LLM's reading couldn't be verified against the filing."
    return facts


def analyze(filing_id: str, raw_html: str) -> tuple[dict, list]:
    pages = split_pages(raw_html)
    filing = extract_ballot(filing_id, pages).to_dict()
    facts = _cross_check_facts(filing_id, pages, find_all(pages))

    # One classification per proposal (all nominees share proposal 1's).
    starts = sorted({it["section"]["page_index"] for it in filing["items"] if it["section"]})
    proposals, seen = [], set()
    for it in filing["items"]:
        base = re.sub(r"[a-z]+$", "", it["number"])
        it["proposal"] = base
        if base in seen:
            continue
        seen.add(base)
        s0 = it["section"]["page_index"]
        nxt = next((s for s in starts if s > s0), s0 + 3)
        proposals.append({"number": base, "title": it["proposal_title"] or it["title"],
                          "page_indexes": list(range(s0, min(max(nxt, s0 + 1), s0 + 6, len(pages))))})
    llm_cls, cls_err = ai.classify_items(filing_id, proposals, pages)
    by_prop = {p["number"]: merge(p["title"], llm_cls.get(p["number"]), cls_err if not llm_cls else
                                  ("LLM gave no answer for this item" if p["number"] not in llm_cls else None))
               for p in proposals}

    for it in filing["items"]:
        cls = by_prop[it["proposal"]]
        it["classification"] = cls
        it["policy_rule"] = cls["rule"]
        it["recommendation"] = recommend(it, cls, facts, SETTINGS)

    filing["facts"] = facts
    filing["llm_mode"] = llm.mode_for(filing_id)
    filing["llm_model"] = llm.MODEL if filing["llm_mode"] == "live" else None
    filing["settings"] = SETTINGS
    return filing, pages
