"""filing HTML -> ballot -> facts -> classification -> recommendations."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

import ai
import llm
from classify import merge
from extract import extract_ballot
from facts import find_all, find_attendance, find_fees, find_pay
from links import Linker
from pages import split_pages
from policy import POLICY, SETTINGS
from rules import fold, nominee_name, recommend, surname

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
        # Every name the LLM lists must be in the quote it gave, or the list isn't trusted.
        quoted = set(fold(ans.get("quote", ""))) if ans else set()
        names_ok = bool(ans) and all(surname(n) in quoted for n in ans.get("below_75") or [])
        if ans and ans.get("quote_verified") and ans.get("all_at_least_75") is not None and names_ok:
            att.update(status="found", method="llm", all_at_least_75=ans["all_at_least_75"],
                       below_75=ans.get("below_75") or [],
                       evidence=[{"page": ans["page"], "page_index": ans["page_index"],
                                  "quote": ans["quote"], "find": ans["quote"]}],
                       note="Read by the LLM; quote checked against the filing.")
        elif ans and ans.get("quote_verified") and not names_ok:
            att["note"] += " The LLM named directors who aren't in the sentence it quoted, so its reading isn't trusted."
        elif ans:
            att["note"] += " The LLM's reading couldn't be verified against the filing."
    return facts


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def run_inputs(filing_id: str, raw_html: str, settings: dict) -> dict:
    """Everything a recommendation depends on. If any of it changes, re-analysis
    can tell her which (the filing, a setting, the AI model, or the stub answers)."""
    mode = llm.mode_for(filing_id)
    stub = llm.STUB_DIR / f"{filing_id}.json"
    return {
        "filing_sha256": _sha(raw_html),
        "policy_version": POLICY["version"],
        "settings": dict(settings),
        "llm_mode": mode,
        "llm_model": llm.MODEL if mode == "live" else None,
        "stub_sha256": _sha(stub.read_text(encoding="utf-8")) if mode == "stub" and stub.exists() else None,
    }


def _add_links(filing: dict, pages: list, source_url: str | None) -> None:
    """Point every cited claim, the vote summary and the board's recommendation at
    the exact words in the live filing (SEC.gov), via text-fragment links."""
    linker = Linker(source_url, pages)
    cache: dict = {}

    def link(cite):
        if not cite:
            return None
        key = (cite.get("quote"), tuple(cite.get("terms") or ()), cite["page_index"])
        if key not in cache:
            cache[key] = (linker.quote(cite["quote"], cite["page_index"]) if cite.get("quote")
                          else linker.terms(cite["terms"], cite["page_index"]))
        return cache[key]

    for it in filing["items"]:
        for st in it["recommendation"]["steps"]:
            st["link"] = link(st.get("cite"))
        for key in ("summary", "board_rec_evidence"):
            ev = it.get(key)
            if ev:
                ev["link"] = link({"quote": ev["quote"], "page_index": ev["page_index"], "page": ev["page"]})


def analyze(filing_id: str, raw_html: str, settings: dict | None = None,
            source_url: str | None = None) -> tuple[dict, list]:
    settings = dict(SETTINGS if settings is None else settings)
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
                          "shareholder": it["proponent"] == "shareholder",
                          "page_indexes": list(range(s0, min(max(nxt, s0 + 1), s0 + 6, len(pages))))})
    llm_cls, cls_err = ai.classify_items(filing_id, proposals, pages)
    by_prop = {p["number"]: merge(p["title"], llm_cls.get(p["number"]), cls_err if not llm_cls else
                                  ("LLM gave no answer for this item" if p["number"] not in llm_cls else None),
                                  shareholder=p["shareholder"],
                                  skipped=bool(llm_cls) and p["number"] not in llm_cls)
               for p in proposals}

    nominees = [nominee_name(it) for it in filing["items"] if by_prop[it["proposal"]]["rule"] == "DIRECTORS"]
    for it in filing["items"]:
        cls = by_prop[it["proposal"]]
        it["classification"] = cls
        it["policy_rule"] = cls["rule"]
        it["recommendation"] = recommend(it, cls, facts, settings, nominees)
    _add_links(filing, pages, source_url)

    filing["facts"] = facts
    filing["llm_mode"] = llm.mode_for(filing_id)
    filing["llm_model"] = llm.MODEL if filing["llm_mode"] == "live" else None
    filing["settings"] = settings
    filing["source_url"] = source_url
    filing["run"] = {**run_inputs(filing_id, raw_html, settings),
                     "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return filing, pages
