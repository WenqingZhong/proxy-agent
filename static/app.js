// Proxy Agent front end: plain JS, no build step.
// Routes: #/filing/<id>  and  #/policy

const state = {
  policy: null,
  filings: [],
  cache: {},           // id -> full filing
  viewer: null,        // { filingId, index, quote, pageCount }
  collapsed: {},       // "<filingId>:<proposal>" -> bool
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
const ruleLabel = (id) => {
  const r = ruleById(id);
  if (!r) return id;
  return id === "FLAG" ? "Flag for review" : r.section;
};

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
          <span>${f.item_count} items${f.flag_count ? ` · <span class="flags">${f.flag_count} flag</span>` : ""}</span>
        </span>
      </a>
    </li>`).join("");
}

// ------------------------------------------------------------------ filing

function groupItems(items) {
  // Keep ballot order; director nominees (1a, 1b...) roll up under one group row.
  const groups = [];
  for (const it of items) {
    const base = it.number.replace(/[a-z]+$/, "");
    const isSub = base !== it.number;
    const last = groups[groups.length - 1];
    if (isSub && last && last.base === base) last.items.push(it);
    else groups.push({ base, isGroup: isSub, items: [it] });
  }
  return groups;
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

function srcButton(filing, ev, label) {
  if (!ev) return "";
  return `<button class="src" data-open-page="${ev.page_index}" data-quote="${esc(ev.quote)}"
            title="${esc(label)}">p. ${esc(ev.page)}</button>`;
}

function itemRow(filing, it, { sub = false } = {}) {
  const sec = it.section;
  const recEv = it.board_rec_evidence;
  return `
    <tr class="item">
      <td class="num">${esc(it.number)}</td>
      <td class="title">
        <div class="item-title">${esc(it.title)}</div>
        ${!sub && it.kind !== "director" ? `<div class="item-sub">${esc(kindLabel(it.kind))}</div>` : ""}
      </td>
      <td><span class="proponent">${esc(it.proponent)}</span></td>
      <td>${recBadge(it.board_rec)} ${recEv && !sub ? srcButton(filing, recEv, "Where the board states its recommendation") : ""}</td>
      <td>${ruleChip(it.policy_rule)}</td>
      <td>${srcButton(filing, sec, "Start of this item's section")}</td>
    </tr>`;
}

function kindLabel(kind) {
  return {
    director: "Director election",
    say_on_pay: "Say-on-pay",
    auditor: "Auditor ratification",
    board_structure: "Board structure",
    other: "Not covered by policy",
  }[kind] || kind;
}

function renderFiling(f) {
  const counts = {};
  for (const it of f.items) counts[it.policy_rule] = (counts[it.policy_rule] || 0) + 1;
  const against = f.items.filter((i) => i.board_rec === "AGAINST").length;

  const rows = groupItems(f.items).map((g) => {
    if (!g.isGroup) return itemRow(f, g.items[0]);
    const key = `${f.id}:${g.base}`;
    const collapsed = !!state.collapsed[key];
    const first = g.items[0];
    return `
      <tr class="group">
        <td colspan="6">
          <button class="toggle" data-toggle="${esc(key)}" aria-label="Toggle">${collapsed ? "▸" : "▾"}</button>
          Proposal ${esc(g.base)} · Election of directors
          <span class="group-count">${g.items.length} nominees</span>
          <span style="float:right; font-weight:500">
            Board: ${recBadge(first.board_rec)} ${srcButton(f, first.board_rec_evidence, "Where the board states its recommendation")}
          </span>
        </td>
      </tr>
      ${collapsed ? "" : g.items.map((it) => itemRow(f, it, { sub: true })).join("")}`;
  }).join("");

  $("#main").innerHTML = `
    <div class="page-head">
      <div>
        <div class="kicker">DEF 14A · Annual meeting</div>
        <h1>${esc(f.company)}</h1>
        <div class="meta">
          <span>Meeting ${esc(f.meeting_date || "date not found")}</span>
          <span>${f.page_count} pages</span>
          <code>${esc(f.source_file)}</code>
        </div>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-num">${f.items.length}</div><div class="stat-label">Ballot items</div></div>
        <div class="stat"><div class="stat-num">${(counts.DIRECTORS || 0) + (counts.EXEC_PAY || 0) + (counts.AUDITOR || 0) + (counts.BOARD_STRUCTURE || 0)}</div><div class="stat-label">Covered by policy</div></div>
        <div class="stat flag"><div class="stat-num">${counts.FLAG || 0}</div><div class="stat-label">Flagged for you</div></div>
        <div class="stat"><div class="stat-num">${against}</div><div class="stat-label">Board says against</div></div>
      </div>
    </div>
    ${f.warnings.length ? `<div class="warn">${f.warnings.map(esc).join("<br>")}</div>` : ""}
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>#</th><th>Ballot item</th><th>Proposed by</th><th>Board rec.</th><th>Policy rule</th><th>Source</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ------------------------------------------------------------------ policy

function renderPolicy() {
  const p = state.policy;
  $("#main").innerHTML = `
    <div class="page-head">
      <div>
        <div class="kicker">Hard-coded · version ${esc(p.version)}</div>
        <h1>${esc(p.name)}</h1>
        <div class="meta"><span>${p.rules.length} rules. Anything no rule covers is flagged for the analyst, never auto-voted.</span></div>
      </div>
    </div>
    <div class="policy-grid">
      ${p.rules.map((r) => `
        <div class="policy-card">
          <div>
            <h3>${esc(r.section)}</h3>
            ${ruleChip(r.id)}
          </div>
          <div>
            <p class="policy-text">${esc(r.text)}</p>
            <div class="policy-facts">
              <span><b>Default vote:</b> ${r.default ? esc(r.default) : "None (analyst decides)"}</span>
              <span><b>Applies to:</b> ${r.covers.map(kindLabel).map(esc).join(", ")}</span>
              ${r.needs.length ? `<span><b>Needs from the filing:</b> ${r.needs.map(esc).join("; ")}</span>` : ""}
            </div>
          </div>
        </div>`).join("")}
    </div>
    <p class="note">Edit <code>app/policy.py</code> to change the rules. Recommendations aren't computed yet. This view shows which rule each ballot item falls under.</p>`;
}

// ------------------------------------------------------------------ page viewer

async function openPage(filingId, index, quote) {
  const page = await api(`/api/filings/${encodeURIComponent(filingId)}/pages/${index}`);
  state.viewer = { filingId, index: page.index, quote, pageCount: page.page_count };
  const f = state.cache[filingId];
  $("#viewer-company").textContent = f ? f.company : "";
  $("#viewer-page").textContent = page.label.startsWith("#")
    ? `${page.index + 1} of ${page.page_count} (unnumbered)`
    : `${page.label}  ·  ${page.index + 1} of ${page.page_count} in file`;
  const norm = (s) => s.replace(/\s+/g, " ").trim().toLowerCase();
  const q = quote ? norm(quote) : "";
  $("#viewer-body").innerHTML = page.lines.map((l) => {
    const hit = q && (norm(l) === q || (q.length > 20 && norm(l).includes(q)));
    return `<p class="${hit ? "hit" : ""}">${esc(l)}</p>`;
  }).join("");
  $("#viewer-prev").disabled = page.index === 0;
  $("#viewer-next").disabled = page.index >= page.page_count - 1;
  $("#viewer").hidden = false;
  const hitEl = $("#viewer-body .hit");
  if (hitEl) hitEl.scrollIntoView({ block: "center" });
  else $("#viewer-body").scrollTop = 0;
}

function closeViewer() {
  $("#viewer").hidden = true;
  state.viewer = null;
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
    closeViewer();
    return renderPolicy();
  }
  const m = hash.match(/^#\/filing\/(.+)$/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (state.viewer && state.viewer.filingId !== id) closeViewer();
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
    const m = location.hash.match(/^#\/filing\/(.+)$/);
    if (m) openPage(decodeURIComponent(m[1]), Number(src.dataset.openPage), src.dataset.quote);
    return;
  }
  const tog = e.target.closest("[data-toggle]");
  if (tog) {
    state.collapsed[tog.dataset.toggle] = !state.collapsed[tog.dataset.toggle];
    route();
  }
});
$("#viewer-close").addEventListener("click", closeViewer);
$("#viewer-prev").addEventListener("click", () => {
  const v = state.viewer; if (v && v.index > 0) openPage(v.filingId, v.index - 1, v.quote);
});
$("#viewer-next").addEventListener("click", () => {
  const v = state.viewer; if (v && v.index < v.pageCount - 1) openPage(v.filingId, v.index + 1, v.quote);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeViewer(); });
window.addEventListener("hashchange", route);

(async function init() {
  try {
    [state.policy, state.filings] = await Promise.all([api("/api/policy"), api("/api/filings")]);
    await route();
  } catch (err) {
    $("#main").innerHTML = `<div class="warn">Couldn't reach the server: ${esc(err.message)}</div>`;
  }
})();
