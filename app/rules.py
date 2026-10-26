"""Apply the policy. Plain comparisons on facts that were already extracted.

Every rule returns:
  vote      FOR | AGAINST | FLAG
  headline  one line she reads first
  steps     the reasoning, one specific claim each: {"text", "cite"}. `cite` says
            where in the filing the claim comes from, either
              {"quote": exact filing text, "page_index", "page"}  (a sentence), or
              {"terms": [candidates], "page_index", "page"}       (a table value,
                                                                   then its row label)
            The pipeline turns each cite into a link to the live filing.
  facts     which facts were used
  settings  which policy settings changed the outcome
Missing or unconfirmed inputs always give FLAG, never the default vote.
"""
from __future__ import annotations

import re

RULES_COVER = ("director attendance, say-on-pay, declassifying the board or majority voting, "
               "and auditor ratification")


def _money(v, units="dollars"):
    if v is None:
        return "n/a"
    d = v * 1000 if units == "thousands" else v
    if abs(d) >= 1e6:
        return f"${d / 1e6:,.2f}M"
    return f"${d:,.0f}"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def _num(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v is not None else "n/a"


def _cell(v):
    """A number as the filing's table prints it: 9,765 / 2,069,861 / 110.47 / (147,737,654)."""
    if v is None:
        return None
    return f"({_num(-v)})" if v < 0 else _num(v)


def _short(text, n=220):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def _name_lists(names):
    """How a table might print several names in one cell: "A, B, and C", "A, B and C", "A and B"."""
    from itertools import permutations
    out = []
    for order in permutations(names):
        if len(order) == 1:
            out.append(order[0])
        else:
            head = ", ".join(order[:-1])
            out += [f"{head}, and {order[-1]}", f"{head} and {order[-1]}"]
    return out


def step(text, cite=None):
    return {"text": text, "cite": cite}


def quote_cite(ev):
    return {"quote": ev["quote"], "page_index": ev["page_index"], "page": ev["page"]} if ev else None


def terms_cite(ev, *terms):
    terms = [t for t in terms if t]
    return {"terms": terms, "page_index": ev["page_index"], "page": ev["page"]} if ev and terms else None


def rec(vote, rule, headline, steps=None, facts=None, settings=None):
    steps = [s if isinstance(s, dict) else step(s) for s in steps or []]
    return {"vote": vote, "rule": rule, "headline": headline, "steps": steps,
            "facts": facts or [], "settings": settings or []}


def _fact_ok(fact) -> tuple[bool, str]:
    if fact["status"] != "found":
        return False, fact.get("note") or "Fact not found."
    if fact.get("locator") == "disagree":
        return False, (f"Code found this on p. {fact['evidence'][0]['page']}, but the LLM pointed to "
                       f"{', '.join('p. ' + l for l in fact.get('llm_pages', [])) or 'other pages'}. Check which is right.")
    return True, ""


def _ev(fact):
    return (fact.get("evidence") or [None])[0]


# --------------------------------------------------------------------------- #

def directors(name: str, fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    ev = _ev(fact)
    if not ok:
        return rec("FLAG", "DIRECTORS", "Attendance not confirmed", [step(why, quote_cite(ev))], ["attendance"])
    said = step(f"The filing says: “{ev['quote']}”", quote_cite(ev))
    last = name.split()[-1].lower().strip(".,")
    below = [n for n in fact.get("below_75", []) if last in n.lower()]
    if below:
        return rec("AGAINST", "DIRECTORS", f"{name} attended fewer than 75% of meetings",
                   [said, step(f"{below[0]} is named as attending fewer than 75%, so the policy votes against.")],
                   ["attendance"])
    if fact.get("all_at_least_75"):
        return rec("FOR", "DIRECTORS", "Attended at least 75% of meetings",
                   [said, step(f"That covers every director, {name} included, and no exception is named. "
                               f"The 75% threshold is met, so the policy supports the nominee.")], ["attendance"])
    if fact.get("below_75"):
        return rec("FOR", "DIRECTORS", "Not among directors under 75%",
                   [said, step(f"The directors named under 75% are {', '.join(fact['below_75'])}. "
                               f"{name} isn't one of them.")], ["attendance"])
    return rec("FLAG", "DIRECTORS", "Attendance not confirmed",
               [said, step("The statement doesn't settle whether this director met 75%.")], ["attendance"])


def _tsr3(fact, fy):
    v = fact["tsr_value"]
    if fact["tsr_basis"].startswith("rolling"):
        return (v[fy] / 100 - 1 if v.get(fy) else None), f"the table's 3-year TSR value is ${_num(v.get(fy))} per $100 invested"
    if v.get(fy) and v.get(fy - 3):
        return v[fy] / v[fy - 3] - 1, f"${_num(v[fy])} ÷ ${_num(v[fy - 3])} from the table's cumulative values"
    return None, "not enough years in the table"


def say_on_pay(fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    ev = _ev(fact)
    if not ok:
        return rec("FLAG", "EXEC_PAY", "Pay or TSR not confirmed", [step(why)], ["pay"])

    fy, py = fact["fiscal_year"], fact["fiscal_year"] - 1
    measure = s["ceo_pay_measure"]
    series = fact["cap"] if measure == "cap" else fact["sct"]
    mlabel = "compensation actually paid" if measure == "cap" else "Summary Compensation Table total"
    now, prev = fact["ceos_by_year"].get(fy, []), fact["ceos_by_year"].get(py, [])
    used = ["ceo_pay_measure", "tsr_test"]
    tv = fact["tsr_value"]

    t_now, how_now = _tsr3(fact, fy)
    t_prev, _ = _tsr3(fact, py)
    if s["tsr_test"] == "declined":
        tsr_fell = None if t_now is None or t_prev is None else t_now < t_prev
        tsr_text = f"3-year TSR went from {_pct(t_prev)} (FY{py}) to {_pct(t_now)} (FY{fy}); {how_now}"
    else:
        tsr_fell = None if t_now is None else t_now < 0
        tsr_text = f"3-year TSR to FY{fy} is {_pct(t_now)}; {how_now}"
    tsr_text += "" if tsr_fell is None else (". It fell." if tsr_fell else ". It did not fall.")
    tsr_step = step(tsr_text, terms_cite(ev, _cell(tv.get(fy)), _cell(tv.get(py))))

    def pay_step(name, verdict=""):
        a, b = (series.get(name) or {}).get(py), (series.get(name) or {}).get(fy)
        return step(f"{name}'s pay ({mlabel}) went from {_money(a)} in FY{py} to {_money(b)} in FY{fy}{verdict}",
                    terms_cite(ev, _cell(b) if b else None, _cell(a) if a else None))

    changed = len(now) != 1 or len(prev) != 1 or now != prev
    steps = []
    if changed:
        used.append("on_ceo_change")
        steps.append(step(f"The pay-versus-performance table lists {', '.join(prev) or 'no one'} as CEO in FY{py} "
                          f"and {', '.join(now) or 'no one'} in FY{fy}, so the CEO changed.",
                          terms_cite(ev, *_name_lists(prev if len(prev) <= 4 else prev[:1]))))
        steps += [pay_step(n, ".") for n in dict.fromkeys(prev + now)]
        steps.append(tsr_step)
        if s["on_ceo_change"] == "flag" or len(now) != 1:
            return rec("FLAG", "EXEC_PAY", "CEO changed, so \"CEO pay rose\" is ambiguous",
                       steps + [step("With different people in the job, there's no single \"CEO pay\" to compare. "
                                     "The setting for CEO changes says to flag it for you.")], ["pay"], used)
        steps = [x for x in steps if x is not tsr_step and not x["text"].startswith(f"{prev[0]}'s")] if prev else steps
        steps.append(step(f"Setting: compare {now[0]}'s own pay year over year."))
    name = now[0] if now else None
    pay_now = (series.get(name) or {}).get(fy)
    pay_prev = (series.get(name) or {}).get(py)
    if not pay_now or not pay_prev:
        return rec("FLAG", "EXEC_PAY", "CEO pay for both years not available", steps + [pay_step(name, ".")], ["pay"], used)
    if tsr_fell is None:
        return rec("FLAG", "EXEC_PAY", "3-year TSR not available", steps + [tsr_step], ["pay"], used)

    pay_rose = pay_now > pay_prev
    steps += [pay_step(name, ". It rose." if pay_rose else ". It did not rise."), tsr_step]
    if pay_rose and tsr_fell:
        steps.append(step("CEO pay rose while 3-year TSR fell, which is exactly the case the policy votes against."))
        return rec("AGAINST", "EXEC_PAY", "CEO pay rose while 3-year TSR fell", steps, ["pay"], used)
    steps.append(step("The policy only opposes say-on-pay when pay rose AND 3-year TSR fell. "
                      + ("TSR didn't fall, so it supports." if pay_rose else "Pay didn't rise, so it supports.")))
    headline = ("Pay rose, but 3-year TSR didn't fall" if pay_rose
                else "CEO pay didn't rise" + (" (TSR fell)" if tsr_fell else ""))
    return rec("FOR", "EXEC_PAY", headline, steps, ["pay"], used)


FEE_ROWS = (("audit", "Audit fees", "Audit Fees"), ("audit_related", "Audit-related fees", "Audit-Related Fees"),
            ("tax", "Tax fees", "Tax Fees"), ("other", "All other fees", "All Other Fees"))


def auditor(fact: dict, s: dict) -> dict:
    ok, why = _fact_ok(fact)
    ev = _ev(fact)
    if not ok:
        return rec("FLAG", "AUDITOR", "Fees not confirmed", [step(why)], ["fees"])
    u = fact["units"]
    unit_note = " (the table is in thousands)" if u == "thousands" else ""
    with_related = s["non_audit_includes_audit_related"]
    steps = []
    for key, label, row in FEE_ROWS:
        v = fact[key]
        note = unit_note if key == "audit" else ""
        if key == "audit_related" and not with_related:
            note = ". Not counted as non-audit (setting)"
        steps.append(step(f"{label}, FY{fact['year']}: {_money(v, u)}{note}.", terms_cite(ev, _cell(v), row)))
    parts = [("tax", fact["tax"]), ("all other", fact["other"])]
    if with_related:
        parts.insert(0, ("audit-related", fact["audit_related"]))
    non_audit = sum(v for _, v in parts)
    audit = fact["audit"]
    over = non_audit > audit
    steps.append(step(f"Non-audit fees = {' + '.join(f'{n} {_money(v, u)}' for n, v in parts)} = {_money(non_audit, u)}, "
                      f"which {'exceeds' if over else 'does not exceed'} audit fees of {_money(audit, u)}."))
    if over:
        return rec("AGAINST", "AUDITOR", f"Non-audit fees {_money(non_audit, u)} exceed audit fees {_money(audit, u)}",
                   steps, ["fees"], ["non_audit_includes_audit_related"])
    return rec("FOR", "AUDITOR", f"Non-audit {_money(non_audit, u)} ≤ audit {_money(audit, u)}",
               steps, ["fees"], ["non_audit_includes_audit_related"])


def _asks(item):
    s = item.get("summary")
    if not s:
        return None
    lead = "The resolution reads" if item.get("summary_kind") == "resolution" else "The filing describes it as"
    return step(f"{lead}: “{_short(s['quote'])}”", quote_cite(s))


def board_structure(item: dict, category: str, s: dict) -> dict:
    title = item["proposal_title"] or item["title"]
    asks = [x for x in [_asks(item)] if x]
    if category == "majority_voting" and re.search(r"supermajority", title, re.I):
        if not s["supermajority_is_majority_voting"]:
            return rec("FLAG", "BOARD_STRUCTURE", "Supermajority proposal not counted as majority voting",
                       asks + [step("It changes approval thresholds, not how directors are elected. The setting "
                                    "says only a majority standard for director elections counts.")], [],
                       ["supermajority_is_majority_voting"])
        return rec("FOR", "BOARD_STRUCTURE", "Policy supports majority voting",
                   asks + [step("It replaces supermajority requirements with a majority of votes, which the setting "
                                "counts as \"majority voting\". The policy supports majority voting.")], [],
                   ["supermajority_is_majority_voting"])
    what = "declassifying the board" if category == "declassify_board" else "majority voting"
    return rec("FOR", "BOARD_STRUCTURE", f"Policy supports {what}",
               asks + [step(f"This asks for {what}, which the policy supports.")], [])


# --------------------------------------------------------------------------- #

def recommend(item: dict, cls: dict, facts: dict, s: dict) -> dict:
    asks = [x for x in [_asks(item)] if x]
    if cls["status"] == "disagree":
        return rec("FLAG", "FLAG", "Unclear which rule applies", asks + [step(cls["note"])])
    if cls["status"] == "unverified":
        return rec("FLAG", "FLAG", "Classification not verified", asks + [step(cls["note"])])
    rule = cls["rule"]
    if rule == "DIRECTORS":
        name = re.sub(r"^Elect\s+", "", item["title"])
        return directors(name, facts["attendance"], s)
    if rule == "EXEC_PAY":
        return say_on_pay(facts["pay"], s)
    if rule == "AUDITOR":
        return auditor(facts["fees"], s)
    if rule == "BOARD_STRUCTURE":
        return board_structure(item, cls["category"], s)
    why = (cls.get("llm") or {}).get("reason")
    return rec("FLAG", "FLAG", "Not covered by the policy",
               asks + [step(f"The policy's rules cover {RULES_COVER}. None of them covers this"
                            + (f": {why[0].lower() + why[1:].rstrip('.')}." if why else ".")),
                       step("So there's no automatic vote. It's your call.")])
