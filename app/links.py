"""Links into the live filing on SEC.gov that land on the exact words.

Each link is the filing's EDGAR URL plus a text fragment
(`#:~:text=[prefix-,]start[,end]`), which the browser scrolls to and highlights.
That only works if the words match what the browser renders, so links are built
from the filing's own text as a browser lays it out, not from our cleaned-up lines:

  * inline tags (span, b, font, ...) are dropped without adding a space, so
    "<span>A</span><span>pproval</span>" stays "Approval";
  * block tags (p, div, td, ...) are boundaries; a term can't cross one, so each
    term is picked from inside a single block;
  * the browser highlights the *first* match in the document, so a term is only
    used if its first match is the passage we mean. Otherwise more words are
    added, then a `prefix-` from the text just before it.

Every link says which printed page it lands on and whether it hits the cited
words exactly (`exact`) or only the right page (a fallback anchor).
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote as urlquote

TAG = re.compile(r"<(/?)([a-zA-Z][\w:.-]*)[^>]*>")
INLINE = {"span", "a", "b", "i", "u", "font", "strong", "em", "sup", "sub", "small", "big", "s",
          "strike", "abbr", "label", "ix:nonnumeric", "ix:nonfraction", "ix:continuation"}
WS = re.compile(r"\s+")
JOINERS = re.compile(r" … | • ")     # how extract.py joins lines into one quote
BIG_NUMBER = re.compile(r"^\(?\$?\d{1,3}(,\d{3})+(\.\d+)?\)?$")


def render_blocks(fragment: str) -> list[str]:
    """The text of each block-level element, as the browser renders it (lowercased)."""
    out = TAG.sub(lambda m: "" if m.group(2).lower() in INLINE else "\n", fragment)
    out = html.unescape(out).replace("\xa0", " ")
    blocks = []
    for b in out.split("\n"):
        b = WS.sub(" ", b).strip()
        if b:
            blocks.append(b.lower())
    return blocks


def _enc(term: str) -> str:
    # "-", "," and "&" have meaning inside a text directive, so they must be escaped too.
    return urlquote(term, safe="").replace("-", "%2D")


def _filler(text: str) -> bool:
    return not any(ch.isalnum() for ch in text)


class Linker:
    def __init__(self, url: str | None, pages: list) -> None:
        self.url = url
        self.label = {p.index: p.label for p in pages}
        self.blocks: list[tuple[int, str]] = []        # (page index, block text) in document order
        for p in pages:
            self.blocks += [(p.index, b) for b in render_blocks(p.html)]
        self.by_page: dict[int, list[int]] = {}
        for i, (pi, _) in enumerate(self.blocks):
            self.by_page.setdefault(pi, []).append(i)

    # -- matching ----------------------------------------------------------

    @staticmethod
    def _rx(term: str) -> re.Pattern:
        # Whole words only; a number inside a bigger one ("117" in "1,117") doesn't count.
        return re.compile(r"(?<!\w)(?<!\d[.,])" + re.escape(term) + r"(?!\w)(?![.,]\d)")

    def _first(self, term: str, after: tuple[int, int] = (0, 0)) -> tuple[int, int] | None:
        """(block, offset) of the first match of `term` at or after `after`."""
        rx = self._rx(term)
        bi, off = after
        for i in range(bi, len(self.blocks)):
            m = rx.search(self.blocks[i][1], off if i == bi else 0)
            if m:
                return i, m.start()
        return None

    def _first_after_prefix(self, prefix: str, term: str) -> tuple[int, int] | None:
        """Where `term` first appears right after `prefix` (only whitespace or a block break between)."""
        rp, rt = self._rx(prefix), self._rx(term)
        for i, (_, text) in enumerate(self.blocks):
            for m in rp.finditer(text):
                rest = text[m.end():]
                if rest.strip():
                    if rt.match(rest.lstrip()):
                        return i, m.end() + len(rest) - len(rest.lstrip())
                elif i + 1 < len(self.blocks) and rt.match(self.blocks[i + 1][1]):
                    return i + 1, 0
        return None

    def _suffix(self, bi: int, e: int) -> str | None:
        """The first few words after block bi, offset e (same block, else the block just after)."""
        after = self.blocks[bi][1][e:].strip()
        if not after:
            if bi + 1 >= len(self.blocks) or _filler(self.blocks[bi + 1][1]):
                return None
            after = self.blocks[bi + 1][1]
        return " ".join(after.split()[:4]) or None

    def _first_before_suffix(self, term: str, suffix: str) -> tuple[int, int] | None:
        """Where `term` first appears right before `suffix` (only whitespace or a block break between)."""
        rt, rs = self._rx(term), self._rx(suffix)
        for i, (_, text) in enumerate(self.blocks):
            for m in rt.finditer(text):
                rest = text[m.end():]
                if rest.strip():
                    if rs.match(rest.lstrip()):
                        return i, m.start()
                elif i + 1 < len(self.blocks) and rs.match(self.blocks[i + 1][1]):
                    return i, m.start()
        return None

    def _prefix(self, bi: int, s: int) -> str | None:
        """The last few words before block bi, offset s (same block, else the block just before)."""
        before = self.blocks[bi][1][:s].strip()
        if not before:
            if bi == 0 or _filler(self.blocks[bi - 1][1]):   # a "$" cell can't pin anything
                return None
            before = self.blocks[bi - 1][1]
        return " ".join(before.split()[-3:]) or None

    def _near(self, page_index: int) -> list[int]:
        return [i for p in (page_index, page_index + 1, page_index - 1) for i in self.by_page.get(p, [])]

    def _locate(self, text: str, page_index: int) -> tuple[int, int, int] | None:
        """Find `text` (whitespace-insensitive) in a block on or next to the page.
        Returns (block, start, end) widened to whole words."""
        want = WS.sub("", text.lower())
        if len(want) < 8:
            return None
        for bi in self._near(page_index):
            block = self.blocks[bi][1]
            keep = [k for k, ch in enumerate(block) if not ch.isspace()]
            at = "".join(block[k] for k in keep).find(want)
            if at < 0:
                continue
            s, e = keep[at], keep[at + len(want) - 1] + 1
            while s > 0 and not block[s - 1].isspace():
                s -= 1
            while e < len(block) and not block[e].isspace():
                e += 1
            return bi, s, e
        return None

    def _directive(self, bi: int, s: int, e: int) -> str | None:
        """A text directive whose first match is exactly block bi, chars s..e."""
        words = self.blocks[bi][1][s:e].split()
        prefix = self._prefix(bi, s)
        for k in range(min(4, len(words)), len(words) + 1):
            start = " ".join(words[:k])
            if self._first(start) == (bi, s):
                pre = ""
            elif prefix and self._first_after_prefix(prefix, start) == (bi, s):
                pre = f"{_enc(prefix)}-,"
            else:
                continue
            if k >= len(words) - 1:
                return pre + _enc(" ".join(words))
            for j in range(min(4, len(words) - k), len(words) - k + 1):
                end = " ".join(words[-j:])
                if self._first(end, (bi, s + len(start))) == (bi, e - len(end)):
                    return f"{pre}{_enc(start)},{_enc(end)}"
        # Boilerplate repeated word for word (a board recommendation under the same heading
        # on every proposal): pin the whole passage by the words that follow it.
        whole, suffix = " ".join(words), self._suffix(bi, e)
        if suffix and self._first_before_suffix(whole, suffix) == (bi, s):
            return f"{_enc(whole)},-{_enc(suffix)}"
        return None

    def _link(self, directive: str | None, page_index: int, exact: bool) -> dict:
        url = self.url and (f"{self.url}#:~:text={directive}" if directive else self.url)
        return {"url": url, "page": self.label.get(page_index, ""), "page_index": page_index, "exact": exact}

    # -- public ------------------------------------------------------------

    def quote(self, text: str, page_index: int) -> dict:
        """Link to a passage of prose (the attendance sentence, a resolution, ...).
        A quote joined from several lines is tried whole, then piece by piece."""
        pieces = [text] + sorted((p for p in JOINERS.split(text) if p != text), key=len, reverse=True)
        if ":" in text[:20]:
            pieces.append(text.split(":", 1)[1])          # "RESOLVED:" on its own line
        # A paragraph the filing splits over several blocks: its opening words are enough
        # to scroll to it and highlight where it starts.
        pieces += [" ".join(p.split()[:15]) for p in list(pieces) if len(p.split()) > 15]
        for piece in pieces:
            loc = self._locate(piece, page_index)
            if loc:
                d = self._directive(*loc)
                if d:
                    return self._link(d, self.blocks[loc[0]][0], True)
        return self.anchor(page_index)

    def terms(self, terms: list[str], page_index: int) -> dict:
        """Link to the first candidate (a table value like "9,765", then its row label
        like "Audit Fees") that can be pinned to this page."""
        near = set(self._near(page_index))
        for t in terms:
            t = WS.sub(" ", t).strip().lower()
            hit = self._first(t) if t else None
            if not hit:
                continue
            if hit[0] in near:
                return self._link(_enc(t), self.blocks[hit[0]][0], True)
            for bi in sorted(near):
                m = self._rx(t).search(self.blocks[bi][1])
                prefix = self._prefix(bi, m.start()) if m else None
                if prefix and self._first_after_prefix(prefix, t) == (bi, m.start()):
                    return self._link(f"{_enc(prefix)}-,{_enc(t)}", self.blocks[bi][0], True)
            if BIG_NUMBER.match(t):
                # The same reported figure appears earlier (e.g. a pay total in two tables).
                # It's the same number, so link where the browser will land, and say which page.
                return self._link(_enc(t), self.blocks[hit[0]][0], True)
        return self.anchor(page_index)

    def anchor(self, page_index: int) -> dict:
        """Fallback: a distinctive passage on the page, so the link at least lands there."""
        for bi in self.by_page.get(page_index, []):
            block = self.blocks[bi][1]
            if len(block) >= 40:
                d = self._directive(bi, 0, len(block))
                if d:
                    return self._link(d, page_index, False)
        return self._link(None, page_index, False)
