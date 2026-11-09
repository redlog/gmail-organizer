"use strict";

// ---------- helpers ----------
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => n.toLocaleString();
function fmtSize(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(0) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
}
async function api(path, body) {
  const opts = { headers: { "X-Token": APP.token, "Content-Type": "application/json" } };
  if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

// ---------- modal ----------
function modal(html) {
  $("#modal-box").innerHTML = html;
  $("#modal").classList.remove("hidden");
  return $("#modal-box");
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-box").innerHTML = ""; }
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
function alertModal(title, text) {
  const b = modal(`<h2>${esc(title)}</h2><p>${esc(text)}</p><div class="buttons"><button class="primary" id="m-ok">OK</button></div>`);
  b.querySelector("#m-ok").onclick = closeModal;
}

// ---------- state ----------
let allData = [];
let pins = [];        // [{field, value}] exact-match filters
let search = "";
let busy = false;
const FIELD_LABEL = { from_domain: "domain", from_addr: "from", recipient: "to", list_id: "list", month: "month", unread: "unread" };

// ---------- table ----------
// Clicking a link/button inside a row must not change that row's selection.
let pendingSel = null;
function rowFor(el) {
  const rowEl = el.closest(".tabulator-row");
  return rowEl ? table.getRows().find((r) => r.getElement() === rowEl) : null;
}
document.addEventListener("mousedown", (e) => {
  const el = e.target.closest("#table [data-act]");
  const row = el && rowFor(el);
  pendingSel = row ? { row, was: row.isSelected() } : null;
}, true);

function linkFmt(field) {
  return (cell) => {
    const v = cell.getValue();
    if (!v) return "";
    return `<span class="cell-link" data-act="pin" data-field="${field}" title="Show only ${esc(v)}">${esc(v)}</span>`;
  };
}

// Gmail opens a message from its X-GM-MSGID written in hex. authuser= picks the
// right account even when several Google accounts are signed in.
// (Tested: /mail/u/<email>/ gives a 404 for this Workspace account.)
function gmailUrl(d) {
  return `https://mail.google.com/mail/?authuser=${encodeURIComponent(APP.account)}#all/${BigInt(d.gm_msgid).toString(16)}`;
}

const table = new Tabulator("#table", {
  index: "gm_msgid",
  height: "calc(100vh - 190px)",
  layout: "fitColumns",
  placeholder: "No messages",
  selectableRows: true,  // a click toggles just that row; click-and-drag selects a range
  rowHeader: { formatter: "rowSelection", titleFormatter: "rowSelection", titleFormatterParams: { rowRange: "active" },
               headerSort: false, resizable: false, frozen: true, hozAlign: "center", headerHozAlign: "center", width: 36 },
  initialSort: [{ column: "date", dir: "desc" }],
  rowFormatter: (row) => row.getElement().classList.toggle("unread", !!row.getData().unread),
  columns: [
    { title: "Date", field: "date", width: 130, headerFilter: "input" },
    { title: "From name", field: "from_name", widthGrow: 2, headerFilter: "input" },
    { title: "From address", field: "from_addr", widthGrow: 3, headerFilter: "input", formatter: linkFmt("from_addr") },
    { title: "Domain", field: "from_domain", widthGrow: 2, headerFilter: "input", formatter: linkFmt("from_domain") },
    { title: "To address", field: "recipient", widthGrow: 3, headerFilter: "input", formatter: linkFmt("recipient"),
      visible: APP.showTo },
    { title: "To (header)", field: "to", widthGrow: 2, headerFilter: "input", visible: false },
    { title: "Subject", field: "subject", widthGrow: 5, headerFilter: "input",
      formatter: (c) => `<span class="cell-link" data-act="open" title="Open in Gmail (new tab)">${esc(c.getValue() || "(no subject)")}</span>` },
    { title: "Mailing list", field: "list_id", widthGrow: 2, headerFilter: "input", formatter: linkFmt("list_id") },
    { title: "Size", field: "size", width: 80, hozAlign: "right", sorter: "number", formatter: (c) => fmtSize(c.getValue() || 0) },
    { title: "Unread", field: "unread", width: 75, hozAlign: "center", formatter: (c) => (c.getValue() ? "●" : ""),
      headerFilter: "tickCross", headerFilterParams: { tristate: true }, headerFilterEmptyCheck: (v) => v === null || v === undefined || v === "" },
    { title: "Labels", field: "labels", widthGrow: 1, headerFilter: "input", visible: true },
    { title: "Unsubscribe", field: "unsub_url", width: 105, headerSort: false, formatter: (c) => {
        const d = c.getRow().getData();
        if (d.unsub_one_click) return `<button class="unsub-btn" data-act="unsub1">1-click</button>`;
        if (d.unsub_url) return `<button class="unsub-btn" data-act="unsublink">link</button>`;
        if (d.unsub_mailto) return `<button class="unsub-btn" data-act="unsubmail">email</button>`;
        return "";
      } },
  ],
});

$("#table").addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const p = pendingSel;
  pendingSel = null;
  setTimeout(() => { if (p && p.row.isSelected() !== p.was) p.row.toggleSelect(); }, 0);
  const row = rowFor(el);
  if (!row) return;
  const d = row.getData();
  const act = el.dataset.act;
  if (act === "open") window.open(gmailUrl(d), "_blank", "noopener,noreferrer");
  else if (act === "pin") addPin(el.dataset.field, d[el.dataset.field]);
  else if (act === "unsub1") unsubOneClick(d);
  else if (act === "unsublink") openExternal(d.unsub_url, d);
  else if (act === "unsubmail") openExternal(d.unsub_mailto, d);
});

