"""The analyst's decisions: accept or override each recommendation, then export.

Stored as one JSON file per filing in data/decisions/<filing id>.json:
  {item number: {vote, reason, policy_vote, policy_rule, decided_at}}

The state shown to her is worked out on read, not stored:
  accepted    she took the policy vote
  overridden  she chose a different vote than the policy (reason required)
  decided     the policy flagged it and she chose a vote (reason required)
  stale       the recommendation changed since she decided (a setting or fact
              changed), so the decision no longer counts until she re-confirms
  undecided   nothing yet
"""
from __future__ import annotations

import csv
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

VOTES = ("FOR", "AGAINST", "ABSTAIN")
_LOCK = threading.Lock()


def _path(root: Path, filing_id: str) -> Path:
    return Path(root) / f"{filing_id}.json"


def load(root: Path, filing_id: str) -> dict:
    p = _path(root, filing_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(root: Path, filing_id: str, data: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = _path(root, filing_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_path(root, filing_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply(root: Path, filing: dict, numbers: list[str], action: str,
          vote: str | None = None, reason: str = "") -> dict:
    """Record one decision for each item in `numbers`. Raises ValueError with a
    message she can read if the request doesn't make sense; nothing is saved then."""
    items = {i["number"]: i for i in filing["items"]}
    unknown = [n for n in numbers if n not in items]
    if not numbers or unknown:
        raise ValueError(f"Unknown item(s): {', '.join(unknown) or 'none given'}.")
    reason = (reason or "").strip()

    updates = {}
    for n in numbers:
        rec = items[n]["recommendation"]
        policy = rec["vote"]
        if action == "clear":
            updates[n] = None
            continue
        if action == "accept":
            if policy == "FLAG":
                raise ValueError(f"Item {n} has no policy vote to accept. Choose a vote and give a reason.")
            chosen = policy
        elif action == "vote":
            if vote not in VOTES:
                raise ValueError(f"Vote must be one of {', '.join(VOTES)}.")
            chosen = vote
            if chosen != policy and not reason:
                raise ValueError("A reason is required when you vote differently from the policy"
                                 if policy != "FLAG" else "A reason is required for a flagged item.")
        else:
            raise ValueError(f"Unknown action: {action}.")
        updates[n] = {"vote": chosen, "reason": reason, "policy_vote": policy,
                      "policy_rule": rec["rule"], "decided_at": _now()}

    with _LOCK:
        data = load(root, filing["id"])
        for n, d in updates.items():
            if d is None:
                data.pop(n, None)
            else:
                data[n] = d
        _save(root, filing["id"], data)
    return data


def item_state(item: dict, d: dict | None) -> dict:
    policy = item["recommendation"]["vote"]
    if not d:
        return {"state": "undecided", "final_vote": None}
    out = {"final_vote": d["vote"], "reason": d["reason"], "decided_at": d["decided_at"],
           "policy_vote_then": d["policy_vote"]}
    if d["policy_vote"] != policy:
        return {**out, "state": "stale", "final_vote": None, "final_vote_then": d["vote"],
                "note": f"The recommendation changed from {_shown(d['policy_vote'])} to {_shown(policy)} "
                        f"after you decided. Confirm again."}
    if policy == "FLAG":
        return {**out, "state": "decided"}
    return {**out, "state": "accepted" if d["vote"] == policy else "overridden"}


def states(filing: dict, data: dict) -> dict:
    """{item number: state}, plus counts for the progress bar."""
    by_item = {i["number"]: item_state(i, data.get(i["number"])) for i in filing["items"]}
    open_ = [n for n, s in by_item.items() if s["final_vote"] is None]
    return {"items": by_item, "decided": len(by_item) - len(open_), "total": len(by_item),
            "undecided": open_,
            "overridden": sum(1 for s in by_item.values() if s["state"] == "overridden")}


# --------------------------------------------------------------------------- #
# Export

def _shown(vote: str) -> str:
    return "REVIEW" if vote == "FLAG" else vote


def main_page(item: dict, facts: dict) -> str:
    """The filing page the recommendation mainly rests on."""
    for name in item["recommendation"]["facts"]:
        ev = facts.get(name, {}).get("evidence") or []
        if ev:
            return ev[0]["page"]
    for key in ("board_rec_evidence", "section"):
        if item.get(key):
            return item[key]["page"]
    return ""


DECISION_LABEL = {"accepted": "accepted policy", "overridden": "overrode policy",
                  "decided": "decided (policy flagged)", "stale": "needs re-confirming",
                  "undecided": "undecided"}

COLUMNS = ["company", "meeting_date", "item", "title", "proponent", "board_rec", "policy_rec",
           "policy_rule", "policy_reason", "source_page", "final_vote", "changed", "decision",
           "reason", "decided_at"]


def export_rows(filing: dict, data: dict, rule_names: dict) -> list[dict]:
    st = states(filing, data)["items"]
    rows = []
    for it in filing["items"]:
        s, rec = st[it["number"]], it["recommendation"]
        rows.append({
            "company": filing["company"],
            "meeting_date": filing["meeting_date"] or "",
            "item": it["number"],
            "title": it["title"],
            "proponent": it["proponent"],
            "board_rec": it["board_rec"] or "",
            "policy_rec": _shown(rec["vote"]),
            "policy_rule": rule_names.get(rec["rule"], rec["rule"]),
            "policy_reason": rec["headline"],
            "source_page": main_page(it, filing["facts"]),
            "final_vote": s["final_vote"] or "",
            "changed": "yes" if s["state"] == "overridden" else "no",
            "decision": DECISION_LABEL[s["state"]],
            "reason": s.get("reason", ""),
            "decided_at": s.get("decided_at", "") if s["final_vote"] else "",
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def to_json(filing: dict, data: dict, rule_names: dict, policy: dict) -> dict:
    st = states(filing, data)
    return {
        "company": filing["company"],
        "meeting_date": filing["meeting_date"],
        "source_file": filing.get("source_file"),
        "exported_at": _now(),
        "policy": {"name": policy["name"], "version": policy["version"],
                   "settings": {s["id"]: s["value"] for s in policy["settings"]}},
        "complete": not st["undecided"],
        "undecided": st["undecided"],
        "votes": export_rows(filing, data, rule_names),
    }
