"use strict";

const EXAMPLES = [
  "Why did smartwatch returns spike in 2026?",
  "Total shipping fees collected by product category",
  "What are customers complaining about during the festive season?",
  "How did people react to the new Mathura store?",
  "Show monthly revenue and gross profit trend",
  "Which subcategory had the highest return rate in 2026?",
  "How did online vs store revenue share change by quarter?",
  "Top 10 products by revenue this year",
  "Which region has the highest average order value?",
  "Compare festive season (Oct-Nov) revenue in 2025 vs 2024 by category",
  "Who are our top 5 platinum customers by spend?",
  "How is the new Mathura store doing compared to other North stores?",
  "What share of revenue comes from each payment method?",
];

const $ = (s, el = document) => el.querySelector(s);
const thread = $("#thread");
const turns = []; // {question, sql} of successful answers, for follow-up context

// ---------------------------------------------------------------- helpers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}
const fmtNum = (v) => {
  if (typeof v !== "number") return esc(v);
  const abs = Math.abs(v);
  if (abs >= 1e7) return (v / 1e7).toFixed(2) + " Cr";
  if (abs >= 1e5) return (v / 1e5).toFixed(2) + " L";
  return Number.isInteger(v) ? v.toLocaleString("en-IN") : v.toLocaleString("en-IN", { maximumFractionDigits: 2 });
};
const SQL_KW = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|HAVING|JOIN|LEFT JOIN|INNER JOIN|ON|AS|AND|OR|NOT|IN|WITH|CASE|WHEN|THEN|ELSE|END|SUM|COUNT|AVG|MIN|MAX|DISTINCT|FETCH FIRST|ROWS ONLY|ROW ONLY|OVER|PARTITION BY|DESC|ASC|LIMIT|BETWEEN|IS|NULL|UNION ALL|UNION|DECIMAL|YEAR|MONTH|QUARTER|NULLIF|LAG|LEAD|RANK|CURRENT DATE)\b/g;
function highlightSql(sql) {
  return esc(sql)
    .replace(/'([^']|'')*'/g, m => `<span class="str">${m}</span>`)
    .replace(SQL_KW, m => `<span class="kw">${m}</span>`);
}
function autosize(t) { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; }

// ---------------------------------------------------------------- sidebar
function initTabs() {
  document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === "panel-" + btn.dataset.tab));
    ({ history: loadHistory, memory: loadMemory, schema: loadSchema, social: loadSocial })[btn.dataset.tab]?.();
  }));
  $("#examples").innerHTML = EXAMPLES.map(q => `<li>${esc(q)}</li>`).join("");
  $("#examples").addEventListener("click", e => { if (e.target.tagName === "LI") ask(e.target.textContent); });
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const v = h.db2.version;
    $("#status-line").textContent = `${h.db2.dbms} ${v} · ${h.llm || "offline mode"}`;
  } catch (e) {
    $("#status-line").textContent = "Db2 unreachable";
  }
}

async function loadStats() {
  try {
    const s = await api("/api/stats");
    const t = s.totals;
    $("#stats").innerHTML = `
      <div><b>${t.QUESTIONS ?? 0}</b>questions</div>
      <div><b>${t.SELF_CORRECTED ?? 0}</b>self-fixed</div>
      <div><b>${t.GRAIN_CAUGHT ?? 0}</b>bad totals caught</div>
      <div><b>${s.memory_size}</b>memories</div>`;
  } catch { /* ignore */ }
}

async function loadHistory() {
  const { items } = await api("/api/history?limit=40");
  $("#history").innerHTML = items.map(i => `
    <li data-q="${esc(i.QUESTION)}">${esc(i.QUESTION)}
      <div class="meta"><span class="badge ${i.STATUS}">${i.STATUS}</span>
        <span>${i.ELAPSED_MS ?? "–"} ms</span><span>${i.ROW_COUNT ?? 0} rows</span>
        ${i.ATTEMPTS > 1 ? `<span>${i.ATTEMPTS} attempts</span>` : ""}</div></li>`).join("")
    || `<p class="hint">No questions yet.</p>`;
  $("#history").onclick = e => { const li = e.target.closest("li"); if (li) ask(li.dataset.q); };
}

