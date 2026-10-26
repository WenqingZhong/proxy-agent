// Proxy Agent front end: plain JS, no build step.
// Routes: #/filing/<id>   #/filing/<id>/item/<number>   #/policy

const state = {
  policy: null,
  filings: [],
  cache: {},            // id -> full filing
  decisions: {},        // id -> { items: {number: {state, final_vote, reason, ...}}, decided, total, undecided }
  page: null,           // page drawer: { filingId, index, find, kind, label, pageCount }
  editing: null,        // item number whose vote form is open
  collapsed: {},
  reports: {},          // id -> what the last re-analysis changed (from the server)
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : undefined);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${path}`);
  return data;
}

const ruleById = (id) => state.policy.rules.find((r) => r.id === id);
const ruleLabel = (id) => (id === "FLAG" ? "Flag for review" : (ruleById(id)?.section ?? id));
const parseRoute = () => {
  const m = location.hash.match(/^#\/filing\/([^/]+)(?:\/item\/([^/]+))?$/);
  return m ? { filingId: decodeURIComponent(m[1]), item: m[2] ? decodeURIComponent(m[2]) : null } : null;
};
const currentFiling = () => { const r = parseRoute(); return r ? state.cache[r.filingId] : null; };
const itemHref = (f, n) => `#/filing/${encodeURIComponent(f.id)}/item/${encodeURIComponent(n)}`;
const filingHref = (f) => `#/filing/${encodeURIComponent(f.id)}`;
const typing = (e) => e.target.matches("input, textarea, select, [contenteditable]");

const LLM_MODE = {
  live: { label: "AI: live", title: "Classification and page-finding cross-checked with Claude." },
  stub: { label: "AI: stub", title: "No ANTHROPIC_API_KEY set. AI answers come from a hand-written stub file, so the workflow can be shown offline." },
  off: { label: "AI: off", title: "No API key and no stub: recommendations rely on code and keywords only." },
};

const FACT_LABEL = { attendance: "Director attendance", fees: "Auditor fees", pay: "CEO pay & TSR" };
const CLS_LABEL = {
  agreed: ["ok", "Keywords + LLM agree"],
  keywords_only: ["muted", "Keywords only"],
  disagree: ["warn", "Keywords and LLM disagree"],
  unverified: ["warn", "LLM quote not found in filing"],
};
const STATE_LABEL = {
  accepted: "Accepted policy",
  overridden: "Changed from policy",
  decided: "Your decision",
  stale: "Re-confirm",
  undecided: "Undecided",
};
// Why a decision needs another look after a re-analysis (decisions.py has the full list).
const CHANGE_LABEL = { conflicts: "Re-decide", now_agrees: "Confirm", item_changed: "Re-decide" };
const VOTES = ["FOR", "AGAINST", "ABSTAIN", "ABSENT"];
const ALWAYS_EXPLAIN = ["ABSTAIN", "ABSENT"];
const VOTE_LABEL = { FOR: "FOR", AGAINST: "AGAINST", ABSTAIN: "ABSTAIN", ABSENT: "ABSENT (not voting)" };

// ------------------------------------------------------------------ small pieces

function voteBadge(vote, big = false) {
  const text = vote === "FLAG" ? "REVIEW" : vote;
  return `<span class="vote ${vote} ${big ? "big" : ""}">${text}</span>`;
}

function recBadge(rec) {
  if (rec === "FOR" || rec === "AGAINST") return `<span class="badge ${rec}">${rec}</span>`;
  if (rec === "NONE") return `<span class="badge NONE" title="The board makes no recommendation on this item">NO REC.</span>`;
  return `<span class="badge UNKNOWN" title="Recommendation not found in the filing">—</span>`;
}

function ruleChip(id) {
  const r = ruleById(id);
  return `<span class="rule ${id}" title="${esc(r ? r.text : "")}">${esc(ruleLabel(id))}</span>`;
}

function chip(kind, text) {
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}

function differsFromBoard(it) {
  const v = it.recommendation.vote;
  return (v === "FOR" || v === "AGAINST") && (it.board_rec === "FOR" || it.board_rec === "AGAINST") && v !== it.board_rec;
}

function shortTitle(it) {
  if (it.kind === "director") return it.title.replace(/^Elect\s+/, "");
  if (it.policy_rule === "EXEC_PAY") return "Say-on-pay";
  if (it.policy_rule === "AUDITOR") return "Ratify auditor";
  return it.title.replace(/^(Stockholder|Shareholder) Proposal\s+(Requesting|Regarding|on|for|to)?\s*(that\s+|an?\s+)?/i, "")
    .replace(/^\w/, (c) => c.toUpperCase());
}

const decisionOf = (f, n) => state.decisions[f.id]?.items[n] || { state: "undecided", final_vote: null };

function yourVote(f, it) {
  const d = decisionOf(f, it.number);
  if (d.state === "stale") return `<span class="yv stale" title="${esc(d.note)}">${esc(CHANGE_LABEL[d.change] || "Re-confirm")}</span>`;
  if (!d.final_vote) return `<span class="yv none">—</span>`;
  return `${voteBadge(d.final_vote)} <span class="yv ${d.state}">${esc(STATE_LABEL[d.state])}</span>
    ${d.basis_changed ? `<span class="chip warn" title="${esc(d.changes.join("\n"))}">Reasoning changed</span>` : ""}`;
}

function dotFor(f, it) {
  const d = decisionOf(f, it.number);
  const v = d.final_vote || it.recommendation.vote;
  const title = d.state === "stale" ? CHANGE_LABEL[d.change] || "Re-confirm" : d.basis_changed ? "Reasoning changed" : STATE_LABEL[d.state];
  return `<span class="dot ${v} ${d.final_vote ? "done" : ""} ${d.state} ${d.basis_changed ? "basis" : ""}" title="${esc(title)}"></span>`;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 2600);
}

// ------------------------------------------------------------------ rail

