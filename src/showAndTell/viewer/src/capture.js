// capture.js — local-serve workflow recording and testcase-draft creation.
// Defines: initTaskCapture(). Uses: DATA, html.

let captureRuntime = null;
let captureAssetRuntime = null;
let capturePendingLaunch = null;

function captureConfig() {
  return DATA.taskCapture || {enabled: false, applications: []};
}

function captureSetError(message) {
  const el = document.getElementById("capture-error");
  el.textContent = message || "";
  el.hidden = !message;
}

function captureStage(name) {
  for (const stage of ["setup", "assets", "seed", "live", "done"])
    document.getElementById(`capture-${stage}`).hidden = stage !== name;
}

function captureRenderLoginGuidance(session) {
  const surfaces = session.surfaces || [];
  const results = new Map((session.login_results || []).map((row) => [row.id, row]));
  const grid = document.getElementById("capture-login-grid");
  document.getElementById("capture-copy-status").textContent = "";
  const statusText = {
    "signed-in": "Signed in automatically",
    failed: "Automatic sign-in failed — use these credentials",
    manual: "Sign in with these credentials",
  };
  grid.innerHTML = html`${surfaces.map((surface, index) => {
    const credentials = surface.credentials || {};
    const username = credentials.email || "";
    const password = credentials.password || "";
    const requiresLogin = Boolean(username || password);
    const result = results.get(surface.id) || {};
    const state = requiresLogin ? (result.status || "manual") : "not-required";
    return html`<article class="capture-login-card" data-login-state="${state}">
      <div class="capture-login-title">
        <b>${surface.label}</b>
        <span>${requiresLogin ? (statusText[state] || statusText.manual) : "No login required"}</span>
      </div>
      ${requiresLogin ? html`<dl>
        <div><dt>Username / email</dt><dd><code>${username}</code><button type="button" data-copy-login="${index}" data-copy-field="email">Copy</button></dd></div>
        <div><dt>Password</dt><dd><code>${password}</code><button type="button" data-copy-login="${index}" data-copy-field="password">Copy</button></dd></div>
      </dl>` : html`<p>Open and use this app directly in managed Chrome.</p>`}
    </article>`;
  })}`.s;
  grid.querySelectorAll("[data-copy-login]").forEach((button) => {
    button.addEventListener("click", async () => {
      const surface = surfaces[Number(button.dataset.copyLogin)] || {};
      const value = (surface.credentials || {})[button.dataset.copyField] || "";
      const copied = document.getElementById("capture-copy-status");
      try {
        await navigator.clipboard.writeText(value);
        copied.textContent = `${button.dataset.copyField === "password" ? "Password" : "Username"} copied for ${surface.label}.`;
      } catch {
        copied.textContent = "Copy was blocked by the browser. Select the value and copy it manually.";
      }
    });
  });
}

function captureOpen() {
  const config = captureConfig();
  if (!config.enabled) return;
  captureSetError("");
  captureRuntime = null;
  captureAssetRuntime = null;
  capturePendingLaunch = null;
  document.getElementById("capture-name").value = "";
  document.getElementById("capture-summary").value = "";
  document.querySelectorAll("[data-capture-app]").forEach((input) => {
    input.checked = false;
  });
  captureStage("setup");
  // A previous discard may have left these relabelled or disabled.
  for (const [id, label] of [["capture-discard", "Discard recording"],
                             ["capture-stop", "■ Stop & create testcase"]]) {
    const button = document.getElementById(id);
    button.disabled = false;
    button.textContent = label;
  }
  document.getElementById("task-capture").hidden = false;
  document.getElementById("capture-name").focus();
}

