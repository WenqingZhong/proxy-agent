# Proxy Agent

A small local app for a stewardship analyst reviewing proxy ballots against a voting policy.

For each filing it lists the ballot (every nominee and proposal), quotes what each item asks,
recommends **FOR / AGAINST / REVIEW** under the policy, and explains why. Each reason is one
specific claim (a fee, a pay figure, the attendance sentence) linked to the exact words in the
live filing on SEC.gov. She accepts the policy's vote or overrides it with a reason, then exports
the final votes.

## Run

Python 3.9+, no packages to install.

```bash
python3 server.py                 # http://localhost:8000
python3 server.py 8080            # another port
python3 -m unittest discover tests
```

AI mode is chosen automatically (shown as a pill next to each filing's title):

| Mode | When | What happens |
|---|---|---|
| live | `ANTHROPIC_API_KEY` is set | Claude classifies items and finds pages; answers cached in `data/llm_cache/` |
| stub | no key, stub file exists | answers from `data/llm_stub/<filing>.json` (hand-written for the 3 samples) |
| off  | neither | code and keywords only, and the app says so |

Force one with `LLM_MODE=live|stub|off`. Model: `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).

### Add a filing

Save the DEF 14A `.htm` from EDGAR into `data/filings/`, and add its EDGAR URL to
`data/filings/sources.json` so evidence links point at the live filing. Then restart, or change
a policy setting (which re-analyses every filing).

## How a recommendation is made

```
filing.htm
  │  pages.py      split into printed pages; parse tables (colspan/rowspan aware)
  │  extract.py    ballot items, board recommendation, nominees, the RESOLVED text
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
  · each reason is one claim citing where it comes from in the filing
  · missing / unconfirmed fact → REVIEW, never the default vote
  · judgement calls the policy leaves open are SETTINGS in policy.py
    (CEO pay measure, meaning of "TSR fell", CEO changes, what counts as non-audit,
    whether supermajority→majority counts as "majority voting"), shown with each result
  ▼
LINKS (links.py)                             citation → SEC.gov URL with a text fragment
  · built from the filing as a browser renders it, so the browser highlights the exact words
  · "exact" only if the browser's first match is the cited passage; otherwise the link
    lands on the right page and says so
```

The LLM never chooses a vote and never supplies a number.

**Decisions.** Votes are FOR or AGAINST. Choosing the policy's vote accepts it; the other vote is
an override and needs a reason. On a REVIEW item either vote is an override. If a later
re-analysis changes a recommendation, her decision is marked to re-confirm instead of being
changed for her.

## API

JSON over HTTP on the same port as the app. Only the app's own page may call it: requests whose
`Host` or `Origin` isn't `localhost` / `127.0.0.1` on the server's port get `403`, and so does a
`POST` whose `Content-Type` isn't `application/json`.

Errors are `{"error": "<message>"}`: `400` for a request that doesn't make sense (nothing is
saved), `403` as above, `404` for an unknown filing, export or route, and `409` with
`"conflict": true` when the recommendation changed since the page was loaded.

`<id>` is a filing id: its file name without `.htm`, e.g. `starbucks-2026-def14a`.

### Read

| Method & path | Returns |
|---|---|
| `GET /api/policy` | The policy: `rules` (id, section, text, default vote, what it needs) and `settings`, each with its current `value`, its `default` and its `options`. |
| `GET /api/filings` | One summary per filing: `id`, `company`, `meeting_date`, `source_file`, `item_count`, `flag_count`, `decided_count`, `review_count` (decisions to look at again), `export_outdated`, `llm_mode`, `warnings`. |
| `GET /api/filings/<id>` | The full analysis. Top level: `company`, `meeting_date`, `items`, `facts`, `settings`, `source_url`, `run` (filing hash, policy version, settings, AI mode). Each item: `number`, `title`, `proponent`, `board_rec` and `board_rec_evidence`, `summary` (what is being voted on, verbatim) with `summary_kind`, `classification`, and `recommendation`: `vote`, `rule`, `headline` and `steps`. Each step is `{text, cite, link}`, and `link` is `{url, page, exact}`. |
| `GET /api/filings/<id>/decisions` | Her decisions. `items`: per item `state` (`undecided`, `accepted`, `overridden`, `decided`, `stale`), `final_vote`, `reason`, `decided_at`, `history`; stale items also carry `change` and `note`. Plus `decided`, `total`, `undecided`, `overridden`, `review` (item numbers to revisit, grouped by why) and `export` (her last export and whether it's outdated). |
| `GET /api/filings/<id>/exports/<export id>.csv` or `.json` | A saved export, byte for byte as it was downloaded. |
| `GET /api/filings/<id>/pages/<index>` | One page of the filing as parsed: `index`, `label` (printed page number), `lines`, `blocks`, `page_count`. |

### Change

| Method & path | Body | Does |
|---|---|---|
| `POST /api/filings/<id>/decisions` | `{items, action, vote?, reason?, expect}` | Records a decision for each item number in `items`. `action`: `accept` (take the policy's vote), `vote` (`vote` is `FOR` or `AGAINST`; `reason` required when it differs from the policy, and for REVIEW items), `confirm` (keep her earlier vote against the new recommendation), or `clear` (undo). `expect` maps each item number to the policy vote she was shown. It's required for every action except `clear`, and if any differs, nothing is saved and the answer is `409`. Returns the same payload as `GET …/decisions`. |
| `POST /api/filings/<id>/exports` | `{format: "csv" \| "json"}` | Saves the export with a timestamp and returns `saved` (`id`, which is the timestamp, and `format`), `download` (its URL) and the decisions payload, whose `export.history` lists every saved export. |
| `POST /api/policy/settings` | `{id, value}` | Saves one policy setting (`value` must be one of its options), then re-analyses every filing. Returns `policy`, `reports` and `filings`. |
| `POST /api/reanalyze` | `{filing?}` | Reads the filing (or all of them) from disk again and re-runs it with the current settings and AI. Decisions are never changed. Returns `reports` (per filing: `causes`, `recommendations_changed`, `items_added`, `items_removed`, `review`) and `filings`. |

Example:

```bash
curl -s localhost:8000/api/filings/starbucks-2026-def14a/decisions \
  -H 'Content-Type: application/json' \
  -d '{"items": ["4"], "action": "vote", "vote": "AGAINST",
       "reason": "Keep supermajority protections", "expect": {"4": "FOR"}}'
```

## Files

```
server.py            stdlib HTTP server + JSON API
app/pages.py         page splitting, tables
app/tables.py        match table values to column headers
app/extract.py       ballot items, the RESOLVED text / lead paragraph
app/facts.py         attendance / fees / pay, by code
app/llm.py           Claude client (urllib), cache, stub/off modes
app/ai.py            LLM jobs: classify, locate, read attendance; quote verification
app/classify.py      keywords vs LLM → rule
app/rules.py         the policy as functions; each reason cites the filing
app/links.py         citations → SEC.gov text-fragment links
app/policy.py        rules text + settings (changes saved to data/policy_settings.json)
app/pipeline.py      wires it together
app/decisions.py     her decisions, re-confirmation after re-analysis, export rows
app/exports.py       saved exports and whether the latest is outdated
data/filings/        the filings + sources.json (filing → EDGAR URL)
static/              front end (plain JS, no build step)
tests/               91 tests: 3 filings end to end, failure paths, decisions, exports,
                     re-analysis, links, server request checks
```

Her decisions (`data/decisions/`), saved exports (`data/exports/`), setting changes
(`data/policy_settings.json`) and the AI cache (`data/llm_cache/`) are kept out of git.