function groupItems(items) {
  const groups = [];
  for (const it of items) {
    const isSub = it.proposal !== it.number;
    const last = groups[groups.length - 1];
    if (isSub && last && last.base === it.proposal) last.items.push(it);
    else groups.push({ base: it.proposal, isGroup: isSub, items: [it] });
  }
  return groups;
}

function railItems(f, activeItem) {
  return groupItems(f.items).map((g) => {
    const link = (it, sub) => `
      <a class="rail-item ${sub ? "sub" : ""} ${activeItem === it.number ? "active" : ""}" href="${itemHref(f, it.number)}">
        <span class="rail-num">${esc(it.number)}</span>
        <span class="rail-title">${esc(shortTitle(it))}</span>
        ${dotFor(f, it)}
      </a>`;
    if (!g.isGroup) return link(g.items[0], false);
    const key = `rail:${f.id}:${g.base}`;
    const holdsActive = g.items.some((it) => it.number === activeItem);
    const collapsed = state.collapsed[key] ?? !holdsActive;
    const done = g.items.filter((it) => decisionOf(f, it.number).final_vote).length;
    return `
      <button class="rail-group" data-toggle="${esc(key)}" data-default="${collapsed}">
        <span class="caret">${collapsed ? "▸" : "▾"}</span>
        <span class="rail-title">Election of directors</span>
        <span class="rail-count">${done}/${g.items.length}</span>
      </button>
      ${collapsed ? "" : g.items.map((it) => link(it, true)).join("")}`;
  }).join("");
}

