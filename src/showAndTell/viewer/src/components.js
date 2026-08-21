// components.js — shared render helpers.
// Defines: assetUrl(), taskTitle(), band(), fmtScore(), scorePill(),
//          verdictChip(), emptyState().
// Uses: html.

// Repo-relative asset path -> a URL that works both with index.html opened as
// a file (relative to src/showAndTell/viewer/) and served by serve.py
// (site-absolute).
function assetUrl(path) {
  return location.protocol === "file:" ? "../../../" + path : "/" + path;
}

function taskTitle(task) {
  return task.title || task.name.replaceAll("_", "-").split("-")
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

function band(score) {
  if (score == null) return null;
  return score >= 0.8 ? "pass" : score >= 0.5 ? "partial" : "fail";
}

function fmtScore(score) {
  return score == null ? "—" : String(Number(score.toFixed(3)));
}

function scorePill(score, { title = "", best = false } = {}) {
  const b = band(score);
  if (!b) return html`<span class="pill-empty" title="${title}">—</span>`;
  const pct = Math.round(Math.max(0, Math.min(1, score)) * 100);
  return html`<span class="pill band-${b}" title="${title}">
    <span class="pill-n">${fmtScore(score)}${
      best ? html`<span class="best-dot" title="best score on this task"></span>` : ""
    }</span>
    <span class="pill-bar"><i style="width:${pct}%"></i></span>
  </span>`;
}

function verdictChip(pq) {
  if (!pq) return html`<span class="verdict verdict-none">—</span>`;
  if (pq.type === "multiple_choice" || pq.type === "closed") {
    return pq.ok
      ? html`<span class="verdict band-pass" title="correct">✓</span>`
      : html`<span class="verdict band-fail" title="wrong">✗</span>`;
  }
  const b = band(pq.score);
  return html`<span class="verdict ${b ? "band-" + b : "verdict-none"}" title="rubric score">${fmtScore(pq.score)}</span>`;
}

function emptyState(message, hint) {
  return html`<div class="empty"><strong>${message}</strong>${
    hint ? html`<div class="empty-hint">${hint}</div>` : ""
  }</div>`;
}