async function loadMemory() {
  const { items } = await api("/api/memory");
  $("#memory").innerHTML = items.map(i => `
    <li data-q="${esc(i.QUESTION)}"><button class="del" data-id="${i.MEMORY_ID}" title="Forget">✕</button>${esc(i.QUESTION)}
      <div class="meta"><span class="badge ${i.SOURCE}">${i.SOURCE}</span><span>used ${i.TIMES_USED}×</span></div></li>`).join("");
  $("#memory").onclick = async e => {
    if (e.target.classList.contains("del")) {
      e.stopPropagation();
      await api(`/api/memory/${e.target.dataset.id}`, { method: "DELETE" });
      loadMemory(); loadStats();
      return;
    }
    const li = e.target.closest("li"); if (li) ask(li.dataset.q);
  };
}

const SOCIAL_EXAMPLES = [
  "Why did smartwatch returns spike in 2026?",
  "Monthly post volume and average sentiment by platform",
  "Which brands have the most negative posts this year?",
  "What do customers say about delivery during Diwali?",
];

async function loadSocial() {
  const el = $("#social");
  const s = await api("/api/stats").then(r => r.social).catch(() => null);
  const rows = s?.platforms?.map(p => `<div class="col-row"><span>${esc(p.platform)}</span>
      <span>${p.posts.toLocaleString()} posts · ${sentimentLabel(p.avg_sentiment)}</span></div>`).join("") || "";
  el.innerHTML = `
    <p class="hint">${(s?.total ?? 0).toLocaleString()} customer posts in <code>RETAIL.SOCIAL_POSTS</code>, embedded as
      <code>VECTOR(768)</code>. "Why" questions pull matching posts with one hybrid Db2 query
      (<code>VECTOR_DISTANCE</code> + date/product filters).</p>
    ${rows}
    <form id="social-form" class="social-form"><input id="social-q" placeholder="Search posts by meaning, e.g. strap broke"><button>Search</button></form>
    <div id="social-results"></div>
    <p class="hint" style="margin-top:14px">Try asking:</p>
    <ul class="examples">${SOCIAL_EXAMPLES.map(q => `<li>${esc(q)}</li>`).join("")}</ul>`;
  el.querySelector(".examples").onclick = e => { if (e.target.tagName === "LI") ask(e.target.textContent); };
  $("#social-form").onsubmit = async e => {
    e.preventDefault();
    const q = $("#social-q").value.trim(); if (!q) return;
    const out = $("#social-results"); out.innerHTML = `<p class="hint">searching…</p>`;
    try {
      const r = await api(`/api/social/search?q=${encodeURIComponent(q)}`);
      out.innerHTML = `<p class="hint">${r.total_matches} matching posts</p>` + r.posts.slice(0, 6).map(renderPost).join("");
    } catch (err) { out.innerHTML = `<div class="error-box">${esc(err.message)}</div>`; }
  };
}

function sentimentLabel(v) {
  if (v == null) return "–";
  const word = v > 0.25 ? "positive" : v < -0.25 ? "negative" : "mixed";
  return `${word} (${v > 0 ? "+" : ""}${v.toFixed(2)})`;
}

const PLATFORM_ICON = { REDDIT: "r/", X: "𝕏", REVIEW: "★", OTHER: "•" };

function renderPost(p) {
  const tone = p.sentiment > 0.25 ? "pos" : p.sentiment < -0.25 ? "neg" : "mid";
  const about = p.product || (p.store_city ? `Bazaar ${p.store_city}` : p.brand) || "";
  return `<div class="post">
    <div class="post-head"><span class="plat ${esc(p.platform)}">${PLATFORM_ICON[p.platform] || "•"}</span>
      <span class="post-ch">${esc(p.channel)}</span><span class="post-date">${esc(p.date)}</span>
      <span class="senti ${tone}">${p.sentiment > 0 ? "+" : ""}${p.sentiment.toFixed(2)}</span></div>
    <div class="post-text">${esc(p.text)}</div>
    <div class="post-meta">${about ? esc(about) + " · " : ""}${esc(p.topic)} · ${p.engagement} engagements${p.similarity != null ? ` · match ${(p.similarity * 100).toFixed(0)}%` : ""}</div>
  </div>`;
}

