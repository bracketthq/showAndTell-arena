// html.js — auto-escaping HTML templating.
// Defines: Raw, raw(), escapeHtml(), html``.
//
// Every interpolated value is HTML-escaped: strings/numbers become safe text,
// arrays are joined (each element processed), nested html`` results and
// raw(trusted) pass through, null/undefined/booleans render as "" (so
// `${cond && html`…`}` works). ALWAYS quote attribute interpolations:
//   html`<td title="${t}">`   OK (escaping covers the quotes)
//   html`<td title=${t}>`     WRONG (spaces would split the attribute)

class Raw {
  constructor(s) {
    this.s = s;
  }
}

function raw(s) {
  return new Raw(String(s));
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

function toHtml(v) {
  if (v == null || typeof v === "boolean") return "";
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(toHtml).join("");
  return escapeHtml(v);
}

function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += toHtml(values[i]) + strings[i + 1];
  return new Raw(out);
}
