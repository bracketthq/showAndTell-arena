// views/results.js — per-product score cards + cross-product comparison table.
// Defines: renderResults(), resultCard(), answerBlock(), comparisonRows(),
//          recordingPanel().
// Uses: PRODUCTS, state, html, band, fmtScore, verdictChip,
//       emptyState, questionBody (views/quiz.js).

function resultCard(p, r) {
  if (!r) {
    return html`<div class="rescard rescard-none">
      <div class="rescard-p">${p.label}</div>
      <div class="rescard-big">not run</div>
    </div>`;
  }
  const b = band(r.score);
  return html`<div class="rescard ${b ? "band-" + b : ""}">
    <div class="rescard-p">${p.label}</div>
    <div class="rescard-big ${r.score == null ? "ungraded" : ""}">${
      r.score == null ? "not graded" : fmtScore(r.score)}</div>
    ${r.run_count > 1 ? html`<div class="rescard-average">${
      r.score == null ? `${r.run_count} saved replays`
      : r.graded_run_count === r.run_count ? `average of ${r.run_count} graded runs`
      : `${r.graded_run_count} graded of ${r.run_count} saved replays`}</div>` : ""}
    ${r.score == null ? html`<div class="rescard-meta">${r.grading
      ? "complete replay · regrading required"
      : `${r.status || "complete"} trial · comprehension unavailable`}</div>`
      : html`<div class="rescard-meta">multiple choice ${r.multiple_choice_correct}/${r.multiple_choice_total}</div>`}
    ${r.cached_at ? html`<div class="rescard-when">${r.cached_at.replace("T", " ")}</div>` : ""}
    ${r.recording ? html`<button class="rec-toggle ${state.openRecording === p.id ? "on" : ""}"
      data-action="toggle-recording" data-p="${p.id}">▶ recording</button>` : ""}
  </div>`;
}

let regradeRuntime = null;
let resultDeletionRuntime = null;

function regradeConfig() {
  return DATA.regrades || {enabled: false};
}

function regradeStatus(grading) {
  const active = regradeRuntime;
  if (!active || active.run !== grading.run) return "";
  const label = active.status === "running" ? "Regrading saved response…"
    : active.status === "complete" ? "Regrading complete. Refreshing results…"
    : "Regrading failed.";
  return html`<div class="grading-regrade-status status-${active.status}" role="status">
    <b>${label}</b>${active.error ? html`<p>${active.error}</p>` : ""}
    ${active.status === "failed" && active.log ? html`<pre><code>${active.log}</code></pre>` : ""}
  </div>`;
}

function gradingNotice(p, r) {
  if (!r?.grading) return "";
  const grading = r.grading;
  const judge = [grading.backend, grading.model].filter(Boolean).join(" / ");
  const canConfigure = Boolean(DATA.settings?.enabled);
  const canRegrade = Boolean(regradeConfig().enabled && grading.task && grading.run);
  const running = regradeRuntime?.status === "running";
  const credential = grading.credential
    ? html`Add <code>${grading.credential}</code> in Settings, save it, then regrade this saved response.`
    : "Configure the grader in Settings, save it, then regrade this saved response.";
  return html`<div class="card grading-notice" role="status">
    <div class="grading-notice-title">${p.label} replay completed — regrading required</div>
    <p>The replay and its artifacts were saved. The grading failure did not count as a product failure.</p>
    <p class="grading-notice-error">${grading.error}</p>
    ${judge ? html`<div class="grading-notice-judge">Configured judge: ${judge}</div>` : ""}
    ${canConfigure ? html`
      <div class="grading-notice-label">${credential}</div>
      <div class="grading-notice-actions">
        <button type="button" data-action="open-settings">Open Settings</button>
        ${canRegrade ? html`<button type="button" class="primary" data-action="regrade"
          data-task="${grading.task}" data-run="${grading.run}" data-product="${p.id}"
          ${running ? "disabled" : ""}>${running ? "Regrading…" : "Regrade"}</button>` : ""}
      </div>
      ${regradeStatus(grading)}
      <details class="grading-command"><summary>Command-line alternative</summary>
        <pre><code>${grading.recovery_command}</code></pre>
      </details>` : html`
      <div class="grading-notice-label">Configure the judge and regrade this saved response from the repository root:</div>
      <pre><code>${grading.recovery_command}</code></pre>`}
    <p class="grading-notice-foot">Regrading does not replay the product.</p>
  </div>`;
}

