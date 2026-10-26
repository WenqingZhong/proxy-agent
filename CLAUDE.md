# CLAUDE.md: Proxy Agent

Context for Claude Code. Read this before changing anything.

## The project

Take-home for Pennant (founding engineer, product). **Due Wed Oct 28, 2026**, to tommy@getpennant.ai.
Budget ~3–4 h of build time. Followed by a 45-min walkthrough where Tommy and Ryan play the analyst.

The whole brief from the user (a stewardship analyst at a mid-sized asset manager):
> "I review about 40 proxy ballots a week against our voting policy, and it takes forever. Can you help?"

Minimum the app must do:
1. Load a filing and see the items on the ballot.
2. See a recommended vote per item, with the policy rule and the filing page it's based on.
3. Accept or override each recommendation, with a reason.
4. Export final votes, including what she changed and why.

Graders look at: scoping a vague ask, works end to end, handling a recommendation she disagrees
with and items the policy doesn't cover, clarity of explanation. "Keep the AI part simple; a basic
LLM call or a stub is fine. We care more about the workflow than the model."

Also deliver a **half-page note**: who the user is / what she needs, what was cut and why,
what to ship first, what to ask her before building more.

Sample voting policy (hard-coded in `app/policy.py`):
- DIRECTORS: vote against any director who attended < 75% of board and committee meetings.
- EXECUTIVE PAY: support say-on-pay unless CEO pay rose while 3-year TSR fell.
- BOARD STRUCTURE: support declassifying the board and majority voting.
- AUDITOR: support ratification unless non-audit fees exceed audit fees.
- ANYTHING ELSE: no automatic vote, flag for the analyst.

Filings in `data/filings/` (the app must work for all three): Starbucks 2026 DEF 14A (the required
one), Alphabet 2026, Amazon 2026.

## Run

```bash
python3 server.py                  # http://localhost:8000 (port arg optional)
python3 -m unittest discover tests # 67 tests, all should pass
```
Python 3.9+ standard library only, no pip installs. That was deliberate: package downloads were
blocked in the original build environment, and a zero-install app is easy to demo.
Front end is plain JS/CSS in `static/`, no build step.

## Core design decisions (agreed with the user; keep them)

1. **The LLM never chooses a vote and never supplies a number.** Pipeline:
   classify item (keywords + LLM) → find facts (code reads tables; LLM only locates pages / reads
   prose) → rules are plain Python comparisons → explanation shown to the analyst.
2. **Don't let an LLM read the whole filing.** Risks we discussed: picking a near-duplicate number
   (Alphabet lists 2024 fees *before* 2025; the wrong column flips the auditor vote: $34.4M vs
   $31.1M AGAINST, versus the correct $30.8M vs $37.9M FOR), silently choosing definitions,
   hallucinated citations, lost-in-the-middle on ~100k-word docs, nondeterminism, prompt
   injection from shareholder proposals, arithmetic slips.
3. **Two independent locators, cross-checked.** Keywords and the LLM each say which page holds a
   fact. Agree → proceed. Disagree → FLAG. Keywords miss → the LLM's page is read by *code*.
   Neither → FLAG.
4. **Code reads tables** (fees, pay-versus-performance). **The LLM reads prose** only when code can't
   settle it (an attendance sentence naming exceptions). Every LLM quote must appear verbatim on the
   page, or it's not trusted.
5. **Classification** (which rule applies) = keywords + LLM pick from a fixed category list
   (`director_election, say_on_pay, auditor_ratification, declassify_board, majority_voting, other`)
   plus direction ("toward"/"away") for board structure. Agree → rule. Disagree, or the LLM quote
   isn't found → FLAG. The failure modes are lopsided: wrongly "other" is safe (she reviews it);
   wrongly *in* a rule gives a wrong auto-vote.