function captureClose() {
  if (captureAssetRuntime && ["queued", "running", "canceling"].includes(captureAssetRuntime.status)) {
    captureCancelAssets(true);
    return;
  }
  const runtime = captureRuntime;
  if (capturePendingLaunch?.requesting && !runtime) {
    captureCancelPendingLaunch();
    return;
  }
  if (runtime && !runtime.finished) {
    // Never leave the close control dead: route to whichever cancel the
    // current stage owns, so the applications are released server-side
    // instead of the session being abandoned.
    if (runtime.startedAt) captureDiscardRecording();
    else captureCancelSeed();
    return;
  }
  const refreshDrafts = Boolean(runtime?.finished && runtime?.draft);
  document.getElementById("task-capture").hidden = true;
  if (refreshDrafts) location.reload();
}

async function captureCancelPendingLaunch() {
  const pending = capturePendingLaunch;
  if (!pending || pending.cancelled) return;
  pending.cancelled = true;
  const button = document.getElementById("capture-start");
  button.disabled = true;
  button.textContent = "Canceling app startup…";
  try {
    await captureApi(
      `${captureConfig().endpoint}/pending/${pending.launchId}/cancel`,
      {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    document.getElementById("task-capture").hidden = true;
  } catch (error) {
    pending.cancelled = false;
    captureSetError(error.message || String(error));
  } finally {
    button.disabled = false;
    button.textContent = "Start clean apps";
  }
}

function captureSlug(value) {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function captureApi(url, options = {}) {
  return apiFetch(captureConfig(), url, options, "Capture request");
}

function captureAssetApi(path, options = {}) {
  return apiFetch(captureConfig(), path, options, "Application asset request");
}

function captureBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const rank = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / (1024 ** rank);
  return `${amount >= 10 || rank === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[rank]}`;
}

function captureRequiredSpace(value, estimated = false) {
  return `${estimated ? "~" : ""}${captureBytes(value)} required`;
}

function captureRenderAssetPreflight(preflight) {
  const downloads = preflight.downloads || [];
  const blockers = preflight.blockers || [];
  const rows = [...downloads, ...blockers];
  const title = document.getElementById("capture-assets-title");
  const summary = document.getElementById("capture-assets-summary");
  const help = document.getElementById("capture-asset-help");
  const error = document.getElementById("capture-asset-error");
  const install = document.getElementById("capture-assets-install");
  document.getElementById("capture-asset-total").hidden = true;
  document.getElementById("capture-asset-list").innerHTML = html`${rows.map((asset) => html`
    <article class="capture-asset-row" data-asset-id="${asset.id}" data-kind="${asset.kind}" data-status="waiting">
      <div><b>${asset.label}</b><span>${asset.kind === "external" ? "External service" : captureBytes(asset.size_bytes)}</span></div>
      ${asset.kind === "external" ? "" : html`<progress max="1" value="${asset.cached_bytes / asset.size_bytes}"></progress>`}
      <small>${asset.kind === "external" ? asset.message :
        asset.cached_bytes ? `${captureBytes(asset.cached_bytes)} already cached · ${asset.verification}${asset.platform ? ` · ${asset.platform}` : ""}` :
          `${asset.verification}${asset.platform ? ` · ${asset.platform}` : ""}`}</small>
    </article>`)} `.s;
  error.hidden = true;
  if (blockers.length) {
    title.textContent = "External application required";
    summary.textContent = "This application is managed outside ShowAndTell and cannot be downloaded as one local asset.";
    help.textContent = blockers.map((asset) => asset.message).join(" ");
    install.hidden = true;
  } else {
    title.textContent = "Install required application data";
    summary.textContent = `${downloads.length} selected application asset${downloads.length === 1 ? "" : "s"} must be installed before clean apps can start.`;
    const dockerWarning = downloads.some((asset) => asset.kind === "docker")
      ? " WebArena publishes no checksums for its legacy Docker archives: ShowAndTell tries the HTTPS Google Drive source first, falls back to WebArena’s HTTP CMU mirror when Drive is rate-limited, and verifies the exact byte size plus imported image name."
      : "";
    const mapWarning = downloads.some((asset) => asset.kind === "map")
      ? " The complete local map includes tiles, geocoding, and car/bike/foot routing. WebArena-Verified does not publish archive hashes, so ShowAndTell enforces the exact S3 byte sizes and probes all four services after extraction. Its pinned runtime image is amd64-only and uses Docker emulation on Apple Silicon; Docker Desktop’s disk allocation must also be large enough."
      : "";
    help.textContent = `Download: ${captureBytes(preflight.download_bytes)}. Temporary disk required for import and extraction: ${captureBytes(preflight.required_bytes)}. Free: ${captureBytes(preflight.free_bytes)}. Partial downloads are retained and resumed.${dockerWarning}${mapWarning}`;
    install.hidden = false;
    install.disabled = !preflight.enough_space;
    install.textContent = `Download ${captureBytes(preflight.download_bytes)} & continue`;
    if (!preflight.enough_space) {
      error.textContent = `Not enough disk space. Free at least ${captureBytes(preflight.required_bytes - preflight.free_bytes)} and retry.`;
      error.hidden = false;
    }
  }
  captureStage("assets");
}

function captureRenderAssetProgress(job) {
  captureAssetRuntime = job;
  const total = document.getElementById("capture-asset-total");
  const progress = Math.max(0, Math.min(1, job.progress || 0));
  total.hidden = false;
  document.getElementById("capture-asset-message").textContent = job.message || "Installing…";
  document.getElementById("capture-asset-percent").textContent = `${Math.round(progress * 100)}%`;
  document.getElementById("capture-asset-progress").value = progress;
  document.getElementById("capture-asset-bytes").textContent =
    `${captureBytes(job.downloaded_bytes)} of ${captureBytes(job.total_bytes)} downloaded`;
  for (const asset of job.assets || []) {
    const row = document.querySelector(`[data-asset-id="${asset.id}"]`);
    if (!row) continue;
    row.dataset.status = asset.status;
    const bar = row.querySelector("progress");
    if (bar) bar.value = Math.max(0, Math.min(1, asset.progress || 0));
    const detail = row.querySelector("small");
    if (detail) detail.textContent = asset.status === "pulling"
      ? "Pulling the pinned map runtime image"
      : asset.status === "extracting" ? "Downloads complete · extracting map service volumes"
        : asset.status === "starting" ? "Starting and probing tiles, search, and routing"
          : asset.status === "importing" ? "Download complete · importing into Docker"
            : asset.status === "verifying" ? "Download complete · verifying SHA-256"
              : asset.status === "completed" ? "Installed and verified"
                : `${captureBytes(asset.downloaded_bytes || asset.cached_bytes || 0)} of ${captureBytes(asset.size_bytes)}`;
  }
}

async function capturePollAssets(job) {
  while (captureAssetRuntime?.id === job.id) {
    await new Promise((resolve) => setTimeout(resolve, 750));
    let current;
    try {
      current = await captureAssetApi(job.status_endpoint);
    } catch (error) {
      const el = document.getElementById("capture-asset-error");
      el.textContent = error.message || String(error);
      el.hidden = false;
      return;
    }
    captureRenderAssetProgress({...current,
      status_endpoint: job.status_endpoint, cancel_endpoint: job.cancel_endpoint});
    if (current.status === "completed") {
      const pending = capturePendingLaunch;
      captureAssetRuntime = null;
      if (pending) {
        captureStage("setup");
        await captureLaunch(pending.slug, pending.summary, pending.selected);
      }
      return;
    }
    if (["failed", "canceled"].includes(current.status)) {
      const el = document.getElementById("capture-asset-error");
      el.textContent = current.error || current.message;
      el.hidden = false;
      const button = document.getElementById("capture-assets-install");
      button.hidden = false;
      button.disabled = false;
      button.textContent = current.status === "canceled" ? "Resume download" : "Retry installation";
      document.getElementById("capture-assets-back").textContent = "Back";
      return;
    }
  }
}

async function captureInstallAssets() {
  if (!capturePendingLaunch) return;
  const button = document.getElementById("capture-assets-install");
  const error = document.getElementById("capture-asset-error");
  button.disabled = true;
  button.textContent = "Preparing…";
  error.hidden = true;
  try {
    const job = await captureAssetApi(`${captureConfig().assetsEndpoint}/install`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({applications: capturePendingLaunch.selected.map((app) => app.application)}),
    });
    if (job.status === "completed") {
      captureStage("setup");
      await captureLaunch(capturePendingLaunch.slug, capturePendingLaunch.summary,
        capturePendingLaunch.selected);
      return;
    }
    captureRenderAssetProgress(job);
    button.hidden = true;
    document.getElementById("capture-assets-back").textContent = "Cancel download";
    await capturePollAssets(job);
  } catch (err) {
    error.textContent = err.message || String(err);
    error.hidden = false;
    button.disabled = false;
    button.textContent = "Retry installation";
  }
}

async function captureCancelAssets(close = false) {
  const job = captureAssetRuntime;
  if (job?.cancel_endpoint && ["queued", "running", "canceling"].includes(job.status)) {
    try {
      await captureAssetApi(job.cancel_endpoint, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      });
    } catch { /* closing must not strand the dialog */ }
  }
  captureAssetRuntime = null;
  document.getElementById("capture-assets-back").textContent = "Back";
  document.getElementById("capture-assets-install").hidden = false;
  if (close) document.getElementById("task-capture").hidden = true;
  else captureStage("setup");
}

