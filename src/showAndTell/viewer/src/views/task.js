// views/task.js — task page: header, tab bar, delegates to tab renderers.
// Defines: renderTask(), TAB_LABELS, tabCount(), TAB_BODIES.
// Uses: state, TASKS_BY_NAME, TABS, html, renderDemo, renderQuiz,
//       renderResults.

const TAB_LABELS = { demo: "Demonstration", quiz: "Quiz", results: "Results" };

function tabCount(t, tab) {
  if (tab === "quiz") return t.questions.length;
  return Object.values(t.results).reduce((n, r) => n + ((r.runs || []).length || 1), 0);
}

const TAB_BODIES = {
  demo: (t) => renderDemo(t),
  quiz: (t) => renderQuiz(t),
  results: (t) => renderResults(t),
};

function renderTask() {
  const t = TASKS_BY_NAME[state.task];
  const blocked = t.status && t.status.state === "blocked";
  const c = t.complexity;
  const dims = c ? [
    ["rule", c.dims.rule], ["evidence", c.dims.evidence], ["plan", c.dims.plan],
    ["precision", c.dims.precision], ["inference", c.dims.inference], ["signal", c.dims.signal],
  ] : [];
  return html`
    <div class="task-head">
      <div class="task-breadcrumb"><span>${t.primaryApplication}</span><b>/</b><span>Task detail</span></div>
      <div class="task-title-row">
        <h1 class="page-title">${taskTitle(t)}</h1>
        <span class="chip chip-application">${t.primaryApplication}</span>
        ${t.draft ? html`<span class="chip chip-draft">captured task</span>` : ""}
        ${c ? html`<span class="tci-pill tier-${c.tier}" title="Task Complexity Index, tier ${c.tier}">TCI ${c.tci.toFixed(1)}</span>` : ""}
        ${blocked ? html`<span class="chip chip-blocked">blocked</span>` : ""}
      </div>
      <p class="task-id">Task ID <code>${t.name}</code></p>
      <p class="task-summary">${t.summary}</p>
      ${c ? html`<p class="tci-strip">${dims.map(([k, v]) =>
        html`<span class="tci-dim">${k} <b>${v.toFixed(1)}</b></span>`)}</p>` : ""}
      ${t.draft ? html`<div class="capture-help">Your recording is saved. Use <b>Add to tasks</b> to make it an ordinary local test.</div>` : ""}
      ${!t.questions.length ? html`<div class="task-question-warning" role="note">
        <b>No quiz questions.</b> Add a question in the Quiz tab before running
        if you want this task to produce a graded result.
      </div>` : ""}
      ${renderTaskRunControls(t)}
    </div>
    <div class="tabs" role="tablist">
      ${TABS.map((tab) => html`
        <button class="tab ${state.tab === tab ? "active" : ""}" role="tab"
          aria-selected="${state.tab === tab}" data-action="tab" data-tab="${tab}">
          ${TAB_LABELS[tab]}${tab === "demo" ? "" :
            html`<span class="tab-count">${tabCount(t, tab)}</span>`}
        </button>`)}
    </div>
    <section id="tab-body">${TAB_BODIES[state.tab](t)}</section>`;
}
