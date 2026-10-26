"""Split an EDGAR proxy HTML file into printed pages of plain-text lines.

EDGAR filings mark page boundaries with CSS `page-break-before/after: always`.
Each page's printed number lives in its footer (or header), in a slightly
different spot for every filer, so we look for it near the edges of the page.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

PAGE_BREAK = re.compile(r"<[^>]*page-break-(?:before|after)\s*:\s*always[^>]*>", re.I)
HIDDEN = re.compile(r"(?is)<(script|style|ix:header|head)\b.*?</\1>")
BLOCK_END = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h[1-6]|table)>")
CELL_END = re.compile(r"(?i)</t[dh]>")
TAG = re.compile(r"<[^>]+>")
SPACES = re.compile(r"[ \t\xa0​  ]+")


@dataclass
class Page:
    index: int                 # 0-based position in the file
    lines: list[str]
    printed: int | None = None  # page number printed on the page, if found
    label: str = field(default="")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def html_to_lines(fragment: str) -> list[str]:
    t = HIDDEN.sub(" ", fragment)
    t = CELL_END.sub(" | ", t)
    t = BLOCK_END.sub("\n", t)
    t = TAG.sub(" ", t)
    t = html.unescape(t)
    out = []
    for line in t.split("\n"):
        line = SPACES.sub(" ", line).strip(" |")
        line = re.sub(r"(\s*\|\s*)+", " | ", line).strip(" |")
        if line:
            out.append(line)
    return out


# A footer like "14 STARBUCKS 2026 PROXY", "Alphabet 2026 Proxy Statement 21",
# "2026 Proxy Statement 9", or just "8".
_EDGE_NUM = [
    re.compile(r"^(\d{1,3})$"),
    re.compile(r"^(\d{1,3})\s+\D.*\bproxy\b", re.I),
    re.compile(r"\bproxy\b.*\D\s(\d{1,3})$", re.I),
    re.compile(r"^(\d{1,3})\s*\|\s*\D"),
]


def _find_printed(lines: list[str]) -> int | None:
    edge = lines[-4:][::-1] + lines[:3]
    for line in edge:
        for rx in _EDGE_NUM:
            m = rx.search(line)
            if m:
                return int(m.group(1))
    return None


def split_pages(raw_html: str) -> list[Page]:
    raw_html = HIDDEN.sub(" ", raw_html)
    chunks = PAGE_BREAK.split(raw_html)
    pages: list[Page] = []
    for chunk in chunks:
        lines = html_to_lines(chunk)
        if not lines:
            continue
        pages.append(Page(index=len(pages), lines=lines))
    for p in pages:
        p.printed = _find_printed(p.lines)
    _clean_page_numbers(pages)
    for p in pages:
        p.label = str(p.printed) if p.printed is not None else f"#{p.index + 1}"
    return pages


def _clean_page_numbers(pages: list[Page]) -> None:
    """Drop page numbers that break the expected sequence (e.g. a stray '2025')
    and fill single gaps from neighbours."""
    nums = [p.printed for p in pages]
    for i, n in enumerate(nums):
        if n is None:
            continue
        prev = next((nums[j] for j in range(i - 1, -1, -1) if nums[j] is not None), None)
        nxt = next((nums[j] for j in range(i + 1, len(nums)) if nums[j] is not None), None)
        ok_prev = prev is not None and 0 < n - prev <= 3
        ok_next = nxt is not None and 0 < nxt - n <= 3
        if not (ok_prev or ok_next):
            nums[i] = None
    for i in range(len(nums)):
        if nums[i] is None:
            if 0 < i < len(nums) - 1 and nums[i - 1] is not None and nums[i + 1] == nums[i - 1] + 2:
                nums[i] = nums[i - 1] + 1
            elif i > 0 and nums[i - 1] is not None and i + 1 < len(nums) and nums[i + 1] is None:
                pass
    for p, n in zip(pages, nums):
        p.printed = n