function captureShowBusyError(payload, targetId = "capture-error") {
  const el = document.getElementById(targetId);
  const heldBy = payload.held_by || "another capture";
  const managedCapture = payload.code === "managed_capture_busy";
  const minutes = payload.since
    ? Math.max(1, Math.round((Date.now() / 1000 - payload.since) / 60)) : null;
  const heldFor = minutes ? ` for ${minutes} min` : "";
  const subject = managedCapture ? "A managed capture is already active" : "The fixture host is busy";
  el.innerHTML = html`${subject}: held by <b>${heldBy}</b>${heldFor}.
    Open <b>Executions</b> in this viewer to inspect and force terminate a stuck task.`.s;
  el.hidden = false;
}

function captureMimeType() {
  const choices = [
    "video/webm;codecs=vp9,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm",
  ];
  return choices.find((value) => MediaRecorder.isTypeSupported(value)) || "";
}

function captureStopTracks(runtime) {
  for (const stream of [runtime.display, runtime.microphone]) {
    if (stream) for (const track of stream.getTracks()) track.stop();
  }
}

function captureStartTranscript(runtime) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    runtime.transcriptSupported = false;
    document.getElementById("capture-live-status").textContent =
      "Screen and microphone audio are recording. Live speech-to-text is unavailable in this browser; the video still contains your narration.";
    return;
  }
  runtime.transcriptSupported = true;
  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = document.documentElement.lang || "en-US";
  recognition.onresult = (event) => {
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const text = event.results[i][0].transcript.trim();
      if (!text) continue;
      if (event.results[i].isFinal) {
        runtime.steps.push({at_ms: Date.now() - runtime.startedAt, text});
      } else interim += `${text} `;
    }
    const lines = runtime.steps.slice(-5).map((step) => html`<div>${step.text}</div>`);
    if (interim) lines.push(html`<div class="capture-interim">${interim.trim()}</div>`);
    document.getElementById("capture-transcript").innerHTML = lines.length
      ? html`${lines}`.s : html`<span>Waiting for narration…</span>`.s;
  };
  recognition.onerror = (event) => {
    if (["not-allowed", "service-not-allowed"].includes(event.error)) {
      runtime.transcriptSupported = false;
      runtime.recognition = null;
    }
  };
  recognition.onend = () => {
    if (!runtime.stopping && runtime.recognition === recognition) {
      try { recognition.start(); } catch { /* browser may throttle restarts */ }
    }
  };
  try {
    recognition.start();
    runtime.recognition = recognition;
  } catch {
    runtime.transcriptSupported = false;
  }
}

