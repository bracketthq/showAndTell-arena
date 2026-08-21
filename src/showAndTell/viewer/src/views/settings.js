// views/settings.js — serve-mode fixture-host and judge configuration.

let settingsRuntime = {
  data: null, loading: false, error: "", timer: null,
  hostStatus: null, judgeStatus: null, saveStatus: null, dirty: false,
};

const SETTINGS_API_KEYS = {
  "automatic": "ANTHROPIC_API_KEY",
  "anthropic-api": "ANTHROPIC_API_KEY",
  "openai-api": "OPENAI_API_KEY",
  "gemini-api": "GEMINI_API_KEY",
};

function settingsConfig() {
  return DATA.settings || {enabled: false};
}

function settingsApi(suffix = "", options = {}) {
  return apiFetch(
    settingsConfig(), settingsConfig().endpoint + suffix, options,
    "Settings request");
}

function scheduleSettingsLoad(delay = 0) {
  clearTimeout(settingsRuntime.timer);
  if (state.page !== "settings" || !settingsConfig().enabled) return;
  settingsRuntime.timer = setTimeout(loadSettings, delay);
}

async function loadSettings() {
  if (state.page !== "settings" || settingsRuntime.loading) return;
  if (settingsRuntime.dirty) {
    // never clobber in-progress edits with a background refresh
    scheduleSettingsLoad(5000);
    return;
  }
  settingsRuntime.loading = true;
  try {
    settingsRuntime.data = await settingsApi();
    settingsRuntime.error = "";
  } catch (err) {
    settingsRuntime.error = String(err.message || err);
  } finally {
    settingsRuntime.loading = false;
    render();
  }
}

function sourceBadge(source) {
  return html`<span class="settings-source source-${source.replaceAll(" ", "-")}">${source}</span>`;
}

function settingsStatus(status) {
  if (!status) return "";
  return html`<div class="settings-status settings-status-${status.kind}" role="status">${status.text}</div>`;
}

function renderSettings() {
  if (!settingsConfig().enabled)
    return emptyState("Settings are unavailable.", "Start the local viewer to configure this machine.");
  if (settingsRuntime.error)
    return emptyState("Could not load settings.", settingsRuntime.error);
  if (!settingsRuntime.data)
    return html`<section class="settings-view"><div class="empty"><strong>Loading settings…</strong></div></section>`;

  const data = settingsRuntime.data;
  const host = data.fixture_host;
  const judge = data.judge;
  const credential = judge.credential;
  const hostLocal = host.mode === "local";
  return html`<section class="settings-view">
    <div class="view-heading settings-heading">
      <div><h1>Settings</h1><p>Configuration used by new captures, replays, and benchmark runs on this machine.</p></div>
      <span class="settings-scope">local viewer only</span>
    </div>
    ${data.warning ? html`<div class="settings-warning">${data.warning}</div>` : ""}
    <div class="settings-note">Saved values go to <code>${data.settings_path}</code>. The file is gitignored and owner-readable only, but it contains any credentials entered below.</div>

    <div class="settings-card">
      <div class="settings-card-head">
        <div><h2>Fixture host</h2><p>Run application fixtures automatically on this machine or use a shared remote host.</p></div>
        ${sourceBadge(host.mode_source)}
      </div>
      <div class="settings-grid">
        <label><span>Location</span>
          <select id="settings-host-mode" ${host.locked ? "disabled" : ""}>
            <option value="local" ${host.mode === "local" ? "selected" : ""}>Local · automatic</option>
            <option value="remote" ${host.mode === "remote" ? "selected" : ""}>Remote host</option>
          </select>
          <small>${host.locked ? "Controlled by SHOWANDTELL_FIXTURE_HOST_URL." : "Local mode starts a loopback fixture agent when first needed."}</small>
        </label>
        <label><span>Remote URL ${sourceBadge(host.url_source)}</span>
          <input id="settings-host-url" value="${host.url}" placeholder="https://fixtures.example:8091"
            data-locked="${host.locked}" ${host.locked || hostLocal ? "disabled" : ""}>
        </label>
        <label class="settings-secret"><span>Access token</span>
          <input id="settings-host-token" type="password" autocomplete="new-password"
            placeholder="${host.token.configured ? "Saved in .env · enter to replace" : "Saved locally in .env"}"
            data-locked="${host.token.locked}" ${host.token.locked || hostLocal ? "disabled" : ""}>
          <small>${host.token.configured ? `Credential source: ${host.token.source}.` : "No token configured."}</small>
        </label>
      </div>
      <div class="settings-actions">
        <button type="button" data-action="test-host-settings">Test current host</button>
        ${host.token.source === "dotenv" ? html`<button type="button" data-action="clear-host-token">Remove saved token</button>` : ""}
      </div>
      ${settingsStatus(settingsRuntime.hostStatus)}
    </div>

    <div class="settings-card">
      <div class="settings-card-head">
        <div><h2>Grader</h2><p>Select the rubric judge. Any model or backend override is recorded as unofficial.</p></div>
        <span class="settings-canonical ${judge.canonical ? "is-canonical" : "is-override"}">${judge.canonical ? "canonical" : "unofficial override"}</span>
      </div>
      <div class="settings-grid">
        <label><span>Backend ${sourceBadge(judge.backend_source)}</span>
          <select id="settings-judge-backend" ${judge.backend_locked ? "disabled" : ""}>
            ${judge.backends.map((backend) => html`<option value="${backend}" ${backend === judge.backend ? "selected" : ""}>${backend}</option>`)}
          </select>
        </label>
        <label><span>Model ${sourceBadge(judge.model_source)}</span>
          <input id="settings-judge-model" value="${judge.model}" ${judge.model_locked ? "disabled" : ""}>
        </label>
        <label class="settings-secret"><span id="settings-key-label">${credential.variable || "Credential"}</span>
          <input id="settings-judge-key" type="password" autocomplete="new-password"
            placeholder="${credential.required ? (credential.configured ? "Saved in .env · enter to replace" : "Saved locally in .env") : "Not required for CLI backends"}"
            data-locked="${credential.locked}" ${credential.locked || !credential.required ? "disabled" : ""}>
          <small id="settings-key-help">Credential source: ${credential.source}.</small>
        </label>
      </div>
      <div class="settings-actions">
        <button type="button" data-action="test-judge-settings">Test current grader</button>
        <button type="button" data-action="reset-judge-settings" ${judge.backend_locked && judge.model_locked ? "disabled" : ""}>Reset to canonical</button>
        ${credential.source === "dotenv" ? html`<button type="button" data-action="clear-judge-key">Remove saved key</button>` : ""}
      </div>
      ${settingsStatus(settingsRuntime.judgeStatus)}
    </div>

    <div class="settings-savebar">
      <div><b>Local .env configuration</b><span>${data.settings_path}</span></div>
      <button type="button" data-action="save-settings">Save settings</button>
    </div>
    ${settingsStatus(settingsRuntime.saveStatus)}
  </section>`;
}

