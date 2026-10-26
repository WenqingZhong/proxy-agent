"""The voting policy, hard-coded for now (from the Pennant brief).

Each rule says which kind of ballot item it covers. Items no rule covers fall
through to FLAG: no automatic vote, the analyst decides.
"""
from __future__ import annotations

POLICY = {
    "name": "Sample voting policy",
    "version": "2026-10",
    "rules": [
        {
            "id": "DIRECTORS",
            "section": "Directors",
            "name": "Under 75% attendance",
            "text": "Vote against any director who attended fewer than 75% of board and committee meetings.",
            "default": "FOR",
            "covers": ["director"],
            "needs": ["Each nominee's board and committee meeting attendance"],
        },
        {
            "id": "EXEC_PAY",
            "section": "Executive pay",
            "name": "Pay up, TSR down",
            "text": "Support say-on-pay unless CEO pay rose while 3-year total shareholder return fell.",
            "default": "FOR",
            "covers": ["say_on_pay"],
            "needs": ["CEO pay, this year and last", "3-year TSR, this year and last"],
        },
        {
            "id": "BOARD_STRUCTURE",
            "section": "Board structure",
            "name": "Support",
            "text": "Support declassifying the board and majority voting.",
            "default": "FOR",
            "covers": ["board_structure"],
            "needs": [],
        },
        {
            "id": "AUDITOR",
            "section": "Auditor",
            "name": "Non-audit > audit",
            "text": "Support ratification unless non-audit fees exceed audit fees.",
            "default": "FOR",
            "covers": ["auditor"],
            "needs": ["Audit fees", "Non-audit fees (audit-related + tax + all other)"],
        },
        {
            "id": "FLAG",
            "section": "Anything else",
            "name": "Flag",
            "text": "No automatic vote. Flag it for the analyst.",
            "default": None,
            "covers": ["other"],
            "needs": [],
        },
    ],
}

_BY_KIND = {kind: r["id"] for r in POLICY["rules"] for kind in r["covers"]}


def rule_for(kind: str) -> str:
    return _BY_KIND.get(kind, "FLAG")
