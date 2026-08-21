// app.js — boot: parse the data island, wire chrome + delegated actions,
// run the render loop.
// Defines: render(), renderSidebar(), ACTIONS, initActions(),
//          initTheme(), toggleTheme(), initChrome(), boot().
// Uses: html, initData, state, subscribe, setState, filteredGroups, PRODUCTS,
//       band, fmtScore, renderOverview, renderTask, openTask, openOverview,
//       syncHash, initRouter, cycleSort, initPalette, initKeyboard.

function parseData() {
  return JSON.parse(document.getElementById("showAndTell-data").textContent);
}

function renderFatal(err) {
  document.getElementById("view").innerHTML = html`<div class="empty">
    <strong>Could not read the embedded data.</strong>
    <div class="empty-hint">Re-run <code>uv run python -m showAndTell.viewer.generate</code> and reload. (${String(err)})</div>
  </div>`.s;
}

function renderSidebar() {
  const groups = filteredGroups();
  const items = [
    html`<div class="nav-section-label">Workspace</div>`,
    html`<button class="nav-overview nav-dashboard ${state.page === "overview" ? "active" : ""}" data-action="open-overview"><span class="nav-icon" aria-hidden="true">⌁</span><span>Overview</span></button>`,
    DATA.executions?.enabled ? html`<button class="nav-overview ${state.page === "executions" ? "active" : ""}"
      data-action="open-executions"><span class="nav-icon nav-icon-live" aria-hidden="true"></span><span>Executions</span></button>` : "",
    DATA.settings?.enabled ? html`<button class="nav-overview ${state.page === "settings" ? "active" : ""}"
      data-action="open-settings"><span class="nav-icon" aria-hidden="true">⚙</span><span>Settings</span></button>` : "",
    html`<div class="nav-section-label nav-tasks-label"><span>Task library</span><span>${DATA.tasks.length}</span></div>`,
  ];
  for (const [fx, tasks] of groups) {
    items.push(html`<div class="nav-group">${fx}</div>`);
    for (const t of tasks) {
      const pips = t.draft ? html`<span class="nav-draft">NEW</span>` : PRODUCTS.map((p) => {
        const r = t.results[p.id];
        return html`<span class="nav-pip ${r ? "band-" + (band(r.score) || "fail") : ""}"
          title="${p.label}: ${r ? fmtScore(r.score) : "not run"}"></span>`;
      });
      items.push(html`<button class="nav-task ${state.page === "task" && state.task === t.name ? "active" : ""}"
        data-action="open-task" data-task="${t.name}" title="Task ID: ${t.name}">
        <span class="nav-name">${taskTitle(t)}</span><span class="nav-pips">${pips}</span>
      </button>`);
    }
  }
  if (!groups.length) items.push(html`<div class="nav-empty">No tasks match “${state.filter}”.</div>`);
  document.getElementById("side").innerHTML = html`${items}`.s;
}

function render() {
  renderSidebar();
  document.getElementById("view").innerHTML = (
    state.page === "executions" ? renderExecutions()
      : state.page === "settings" ? renderSettings()
      : state.task ? renderTask() : renderOverview()).s;
}

const ACTIONS = {
  "open-task": (el) => {
    openTask(el.dataset.task);
    document.getElementById("side").classList.remove("open"); // close mobile drawer
  },
  "open-overview": () => {
    openOverview();
    document.getElementById("side").classList.remove("open");
  },
  "open-executions": () => {
    openExecutions();
    document.getElementById("side").classList.remove("open");
  },
  "open-settings": () => {
    openSettings();
    document.getElementById("side").classList.remove("open");
  },
  "tab": (el) => {
    if (el.dataset.tab !== state.tab && !confirmDiscardQuestionChanges()) return;
    setState({ tab: el.dataset.tab, questionEditor: null });
    syncHash();
  },
  "run-task": (el) => startTaskRun(
    el.dataset.task, el.dataset.adapter, el.dataset.source || "task"),
  "toggle-task-run-audio": () => toggleTaskRunAudio(),
  "cancel-task-run": (el) => cancelTaskRun(el.dataset.run),
  "replace-task-run": (el) => replaceTaskRun(
    el.dataset.task, el.dataset.adapter, el.dataset.source || "task"),
  "gate-open": (el) => openTaskRunGate(el.dataset.run),
  "gate-continue": (el) => answerTaskRunGate(el.dataset.run, "continue"),
  "gate-cancel": (el) => answerTaskRunGate(el.dataset.run, "cancel"),
  "regrade": (el) => startRegrade(el),
  "delete-result": (el) => deleteResult(el),
  "force-release": (el) => forceReleaseExecution(el.dataset.execution),
  "save-settings": () => saveSettings(),
  "test-host-settings": () => testHostSettings(),
  "test-judge-settings": () => testJudgeSettings(),
  "reset-judge-settings": () => resetJudgeSettings(),
  "clear-host-token": () => clearHostToken(),
  "clear-judge-key": () => clearJudgeKey(),
  "promote-draft": (el) => promoteDraft(el.dataset.task),
  "toggle-row": (el) => {
    const expanded = new Set(state.expanded);
    if (expanded.has(el.dataset.q)) expanded.delete(el.dataset.q);
    else expanded.add(el.dataset.q);
    setState({ expanded });
  },
  "toggle-recording": (el) => {
    const p = el.dataset.p;
    setState({ openRecording: state.openRecording === p ? null : p });
  },
  "sort": (el) => cycleSort(el.dataset.product),
  "toggle-quiz-question": (el) => setState({
    openQuizQuestion: state.openQuizQuestion === el.dataset.question
      ? null : el.dataset.question,
  }),
  "edit-questions": () => {
    const t = TASKS_BY_NAME[state.task];
    openQuestionEditor(t);
  },
  "cancel-question-edit": () => {
    if (confirmDiscardQuestionChanges()) setState({questionEditor: null});
  },
  "open-question-card": (el) => openQuestionCard(Number(el.dataset.index)),
  "add-question": () => {
    try {
      const questions = collectQuestionDraft();
      questions.push(newQuestionDraft(questions));
      updateQuestionDraft(questions, null, {activeQuestion: questions.length - 1});
    } catch (err) { questionDraftError(err); }
  },
  "remove-question": (el) => {
    try {
      const questions = collectQuestionDraft();
      const index = Number(el.dataset.index);
      const active = state.questionEditor.activeQuestion;
      questions.splice(index, 1);
      const nextActive = !questions.length ? -1
        : index < active ? active - 1
        : index === active ? Math.min(index, questions.length - 1) : active;
      updateQuestionDraft(questions, null, {activeQuestion: nextActive});
    } catch (err) { questionDraftError(err); }
  },
  "move-question-up": (el) => moveQuestionDraft(Number(el.dataset.index), -1),
  "move-question-down": (el) => moveQuestionDraft(Number(el.dataset.index), 1),
  "add-question-option": (el) =>
    updateQuestionOptionDraft(Number(el.dataset.questionIndex)),
  "remove-question-option": (el) => updateQuestionOptionDraft(
    Number(el.dataset.questionIndex), Number(el.dataset.optionIndex)),
  "save-questions": () => saveQuestionDraft(),
};

