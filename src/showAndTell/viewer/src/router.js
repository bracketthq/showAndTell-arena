// router.js — hash <-> state (#/task/<name>/<tab>).
// Defines: openTask(), openOverview(), openExecutions(), openSettings(),
//          syncHash(), initRouter().
// Uses: state, setState, TASKS_BY_NAME, TABS.

let suppressHashEvent = false;

function syncHash() {
  const h = state.page === "executions" ? "#/executions"
    : state.page === "settings" ? "#/settings"
    : state.task ? `#/task/${encodeURIComponent(state.task)}/${state.tab}` : "#/";
  if (location.hash !== h) {
    suppressHashEvent = true; // our own write; applyHash would be redundant
    location.hash = h;
  }
}

function openTask(name, tab) {
  if (name !== state.task && !confirmDiscardQuestionChanges()) return;
  setState({
    page: "task",
    task: name,
    tab: tab || state.tab, // keep the current tab when hopping tasks (j/k compare flow)
    expanded: name === state.task ? state.expanded : new Set(),
    openRecording: name === state.task ? state.openRecording : null,
    openQuizQuestion: name === state.task ? state.openQuizQuestion : null,
    questionEditor: name === state.task ? state.questionEditor : null,
  });
  syncHash();
}

function openOverview() {
  if (!confirmDiscardQuestionChanges()) return;
  setState({ page: "overview", task: null, questionEditor: null });
  syncHash();
}

function openExecutions() {
  if (!confirmDiscardQuestionChanges()) return;
  setState({ page: "executions", task: null, questionEditor: null });
  syncHash();
  scheduleExecutionsPoll(0);
}

function openSettings() {
  if (!confirmDiscardQuestionChanges()) return;
  setState({ page: "settings", task: null, questionEditor: null });
  syncHash();
  scheduleSettingsLoad(0);
}

function applyHash() {
  if (!confirmDiscardQuestionChanges()) {
    syncHash();
    return;
  }
  if (location.hash === "#/executions") {
    setState({page: "executions", task: null, questionEditor: null});
    scheduleExecutionsPoll(0);
    return;
  }
  if (location.hash === "#/settings" && DATA.settings?.enabled) {
    setState({page: "settings", task: null, questionEditor: null});
    scheduleSettingsLoad(0);
    return;
  }
  const m = location.hash.match(/^#\/task\/([^/]+)(?:\/(\w+))?/);
  let name = null;
  if (m) {
    try {
      name = decodeURIComponent(m[1]);
    } catch {
      name = null; // malformed escape -> unknown route -> Overview
    }
  }
  if (name && TASKS_BY_NAME[name]) {
    setState({
      page: "task",
      task: name,
      tab: m[2] && TABS.includes(m[2]) ? m[2] : "demo",
      expanded: name === state.task ? state.expanded : new Set(),
      openRecording: name === state.task ? state.openRecording : null,
      openQuizQuestion: null,
      questionEditor: null,
    });
  } else {
    setState({ page: "overview", task: null, questionEditor: null }); // unknown/stale routes fall back to Overview
  }
}

function initRouter() {
  window.addEventListener("hashchange", () => {
    if (suppressHashEvent) {
      suppressHashEvent = false;
      return;
    }
    applyHash();
  });
  applyHash(); // first render happens here via setState -> subscribers
}
