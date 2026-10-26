"""Read values out of EDGAR tables and match each one to its column header.

Two ways to match, because filers build tables inconsistently:
  * order:  header leaves (deepest header cells) in reading order, paired with the
            row's values in order, used when the counts match (works even when the
            data rows' colspans don't line up with the header's, as in Alphabet's).
  * grid:   pair each value with the header cell directly above it (colspan/rowspan).
"""
from __future__ import annotations

import re

from pages import table_grid

FOOTNOTE = re.compile(r"^\(\d{1,2}\)$")
ORDINAL = re.compile(r"^(st|nd|rd|th)$", re.I)
YEAR_CELL = re.compile(r"^(?:fiscal\s+(?:year\s+)?)?(20\d\d)\b", re.I)


def norm_header(s: str) -> str:
    s = re.sub(r"(\w)- (\w)", r"\1\2", s)          # "Compen- sation" -> "Compensation"
    s = re.sub(r"\(\d\)", " ", s)                    # footnote markers
    return re.sub(r"\s+", " ", s).strip(" :")


def parse_number(text: str):
    """'$ 30,992,773' -> 30992773.0, '( 147,737,654 )' -> -147737654.0,
    '$—' -> 0.0, 'N/A' -> None. Returns None if it isn't a number."""
    t = text.replace("$", "").replace(",", "").replace(" ", "")
    if t in ("—", "–", "-", "0"):
        return 0.0
    if t.upper() in ("N/A", "NA", "NM"):
        return None
    neg = t.startswith("(")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _value_cells(row):
    return [c for c in row if not FOOTNOTE.match(c[2]) and not ORDINAL.match(c[2])]


def _header_leaves(header_rows):
    """Deepest header cells in reading order, each with its full path text.
    A lower-row cell with no parent above it (a common filer quirk) goes right
    after the previous cell in its own row."""
    order = [dict(cell=c, path=norm_header(c[2])) for c in header_rows[0]] if header_rows else []
    for row in header_rows[1:]:
        new_order = []
        placed_children = {id(n): [] for n in order}
        orphans = []                       # (anchor node or None, node)
        last_in_row = None
        for c in row:
            node = dict(cell=c, path=None)
            parent = next((n for n in order if n["cell"][0] <= c[1] and n["cell"][1] >= c[0]), None)
            if parent:
                node["path"] = f'{parent["path"]} / {norm_header(c[2])}'
                placed_children[id(parent)].append(node)
            else:
                node["path"] = norm_header(c[2])
                orphans.append((last_in_row, node))
            last_in_row = node
        for n in order:
            kids = placed_children[id(n)]
            new_order.extend(kids if kids else [n])
        for anchor, node in orphans:
            if anchor is not None and anchor in new_order:
                new_order.insert(new_order.index(anchor) + 1, node)
            else:
                pos = next((i for i, n in enumerate(new_order) if n["cell"][0] > node["cell"][0]), len(new_order))
                new_order.insert(pos, node)
        order = new_order
    return [n for n in order if not YEAR_CELL.match(n["path"]) and not re.match(r"^(fiscal )?year$", n["path"], re.I)]


def year_table(table_html: str):
    """For tables with one row per year (e.g. pay versus performance).
    Returns {"columns": [header path...], "rows": {year: [value text...]}, "method": ...}
    or None if the table doesn't look like that."""
    grid = table_grid(table_html)
    header, data = [], []
    for row in grid:
        if YEAR_CELL.match(row[0][2]):
            data.append(row)
        elif not data:
            header.append(row)
    if not header or not data:
        return None

    leaves = _header_leaves(header)
    rows_by_order = {}
    ok_order = True
    for row in data:
        year = int(YEAR_CELL.match(row[0][2]).group(1))
        vals = _value_cells(row[1:])
        if len(vals) != len(leaves):
            ok_order = False
            break
        rows_by_order[year] = [v[2] for v in vals]

    if ok_order and leaves:
        return {"columns": [n["path"] for n in leaves], "rows": rows_by_order, "method": "order"}

    # Fall back to the grid: header text directly above each value.
    cols, rows = None, {}
    for row in data:
        year = int(YEAR_CELL.match(row[0][2]).group(1))
        vals = _value_cells(row[1:])
        heads = [
            norm_header(" / ".join(h[2] for hr in header for h in hr if h[0] <= v[1] and h[1] >= v[0]))
            for v in vals
        ]
        cols = cols or heads
        if heads != cols:
            return None
        rows[year] = [v[2] for v in vals]
    return {"columns": cols, "rows": rows, "method": "grid"}