async function captureStartRecording() {
  captureSetError("");
  const button = document.getElementById("capture-start");
  const slug = captureSlug(document.getElementById("capture-name").value);
  const summary = document.getElementById("capture-summary").value.trim();
  const selected = [...document.querySelectorAll("[data-capture-app]:checked")]
    .map((el) => ({id: el.value, application: el.dataset.application,
      url: el.dataset.url, label: el.dataset.label}));
  if (!new RegExp(captureConfig().slugPattern).test(slug)) {
    captureSetError("Enter a name with 3–64 letters, numbers, or hyphens.");
    return;
  }
  if (!selected.length) {
    captureSetError("Select at least one application.");
    return;
  }
  const launchId = crypto.randomUUID().replaceAll("-", "");
  capturePendingLaunch = {
    slug, summary, selected, launchId, requesting: false, cancelled: false,
  };
  button.disabled = true;
  button.textContent = "Checking application assets…";
  try {
    const preflight = await captureAssetApi(`${captureConfig().assetsEndpoint}/preflight`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({applications: selected.map((app) => app.application)}),
    });
    if (!preflight.ok) {
      captureRenderAssetPreflight(preflight);
      return;
    }
    await captureLaunch(slug, summary, selected);
  } catch (error) {
    captureSetError(error.message || String(error));
  } finally {
    button.disabled = false;
    button.textContent = "Start clean apps";
  }
}

