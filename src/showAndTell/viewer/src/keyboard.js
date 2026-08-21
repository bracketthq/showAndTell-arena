// keyboard.js — global shortcuts: j/k next/prev task, 1–3 tabs, / filter,
// ⌘K/Ctrl+K palette, Esc blur. Ignores keys while typing in a field
// (except Esc, and ⌘K which works everywhere).
// Defines: initKeyboard(), moveTask().
// Uses: state, setState, syncHash, TABS, filteredGroups, openTask, openPalette.

function moveTask(delta) {
  const flat = filteredGroups().flatMap(([, tasks]) => tasks);
  if (!flat.length) return;
  if (!state.task) {
    if (delta > 0) openTask(flat[0].name);
    return;
  }
  const i = flat.findIndex((t) => t.name === state.task);
  const next = flat[i + delta]; // i === -1 (task filtered out): j lands on flat[0]
  if (next) openTask(next.name);
}

function initKeyboard() {
  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openPalette();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = document.activeElement ? document.activeElement.tagName : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      if (e.key === "Escape") document.activeElement.blur();
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      document.getElementById("filter").focus();
    } else if (e.key === "j") {
      moveTask(1);
    } else if (e.key === "k") {
      moveTask(-1);
    } else if (state.task && ["1", "2", "3"].includes(e.key)) {
      setState({ tab: TABS[Number(e.key) - 1] });
      syncHash();
    }
  });
}