function moveQuestionDraft(index, delta) {
  try {
    const questions = collectQuestionDraft();
    const target = index + delta;
    if (target < 0 || target >= questions.length) return;
    [questions[index], questions[target]] = [questions[target], questions[index]];
    const active = state.questionEditor.activeQuestion;
    const nextActive = active === index ? target : active === target ? index : active;
    updateQuestionDraft(questions, null, {activeQuestion: nextActive});
  } catch (err) { questionDraftError(err); }
}

function initActions() {
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const handler = ACTIONS[el.dataset.action];
    if (handler) handler(el);
  });
  // Media `error` events don't bubble — capture-phase listen on the document
  // to flag a broken recording without wiring a handler per <video>.
  document.addEventListener("error", (e) => {
    const v = e.target;
    if (v && v.tagName === "VIDEO" && v.closest(".rec-panel"))
      v.closest(".rec-panel").classList.add("rec-broken");
  }, true);
  document.addEventListener("change", (e) => {
    if (e.target.id === "settings-host-mode") {
      const local = e.target.value === "local";
      const url = document.getElementById("settings-host-url");
      const token = document.getElementById("settings-host-token");
      if (url) url.disabled = local || url.dataset.locked === "true";
      if (token) token.disabled = local || token.dataset.locked === "true";
      return;
    }
    if (e.target.id === "settings-judge-backend") {
      updateCredentialHint(e.target.value);
      return;
    }
    if (e.target.closest?.(".qedit[data-question-index]")) markQuestionEditorDirty(e.target);
    if (!e.target.matches("[data-question-type]")) return;
    const form = e.target.closest(".qedit");
    for (const section of form.querySelectorAll("[data-kind]")) {
      section.hidden = section.dataset.kind !== e.target.value;
    }
  });
  document.addEventListener("input", (e) => {
    if (e.target.closest?.(".qedit[data-question-index]")) markQuestionEditorDirty(e.target);
  });
  window.addEventListener("beforeunload", (e) => {
    if (!hasUnsavedQuestionChanges()) return;
    e.preventDefault();
    e.returnValue = "";
  });
}

const THEME_KEY = "showAndTell-theme";

function applyTheme(v) {
  document.documentElement.setAttribute("data-theme", v);
}

function initTheme() {
  try {
    applyTheme(localStorage.getItem(THEME_KEY) || "");
  } catch {
    /* localStorage can be unavailable on file:// — OS preference still applies */
  }
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const dark = cur ? cur === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* see above */
  }
}

function initChrome() {
  // Only the sidebar reads the filter — skip the full-app re-render so the
  // open task's video keeps playing while the user types.
  document.getElementById("filter").addEventListener("input", (e) => {
    state.filter = e.target.value;
    renderSidebar();
  });
  document.getElementById("menu-toggle").addEventListener("click", () =>
    document.getElementById("side").classList.toggle("open"));
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
}

function boot() {
  let parsed;
  try {
    parsed = parseData();
  } catch (err) {
    renderFatal(err);
    return;
  }
  initData(parsed);
  initTheme();
  initChrome();
  initActions();
  initTaskCapture();
  initPalette();
  initKeyboard();
  subscribe(render);
  initRouter(); // applyHash -> setState -> first render
  openPendingCaptureQuestionEditor();
  initIntroGuide();
}

boot();

// A background settings refresh must not clobber in-progress edits.
for (const kind of ["input", "change"]) {
  document.addEventListener(kind, (event) => {
    if (event.target.closest?.(".settings-view")) settingsRuntime.dirty = true;
  });
}
