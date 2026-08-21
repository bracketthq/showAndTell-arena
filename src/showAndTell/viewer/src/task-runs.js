// task-runs.js — live serve-mode launch/status controls for benchmark products.
// Defines: renderTaskRunControls(), startTaskRun(). Uses: DATA, html, render().

let taskRunRuntime = null;
let draftPromotionRuntime = null;
let taskRunOpenedGate = null;
let taskRunHearNarration = false;
let taskRunAudioPending = null;

const TASK_RUN_STAGE_PLANS = {
  "brackett-teach": {
    thresholds: [1, 3, 4, 5, 6],
    labels: ["Opening Brackett", "Starting task apps", "Starting Show and Tell",
             "Demonstration running", "Evaluating", "Evaluated"],
  },
  "claude-teach": {
    thresholds: [1, 2, 4, 6, 7],
    labels: ["Preparing browser", "Starting task apps", "Opening Claude Teach",
             "Demonstration running", "Evaluating", "Evaluated"],
  },
  "codex-record": {
    thresholds: [1, 3, 5, 6, 7],
    labels: ["Opening Codex", "Starting task apps", "Starting Record & Replay",
             "Demonstration running", "Evaluating", "Evaluated"],
  },
};

function taskRunConfig() {
  return DATA.taskRuns || {enabled: false, adapters: []};
}

function taskRunApi(url, options = {}) {
  return apiFetch(taskRunConfig(), url, options, "Run request");
}

function normalizeTaskRun(next, fallback = {}) {
  return {
    ...next,
    task: next.task || fallback.task,
    adapter: next.adapter || fallback.adapter,
    source: next.source || fallback.source || "task",
    label: next.label || fallback.label,
  };
}

function confirmQuestionlessRun(task) {
  const t = TASKS_BY_NAME[task];
  if (!t || t.questions.length) return true;
  return window.confirm(
    `${task} has no quiz questions. The workflow can still run, but it cannot `
    + "produce a graded result. Start anyway?"
  );
}

function taskRunProgress(run) {
  const plan = TASK_RUN_STAGE_PLANS[run.adapter] || {
    thresholds: [1, 2, 3, 4, 5],
    labels: ["Preparing", "Starting task apps", "Starting demonstration",
             "Demonstration running", "Evaluating", "Evaluated"],
  };
  const numbered = Array.from(String(run.log || "").matchAll(
    /(?:^|\n)\s*(\d+)\/(\d+)\s+([^\n]+)/g
  ));
  const latestStep = numbered.length ? Number(numbered[numbered.length - 1][1]) : 0;
  let current = 0;
  plan.thresholds.forEach((threshold, index) => {
    if (latestStep >= threshold) current = index;
  });
  if (run.status === "complete") current = plan.labels.length - 1;
  const currentLabel = plan.labels[current];
  let summary = currentLabel;
  if (run.status === "cancelling") summary = `Cancelling during ${currentLabel.toLowerCase()}…`;
  if (run.status === "cancelled") summary = `Cancelled during ${currentLabel.toLowerCase()}`;
  if (run.status === "failed") summary = `Failed during ${currentLabel.toLowerCase()}`;
  return {...plan, current, summary};
}

function renderTaskRunProgress(run) {
  const progress = taskRunProgress(run);
  return html`
    <div class="task-run-progress-head">
      <b>${progress.summary}</b>
      <span>Step ${progress.current + 1} of ${progress.labels.length}</span>
    </div>
    <ol class="task-run-progress" aria-label="Run progress">
      ${progress.labels.map((label, index) => {
        const complete = run.status === "complete" || index < progress.current;
        const current = index === progress.current && run.status !== "complete";
        const problem = current && ["failed", "cancelled"].includes(run.status);
        const stateClass = complete ? "is-complete"
          : current ? problem ? "is-current is-problem" : "is-current"
            : "is-pending";
        return html`<li class="task-run-stage ${stateClass}"
          aria-current="${current ? "step" : "false"}">
          <span class="task-run-stage-marker">${complete ? "✓" : index + 1}</span>
          <span>${label}</span>
        </li>`;
      })}
    </ol>`;
}

function renderTaskRunLog(run) {
  if (!run.log) return "";
  return html`<details class="task-run-details">
    <summary><span class="task-run-details-label">Technical details</span>
      <span class="task-run-details-meta">Run log</span></summary>
    <pre tabindex="0">${run.log}</pre>
  </details>`;
}

