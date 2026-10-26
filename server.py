"""Proxy Agent: a small local web app for reviewing proxy ballots.

Run:  python3 server.py   then open http://localhost:8000

No third-party packages. Filings are read from data/filings/ (any .htm/.html
DEF 14A saved from EDGAR). After dropping in a new or updated one, re-analyse
from the app (or restart); changing a policy setting re-analyses automatically.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

import decisions  # noqa: E402
from pipeline import analyze  # noqa: E402
from policy import POLICY, current_settings, policy_view, set_setting  # noqa: E402

FILINGS_DIR = ROOT / "data" / "filings"
DECISIONS_DIR = ROOT / "data" / "decisions"
RULE_NAMES = {r["id"]: r["section"] for r in POLICY["rules"]}
STATIC_DIR = ROOT / "static"


class Store:
    """Parses every filing once at startup and keeps it in memory."""

    def __init__(self) -> None:
        self.filings: dict[str, dict] = {}
        self.pages: dict[str, list] = {}
        self.files: dict[str, str] = {}
        self.running = threading.Lock()   # one re-analysis at a time

    def _analyze(self, path: Path, settings: dict) -> tuple[dict, list]:
        t0 = time.time()
        raw = path.read_text(encoding="utf-8", errors="ignore")
        filing, pages = analyze(path.stem, raw, settings)
        filing["source_file"] = path.name
        votes = [i["recommendation"]["vote"] for i in filing["items"]]
        print(f"  {path.name}: {filing['company']}, {len(votes)} items "
              f"({votes.count('FOR')} FOR, {votes.count('AGAINST')} AGAINST, {votes.count('FLAG')} FLAG), "
              f"LLM: {filing['llm_mode']} ({time.time() - t0:.1f}s)")
        return filing, pages

    def load(self) -> None:
        self.reanalyze()

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
            st = decisions.states(f, decisions.load(DECISIONS_DIR, f["id"]))
            out.append({
                "id": f["id"],
                "company": f["company"],
                "meeting_date": f["meeting_date"],
                "source_file": f["source_file"],
                "item_count": len(items),
                "flag_count": sum(1 for i in items if i["recommendation"]["vote"] == "FLAG"),
                "decided_count": st["decided"],
                "review_count": sum(len(v) for v in st["review"].values()),
                "llm_mode": f["llm_mode"],
                "warnings": f["warnings"],
            })
        return sorted(out, key=lambda s: s["company"].lower())


STORE = Store()


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

    def do_GET(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:1] != ["api"]:
            return super().do_GET()

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
        # /api/filings/<id>/decisions  and  /api/filings/<id>/export.csv|json
        if len(parts) == 4 and parts[1] == "filings" and parts[2] in STORE.filings:
            f, fid = STORE.filings[parts[2]], parts[2]
            data = decisions.load(DECISIONS_DIR, fid)
            if parts[3] == "decisions":
                return self._json(decisions.states(f, data))
            if parts[3] == "export.csv":
                body = decisions.to_csv(decisions.export_rows(f, data, RULE_NAMES))
                return self._send(body.encode("utf-8-sig"), "text/csv; charset=utf-8", filename=f"{fid}-votes.csv")
            if parts[3] == "export.json":
                return self._json(decisions.to_json(f, data, RULE_NAMES, policy_view(f["settings"])),
                                  filename=f"{fid}-votes.json")
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
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
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
                data = decisions.apply(DECISIONS_DIR, f, body.get("items") or [], body.get("action"),
                                       body.get("vote"), body.get("reason") or "")
                return self._json(decisions.states(f, data))
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
