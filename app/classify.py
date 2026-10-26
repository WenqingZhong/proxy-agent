"""Which kind of item is this? Keywords and the LLM each answer; we compare.

Outcome per proposal ("status"):
  agreed         both say the same category (and direction)  -> use it
  keywords_only  LLM unavailable                             -> use keywords, say so
  unconfirmed    keywords put a shareholder proposal in a rule
                 with no LLM answer, or the LLM skipped it    -> FLAG for the analyst
  disagree       they differ                                 -> FLAG for the analyst
  unverified     LLM's supporting quote isn't in the filing  -> FLAG for the analyst
"""
from __future__ import annotations

import re

RULE_FOR_CATEGORY = {
    "director_election": "DIRECTORS",
    "say_on_pay": "EXEC_PAY",
    "auditor_ratification": "AUDITOR",
    "declassify_board": "BOARD_STRUCTURE",
    "majority_voting": "BOARD_STRUCTURE",
    "other": "FLAG",
}

CATEGORY_LABEL = {
    "director_election": "Director election",
    "say_on_pay": "Say-on-pay",
    "auditor_ratification": "Auditor ratification",
    "declassify_board": "Declassify the board",
    "majority_voting": "Majority voting",
    "other": "Not covered by policy",
}


def keyword_category(title: str) -> dict:
    t = title.lower()
    direction = "n/a"
    if re.search(r"\belect(?:ion)? of (?:\d+ |the )?directors?\b|^elect\b", t):
        cat = "director_election"
    elif re.search(r"advisory vote|say.on.pay", t) and re.search(r"compensation|pay", t) and "frequency" not in t:
        cat = "say_on_pay"
    elif re.search(r"ratif", t) and re.search(r"accounting firm|auditor", t):
        cat = "auditor_ratification"
    elif re.search(r"declassif|eliminat\w* (?:the |our )?classified board|annual election of (?:all )?directors", t):
        cat, direction = "declassify_board", "toward"
    elif re.search(r"majority vot|simple majority|eliminat\w* supermajority|replac\w* supermajority", t):
        cat, direction = "majority_voting", "toward"
    else:
        cat = "other"
    if cat in ("declassify_board", "majority_voting") and re.search(
            r"\bclassify the board|adopt a classified|plurality|supermajority (?:vote )?requirement for", t):
        direction = "away"
    return {"category": cat, "direction": direction}


def merge(title: str, llm_answer: dict | None, llm_error: str | None,
          shareholder: bool = False, skipped: bool = False) -> dict:
    kw = keyword_category(title)
    out = {"keyword": kw, "llm": llm_answer, "llm_error": llm_error}

    def finish(cat, direction, status, note):
        rule = RULE_FOR_CATEGORY[cat]
        if cat in ("declassify_board", "majority_voting") and direction != "toward":
            rule, note = "FLAG", note + " The item would move away from the policy's position, which the policy doesn't cover."
        if status in ("disagree", "unverified", "unconfirmed"):
            rule = "FLAG"
        out.update(category=cat, direction=direction, status=status, note=note,
                   rule=rule, label=CATEGORY_LABEL[cat])
        return out

    if not llm_answer:
        if kw["category"] != "other" and (shareholder or skipped):
            # Keywords alone would auto-vote this. Shareholder proposals often only sound like a
            # covered item ("advisory vote on golden parachute pay"), and an item the LLM was
            # asked about but skipped has no second opinion; both go to her.
            why = ("the LLM was asked about it but gave no answer" if skipped
                   else f"the LLM wasn't used ({llm_error or 'unavailable'})")
            return finish(kw["category"], kw["direction"], "unconfirmed",
                          f"Keywords suggest {CATEGORY_LABEL[kw['category']]}, but {why}. "
                          + ("Shareholder proposals often only sound like a covered item, so " if shareholder else "")
                          + "it isn't voted on keywords alone.")
        return finish(kw["category"], kw["direction"], "keywords_only",
                      f"LLM not used ({llm_error or 'unavailable'}); category from keywords only.")
    if not llm_answer.get("quote_verified"):
        return finish(kw["category"], kw["direction"], "unverified",
                      "The LLM's supporting quote wasn't found in the filing, so its answer isn't trusted.")
    same = llm_answer["category"] == kw["category"] and (
        kw["category"] not in ("declassify_board", "majority_voting")
        or llm_answer.get("direction") == kw["direction"])
    if same:
        return finish(kw["category"], kw["direction"], "agreed", "Keywords and LLM agree.")
    return finish(llm_answer["category"], llm_answer.get("direction", "n/a"), "disagree",
                  f"Keywords say {CATEGORY_LABEL[kw['category']]}; LLM says {CATEGORY_LABEL[llm_answer['category']]}.")
