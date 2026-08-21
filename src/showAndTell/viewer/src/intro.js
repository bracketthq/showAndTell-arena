// intro.js — five-step first-run spotlight tour for the local live viewer.
// Defines: initIntroGuide(). Uses: DATA, allTasks, openTask, captureClose.

const INTRO_GUIDE_KEY = "showAndTell-intro-v2";
const INTRO_STEPS = [
  {
    title: "Choose a task",
    copy: "Pick any task from the library. We’ll open one now so you can see where to run it.",
    target: () => document.querySelector(".nav-task"),
    placement: "right",
  },
  {
    title: "Run the task",
    copy: "Choose Brackett, Codex, or Claude. The run and its progress stay visible on this page.",
    target: () => document.querySelector(".task-run-controls"),
    placement: "below",
  },
  {
    title: "Create your own task",
    copy: "Use New task to choose apps, prepare the starting data, then record and narrate the workflow.",
    target: () => document.getElementById("new-task"),
    placement: "below",
  },
  {
    title: "Select the apps",
    copy: "Choose every app the workflow uses. ShowAndTell starts clean, isolated copies so every recording begins from a known state.",
    target: () => document.querySelector(".capture-app-card"),
    placement: "below",
  },
  {
    title: "Start clean apps",
    copy: "Give the task a name, optionally add a short description, then start the apps. You’ll prepare their starting data before recording begins.",
    target: () => document.getElementById("capture-start"),
    placement: "above",
  },
];

let introStep = 0;
let introActive = false;

function introGuideEnabled() {
  return Boolean(DATA.taskRuns?.enabled && DATA.taskCapture?.enabled && allTasks().length);
}

function introGuideSeen() {
  try {
    return localStorage.getItem(INTRO_GUIDE_KEY) === "seen";
  } catch {
    return false;
  }
}

function rememberIntroGuide() {
  try {
    localStorage.setItem(INTRO_GUIDE_KEY, "seen");
  } catch {
    /* A locked-down browser may show the tour again on its next visit. */
  }
}

function introEl(id) {
  return document.getElementById(id);
}

function introIsMobile() {
  return matchMedia("(max-width: 820px)").matches;
}

function introPrepareStep(index) {
  const side = introEl("side");
  if (index === 0 && introIsMobile()) {
    // The target must be in its final position before the spotlight is measured.
    side.classList.add("intro-tour-open", "open");
    requestAnimationFrame(() => side.classList.remove("intro-tour-open"));
  } else side.classList.remove("open");

  for (const el of [introEl("intro-spotlight"), introEl("intro-popover")])
    el.classList.toggle("intro-over-capture", index >= 3);

  if (index === 1 && !document.querySelector(".task-run-controls")) {
    const task = document.querySelector(".nav-task")?.dataset.task;
    if (task) openTask(task, "demo");
  }
}

function introPosition() {
  if (!introActive) return;
  const step = INTRO_STEPS[introStep];
  const target = step.target();
  if (!target) {
    if (introStep >= 3 && introEl("task-capture").hidden) {
      introStep = 2;
      introRenderStep();
      return;
    }
    if (introStep === 1) {
      const task = document.querySelector(".nav-task")?.dataset.task;
      if (task) openTask(task, "demo");
    }
    requestAnimationFrame(introPosition);
    return;
  }

  target.scrollIntoView({block: "center", inline: "nearest"});
  const rect = target.getBoundingClientRect();
  const pad = 6;
  const spotlight = introEl("intro-spotlight");
  spotlight.style.left = `${Math.max(4, rect.left - pad)}px`;
  spotlight.style.top = `${Math.max(4, rect.top - pad)}px`;
  spotlight.style.width = `${Math.max(24, rect.width + pad * 2)}px`;
  spotlight.style.height = `${Math.max(24, rect.height + pad * 2)}px`;

  const popover = introEl("intro-popover");
  const gap = 14;
  const margin = 12;
  const pop = popover.getBoundingClientRect();
  let left;
  let top;
  if (step.placement === "right" && rect.right + gap + pop.width <= innerWidth - margin) {
    left = rect.right + gap;
    top = rect.top + rect.height / 2 - pop.height / 2;
  } else if (rect.bottom + gap + pop.height <= innerHeight - margin) {
    left = rect.left + rect.width / 2 - pop.width / 2;
    top = rect.bottom + gap;
  } else {
    left = rect.left + rect.width / 2 - pop.width / 2;
    top = rect.top - gap - pop.height;
  }
  popover.style.left = `${Math.max(margin, Math.min(left, innerWidth - pop.width - margin))}px`;
  popover.style.top = `${Math.max(margin, Math.min(top, innerHeight - pop.height - margin))}px`;
}

function introRenderStep() {
  const step = INTRO_STEPS[introStep];
  introPrepareStep(introStep);
  introEl("intro-progress").textContent = `${introStep + 1} of ${INTRO_STEPS.length}`;
  introEl("intro-title").textContent = step.title;
  introEl("intro-copy").textContent = step.copy;
  introEl("intro-back").hidden = introStep === 0;
  introEl("intro-next").textContent = introStep === INTRO_STEPS.length - 1
    ? "Finish" : "Next";
  requestAnimationFrame(() => requestAnimationFrame(introPosition));
}

function introOpen() {
  if (!introGuideEnabled()) return;
  introActive = true;
  introStep = 0;
  introEl("intro-spotlight").hidden = false;
  introEl("intro-popover").hidden = false;
  introRenderStep();
  introEl("intro-next").focus({preventScroll: true});
}

function introClose() {
  introActive = false;
  introEl("intro-spotlight").hidden = true;
  introEl("intro-popover").hidden = true;
  introEl("intro-spotlight").classList.remove("intro-over-capture");
  introEl("intro-popover").classList.remove("intro-over-capture");
  introEl("side").classList.remove("open");
  if (!introEl("task-capture").hidden) captureClose();
  openOverview();
  rememberIntroGuide();
  introEl("intro-help").focus({preventScroll: true});
}

function introNext() {
  if (introStep === 0) {
    const task = document.querySelector(".nav-task")?.dataset.task;
    if (task) openTask(task, "demo");
  }
  if (introStep === 2) {
    introEl("new-task").click();
    return;
  }
  if (introStep === INTRO_STEPS.length - 1) {
    introClose();
    return;
  }
  introStep += 1;
  introRenderStep();
}

function introBack() {
  if (introStep === 0) return;
  if (introStep === 3 && !introEl("task-capture").hidden) captureClose();
  introStep -= 1;
  introRenderStep();
}

function initIntroGuide() {
  if (!introGuideEnabled()) return;
  const help = introEl("intro-help");
  help.hidden = false;
  help.addEventListener("click", introOpen);
  introEl("new-task").addEventListener("click", () => {
    if (!introActive || introStep !== 2) return;
    introStep = 3;
    introRenderStep();
  });
  introEl("intro-skip").addEventListener("click", introClose);
  introEl("intro-next").addEventListener("click", introNext);
  introEl("intro-back").addEventListener("click", introBack);
  window.addEventListener("resize", introPosition);
  document.querySelector(".content").addEventListener("scroll", introPosition);
  introEl("side").addEventListener("scroll", introPosition);
  window.addEventListener("keydown", (event) => {
    if (introActive && event.key === "Escape") introClose();
  });
  if (!introGuideSeen()) introOpen();
}