function renderNarrationToggle(disabled = false, running = false) {
  const title = running
    ? "Turn speaker narration on or off now; the task still receives virtual microphone audio"
    : "Also play narration through your speakers while it is sent to the task";
  return html`<button type="button" class="task-run-button task-run-audio-toggle"
    data-action="toggle-task-run-audio"
    title="${title}"
    aria-pressed="${taskRunHearNarration ? "true" : "false"}"
    ${disabled ? "disabled" : ""}>
    ${taskRunHearNarration ? "🔊 Hear narration: On" : "🔇 Hear narration: Off"}
  </button>`;
}

// Replace only the status/controls block while a run is polled: a full
// render() every 1.5s would recreate the demo <video> (interrupting playback)
// and re-stringify the whole task view each tick.
function updateTaskRunControls(taskName) {
  const container = document.querySelector(".task-run-controls");
  const t = TASKS_BY_NAME[taskName];
  if (!container || !t || state.page !== "task" || state.task !== taskName) {
    render();
    return;
  }
  const details = container.querySelector(".task-run-details");
  const log = details?.querySelector("pre");
  const detailsOpen = Boolean(details?.open);
  const logScroll = log?.scrollTop || 0;
  const logAtBottom = log ? log.scrollHeight - log.clientHeight - log.scrollTop < 16 : true;
  const parent = container.parentElement;
  container.outerHTML = html`${renderTaskRunControls(t)}`.s;
  const nextDetails = parent?.querySelector(".task-run-controls .task-run-details");
  if (detailsOpen && nextDetails) {
    nextDetails.open = true;
    const nextLog = nextDetails.querySelector("pre");
    if (nextLog) nextLog.scrollTop = logAtBottom ? nextLog.scrollHeight : logScroll;
  }
}

function renderTaskRunControls(t) {
  const config = taskRunConfig();
  if (!config.enabled) return "";
  const active = taskRunRuntime;
  const running = active && active.status === "running";
  const source = t.draft ? "draft" : "task";
  const forTask = active && active.task === t.name && active.source === source;
  const promotion = draftPromotionConfig();
  const promoting = t.draft && draftPromotionRuntime?.slug === t.name
    && draftPromotionRuntime.status === "running";
  const promotionFailed = t.draft && draftPromotionRuntime?.slug === t.name
    && draftPromotionRuntime.status === "failed";
  const actionOrder = {"brackett-teach": 0, "codex-record": 1, "claude-teach": 2};
  const adapters = [...config.adapters].sort((a, b) =>
    (actionOrder[a.id] ?? 99) - (actionOrder[b.id] ?? 99));
  const buttons = adapters.map((adapter) => html`
    <button type="button" class="task-run-button task-run-${adapter.id}"
      data-action="run-task" data-task="${t.name}" data-adapter="${adapter.id}"
      data-source="${source}"
      ${(running && forTask) || promoting ? "disabled" : ""}>Run with ${adapter.label}</button>`);
  return html`
    <div class="task-run-controls" aria-label="Run task">
      <div class="task-run-buttons">${buttons}
        ${renderNarrationToggle(
          promoting || (running && !active.audio_endpoint) || taskRunAudioPending !== null,
          running)}
        ${t.draft && promotion.enabled ? html`
          <button type="button" class="task-run-button task-promote-button"
            data-action="promote-draft" data-task="${t.name}"
            ${running || promoting ? "disabled" : ""}>
            ${promoting ? "Adding…" : "Add to tasks"}</button>` : ""}
      </div>
      ${promotionFailed ? html`<div class="task-run-status status-failed" role="status">
        <p>${draftPromotionRuntime.error}</p>
      </div>` : ""}
      ${forTask ? html`
        <div class="task-run-status status-${active.status}" role="status">
          <div><span class="task-run-dot"></span><b>${active.label}</b>
            <span>${active.status === "running" ? "running…" : active.status}</span></div>
          ${renderTaskRunProgress(active)}
          ${active.gate ? html`
            <div class="task-run-gate" role="alert">
              <p><b>✋ ${active.gate.problem}</b></p>
              <p>${linkifyGateInstructions(active.gate, active.id)}</p>
              <div class="task-run-buttons">
                ${active.gate.open_browser ? html`
                  <button type="button" data-action="gate-open"
                    data-run="${active.id}">${active.gate.open_label || "Open managed Chrome"} again</button>` : ""}
                <button type="button" data-action="gate-continue"
                  data-run="${active.id}">Continue</button>
                <button type="button" data-action="gate-cancel"
                  data-run="${active.id}">Cancel run</button>
              </div>
            </div>` : ""}
          ${active.status === "running" && active.id && !active.gate ? html`
            <div class="task-run-buttons task-run-cancel-row">
              <button type="button" class="task-run-button task-run-cancel"
                data-action="cancel-task-run" data-run="${active.id}">Cancel run</button>
            </div>` : ""}
          ${active.error ? html`<p>${active.error}</p>` : ""}
          ${active.busy_run ? html`
            <p><b>${active.busy_run.label}</b> is currently running
              ${active.busy_run.task}.</p>
            <div class="task-run-buttons">
              <button type="button" class="task-run-button task-run-replace"
                data-action="replace-task-run" data-task="${t.name}"
                data-adapter="${active.adapter}" data-source="${source}">
                Stop current run and start this one</button>
            </div>` : ""}
          ${renderTaskRunLog(active)}
        </div>` : running ? html`
        <div class="task-run-status status-running" role="status">
          <div><span class="task-run-dot"></span><b>${active.label}</b>
            <span>${taskRunProgress(active).summary} for ${active.task}</span></div>
          ${renderTaskRunProgress(active)}
          <div class="task-run-buttons task-run-cancel-row">
            <button type="button" class="task-run-button task-run-cancel"
              data-action="cancel-task-run" data-run="${active.id}">Cancel run</button>
          </div>
          ${renderTaskRunLog(active)}
        </div>` : ""}
    </div>`;
}