function renderRail() {
  const route = parseRoute();
  $("#policy-count").textContent = `${state.policy.rules.length} rules`;
  $("#nav-policy").classList.toggle("active", location.hash === "#/policy");
  $("#filing-count").textContent = state.filings.length;
  $("#filing-list").innerHTML = state.filings.map((s) => {
    const selected = route && route.filingId === s.id;
    const f = state.cache[s.id];
    return `
    <li>
      <a class="filing-link ${selected && !route.item ? "active" : ""} ${selected ? "selected" : ""}" href="${filingHref(s)}">
        <span class="filing-name">${esc(s.company)}</span>
        <span class="filing-sub">
          <span>${esc(s.meeting_date || "Date unknown")}</span>
          <span>${s.review_count ? `<span class="flags">${s.review_count} to revisit</span> · ` : ""}${s.export_outdated ? `<span class="flags" title="Votes changed since the last export">export outdated</span> · ` : ""}${s.decided_count}/${s.item_count} decided</span>
        </span>
      </a>
      ${selected && f ? `<div class="rail-items">${railItems(f, route.item)}</div>` : ""}
    </li>`;
  }).join("");
  const active = $(".rail-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ filing overview

function itemRow(f, it, sub = false) {
  const r = it.recommendation;
  const diff = differsFromBoard(it) ? `<span class="diff" title="Policy differs from the board's recommendation">≠ board</span>` : "";
  return `
    <tr class="item" data-href="${itemHref(f, it.number)}" tabindex="0">
      <td class="num">${esc(it.number)}</td>
      <td class="title">
        <div class="item-title">${esc(it.title)}</div>
        ${sub ? "" : `<div class="item-sub">${esc(it.proponent === "shareholder" ? "Shareholder proposal" : "Management proposal")}</div>`}
      </td>
      <td>${recBadge(it.board_rec)}</td>
      <td class="recc">
        <div class="rec-line">${voteBadge(r.vote)} ${diff}</div>
        <div class="rec-why">${esc(r.headline)}</div>
      </td>
      <td class="rulec">${ruleChip(it.policy_rule)}</td>
      <td class="yourc">${yourVote(f, it)}</td>
    </tr>`;
}

const reviewCount = (ds) => Object.values(ds.review || {}).reduce((n, v) => n + v.length, 0);

function itemLinks(f, numbers) {
  return numbers.map((n) => {
    const it = f.items.find((x) => x.number === n);
    return `<a href="${itemHref(f, n)}" title="${esc(it?.title || "")}">${esc(n)}${it ? ` ${esc(shortTitle(it))}` : ""}</a>`;
  }).join(", ");
}

// What changed since she decided (from the decisions), plus what the last re-analysis moved.
const when = (iso) => new Date(iso).toLocaleString();
const exportUrl = (f, x, fmt) => `/api/filings/${encodeURIComponent(f.id)}/exports/${encodeURIComponent(x.id)}.${fmt}`;

// Her last export vs what she'd export now, and every past export (saved with its time).
function exportCard(f, ds) {
  const ex = ds.export;
  if (!ex?.last) return "";
  const last = ex.last;
  const history = `
    <details class="how export-history">
      <summary>Export history (${ex.history.length})</summary>
      <table class="mini">
        ${ex.history.map((x) => `<tr>
          <td>${esc(when(x.exported_at))}</td>
          <td>${esc(x.format.toUpperCase())}</td>
          <td>${x.decided}/${x.total} decided${x.complete ? "" : ` <span class="muted">(${x.undecided} blank)</span>`}</td>
          <td><a href="${exportUrl(f, x, "csv")}">CSV</a> · <a href="${exportUrl(f, x, "json")}">JSON</a></td>
        </tr>`).join("")}
      </table>
    </details>`;
  if (!ex.outdated) {
    return `<div class="export-card ok"><b>Last export ${esc(when(last.exported_at))} (${esc(last.format.toUpperCase())}) matches your current votes.</b>${history}</div>`;
  }
  return `
    <div class="export-card">
      <div class="review-head"><b>Your last export (${esc(when(last.exported_at))}, ${esc(last.format.toUpperCase())}) is out of date: ${ex.changes.length} item${ex.changes.length === 1 ? "" : "s"} changed since.</b>
        <span><button class="btn small" data-export="${esc(last.format)}">Export again</button></span></div>
      <ul class="review-list">${ex.changes.map((c) => {
        const it = f.items.find((x) => x.number === c.item);
        const name = it ? `<a href="${itemHref(f, c.item)}">${esc(c.item)} ${esc(shortTitle(it))}</a>` : `${esc(c.item)} ${esc(c.title)}`;
        return `<li>${name}: ${c.changes.map(esc).join("; ")}</li>`;
      }).join("")}</ul>
      ${history}
    </div>`;
}

function reviewBanner(f, ds) {
  const r = ds.review || {};
  const rep = state.reports[f.id];
  const rows = [
    [r.conflicts, "Recommendation changed and no longer matches your vote. Decide again"],
    [r.item_changed, "A different item now has this number. Decide again"],
    [r.now_agrees, "Recommendation changed and now matches your vote. Confirm to keep it"],
    [r.basis_changed, "Same recommendation, different reasoning. Your vote still counts; worth a look"],
  ].filter(([ns]) => ns && ns.length);
  const removed = r.removed || [];
  const undecidedMoved = (rep?.recommendations_changed || []).filter((m) => !(ds.items[m.number]?.final_vote_then || ds.items[m.number]?.final_vote));
  if (!rows.length && !removed.length && !rep) return "";
  const causes = ds.causes?.length ? ds.causes : rep?.causes || [];
  const ok = !rows.length && !removed.length;
  return `
    <div class="review-card ${ok ? "ok" : ""}">
      <div class="review-head">
        <b>${ok ? "Re-analysed: none of your decisions are affected" : `${reviewCount(ds)} decision${reviewCount(ds) === 1 ? "" : "s"} to revisit`}</b>
        ${rep ? `<button class="btn ghost small" data-dismiss-report>Dismiss</button>` : ""}
      </div>
      ${causes.length ? `<div class="meta-line">What changed: ${causes.map(esc).join("; ")}.</div>` : ""}
      ${rows.length ? `<ul class="review-list">${rows.map(([ns, label]) => `<li><span>${esc(label)}:</span> ${itemLinks(f, ns)}</li>`).join("")}</ul>` : ""}
      ${removed.length ? `<div class="meta-line">No longer on the ballot (kept, not exported): ${removed.map((x) => `${esc(x.number)} ${esc(x.title)} (you voted ${esc(x.vote)})`).join("; ")}</div>` : ""}
      ${undecidedMoved.length ? `<div class="meta-line">Recommendations that changed on items you hadn't decided: ${undecidedMoved.map((m) => `<a href="${itemHref(f, m.number)}">${esc(m.number)}</a> ${esc(m.before)} → ${esc(m.after)}`).join(", ")}</div>` : ""}
      ${rep && !causes.length && !rep.recommendations_changed.length ? `<div class="meta-line">Nothing in the filing, policy settings or AI changed, so every recommendation is the same.</div>` : ""}
    </div>`;
}

function renderFiling(f) {
  const votes = { FOR: 0, AGAINST: 0, FLAG: 0 };
  for (const it of f.items) votes[it.recommendation.vote]++;
  const diffs = f.items.filter(differsFromBoard).length;
  const mode = LLM_MODE[f.llm_mode] || LLM_MODE.off;
  const ds = state.decisions[f.id];
  const pct = Math.round((100 * ds.decided) / ds.total);
  const nextOpen = f.items.find((it) => !decisionOf(f, it.number).final_vote);

  const rows = groupItems(f.items).map((g) => {
    if (!g.isGroup) return itemRow(f, g.items[0]);
    const key = `${f.id}:${g.base}`;
    const collapsed = !!state.collapsed[key];
    const gv = { FOR: 0, AGAINST: 0, FLAG: 0 };
    g.items.forEach((it) => gv[it.recommendation.vote]++);
    const summary = Object.entries(gv).filter(([, n]) => n).map(([v, n]) => `${n} ${v === "FLAG" ? "review" : v}`).join(" · ");
    const passing = g.items.filter((it) => it.recommendation.vote === "FOR" && !decisionOf(f, it.number).final_vote);
    return `
      <tr class="group">
        <td colspan="6">
          <div class="group-row">
            <span>
              <button class="toggle" data-toggle="${esc(key)}" aria-label="Toggle">${collapsed ? "▸" : "▾"}</button>
              Proposal ${esc(g.base)} · Election of directors
              <span class="group-count">${g.items.length} nominees · ${summary}</span>
            </span>
            ${passing.length ? `<button class="btn small" data-bulk-accept="${esc(passing.map((it) => it.number).join(","))}"
                 title="Accept the policy's FOR for every nominee who passes the attendance rule">Accept ${passing.length} passing nominee${passing.length > 1 ? "s" : ""}</button>` : ""}
          </div>
        </td>
      </tr>
      ${collapsed ? "" : g.items.map((it) => itemRow(f, it, true)).join("")}`;
  }).join("");

  const main = $("#main");
  main.classList.remove("full");
  main.innerHTML = `
    <div class="page-head">
      <div>
        <div class="kicker">DEF 14A · Annual meeting <span class="mode ${f.llm_mode}" title="${esc(mode.title)}">${esc(mode.label)}</span></div>
        <h1>${esc(f.company)}</h1>
        <div class="meta">
          <span>Meeting ${esc(f.meeting_date || "date not found")}</span>
          <span>${f.page_count} pages</span>
          <code>${esc(f.source_file)}</code>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-num for">${votes.FOR}</div><div class="stat-label">For</div></div>
        <div class="stat"><div class="stat-num against">${votes.AGAINST}</div><div class="stat-label">Against</div></div>
        <div class="stat"><div class="stat-num flag">${votes.FLAG}</div><div class="stat-label">For you to review</div></div>
        <div class="stat"><div class="stat-num">${diffs}</div><div class="stat-label">Differ from board</div></div>
      </div>
    </div>
    ${f.warnings.length ? `<div class="warn">${f.warnings.map(esc).join("<br>")}</div>` : ""}
    <div class="progress-card">
      <div class="progress-text">
        <b>${ds.decided} of ${ds.total} decided</b>
        ${ds.overridden ? `<span class="muted"> · ${ds.overridden} changed from policy</span>` : ""}
        ${ds.undecided.length ? `<span class="muted"> · ${ds.undecided.length} still open</span>` : `<span class="ok-text"> · Ballot complete</span>`}
      </div>
      <div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
      <div class="progress-actions">
        ${nextOpen ? `<a class="btn primary" href="${itemHref(f, nextOpen.number)}">${ds.decided ? "Continue" : "Start review"} → item ${esc(nextOpen.number)}</a>` : ""}
        <button class="btn" data-export="csv">Export CSV</button>
        <button class="btn" data-export="json">Export JSON</button>
        <button class="btn" data-reanalyse="${esc(f.id)}" title="Read the filing again and re-run every item with the current policy settings and AI. Your decisions are kept and checked against the new recommendations.">Re-analyse</button>
      </div>
      <div class="meta-line run-line">Analysed ${esc(new Date(f.run.analyzed_at).toLocaleString())} · ${esc(f.run.llm_model || LLM_MODE[f.llm_mode]?.label || f.llm_mode)}</div>
    </div>
    ${reviewBanner(f, ds)}
    ${exportCard(f, ds)}
    <div class="card">
      <table>
        <thead>
          <tr><th>#</th><th>Ballot item</th><th>Board</th><th>Recommendation</th><th>Policy rule</th><th>Your vote</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="hint">Open an item to see what it asks, why the policy recommends what it does, and to accept or change the vote.</p>`;
}

// ------------------------------------------------------------------ item view

// Where the recommendation comes from in the filing, as links that open the exact passage.
function sources(f, it) {
  const out = [];
  for (const name of it.recommendation.facts) {
    for (const ev of f.facts[name]?.evidence || []) out.push({ ...ev, label: FACT_LABEL[name] });
  }
  if (it.board_rec_evidence) out.push({ ...it.board_rec_evidence, find: it.board_rec_evidence.quote, label: "Board's recommendation" });
  return out;
}

function sourceLink(ev) {
  if (!ev || ev.page_index == null) return "";
  return `<button class="source" data-open-page="${ev.page_index}" data-find="${esc(ev.find || ev.quote || "")}"
            data-kind="${esc(ev.kind || "text")}" data-label="${esc(ev.label || "")}">
            <span>${esc(ev.label || "Filing")}</span><span class="source-page">p. ${esc(ev.page)} →</span></button>`;
}

function settingLine(id) {
  const s = state.policy.settings.find((x) => x.id === id);
  if (!s) return "";
  const val = s.options[String(s.value)] ?? String(s.value);
  return `<li><b>${esc(s.label)}:</b> ${esc(val)}<div class="why">${esc(s.why)}</div></li>`;
}

function voteForm(it, d) {
  const policy = it.recommendation.vote;
  const pre = d.final_vote || d.final_vote_then;
  const cancellable = policy !== "FLAG" || d.final_vote || d.state === "stale";
  return `
    <form class="vote-form" data-vote-form="${esc(it.number)}">
      <div class="vote-label">${policy === "FLAG" ? "The policy doesn't decide this one. Your vote:" : "Your vote:"}</div>
      <div class="seg" role="radiogroup">
        ${VOTES.map((v) => `
          <label class="seg-opt ${v}">
            <input type="radio" name="vote" value="${v}" ${pre === v ? "checked" : ""}>
            <span>${VOTE_LABEL[v]}${v === policy ? ` <small>(policy)</small>` : ""}</span>
          </label>`).join("")}
      </div>
      <label class="reason-label" for="reason">Reason <span class="muted" data-reason-hint>${reasonHint(it, pre)}</span></label>
      <textarea id="reason" name="reason" rows="3" placeholder="Why this vote? It's saved with the vote and goes into the export.">${esc(d.reason || "")}</textarea>
      <div class="form-err" data-form-err hidden></div>
      <div class="form-actions">
        <button class="btn primary" type="submit">Save vote</button>
        ${cancellable ? `<button class="btn" type="button" data-cancel-edit>Cancel</button>` : ""}
      </div>
    </form>`;
}

const needsReason = (it, vote) =>
  it.recommendation.vote === "FLAG" || ALWAYS_EXPLAIN.includes(vote) || (vote && vote !== it.recommendation.vote);
const reasonHint = (it, vote) => (needsReason(it, vote) ? "(required)" : vote ? "(optional)" : "(required unless you keep the policy vote)");

// Before/after for a decision whose recommendation or reasoning changed since she made it.
function changesBox(d) {
  const b = d.basis_then;
  const causes = d.causes?.length ? `<div class="meta-line">Because: ${d.causes.map(esc).join("; ")}.</div>` : "";
  return `
    <ul class="changes">${(d.changes || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
    ${causes}
    ${b ? `<details class="how"><summary>What you saw when you decided</summary>
      <div class="then">${voteBadge(b.vote)} <span class="rec-head">${esc(b.headline)}</span></div>
      ${b.steps?.length ? `<ol class="steps">${b.steps.map((x) => `<li>${esc(x?.text ?? x)}</li>`).join("")}</ol>` : ""}
    </details>` : ""}`;
}

function decisionBox(f, it) {
  const d = decisionOf(f, it.number);
  const policy = it.recommendation.vote;
  const editing = state.editing === it.number || (!d.final_vote && policy === "FLAG" && d.change !== "now_agrees");
  let body;
  if (d.state === "stale" && state.editing !== it.number) {
    const then = `You voted <b>${esc(d.final_vote_then || "")}</b>${d.reason ? `: “${esc(d.reason)}”` : ""}.`;
    let actions;
    if (d.change === "now_agrees") {
      actions = `<div class="form-actions">
        <button class="btn primary" data-confirm="${esc(it.number)}">Keep ${esc(d.final_vote_then)} <kbd>A</kbd></button>
        <button class="btn" data-edit="${esc(it.number)}">Choose another vote <kbd>O</kbd></button></div>`;
    } else if (policy === "FLAG") {
      actions = voteForm(it, d);
    } else {
      actions = `<div class="form-actions">
        <button class="btn primary" data-accept="${esc(it.number)}">Accept ${esc(policy)} <kbd>A</kbd></button>
        <button class="btn" data-edit="${esc(it.number)}">Keep or choose another vote <kbd>O</kbd></button></div>`;
    }
    body = `<div class="warn">${esc(d.note)} ${then}</div>${changesBox(d)}${actions}`;
  } else if (editing) {
    body = voteForm(it, d);
  } else if (d.final_vote) {
    body = `
      <div class="decided">
        <div class="decided-top">${voteBadge(d.final_vote, true)} <span class="yv ${d.state}">${esc(STATE_LABEL[d.state])}</span>
          ${d.state === "overridden" ? `<span class="muted">Policy said ${esc(policy)}</span>` : ""}</div>
        ${d.reason ? `<div class="decided-reason">“${esc(d.reason)}”</div>` : ""}
        <div class="meta-line">Saved ${esc(new Date(d.decided_at).toLocaleString())}${d.reconfirmed_after?.length ? ` · re-confirmed after: ${esc(d.reconfirmed_after.join("; "))}` : ""}</div>
      </div>
      ${d.basis_changed ? `<div class="info"><b>The reasoning behind this recommendation changed since you decided.</b> The vote is the same, so your decision still counts.
        ${changesBox(d)}
        <div class="form-actions"><button class="btn small" data-confirm="${esc(it.number)}">Mark as reviewed</button></div></div>` : ""}
      <div class="form-actions">
        <button class="btn" data-edit="${esc(it.number)}">Change</button>
        <button class="btn ghost" data-clear="${esc(it.number)}">Undo decision</button>
      </div>`;
  } else {
    body = `
      <div class="form-actions">
        <button class="btn primary" data-accept="${esc(it.number)}">Accept ${esc(policy)} <kbd>A</kbd></button>
        <button class="btn" data-edit="${esc(it.number)}">Override… <kbd>O</kbd></button>
      </div>`;
  }
  return `<section class="block decision ${d.state}" id="decision"><h2>Your decision</h2>${body}</section>`;
}

function votingOn(it) {
  const s = it.summary;
  const lead = it.kind === "director"
    ? `<p class="lead">Elect <b>${esc(it.title.replace(/^Elect\s+/, ""))}</b> to the board.</p>` : "";
  if (!s) return `${lead}<p class="muted">Couldn't find the proposal's description in the filing. ${sourceLink({ ...it.section, find: it.section.quote, label: "Open the item's section" })}</p>`;
  return `
    ${lead}
    <blockquote class="filing-quote">${esc(s.quote)}</blockquote>
    <div class="sources">${sourceLink({ ...s, find: s.quote, label: it.summary_kind === "resolution" ? "The resolution in the filing" : "The proposal in the filing" })}</div>`;
}

function renderItem(f, it) {
  const r = it.recommendation;
  const c = it.classification;
  const cl = CLS_LABEL[c.status] || ["muted", c.status];
  const llm = c.llm;
  const rule = ruleById(r.rule);
  const idx = f.items.indexOf(it);
  const prev = f.items[idx - 1], next = f.items[idx + 1];
  const src = sources(f, it);

  const main = $("#main");
  main.classList.add("full");
  main.innerHTML = `
    <div class="iv">
      <header class="iv-head">
        <a class="btn small ghost" href="${filingHref(f)}" title="Back to overview (Esc)">← Overview</a>
        <div class="iv-titles">
          <div class="kicker">${esc(f.company)} · Item ${esc(it.number)} · ${idx + 1} of ${f.items.length} · ${esc(it.proponent === "shareholder" ? "Shareholder proposal" : "Management proposal")}</div>
          <div class="iv-title">${esc(it.title)}</div>
        </div>
        <div class="iv-nav">
          <a class="icon-btn ${prev ? "" : "disabled"}" ${prev ? `href="${itemHref(f, prev.number)}"` : ""} title="Previous item (←)" aria-label="Previous item">‹</a>
          <a class="icon-btn ${next ? "" : "disabled"}" ${next ? `href="${itemHref(f, next.number)}"` : ""} title="Next item (→)" aria-label="Next item">›</a>
        </div>
      </header>
      <div class="iv-scroll">
        <div class="iv-col">
          <section class="block">
            <h2>What you're voting on</h2>
            ${votingOn(it)}
          </section>

          <section class="block rec ${r.vote}">
            <h2>Recommendation</h2>
            <div class="rec-grid">
              <span class="rec-who">Policy</span>
              <span>${voteBadge(r.vote, true)} <span class="rec-head">${esc(r.headline)}</span></span>
              <span class="rec-who">Board</span>
              <span>${recBadge(it.board_rec)} ${differsFromBoard(it) ? chip("warn", "Differs from the policy") : ""}</span>
            </div>
            ${r.steps.length ? `<ol class="steps">${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` : ""}
            ${src.length ? `<div class="sources"><span class="sources-label">See it in the filing:</span>${src.map(sourceLink).join("")}</div>` : ""}
          </section>

          ${decisionBox(f, it)}

          <section class="block">
            <h2>Policy rule</h2>
            <div class="rule-line">${ruleChip(r.rule)}</div>
            <p class="rule-text">${r.rule === "FLAG" ? "No rule in the policy covers this item, or a fact the rule needs couldn't be confirmed. Your call." : `“${esc(rule?.text || "")}”`}</p>
            ${r.settings.length ? `<div class="sub-label">Judgement calls applied (set in the policy)</div><ul class="settings">${r.settings.map(settingLine).join("")}</ul>` : ""}
            ${c.status !== "agreed" ? `<div class="warn">${esc(c.note)}</div>` : ""}
            <details class="how">
              <summary>How the rule was matched ${chip(cl[0], cl[1])}</summary>
              <div class="meta-line">Keywords say: ${esc(c.keyword.category.replace(/_/g, " "))}${c.keyword.direction !== "n/a" ? ` (${esc(c.keyword.direction)})` : ""}.
                AI says: ${llm ? esc(llm.category.replace(/_/g, " ")) + (llm.direction && llm.direction !== "n/a" ? ` (${esc(llm.direction)})` : "") + ` <span class="muted">[${esc(llm.source)}]</span>` : `<span class="muted">${esc(c.llm_error || "not used")}</span>`}.</div>
              ${llm ? `<div class="meta-line">AI's reason: ${esc(llm.reason)}</div>
                <div class="sources">${llm.quote_verified ? sourceLink({ page: llm.page, page_index: llm.page_index, quote: llm.quote, label: "Passage the AI relied on" }) : chip("warn", "AI's quote not found in the filing")}</div>` : ""}
            </details>
          </section>
          <p class="hint">Keys: ← → items · A accept · O override · Esc overview</p>
        </div>
      </div>
    </div>`;
}

function refreshDecision(f, it) {
  $("#decision").outerHTML = decisionBox(f, it);
  const ta = $("#decision textarea");
  if (ta && state.editing === it.number) ta.focus();
}

async function saveDecision(f, numbers, action, vote, reason) {
  const ds = await api(`/api/filings/${encodeURIComponent(f.id)}/decisions`, { items: numbers, action, vote, reason });
  state.decisions[f.id] = ds;
  const s = state.filings.find((x) => x.id === f.id);
  if (s) { s.decided_count = ds.decided; s.review_count = reviewCount(ds); s.export_outdated = !!ds.export?.outdated; }
  state.editing = null;
  return ds;
}

// After a decision, move on to the next open item (after this one, then from the top).
function goToNextOpen(f, fromNumber) {
  const ds = state.decisions[f.id];
  const order = f.items.map((it) => it.number);
  const from = order.indexOf(fromNumber);
  const open = new Set(ds.undecided);
  const next = [...order.slice(from + 1), ...order.slice(0, from)].find((n) => open.has(n));
  if (next) location.hash = itemHref(f, next);
  else { toast("Every item is decided. Ready to export."); location.hash = filingHref(f); }
}

// ------------------------------------------------------------------ page viewer (drawer)

async function openPage(index, find, kind = "text", label = "") {
  const f = currentFiling();
  if (!f) return;
  const page = await api(`/api/filings/${encodeURIComponent(f.id)}/pages/${index}`);
  state.page = { filingId: f.id, index: page.index, find, kind, label, pageCount: page.page_count };
  $("#overlay").hidden = false;
  const target = $("#overlay .overlay-box");

  const norm = (s) => s.replace(/(\w)- (\w)/g, "$1$2").replace(/[’]/g, "'").replace(/[“”]/g, '"')
    .replace(/\s+/g, " ").trim().toLowerCase();
  const q = find ? norm(find) : "";
  const lineHit = (t) => {
    if (!q || kind === "table") return false;
    const n = norm(t);
    return n === q || n.includes(q) || (q.length > 30 && n.length > 30 && q.includes(n));
  };
  // EDGAR often wraps prose in one-cell tables, so a text quote can land in a table too.
  const tableHit = (rows) => q && rows.some((r) => r.some((c) => (kind === "table" ? norm(c).includes(q) : lineHit(c))));
  const blocks = page.blocks || page.lines.map((text) => ({ t: "p", text }));
  const where = page.label.startsWith("#")
    ? `Unnumbered page (${page.index + 1} of ${page.page_count} in file)`
    : `Page ${page.label} of the filing`;
  target.innerHTML = `
    <div class="pv-head">
      <div><div class="kicker">${esc(label || "Filing")}</div><div class="pv-title">${esc(where)}</div></div>
      <div class="pv-actions">
        <button class="icon-btn" data-page-step="-1" title="Previous page" aria-label="Previous page" ${page.index === 0 ? "disabled" : ""}>↑</button>
        <button class="icon-btn" data-page-step="1" title="Next page" aria-label="Next page" ${page.index >= page.page_count - 1 ? "disabled" : ""}>↓</button>
        <button class="icon-btn" data-close-overlay title="Close (Esc)" aria-label="Close">×</button>
      </div>
    </div>
    <div class="pv-body pagetext">${blocks.map((b) => b.t === "table"
      ? `<div class="ptable ${tableHit(b.rows) ? "hit" : ""}"><table>${b.rows.map((r) =>
          `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</table></div>`
      : `<p class="${lineHit(b.text) ? "hit" : ""}">${esc(b.text)}</p>`).join("")}</div>`;
  const body = target.querySelector(".pv-body");
  const hitEl = body.querySelector(".hit");
  // Scroll only the page viewer, not the surrounding layout.
  body.scrollTop = hitEl ? Math.max(0, hitEl.offsetTop - body.clientHeight / 4) : 0;
}

function closeOverlay() {
  $("#overlay").hidden = true;
  state.page = null;
}

// ------------------------------------------------------------------ policy

function settingSelect(s) {
  const changed = String(s.value) !== String(s.default);
  return `<li class="setting ${changed ? "changed" : ""}">
    <label><b>${esc(s.label)}</b>
      <select data-setting="${esc(s.id)}">${Object.entries(s.options).map(([k, text]) =>
        `<option value="${esc(k)}" ${k === String(s.value) ? "selected" : ""}>${esc(text)}${k === String(s.default) ? " (default)" : ""}</option>`).join("")}</select>
    </label>
    <div class="why">${esc(s.why)}</div></li>`;
}

// After a settings change: one line per filing on what moved and what she needs to revisit.
function reportList() {
  const reps = Object.values(state.reports);
  if (!reps.length) return "";
  return `<div class="review-card">
    <div class="review-head"><b>Re-analysed ${reps.length} filing${reps.length > 1 ? "s" : ""}</b>
      <button class="btn ghost small" data-dismiss-report>Dismiss</button></div>
    <ul class="review-list">${reps.map((r) => {
      const n = Object.values(r.review).reduce((a, v) => a + v.length, 0);
      const moved = r.recommendations_changed.map((m) => `${esc(m.number)} ${esc(m.before)} → ${esc(m.after)}`).join(", ");
      return `<li><a href="#/filing/${encodeURIComponent(r.filing)}"><b>${esc(r.company)}</b></a>:
        ${moved ? `recommendations changed on ${moved}` : "no recommendation changed"}${n ? `; <span class="flags">${n} of your decisions to revisit</span>` : ""}.</li>`;
    }).join("")}</ul></div>`;
}

function renderPolicy() {
  const p = state.policy;
  const main = $("#main");
  main.classList.remove("full");
  main.innerHTML = `
    <div class="page-head">
      <div>
        <div class="kicker">Rules hard-coded · version ${esc(p.version)} · settings editable</div>
        <h1>${esc(p.name)}</h1>
        <div class="meta"><span>${p.rules.length} rules. Anything no rule covers, or any rule missing a confirmed fact, goes to the analyst. It's never auto-voted.</span></div>
      </div>
    </div>
    ${reportList()}
    <div class="policy-grid">
      ${p.rules.map((r) => {
        const settings = p.settings.filter((s) => s.rule === r.id);
        return `
        <div class="policy-card">
          <div><h3>${esc(r.section)}</h3>${ruleChip(r.id)}</div>
          <div>
            <p class="policy-text">${esc(r.text)}</p>
            <div class="policy-facts">
              <span><b>Default vote:</b> ${r.default ? esc(r.default) : "None (analyst decides)"}</span>
              ${r.needs.length ? `<span><b>Needs from the filing:</b> ${r.needs.map(esc).join("; ")}</span>` : ""}
            </div>
            ${settings.length ? `<ul class="settings editable">${settings.map(settingSelect).join("")}</ul>` : ""}
          </div>
        </div>`;
      }).join("")}
    </div>
    <p class="note">Settings are the judgement calls the policy wording leaves open. Each one is shown next to the recommendations it affects.
      Changing one re-analyses every filing; your decisions are kept, and any the change affects are marked for you to revisit.
      Rules and defaults live in <code>app/policy.py</code>.</p>`;
}

// ------------------------------------------------------------------ re-analysis

// The server re-ran the pipeline: drop cached filings so the next render fetches the new ones.
function applyReanalysis(res) {
  state.filings = res.filings;
  if (res.policy) state.policy = res.policy;
  state.reports = {};
  for (const r of res.reports) {
    state.reports[r.filing] = r;
    delete state.cache[r.filing];
    delete state.decisions[r.filing];
  }
  state.editing = null;
}

async function reanalyse(body, busyEl) {
  const label = busyEl?.textContent;
  if (busyEl) {
    busyEl.disabled = true;
    if (busyEl.tagName === "BUTTON") busyEl.textContent = "Re-analysing…";
  }
  toast("Re-analysing…");
  try {
    const res = await api(body.id ? "/api/policy/settings" : "/api/reanalyze", body);
    applyReanalysis(res);
    const n = res.reports.reduce((a, r) => a + Object.values(r.review).reduce((x, v) => x + v.length, 0), 0);
    toast(n ? `Re-analysed. ${n} decision${n > 1 ? "s" : ""} to revisit.` : "Re-analysed. None of your decisions are affected.");
    await route();
  } catch (err) {
    toast(err.message);
    if (location.hash === "#/policy") renderPolicy();
  } finally {
    // Buttons outside #main (the rail footer) aren't re-rendered, so put them back.
    if (busyEl?.isConnected) {
      busyEl.disabled = false;
      if (busyEl.tagName === "BUTTON") busyEl.textContent = label;
    }
  }
}

// ------------------------------------------------------------------ routing

async function loadFiling(id) {
  if (!state.cache[id]) {
    $("#main").classList.remove("full");
    $("#main").innerHTML = `<div class="empty">Loading filing…</div>`;
    const [f, ds] = await Promise.all([
      api(`/api/filings/${encodeURIComponent(id)}`),
      api(`/api/filings/${encodeURIComponent(id)}/decisions`),
    ]);
    state.cache[id] = f;
    state.decisions[id] = ds;
  }
  return state.cache[id];
}

async function route() {
  const hash = location.hash || "";
  closeOverlay();
  if (!hash || hash === "#/") {
    if (state.filings.length) location.hash = filingHref(state.filings[0]);
    else renderPolicy();
    return;
  }
  if (hash === "#/policy") {
    renderRail();
    return renderPolicy();
  }
  const r = parseRoute();
  if (r) {
    const f = await loadFiling(r.filingId);
    renderRail();
    if (!r.item) return renderFiling(f);
    const it = f.items.find((x) => x.number === r.item);
    if (!it) { $("#main").innerHTML = `<div class="empty">No item ${esc(r.item)} in this filing.</div>`; return; }
    if (state.editing !== it.number) state.editing = null;
    return renderItem(f, it);
  }
  renderRail();
  $("#main").innerHTML = `<div class="empty">Not found.</div>`;
}

async function act(fn) {
  try { await fn(); } catch (err) { toast(err.message); }
}

document.addEventListener("click", (e) => {
  const t = e.target;
  const src = t.closest("[data-open-page]");
  if (src) {
    e.preventDefault();
    openPage(Number(src.dataset.openPage), src.dataset.find, src.dataset.kind, src.dataset.label);
    return;
  }
  const step = t.closest("[data-page-step]");
  if (step && state.page) {
    const p = state.page;
    openPage(p.index + Number(step.dataset.pageStep), p.find, p.kind, p.label);
    return;
  }
  if (t.closest("[data-close-overlay]") || t.id === "overlay") return closeOverlay();

  const tog = t.closest("[data-toggle]");
  if (tog) {
    const key = tog.dataset.toggle;
    const was = key in state.collapsed ? state.collapsed[key] : tog.dataset.default === "true";
    state.collapsed[key] = !was;
    renderRail();
    if (!key.startsWith("rail:") && !parseRoute()?.item) renderFiling(currentFiling());
    return;
  }

  const rean = t.closest("[data-reanalyse]");
  if (rean) return reanalyse(rean.dataset.reanalyse ? { filing: rean.dataset.reanalyse } : {}, rean);
  if (t.closest("[data-dismiss-report]")) {
    const r = parseRoute();
    if (r) delete state.reports[r.filingId]; else state.reports = {};
    return route();
  }

  const f = currentFiling();
  const exp = t.closest("[data-export]");
  if (exp && f) {
    const open = state.decisions[f.id].undecided.length;
    if (open && !confirm(`${open} item${open > 1 ? "s are" : " is"} still undecided. They'll have a blank final vote in the file. Export anyway?`)) return;
    return act(async () => {
      // Saved first (with its time, in the export history), then downloaded from the saved copy.
      const res = await api(`/api/filings/${encodeURIComponent(f.id)}/exports`, { format: exp.dataset.export });
      const { saved: _saved, download, ...ds } = res;
      state.decisions[f.id] = ds;
      const s = state.filings.find((x) => x.id === f.id);
      if (s) s.export_outdated = false;
      location.href = download;
      toast(`Exported ${exp.dataset.export.toUpperCase()}. Saved to the export history.`);
      renderRail(); renderFiling(f);
    });
  }
  const bulk = t.closest("[data-bulk-accept]");
  if (bulk && f) {
    const nums = bulk.dataset.bulkAccept.split(",");
    return act(async () => {
      await saveDecision(f, nums, "accept");
      toast(`Accepted FOR on ${nums.length} nominee${nums.length > 1 ? "s" : ""}.`);
      renderRail(); renderFiling(f);
    });
  }
  const it = f && parseRoute()?.item ? f.items.find((x) => x.number === parseRoute().item) : null;
  if (it && t.closest("[data-accept]")) {
    return act(async () => {
      await saveDecision(f, [it.number], "accept");
      toast(`Item ${it.number}: accepted ${it.recommendation.vote}.`);
      goToNextOpen(f, it.number);
    });
  }
  if (it && t.closest("[data-confirm]")) {
    const wasStale = decisionOf(f, it.number).state === "stale";
    return act(async () => {
      const ds = await saveDecision(f, [it.number], "confirm");
      toast(`Item ${it.number}: kept ${ds.items[it.number].final_vote}.`);
      if (wasStale) goToNextOpen(f, it.number);
      else { renderRail(); refreshDecision(f, it); }
    });
  }
  if (it && t.closest("[data-edit]")) { state.editing = it.number; return refreshDecision(f, it); }
  if (it && t.closest("[data-cancel-edit]")) { state.editing = null; return refreshDecision(f, it); }
  if (it && t.closest("[data-clear]")) {
    return act(async () => {
      await saveDecision(f, [it.number], "clear");
      renderRail(); refreshDecision(f, it);
    });
  }
  const row = t.closest("tr.item[data-href]");
  if (row && !t.closest("button, a")) location.hash = row.dataset.href;
});

document.addEventListener("change", (e) => {
  const sel = e.target.closest("[data-setting]");
  if (sel) return reanalyse({ id: sel.dataset.setting, value: sel.value }, sel);
  const form = e.target.closest("[data-vote-form]");
  const f = currentFiling();
  if (!form || !f || e.target.name !== "vote") return;
  const it = f.items.find((x) => x.number === form.dataset.voteForm);
  form.querySelector("[data-reason-hint]").textContent = reasonHint(it, e.target.value);
});

document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-vote-form]");
  if (!form) return;
  e.preventDefault();
  const f = currentFiling();
  const it = f.items.find((x) => x.number === form.dataset.voteForm);
  const vote = form.querySelector("input[name=vote]:checked")?.value;
  const reason = form.querySelector("textarea").value;
  const err = form.querySelector("[data-form-err]");
  const fail = (msg) => { err.textContent = msg; err.hidden = false; };
  if (!vote) return fail("Choose a vote.");
  if (needsReason(it, vote) && !reason.trim())
    return fail("Add a reason. It's saved with the vote and goes into the export.");
  saveDecision(f, [it.number], "vote", vote, reason)
    .then((ds) => {
      const s = ds.items[it.number];
      toast(`Item ${it.number}: ${vote} (${STATE_LABEL[s.state].toLowerCase()}).`);
      goToNextOpen(f, it.number);
    })
    .catch((x) => fail(x.message));
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("#overlay").hidden) return closeOverlay();
    if (typing(e)) return e.target.blur();
  }
  if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  const r = parseRoute();
  const f = currentFiling();
  if (e.key === "Enter" && e.target.matches("tr.item[data-href]")) { location.hash = e.target.dataset.href; return; }
  if (!r?.item || !f) return;
  const idx = f.items.findIndex((x) => x.number === r.item);
  const it = f.items[idx];
  if (e.key === "Escape") location.hash = filingHref(f);
  else if (e.key === "ArrowLeft" && idx > 0) location.hash = itemHref(f, f.items[idx - 1].number);
  else if (e.key === "ArrowRight" && idx < f.items.length - 1) location.hash = itemHref(f, f.items[idx + 1].number);
  else if ((e.key === "a" || e.key === "A") && $("[data-accept], .decision.stale [data-confirm]"))
    $("[data-accept], .decision.stale [data-confirm]").click();
  else if ((e.key === "o" || e.key === "O") && $("[data-edit]")) { e.preventDefault(); state.editing = it.number; refreshDecision(f, it); }
});
window.addEventListener("hashchange", route);

(async function init() {
  try {
    [state.policy, state.filings] = await Promise.all([api("/api/policy"), api("/api/filings")]);
    await route();
  } catch (err) {
    $("#main").innerHTML = `<div class="warn">Couldn't reach the server: ${esc(err.message)}</div>`;
  }
})();