6. **A missing or unconfirmed fact always gives FLAG, never the rule's default FOR.**
7. **Judgement calls the policy leaves open are explicit SETTINGS** in `app/policy.py`, shown in the UI
   next to every result they affect:
   - `ceo_pay_measure`: `sct` (default) | `cap`. They diverge: Amazon 2025 SCT rose, CAP fell.
   - `tsr_test`: `declined` (default; 3-yr TSR lower than last year's 3-yr TSR) | `negative`.
   - `on_ceo_change`: `flag` (default) | `current_ceo`. Starbucks had 3 CEOs in FY2024.
   - `non_audit_includes_audit_related`: True (default).
   - `supermajority_is_majority_voting`: True (default). Starbucks Prop 4 is about approval
     thresholds, not director elections.
   These are also the "questions to ask her" for the note.
8. UI wording: internal vote `FLAG` is displayed as **REVIEW**.

## Expected results (also pinned in tests)

| | Starbucks | Alphabet | Amazon |
|---|---|---|---|
| Ballot | 11 nominees + Props 2–9 (19 items) | 10 nominees + 2–14 (23) | 11 nominees + 2–7 (17) |
| Directors | FOR (blanket ≥75% sentence, p. 29) | FOR (p. 24) | FOR (p. 20) |
| Auditor | FOR: non-audit $354K ≤ audit $9.77M (thousands) | FOR: $30.80M ≤ $37.90M (FY2025 is the 2nd column) | FOR: $12.00M ≤ $47.26M (dollars) |
| Say-on-pay | **FLAG**: CEO changed (Narasimhan, Ruggeri, Niccol in FY2024); TSR 3-yr +25.7% → +10.5% | FOR: pay rose, TSR rose | FOR: Jassy SCT $1.60M → $2.07M rose, TSR rose |
| Board structure | Prop 4 supermajority→majority: **FOR**; the board makes **no recommendation** | Prop 7 "equal voting" (one vote per share) → FLAG, not majority voting | n/a |
| Other | Props 5–9 FLAG | Prop 3 stock plan + shareholder props FLAG | Props 4–7 FLAG |

## Code map

```
server.py          stdlib ThreadingHTTPServer; parses all filings at startup.
                   API: GET /api/policy, /api/filings, /api/filings/<id>, /api/filings/<id>/pages/<index>,
                   /api/filings/<id>/decisions (states + export status), /api/filings/<id>/exports/<eid>.csv|json;
                   POST /api/filings/<id>/decisions, /api/filings/<id>/exports {format}, /api/reanalyze {filing?}, /api/policy/settings {id, value}
app/pages.py       split HTML on CSS page-breaks; printed page number from footer (cleaned to a
                   monotonic sequence); Page.lines, .tables, .tables_html, .blocks (for display);
                   table_grid() is colspan/rowspan-aware
app/tables.py      year_table(): one-row-per-year tables → {columns, rows}; matches values to
                   header leaves by order (handles Alphabet's misaligned colspans), grid fallback
app/extract.py     company, meeting date, ballot items. Headings "PROPOSAL 1 –" / "Proposal Number 1:"
                   / "ITEM 1—"; section page from the summary's page reference; board rec incl.
                   "makes no recommendation" → NONE; nominees from the proxy card (1a. 1b. …),
                   else the meeting notice (Alphabet has no card)
app/facts.py       code finders: find_attendance / find_fees / find_pay + keyword_pages locator.
                   Each fact: status found|needs_review|missing, evidence [{page, page_index,
                   quote, find, kind: text|table}], method, locator
app/llm.py         Claude client via urllib; disk cache data/llm_cache/; modes live|stub|off
                   (auto: key → live; stub file → stub; else off; override with LLM_MODE).
                   Model env ANTHROPIC_MODEL (default claude-sonnet-5-5). Live path is only tested
                   against a mocked HTTP response so far; it has never hit the real API.
app/ai.py          LLM jobs: classify_items, locate_facts (page index of headings + table labels),
                   read_attendance; quote_on_pages() verification
app/classify.py    keyword_category() + merge() → status agreed|keywords_only|disagree|unverified
app/rules.py       directors / say_on_pay / auditor / board_structure / recommend(); each returns
                   {vote, rule, headline, steps, facts, settings}
app/policy.py      POLICY (rules + settings), SETTINGS (defaults); current_settings() / set_setting()
                   overlay data/policy_settings.json (gitignored); policy_view() for the app
app/decisions.py   analyst decisions (accept/override/confirm), states, re-analysis diff, CSV/JSON export
app/exports.py     saved exports in data/exports/<id>/<timestamp>.json (gitignored); history; status()
                   compares the latest export with what she'd export now
app/pipeline.py    analyze(filing_id, raw_html, settings) → filing dict + pages; filing["run"] records
                   the inputs (filing hash, settings, LLM mode/model, stub hash)
data/llm_stub/     hand-written stand-in LLM answers for the 3 samples (categories are a human
                   reading; quotes copied from the filings). Labelled "AI: stub" in the UI. Be
                   upfront about this in the walkthrough.
tests/             test_ballots.py (extraction, vote summaries), test_decisions.py, test_recommendations.py (facts, recs, failure
                   paths), test_reanalysis.py, test_exports.py, test_llm_client.py (mocked API)
```

Gotchas found along the way:
- EDGAR tables split values over cells ("$" | "47,260,000"); footnote cells "(4)"; ordinal cells "th".
- Starbucks hyphenates headers ("Compen- sation"); normalise with `(\w)- (\w)` → `\1\2`.
- Starbucks' TSR column is already 3-year; Amazon/Alphabet are cumulative from a base year, so
  3-yr TSR = V[y]/V[y-3] − 1.
- Fee units: "in thousands" on the page (Starbucks, Alphabet) vs dollars (Amazon).
- Section page windows must stop *before* the next item's section, or the LLM sees the wrong
  proposal's text.

## Status / next steps

Done: browse filings, ballot, board recs, facts, classification, recommendations, policy page with
settings, tests (67). Plus:
- **Item view** (`#/filing/<id>/item/<n>`), one column, top to bottom: *What you're voting on*
  (verbatim RESOLVED text, else the section's lead paragraph: `summary` / `summary_kind` from
  `extract._find_summary`), *Recommendation* (board rec, then the policy rec with its rule text,
  settings applied and numbered reasons), *Your decision*: two buttons, FOR and AGAINST. The
  policy's vote accepts; the other is an override with a required reason (on REVIEW items both are
  overrides). No in-page keyboard hint, no rail footer, no "how the rule was matched" panel.
  Each reason is one specific claim (a fee cell, a pay figure, the attendance sentence) with its
  own link to the live filing on SEC.gov (`data/filings/sources.json`). Links carry a text
  fragment (`#:~:text=`) built by `app/links.py` from the filing as a browser renders it, so the
  browser highlights the exact words; a link is only "exact" if the browser's first match is the
  cited passage (190/192 are; the rest land on the right page and say so). There's no in-app
  page viewer any more. Left rail expands the selected filing's items. Keys: ←/→, A (accept),
  O (override), Esc.
