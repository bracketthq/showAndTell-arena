// api.js — the one authenticated JSON fetch behind every serve-mode feature.
// Defines: apiFetch(). Uses: nothing.

// Every feature config block ({enabled, endpoint, token, …}) authenticates
// with the same edit token and answers JSON. Failures throw an Error whose
// `payload` keeps the structured fields (e.g. host_busy details).
async function apiFetch(config, url, options = {}, label = "Request") {
  const headers = new Headers(options.headers || {});
  headers.set("X-ShowAndTell-Edit-Token", config.token);
  if (options.body !== undefined && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  const response = await fetch(url, {...options, headers});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `${label} failed (${response.status})`);
    error.payload = payload;
    throw error;
  }
  return payload;
}
