"""The analyst's decisions: accept or override each recommendation, then export.

Stored as one JSON file per filing in data/decisions/<filing id>.json:
  {item number: {vote, reason, policy_vote, policy_rule, decided_at,
                 key, basis, run, history, reconfirmed_after}}
  key      item number + title, so a renumbered ballot can't move her vote to another item
  basis    what the recommendation rested on when she decided (vote, rule, facts, settings, pages)
  run      the inputs of the analysis she saw (filing hash, settings, AI model)
  history  her earlier decisions on this item, oldest first

The state shown to her is worked out on read, not stored:
  accepted    she took the policy vote
  overridden  she chose a different vote than the policy (reason required)
  decided     the policy flagged it and she chose a vote (reason required)
  stale       the item or its recommendation changed since she decided, so the
              decision no longer counts until she re-confirms. `change` says how:
                conflicts     the new recommendation differs from her vote
                now_agrees    the new recommendation now matches her vote
                item_changed  this number is now a different item
  undecided   nothing yet
A decision whose vote still matches but whose basis changed (a fee figure, a
setting, the evidence page) keeps counting; it carries `basis_changed` so she is told.
Nothing is ever changed for her automatically.
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from policy import POLICY

VOTES = ("FOR", "AGAINST", "ABSTAIN", "ABSENT")   # ABSENT: not voting the shares
ALWAYS_EXPLAIN = ("ABSTAIN", "ABSENT")
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


# --------------------------------------------------------------------------- #
# What a decision rested on, and what changed since

FACT_LABEL = {"attendance": "Director attendance", "fees": "Auditor fees", "pay": "CEO pay & TSR"}
# The values a rule reads; notes, page indexes and locator wording are left out.
FACT_FIELDS = {
    "attendance": ("status", "locator", "all_at_least_75", "below_75"),
    "fees": ("status", "locator", "year", "units", "audit", "audit_related", "tax", "other"),
    "pay": ("status", "locator", "fiscal_year", "ceos_by_year", "sct", "cap", "tsr_value", "tsr_basis"),
}
SETTING_LABEL = {x["id"]: x["label"] for x in POLICY["settings"]}
SETTING_OPTIONS = {x["id"]: {str(k).lower(): v for k, v in x["options"].items()} for x in POLICY["settings"]}
RULE_LABEL = {r["id"]: r["section"] for r in POLICY["rules"]}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


def item_key(item: dict) -> str:
    return f"{item['number']}|{_norm(item['title'])}"


def _step_texts(steps) -> list[str]:
    """Steps may be plain strings or {text, cite, link}; only the wording counts as reasoning."""
    return [x.get("text", "") if isinstance(x, dict) else x for x in steps or []]


def basis(filing: dict, item: dict) -> dict:
    rec = item["recommendation"]
    facts = {}
    for name in rec["facts"]:
        fact = filing["facts"].get(name, {})
        facts[name] = {k: fact.get(k) for k in FACT_FIELDS.get(name, ())}
        facts[name]["pages"] = [e["page"] for e in fact.get("evidence") or []]
    b = {
        "title": item["title"],
        "vote": rec["vote"], "rule": rec["rule"], "headline": rec["headline"], "steps": _step_texts(rec["steps"]),
        "category": item["classification"]["category"],
        "board_rec": item.get("board_rec"),
        "settings": {k: filing["settings"].get(k) for k in rec["settings"]},
        "facts": facts,
    }
    return json.loads(json.dumps(b))   # same shape as after a save (year keys become strings)


def _flat(d, prefix=""):
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: d}


def _val(v) -> str:
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (int, float)):
        return f"{v:,}"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    return str(v)


def _setting(k: str, a, b) -> str:
    opt = lambda v: SETTING_OPTIONS.get(k, {}).get(str(v).lower(), _val(v))  # noqa: E731
    return f"{SETTING_LABEL.get(k, k)} (setting): {opt(a)} → {opt(b)}"


def changes(old: dict | None, new: dict) -> list[str]:
    """What differs between two bases, one readable line each."""
    if not old:
        return []
    out = []
    if old.get("vote") != new["vote"]:
        out.append(f"Recommendation: {_shown(old.get('vote'))} → {_shown(new['vote'])}")
    if old.get("rule") != new["rule"]:
        out.append(f"Rule: {RULE_LABEL.get(old.get('rule'), old.get('rule'))} → {RULE_LABEL.get(new['rule'], new['rule'])}")
    elif old.get("category") != new["category"]:
        out.append(f"Item type: {old.get('category')} → {new['category']}".replace("_", " "))
    if old.get("board_rec") != new["board_rec"]:
        out.append(f"Board recommendation: {_val(old.get('board_rec'))} → {_val(new['board_rec'])}")
    for k in sorted(set(old.get("settings", {})) | set(new["settings"])):
        a, b = old.get("settings", {}).get(k), new["settings"].get(k)
        if a != b:
            out.append(_setting(k, a, b))
    for name in sorted(set(old.get("facts", {})) | set(new["facts"])):
        a, b = _flat(old.get("facts", {}).get(name, {})), _flat(new["facts"].get(name, {}))
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                what = "evidence page" if k == "pages" else k.replace("_", " ")
                out.append(f"{FACT_LABEL.get(name, name)}, {what}: {_val(a.get(k))} → {_val(b.get(k))}")
    if not out and (old.get("headline") != new["headline"] or _step_texts(old.get("steps")) != new["steps"]):
        out.append("The reasoning shown for the recommendation changed")
    return out


def run_changes(old: dict | None, new: dict | None, settings: bool = True) -> list[str]:
    """Why a re-analysis could give different answers: which inputs changed.
    settings=False leaves settings out (an item's own `changes` already names the ones it uses)."""
    if not old or not new:
        return []
    out = []
    if old.get("filing_sha256") != new.get("filing_sha256"):
        out.append("The filing was updated")
    if old.get("policy_version") != new.get("policy_version"):
        out.append(f"Policy version: {old.get('policy_version')} → {new.get('policy_version')}")
    so, sn = old.get("settings") or {}, new.get("settings") or {}
    for k in sorted(set(so) | set(sn)) if settings else ():
        if so.get(k) != sn.get(k):
            out.append(_setting(k, so.get(k), sn.get(k)))
    if (old.get("llm_mode"), old.get("llm_model")) != (new.get("llm_mode"), new.get("llm_model")):
        ai = lambda r: r.get("llm_model") or r.get("llm_mode")  # noqa: E731
        out.append(f"AI: {ai(old)} → {ai(new)}")
    elif old.get("stub_sha256") != new.get("stub_sha256"):
        out.append("The AI stub answers were updated")
    return out


def _inputs(filing: dict) -> dict | None:
    run = filing.get("run")
    return {k: v for k, v in run.items() if k != "analyzed_at"} if run else None


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
        if action == "confirm":
            # Keep the vote and reason she already gave, against the current recommendation.
            prev = load(root, filing["id"]).get(n)
            if not prev:
                raise ValueError(f"Item {n} has no earlier decision to keep.")
            chosen, reason = prev["vote"], reason or prev.get("reason", "")
            if (chosen != policy or chosen in ALWAYS_EXPLAIN) and not reason:
                raise ValueError(f"Item {n}: a reason is required to keep {chosen}.")
        elif action == "accept":
            if policy == "FLAG":
                raise ValueError(f"Item {n} has no policy vote to accept. Choose a vote and give a reason.")
            chosen = policy
        elif action == "vote":
            if vote not in VOTES:
                raise ValueError(f"Vote must be one of {', '.join(VOTES)}.")
            chosen = vote
            if (chosen != policy or chosen in ALWAYS_EXPLAIN) and not reason:
                raise ValueError(f"A reason is required for {chosen}." if chosen in ALWAYS_EXPLAIN
                                 else "A reason is required when you vote differently from the policy"
                                 if policy != "FLAG" else "A reason is required for a flagged item.")
        else:
            raise ValueError(f"Unknown action: {action}.")
        updates[n] = {"vote": chosen, "reason": reason, "policy_vote": policy,
                      "policy_rule": rec["rule"], "decided_at": _now(),
                      "key": item_key(items[n]), "basis": basis(filing, items[n]), "run": _inputs(filing)}

    with _LOCK:
        data = load(root, filing["id"])
        for n, d in updates.items():
            if d is None:
                data.pop(n, None)
                continue
            prev = data.get(n)
            if prev:
                st = item_state(filing, items[n], prev)
                why = st.get("changes") if st["state"] == "stale" or st.get("basis_changed") else None
                if why:
                    d["reconfirmed_after"] = why
                d["history"] = prev.get("history", []) + [
                    {k: v for k, v in prev.items() if k not in ("history", "basis", "run")}]
            data[n] = d
        _save(root, filing["id"], data)
    return data


def item_state(filing: dict, item: dict, d: dict | None) -> dict:
    policy = item["recommendation"]["vote"]
    if not d:
        return {"state": "undecided", "final_vote": None}
    out = {"final_vote": d["vote"], "reason": d["reason"], "decided_at": d["decided_at"],
           "policy_vote_then": d["policy_vote"], "history": d.get("history", []),
           "reconfirmed_after": d.get("reconfirmed_after", [])}
    now = basis(filing, item)
    diff = changes(d.get("basis"), now)
    causes = run_changes(d.get("run"), _inputs(filing), settings=False)
    stale = {**out, "state": "stale", "final_vote": None, "final_vote_then": d["vote"],
             "basis_then": d.get("basis"), "changes": diff, "causes": causes}
    if d.get("key") and d["key"] != item_key(item):
        then = (d.get("basis") or {}).get("title") or d["key"].split("|", 1)[1]
        return {**stale, "change": "item_changed", "changes": [f"Item: {then} → {item['title']}"],
                "note": f"Item {item['number']} is now \"{item['title']}\". Your vote was for \"{then}\". Decide again."}
    if d["policy_vote"] != policy:
        if d["vote"] == policy:
            return {**stale, "change": "now_agrees", "changes": diff or [
                        f"Recommendation: {_shown(d['policy_vote'])} → {_shown(policy)}"],
                    "note": f"The recommendation changed from {_shown(d['policy_vote'])} to {_shown(policy)}, "
                            f"which now matches your vote. Confirm to keep it."}
        return {**stale, "change": "conflicts", "changes": diff or [
                    f"Recommendation: {_shown(d['policy_vote'])} → {_shown(policy)}"],
                "note": f"The recommendation changed from {_shown(d['policy_vote'])} to {_shown(policy)} "
                        f"after you voted {d['vote']}. Decide again."}
    if diff:
        out.update(basis_changed=True, basis_then=d.get("basis"), changes=diff, causes=causes)
    if policy == "FLAG":
        return {**out, "state": "decided"}
    return {**out, "state": "accepted" if d["vote"] == policy else "overridden"}


def states(filing: dict, data: dict) -> dict:
    """{item number: state}, plus counts for the progress bar and what needs a second look."""
    by_item = {i["number"]: item_state(filing, i, data.get(i["number"])) for i in filing["items"]}
    open_ = [n for n, s in by_item.items() if s["final_vote"] is None]
    review = {k: [n for n, s in by_item.items() if s.get("change") == k]
              for k in ("conflicts", "now_agrees", "item_changed")}
    review["basis_changed"] = [n for n, s in by_item.items() if s.get("basis_changed")]
    # Decisions for item numbers no longer on the ballot. Kept on disk, never exported.
    review["removed"] = [{"number": n, "title": (d.get("basis") or {}).get("title", ""), "vote": d["vote"]}
                         for n, d in data.items() if n not in by_item]
    # For the banner: every input that changed under the decisions that need a look.
    affected = [n for n, s in by_item.items() if s["state"] == "stale" or s.get("basis_changed")]
    causes = list(dict.fromkeys(c for n in affected
                                for c in run_changes(data[n].get("run"), _inputs(filing))))
    return {"items": by_item, "decided": len(by_item) - len(open_), "total": len(by_item),
            "undecided": open_,
            "overridden": sum(1 for s in by_item.values() if s["state"] == "overridden"),
            "review": review, "causes": causes}


def reanalysis_report(before: dict, after: dict, data: dict) -> dict:
    """What a re-analysis changed for one filing: every recommendation that moved
    (decided or not), items added or removed, and the decisions she needs to revisit."""
    old = {i["number"]: i for i in before["items"]}
    new = {i["number"]: i for i in after["items"]}
    moved = []
    for n, it in new.items():
        o = old.get(n)
        if o and item_key(o) == item_key(it) and o["recommendation"]["vote"] != it["recommendation"]["vote"]:
            moved.append({"number": n, "title": it["title"], "before": _shown(o["recommendation"]["vote"]),
                          "after": _shown(it["recommendation"]["vote"])})
    st = states(after, data)
    return {
        "filing": after["id"], "company": after["company"],
        "causes": run_changes(_inputs(before), _inputs(after)),
        "recommendations_changed": moved,
        "items_added": [n for n, it in new.items() if n not in old or item_key(old[n]) != item_key(it)],
        "items_removed": [n for n, it in old.items() if n not in new or item_key(new[n]) != item_key(it)],
        "review": st["review"], "decided": st["decided"], "total": st["total"],
    }


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
STALE_LABEL = {"conflicts": "needs re-deciding: recommendation changed",
               "now_agrees": "needs re-confirming: recommendation now matches",
               "item_changed": "needs re-deciding: item changed"}

COLUMNS = ["company", "meeting_date", "item", "title", "proponent", "board_rec", "policy_rec",
           "policy_rule", "policy_reason", "source_page", "final_vote", "changed", "decision",
           "reason", "decided_at", "reconfirmed_after", "changed_since_decided"]


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
            "decision": STALE_LABEL.get(s.get("change"), DECISION_LABEL[s["state"]]),
            "reason": s.get("reason", ""),
            "decided_at": s.get("decided_at", "") if s["final_vote"] else "",
            "reconfirmed_after": "; ".join(s.get("reconfirmed_after") or []) if s["final_vote"] else "",
            "changed_since_decided": "; ".join(s.get("changes") or []),
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
                   "settings": filing.get("settings") or {s["id"]: s["value"] for s in policy["settings"]}},
        "analysis": filing.get("run"),
        "complete": not st["undecided"],
        "undecided": st["undecided"],
        "needs_review": {k: v for k, v in st["review"].items() if v},
        "votes": export_rows(filing, data, rule_names),
    }
