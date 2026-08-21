// executions.js — task-centric fixture-lease dashboard inside the viewer.

let executionsRuntime = {rows: [], loading: false, error: "", timer: null};

function executionsConfig() {
  return DATA.executions || {enabled: false};
}

function executionsApi(url, options = {}) {
  return apiFetch(executionsConfig(), url, options, "Execution request");
}

function scheduleExecutionsPoll(delay = 1500) {
  clearTimeout(executionsRuntime.timer);
  if (state.page !== "executions" || !executionsConfig().enabled) return;
  executionsRuntime.timer = setTimeout(refreshExecutions, delay);
}

async function refreshExecutions() {
  if (state.page !== "executions") return;
  executionsRuntime.loading = true;
  try {
    const payload = await executionsApi(executionsConfig().endpoint);
    executionsRuntime.rows = payload.executions || [];
    executionsRuntime.error = "";
  } catch (err) {
    executionsRuntime.error = String(err.message || err);
  } finally {
    executionsRuntime.loading = false;
    render();
    scheduleExecutionsPoll();
  }
}

function renderExecutions() {
  const rows = executionsRuntime.rows;
  return html`<section class="executions-view">
    <div class="view-heading">
      <div><h1>Executions</h1><p>Live fixture leases, grouped by task.</p></div>
      <span class="execution-count">${rows.length} running</span>
    </div>
    ${executionsRuntime.error ? html`<div class="empty"><strong>Could not load executions.</strong>
      <div class="empty-hint">${executionsRuntime.error}</div></div>` : ""}
    ${!executionsRuntime.error && !rows.length ? html`<div class="empty">
      <strong>No tasks are running.</strong>
      <div class="empty-hint">New captures, replays, and product runs appear here automatically.</div>
    </div>` : html`<div class="execution-table-wrap"><table class="execution-table">
      <thead><tr><th>Task</th><th>Type</th><th>Application assignments</th>
        <th>Started</th><th aria-label="Action"></th></tr></thead>
      <tbody>${rows.map((row) => html`<tr>
        <td><b>${row.task}</b><span class="execution-state">${row.state}</span></td>
        <td>${row.product}<span class="execution-kind">${row.kind}</span></td>
        <td><div class="application-assignments">${row.applications.map((application) => html`
          <span class="application-assignment"><b>${application.name}</b>
            <span>${application.instance}</span></span>`)}</div></td>
        <td><time>${row.started_at ? new Date(row.started_at).toLocaleString() : "Unknown"}</time></td>
        <td><button type="button" class="force-release"
          data-action="force-release" data-execution="${row.id}">Force release</button></td>
      </tr>`)}</tbody>
    </table></div>`}
  </section>`;
}

async function forceReleaseExecution(executionId) {
  const row = executionsRuntime.rows.find((item) => item.id === executionId);
  if (!row) return;
  const assignments = row.applications.map((item) =>
    `${item.name} (${item.instance})`).join(", ");
  if (!confirm(`Force release ${row.task}?\n\n${assignments}\n\nThis revokes fixture ownership so the application replicas can be reused. It does not stop the runner process.`)) return;
  try {
    await executionsApi(`${executionsConfig().endpoint}/${executionId}/force-release`, {
      method: "POST", body: "{}",
    });
    executionsRuntime.rows = executionsRuntime.rows.filter((item) => item.id !== executionId);
    render();
    scheduleExecutionsPoll(250);
  } catch (err) {
    executionsRuntime.error = String(err.message || err);
    render();
  }
}
