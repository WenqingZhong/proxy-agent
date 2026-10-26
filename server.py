"""Proxy Agent: a small local web app for reviewing proxy ballots.

Run:  python3 server.py   then open http://localhost:8000

No third-party packages. Filings are read from data/filings/ (any .htm/.html
DEF 14A saved from EDGAR). After dropping in a new or updated one, re-analyse
from the app (or restart); changing a policy setting re-analyses automatically.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

import decisions  # noqa: E402
import edgar  # noqa: E402
import exports  # noqa: E402
from extract import find_company, find_meeting_date  # noqa: E402
from pages import split_pages  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import POLICY, current_settings, policy_view, set_setting  # noqa: E402

FILINGS_DIR = ROOT / "data" / "filings"
DECISIONS_DIR = ROOT / "data" / "decisions"
EXPORTS_DIR = ROOT / "data" / "exports"
SOURCES_FILE = FILINGS_DIR / "sources.json"   # filing id -> its URL on SEC.gov, for evidence links
RULE_NAMES = {r["id"]: r["section"] for r in POLICY["rules"]}
STATIC_DIR = ROOT / "static"
MAX_UPLOAD = 25 * 1024 * 1024    # bytes of request body; an EDGAR proxy statement is a few MB


def filing_id(filename: str) -> str:
    """The id (and file name) for an uploaded filing: its name, lowercased, reduced to
    [a-z0-9-]. Nothing else from the upload ever reaches a path."""
    m = re.fullmatch(r"(.+)\.html?", Path(filename).name, re.I) if isinstance(filename, str) else None
    if not m:
        raise ValueError("The filing must be an .htm or .html file saved from EDGAR.")
    fid = re.sub(r"[^a-z0-9]+", "-", m.group(1).lower()).strip("-")
    if not fid:
        raise ValueError("The file name needs at least one letter or digit.")
    return fid


_SUFFIXES = {"the", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "plc", "llc"}


def company_key(name: str) -> str:
    """A company name without punctuation or legal suffixes: "Amazon.com, Inc." -> "amazon com"."""
    return " ".join(w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _SUFFIXES)


def cik_of(url) -> str | None:
    """The SEC company number in an EDGAR link (/Archives/edgar/data/<CIK>/...)."""
    m = re.search(r"/Archives/edgar/data/(\d+)/", url or "")
    return m.group(1) if m else None


def year_of(date) -> str | None:
    m = re.search(r"\b(20\d\d)\b", date or "")
    return m.group(1) if m else None


class Store:
    """Parses every filing once at startup and keeps it in memory."""

    def __init__(self) -> None:
        self.filings: dict[str, dict] = {}
        self.pages: dict[str, list] = {}
        self.files: dict[str, str] = {}
        self.running = threading.Lock()   # one re-analysis at a time

    def _analyze(self, path: Path, settings: dict) -> tuple[dict, list]:
        return self._run(path.stem, path.read_text(encoding="utf-8", errors="ignore"), path.name, settings)

    def _run(self, fid: str, raw: str, file_name: str, settings: dict, source_url: str | None = None):
        t0 = time.time()
        sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {}
        filing, pages = analyze(fid, raw, settings, source_url=source_url or sources.get(fid))
        filing["source_file"] = file_name
        votes = [i["recommendation"]["vote"] for i in filing["items"]]
        print(f"  {file_name}: {filing['company']}, {len(votes)} items "
              f"({votes.count('FOR')} FOR, {votes.count('AGAINST')} AGAINST, {votes.count('FLAG')} FLAG), "
              f"LLM: {filing['llm_mode']} ({time.time() - t0:.1f}s)")
        return filing, pages

    def load(self) -> None:
        self.reanalyze()

    def add(self, filename: str, raw: str, source_url: str | None = None, replace: bool = False):
        """An uploaded filing: analyse it right away and, only if a ballot is found, save it to
        data/filings/ (so later re-analyses pick it up) and serve it. Replacing a filing that's
        already loaded needs replace=True; her decisions on it are then re-checked, never changed.
        Returns (filing, reports): reports has the re-analysis report when a filing was replaced."""
        if source_url:
            source_url = edgar.canonical(source_url)
        return self._add(filing_id(filename), raw, source_url, replace)

    def add_url(self, url: str, replace: bool = False):
        """A filing added by its SEC.gov link: fetched (outside the lock, so a slow download
        doesn't hold up re-analysis), then added like an upload."""
        url, raw = edgar.fetch(url, MAX_UPLOAD)
        return self._add(self.id_for(raw, url), raw, url, replace)

    def id_for(self, raw: str, url: str) -> str:
        """The id for a filing added by link. The same company's proxy for the same meeting
        year as a loaded filing (same SEC company number, or same name) gets that filing's id,
        so it's offered as a replacement. Otherwise company + year, like the starter filings
        (amazon-2026-def14a), with the company number added if a different company has that."""
        pages = split_pages(raw)
        company, year, cik = find_company(pages), year_of(find_meeting_date(pages)), cik_of(url)
        for f in self.filings.values():
            if year and year_of(f["meeting_date"]) == year and (
                    (cik and cik_of(f.get("source_url")) == cik) or company_key(f["company"]) == company_key(company)):
                return f["id"]
        first = (company_key(company).split() or [""])[0]
        if not (first and year):
            return filing_id(url.rsplit("/", 1)[-1])
        base = f"{first}-{year}-def14a"
        return base if base not in self.filings else f"{first}-{cik}-{year}-def14a"

    def _add(self, fid: str, raw, source_url: str | None, replace: bool):
        if not isinstance(raw, str) or "<" not in raw:
            raise ValueError("The upload has no HTML in it.")
        with self.running:
            before = self.filings.get(fid)
            if before and not replace:
                n = decisions.states(before, decisions.load(DECISIONS_DIR, fid))["decided"]
                raise decisions.Conflict(
                    f"{before['company']} ({before['meeting_date'] or fid}) is already loaded"
                    + (f", with {n} decision{'s' if n != 1 else ''}" if n else "")
                    + ". Replace it? Your decisions on it will be re-checked, not changed.")
            path = FILINGS_DIR / (before["source_file"] if before else f"{fid}.htm")
            filing, pages = self._run(fid, raw, path.name, current_settings(), source_url)
            if not filing["items"]:
                raise ValueError("No ballot items found, so this doesn't look like a proxy statement "
                                 "(DEF 14A) the app can read. Nothing was saved.")
            if before:
                # Her decisions are keyed by item number + title, and titles like "Ratification of
                # auditor" repeat across companies, so a replacement must be the same company.
                old_cik, new_cik = cik_of(before.get("source_url")), cik_of(source_url)
                if company_key(before["company"]) != company_key(filing["company"]) or (
                        old_cik and new_cik and old_cik != new_cik):
                    raise ValueError(f"That's {filing['company']}, not {before['company']}, so it can't "
                                     f"replace it. Nothing was saved.")
            FILINGS_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".upload")
            tmp.write_text(raw, encoding="utf-8")
            tmp.replace(path)
            if source_url:
                sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {}
                sources[fid] = source_url
                tmp = SOURCES_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
                tmp.replace(SOURCES_FILE)
            self.filings[fid], self.pages[fid] = filing, pages
            reports = [decisions.reanalysis_report(before, filing, decisions.load(DECISIONS_DIR, fid))] if before else []
            return filing, reports

    def reanalyze(self, only: str | None = None) -> list[dict]:
        """Read the filing(s) from disk again and re-run everything with the current
        settings and AI. Returns what changed per filing. Decisions are never altered;
        they're compared with the new recommendations when read."""
        with self.running:
            settings = current_settings()
            paths = sorted(p for p in FILINGS_DIR.iterdir() if p.suffix.lower() in (".htm", ".html"))
            if only:
                paths = [p for p in paths if p.stem == only]
            reports = []
            for path in paths:
                filing, pages = self._analyze(path, settings)
                before = self.filings.get(path.stem)
                # Swap in one step so a request never sees half of an update.
                self.filings[path.stem], self.pages[path.stem] = filing, pages
                if before:
                    reports.append(decisions.reanalysis_report(
                        before, filing, decisions.load(DECISIONS_DIR, path.stem)))
            if not only:
                for fid in set(self.filings) - {p.stem for p in paths}:
                    del self.filings[fid], self.pages[fid]
            return reports

    def summaries(self) -> list[dict]:
        out = []
        for f in self.filings.values():
            items = f["items"]
            data = decisions.load(DECISIONS_DIR, f["id"])
            st = decisions.states(f, data)
            ex = exports.status(EXPORTS_DIR, f, data, RULE_NAMES)
            out.append({
                "id": f["id"],
                "company": f["company"],
                "meeting_date": f["meeting_date"],
                "source_file": f["source_file"],
                "item_count": len(items),
                "flag_count": sum(1 for i in items if i["recommendation"]["vote"] == "FLAG"),
                "decided_count": st["decided"],
                "review_count": sum(len(v) for v in st["review"].values()),
                "export_outdated": ex["outdated"],
                "llm_mode": f["llm_mode"],
                "warnings": f["warnings"],
            })
        return sorted(out, key=lambda s: s["company"].lower())