async function pollRegrade() {
  const current = regradeRuntime;
  if (!current || current.status !== "running") return;
  try {
    regradeRuntime = await apiFetch(regradeConfig(), current.status_endpoint, {
      method: "POST", body: "{}",
    }, "Regrade");
    render();
    if (regradeRuntime.status === "running") setTimeout(pollRegrade, 1200);
    else if (regradeRuntime.status === "complete") setTimeout(() => location.reload(), 700);
  } catch (err) {
    regradeRuntime = {...current, status: "failed", error: String(err.message || err)};
    render();
  }
}

async function startRegrade(el) {
  if (!regradeConfig().enabled || regradeRuntime?.status === "running") return;
  regradeRuntime = {task: el.dataset.task, run: el.dataset.run,
                    product: el.dataset.product, status: "running"};
  render();
  try {
    regradeRuntime = await apiFetch(regradeConfig(), regradeConfig().endpoint, {
      method: "POST",
      body: JSON.stringify({task: el.dataset.task, run: el.dataset.run}),
    }, "Regrade");
    render();
    setTimeout(pollRegrade, 400);
  } catch (err) {
    regradeRuntime = {task: el.dataset.task, run: el.dataset.run,
      product: el.dataset.product, status: "failed", error: String(err.message || err)};
    render();
  }
}

function answerBlock(p, pq) {
  return html`<div class="answer-block">
    <span class="answer-prod">${p.label}</span>
    ${verdictChip(pq)}
    <span class="answer-text ${pq && pq.answer ? "" : "none"}">${
      pq ? pq.answer || "no answer recorded" : "not run on this task"
    }</span>
  </div>`;
}

function comparisonRows(t, byProd) {
  const rows = [];
  for (const q of t.questions) {
    const open = state.expanded.has(q.id);
    const lookup = (p) => (byProd[p.id] ? byProd[p.id].get(q.id) : null);
    rows.push(html`<tr class="q-row ${open ? "open" : ""}" data-action="toggle-row" data-q="${q.id}">
      <td>
        <span class="q-id">${q.id}</span>
        <span class="q-text">${q.question}</span>
      </td>
      ${PRODUCTS.map((p) => html`<td class="v-cell">${verdictChip(lookup(p))}</td>`)}
    </tr>`);
    if (open) {
      rows.push(html`<tr class="answer-row"><td colspan="${1 + PRODUCTS.length}">
        <div class="answers">
          <div class="expected">${questionBody(q)}</div>
          ${PRODUCTS.map((p) => answerBlock(p, lookup(p)))}
        </div>
      </td></tr>`);
    }
  }
  return rows;
}

function recordingPanel(p, r) {
  return html`<div class="card rec-panel">
    <div class="rec-title">${p.label} — screen recording
      <a class="rec-file" href="${r.recording}" target="_blank">open the file directly</a>
    </div>
    <video controls preload="metadata" src="${r.recording}"></video>
    <div class="rec-fallback">This browser can’t play the format — use the file link above.</div>
  </div>`;
}