function draftPromotionConfig() {
  return DATA.draftPromotion || {enabled: false};
}


// The shared promotion choreography; success lands on the promoted task's
// demo tab.
async function promoteDraftFlow(slug, onSuccess) {
  const config = draftPromotionConfig();
  const result = await apiFetch(config, config.endpoint, {
    method: "POST", body: JSON.stringify({slug}),
  }, "Promotion");
  if (onSuccess) onSuccess(result);
  location.hash = `#/task/${slug}/demo`;
  location.reload();
}

async function promoteDraft(task) {
  if (!draftPromotionConfig().enabled
      || draftPromotionRuntime?.status === "running") return;
  draftPromotionRuntime = {slug: task, status: "running"};
  render();
  try {
    await promoteDraftFlow(task);
  } catch (err) {
    draftPromotionRuntime = {slug: task, status: "failed",
                             error: String(err.message || err)};
    render();
  }
}


async function taskRunPoll() {
  const current = taskRunRuntime;
  if (!current || current.status !== "running") return;
  try {
    const next = await taskRunApi(current.status_endpoint, {
      method: "POST", body: "{}",
    });
    // A cancel request can finish while this status request is in flight.
    // Never let the stale poll overwrite the terminal cancelled state.
    if (taskRunRuntime !== current || taskRunRuntime.status !== "running") return;
    taskRunRuntime = normalizeTaskRun(next, current);
    taskRunHearNarration = taskRunRuntime.hear_narration;
    updateTaskRunControls(current.task);
    await autoOpenTaskRunGate(taskRunRuntime);
    if (taskRunRuntime.status === "running") {
      setTimeout(taskRunPoll, 1500);
    } else if (taskRunRuntime.status === "complete") {
      setTimeout(() => location.reload(), 900);
    }
  } catch (err) {
    taskRunRuntime = {...current, status: "failed", error: String(err.message || err)};
    updateTaskRunControls(current.task);
  }
}

async function startTaskRun(task, adapter, source = "task", replace = false) {
  const config = taskRunConfig();
  if (!config.enabled) return;
  if (taskRunRuntime?.status === "running" && taskRunRuntime.task === task
      && taskRunRuntime.source === source) return;
  const questionsConfirmed = replace || confirmQuestionlessRun(task);
  if (!questionsConfirmed) return;
  const label = config.adapters.find((item) => item.id === adapter)?.label || adapter;
  const pending = {task, adapter, source, label, status: "running", log: "Starting…"};
  taskRunRuntime = pending;
  render();
  try {
    taskRunRuntime = normalizeTaskRun(await taskRunApi(config.endpoint, {
      method: "POST", body: JSON.stringify({
        task, adapter, source, hear_narration: taskRunHearNarration,
        ...(replace ? {replace: true} : {}),
      }),
    }), pending);
    taskRunHearNarration = taskRunRuntime.hear_narration;
    render();
    setTimeout(taskRunPoll, 500);
  } catch (err) {
    const busyRun = err.payload?.code === "task_run_busy"
      ? err.payload.active_run : null;
    taskRunRuntime = {task, adapter, source, label, status: "failed",
                      error: String(err.message || err), log: "",
                      busy_run: busyRun, questions_confirmed: questionsConfirmed};
    render();
  }
}

