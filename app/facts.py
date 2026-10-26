"""Find the facts the policy needs, with code.

Each finder returns a dict with "status":
  found          code read the fact and it passed its checks
  needs_review   something was found but code can't settle it (e.g. attendance
                 sentence names exceptions); goes to the LLM, then the analyst
  missing        nothing found; goes to the LLM locator, then the analyst
and "evidence": [{page, page_index, quote, find}] so the UI can open the page
and highlight the line (`find` is the text to highlight).
"""
from __future__ import annotations

import re

from pages import Page
from tables import year_table, parse_number, norm_header

# --------------------------------------------------------------------------- #
# Keyword locator: which pages probably hold each fact. The LLM locator is
# cross-checked against this.

LOCATOR_KEYWORDS = {
    "attendance": [r"75\s*%|75 percent|seventy-five percent", r"attend"],
    "fees": [r"audit[- ]related fees", r"all other fees"],
    "pay": [r"actually\s+paid", r"pay versus performance|pay vs\.? performance"],
}


def keyword_pages(pages: list[Page], fact: str) -> list[int]:
    pats = [re.compile(p, re.I) for p in LOCATOR_KEYWORDS[fact]]
    out = []
    for p in pages:
        text = re.sub(r"(\w)- (\w)", r"\1\2", p.text)
        if all(rx.search(text) for rx in pats):
            out.append(p.index)
    return out


def _ev(page: Page, quote: str, find: str | None = None, kind: str = "text") -> dict:
    """kind "text": highlight lines matching `find`; "table": highlight the table containing it."""
    return {"page": page.label, "page_index": page.index, "quote": quote, "find": find or quote, "kind": kind}


# --------------------------------------------------------------------------- #
# Attendance

SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z])")
SEVENTY_FIVE = re.compile(r"75\s*%|75 percent|seventy-five percent", re.I)
EXCEPTION = re.compile(
    r"\bexcept\b|\bother than\b|with the exception|did not attend|"
    r"(?:fewer|less) than 75|below 75", re.I)
BLANKET = re.compile(
    r"\b(?:each|all|every)\b(?: of (?:our|the))?(?: (?:incumbent|current|then-serving|of the))*"
    r" (?:directors?|board members?|members of (?:our|the) board)\b[^.]{0,80}?"
    r"\battended\b[^.]{0,20}?\b(?:at least|more than|over|in excess of)\s+75", re.I)


def find_attendance(pages: list[Page], page_indexes: list[int] | None = None) -> dict:
    scope = [pages[i] for i in page_indexes] if page_indexes else pages
    hits = []
    for p in scope:
        for line in p.lines:
            for sent in SENT_SPLIT.split(line):
                if SEVENTY_FIVE.search(sent) and re.search(r"attend", sent, re.I) and re.search(r"meeting", sent, re.I):
                    hits.append((p, sent.strip()))
    if not hits:
        return {"status": "missing", "evidence": [],
                "note": "No sentence about 75% meeting attendance found."}

    blanket = [(p, s) for p, s in hits if BLANKET.search(s) and not EXCEPTION.search(s)]
    with_exceptions = [(p, s) for p, s in hits if EXCEPTION.search(s)]

    if with_exceptions:
        p, s = with_exceptions[0]
        return {"status": "needs_review", "all_at_least_75": None, "below_75": [],
                "evidence": [_ev(p, s)],
                "note": "The attendance statement mentions exceptions; it needs a reading, not a keyword match."}
    if blanket:
        p, s = blanket[0]
        return {"status": "found", "all_at_least_75": True, "below_75": [],
                "evidence": [_ev(p, s)],
                "note": "Filing states every director met the 75% threshold."}
    p, s = hits[0]
    return {"status": "needs_review", "all_at_least_75": None, "below_75": [],
            "evidence": [_ev(p, s)],
            "note": "Found an attendance sentence but couldn't tell whether it covers every director."}


# --------------------------------------------------------------------------- #
# Auditor fees

FEE_ROWS = {
    "audit": re.compile(r"^audit fees\b", re.I),
    "audit_related": re.compile(r"^audit[- ]related fees\b", re.I),
    "tax": re.compile(r"^tax fees\b", re.I),
    "other": re.compile(r"^all other fees\b", re.I),
    "total": re.compile(r"^total(?: fees)?\b", re.I),
}
YEAR_TOKEN = re.compile(r"\b(20\d\d)\b")


