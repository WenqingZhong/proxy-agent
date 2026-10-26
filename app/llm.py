"""Minimal Claude client (stdlib only) with a disk cache and a stub mode.

Modes (env LLM_MODE overrides the automatic choice):
  live   ANTHROPIC_API_KEY is set: call the API; every answer is cached in
         data/llm_cache/ so re-runs (and demos) are free and repeatable.
  stub   no key: answers come from data/llm_stub/<filing>.json, hand-written
         for the sample filings so the workflow can be shown offline.
  off    no key and no stub: LLM steps are skipped; code results stand alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "llm_cache"
STUB_DIR = ROOT / "data" / "llm_stub"

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def mode_for(filing_id: str) -> str:
    forced = os.environ.get("LLM_MODE")
    if forced in ("live", "stub", "off"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "live"
    if (STUB_DIR / f"{filing_id}.json").exists():
        return "stub"
    return "off"


def stub_answer(filing_id: str, task: str):
    path = STUB_DIR / f"{filing_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get(task)


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in response")
    end = max(text.rfind("}"), text.rfind("]"))
    return json.loads(text[start:end + 1])


def ask_json(system: str, user: str, max_tokens: int = 2000):
    """Call Claude and parse a JSON answer. Returns (data, error)."""
    key = hashlib.sha256(f"{MODEL}\n{system}\n{user}".encode()).hexdigest()[:24]
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8")), None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, "No ANTHROPIC_API_KEY set"
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read())
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        data = _extract_json(text)
    except urllib.error.HTTPError as e:
        return None, f"API error {e.code}: {e.read()[:200].decode(errors='ignore')}"
    except Exception as e:  # network, timeout, bad JSON
        return None, f"LLM call failed: {e}"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data, None