async function loadSchema() {
  const el = $("#schema");
  if (el.dataset.loaded) return;
  const { schema, tables } = await api("/api/schema");
  el.innerHTML = tables.map(t => `
    <div class="tbl-name"><span>${t.kind === "view" ? "◇" : "▦"} ${schema}.${esc(t.name)}</span><small>${t.rows ? t.rows.toLocaleString() + " rows" : t.kind}</small></div>
    ${t.remark ? `<div class="tbl-remark">${esc(t.remark)}</div>` : ""}
    ${t.columns.map(c => `<div class="col-row"><span class="mono">${esc(c.name)}</span><span>${esc(c.type)}</span></div>`).join("")}`).join("");
  el.dataset.loaded = "1";
}

// ---------------------------------------------------------------- asking
async function ask(question) {
  question = question.trim();
  if (!question) return;
  $("#welcome")?.remove();
  const card = $("#card-tpl").content.firstElementChild.cloneNode(true);
  $(".card-q", card).textContent = question;
  thread.appendChild(card);
  card.scrollIntoView({ behavior: "smooth", block: "start" });

  const btn = $("#send"); btn.disabled = true;
  const history = $("#use-context").checked ? turns.slice(-3) : [];
  try {
    const res = await api("/api/ask", { method: "POST", body: JSON.stringify({ question, history }) });
    renderResult(card, res);
    if (res.status === "OK" && res.sql) turns.push({ question, sql: res.sql });
  } catch (e) {
    $(".card-body", card).innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    loadStats();
  }
}

function renderResult(card, r) {
  const body = $(".card-body", card);
  const parts = [];
  if (r.status === "OK") {
    parts.push(`<div class="title-row"><h3>${esc(r.title || "Result")}</h3>
      <div class="view-toggle"><button data-v="chart" class="on">Chart</button><button data-v="table">Table</button></div></div>`);
    parts.push(renderVerification(r));
    if (r.answer) parts.push(`<div class="answer">${esc(r.answer).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</div>`);
    parts.push(`<div class="viz"></div><div class="tbl" hidden></div>`);
    if (r.truncated) parts.push(`<p class="hint">Showing the first ${r.rows.length} rows (row cap).</p>`);
    if (r.social?.posts?.length) parts.push(renderVoice(r.social));
  } else {
    const cls = r.status === "BLOCKED" ? "error-box blocked" : "error-box";
    const label = { BLOCKED: "Blocked by guardrail", NO_LLM: "Offline mode", ERROR: "Could not answer" }[r.status] || r.status;
    parts.push(`<div class="${cls}"><b>${label}.</b> ${esc(r.error)}</div>`);
  }
  if (r.follow_ups?.length) {
    parts.push(`<div class="followups">${r.follow_ups.map(f => `<button>${esc(f)}</button>`).join("")}</div>`);
  }
  if (r.status === "OK") {
    parts.push(`<div class="actions">
      <button class="save">👍 Correct — save to Db2 memory</button>
      <button class="copy">Copy SQL</button>
      <button class="csv">Download CSV</button></div>`);
  }
  parts.push(renderTrace(r));
  body.innerHTML = parts.join("");

  if (r.status === "OK") {
    const viz = $(".viz", body), tbl = $(".tbl", body);
    tbl.innerHTML = renderTable(r.columns, r.rows);
    const chartType = r.chart?.type || "table";
    if (chartType === "table") {
      viz.hidden = true; tbl.hidden = false;
      $(".view-toggle", body).hidden = true;
    } else if (chartType === "number") {
      viz.innerHTML = renderKpis(r.columns, r.rows, r.chart);
    } else {
      viz.innerHTML = `<div class="chart-wrap"><canvas></canvas></div>`;
      drawChart($("canvas", viz), r);
    }
    const vc = $(".voice canvas", body);
    if (vc) drawVoice(vc, r.social);
    body.querySelectorAll(".view-toggle button").forEach(b => b.addEventListener("click", () => {
      body.querySelectorAll(".view-toggle button").forEach(x => x.classList.toggle("on", x === b));
      viz.hidden = b.dataset.v !== "chart"; tbl.hidden = b.dataset.v !== "table";
    }));
    $(".save", body).addEventListener("click", async (e) => {
      const b = e.currentTarget; b.disabled = true;
      try {
        const out = await api("/api/feedback", { method: "POST", body: JSON.stringify({ question: r.question, sql: r.sql }) });
        b.outerHTML = `<span class="saved">✓ Stored as memory #${out.memory_id} (VECTOR embedding in Db2)</span>`;
        loadStats();
      } catch (err) { b.disabled = false; alertInline(body, err.message); }
    });
    $(".copy", body).addEventListener("click", e => { navigator.clipboard.writeText(r.sql); e.currentTarget.textContent = "Copied"; });
    $(".csv", body).addEventListener("click", () => downloadCsv(r));
  }
  body.querySelectorAll(".followups button").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
}