- **Decisions** (`app/decisions.py`, `data/decisions/<id>.json`, gitignored): accept, or vote
  FOR or AGAINST with a reason (required when it differs from the policy, and for REVIEW items).
  Only FOR and AGAINST exist: no abstain or absent (user's call). Stale if the recommendation changes later.
  Bulk-accept passing nominees. Auto-advance to the next open item.
- **Export** CSV + JSON per filing; warns if undecided. Every export is saved first with its
  timestamp (`POST /api/filings/<id>/exports`), then downloaded from the saved copy, so past exports
  re-download exactly as sent (export history on the overview). If her votes, reasons, decision
  states or the policy recommendations differ from the latest export, the overview says it's out
  of date and lists what changed per item; the rail shows "export outdated". Timestamp-only changes
  (re-confirming the same vote) don't count.
- **Re-analysis** (agreed with the user): when a setting, the filing or the AI changes, every item is
  re-run and her decisions are checked against the new recommendations. Each decision stores an
  item `key` (number + title), a `basis` (vote, rule, category, board rec, settings used, the fact
  values the rule read, evidence pages) and the `run` inputs. Worked out on read:
  `stale` + `change` = `conflicts` (new rec ≠ her vote) | `now_agrees` (new rec = her vote) |
  `item_changed` (number now a different item) → blocks completeness until she acts;
  `basis_changed` (same vote, different facts/settings/pages) → **notify only**, still counts;
  decisions for items no longer on the ballot → `review.removed`, kept, not exported.
  **Nothing is ever changed for her automatically** (user's call): `now_agrees` needs a click
  (action `confirm` keeps her vote + reason against the current recommendation). Old decisions go
  to `history`; `reconfirmed_after` lists what changed; both reach the export.
  Triggers: editing a setting on the policy page (re-analyses all filings), the Re-analyse button
  (one filing; a new filing file needs a settings change or a restart), or a restart with a different
  `ANTHROPIC_MODEL` / `LLM_MODE` (decisions compare their stored `run` on read).

UI: desktop web only, no mobile styles (user's call).

Still to do:
1. Let her **correct a fact** (e.g. the wrong fee figure) with method "analyst"; rules re-run. This is
   different from overriding the vote.
2. Optional: run once with a real ANTHROPIC_API_KEY to replace the stub with cached live answers.
3. Write the half-page note and README updates; rehearse the demo: override Starbucks Prop 4,
   decide a flagged shareholder proposal, show why Alphabet Prop 7 is flagged, show the Starbucks
   say-on-pay CEO-change flag and flip the `on_ceo_change` setting.

Conventions: keep zero dependencies; keep the LLM out of numbers and votes; every new fact carries
page + quote; add a test for every new behaviour across all three filings.
