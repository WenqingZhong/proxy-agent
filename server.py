"""Proxy Agent: a small local web app for reviewing proxy ballots.

Run:  python3 server.py   then open http://localhost:8000

No third-party packages. Filings are read from data/filings/ (any .htm/.html
DEF 14A saved from EDGAR); drop a new one in and restart.
"""
from __future__ import annotations

import json
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

from pages import split_pages  # noqa: E402
from extract import extract_ballot  # noqa: E402
from policy import POLICY, rule_for  # noqa: E402

FILINGS_DIR = ROOT / "data" / "filings"
STATIC_DIR = ROOT / "static"


class Store:
    """Parses every filing once at startup and keeps it in memory."""

    def __init__(self) -> None:
        self.filings: dict[str, dict] = {}
        self.pages: dict[str, list] = {}
        self.files: dict[str, str] = {}

    def load(self) -> None:
        paths = sorted(p for p in FILINGS_DIR.iterdir() if p.suffix.lower() in (".htm", ".html"))
        for path in paths:
            t0 = time.time()
            fid = path.stem
            raw = path.read_text(encoding="utf-8", errors="ignore")
            pages = split_pages(raw)
            filing = extract_ballot(fid, pages)
            for item in filing.items:
                item.policy_rule = rule_for(item.kind)
            self.filings[fid] = filing.to_dict()
            self.filings[fid]["source_file"] = path.name
            self.pages[fid] = pages
            print(f"  loaded {path.name}: {filing.company}, {len(filing.items)} ballot items, "
                  f"{len(pages)} pages ({time.time() - t0:.1f}s)")

    def summaries(self) -> list[dict]:
        out = []
        for f in self.filings.values():
            items = f["items"]
            out.append({
                "id": f["id"],
                "company": f["company"],
                "meeting_date": f["meeting_date"],
                "source_file": f["source_file"],
                "item_count": len(items),
                "flag_count": sum(1 for i in items if i["policy_rule"] == "FLAG"),
                "warnings": f["warnings"],
            })
        return sorted(out, key=lambda s: s["company"].lower())


STORE = Store()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:1] != ["api"]:
            return super().do_GET()

        # /api/policy
        if parts == ["api", "policy"]:
            return self._json(POLICY)
        # /api/filings
        if parts == ["api", "filings"]:
            return self._json(STORE.summaries())
        # /api/filings/<id>
        if len(parts) == 3 and parts[1] == "filings":
            f = STORE.filings.get(parts[2])
            return self._json(f) if f else self._json({"error": "not found"}, 404)
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
                "page_count": len(pages),
            })
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