table.on("dataFiltered", () => setTimeout(updateCount, 0));
table.on("rowSelectionChanged", () => updateCount());
table.on("tableBuilt", () => { applyFilters(); });

function filterFn(d) {
  for (const p of pins) {
    if (p.field === "month") { if (!(d.date || "").startsWith(p.value)) return false; }
    else if (String(d[p.field] ?? "") !== String(p.value)) return false;
  }
  if (search) {
    const s = search.toLowerCase();
    const hay = `${d.from_name} ${d.from_addr} ${d.recipient} ${d.to} ${d.subject} ${d.list_id}`.toLowerCase();
    if (!hay.includes(s)) return false;
  }
  return true;
}
function applyFilters() {
  table.setFilter(filterFn);
  renderChips();
  updateCount();
}
function addPin(field, value) {
  pins = pins.filter((p) => p.field !== field);
  pins.push({ field, value: value ?? "" });
  applyFilters();
  showTab("messages");
}
function renderChips() {
  $("#chips").innerHTML = pins.map((p, i) =>
    `<span class="chip">${FIELD_LABEL[p.field] || p.field} = <b>${esc(p.value || "(none)")}</b><button data-i="${i}" title="Remove">✕</button></span>`).join("");
  $("#chips").querySelectorAll("button").forEach((b) => (b.onclick = () => { pins.splice(+b.dataset.i, 1); applyFilters(); }));
}
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { search = e.target.value.trim(); applyFilters(); }, 200);
});
$("#clear").onclick = () => {
  pins = []; search = ""; $("#search").value = "";
  table.clearHeaderFilter();
  applyFilters();
};

function shownRows() { return table.getData("active"); }
function selectedShown() {
  const shown = new Set(shownRows().map((d) => d.gm_msgid));
  return table.getSelectedData().filter((d) => shown.has(d.gm_msgid));
}
function updateCount() {
  const shown = table.getDataCount("active");
  const sel = selectedShown().length;
  $("#count").textContent = `Showing ${fmtN(shown)} of ${fmtN(allData.length)} · ${fmtN(sel)} selected`;
  $("#arch-sel").disabled = $("#trash-sel").disabled = busy || sel === 0;
  $("#arch-all").disabled = $("#trash-all").disabled = busy || shown === 0;
  $("#arch-all").textContent = `Archive all shown (${fmtN(shown)})`;
  $("#trash-all").textContent = `Move all shown to Trash (${fmtN(shown)})`;
  if ($("#tab-stats").classList.contains("active")) renderStats();
}

