# Proxy Agent

A small local app for a stewardship analyst reviewing proxy ballots against a voting policy.

**This version:** browse every loaded filing, see its ballot (each director nominee
and proposal, the board's recommendation, the policy rule that covers it, and the
filing page it comes from), and read the hard-coded voting policy.

## Run

Python 3.9+ only, no packages to install.

```bash
python3 server.py          # then open http://localhost:8000
python3 server.py 8080     # different port
python3 -m unittest discover tests
```

## Add a filing

Save a DEF 14A from EDGAR as `.htm` into `data/filings/` and restart the server.

## How it works

```
data/filings/*.htm
      │
      ▼
app/pages.py     split on CSS page breaks → pages of text lines,
                 read the printed page number from each footer
      │
      ▼
app/extract.py   company, meeting date, ballot items:
                 · item headings ("PROPOSAL 1 –", "Proposal Number 1:", "ITEM 1—")
                 · section page from the summary's page reference, else first
                   heading at the top of a page
                 · board recommendation: first "recommends a vote FOR/AGAINST"
                   (or "makes no recommendation") inside that section
                 · director nominees from the proxy card (1a., 1b., …), or the
                   meeting notice if there is no card
                 · item type (director / say-on-pay / auditor / board structure / other)
      │
      ▼
app/policy.py    the hard-coded policy; maps item type → rule (unmatched → FLAG)
      │
      ▼
server.py        stdlib HTTP server, JSON API + static/ front end
```

API: `GET /api/policy`, `GET /api/filings`, `GET /api/filings/<id>`,
`GET /api/filings/<id>/pages/<index>`.

## Not built yet

Vote recommendations (facts → rule → FOR/AGAINST), accept/override with reasons, export.
