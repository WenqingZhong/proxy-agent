"""The three LLM jobs. None of them chooses a vote.

  classify_items   what kind of item each proposal is (cross-checked with keywords)
  locate_facts     which pages hold attendance / fees / pay (cross-checked with keywords)
  read_attendance  read an attendance sentence that names exceptions (prose)

Every answer that cites text is checked: the quote must appear on the page.
"""
from __future__ import annotations

import re

import llm
from pages import Page

CATEGORIES = [
    "director_election", "say_on_pay", "auditor_ratification",
    "declassify_board", "majority_voting", "other",
]


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_on_pages(quote: str, pages: list[Page]) -> Page | None:
    q = _norm(quote or "")
    if len(q) < 12:
        return None
    for p in pages:
        if q in _norm(" ".join(p.lines)):
            return p
    return None


# --------------------------------------------------------------------------- #
# 1. Classification

CLASSIFY_SYSTEM = (
    "You sort proxy ballot items into categories for a voting-policy engine. "
    "You never recommend votes. Answer with JSON only."
)

CLASSIFY_PROMPT = """Classify each ballot item below.

Categories:
- director_election: election of directors
- say_on_pay: advisory vote on executive compensation (NOT say-on-frequency, NOT equity plans)
- auditor_ratification: ratify the independent auditor / accounting firm
- declassify_board: move to annual election of all directors / eliminate a classified (staggered) board
- majority_voting: adopt a majority vote standard, or replace supermajority requirements with majority voting
- other: anything else, including items that only sound related (e.g. dual-class / "equal voting" proposals)

Also give "direction": "toward" if a FOR vote moves the company toward the category's
reform (e.g. adopting majority voting), "away" if a FOR vote moves away from it, else "n/a".

For each item return a verbatim "quote" (one sentence, copied exactly) from its text that
justifies the category, and a short "reason".

Return: {{"items": [{{"number": "...", "category": "...", "direction": "...", "reason": "...", "quote": "..."}}]}}

Items:
{items}
"""


def classify_items(filing_id: str, proposals: list[dict], pages: list[Page]) -> tuple[dict, str | None]:
    """proposals: [{number, title, page_indexes: [...]}]. Returns ({number: answer}, error)."""
    mode = llm.mode_for(filing_id)
    if mode == "off":
        return {}, "LLM off"
    if mode == "stub":
        data, err = llm.stub_answer(filing_id, "classify"), None
    else:
        blocks = []
        for prop in proposals:
            text = " ".join(" ".join(pages[i].lines) for i in prop["page_indexes"][:2])
            blocks.append(f'### Item {prop["number"]}: {prop["title"]}\n{text[:2500]}')
        data, err = llm.ask_json(CLASSIFY_SYSTEM, CLASSIFY_PROMPT.format(items="\n\n".join(blocks)), 3000)
    if not data:
        return {}, err or "No answer"

    out = {}
    by_num = {p["number"]: p for p in proposals}
    for ans in data.get("items", []):
        num = str(ans.get("number", "")).strip()
        if num not in by_num:
            continue
        cat = ans.get("category") if ans.get("category") in CATEGORIES else "other"
        scope = [pages[i] for i in by_num[num]["page_indexes"]]
        page = quote_on_pages(ans.get("quote", ""), scope)
        out[num] = {
            "category": cat,
            "direction": ans.get("direction", "n/a"),
            "reason": ans.get("reason", ""),
            "quote": ans.get("quote", ""),
            "quote_verified": page is not None,
            "page": page.label if page else None,
            "page_index": page.index if page else None,
            "source": mode,
        }
    return out, None


# --------------------------------------------------------------------------- #
# 2. Locator

LOCATE_SYSTEM = "You find where facts are in a long SEC filing. Answer with JSON only."

LOCATE_PROMPT = """Below is an index of a DEF 14A proxy statement: one entry per page with its
page label, first lines, and table row labels.

Which pages contain:
- "attendance": the statement of directors' attendance at board and committee meetings (the 75% threshold)
- "fees": the table of fees paid to the independent auditor (audit, audit-related, tax, all other)
- "pay": the pay-versus-performance table (compensation actually paid and total shareholder return by year)

Return {{"attendance": [labels], "fees": [labels], "pay": [labels]}}, at most 3 labels each, best first.

Index:
{index}
"""


def page_index_text(pages: list[Page]) -> str:
    rows = []
    for p in pages:
        head = " / ".join(l[:90] for l in p.lines[:3])
        labels = []
        for t in p.tables:
            labels += [r[0][:40] for r in t[:8] if r and not re.match(r"^[\d$.,()%\s—-]+$", r[0])]
        rows.append(f"[{p.label}] {head}" + (f" || table rows: {'; '.join(labels[:10])}" if labels else ""))
    return "\n".join(rows)


def locate_facts(filing_id: str, pages: list[Page]) -> tuple[dict, str | None]:
    """Returns ({fact: [page_index...]}, error)."""
    mode = llm.mode_for(filing_id)
    if mode == "off":
        return {}, "LLM off"
    if mode == "stub":
        data, err = llm.stub_answer(filing_id, "locate"), None
    else:
        data, err = llm.ask_json(LOCATE_SYSTEM, LOCATE_PROMPT.format(index=page_index_text(pages)), 800)
    if not data:
        return {}, err or "No answer"
    by_label = {p.label: p.index for p in pages}
    return {k: [by_label[str(l)] for l in data.get(k, []) if str(l) in by_label]
            for k in ("attendance", "fees", "pay")}, None


# --------------------------------------------------------------------------- #
# 3. Read attendance prose

ATTEND_SYSTEM = "You extract facts from SEC filings exactly as written. Answer with JSON only."

ATTEND_PROMPT = """From the text below, report board/committee meeting attendance for the last fiscal year.

Return {{"all_at_least_75": true|false|null,
         "below_75": ["full names of directors who attended fewer than 75%"],
         "quote": "the exact sentence you relied on, copied verbatim"}}
Use null if the text doesn't say.

Text:
{text}
"""


def read_attendance(filing_id: str, pages: list[Page]) -> tuple[dict | None, str | None]:
    mode = llm.mode_for(filing_id)
    if mode == "off":
        return None, "LLM off"
    if mode == "stub":
        data, err = llm.stub_answer(filing_id, "attendance"), None
    else:
        text = "\n".join("\n".join(p.lines) for p in pages)[:12000]
        data, err = llm.ask_json(ATTEND_SYSTEM, ATTEND_PROMPT.format(text=text), 600)
    if not data:
        return None, err or "No answer"
    page = quote_on_pages(data.get("quote", ""), pages)
    data["quote_verified"] = page is not None
    data["page"] = page.label if page else None
    data["page_index"] = page.index if page else None
    data["source"] = mode
    return data, None
