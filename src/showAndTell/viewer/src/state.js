// state.js — data globals + the single UI store.
// Defines: DATA, PRODUCTS, TASKS_BY_NAME, initData(), allTasks(), TABS,
//          state, subscribe(), setState(), applicationGroups(),
//          filteredGroups().
// Uses: nothing (loaded right after html.js).

let DATA = { generatedAt: "", products: [], applicationOrder: [], tasks: [], drafts: [], dataset: null };
let PRODUCTS = [];
let TASKS_BY_NAME = {};

function allTasks() {
  return [...DATA.tasks, ...DATA.drafts];
}

function initData(parsed) {
  DATA = parsed;
  PRODUCTS = DATA.products;
  DATA.drafts = DATA.drafts || [];
  TASKS_BY_NAME = Object.fromEntries(allTasks().map((t) => [t.name, t]));
}

const TABS = ["demo", "quiz", "results"];

const state = {
  page: "overview",    // overview | task | executions | settings
  task: null,          // open task name, or null = overview
  tab: "demo",         // demo | quiz | results
  filter: "",          // sidebar filter text
  sort: null,          // {product, dir: "desc"|"asc"} or null = application grouping
  expanded: new Set(), // expanded question rows on the results comparison
  openRecording: null, // product id whose recording player is open (results tab)
  openQuizQuestion: null, // expanded question id in the normal Quiz view
  questionEditor: null, // {task, questions, status, activeQuestion, dirty, validation}; serve-mode only
};

const listeners = [];

function subscribe(fn) {
  listeners.push(fn);
}

function setState(patch) {
  Object.assign(state, patch);
  for (const fn of listeners) fn();
}

function applicationGroups(tasks) {
  const groups = new Map();
  for (const t of tasks) {
    const group = t.draft ? "captured tasks" : t.primaryApplication;
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(t);
  }
  // Within an application, order easiest -> hardest (TCI), titles breaking ties.
  const tci = (t) => (t.complexity ? t.complexity.tci : Infinity);
  for (const ts of groups.values())
    ts.sort((a, b) => tci(a) - tci(b) || taskTitle(a).localeCompare(taskTitle(b)));
  const order = DATA.applicationOrder;
  return [...groups.entries()].sort(([a], [b]) => {
    if (a === "captured tasks") return -1;
    if (b === "captured tasks") return 1;
    const ia = order.indexOf(a);
    const ib = order.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
}

function filteredGroups() {
  const f = state.filter.trim().toLowerCase();
  const match = (t) =>
    !f || taskTitle(t).toLowerCase().includes(f) || t.name.toLowerCase().includes(f)
      || (t.summary || "").toLowerCase().includes(f);
  return applicationGroups(allTasks().filter(match));
}