async function captureLaunch(slug, summary, selected) {
  const button = document.getElementById("capture-start");
  const managed = captureConfig().mode === "managed";
  if (!managed && (!navigator.mediaDevices?.getDisplayMedia || !navigator.mediaDevices?.getUserMedia
      || typeof MediaRecorder === "undefined")) {
    captureSetError("This browser does not support screen and microphone recording.");
    return;
  }
  const mimeType = managed ? "" : captureMimeType();
  if (!managed && !mimeType) {
    captureSetError("This browser cannot create a WebM recording. Use Chrome or Edge for task capture.");
    return;
  }

  button.disabled = true;
  button.textContent = managed ? "Opening managed Chrome…" : "Waiting for permissions…";
  let display = null;
  let microphone = null;
  const pending = capturePendingLaunch;
  try {
    let opened = selected.length;
    if (!managed) {
      // Browser-only fallback: open tabs while the click still carries popup permission.
      opened = selected.map((app, index) =>
        window.open(app.url, `showAndTell-capture-${index}`)).filter(Boolean).length;
      display = await navigator.mediaDevices.getDisplayMedia({
        video: {frameRate: {ideal: 15, max: 30}}, audio: false,
      });
      microphone = await navigator.mediaDevices.getUserMedia({
        audio: {echoCancellation: true, noiseSuppression: true}, video: false,
      });
    }
    pending.requesting = true;
    const session = await captureApi(captureConfig().endpoint, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        slug,
        title: slug.split("-").map((part) => part[0].toUpperCase() + part.slice(1)).join(" "),
        summary,
        applications: selected.map((app) => app.id),
        mode: managed ? "managed" : "browser",
        launch_id: pending.launchId,
      }),
    });
    pending.requesting = false;
    if (pending.cancelled) {
      await captureApi(session.cancel_endpoint, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
      });
      return;
    }
    let recorder = null;
    if (!managed) {
      const stream = new MediaStream([
        ...display.getVideoTracks(),
        ...microphone.getAudioTracks(),
      ]);
      recorder = new MediaRecorder(stream, {mimeType});
    }
    const runtime = {
      display, microphone, recorder, session,
      startedAt: null, steps: [], nextChunk: 0,
      uploadQueue: Promise.resolve(), uploadError: null,
      stopping: false, finished: false,
    };
    captureRuntime = runtime;
    captureAssetRuntime = null;
    if (recorder) recorder.ondataavailable = (event) => {
      if (!event.data || !event.data.size) return;
      const sequence = runtime.nextChunk++;
      runtime.uploadQueue = runtime.uploadQueue.then(() => captureApi(session.chunk_endpoint, {
        method: "POST",
        headers: {"Content-Type": "video/webm", "X-ShowAndTell-Chunk": String(sequence)},
        body: event.data,
      })).catch((error) => {
        runtime.uploadError = error;
        throw error;
      });
    };
    if (recorder) {
      runtime.startedAt = Date.now();
      recorder.start(5000);
    }
    if (display) display.getVideoTracks()[0].addEventListener("ended", () => {
      if (!runtime.stopping) captureStopRecording();
    }, {once: true});
    if (managed) {
      document.getElementById("capture-seed-error").hidden = true;
      captureRenderLoginGuidance(session);
      captureStage("seed");
    } else {
      captureStartTranscript(runtime);
      captureStage("live");
      const popupNote = opened < selected.length
        ? ` ${selected.length - opened} app tab(s) were popup-blocked; open them manually.` : "";
      document.getElementById("capture-live-status").textContent =
        "Actions, screen, microphone, and narration are being recorded." + popupNote;
      captureStartTimer(runtime);
    }
  } catch (error) {
    if (display) for (const track of display.getTracks()) track.stop();
    if (microphone) for (const track of microphone.getTracks()) track.stop();
    if (pending?.cancelled) {
      return;
    } else if (["host_busy", "managed_capture_busy"].includes(error.payload?.code)) {
      captureShowBusyError(error.payload);
    } else {
      captureSetError(error.name === "NotAllowedError"
        ? "Screen or microphone permission was not granted. Nothing was recorded."
        : error.message || String(error));
    }
  } finally {
    if (pending) pending.requesting = false;
    button.disabled = false;
    button.textContent = "Start clean apps";
  }
}

