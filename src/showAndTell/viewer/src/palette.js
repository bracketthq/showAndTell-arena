// palette.js — ⌘K quick-open overlay. Manages its own DOM; module-local
// selection state, no store round-trip (typing must not re-render the app).
// Defines: initPalette(), openPalette(), closePalette(), fuzzyScore().
// Uses: DATA, html, openTask.

let paletteSelection = 0;
let paletteMatches = [];

function fuzzyScore(query, name) {
  const q = query.toLowerCase();
  const n = name.toLowerCase();
  let qi = 0;
  let prev = -2;
  let score = 0;
  for (let i = 0; i < n.length && qi < q.length; i++) {
    if (n[i] !== q[qi]) continue;
    score += prev === i - 1 ? 3 : 1; // consecutive runs beat scattered hits
    prev = i;
    qi++;
  }
  if (qi < q.length) return null; // not all query chars found in order
  return score - n.length * 0.01; // shorter names win ties
}

function paletteEl(id) {
  return document.getElementById(id);
}

function renderPaletteList() {
  const q = paletteEl("palette-input").value.trim();
  const available = allTasks();
  paletteMatches = q
    ? available
        .map((t) => [Math.max(
          fuzzyScore(q, taskTitle(t)) ?? -Infinity,
          fuzzyScore(q, t.name) ?? -Infinity,
        ), t])
        .filter(([s]) => Number.isFinite(s))
        .sort((a, b) => b[0] - a[0])
        .map(([, t]) => t)
        .slice(0, 12)
    : available.slice(0, 12);
  paletteSelection = Math.max(0, Math.min(paletteSelection, paletteMatches.length - 1));
  const items = paletteMatches.length
    ? paletteMatches.map((t, i) => html`<li class="${i === paletteSelection ? "sel" : ""}" data-name="${t.name}">
        <span class="pal-name">${taskTitle(t)}</span><span class="pal-app">${t.primaryApplication}</span>
      </li>`)
    : [html`<li class="pal-none">No matching task.</li>`];
  paletteEl("palette-list").innerHTML = html`${items}`.s;
}

function openPalette() {
  paletteSelection = 0;
  paletteEl("palette-input").value = "";
  paletteEl("palette").hidden = false;
  renderPaletteList();
  paletteEl("palette-input").focus();
}

function closePalette() {
  paletteEl("palette").hidden = true;
}

function pickPalette(name) {
  closePalette();
  openTask(name);
}

function initPalette() {
  const input = paletteEl("palette-input");
  input.addEventListener("input", () => {
    paletteSelection = 0;
    renderPaletteList();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = paletteMatches.length;
      if (n) paletteSelection = (paletteSelection + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
      renderPaletteList();
    } else if (e.key === "Enter") {
      if (paletteMatches[paletteSelection]) pickPalette(paletteMatches[paletteSelection].name);
    } else if (e.key === "Escape") {
      closePalette();
    }
  });
  paletteEl("palette").addEventListener("click", (e) => {
    if (e.target === paletteEl("palette")) {
      closePalette(); // backdrop click
      return;
    }
    const li = e.target.closest("li[data-name]");
    if (li) pickPalette(li.dataset.name);
  });
}
