"""Apply the policy. Plain comparisons on facts that were already extracted.

Every rule returns:
  vote      FOR | AGAINST | FLAG
  headline  one line she reads first
  steps     the comparison, with the actual numbers
  facts     which facts were used (each carries its page + quote)
  settings  which policy settings changed the outcome
Missing or unconfirmed inputs always give FLAG, never the default vote.
"""
from __future__ import annotations

import re


def _money(v, units="dollars"):
    if v is None:
        return "n/a"
    d = v * 1000 if units == "thousands" else v
    if abs(d) >= 1e6:
        return f"${d / 1e6:,.2f}M"
    return f"${d:,.0f}"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def rec(vote, rule, headline, steps=None, facts=None, settings=None):
    return {"vote": vote, "rule": rule, "headline": headline, "steps": steps or [],
            "facts": facts or [], "settings": settings or []}


def _fact_ok(fact) -> tuple[bool, str]:
    if fact["status"] != "found":
        return False, fact.get("note") or "Fact not found."
    if fact.get("locator") == "disagree":
        return False, (f"Code found this on p. {fact['evidence'][0]['page']}, but the LLM pointed to "
                       f"{', '.join('p. ' + l for l in fact.get('llm_pages', [])) or 'other pages'}. Check which is right.")
    return True, ""


# --------------------------------------------------------------------------- #