function captureStartTimer(runtime) {
  document.getElementById("capture-timer").textContent = "00:00";
  runtime.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - runtime.startedAt) / 1000);
    document.getElementById("capture-timer").textContent =
      `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }, 250);
}

async function captureBeginRecording() {
  const runtime = captureRuntime;
  if (!runtime || runtime.finished || runtime.startedAt) return;
  const button = document.getElementById("capture-record");
  const error = document.getElementById("capture-seed-error");
  error.hidden = true;
  button.disabled = true;
  // Freezing exact application state takes ~30s; say so rather than looking hung.
  button.textContent = "Freezing application state…";
  try {
    const result = await captureApi(runtime.session.record_endpoint, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    });
    runtime.startedAt = Date.now();
    captureStartTranscript(runtime);
    document.getElementById("capture-live-status").textContent =
      result.voice_recording_available
        ? "Actions, selectors, accessibility trees, screenshots, screen, microphone, and narration are being recorded."
        : "Actions, selectors, accessibility trees, screenshots, and live narration are being captured. OS screen/microphone recording is unavailable.";
    captureStage("live");
    captureStartTimer(runtime);
  } catch (err) {
    error.textContent = err.message || String(err);
    error.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "● Start recording";
  }
}

async function captureCancelSeed() {
  const runtime = captureRuntime;
  if (!runtime || runtime.startedAt) return;
  const button = document.getElementById("capture-seed-cancel");
  button.disabled = true;
  try {
    await captureApi(runtime.session.cancel_endpoint, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    });
    runtime.finished = true;
    captureClose();
  } catch (err) {
    const error = document.getElementById("capture-seed-error");
    error.textContent = err.message || String(err);
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function captureDiscardRecording() {
  const runtime = captureRuntime;
  if (!runtime || runtime.finished || runtime.stopping) return;
  const elapsed = document.getElementById("capture-timer").textContent;
  if (!window.confirm(`Discard this ${elapsed} recording? The applications are `
      + `released and no draft is written. This cannot be undone.`)) return;
  const button = document.getElementById("capture-discard");
  const stop = document.getElementById("capture-stop");
  runtime.stopping = true;
  button.disabled = stop.disabled = true;
  button.textContent = "Discarding…";
  clearInterval(runtime.timer);
  if (runtime.recognition) {
    // Clear it first so the onend handler does not restart recognition.
    const recognition = runtime.recognition;
    runtime.recognition = null;
    try { recognition.stop(); } catch { /* already stopped */ }
  }
  if (runtime.recorder && runtime.recorder.state !== "inactive") {
    try { runtime.recorder.stop(); } catch { /* already stopped */ }
  }
  captureStopTracks(runtime);
  try {
    await captureApi(runtime.session.cancel_endpoint, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
    });
    runtime.finished = true;
    captureClose();
  } catch (error) {
    // Local capture is already torn down, so let the operator out either way;
    // retrying is still offered because the server may still hold the session.
    runtime.finished = true;
    runtime.stopping = false;
    document.getElementById("capture-live-status").textContent =
      `Could not release the applications: ${error.message || String(error)}`;
    button.disabled = false;
    button.textContent = "Retry discard";
  }
}

async function captureStopRecording() {
  const runtime = captureRuntime;
  if (!runtime) return;
  if (runtime.finished) {
    captureClose();
    return;
  }
  if (runtime.stopping) return;
  runtime.stopping = true;
  const button = document.getElementById("capture-stop");
  button.disabled = true;
  button.textContent = "Finalizing recording…";
  clearInterval(runtime.timer);
  if (runtime.recognition) {
    try { runtime.recognition.stop(); } catch { /* already stopped */ }
  }
  try {
    if (runtime.recorder) await new Promise((resolve) => {
        runtime.recorder.addEventListener("stop", resolve, {once: true});
        if (runtime.recorder.state === "inactive") resolve();
        else runtime.recorder.stop();
      });
    captureStopTracks(runtime);
    await runtime.uploadQueue;
    if (runtime.uploadError) throw runtime.uploadError;
    const result = await captureApi(runtime.session.finish_endpoint, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        duration_ms: Date.now() - runtime.startedAt,
        steps: runtime.steps,
        transcript_supported: runtime.transcriptSupported,
        voice_recorded: true,
      }),
    });
    runtime.finished = true;
    runtime.draft = result;
    const snapshots = result.state_snapshots || [];
    // Distinguish the three genuinely different outcomes: exact application
    // state was frozen, the fixture described its own state, or neither and
    // replay depends entirely on the recorded setup actions.
    const seedNote = snapshots.length
      ? `exact application state frozen (${snapshots.join(", ")})`
      : result.seed_exported
        ? "exported to demo/seed.json"
        : "preserved only through replayable setup actions";
    document.getElementById("capture-done-summary").textContent =
      `${result.actions || 0} replayable action${result.actions === 1 ? "" : "s"} and ${result.steps} spoken step${result.steps === 1 ? "" : "s"} captured; seed ${seedNote}${result.replay_generated ? "; demonstrate.py was generated" : ""}${result.recording_available ? "" : "; screen/microphone evidence was unavailable"}.`;
    document.getElementById("capture-done-path").textContent = result.path;
    captureStage("done");
  } catch (error) {
    captureStopTracks(runtime);
    document.getElementById("capture-live-status").textContent =
      `Could not create the testcase: ${error.message || String(error)}`;
    button.disabled = false;
    if (runtime.uploadError) {
      runtime.finished = true;
      button.textContent = "Close";
    } else {
      button.textContent = "Retry create testcase";
      runtime.stopping = false;
    }
  }
}

const PENDING_CAPTURE_QUESTION_EDITOR = "showAndTell-pending-capture-question-editor";

function captureOpenTask() {
  const slug = captureRuntime?.draft?.slug;
  if (!slug) return;
  location.hash = `#/task/${encodeURIComponent(slug)}/demo`;
  location.reload();
}