async function toggleTaskRunAudio() {
  const active = taskRunRuntime?.status === "running" ? taskRunRuntime : null;
  if (active) {
    if (!active.audio_endpoint || taskRunAudioPending !== null) return;
    const hearNarration = !active.hear_narration;
    taskRunAudioPending = {id: active.id, hear_narration: hearNarration};
    taskRunHearNarration = hearNarration;
    taskRunRuntime = {...active, hear_narration: hearNarration, error: ""};
    updateTaskRunControls(active.task);
    try {
      const updated = await taskRunApi(active.audio_endpoint, {
        method: "POST", body: JSON.stringify({hear_narration: hearNarration}),
      });
      taskRunHearNarration = updated.hear_narration;
      taskRunRuntime = normalizeTaskRun(updated, active);
    } catch (err) {
      taskRunHearNarration = active.hear_narration;
      taskRunRuntime = {...active, error: String(err.message || err)};
    } finally {
      taskRunAudioPending = null;
      updateTaskRunControls(active.task);
      setTimeout(taskRunPoll, 0);
    }
    return;
  }
  taskRunHearNarration = !taskRunHearNarration;
  render();
}

function replaceTaskRun(task, adapter, source = "task") {
  const active = taskRunRuntime?.busy_run;
  if (!taskRunRuntime?.questions_confirmed && !confirmQuestionlessRun(task)) return;
  const description = active
    ? `${active.label} run for ${active.task}` : "current task run";
  if (!window.confirm(`Stop the ${description} and start this task?`)) return;
  startTaskRun(task, adapter, source, true);
}

async function cancelTaskRun(runId) {
  if (!window.confirm("Cancel the running task?")) return;
  const current = taskRunRuntime;
  if (!current || current.id !== runId || current.status !== "running") return;
  taskRunRuntime = {...current, status: "cancelling", error: ""};
  updateTaskRunControls(current.task);
  try {
    taskRunRuntime = normalizeTaskRun(await taskRunApi(
      current.cancel_endpoint || `/api/task-runs/${runId}/cancel`, {
      method: "POST", body: "{}",
      }), current);
  } catch (err) {
    taskRunRuntime = {...current, error: String(err.message || err)};
  }
  render();
}

async function answerTaskRunGate(runId, action) {
  try {
    const status = await taskRunApi(`/api/task-runs/${runId}/gate`, {
      method: "POST", body: JSON.stringify({action}),
    });
    taskRunRuntime = normalizeTaskRun(status, taskRunRuntime);
  } catch (err) {
    if (taskRunRuntime) taskRunRuntime.error = String(err.message || err);
  }
  render();
}

async function openTaskRunGate(runId) {
  try {
    taskRunRuntime = normalizeTaskRun(await taskRunApi(`/api/task-runs/${runId}/gate`, {
      method: "POST", body: JSON.stringify({action: "open"}),
    }), taskRunRuntime);
  } catch (err) {
    if (taskRunRuntime) taskRunRuntime.error = String(err.message || err);
  }
  if (taskRunRuntime?.task) updateTaskRunControls(taskRunRuntime.task);
}

async function autoOpenTaskRunGate(runtime) {
  const gate = runtime?.gate;
  if (!gate?.open_browser) return;
  const key = `${runtime.id}:${gate.problem}:${gate.open_url || "focus"}`;
  if (taskRunOpenedGate === key) return;
  taskRunOpenedGate = key;
  await openTaskRunGate(runtime.id);
}

// Readiness URLs use the gate action so they always open in this run's
// managed Chrome profile, never in the viewer's/default browser.
function linkifyGateInstructions(gate, runId) {
  const text = gate?.instructions || "";
  return String(text).split(/(https?:\/\/[^\s`'"<>]+)/g).map((part) =>
    /^https?:\/\//.test(part)
      ? gate.open_url ? html`<button type="button" class="task-run-gate-link"
          data-action="gate-open" data-run="${runId}">${part}</button>`
        : part
      : part);
}