function collectSettings() {
  const current = settingsRuntime.data;
  const hostMode = document.getElementById("settings-host-mode").value;
  const hostToken = document.getElementById("settings-host-token").value;
  const apiKey = document.getElementById("settings-judge-key").value;
  const payload = {judge: {
    backend: document.getElementById("settings-judge-backend").value,
    model: document.getElementById("settings-judge-model").value,
  }};
  if (!current.fixture_host.locked) {
    payload.fixture_host = {
      mode: hostMode,
      url: document.getElementById("settings-host-url").value,
    };
  }
  if (hostToken) {
    payload.fixture_host = payload.fixture_host || {};
    payload.fixture_host.token = hostToken;
  }
  if (apiKey) payload.judge.api_key = apiKey;
  return payload;
}

async function saveSettings() {
  // read the form before any render(): a render rebuilds the form from the
  // last-loaded server state and would discard the edits being saved
  const payload = collectSettings();
  settingsRuntime.saveStatus = {kind: "working", text: "Saving settings…"};
  render();
  try {
    settingsRuntime.data = await settingsApi("", {
      method: "POST", body: JSON.stringify(payload),
    });
    settingsRuntime.dirty = false;
    settingsRuntime.saveStatus = {
      kind: "success", text: "Settings saved. New work will use this configuration.",
    };
  } catch (err) {
    settingsRuntime.saveStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

async function testHostSettings() {
  settingsRuntime.hostStatus = {kind: "working", text: "Testing the current fixture host…"};
  render();
  try {
    const result = await settingsApi("/test-host", {method: "POST", body: "{}"});
    settingsRuntime.hostStatus = {
      kind: "success",
      text: `Connected in ${result.mode} mode · ${result.applications.length} applications available.`,
    };
  } catch (err) {
    settingsRuntime.hostStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

async function testJudgeSettings() {
  settingsRuntime.judgeStatus = {kind: "working", text: "Checking authentication and making a small model call…"};
  render();
  try {
    const result = await settingsApi("/test-judge", {method: "POST", body: "{}"});
    const preflight = result.preflight || {};
    settingsRuntime.judgeStatus = {
      kind: "success",
      text: `Grader ready · ${preflight.backend || "stub"}${preflight.model ? ` / ${preflight.model}` : ""}.`,
    };
  } catch (err) {
    settingsRuntime.judgeStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

async function resetJudgeSettings() {
  try {
    settingsRuntime.data = await settingsApi("", {
      method: "POST", body: JSON.stringify({judge: {canonical: true}}),
    });
    settingsRuntime.dirty = false;
    settingsRuntime.saveStatus = {kind: "success", text: "Restored the repository judge configuration."};
  } catch (err) {
    settingsRuntime.saveStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

async function clearHostToken() {
  try {
    settingsRuntime.data = await settingsApi("", {
      method: "POST", body: JSON.stringify({fixture_host: {clear_token: true}}),
    });
    settingsRuntime.saveStatus = {kind: "success", text: "Saved fixture token removed from .env."};
  } catch (err) {
    settingsRuntime.saveStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

async function clearJudgeKey() {
  try {
    settingsRuntime.data = await settingsApi("", {
      method: "POST", body: JSON.stringify({judge: {clear_api_key: true}}),
    });
    settingsRuntime.saveStatus = {kind: "success", text: "Saved judge key removed from .env."};
  } catch (err) {
    settingsRuntime.saveStatus = {kind: "error", text: String(err.message || err)};
  }
  render();
}

function updateCredentialHint(backend) {
  const name = SETTINGS_API_KEYS[backend];
  const label = document.getElementById("settings-key-label");
  const input = document.getElementById("settings-judge-key");
  const help = document.getElementById("settings-key-help");
  if (label) label.textContent = name || "Credential";
  if (input && input.dataset.locked !== "true") input.disabled = !name;
  if (input) input.placeholder = name ? "Saved locally in .env" : "Not required for CLI backends";
  if (help) help.textContent = name ? `${name} will be stored in the gitignored .env file.` : "The CLI handles its own authentication.";
}