def find_fees(pages: list[Page], page_indexes: list[int] | None = None) -> dict:
    scope = [pages[i] for i in page_indexes] if page_indexes else pages
    for p in scope:
        for table in p.tables:
            labels = {k: None for k in FEE_ROWS}
            header_years: list[int] = []
            for row in table:
                key = next((k for k, rx in FEE_ROWS.items() if rx.match(row[0])), None)
                if key is None:
                    ys = [int(y) for c in row for y in YEAR_TOKEN.findall(c)]
                    if len(ys) >= 2 and labels["audit"] is None:
                        header_years = ys
                    continue
                if labels[key] is None:
                    labels[key] = row
            if labels["audit"] is None or labels["other"] is None or not header_years:
                continue

            n = len(header_years)
            values = {}
            for k, row in labels.items():
                if row is None:
                    continue
                nums = [parse_number(c) for c in row[1:]]
                nums = [x for x in nums if x is not None]
                if len(nums) < n:
                    break
                values[k] = dict(zip(header_years, nums[-n:]))
            else:
                year = max(header_years)
                prior = sorted(header_years)[-2] if n > 1 else None
                cur = {k: v[year] for k, v in values.items()}
                units = "thousands" if re.search(r"in thousands", p.text, re.I) else "dollars"
                # Sanity check: categories add up to the stated total.
                parts = sum(cur.get(k, 0) for k in ("audit", "audit_related", "tax", "other"))
                total_ok = "total" not in cur or abs(parts - cur["total"]) <= max(2, 0.001 * cur["total"])
                col_pos = header_years.index(year) + 1
                order_note = "" if col_pos == 1 else f" (column {col_pos} of {n}; this filing lists years oldest first)"
                return {
                    "status": "found" if total_ok else "needs_review",
                    "year": year,
                    "prior_year": prior,
                    "units": units,
                    "audit": cur.get("audit"),
                    "audit_related": cur.get("audit_related", 0.0),
                    "tax": cur.get("tax", 0.0),
                    "other": cur.get("other", 0.0),
                    "total": cur.get("total"),
                    "by_year": values,
                    "evidence": [_ev(p, f"Fee table, fiscal {year}{order_note}: " + "; ".join(
                        f"{k.replace('_', '-')} {v[year]:,.0f}" for k, v in values.items()),
                        find=labels["audit"][0], kind="table")],
                    "note": "" if total_ok else "Fee categories don't add up to the table's total.",
                }
    return {"status": "missing", "evidence": [], "note": "No auditor fee table found."}


# --------------------------------------------------------------------------- #
# Pay versus performance: CEO pay and TSR

def _col_kind(header: str) -> str | None:
    h = header.lower()
    if re.search(r"average|non-?\s*(peo|ceo)|other neo", h):
        return None
    if re.search(r"actually paid", h):
        return "cap"
    if re.search(r"summary compensation|\bsct\b", h):
        return "sct"
    if re.search(r"total shareholder return|\btsr\b", h) and not re.search(r"peer|relative|nyse|s&p|index|nasdaq", h):
        return "tsr"
    return None


def _person(header: str) -> str:
    h = norm_header(header)
    m = re.search(r"\(([A-Z][A-Za-z.'’ -]+)\)\s*$", h)          # "... CEO (Jassy)"
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:for|to)\s+(.+)$", h.split(" / ")[-1])
    name = m.group(1) if m else h
    name = re.sub(r"\(\$\)|\$", "", name).strip()
    return "CEO" if re.fullmatch(r"(?:the\s+)?(?:peo|ceo)", name, re.I) else name


def find_pay(pages: list[Page], page_indexes: list[int] | None = None) -> dict:
    scope = [pages[i] for i in page_indexes] if page_indexes else pages
    for p in scope:
        for th in p.tables_html:
            yt = year_table(th)
            if not yt or not any(_col_kind(c) == "cap" for c in yt["columns"]):
                continue
            sct, cap, tsr = {}, {}, {}
            tsr_header = None
            for i, col in enumerate(yt["columns"]):
                kind = _col_kind(col)
                if kind is None:
                    continue
                series = {y: parse_number(vals[i]) for y, vals in yt["rows"].items()}
                if kind == "tsr":
                    if tsr_header is None:
                        tsr, tsr_header = series, col
                else:
                    (sct if kind == "sct" else cap)[_person(col)] = series
            if not sct or not tsr:
                continue
            years = sorted(yt["rows"], reverse=True)
            fy = years[0]
            rolling3 = bool(re.search(r"3[- ]year|three[- ]year", tsr_header, re.I))
            # Who was CEO each year: anyone with non-zero pay reported for that year.
            ceos = {y: [n for n, s in sct.items() if s.get(y)] for y in years}
            people = list(dict.fromkeys(n for n in sct))
            return {
                "status": "found",
                "fiscal_year": fy,
                "years": years,
                "sct": sct,
                "cap": cap,
                "tsr_value": tsr,
                "tsr_header": tsr_header,
                "tsr_basis": "rolling 3-year" if rolling3 else "cumulative from first year in table",
                "ceos_by_year": ceos,
                "table_method": yt["method"],
                "evidence": [_ev(p, f"Pay versus performance table, FY{years[-1]}–FY{fy}. CEO pay columns: "
                                    f"{', '.join(people)}. TSR column: \u201c{tsr_header}\u201d.",
                                 find="Actually Paid", kind="table")],
                "note": "",
            }
    return {"status": "missing", "evidence": [], "note": "No pay versus performance table found."}


# --------------------------------------------------------------------------- #

def find_all(pages: list[Page]) -> dict:
    """Code-only pass: keyword locator + extraction for each fact."""
    out = {}
    finders = {"attendance": find_attendance, "fees": find_fees, "pay": find_pay}
    for name, fn in finders.items():
        kw = keyword_pages(pages, name)
        fact = fn(pages, kw) if kw else {"status": "missing", "evidence": [], "note": "Keywords not found."}
        fact["keyword_pages"] = [pages[i].label for i in kw]
        fact["keyword_page_indexes"] = kw
        fact["method"] = "code" if fact["status"] != "missing" else None
        out[name] = fact
    return out