function runHistory(t) {
  const rows = [];
  for (const p of PRODUCTS) {
    const result = t.results[p.id];
    for (const [i, r] of ((result && result.runs) || []).entries()) {
      const deleting = resultDeletionRuntime === `${r.deletion?.kind}:${r.deletion?.id}`;
      rows.push(html`<tr>
        <td>${p.label}${i === 0 ? html` <span class="latest-run">latest</span>` : ""}</td>
        <td class="run-when">${r.cached_at ? r.cached_at.replace("T", " ") : "—"}</td>
        <td class="run-score">${fmtScore(r.score)}</td>
        <td>${r.multiple_choice_correct == null || r.multiple_choice_total == null
          ? "—" : `${r.multiple_choice_correct}/${r.multiple_choice_total}`}</td>
        <td>${runArtifacts(r)}</td>
        ${DATA.resultDeletion?.enabled ? html`<td class="run-delete-cell">
          ${r.deletion ? html`<button type="button" class="run-delete"
            data-action="delete-result" data-kind="${r.deletion.kind}"
            data-id="${r.deletion.id}" data-task="${r.deletion.task || ""}"
            data-product="${p.label}" data-when="${r.cached_at || ""}"
            ${deleting ? "disabled" : ""}
            aria-label="Delete ${p.label} result from ${r.cached_at || "this run"}">${
              deleting ? "Deleting…" : "Delete"}</button>` : ""}
        </td>` : ""}
      </tr>`);
    }
  }
  if (!rows.length) return "";
  return html`<div class="card run-history">
    <div class="run-history-title">Run history <span>${rows.length} total</span></div>
    <div class="table-scroll"><table>
      <thead><tr><th>product</th><th>run time</th><th>score</th><th>multiple choice</th><th>artifact</th>${
        DATA.resultDeletion?.enabled ? html`<th>action</th>` : ""}</tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </div>`;
}

async function deleteResult(el) {
  if (!DATA.resultDeletion?.enabled || resultDeletionRuntime) return;
  const when = el.dataset.when ? el.dataset.when.replace("T", " ") : "this saved run";
  if (!window.confirm(
      `Delete the ${el.dataset.product} result from ${when}?\n\n` +
      "This permanently deletes the result and its saved artifacts.")) return;
  const key = `${el.dataset.kind}:${el.dataset.id}`;
  resultDeletionRuntime = key;
  render();
  try {
    await apiFetch(DATA.resultDeletion, DATA.resultDeletion.endpoint, {
      method: "POST",
      body: JSON.stringify({kind: el.dataset.kind, id: el.dataset.id,
                            ...(el.dataset.task ? {task: el.dataset.task} : {})}),
    }, "Delete result");
    location.reload();
  } catch (err) {
    resultDeletionRuntime = null;
    render();
    window.alert(err.message || String(err));
  }
}

function runArtifacts(r) {
  const links = [];
  if (r.recording) links.push({label: "recording", url: r.recording});
  links.push(...(r.artifacts || []));
  if (!links.length) return "—";
  return html`<span class="run-artifacts">${links.map((item) =>
    html`<a href="${item.url}" target="_blank" rel="noopener">${item.label}</a>`)}</span>`;
}

function renderResults(t) {
  const cards = html`<div class="result-cards">${PRODUCTS.map((p) => resultCard(p, t.results[p.id]))}</div>`;
  const ran = PRODUCTS.filter((p) => t.results[p.id]);
  const gradingNotices = ran.map((p) => gradingNotice(p, t.results[p.id]));
  const openRec = ran.find(
    (p) => p.id === state.openRecording && t.results[p.id].recording);
  const recPanel = openRec ? recordingPanel(openRec, t.results[openRec.id]) : "";
  if (!ran.length) {
    return html`${cards}${t.draft
      ? emptyState("No replay trial has been saved for this draft yet.",
          "Use one of the Replay buttons above; the Results tab refreshes when the replay completes.")
      : emptyState("No product has been run on this task yet.",
          html`Run <code>uv run showAndTell &lt;adapter&gt; --task tasks/${t.name}</code>, then regenerate the viewer.`)}`;
  }
  const byProd = {};
  for (const p of ran) {
    byProd[p.id] = new Map((t.results[p.id].per_question || []).map((pq) => [pq.id, pq]));
  }
  return html`${cards}${gradingNotices}${recPanel}
    <div class="card result-comparison">
      <div class="result-comparison-title">Question results
        <span>Click a question to compare the expected answer with each product’s answer.</span>
      </div>
      <table class="compare">
        <thead><tr>
          <th>question</th>
          ${PRODUCTS.map((p) => html`<th class="p-col">${p.label}</th>`)}
        </tr></thead>
        <tbody>${comparisonRows(t, byProd)}</tbody>
      </table>
    </div>
    ${runHistory(t)}`;
}