function captureEditQuestions() {
  const slug = captureRuntime?.draft?.slug;
  if (!slug) return;
  try {
    sessionStorage.setItem(PENDING_CAPTURE_QUESTION_EDITOR, slug);
  } catch {
    /* Navigation still lands on the Quiz tab; the Add questions button remains. */
  }
  location.hash = `#/task/${encodeURIComponent(slug)}/quiz`;
  location.reload();
}

function openPendingCaptureQuestionEditor() {
  let slug = null;
  try {
    slug = sessionStorage.getItem(PENDING_CAPTURE_QUESTION_EDITOR);
    sessionStorage.removeItem(PENDING_CAPTURE_QUESTION_EDITOR);
  } catch {
    return;
  }
  const task = slug && TASKS_BY_NAME[slug];
  if (task && state.task === slug && questionEditingEnabled(task)) {
    openQuestionEditor(task);
  }
}


async function capturePromote() {
  const runtime = captureRuntime;
  if (!runtime?.draft || runtime.promoting) return;
  runtime.promoting = true;
  const button = document.getElementById("capture-promote");
  button.disabled = true;
  const status = document.getElementById("capture-promotion-status");
  status.textContent = "Adding the task…";
  try {
    await promoteDraftFlow(runtime.draft.slug, () => {
      status.textContent = "Task added. Opening it…";
    });
  } catch (error) {
    runtime.promoting = false;
    button.disabled = false;
    status.textContent = error.message || String(error);
  }
}

