"""Saved exports: every export she makes is kept, so she can tell whether the
file she sent is still what the app would produce now.

Stored in data/exports/<filing id>/<export id>.json: exactly the JSON export
(decisions.to_json) plus an "_export" header {id, format}. The CSV is rebuilt
from the same rows, so a past export downloads byte-for-byte as it was.

"Outdated" compares the latest export's rows with the current ones on the fields
that matter to the vote she'd submit: final vote, decision state, reason, policy
recommendation and the item itself. Timestamps alone (re-confirming the same
vote) don't make an export outdated.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import decisions

FORMATS = ("csv", "json")
_LOCK = threading.Lock()


def _dir(root: Path, filing_id: str) -> Path:
    return Path(root) / filing_id


def _meta(snap: dict) -> dict:
    return {**snap["_export"], "exported_at": snap["exported_at"], "complete": snap["complete"],
            "undecided": len(snap["undecided"]),
            "decided": sum(1 for r in snap["votes"] if r["final_vote"]), "total": len(snap["votes"])}


def record(root: Path, filing: dict, data: dict, rule_names: dict, policy: dict, fmt: str) -> dict:
    """Save what an export contains right now and return it (with its header)."""
    if fmt not in FORMATS:
        raise ValueError(f"Export format must be one of {', '.join(FORMATS)}.")
    snap = decisions.to_json(filing, data, rule_names, policy)
    base = snap["exported_at"].replace("+00:00", "Z").replace(":", "-")   # 2026-10-26T21-30-05Z
    with _LOCK:
        d = _dir(root, filing["id"])
        d.mkdir(parents=True, exist_ok=True)
        eid, n = base, 2
        while (d / f"{eid}.json").exists():     # two exports in the same second
            eid, n = f"{base}-{n}", n + 1
        snap["_export"] = {"id": eid, "format": fmt}
        (d / f"{eid}.json").write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap


def get(root: Path, filing_id: str, export_id: str) -> dict | None:
    if not re.fullmatch(r"[\w-]+", export_id or ""):
        return None
    p = _dir(root, filing_id) / f"{export_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def history(root: Path, filing_id: str) -> list[dict]:
    """Every saved export, newest first."""
    d = _dir(root, filing_id)
    if not d.exists():
        return []
    snaps = [json.loads(p.read_text(encoding="utf-8")) for p in d.glob("*.json")]
    return sorted((_meta(s) for s in snaps), key=lambda m: (m["exported_at"], m["id"]), reverse=True)


def as_file(snap: dict, fmt: str) -> tuple[bytes, str]:
    """(body, content type) for downloading a saved export, exactly as it was."""
    if fmt == "csv":
        return decisions.to_csv(snap["votes"]).encode("utf-8-sig"), "text/csv; charset=utf-8"
    body = {k: v for k, v in snap.items() if k != "_export"}
    return json.dumps(body, indent=2, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


def _vote(v: str) -> str:
    return v or "none"


def changes(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    """[{item, title, changes: [...]}] for every item whose exported line would differ."""
    old = {r["item"]: r for r in old_rows}
    new = {r["item"]: r for r in new_rows}
    out = []
    for n in list(new) + [n for n in old if n not in new]:
        a, b = old.get(n), new.get(n)
        if not a:
            lines = ["new item on the ballot"]
        elif not b:
            lines = ["no longer on the ballot"]
        elif a["title"] != b["title"]:
            lines = [f"now a different item (was \"{a['title']}\")"]
        else:
            lines = []
            if a["final_vote"] != b["final_vote"]:
                lines.append(f"vote: {_vote(a['final_vote'])} → {_vote(b['final_vote'])}"
                             + (f" ({b['decision']})" if not b["final_vote"] else ""))
            elif not b["final_vote"] and a["decision"] != b["decision"]:
                # Only for blank votes (e.g. undecided → needs re-deciding). With a vote set, a real
                # status change shows as a vote or policy change; label wording alone isn't one.
                lines.append(f"status: {a['decision']} → {b['decision']}")
            elif a["reason"] != b["reason"] and b["final_vote"]:   # same vote, new reason
                lines.append("reason changed")
            if a["policy_rec"] != b["policy_rec"]:
                lines.append(f"policy recommendation: {a['policy_rec']} → {b['policy_rec']}")
        if lines:
            out.append({"item": n, "title": (b or a)["title"], "changes": lines})
    return out


def status(root: Path, filing: dict, data: dict, rule_names: dict) -> dict:
    """Is the latest export still what she'd export now? Plus the full history."""
    hist = history(root, filing["id"])
    if not hist:
        return {"last": None, "outdated": False, "changes": [], "history": []}
    last = get(root, filing["id"], hist[0]["id"])
    diff = changes(last["votes"], decisions.export_rows(filing, data, rule_names))
    return {"last": hist[0], "outdated": bool(diff), "changes": diff, "history": hist}