function describeFilter() {
  const parts = pins.map((p) => `${FIELD_LABEL[p.field] || p.field} = ${p.value || "(none)"}`);
  if (search) parts.push(`search "${search}"`);
  table.getHeaderFilters().forEach((h) => parts.push(`${h.field} contains "${h.value}"`));
  return parts.join("; ");
}

// ---------- archive / trash ----------
function topCounts(rows, key, n) {
  const m = new Map();
  rows.forEach((r) => m.set(r[key] || "(none)", (m.get(r[key] || "(none)") || 0) + 1));
  return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n);
}

function confirmAction(action, rows, how) {
  if (!rows.length) return;
  const verb = action === "archive" ? "Archive" : "Move to Trash";
  const what = action === "archive"
    ? "They will leave INBOX but stay in All Mail. You can undo this from the Action log."
    : "They go to Gmail's Trash, where Gmail keeps them for 30 days. Nothing is permanently deleted. You can undo this from the Action log.";
  const desc = how === "selected" ? `${rows.length} selected` + (describeFilter() ? ` (filter: ${describeFilter()})` : "") : (describeFilter() || "everything in INBOX");
  const doms = topCounts(rows, "from_domain", 5).map(([k, v]) => `<li>${esc(k)} — ${fmtN(v)}</li>`).join("");
  const samples = rows.slice(0, 8).map((r) => `<li><b>${esc(r.from_name || r.from_addr)}</b>: ${esc(r.subject)}</li>`).join("");
  const b = modal(`
    <h2>${verb} ${fmtN(rows.length)} message${rows.length === 1 ? "" : "s"}?</h2>
    <p class="note">${esc(what)}</p>
    <p><b>Which:</b> ${esc(desc)}</p>
    <p><b>Top senders</b></p><ul>${doms}</ul>
    <p><b>Examples</b></p><ul>${samples}</ul>
    <div class="buttons"><button id="m-cancel">Cancel</button>
    <button id="m-go" class="${action === "trash" ? "danger-solid" : "primary"}">${verb} ${fmtN(rows.length)}</button></div>`);
  b.querySelector("#m-cancel").onclick = closeModal;
  b.querySelector("#m-go").onclick = async () => {
    closeModal();
    let j;
    try {
      j = await runJob("/api/action", { account: APP.account, action, gm_msgids: rows.map((x) => x.gm_msgid), description: desc },
                       `${verb} ${fmtN(rows.length)} message${rows.length === 1 ? "" : "s"}`);
    } catch (e) { return alertModal("That didn't work", e.message); }
    await afterChange();
    if (j.state === "cancelled") return alertModal("Stopped", "Stopped before anything was moved. Nothing changed in Gmail.");
    if (j.state === "error") return alertModal("That didn't work", j.error);
    const r = j.result;
    let msg = `${verb}: ${fmtN(r.done)} message(s) confirmed out of INBOX.`;
    if (r.stopped) msg += ` You pressed Stop, so the rest weren't touched; select them again to finish.`;
    if (r.still_in_inbox) msg += ` ${fmtN(r.still_in_inbox)} are STILL in INBOX (Gmail didn't move them).`;
    if (r.missing) msg += ` ${fmtN(r.missing)} were already gone from INBOX and were skipped.`;
    alertModal(r.stopped ? "Stopped partway" : r.still_in_inbox ? "Partly done" : "Done", msg);
  };
}
$("#arch-sel").onclick = () => confirmAction("archive", selectedShown(), "selected");
$("#trash-sel").onclick = () => confirmAction("trash", selectedShown(), "selected");
$("#arch-all").onclick = () => confirmAction("archive", shownRows(), "all");
$("#trash-all").onclick = () => confirmAction("trash", shownRows(), "all");
$("#csv").onclick = () => table.download("csv", "inbox.csv", {}, "active");

