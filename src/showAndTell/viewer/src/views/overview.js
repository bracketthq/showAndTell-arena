// views/overview.js — stat tiles + task × product matrix (sorting: Task 6).
// Defines: renderOverview(), matrixHeader(), matrixRows(), matrixCells(), legend(), cycleSort(), sortedTasks().
// Uses: DATA, PRODUCTS, html, applicationGroups, band, fmtScore, scorePill, state, setState.

function legend() {
  return html`<div class="legend">
    <span><i style="background:var(--pass)"></i> ≥ 0.80</span>
    <span><i style="background:var(--partial)"></i> 0.50–0.79</span>
    <span><i style="background:var(--fail)"></i> &lt; 0.50</span>
    <span><i style="background:var(--line-strong)"></i> not run</span>
  </div>`;
}

function cycleSort(productId) {
  const s = state.sort;
  if (!s || s.product !== productId) setState({ sort: { product: productId, dir: "desc" } });
  else if (s.dir === "desc") setState({ sort: { product: productId, dir: "asc" } });
  else setState({ sort: null });
}

function sortedTasks() {
  const { product, dir } = state.sort;
  const score = (t) => {
    if (product === "__tci") return t.complexity ? t.complexity.tci : null;
    const r = t.results[product];
    return r && r.score != null ? r.score : null;
  };
  return [...DATA.tasks].sort((a, b) => {
    const sa = score(a);
    const sb = score(b);
    if (sa == null && sb == null) return taskTitle(a).localeCompare(taskTitle(b));
    if (sa == null) return 1;  // not-run sinks in both directions
    if (sb == null) return -1;
    return dir === "desc" ? sb - sa : sa - sb;
  });
}

function tciCell(t) {
  const c = t.complexity;
  if (!c) return html`<td class="cell cell-empty">·</td>`;
  const d = c.dims;
  return html`<td class="cell cell-tci"><span class="tci-pill tier-${c.tier}"
    title="tier ${c.tier} · rule ${d.rule} · evidence ${d.evidence} · plan ${d.plan} · precision ${d.precision} · inference ${d.inference} · signal ${d.signal}">${c.tci.toFixed(1)}</span></td>`;
}

function matrixCells(t) {
  const run = PRODUCTS.filter((p) => t.results[p.id]);
  const best = run.length >= 2 ? Math.max(...run.map((p) => t.results[p.id].score ?? 0)) : null;
  return PRODUCTS.map((p) => {
    const r = t.results[p.id];
    if (!r) return html`<td class="cell cell-empty">·</td>`;
    return html`<td class="cell">${scorePill(r.score, {
      title: `multiple choice ${r.multiple_choice_correct}/${r.multiple_choice_total}`,
      best: best != null && (r.score ?? 0) === best,
    })}</td>`;
  });
}

function matrixHeader() {
  const tci = state.sort && state.sort.product === "__tci";
  const tciArrow = !tci ? "" : state.sort.dir === "desc" ? "▾" : "▴";
  return html`<thead><tr><th>task</th>
    <th class="p-h ${tci ? "sorted" : ""}">
      <button class="sort-btn" data-action="sort" data-product="__tci"
        title="Sort by task complexity">TCI<span class="sort-arrow">${tciArrow}</span></button>
    </th>${PRODUCTS.map((p) => {
    const active = state.sort && state.sort.product === p.id;
    const arrow = !active ? "" : state.sort.dir === "desc" ? "▾" : "▴";
    return html`<th class="p-h ${active ? "sorted" : ""}">
      <button class="sort-btn" data-action="sort" data-product="${p.id}"
        title="Sort by ${p.label} score">${p.label}<span class="sort-arrow">${arrow}</span></button>
    </th>`;
  })}</tr></thead>`;
}

function matrixRows() {
  if (state.sort) {
    return sortedTasks().map((t) => html`<tr class="task-row" data-action="open-task" data-task="${t.name}">
      <td class="tn" title="Task ID: ${t.name}">${taskTitle(t)}<span class="app-inline">${t.primaryApplication}${t.dataset ? " · dataset" : ""}</span></td>${tciCell(t)}${matrixCells(t)}</tr>`);
  }
  const rows = [];
  for (const [app, ts] of applicationGroups(DATA.tasks)) {
    rows.push(html`<tr class="grouprow"><td colspan="${2 + PRODUCTS.length}">${app}</td></tr>`);
    for (const t of ts) {
      rows.push(html`<tr class="task-row" data-action="open-task" data-task="${t.name}">
        <td class="tn" title="Task ID: ${t.name}">${taskTitle(t)}</td>${tciCell(t)}${matrixCells(t)}</tr>`);
    }
  }
  return rows;
}

function renderOverview() {
  const tasks = DATA.tasks;
  const nQuestions = tasks.reduce((a, t) => a + t.questions.length, 0);
  const generatedDate = new Date(DATA.generatedAt);
  const generatedLabel = Number.isNaN(generatedDate.valueOf())
    ? DATA.generatedAt
    : generatedDate.toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
  const productTiles = PRODUCTS.map((p) => {
    const runs = tasks.map((t) => t.results[p.id]).filter(Boolean);
    const mean = runs.length ? runs.reduce((a, r) => a + (r.score || 0), 0) / runs.length : null;
    const b = band(mean);
    return html`<div class="tile tile-product">
      <div class="tile-value ${b ? "band-" + b : ""}">${fmtScore(mean)}</div>
      <div class="tile-label"><b>${p.label}</b> mean · ${runs.length}/${tasks.length} run</div>
    </div>`;
  });
  return html`
    <h1 class="page-title">Overview</h1>
    <p class="page-sub">Every task's learn-then-quiz result, by product. Updated ${generatedLabel}.${DATA.dataset ? ` Dataset ${DATA.dataset.repo} @ ${DATA.dataset.revision} · ${DATA.dataset.count} published task${DATA.dataset.count === 1 ? "" : "s"}.` : ""}</p>
    <div class="tiles">
      <div class="tile"><div class="tile-value">${tasks.length}</div>
        <div class="tile-label">tasks across <b>${applicationGroups(tasks).length}</b> applications</div></div>
      <div class="tile"><div class="tile-value">${nQuestions}</div><div class="tile-label">quiz questions</div></div>
      ${productTiles}
    </div>
    <div class="card matrix-card">
      <div class="matrix-toolbar">
        <div><h2>Results matrix</h2><p>Compare task complexity and product performance.</p></div>
        ${legend()}
      </div>
      <div class="table-scroll">
      <table class="matrix">
        ${matrixHeader()}
        <tbody>${matrixRows()}</tbody>
      </table></div>
    </div>`;
}
