// Proxy Agent front end: plain JS, no build step.
// Routes: #/filing/<id>  and  #/policy

const state = {
  policy: null,
  filings: [],
  cache: {},            // id -> full filing
  panel: null,          // { mode: "item"|"page", filingId, itemNumber, page: {...}, quote }
  collapsed: {},
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}

const ruleById = (id) => state.policy.rules.find((r) => r.id === id);
const ruleLabel = (id) => (id === "FLAG" ? "Flag for review" : (ruleById(id)?.section ?? id));
const currentFiling = () => {
  const m = location.hash.match(/^#\/filing\/(.+)$/);
  return m ? state.cache[decodeURIComponent(m[1])] : null;
};

const LLM_MODE = {
  live: { label: "AI: live", title: "Classification and page-finding cross-checked with Claude." },
  stub: { label: "AI: stub", title: "No ANTHROPIC_API_KEY set. AI answers come from a hand-written stub file, so the workflow can be shown offline." },
  off: { label: "AI: off", title: "No API key and no stub: recommendations rely on code and keywords only." },
};

const FACT_LABEL = { attendance: "Director attendance", fees: "Auditor fees", pay: "CEO pay & TSR" };
const METHOD_LABEL = { code: "Read by code", llm: "Read by LLM, quote verified", analyst: "Entered by analyst" };
const LOCATOR_LABEL = {
  agree: ["ok", "Keywords + LLM agree on the page"],
  keywords_only: ["muted", "Keywords only (LLM not used)"],
  disagree: ["warn", "Keywords and LLM disagree on the page"],
  llm_only: ["warn", "Found via LLM after keywords missed"],
  neither: ["warn", "Not found by keywords or LLM"],
};
const CLS_LABEL = {
  agreed: ["ok", "Keywords + LLM agree"],
  keywords_only: ["muted", "Keywords only"],
  disagree: ["warn", "Keywords and LLM disagree"],
  unverified: ["warn", "LLM quote not found in filing"],
};

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

function pageBtn(ev, label = "") {
  if (!ev || ev.page_index == null) return "";
  return `<button class="src" data-open-page="${ev.page_index}" data-find="${esc(ev.find || ev.quote || "")}"
            data-kind="${esc(ev.kind || "text")}" title="${esc(label || "Open this page")}">p. ${esc(ev.page)}</button>`;
}

function chip(kind, text) {
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}

function differsFromBoard(it) {
  const v = it.recommendation.vote;
  return (v === "FOR" || v === "AGAINST") && (it.board_rec === "FOR" || it.board_rec === "AGAINST") && v !== it.board_rec;
}

// ------------------------------------------------------------------ rail

function renderRail() {
  const route = location.hash;
  $("#policy-count").textContent = `${state.policy.rules.length} rules`;
  $("#nav-policy").classList.toggle("active", route === "#/policy");
  $("#filing-count").textContent = state.filings.length;
  $("#filing-list").innerHTML = state.filings.map((f) => `
    <li>
      <a class="filing-link ${route === `#/filing/${f.id}` ? "active" : ""}" href="#/filing/${encodeURIComponent(f.id)}">
        <span class="filing-name">${esc(f.company)}</span>
        <span class="filing-sub">
          <span>${esc(f.meeting_date || "Date unknown")}</span>
          <span>${f.item_count} items${f.flag_count ? ` · <span class="flags">${f.flag_count} to review</span>` : ""}</span>
        </span>
      </a>
    </li>`).join("");
}

// ------------------------------------------------------------------ filing

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

function itemRow(f, it, sub = false) {
  const r = it.recommendation;
  const active = state.panel && state.panel.itemNumber === it.number ? "active" : "";
  const diff = differsFromBoard(it) ? `<span class="diff" title="Policy differs from the board's recommendation">≠ board</span>` : "";
  return `
    <tr class="item ${active}" data-item="${esc(it.number)}" tabindex="0">
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
      <td>${ruleChip(it.policy_rule)}</td>
    </tr>`;
}

function factCard(name, fact) {
  const ok = fact.status === "found" && fact.locator !== "disagree";
  const ev = fact.evidence && fact.evidence[0];
  const loc = LOCATOR_LABEL[fact.locator] || ["muted", ""];
  let summary = "";
  if (fact.status === "found") {
    if (name === "attendance") summary = fact.all_at_least_75 ? "All directors ≥ 75%" : `${(fact.below_75 || []).length} under 75%`;
    if (name === "fees") summary = `FY${fact.year} fee table`;
    if (name === "pay") summary = `FY${fact.fiscal_year} pay vs. performance`;
  } else summary = fact.status === "needs_review" ? "Needs review" : "Not found";
  return `
    <div class="fact ${ok ? "" : "bad"}">
      <div class="fact-top"><span class="fact-name">${esc(FACT_LABEL[name])}</span>${pageBtn(ev)}</div>
      <div class="fact-val">${esc(summary)}</div>
      <div class="fact-how">${chip(loc[0], loc[1])}</div>
    </div>`;
}

function renderFiling(f) {
  const votes = { FOR: 0, AGAINST: 0, FLAG: 0 };
  for (const it of f.items) votes[it.recommendation.vote]++;
  const diffs = f.items.filter(differsFromBoard).length;
  const mode = LLM_MODE[f.llm_mode] || LLM_MODE.off;

  const rows = groupItems(f.items).map((g) => {
    if (!g.isGroup) return itemRow(f, g.items[0]);
    const key = `${f.id}:${g.base}`;
    const collapsed = !!state.collapsed[key];
    const gv = { FOR: 0, AGAINST: 0, FLAG: 0 };
    g.items.forEach((it) => gv[it.recommendation.vote]++);
    const summary = Object.entries(gv).filter(([, n]) => n).map(([v, n]) => `${n} ${v === "FLAG" ? "review" : v}`).join(" · ");
    return `
      <tr class="group">
        <td colspan="5">
          <button class="toggle" data-toggle="${esc(key)}" aria-label="Toggle">${collapsed ? "▸" : "▾"}</button>
          Proposal ${esc(g.base)} · Election of directors
          <span class="group-count">${g.items.length} nominees · ${summary}</span>
        </td>
      </tr>
      ${collapsed ? "" : g.items.map((it) => itemRow(f, it, true)).join("")}`;
  }).join("");

  $("#main").innerHTML = `
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
    <div class="facts">${["attendance", "fees", "pay"].map((n) => factCard(n, f.facts[n])).join("")}</div>
    <div class="card">
      <table>
        <thead>
          <tr><th>#</th><th>Ballot item</th><th>Board</th><th>Recommendation</th><th>Policy rule</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="hint">Click any row to see how the recommendation was reached and the filing text behind it.</p>`;
}

// ------------------------------------------------------------------ item detail panel

function settingText(id) {
  const s = state.policy.settings.find((x) => x.id === id);
  if (!s) return "";
  const val = s.options[String(s.value)] ?? String(s.value);
  return `<li><b>${esc(s.label)}:</b> ${esc(val)}<div class="why">${esc(s.why)}</div></li>`;
}

function factEvidence(name, fact) {
  const loc = LOCATOR_LABEL[fact.locator] || ["muted", ""];
  const method = METHOD_LABEL[fact.method] || "Not found";
  const pages = fact.keyword_pages?.length || fact.llm_pages?.length
    ? `<div class="ev-meta">Keyword pages: ${esc((fact.keyword_pages || []).join(", ") || "none")} · LLM pages: ${esc((fact.llm_pages || []).join(", ") || "none")}</div>` : "";
  return `
    <div class="ev">
      <div class="ev-head"><b>${esc(FACT_LABEL[name])}</b> ${chip(fact.status === "found" ? "ok" : "warn", method)} ${chip(loc[0], loc[1])}</div>
      ${(fact.evidence || []).map((e) => `<blockquote>${esc(e.quote)} ${pageBtn(e)}</blockquote>`).join("") || `<p class="muted">${esc(fact.note || "Nothing found.")}</p>`}
      ${fact.note && fact.evidence?.length ? `<div class="ev-meta">${esc(fact.note)}</div>` : ""}
      ${pages}
    </div>`;
}

function renderItemPanel(f, it) {
  const r = it.recommendation;
  const c = it.classification;
  const cl = CLS_LABEL[c.status] || ["muted", c.status];
  const llm = c.llm;
  $("#panel-kicker").textContent = `${f.company} · Item ${it.number}`;
  $("#panel-title").textContent = it.title;
  $("#panel-back").hidden = true;
  $("#page-prev").hidden = $("#page-next").hidden = true;

  $("#panel-body").innerHTML = `
    <section class="verdict ${r.vote}">
      <div class="verdict-top">${voteBadge(r.vote, true)} <span class="verdict-rule">${ruleChip(r.rule)}</span></div>
      <div class="verdict-head">${esc(r.headline)}</div>
      ${r.steps.length ? `<ol class="steps">${r.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` : ""}
      ${r.rule !== "FLAG" ? `<div class="policy-quote">Policy: “${esc(ruleById(r.rule)?.text || "")}”</div>` : ""}
    </section>

    ${r.facts.length ? `<section><h4>Evidence from the filing</h4>${r.facts.map((n) => factEvidence(n, f.facts[n])).join("")}</section>` : ""}

    <section>
      <h4>Which rule applies</h4>
      <div class="ev">
        <div class="ev-head"><b>${esc(c.label)}</b> ${chip(cl[0], cl[1])}</div>
        <div class="ev-meta">Keywords: ${esc(c.keyword.category.replace(/_/g, " "))}${c.keyword.direction !== "n/a" ? ` (${esc(c.keyword.direction)})` : ""}
          · LLM: ${llm ? esc(llm.category.replace(/_/g, " ")) + (llm.direction && llm.direction !== "n/a" ? ` (${esc(llm.direction)})` : "") + ` <span class="muted">[${esc(llm.source)}]</span>` : `<span class="muted">${esc(c.llm_error || "not used")}</span>`}</div>
        ${llm ? `<div class="ev-meta">LLM reason: ${esc(llm.reason)}</div>
                 <blockquote>${esc(llm.quote)} ${llm.quote_verified ? pageBtn({ page: llm.page, page_index: llm.page_index, quote: llm.quote }) : chip("warn", "not found in filing")}</blockquote>` : ""}
        ${c.status !== "agreed" ? `<div class="ev-meta">${esc(c.note)}</div>` : ""}
      </div>
    </section>

    ${r.settings.length ? `<section><h4>Policy settings used</h4><ul class="settings">${r.settings.map(settingText).join("")}</ul></section>` : ""}

    <section>
      <h4>Board recommendation</h4>
      <div class="ev">
        <div class="ev-head">${recBadge(it.board_rec)} ${differsFromBoard(it) ? chip("warn", "Policy differs from board") : ""}</div>
        ${it.board_rec_evidence ? `<blockquote>${esc(it.board_rec_evidence.quote)} ${pageBtn({ ...it.board_rec_evidence, find: it.board_rec_evidence.quote })}</blockquote>` : `<p class="muted">Not found in the filing.</p>`}
        <div class="ev-meta">Item section starts ${pageBtn({ ...it.section, find: it.section.quote })}</div>
      </div>
    </section>`;
  $("#panel").hidden = false;
  document.querySelector(".app").classList.add("panel-open");
}

function openItem(number) {
  const f = currentFiling();
  if (!f) return;
  const it = f.items.find((x) => x.number === number);
  if (!it) return;
  state.panel = { mode: "item", filingId: f.id, itemNumber: number };
  renderItemPanel(f, it);
  document.querySelectorAll("tr.item").forEach((tr) => tr.classList.toggle("active", tr.dataset.item === number));
}

// ------------------------------------------------------------------ page view (inside the panel)

async function openPage(index, find, kind = "text") {
  const f = currentFiling();
  if (!f) return;
  const page = await api(`/api/filings/${encodeURIComponent(f.id)}/pages/${index}`);
  const back = state.panel && state.panel.itemNumber;
  state.panel = { mode: "page", filingId: f.id, itemNumber: back, index: page.index, find, kind, pageCount: page.page_count };
  $("#panel-kicker").textContent = f.company;
  $("#panel-title").textContent = page.label.startsWith("#")
    ? `Unnumbered page (${page.index + 1} of ${page.page_count})`
    : `Page ${page.label}  ·  ${page.index + 1} of ${page.page_count} in file`;
  $("#panel-back").hidden = !back;
  $("#page-prev").hidden = $("#page-next").hidden = false;
  $("#page-prev").disabled = page.index === 0;
  $("#page-next").disabled = page.index >= page.page_count - 1;

  const norm = (s) => s.replace(/(\w)- (\w)/g, "$1$2").replace(/[’]/g, "'").replace(/[“”]/g, '"')
    .replace(/\s+/g, " ").trim().toLowerCase();
  const q = find ? norm(find) : "";
  const lineHit = (t) => {
    if (!q || kind === "table") return false;
    const n = norm(t);
    return n === q || n.includes(q) || (q.length > 30 && n.length > 30 && q.includes(n));
  };
  const tableHit = (rows) => q && kind === "table" && rows.some((r) => r.some((c) => norm(c).includes(q)));
  const blocks = page.blocks || page.lines.map((text) => ({ t: "p", text }));
  $("#panel-body").innerHTML = `<div class="pagetext">${blocks.map((b) => b.t === "table"
    ? `<div class="ptable ${tableHit(b.rows) ? "hit" : ""}"><table>${b.rows.map((r) =>
        `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</table></div>`
    : `<p class="${lineHit(b.text) ? "hit" : ""}">${esc(b.text)}</p>`).join("")}</div>`;
  $("#panel").hidden = false;
  document.querySelector(".app").classList.add("panel-open");
  const hitEl = $("#panel-body .hit");
  if (hitEl) hitEl.scrollIntoView({ block: "center" });
  else $("#panel-body").scrollTop = 0;
}

function closePanel() {
  $("#panel").hidden = true;
  document.querySelector(".app").classList.remove("panel-open");
  state.panel = null;
  document.querySelectorAll("tr.item.active").forEach((tr) => tr.classList.remove("active"));
}

// ------------------------------------------------------------------ policy

function renderPolicy() {
  const p = state.policy;
  const optText = (s) => s.options[String(s.value)] ?? String(s.value);
  $("#main").innerHTML = `
    <div class="page-head">
      <div>
        <div class="kicker">Hard-coded · version ${esc(p.version)}</div>
        <h1>${esc(p.name)}</h1>
        <div class="meta"><span>${p.rules.length} rules. Anything no rule covers, or any rule missing a confirmed fact, goes to the analyst. It's never auto-voted.</span></div>
      </div>
    </div>
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
            ${settings.length ? `<ul class="settings">${settings.map((s) =>
              `<li><b>${esc(s.label)}:</b> ${esc(optText(s))}<div class="why">${esc(s.why)}</div></li>`).join("")}</ul>` : ""}
          </div>
        </div>`;
      }).join("")}
    </div>
    <p class="note">Rules and settings live in <code>app/policy.py</code>. Settings are the judgement calls the policy wording leaves open. Each one is shown next to the recommendations it affects.</p>`;
}

// ------------------------------------------------------------------ routing

async function route() {
  const hash = location.hash || "";
  if (!hash || hash === "#/") {
    if (state.filings.length) location.hash = `#/filing/${state.filings[0].id}`;
    else renderPolicy();
    return;
  }
  renderRail();
  if (hash === "#/policy") {
    closePanel();
    return renderPolicy();
  }
  const m = hash.match(/^#\/filing\/(.+)$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (state.panel && state.panel.filingId !== id) closePanel();
    if (!state.cache[id]) {
      $("#main").innerHTML = `<div class="empty">Loading filing…</div>`;
      state.cache[id] = await api(`/api/filings/${encodeURIComponent(id)}`);
    }
    return renderFiling(state.cache[id]);
  }
  $("#main").innerHTML = `<div class="empty">Not found.</div>`;
}

document.addEventListener("click", (e) => {
  const src = e.target.closest("[data-open-page]");
  if (src) {
    e.stopPropagation();
    openPage(Number(src.dataset.openPage), src.dataset.find, src.dataset.kind);
    return;
  }
  const tog = e.target.closest("[data-toggle]");
  if (tog) {
    state.collapsed[tog.dataset.toggle] = !state.collapsed[tog.dataset.toggle];
    route();
    return;
  }
  const row = e.target.closest("tr.item[data-item]");
  if (row) openItem(row.dataset.item);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closePanel();
  if (e.key === "Enter" && e.target.matches("tr.item[data-item]")) openItem(e.target.dataset.item);
});
$("#panel-close").addEventListener("click", closePanel);
$("#panel-back").addEventListener("click", () => state.panel?.itemNumber && openItem(state.panel.itemNumber));
$("#page-prev").addEventListener("click", () => { const p = state.panel; if (p?.mode === "page" && p.index > 0) openPage(p.index - 1, p.find, p.kind); });
$("#page-next").addEventListener("click", () => { const p = state.panel; if (p?.mode === "page" && p.index < p.pageCount - 1) openPage(p.index + 1, p.find, p.kind); });
window.addEventListener("hashchange", route);

(async function init() {
  try {
    [state.policy, state.filings] = await Promise.all([api("/api/policy"), api("/api/filings")]);
    await route();
  } catch (err) {
    $("#main").innerHTML = `<div class="warn">Couldn't reach the server: ${esc(err.message)}</div>`;
  }
})();
