"""The voting policy, hard-coded for now (from the Pennant brief).

RULES are what she was given. SETTINGS are the judgement calls the rules leave
open; each one is shown in the app next to the recommendations it affects, so
nothing is decided silently. Defaults are set here; she can change them on the
policy page (saved to data/policy_settings.json), which re-analyses every filing.
"""
from __future__ import annotations

import json
from pathlib import Path

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
            "covers": ["director_election"],
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
            "covers": ["declassify_board", "majority_voting"],
            "needs": [],
        },
        {
            "id": "AUDITOR",
            "section": "Auditor",
            "name": "Non-audit > audit",
            "text": "Support ratification unless non-audit fees exceed audit fees.",
            "default": "FOR",
            "covers": ["auditor_ratification"],
            "needs": ["Audit fees", "Non-audit fees"],
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
    "settings": [
        {
            "id": "ceo_pay_measure",
            "rule": "EXEC_PAY",
            "label": "CEO pay measure",
            "value": "sct",
            "options": {"sct": "Summary Compensation Table total", "cap": "Compensation actually paid"},
            "why": "The two can move in opposite directions (Amazon 2025: table total rose, actually paid fell).",
        },
        {
            "id": "tsr_test",
            "rule": "EXEC_PAY",
            "label": "\"3-year TSR fell\" means",
            "value": "declined",
            "options": {"declined": "3-year TSR is lower than last year's 3-year TSR",
                        "negative": "3-year TSR is below zero"},
            "why": "The policy wording supports both readings.",
        },
        {
            "id": "on_ceo_change",
            "rule": "EXEC_PAY",
            "label": "If the CEO changed",
            "value": "flag",
            "options": {"flag": "Flag for the analyst",
                        "current_ceo": "Compare the current CEO's own pay year over year"},
            "why": "\"CEO pay rose\" has no clean answer when different people held the job (Starbucks 2024).",
        },
        {
            "id": "non_audit_includes_audit_related",
            "rule": "AUDITOR",
            "label": "Non-audit fees include audit-related fees",
            "value": True,
            "options": {True: "Yes: audit-related + tax + all other", False: "No: tax + all other only"},
            "why": "SEC categories put audit-related fees outside \"audit fees\", but some policies count them as audit work.",
        },
        {
            "id": "supermajority_is_majority_voting",
            "rule": "BOARD_STRUCTURE",
            "label": "Replacing supermajority requirements counts as \"majority voting\"",
            "value": True,
            "options": {True: "Yes", False: "No: only a majority standard for director elections"},
            "why": "\"Majority voting\" usually means director elections; Starbucks Prop 4 is about approval thresholds.",
        },
    ],
}

SETTINGS = {s["id"]: s["value"] for s in POLICY["settings"]}   # the defaults above

# Changes she makes in the app are saved here, over the defaults (gitignored).
SETTINGS_FILE = Path(__file__).resolve().parents[1] / "data" / "policy_settings.json"


def current_settings(path: Path | None = None) -> dict:
    p = Path(path or SETTINGS_FILE)
    saved = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {k: saved.get(k, v) for k, v in SETTINGS.items()}


def set_setting(setting_id: str, value, path: Path | None = None) -> dict:
    """Save one setting. Raises ValueError if the id or value isn't one of the options."""
    s = next((x for x in POLICY["settings"] if x["id"] == setting_id), None)
    if not s:
        raise ValueError(f"Unknown setting: {setting_id}.")
    match = [o for o in s["options"] if str(o).lower() == str(value).lower()]
    if not match:
        raise ValueError(f"{s['label']} must be one of: {', '.join(map(str, s['options']))}.")
    p = Path(path or SETTINGS_FILE)
    values = current_settings(p)
    values[setting_id] = match[0]
    changed = {k: v for k, v in values.items() if v != SETTINGS[k]}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(changed, indent=2), encoding="utf-8")
    return values


def policy_view(settings: dict) -> dict:
    """POLICY with each setting's current value (and its default) for the app."""
    return {**POLICY, "settings": [{**s, "default": s["value"], "value": settings[s["id"]]}
                                   for s in POLICY["settings"]]}