const money = v => (v == null ? "–" : "₹" + fmtNum(v));

function renderVerification(r) {
  const g = r.grain;
  if (!g) return "";
  const caught = r.grain_caught || [];
  if (caught.length && g.status !== "fail") {
    const c = caught[0];
    return `<div class="verify fixed"><b>Db2 caught a wrong number.</b> The first draft's <code>${esc(c.aggregate)}</code>
      counted rows of ${esc(c.grain_table)} more than once (${c.source_rows.toLocaleString()} rows became
      ${c.joined_rows.toLocaleString()} after the join): ${money(c.naive_value)} instead of ${money(c.exact_value)},
      <b>${c.factor}× inflated</b>. The query was rewritten and re-verified.</div>`;
  }
  if (g.status === "fail") {
    const c = caught[caught.length - 1] || {};
    return `<div class="verify bad"><b>Unverified total.</b> Db2's grain check found <code>${esc(c.aggregate)}</code>
      ${c.factor}× inflated by a join and the model could not fix it. Treat these numbers with caution.</div>`;
  }
  if (g.status === "pass") {
    const n = g.checks.filter(c => c.status === "pass").length;
    return `<div class="verify ok">✓ Totals verified by Db2: ${n} aggregate${n > 1 ? "s" : ""} re-computed over distinct source rows, no double counting.</div>`;
  }
  return "";
}

function renderVoice(s) {
  const f = Object.entries(s.filters || {}).map(([k, v]) => `${k.replace("_", " ")}: ${esc(v)}`).join(" · ");
  return `<section class="voice">
    <div class="voice-head"><h4>Voice of the customer</h4>
      <span class="hint">${s.total_matches} posts match “${esc(s.query)}” ${f ? "· " + f : ""} · Db2 VECTOR_DISTANCE + SQL filters</span></div>
    ${s.timeline.length > 1 ? `<div class="voice-chart"><canvas></canvas></div>` : ""}
    <div class="posts">${s.posts.slice(0, 4).map(renderPost).join("")}</div>
  </section>`;
}

function drawVoice(canvas, s) {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const tick = dark ? "#9aa1ab" : "#5d6470", grid = dark ? "#2f343c" : "#e5e7eb";
  new Chart(canvas, {
    data: {
      labels: s.timeline.map(t => t.month),
      datasets: [
        { type: "bar", label: "matching posts", data: s.timeline.map(t => t.mentions), backgroundColor: "#8a3ffc", yAxisID: "y" },
        { type: "line", label: "avg sentiment", data: s.timeline.map(t => t.avg_sentiment), borderColor: "#ff832b",
          backgroundColor: "#ff832b", tension: .3, pointRadius: 2, yAxisID: "s" },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: tick, boxWidth: 10 } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: tick, maxRotation: 45, autoSkip: true } },
        y: { beginAtZero: true, grid: { color: grid }, ticks: { color: tick, precision: 0 } },
        s: { position: "right", min: -1, max: 1, grid: { display: false }, ticks: { color: tick } },
      },
    },
  });
}

function alertInline(body, msg) {
  const d = document.createElement("div"); d.className = "error-box"; d.textContent = msg; body.appendChild(d);
}