function initTaskCapture() {
  const config = captureConfig();
  const launch = document.getElementById("new-task");
  if (!config.enabled) {
    launch.disabled = true;
    launch.title = "New task capture is available from showAndTell.viewer.serve on localhost";
    return;
  }
  document.getElementById("capture-app-grid").innerHTML = html`${config.applications.map((app) => html`
    <label class="capture-app-card">
      <input type="checkbox" data-capture-app value="${app.id}" data-application="${app.application}" data-url="${app.url}" data-label="${app.label}">
      <span class="capture-app-copy">
        <b>${app.familiar_label || app.label}</b>
        <small>${app.label}</small>
      </span>
      <span class="capture-app-space">${captureRequiredSpace(app.space_required_bytes, app.space_required_estimated)}</span>
    </label>`)} `.s;
  launch.addEventListener("click", captureOpen);
  document.getElementById("capture-close").addEventListener("click", captureClose);
  document.getElementById("capture-cancel").addEventListener("click", captureClose);
  document.getElementById("capture-finish").addEventListener("click", captureClose);
  document.getElementById("capture-questions").addEventListener("click", captureEditQuestions);
  document.getElementById("capture-start").addEventListener("click", captureStartRecording);
  document.getElementById("capture-assets-install").addEventListener("click", captureInstallAssets);
  document.getElementById("capture-assets-back").addEventListener("click", () => captureCancelAssets(false));
  document.getElementById("capture-record").addEventListener("click", captureBeginRecording);
  document.getElementById("capture-seed-cancel").addEventListener("click", captureCancelSeed);
  document.getElementById("capture-discard").addEventListener("click", captureDiscardRecording);
  document.getElementById("capture-stop").addEventListener("click", captureStopRecording);
  document.getElementById("capture-open-task").addEventListener("click", captureOpenTask);
  document.getElementById("capture-promote").addEventListener("click", () => capturePromote());
  document.getElementById("capture-name").addEventListener("blur", (event) => {
    event.target.value = captureSlug(event.target.value);
  });
  document.getElementById("task-capture").addEventListener("click", (event) => {
    if (event.target.id === "task-capture") captureClose();
  });
}
