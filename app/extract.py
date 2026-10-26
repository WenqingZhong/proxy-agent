"""Pull the ballot out of a DEF 14A: company, meeting date, and each voting item
with the board's recommendation and the page it comes from.

This is plain rules, no LLM. It was written against three filings whose layouts
differ (Starbucks, Alphabet, Amazon):

  * item headings: "PROPOSAL 1 –", "Proposal Number 1:", "ITEM 1—"
  * the summary table usually gives the section's page number on the next line
  * director nominees come from the proxy card ("1a. Name 1b. Name ...") when
    the filing includes one, otherwise from the meeting notice
    ("Election of directors: A, B, and C")
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from pages import Page

MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"

HEADING = re.compile(
    r"^(?:proposal|item)\s*(?:number|no\.?)?\s*(\d{1,2})\b\s*[:–—\-.]?\s*(.*)$", re.I
)
PAGE_REF = re.compile(r"^(?:page\s*)?(\d{1,3})$", re.I)
PAGE_REF_INLINE = re.compile(r"\(page\s*(\d{1,3})\s*\)", re.I)

REC = re.compile(
    r"recommends?\s+(?:that\s+(?:you|shareholders)\s+vote\s+(?:your\s+shares\s+)?|a\s+vote\s+|you\s+vote\s+)"
    r"[“\"']?(FOR|AGAINST)[”\"']?",
    re.I,
)
NO_REC = re.compile(r"makes?\s+no\s+recommendation", re.I)


@dataclass
class Evidence:
    page: str          # printed page label, e.g. "85" (or "#119" if unnumbered)
    page_index: int    # position in the file, for linking
    quote: str


@dataclass
class BallotItem:
    number: str                    # "1", "1a", "4"
    title: str
    kind: str                      # director | say_on_pay | auditor | board_structure | other
    proponent: str                 # management | shareholder
    board_rec: str | None          # FOR | AGAINST | NONE (no recommendation) | None (not found)
    section: Evidence | None
    board_rec_evidence: Evidence | None = None
    proposal_title: str = ""       # the proposal's own title (for nominees: "Election of Directors")


@dataclass
class Filing:
    id: str
    company: str
    meeting_date: str | None
    page_count: int
    items: list[BallotItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Helpers

def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip(" :;–—-")
    s = re.sub(r"\bA pproval\b", "Approval", s)  # Alphabet's split-letter typo
    return s


def _smart_title(s: str) -> str:
    """Filings shout their headings; make them readable but keep acronyms."""
    if s.upper() != s:
        return s
    small = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "be", "with"}
    keep = {"LLP", "EY", "AI", "CEO", "U.S.", "NEO", "NEOS", "ESG", "&"}
    words = []
    for i, w in enumerate(s.split()):
        if w in keep:
            words.append(w)
        elif i and w.lower() in small:
            words.append(w.lower())
        else:
            words.append(w.capitalize())
    return " ".join(words)


def _page_by_label(pages: list[Page], label: str) -> Page | None:
    return next((p for p in pages if p.label == label), None)


# --------------------------------------------------------------------------- #
# Company and meeting date

def find_company(pages: list[Page]) -> str:
    for p in pages[:3]:
        for i, line in enumerate(p.lines):
            if "name of registrant" in line.lower() and i > 0:
                name = _clean(p.lines[i - 1])
                return _smart_title(name) if name.isupper() else name
    return "Unknown company"


def find_meeting_date(pages: list[Page]) -> str | None:
    text = " ".join(" ".join(p.lines) for p in pages[:20])
    m = re.search(
        rf"annual meeting[^.]{{0,160}}?\b({MONTHS}\s+\d{{1,2}},\s+\d{{4}})", text, re.I
    )
    if m:
        return m.group(1).title()
    m = re.search(rf"\b({MONTHS}\s+\d{{1,2}},\s+\d{{4}})", text)
    return m.group(1).title() if m else None


# --------------------------------------------------------------------------- #
# Proposals

@dataclass
class _Hit:
    number: int
    title: str
    page: Page
    line_no: int
    page_ref: str | None


def _collect_heading_hits(pages: list[Page]) -> list[_Hit]:
    hits = []
    for p in pages:
        for i, line in enumerate(p.lines):
            if len(line) > 300:
                continue
            m = HEADING.match(line)
            if not m:
                continue
            num = int(m.group(1))
            title = m.group(2).strip()
            nxt = p.lines[i + 1] if i + 1 < len(p.lines) else ""
            nxt2 = p.lines[i + 2] if i + 2 < len(p.lines) else ""
            page_ref = None
            # The title may continue on the next line (wrapped heading), and the
            # summary table puts the section's page number right after it.
            if not title or (len(title) < 12 and not PAGE_REF.match(nxt)):
                title = f"{title} {nxt}".strip()
                nxt, nxt2 = nxt2, ""
            elif nxt and not PAGE_REF.match(nxt) and len(nxt) < 80 and nxt.upper() == nxt \
                    and title.upper() == title and not HEADING.match(nxt):
                title = f"{title} {nxt}"
                nxt, nxt2 = nxt2, ""
            if PAGE_REF.match(nxt):
                page_ref = PAGE_REF.match(nxt).group(1)
            elif PAGE_REF_INLINE.search(line):
                page_ref = PAGE_REF_INLINE.search(line).group(1)
            title = PAGE_REF_INLINE.sub("", title)
            hits.append(_Hit(num, _clean(title), p, i, page_ref))
    return hits


def _consecutive_numbers(nums: set[int]) -> list[int]:
    out = []
    n = 1
    while n in nums:
        out.append(n)
        n += 1
    return out


def _pick_title(hits: list[_Hit]) -> str:
    # The fullest version wins; running headers and the TOC are often cut short.
    titles = [h.title for h in hits if len(h.title) > 3]
    if not titles:
        return ""
    best = max(titles, key=len)
    # Prefer mixed-case version of the same title if one exists.
    mixed = [t for t in titles if t.upper() != t and len(t) >= len(best) * 0.8]
    return _smart_title(max(mixed, key=len) if mixed else best)


def _pick_section_page(hits: list[_Hit], pages: list[Page]) -> tuple[Page, str]:
    # 1) A page reference from the summary / table of contents.
    for h in hits:
        if h.page_ref:
            target = _page_by_label(pages, h.page_ref)
            if target:
                return target, h.page.lines[h.line_no]
    # 2) A heading near the top of a numbered page (where sections start).
    top = [h for h in hits if h.line_no <= 8 and h.page.printed is not None]
    if top:
        h = top[0]
        return h.page, h.page.lines[h.line_no]
    h = hits[-1]
    return h.page, h.page.lines[h.line_no]


def _find_board_rec(pages: list[Page], start: int, end: int):
    for p in pages[start:end]:
        for line in p.lines:
            if NO_REC.search(line) and len(line) < 400:
                return "NONE", Evidence(p.label, p.index, line.strip())
            m = REC.search(line)
            if m and len(line) < 600:
                return m.group(1).upper(), Evidence(p.label, p.index, line.strip())
    return None, None


def _classify(title: str) -> tuple[str, str]:
    t = title.lower()
    proponent = "shareholder" if "shareholder proposal" in t or "stockholder proposal" in t else "management"
    if re.search(r"election of (?:\d+ |the )?directors?", t):
        kind = "director"
    elif re.search(r"advisory vote|say.on.pay", t) and re.search(r"compensation|pay", t) and "frequency" not in t:
        kind = "say_on_pay"
    elif re.search(r"ratif", t) and re.search(r"accounting firm|auditor", t):
        kind = "auditor"
    elif re.search(r"declassif|majority vot", t):
        kind = "board_structure"
    else:
        kind = "other"
    return kind, proponent


# --------------------------------------------------------------------------- #
# Director nominees

_NOMINEE = re.compile(r"(?<![0-9])1([a-z])\.\s*(.+?)(?=(?<![0-9])1[a-z]\.|For\b|SCAN|The Board|Please|NOTE|$)")


def _nominees_from_proxy_card(pages: list[Page]) -> tuple[list[str], Page | None]:
    for p in reversed(pages):
        text = " ".join(p.lines)
        found = {}
        for m in _NOMINEE.finditer(text):
            letter, name = m.group(1), _clean(m.group(2))
            if 3 <= len(name) <= 60 and letter not in found:
                found[letter] = name
        letters = sorted(found)
        if len(letters) >= 3 and letters[0] == "a":
            return [found[k] for k in letters], p
    return [], None


def _nominees_from_notice(pages: list[Page]) -> tuple[list[str], Page | None]:
    rx = re.compile(r"election of (?:\d+ )?directors?\s*[:\-–]\s*(.+)$", re.I)
    for p in pages[:25]:
        for line in p.lines:
            m = rx.search(line)
            if m and "," in m.group(1):
                parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1))
                names = [_clean(x) for x in parts if 3 <= len(_clean(x)) <= 60]
                if len(names) >= 3:
                    return names, p
    return [], None


# --------------------------------------------------------------------------- #

def extract_ballot(filing_id: str, pages: list[Page]) -> Filing:
    filing = Filing(
        id=filing_id,
        company=find_company(pages),
        meeting_date=find_meeting_date(pages),
        page_count=len(pages),
    )

    hits = _collect_heading_hits(pages)
    by_num: dict[int, list[_Hit]] = {}
    for h in hits:
        by_num.setdefault(h.number, []).append(h)
    numbers = _consecutive_numbers(set(by_num))
    if not numbers:
        filing.warnings.append("No ballot items found. The filing layout may not be supported yet.")
        return filing

    sections = {n: _pick_section_page(by_num[n], pages) for n in numbers}
    starts = [sections[n][0].index for n in numbers]

    for k, n in enumerate(numbers):
        page, quote = sections[n]
        later = [s for s in starts if s > page.index]
        end = min(later) if later else min(page.index + 6, len(pages))
        end = max(end, page.index + 1)
        rec, rec_ev = _find_board_rec(pages, page.index, min(end + 1, len(pages)))
        title = _pick_title(by_num[n])
        kind, proponent = _classify(title)
        item = BallotItem(
            number=str(n),
            title=title,
            kind=kind,
            proponent=proponent,
            board_rec=rec,
            section=Evidence(page.label, page.index, quote),
            board_rec_evidence=rec_ev,
            proposal_title=title,
        )
        if rec is None:
            filing.warnings.append(f"Item {n}: couldn't find the board's recommendation.")

        if kind == "director":
            names, src = _nominees_from_proxy_card(pages)
            if not names:
                names, src = _nominees_from_notice(pages)
            if names:
                for i, name in enumerate(names):
                    filing.items.append(BallotItem(
                        number=f"{n}{chr(ord('a') + i)}",
                        title=f"Elect {name}",
                        kind="director",
                        proponent="management",
                        board_rec=rec,
                        section=item.section,
                        board_rec_evidence=rec_ev,
                        proposal_title=title,
                    ))
                continue
            filing.warnings.append("Couldn't list individual director nominees.")
        filing.items.append(item)

    return filing
