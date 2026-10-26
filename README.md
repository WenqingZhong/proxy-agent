# Proxy Agent

A small local app for a stewardship analyst reviewing proxy ballots against a voting policy.

For each filing it lists the ballot (every nominee and proposal), recommends **FOR / AGAINST / REVIEW**
under the policy, and shows how: the rule, the comparison with real numbers, and the filing
text each number came from (click a page to open it with the passage highlighted).

## Run

Python 3.9+, no packages to install.

```bash
python3 server.py                 # http://localhost:8000
python3 -m unittest discover tests
```

AI mode is chosen automatically (shown as a pill next to each filing's title):

| Mode | When | What happens |
|---|---|---|
| live | `ANTHROPIC_API_KEY` is set | Claude classifies items and finds pages; answers cached in `data/llm_cache/` |
| stub | no key, stub file exists | answers from `data/llm_stub/<filing>.json` (hand-written for the 3 samples) |
| off  | neither | code and keywords only, and the app says so |

Force one with `LLM_MODE=live|stub|off`. Model: `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).

## How a recommendation is made

```
filing.htm
  │  pages.py      split into printed pages; parse tables (colspan/rowspan aware)
  │  extract.py    ballot items, board recommendation, nominees (proxy card or notice)
  ▼
FACTS (facts.py, code)                       cross-checked by the LLM locator (ai.py)
  attendance   "each director attended at least 75%…" sentence
  fees         fee table → latest fiscal year by column header, units, sum check
  pay          pay-versus-performance table → CEO pay per person per year, TSR
  · keywords find the page, code reads it
  · LLM independently names the pages; disagreement → REVIEW
  · keywords miss → LLM's page is read by code
  · attendance with exceptions (prose) → LLM reads it, quote must exist on the page
  ▼
CLASSIFICATION (classify.py)                 which rule applies to each proposal
  · keywords and LLM each choose a category (+ direction for board structure)
  · agree → rule; disagree, or LLM quote not in filing → REVIEW
  ▼
RULES (rules.py, plain comparisons)
  · missing / unconfirmed fact → REVIEW, never the default vote
  · judgement calls the policy leaves open are SETTINGS in policy.py
    (CEO pay measure, meaning of "TSR fell", CEO changes, what counts as non-audit,
    whether supermajority→majority counts as "majority voting"), shown with each result
```

The LLM never chooses a vote and never supplies a number.

## Files

```
server.py            stdlib HTTP server + JSON API
app/pages.py         page splitting, tables
app/tables.py        match table values to column headers
app/extract.py       ballot items
app/facts.py         attendance / fees / pay, by code
app/llm.py           Claude client (urllib), cache, stub/off modes
app/ai.py            LLM jobs: classify, locate, read attendance; quote verification
app/classify.py      keywords vs LLM → rule
app/rules.py         the policy as functions
app/policy.py        rules text + settings
app/pipeline.py      wires it together
static/              front end (plain JS)
tests/               31 tests: 3 filings end to end + failure paths
```

## Not built yet

Accept / override with a reason, export of final votes.