// ---------- unsubscribe ----------
function hostOf(u) { try { return new URL(u).host; } catch { return u; } }
function unsubOneClick(d) {
  const b = modal(`<h2>One-click unsubscribe</h2>
    <p>Send an unsubscribe request for <b>${esc(d.from_addr)}</b>${d.list_id ? ` (list ${esc(d.list_id)})` : ""}?</p>
    <p class="note">This sends one standard (RFC 8058) unsubscribe request to <code>${esc(hostOf(d.unsub_url))}</code>. It doesn't change any mail.</p>
    <div class="buttons"><button id="m-cancel">Cancel</button><button id="m-go" class="primary">Unsubscribe</button></div>`);
  b.querySelector("#m-cancel").onclick = closeModal;
  b.querySelector("#m-go").onclick = async () => {
    closeModal();
    try {
      const j = await runJob("/api/unsubscribe", { account: APP.account, gm_msgid: d.gm_msgid }, "Unsubscribing");
      if (j.state !== "done") return alertModal("That didn't work", j.error || "Stopped.");
      const r = j.result;
      if (r.result === "ok") {
        alertModal("Unsubscribe accepted", `${hostOf(r.url)} accepted the request (HTTP ${r.status}).`);
      } else if (r.result === "redirect") {
        const b2 = modal(`<h2>Unclear — the sender redirected</h2>
          <p>${esc(hostOf(r.url))} answered with a redirect (HTTP ${r.status}) instead of confirming.
          The unsubscribe may or may not have gone through.</p>
          ${r.location ? `<p>It points to:</p><p><code>${esc(r.location)}</code></p>
          <p class="note">Opening it shows the sender's page in a new tab, where you can finish unsubscribing if needed.</p>`
            : `<p class="note">It didn't say where. Use the unsubscribe link in the email to be sure.</p>`}
          <div class="buttons"><button id="m-cancel">Close</button>${r.location ? `<button id="m-open" class="primary">Open page</button>` : ""}</div>`);
        b2.querySelector("#m-cancel").onclick = closeModal;
        const open = b2.querySelector("#m-open");
        if (open) open.onclick = () => { window.open(r.location, "_blank", "noopener,noreferrer"); closeModal(); };
      } else {
        alertModal("Unsubscribe failed", `Server answered ${r.status ?? r.error}. Try the link in the email instead.`);
      }
    } catch (e) { alertModal("That didn't work", e.message); }
  };
}
function openExternal(url, d) {
  const isMail = url.toLowerCase().startsWith("mailto:");
  const b = modal(`<h2>${isMail ? "Unsubscribe by email" : "Open unsubscribe page"}</h2>
    <p>From <b>${esc(d.from_addr)}</b>. ${isMail ? "This opens your mail program with an unsubscribe email you can send." : "This opens the sender's page in a new tab:"}</p>
    <p><code>${esc(url)}</code></p>
    <div class="buttons"><button id="m-cancel">Cancel</button><button id="m-go" class="primary">Open</button></div>`);
  b.querySelector("#m-cancel").onclick = closeModal;
  b.querySelector("#m-go").onclick = () => {
    if (/^(https?:|mailto:)/i.test(url)) window.open(url, "_blank", "noopener,noreferrer");
    closeModal();
  };
}

// ---------- Gmail filter export ----------
$("#mkfilter").onclick = () => {
  const get = (f) => (pins.find((p) => p.field === f) || {}).value || "";
  const from = get("from_addr") || get("from_domain");
  const to = get("recipient");
  const list = get("list_id");
  const b = modal(`<h2>Make a Gmail filter</h2>
    <p class="note">Filled in from your current filter. This only creates a file: nothing is sent to Gmail.</p>
    <label>From <input type="text" id="f-from" value="${esc(from)}"></label>
    <label>To <input type="text" id="f-to" value="${esc(to)}"></label>
    <label>Subject <input type="text" id="f-subj"></label>
    <label>Has the words <input type="text" id="f-has" value="${list ? esc(`list:(${list})`) : ""}"></label>
    <p><b>Then:</b></p>
    <label><input type="checkbox" id="f-archive" checked> Skip the Inbox (archive)</label>
    <label><input type="checkbox" id="f-read"> Mark as read</label>
    <label><input type="checkbox" id="f-trash"> Delete it (Gmail moves it to Trash)</label>
    <label><input type="checkbox" id="f-nospam"> Never send it to Spam</label>
    <label>Apply label <input type="text" id="f-label" placeholder="(optional)"></label>
    <p>Gmail search for this filter: <code id="f-q"></code></p>
    <p class="note">To use it: Gmail → Settings → See all settings → Filters and Blocked Addresses → Import filters → choose the file → Open file → Create filters.</p>
    <div class="buttons"><button id="m-cancel">Close</button><button id="m-copy">Copy search</button><button id="m-go" class="primary">Download mailFilters.xml</button></div>`);
  const v = (id) => b.querySelector(id).value.trim();
  const c = (id) => b.querySelector(id).checked;
  const q = () => [v("#f-from") && `from:(${v("#f-from")})`, v("#f-to") && `to:(${v("#f-to")})`,
    v("#f-subj") && `subject:(${v("#f-subj")})`, v("#f-has")].filter(Boolean).join(" ");
  const upd = () => (b.querySelector("#f-q").textContent = q() || "(empty)");
  b.querySelectorAll("input").forEach((i) => i.addEventListener("input", upd));
  upd();
  b.querySelector("#m-cancel").onclick = closeModal;
  b.querySelector("#m-copy").onclick = () => navigator.clipboard.writeText(q());
  b.querySelector("#m-go").onclick = () => {
    if (!q()) return alertModal("Nothing to match", "Fill in at least one of From, To, Subject or Has the words.");
    const x = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/'/g, "&apos;").replace(/"/g, "&quot;");
    const props = [];
    const p = (n, val) => props.push(`<apps:property name='${n}' value='${x(val)}'/>`);
    if (v("#f-from")) p("from", v("#f-from"));
    if (v("#f-to")) p("to", v("#f-to"));
    if (v("#f-subj")) p("subject", v("#f-subj"));
    if (v("#f-has")) p("hasTheWord", v("#f-has"));
    if (c("#f-archive")) p("shouldArchive", "true");
    if (c("#f-read")) p("shouldMarkAsRead", "true");
    if (c("#f-trash")) p("shouldTrash", "true");
    if (c("#f-nospam")) p("shouldNeverSpam", "true");
    if (v("#f-label")) p("label", v("#f-label"));
    const xml = `<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>
<title>Mail Filters</title>
<entry>
<category term='filter'></category>
<title>Mail Filter</title>
<content></content>
${props.join("\n")}
</entry>
</feed>
`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([xml], { type: "application/xml" }));
    a.download = "mailFilters.xml";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
};

// ---------- stats ----------
function rankList(el, entries, field, fmt = fmtN) {
  const max = Math.max(1, ...entries.map((e) => e[1]));
  el.innerHTML = entries.length ? entries.map(([k, v]) =>
    `<div class="rank" data-k="${esc(k)}" title="Show only ${esc(k || "(none)")}">
       <span class="name">${esc(k || "(none)")}</span><span class="num">${fmt(v)}</span>
       <span class="bar" style="width:${(v / max) * 100}%"></span></div>`).join("") : `<p class="muted">Nothing here</p>`;
  el.querySelectorAll(".rank").forEach((r) => (r.onclick = () => addPin(field, r.dataset.k)));
}
function group(rows, key, valFn = () => 1) {
  const m = new Map();
  rows.forEach((r) => { const k = r[key] || ""; m.set(k, (m.get(k) || 0) + valFn(r)); });
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}
function renderStats() {
  const scope = document.querySelector("input[name=scope]:checked").value;
  const rows = scope === "all" ? allData : shownRows();
  const unread = rows.filter((r) => r.unread).length;
  const size = rows.reduce((s, r) => s + (r.size || 0), 0);
  const lists = rows.filter((r) => r.list_id).length;
  const tiles = [
    [fmtN(rows.length), "messages"], [fmtN(unread), "unread"], [fmtSize(size), "total size"],
    [fmtN(new Set(rows.map((r) => r.from_domain)).size), "sender domains"],
    APP.showTo && [fmtN(new Set(rows.map((r) => r.recipient).filter(Boolean)).size), "recipient addresses"],
    [rows.length ? Math.round((lists / rows.length) * 100) + "%" : "–", "from mailing lists"],
  ].filter(Boolean);
  $("#tiles").innerHTML = tiles.map(([v, l]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  rankList($("#st-domain"), group(rows, "from_domain").slice(0, 20), "from_domain");
  rankList($("#st-from"), group(rows, "from_addr").slice(0, 20), "from_addr");
  if (APP.showTo) rankList($("#st-recipient"), group(rows, "recipient").slice(0, 20), "recipient");
  rankList($("#st-list"), group(rows.filter((r) => r.list_id), "list_id").slice(0, 20), "list_id");
  rankList($("#st-size"), group(rows, "from_domain", (r) => r.size || 0).slice(0, 20), "from_domain", fmtSize);
  rankList($("#st-unread"), group(rows.filter((r) => r.unread), "from_domain").slice(0, 20), "from_domain");
  renderMonths(rows);
}
function renderMonths(rows) {
  const m = new Map();
  rows.forEach((r) => { const k = (r.date || "").slice(0, 7); if (k) m.set(k, (m.get(k) || 0) + 1); });
  const keys = [...m.keys()].sort();
  const el = $("#months");
  if (!keys.length) { el.innerHTML = `<p class="muted">Nothing here</p>`; return; }
  const all = [];
  let [y, mo] = keys[0].split("-").map(Number);
  const [ey, em] = keys[keys.length - 1].split("-").map(Number);
  while (y < ey || (y === ey && mo <= em)) {
    all.push(`${y}-${String(mo).padStart(2, "0")}`);
    if (++mo > 12) { mo = 1; y++; }
  }
  // Last 24 months, with anything older folded into one "earlier" bar.
  const recent = all.slice(-24);
  const cols = recent.map((k) => [k, m.get(k) || 0]);
  const older = all.slice(0, -24).reduce((s, k) => s + (m.get(k) || 0), 0);
  if (older) cols.unshift(["earlier", older]);
  const max = Math.max(...cols.map((c) => c[1]));
  const every = Math.ceil(cols.length / 12);
  el.innerHTML = cols.map(([k, v], i) =>
    `<div class="mcol" data-k="${k}" data-v="${v}"><div class="b" style="height:${(v / max) * 100}%"></div>${i % every === 0 || k === "earlier" ? `<span class="lab">${k}</span>` : ""}</div>`
  ).join("");
  const tip = $("#tooltip");
  el.querySelectorAll(".mcol").forEach((c) => {
    if (c.dataset.k !== "earlier") c.onclick = () => addPin("month", c.dataset.k);
    c.onmousemove = (e) => { tip.textContent = `${c.dataset.k === "earlier" ? `Before ${recent[0]}` : c.dataset.k}: ${fmtN(+c.dataset.v)} messages`; tip.style.left = e.clientX + 12 + "px"; tip.style.top = e.clientY - 28 + "px"; tip.classList.remove("hidden"); };
    c.onmouseleave = () => tip.classList.add("hidden");
  });
}
document.querySelectorAll("input[name=scope]").forEach((r) => (r.onchange = renderStats));

// ---------- action log ----------
async function loadLog() {
  const rows = await api("/api/actions");
  $("#log-body").innerHTML = rows.length ? rows.map((a) => `<tr>
    <td>${esc(a.ts.replace("T", " "))}</td>
    <td>${a.action === "archive" ? "Archived" : "Moved to Trash"}</td>
    <td>${fmtN(a.count)}</td>
    <td>${esc(a.description)}</td>
    <td class="${a.undone_ts ? "undone" : ""}">${a.undone_ts ? "Undone " + esc(a.undone_ts.replace("T", " ")) : a.count ? `<button data-id="${a.id}" data-n="${a.count}" data-a="${a.action}">Undo</button>` : ""}</td>
  </tr>`).join("") : `<tr><td colspan="5" class="muted">No actions yet.</td></tr>`;
  $("#log-body").querySelectorAll("button").forEach((btn) => (btn.onclick = () => {
    const what = btn.dataset.a === "archive" ? "put the Inbox label back on" : "move back from Trash to INBOX";
    const b = modal(`<h2>Undo?</h2><p>This will ${what} exactly the ${fmtN(+btn.dataset.n)} message(s) from this action. To find them, it opens ${btn.dataset.a === "archive" ? "All Mail" : "Trash"} and changes only those messages.</p>
      <div class="buttons"><button id="m-cancel">Cancel</button><button id="m-go" class="primary">Undo</button></div>`);
    b.querySelector("#m-cancel").onclick = closeModal;
    b.querySelector("#m-go").onclick = async () => {
      closeModal();
      let j;
      try {
        j = await runJob("/api/undo", { account: APP.account, action_id: +btn.dataset.id }, "Undoing");
      } catch (e) { return alertModal("That didn't work", e.message); }
      await afterChange();
      if (j.state === "cancelled") return alertModal("Stopped", "Stopped before anything was put back. Nothing changed in Gmail.");
      if (j.state === "error") return alertModal("That didn't work", j.error);
      alertModal("Undone", `Put ${fmtN(j.result.restored)} of ${fmtN(j.result.of)} message(s) back in INBOX.`);
    };
  }));
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  if (name === "stats") renderStats();
  if (name === "log") loadLog();
  if (name === "messages") table.redraw();
}
document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------- background jobs + progress dialog ----------
// Every Gmail operation runs on the server as a background job. While one runs, a dialog
// covers the page (so nothing else can be started) and shows progress, with Stop and Exit.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const JOB_TITLES = { refresh: "Refreshing INBOX", switch: "Switching account", archive: "Archiving",
                     trash: "Moving to Trash", undo: "Undoing", unsubscribe: "Unsubscribing" };

function setBusy(v) { busy = v; $("#refresh").disabled = v; updateCount(); }

function showProgress(title) {
  $("#progress-title").textContent = title;
  $("#progress-msg").textContent = "Starting…";
  $("#progress-bar").style.width = "0%";
  $("#progress-barwrap").classList.add("hidden");
  $("#progress-hint").classList.add("hidden");
  $("#progress-stop").disabled = false;
  $("#progress-stop").textContent = "Stop";
  $("#progress").classList.remove("hidden");
}
function updateProgress(j) {
  $("#progress-msg").textContent = j.total ? `${j.message} ${fmtN(j.done)} / ${fmtN(j.total)}` : j.message;
  $("#progress-barwrap").classList.toggle("hidden", !j.total);
  if (j.total) $("#progress-bar").style.width = `${Math.min(100, (100 * j.done) / j.total)}%`;
  $("#progress-time").textContent = `${Math.round(j.elapsed || 0)}s`;
  $("#progress-hint").classList.toggle("hidden", (j.elapsed || 0) < 30);
}
function hideProgress() { $("#progress").classList.add("hidden"); }

$("#progress-stop").onclick = async () => {
  $("#progress-stop").disabled = true;
  $("#progress-stop").textContent = "Stopping…";
  try { await api("/api/cancel", {}); } catch { /* the dialog keeps polling either way */ }
};
$("#progress-exit").onclick = () => exitApp();

// Start a job and wait for it, showing the progress dialog. Returns the finished job
// ({state: "done"|"error"|"cancelled", result, error}). Throws if the request is refused.
async function runJob(path, body, title) {
  const { job } = await api(path, body);
  return watchJob(job, title);
}
async function watchJob(jobId, title) {
  setBusy(true);
  showProgress(title);
  try {
    for (;;) {
      const s = await api("/api/status");
      if (s.job.id === jobId) updateProgress(s.job);
      if (s.job.id !== jobId || s.job.state !== "running") { setStatusLine(s); return s.job; }
      await sleep(500);
    }
  } finally { hideProgress(); setBusy(false); }
}

function setStatusLine(s) {
  const el = $("#status");
  const j = s.job;
  const failed = j.state === "error";
  el.classList.toggle("error", failed);
  el.textContent = failed ? `${JOB_TITLES[j.kind] || "Last operation"} failed: ${j.error}`
    : j.state === "cancelled" ? `${JOB_TITLES[j.kind] || "Operation"} stopped${s.last_refresh ? ` · last refreshed ${s.last_refresh}` : ""}`
    : `${j.kind === "refresh" || j.kind === "switch" ? j.message : ""}${s.last_refresh ? `${j.kind === "refresh" || j.kind === "switch" ? " · " : ""}last refreshed ${s.last_refresh}` : ""}`;
  el.title = el.textContent;
}

async function loadMessages() {
  allData = await api("/api/messages");
  await table.replaceData(allData);
  applyFilters();
}
async function afterChange() {
  await loadMessages();
  loadLog();
}

$("#refresh").onclick = async () => {
  try {
    const j = await runJob("/api/refresh", {}, "Refreshing INBOX");
    await loadMessages();
    if (j.state === "error") alertModal("Refresh failed", j.error);
  } catch (e) { alertModal("Couldn't refresh", e.message); }
};

async function exitApp() {
  $("#exit").disabled = $("#refresh").disabled = $("#progress-exit").disabled = $("#progress-stop").disabled = true;
  $("#progress-msg").textContent = "Stopping the current operation, then exiting…";
  try {
    const r = await api("/api/exit", {});
    document.body.innerHTML = `<div style="padding:48px 24px;text-align:center">
      <h2 style="margin:0 0 8px">Gmail Organizer has stopped</h2>
      <p class="muted">${r.was_running ? "The operation in progress was stopped first. " : ""}The app has shut down. You can close this tab.<br>
      Run <code>python app.py</code> to start it again.</p></div>`;
  } catch (e) {
    // The server is already gone (e.g. stopped with Ctrl+C): that's an exit too.
    document.body.innerHTML = `<div style="padding:48px 24px;text-align:center">
      <h2 style="margin:0 0 8px">Gmail Organizer isn't running</h2>
      <p class="muted">Run <code>python app.py</code> to start it again.</p></div>`;
  }
}
$("#exit").onclick = () => exitApp();

// ---------- account picker ----------
(function accountPicker() {
  const sel = $("#account");
  sel.innerHTML = APP.accounts.map((a) => `<option value="${esc(a)}"${a === APP.account ? " selected" : ""}>${esc(a)}</option>`).join("");
  sel.disabled = APP.accounts.length < 2;
  sel.title = APP.accounts.length < 2 ? "Add more accounts with: python app.py --setup" : "Switch account";
  sel.onchange = async () => {
    const to = sel.value;
    try {
      await runJob("/api/switch", { account: to }, `Switching to ${to}`);
    } catch (e) {
      sel.value = APP.account;
      return alertModal("Couldn't switch account", e.message);
    }
    location.reload();  // the page is rebuilt for the new account (columns, stats, action log)
  };
})();
if (!APP.showTo) $("#panel-recipient").remove();
$("#search").placeholder = APP.showTo ? "Search sender, To address, subject…" : "Search sender, subject…";

// On load: if a job is already running (e.g. the refresh the app starts with), show it.
(async function start() {
  try {
    let s = await api("/api/status");
    if (s.job.state === "running") {
      await watchJob(s.job.id, JOB_TITLES[s.job.kind] || "Working");
      s = await api("/api/status");
    }
    await loadMessages();
    setStatusLine(s);
  } catch (e) {
    $("#status").textContent = `Couldn't reach the app: ${e.message}`;
  }
})();