STORE = Store()


def decision_payload(f: dict, data: dict) -> dict:
    """Decision states plus whether her last export still matches them."""
    return {**decisions.states(f, data), "export": exports.status(EXPORTS_DIR, f, data, RULE_NAMES)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, payload, status=200, filename=None):
        body = json.dumps(payload, ensure_ascii=False, indent=2 if filename else None).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status, filename)

    def _send(self, body: bytes, ctype: str, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _from_this_app(self, post: bool) -> bool:
        """Only the app's own page may use the API. Another site open in her browser could
        otherwise send a POST here and record votes or change a setting, and a hostname
        pointed at 127.0.0.1 (DNS rebinding) could read the data."""
        port = self.server.server_address[1]
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return False
        # A cross-site form or no-cors fetch can't send JSON without the browser asking first.
        return not post or (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:1] != ["api"]:
            return super().do_GET()
        if not self._from_this_app(post=False):
            return self._json({"error": "forbidden"}, 403)

        # /api/policy
        if parts == ["api", "policy"]:
            return self._json(policy_view(current_settings()))
        # /api/filings
        if parts == ["api", "filings"]:
            return self._json(STORE.summaries())
        # /api/filings/<id>
        if len(parts) == 3 and parts[1] == "filings":
            f = STORE.filings.get(parts[2])
            return self._json(f) if f else self._json({"error": "not found"}, 404)
        # /api/filings/<id>/decisions  (states + export status)
        if len(parts) == 4 and parts[1] == "filings" and parts[2] in STORE.filings and parts[3] == "decisions":
            return self._json(decision_payload(STORE.filings[parts[2]], decisions.load(DECISIONS_DIR, parts[2])))
        # /api/filings/<id>/exports/<export id>.csv|json  (a saved export, exactly as it was)
        if len(parts) == 5 and parts[1] == "filings" and parts[3] == "exports":
            eid, _, fmt = parts[4].rpartition(".")
            snap = exports.get(EXPORTS_DIR, parts[2], eid)
            if not snap or fmt not in exports.FORMATS:
                return self._json({"error": "not found"}, 404)
            body, ctype = exports.as_file(snap, fmt)
            return self._send(body, ctype, filename=f"{parts[2]}-votes-{eid}.{fmt}")
        # /api/filings/<id>/pages/<index>
        if len(parts) == 5 and parts[1] == "filings" and parts[3] == "pages":
            pages = STORE.pages.get(parts[2])
            try:
                idx = int(parts[4])
                page = pages[idx]
            except (TypeError, ValueError, IndexError):
                return self._json({"error": "not found"}, 404)
            return self._json({
                "index": page.index,
                "label": page.label,
                "lines": page.lines,
                "blocks": page.blocks,
                "page_count": len(pages),
            })
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if not self._from_this_app(post=True):
            return self._json({"error": "forbidden"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad Content-Length"}, 400)
        if length < 0:     # rfile.read(-1) would read until the client hangs up, with no limit
            return self._json({"error": "bad Content-Length"}, 400)
        if length > MAX_UPLOAD:
            return self._json({"error": f"Request too large (limit {MAX_UPLOAD // (1024 * 1024)} MB)."}, 413)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            # /api/filings  {url, replace?} or {filename, html, source_url?, replace?}:
            # add a filing by its SEC.gov link, or upload one; either way it's analysed right away
            if parts == ["api", "filings"]:
                if "url" in body:
                    filing, reports = STORE.add_url(body.get("url"), bool(body.get("replace")))
                else:
                    filing, reports = STORE.add(body.get("filename"), body.get("html"),
                                                body.get("source_url"), bool(body.get("replace")))
                summaries = STORE.summaries()
                return self._json({"filing": next(x for x in summaries if x["id"] == filing["id"]),
                                   "reports": reports, "filings": summaries}, 200 if reports else 201)
            # /api/reanalyze  {filing?: id}: re-read filings from disk, re-run with current settings + AI
            if parts == ["api", "reanalyze"]:
                fid = body.get("filing")
                if fid and fid not in STORE.filings:
                    return self._json({"error": "not found"}, 404)
                return self._json({"reports": STORE.reanalyze(fid), "filings": STORE.summaries()})
            # /api/policy/settings  {id, value}: save a setting, then re-analyse every filing
            if parts == ["api", "policy", "settings"]:
                set_setting(body.get("id"), body.get("value"))
                return self._json({"policy": policy_view(current_settings()),
                                   "reports": STORE.reanalyze(), "filings": STORE.summaries()})
            # /api/filings/<id>/decisions  {items: [...], action: accept|vote|confirm|clear, vote?, reason?}
            if len(parts) == 4 and parts[:2] == ["api", "filings"] and parts[3] == "decisions" \
                    and parts[2] in STORE.filings:
                f = STORE.filings[parts[2]]
                expect = body.get("expect")
                if body.get("action") != "clear" and not isinstance(expect, dict):
                    raise ValueError("Missing the recommendation you were shown (expect).")
                data = decisions.apply(DECISIONS_DIR, f, body.get("items") or [], body.get("action"),
                                       body.get("vote"), body.get("reason") or "", expect)
                return self._json(decision_payload(f, data))
            # /api/filings/<id>/exports  {format: csv|json}: save the export, then download it by its id
            if len(parts) == 4 and parts[:2] == ["api", "filings"] and parts[3] == "exports" \
                    and parts[2] in STORE.filings:
                f = STORE.filings[parts[2]]
                data = decisions.load(DECISIONS_DIR, f["id"])
                fmt = body.get("format")
                snap = exports.record(EXPORTS_DIR, f, data, RULE_NAMES, policy_view(f["settings"]), fmt)
                return self._json({"saved": snap["_export"],
                                   "download": f"/api/filings/{f['id']}/exports/{snap['_export']['id']}.{fmt}",
                                   **decision_payload(f, data)})
        except decisions.Conflict as e:
            return self._json({"error": str(e), "conflict": True}, 409)
        except (ValueError, AttributeError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print("Loading filings...")
    STORE.load()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\nProxy Agent running at http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