function renderTable(cols, rows) {
  const head = cols.map(c => `<th>${esc(c)}</th>`).join("");
  const bodyRows = rows.map(r => `<tr>${r.map(v => typeof v === "number"
    ? `<td class="num">${v.toLocaleString("en-IN", { maximumFractionDigits: 2 })}</td>`
    : `<td>${esc(v ?? "∅")}</td>`).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${bodyRows}</tbody></table></div>`;
}

function renderKpis(cols, rows, chart) {
  const row = rows[0];
  return `<div class="kpis">${(chart.y || []).map(c => {
    const v = row[cols.indexOf(c)];
    const money = /REVENUE|PROFIT|SPEND|VALUE|SALES|AMOUNT/i.test(c);
    return `<div class="kpi"><div class="label">${esc(c.replace(/_/g, " "))}</div><div class="value">${money ? "₹" : ""}${fmtNum(v)}</div></div>`;
  }).join("")}</div>`;
}

const PALETTE = ["#0f62fe", "#24a148", "#8a3ffc", "#ff832b", "#1192e8", "#fa4d56", "#009d9a", "#a56eff", "#b28600", "#ee5396"];

function drawChart(canvas, r) {
  const { columns: cols, rows, chart } = r;
  const xi = cols.indexOf(chart.x);
  const ys = (chart.y || []).map(c => cols.indexOf(c)).filter(i => i >= 0);
  let labels, datasets;
  if (chart.series && cols.includes(chart.series)) {
    const si = cols.indexOf(chart.series);
    labels = [...new Set(rows.map(row => row[xi]))];
    const series = [...new Set(rows.map(row => row[si]))];
    datasets = series.map((s, k) => ({
      label: String(s),
      data: labels.map(l => { const m = rows.find(row => row[xi] === l && row[si] === s); return m ? m[ys[0]] : null; }),
      backgroundColor: PALETTE[k % PALETTE.length], borderColor: PALETTE[k % PALETTE.length],
      tension: .25, pointRadius: 2, spanGaps: true,
    }));
  } else {
    labels = rows.map(row => row[xi]);
    datasets = ys.map((yi, k) => ({
      label: cols[yi], data: rows.map(row => row[yi]),
      backgroundColor: chart.type === "pie" ? PALETTE : PALETTE[k % PALETTE.length],
      borderColor: chart.type === "pie" ? "transparent" : PALETTE[k % PALETTE.length],
      tension: .25, pointRadius: 2,
    }));
  }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const grid = dark ? "#2f343c" : "#e5e7eb", tick = dark ? "#9aa1ab" : "#5d6470";
  new Chart(canvas, {
    type: chart.type === "pie" ? "doughnut" : chart.type,
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: datasets.length > 1 || chart.type === "pie", labels: { color: tick, boxWidth: 12 } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${fmtNum(c.parsed.y ?? c.parsed)}` } },
      },
      scales: chart.type === "pie" ? {} : {
        x: { grid: { display: false }, ticks: { color: tick, maxRotation: 45, autoSkip: true } },
        y: { grid: { color: grid }, ticks: { color: tick, callback: v => fmtNum(v) }, beginAtZero: true },
      },
    },
  });
}

function renderTrace(r) {
  const chips = [];
  if (r.examples?.length) chips.push(`${r.examples.length} memory hit${r.examples.length > 1 ? "s" : ""}`);
  if (r.plan) chips.push(`cost ${Math.round(r.plan.total_cost).toLocaleString()} timerons`);
  if (r.attempts?.length > 1) chips.push(`${r.attempts.length} attempts`);
  if (r.grain && r.grain.status !== "n/a") chips.push(`grain ${r.grain_caught?.length && r.grain.status !== "fail" ? "fixed" : r.grain.status}`);
  if (r.social) chips.push(`${r.social.total_matches} posts`);
  chips.push(`${r.elapsed_ms} ms`);
  if (r.audit_id) chips.push(`audit #${r.audit_id}`);

  const steps = [];
  if (r.reasoning) steps.push(`<div class="trace-step"><h4>Reasoning</h4>${esc(r.reasoning)}</div>`);
  steps.push(`<div class="trace-step"><h4>1 · Db2 vector memory (VECTOR_DISTANCE, COSINE)</h4>${
    r.examples?.length ? r.examples.map(e => `<div class="mem-hit"><span>${esc(e.question)} <span class="badge ${e.source}">${e.source}</span></span><b>${(e.similarity * 100).toFixed(1)}%</b></div>`).join("")
      : `<span class="hint">No similar verified queries yet.</span>`}</div>`);
  if (r.attempts?.length) {
    steps.push(`<div class="trace-step"><h4>2 · Generated SQL, guardrails &amp; self-correction</h4>${
      r.attempts.map(a => `<div class="attempt">Attempt ${a.attempt}: <span class="stage">${esc(a.stage)}</span>${a.cost != null ? ` · cost ${Math.round(a.cost).toLocaleString()}` : ""}${a.rows != null ? ` · ${a.rows} rows` : ""}${a.error ? `<br><span class="mono">${esc(a.error)}</span>` : ""}</div>`).join("")}</div>`);
  }
  if (r.sql) steps.push(`<div class="trace-step"><h4>Final SQL${r.used_memory_sql ? " (reused from memory)" : ""}</h4><pre class="sql">${highlightSql(r.sql)}</pre>${
    r.notes?.length ? `<p class="hint">${r.notes.map(esc).join(" · ")}</p>` : ""}</div>`);
  if (r.plan) {
    steps.push(`<div class="trace-step"><h4>3 · Db2 EXPLAIN access plan</h4>
      <p class="hint">Estimated cost ${r.plan.total_cost.toLocaleString()} timerons · est. ${r.plan.est_rows ?? "?"} rows
      ${r.plan.indexes_used.length ? ` · indexes: ${r.plan.indexes_used.map(esc).join(", ")}` : ""}</p>
      <div class="ops">${r.plan.operators.map(o => `<span class="${esc(o.type)}" title="cost ${o.cost}">#${o.id} ${esc(o.type)}${o.object ? " " + esc(o.object.split(".").pop()) : ""}</span>`).join("")}</div></div>`);
  }
  if (r.grain && r.grain.status !== "n/a") {
    steps.push(`<div class="trace-step"><h4>4 · Db2 grain check (control queries as read-only user)</h4>${
      r.grain.checks?.length ? `<div class="table-wrap"><table><thead><tr><th>aggregate</th><th>grain table</th><th>joined rows</th><th>source rows</th><th>query total</th><th>exact total</th><th>result</th></tr></thead><tbody>${
        r.grain.checks.map(c => `<tr><td class="mono">${esc(c.aggregate)}</td><td>${esc(c.grain_table)}</td>
          <td class="num">${c.joined_rows?.toLocaleString() ?? "–"}</td><td class="num">${c.source_rows?.toLocaleString() ?? "–"}</td>
          <td class="num">${fmtNum(c.naive_value)}</td><td class="num">${fmtNum(c.exact_value)}</td>
          <td><span class="badge ${c.status === "pass" ? "OK" : c.status === "fail" ? "ERROR" : ""}">${esc(c.status)}</span></td></tr>`).join("")}</tbody></table></div>`
        : `<span class="hint">${esc(r.grain.reason || "")}</span>`}${
      r.grain_caught?.length ? `<p class="hint">Earlier draft rejected: ${r.grain_caught.map(c => `${esc(c.aggregate)} ${c.factor}× inflated`).join(", ")}</p>` : ""}</div>`);
  }
  if (r.social) {
    steps.push(`<div class="trace-step"><h4>5 · Social evidence (hybrid Db2 query)</h4>
      <p class="hint">Semantic query “${esc(r.social.query)}” ranked by <code>VECTOR_DISTANCE(e.EMBEDDING, ?, COSINE)</code> over
      ANALYST.SOCIAL_EMBEDDINGS joined to RETAIL.SOCIAL_POSTS / PRODUCTS / STORES${
      Object.keys(r.social.filters || {}).length ? ", filtered by " + Object.entries(r.social.filters).map(([k, v]) => `${k}=${esc(v)}`).join(", ") : ""}.</p></div>`);
  }
  return `<details class="trace"><summary>How Db2 answered this ${chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")}</summary>
    <div class="trace-body">${steps.join("")}</div></details>`;
}

function downloadCsv(r) {
  const q = v => v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  const csv = [r.columns.map(q).join(","), ...r.rows.map(row => row.map(q).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = (r.title || "result").replace(/[^\w-]+/g, "_") + ".csv";
  a.click();
}

// ---------------------------------------------------------------- boot
const ta = $("#question");
ta.addEventListener("input", () => autosize(ta));
ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#composer").addEventListener("submit", e => { e.preventDefault(); const q = ta.value; ta.value = ""; autosize(ta); ask(q); });
initTabs();
loadHealth();
loadStats();
