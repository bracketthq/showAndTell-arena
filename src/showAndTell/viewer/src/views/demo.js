// views/demo.js — the canonical demonstration video.
// Defines: renderDemo().
// Uses: html, emptyState.

function renderDemo(t) {
  return t.demoRecording
    ? html`<section class="demo-recording">
        <div class="section-label">original task recording — watch before reviewing questions</div>
        <video controls preload="metadata" src="${assetUrl(t.demoRecording)}">
          Your browser cannot play this recording. <a href="${assetUrl(t.demoRecording)}">Open the video file.</a>
        </video>
      </section>`
    : html`<section class="demo-recording demo-recording-missing" role="status">
        <div class="section-label">original task recording — required before reviewing questions</div>
        ${emptyState(
          "No canonical task recording yet.",
          t.draft
            ? "The capture completed without a playable screen recording."
            : `Record this task with showAndTell demo-record --task tasks/${t.questionTask}, then refresh.`,
        )}
      </section>`;
}