def directors(name: str, fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    if not ok:
        return rec("FLAG", "DIRECTORS", "Attendance not confirmed", [why], ["attendance"])
    last = name.split()[-1].lower().strip(".,")
    below = [n for n in fact.get("below_75", []) if last in n.lower()]
    if below:
        return rec("AGAINST", "DIRECTORS", f"{name} attended fewer than 75% of meetings",
                   [f"Filing names {below[0]} among directors under 75%."], ["attendance"])
    if fact.get("all_at_least_75"):
        return rec("FOR", "DIRECTORS", "Attended at least 75% of meetings",
                   ["Filing states every director met the 75% threshold; no exceptions named."], ["attendance"])
    if fact.get("below_75"):
        return rec("FOR", "DIRECTORS", "Not among directors under 75%",
                   [f"Directors named under 75%: {', '.join(fact['below_75'])}. {name} isn't one of them."],
                   ["attendance"])
    return rec("FLAG", "DIRECTORS", "Attendance not confirmed",
               ["The attendance statement doesn't settle whether this director met 75%."], ["attendance"])


def _num(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v is not None else "n/a"


def _tsr3(fact, fy):
    v = fact["tsr_value"]
    if fact["tsr_basis"].startswith("rolling"):
        return (v[fy] / 100 - 1 if v.get(fy) else None), f"table reports 3-year TSR directly (${_num(v.get(fy))} per $100)"
    if v.get(fy) and v.get(fy - 3):
        return v[fy] / v[fy - 3] - 1, f"${_num(v[fy])} ÷ ${_num(v[fy - 3])} from the table's cumulative values"
    return None, "not enough years in the table"


def say_on_pay(fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    if not ok:
        return rec("FLAG", "EXEC_PAY", "Pay or TSR not confirmed", [why], ["pay"])

    fy, py = fact["fiscal_year"], fact["fiscal_year"] - 1
    measure = s["ceo_pay_measure"]
    series = fact["cap"] if measure == "cap" else fact["sct"]
    mlabel = "compensation actually paid" if measure == "cap" else "Summary Compensation Table total"
    now, prev = fact["ceos_by_year"].get(fy, []), fact["ceos_by_year"].get(py, [])
    used = ["ceo_pay_measure", "tsr_test"]

    t_now, how_now = _tsr3(fact, fy)
    t_prev, how_prev = _tsr3(fact, py)
    if s["tsr_test"] == "declined":
        tsr_fell = None if t_now is None or t_prev is None else t_now < t_prev
        tsr_line = f"3-year TSR: FY{py} {_pct(t_prev)} → FY{fy} {_pct(t_now)} ({how_now})"
    else:
        tsr_fell = None if t_now is None else t_now < 0
        tsr_line = f"3-year TSR to FY{fy}: {_pct(t_now)} ({how_now})"
    tsr_line += "" if tsr_fell is None else (": fell." if tsr_fell else ": did not fall.")

    def pay_line(name):
        a, b = (series.get(name) or {}).get(py), (series.get(name) or {}).get(fy)
        return f"{name} ({mlabel}): FY{py} {_money(a)} → FY{fy} {_money(b)}"

    changed = len(now) != 1 or len(prev) != 1 or now != prev
    steps = []
    if changed:
        used.append("on_ceo_change")
        steps.append(f"CEO in FY{py}: {', '.join(prev) or 'unknown'}; in FY{fy}: {', '.join(now) or 'unknown'}.")
        steps += [pay_line(n) + "." for n in dict.fromkeys(prev + now)]
        steps.append(tsr_line)
        if s["on_ceo_change"] == "flag" or len(now) != 1:
            return rec("FLAG", "EXEC_PAY", "CEO changed, so \"CEO pay rose\" is ambiguous",
                       steps + ["Setting: CEO changes are flagged for you."], ["pay"], used)
        steps.append(f"Setting: compare {now[0]}'s own pay year over year.")
        steps = [x for x in steps if x != tsr_line]
    name = now[0] if now else None
    pay_now = (series.get(name) or {}).get(fy)
    pay_prev = (series.get(name) or {}).get(py)
    if not pay_now or not pay_prev:
        return rec("FLAG", "EXEC_PAY", "CEO pay for both years not available",
                   steps + [pay_line(name) + "."], ["pay"], used)
    if tsr_fell is None:
        return rec("FLAG", "EXEC_PAY", "3-year TSR not available", steps + [tsr_line], ["pay"], used)

    pay_rose = pay_now > pay_prev
    steps += [f"CEO pay, {pay_line(name)}: {'rose' if pay_rose else 'did not rise'}.", tsr_line]
    if pay_rose and tsr_fell:
        return rec("AGAINST", "EXEC_PAY", "CEO pay rose while 3-year TSR fell", steps, ["pay"], used)
    headline = ("Pay rose, but 3-year TSR didn't fall" if pay_rose
                else "CEO pay didn't rise" + (" (TSR fell)" if tsr_fell else ""))
    return rec("FOR", "EXEC_PAY", headline, steps, ["pay"], used)


def auditor(fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    if not ok:
        return rec("FLAG", "AUDITOR", "Fees not confirmed", [why], ["fees"])
    u = fact["units"]
    parts = [("tax", fact["tax"]), ("all other", fact["other"])]
    if s["non_audit_includes_audit_related"]:
        parts.insert(0, ("audit-related", fact["audit_related"]))
    non_audit = sum(v for _, v in parts)
    audit = fact["audit"]
    steps = [
        f"FY{fact['year']} audit fees: {_money(audit, u)}.",
        f"Non-audit fees = {' + '.join(f'{n} {_money(v, u)}' for n, v in parts)} = {_money(non_audit, u)}.",
    ]
    if non_audit > audit:
        return rec("AGAINST", "AUDITOR", f"Non-audit fees {_money(non_audit, u)} exceed audit fees {_money(audit, u)}",
                   steps, ["fees"], ["non_audit_includes_audit_related"])
    return rec("FOR", "AUDITOR", f"Non-audit {_money(non_audit, u)} ≤ audit {_money(audit, u)}",
               steps, ["fees"], ["non_audit_includes_audit_related"])


def board_structure(title: str, category: str, s: dict) -> dict:
    if category == "majority_voting" and re.search(r"supermajority", title, re.I):
        if not s["supermajority_is_majority_voting"]:
            return rec("FLAG", "BOARD_STRUCTURE", "Supermajority proposal not counted as majority voting",
                       ["Setting: only a majority standard for director elections counts."], [],
                       ["supermajority_is_majority_voting"])
        return rec("FOR", "BOARD_STRUCTURE", "Policy supports majority voting",
                   ["Replaces supermajority requirements with majority voting.",
                    "Setting: this counts as \"majority voting\"."], [], ["supermajority_is_majority_voting"])
    what = "declassifying the board" if category == "declassify_board" else "majority voting"
    return rec("FOR", "BOARD_STRUCTURE", f"Policy supports {what}", [], [])


# --------------------------------------------------------------------------- #

def recommend(item: dict, cls: dict, facts: dict, s: dict) -> dict:
    if cls["status"] == "disagree":
        return rec("FLAG", "FLAG", "Unclear which rule applies", [cls["note"]])
    if cls["status"] == "unverified":
        return rec("FLAG", "FLAG", "Classification not verified", [cls["note"]])
    rule = cls["rule"]
    if rule == "DIRECTORS":
        name = re.sub(r"^Elect\s+", "", item["title"])
        return directors(name, facts["attendance"], s)
    if rule == "EXEC_PAY":
        return say_on_pay(facts["pay"], s)
    if rule == "AUDITOR":
        return auditor(facts["fees"], s)
    if rule == "BOARD_STRUCTURE":
        return board_structure(item["proposal_title"] or item["title"], cls["category"], s)
    return rec("FLAG", "FLAG", "Not covered by the policy",
               [cls["note"]] if cls["category"] != "other" else ["No rule covers this kind of item. Your call."])
