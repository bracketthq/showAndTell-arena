// views/quiz.js — question cards and a plain-language question editor.
// Defines: renderQuiz(), questionBody() (also used by the results view),
//          collectQuestionDraft(), saveQuestionDraft().
// Uses: html, emptyState.

function questionBody(q) {
  if (q.type === "multiple_choice") {
    const options = (q.options || []).map((o) =>
      html`<span class="alias ${o.id === q.correct_option ? "alias-primary" : ""}">${o.id}) ${o.text}</span>`);
    return html`<div class="section-label">answer choices (highlighted = correct)</div>
      <div class="aliases">${options}</div>`;
  }
  if (q.type === "closed") {
    return html`<div class="section-label">answers counted as correct</div>
      <div class="aliases">${(q.answer_aliases || []).map((a, i) =>
        html`<span class="alias ${i === 0 ? "alias-primary" : ""}">${a}</span>`)}</div>`;
  }
  return html`<div class="section-label">what a correct answer should include</div>
    <div class="rubric">${q.rubric}</div>`;
}

function questionTypeLabel(kind) {
  if (kind === "multiple_choice") return "Multiple choice";
  if (kind === "closed") return "Short answer";
  return "Explained answer";
}

function questionEvidence(q) {
  const evidence = q.evidence || [];
  if (!evidence.length) return "";
  return html`<details class="evidence-block">
    <summary class="evidence-summary">
      <span>Demonstration evidence</span>
      <span class="evidence-count">${evidence.length} item${evidence.length === 1 ? "" : "s"}</span>
      <span class="evidence-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="evidence-list">${evidence.map((e) => {
      if (e.type === "screenshot") {
        const src = assetUrl(e.path);
        return html`<figure class="evidence-shot">
          <div class="evidence-shot-head">
            <span class="evidence-kind">screenshot</span>
            <span class="evidence-ref">${e.step} · line ${e.line}</span>
          </div>
          <a href="${src}" target="_blank" rel="noopener" title="Open full-size evidence screenshot">
            <img src="${src}" loading="lazy" alt="Evidence for ${q.id}: ${e.description}">
          </a>
          <figcaption>${e.description}</figcaption>
        </figure>`;
      }
      const ref = e.key || e.step || e.page || e.ref || "reference";
      const detail = e.text || e.description || "";
      const label = e.type === "narration" ? "voice" : e.type;
      return html`<div class="evidence-item evidence-${e.type}">
        <span class="evidence-kind">${label}</span>
        <span class="evidence-ref">${ref}</span>
        <span class="evidence-detail">${detail}</span>
      </div>`;
    })}</div>
  </details>`;
}

function questionEditingEnabled(t) {
  return ["task", "draft"].includes(t.questionSource)
    && Boolean(DATA.questionEditing && DATA.questionEditing.enabled);
}

function newQuestionDraft(questions) {
  let n = questions.length + 1;
  const ids = new Set(questions.map((q) => q.id));
  while (ids.has(`q${n}`)) n += 1;
  return {id: `q${n}`, type: "closed", question: "", answer_aliases: [""]};
}

function openQuestionEditor(t) {
  const questions = structuredClone(t.questions);
  if (!questions.length) questions.push(newQuestionDraft(questions));
  setState({questionEditor: {
    task: t.name, questions, status: {}, activeQuestion: 0,
    dirty: !t.questions.length, validation: {},
  }});
}

function hasUnsavedQuestionChanges() {
  return Boolean(state.questionEditor && state.questionEditor.dirty);
}

function confirmDiscardQuestionChanges() {
  return !hasUnsavedQuestionChanges()
    || window.confirm("You have unsaved quiz changes. Leave without saving them?");
}

function editorQuestionKind(kind) {
  return kind === "multiple_choice" || kind === "closed" ? kind : "llm_judge";
}

function editorQuestionOptions(q) {
  const options = Array.isArray(q.options) && q.options.length
    ? q.options.map((option) => ({...option}))
    : [{id: "A", text: "Yes"}, {id: "B", text: "No"}];
  if (!options.some((option) => ["I’m not sure", "I'm not sure"].includes(option.text))) {
    options.push({id: nextQuestionOptionId(options), text: "I’m not sure"});
  }
  return options;
}

function nextQuestionOptionId(options) {
  const used = new Set(options.map((option) => option.id));
  for (const id of "ABCDEFGHIJKLMNOPQRSTUVWXYZ") {
    if (!used.has(id)) return id;
  }
  let number = options.length + 1;
  while (used.has(`O${number}`)) number += 1;
  return `O${number}`;
}

function renderQuestionOption(option, optionIndex, questionIndex, optionCount) {
  const isUnsure = ["I’m not sure", "I'm not sure"].includes(option.text);
  return html`<div class="qedit-option" data-option-id="${option.id}">
    <span class="qedit-option-id">${option.id}</span>
    <input data-option-text value="${option.text || ""}"
      aria-label="Answer choice ${option.id}"
      placeholder="Example: Approve the return" autocomplete="off" ${isUnsure ? "readonly" : ""}>
    <button type="button" data-action="remove-question-option"
      data-question-index="${questionIndex}" data-option-index="${optionIndex}"
      ${optionCount <= 2 || isUnsure ? "disabled" : ""}>Remove</button>
  </div>`;
}

function questionDraftIssues(q) {
  const issues = {};
  if (!String(q.question || "").trim()) issues.question = "Enter the question users should answer.";
  if (q.type === "multiple_choice") {
    const options = Array.isArray(q.options) ? q.options : [];
    if (options.length < 2 || options.some((option) => !String(option.text || "").trim())) {
      issues.options = "Complete every answer choice.";
    }
    if (!options.some((option) => option.id === q.correct_option)) {
      issues.correct_option = "Choose the correct answer.";
    }
  } else if (q.type === "closed") {
    if (!(q.answer_aliases || []).some((answer) => String(answer).trim())) {
      issues.answer_aliases = "Enter at least one answer that should count as correct.";
    }
  } else if (!String(q.rubric || "").trim()) {
    issues.rubric = "Describe what a correct explained answer must include.";
  }
  return issues;
}

function renderQuestionError(editor, index, field) {
  const message = editor.validation?.[index]?.[field];
  return html`<span class="qedit-field-error" data-qerror="${field}"
    ${message ? "" : "hidden"}>${message || ""}</span>`;
}

function renderQuestionEditorField(q, index, editor) {
  const kind = editorQuestionKind(q.type || "closed");
  const options = editorQuestionOptions(q);
  const correctOption = options.some((option) => option.id === q.correct_option)
    ? q.correct_option : options[0].id;
  const evidenceCount = Array.isArray(q.evidence) ? q.evidence.length : 0;
  const expanded = editor.activeQuestion === index;
  const complete = !Object.keys(questionDraftIssues(q)).length;
  return html`<div class="qedit qcard ${expanded ? "qedit-open" : "qedit-collapsed"}"
    data-question-index="${index}">
    <div class="qedit-head">
      <button type="button" class="qedit-summary" data-action="open-question-card"
        data-index="${index}" aria-expanded="${expanded}">
        <span class="qedit-number">Question ${index + 1}</span>
        <span class="qedit-summary-text">${q.question || "Untitled question"}</span>
        <span class="qedit-summary-type">${questionTypeLabel(kind)}</span>
        <span class="qedit-completion ${complete ? "qedit-complete" : "qedit-incomplete"}">
          ${complete ? "Complete" : "Needs information"}
        </span>
        <span class="qedit-chevron" aria-hidden="true">⌄</span>
      </button>
      <div class="qedit-move">
        <button type="button" data-action="move-question-up" data-index="${index}" aria-label="Move question up">↑</button>
        <button type="button" data-action="move-question-down" data-index="${index}" aria-label="Move question down">↓</button>
        <button type="button" class="qedit-remove" data-action="remove-question" data-index="${index}">Remove</button>
      </div>
    </div>
    <div class="qedit-body" ${expanded ? "" : "hidden"}>
      <input type="hidden" data-qfield="id" value="${q.id || ""}">
      <div class="qedit-grid qedit-grid-type">
        <label>How should the user answer?
          <select data-qfield="type" data-question-type>
          <option value="closed" ${kind === "closed" ? "selected" : ""}>Short answer</option>
          <option value="multiple_choice" ${kind === "multiple_choice" ? "selected" : ""}>Multiple choice</option>
          <option value="llm_judge" ${kind === "llm_judge" ? "selected" : ""}>Explained answer</option>
        </select></label>
      </div>
      <label>Question
        <span>Ask one clear thing the user should have learned from the video.</span>
        <textarea data-qfield="question" rows="3" aria-invalid="${Boolean(editor.validation?.[index]?.question)}"
          placeholder="Example: What should happen when a return is more than 30 days old?">${q.question || ""}</textarea>
        ${renderQuestionError(editor, index, "question")}</label>
      <div data-kind="multiple_choice" ${kind === "multiple_choice" ? "" : "hidden"}>
        <div class="qedit-help"><strong>Multiple choice</strong>
          <span>The user picks one answer. Keep “I’m not sure” as one of the choices.</span></div>
        <label>Answer choices</label>
        <div class="qedit-options">${options.map((option, optionIndex) =>
          renderQuestionOption(option, optionIndex, index, options.length))}</div>
        ${renderQuestionError(editor, index, "options")}
        <button type="button" class="qedit-add-option" data-action="add-question-option"
          data-question-index="${index}">Add another choice</button>
        <label>Which choice is correct?
          <select data-qfield="correct_option" aria-invalid="${Boolean(editor.validation?.[index]?.correct_option)}">${options.map((option) => html`
            <option value="${option.id}" ${option.id === correctOption ? "selected" : ""}>
              ${option.id} — ${option.text || "Untitled choice"}
            </option>`)}</select>
          ${renderQuestionError(editor, index, "correct_option")}</label>
      </div>
      <div data-kind="closed" ${kind === "closed" ? "" : "hidden"}>
        <div class="qedit-help"><strong>Short answer</strong>
          <span>Best when the answer is a name, status, number, or short phrase.</span></div>
        <label>Answers counted as correct
          <span>Enter one acceptable wording per line. The first line is the preferred answer.</span>
          <textarea data-qfield="answer_aliases" rows="4" aria-invalid="${Boolean(editor.validation?.[index]?.answer_aliases)}"
            placeholder="Approve the return&#10;Approved&#10;Issue the refund">${(q.answer_aliases || [""]).join("\n")}</textarea>
          ${renderQuestionError(editor, index, "answer_aliases")}</label>
      </div>
      <div data-kind="llm_judge" ${kind === "llm_judge" ? "" : "hidden"}>
        <div class="qedit-help"><strong>Explained answer</strong>
          <span>Best when the user must explain a rule or reasoning. An AI grader checks the answer against your guidance.</span></div>
        <label>What must a correct answer include?
          <span>Describe the required facts. Be specific; this is the grading guide.</span>
          <textarea data-qfield="rubric" rows="5" aria-invalid="${Boolean(editor.validation?.[index]?.rubric)}"
            placeholder="Example: The answer must say the 30-day window has expired and the request should be declined politely.">${q.rubric || ""}</textarea>
          ${renderQuestionError(editor, index, "rubric")}</label>
      </div>
      <div class="qedit-evidence-note">
        <strong>Evidence from the demonstration</strong>
        <span>${evidenceCount
          ? `${evidenceCount} existing evidence link${evidenceCount === 1 ? " is" : "s are"} attached and will be kept automatically.`
          : "No evidence is attached yet. This is optional and does not block saving the question."}</span>
      </div>
    </div>
  </div>`;
}

function renderQuestionEditor(t, editor) {
  const status = editor.status || {};
  return html`<div class="qedit-toolbar" data-dirty="${Boolean(editor.dirty)}">
      <div>
        <strong>Edit quiz questions</strong>
        <span>Use plain language. ShowAndTell handles the question file for you.</span>
        <span class="qedit-unsaved">${editor.dirty ? "Unsaved changes" : "No unsaved changes"}</span>
      </div>
      <div class="qedit-actions">
        <button type="button" data-action="add-question">Add question</button>
        <button type="button" data-action="cancel-question-edit">Cancel</button>
        <button type="button" class="qedit-save" data-action="save-questions"
          ${status.kind === "saving" || !editor.dirty ? "disabled" : ""}>${status.kind === "saving" ? "Saving…" : "Save questions"}</button>
      </div>
    </div>
    ${status.message ? html`<div class="qedit-status qedit-status-${status.kind}" role="status">${status.message}</div>` : ""}
    ${editor.questions.map((q, index) => renderQuestionEditorField(q, index, editor))}`;
}

function parseQuestionLines(form, field) {
  const rawValue = form.querySelector(`[data-qfield="${field}"]`).value;
  return rawValue.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function collectQuestionDraft() {
  const editor = state.questionEditor;
  if (!editor) throw new Error("Question editor is not open.");
  const forms = [...document.querySelectorAll(".qedit[data-question-index]")];
  return forms.map((form, index) => {
    const original = editor.questions[index] || {};
    const value = {...original}; // preserve future/unknown per-question fields
    const read = (name) => form.querySelector(`[data-qfield="${name}"]`).value;
    value.id = read("id").trim();
    value.type = read("type");
    value.question = read("question").trim();
    delete value.options;
    delete value.correct_option;
    delete value.answer_aliases;
    delete value.rubric;
    if (value.type === "multiple_choice") {
      value.options = [...form.querySelectorAll(".qedit-option")].map((row) => ({
        id: row.dataset.optionId,
        text: row.querySelector("[data-option-text]").value.trim(),
      }));
      value.correct_option = read("correct_option").trim();
    } else if (value.type === "closed") {
      value.answer_aliases = parseQuestionLines(form, "answer_aliases");
    } else {
      value.rubric = read("rubric").trim();
    }
    return value;
  });
}

function openQuestionCard(index) {
  try {
    const questions = collectQuestionDraft();
    const editor = state.questionEditor;
    setState({questionEditor: {...editor, questions, activeQuestion: index}});
  } catch (err) { questionDraftError(err); }
}

function markQuestionEditorDirty(target) {
  const editor = state.questionEditor;
  const form = target.closest?.(".qedit[data-question-index]");
  if (!editor || !form) return;
  editor.dirty = true;
  if (editor.status?.kind === "success") editor.status = {};
  const field = target.matches("[data-option-text]") ? "options" : target.dataset.qfield;
  const index = Number(form.dataset.questionIndex);
  if (field && editor.validation?.[index]) delete editor.validation[index][field];
  target.removeAttribute("aria-invalid");
  const error = form.querySelector(`[data-qerror="${field}"]`);
  if (error) {
    error.textContent = "";
    error.hidden = true;
  }
  const toolbar = document.querySelector(".qedit-toolbar");
  if (toolbar) {
    toolbar.dataset.dirty = "true";
    const indicator = toolbar.querySelector(".qedit-unsaved");
    if (indicator) indicator.textContent = "Unsaved changes";
    const save = toolbar.querySelector('[data-action="save-questions"]');
    if (save) save.disabled = false;
  }
  document.querySelector(".qedit-status-success")?.remove();
}

function updateQuestionOptionDraft(questionIndex, optionIndex = null) {
  try {
    const questions = collectQuestionDraft();
    const question = questions[questionIndex];
    if (!question || question.type !== "multiple_choice") return;
    const options = question.options || [];
    if (optionIndex === null) {
      options.push({id: nextQuestionOptionId(options), text: ""});
    } else if (options.length > 2) {
      const [removed] = options.splice(optionIndex, 1);
      if (removed && question.correct_option === removed.id) {
        question.correct_option = options[0]?.id || "";
      }
    }
    question.options = options;
    updateQuestionDraft(questions);
  } catch (err) { questionDraftError(err); }
}

function updateQuestionDraft(questions, status = null, options = {}) {
  const current = state.questionEditor;
  setState({questionEditor: {
    ...current,
    questions,
    status: status || current.status || {},
    activeQuestion: options.activeQuestion ?? current.activeQuestion,
    dirty: options.dirty ?? true,
    validation: options.validation ?? {},
  }});
}

function questionDraftError(err) {
  const editor = state.questionEditor;
  if (!editor) return;
  setState({questionEditor: {...editor, status: {kind: "error", message: String(err.message || err)}}});
}

async function saveQuestionDraft() {
  const t = TASKS_BY_NAME[state.task];
  let questions;
  try {
    questions = collectQuestionDraft();
  } catch (err) {
    questionDraftError(err);
    return;
  }
  const validation = {};
  for (const [index, question] of questions.entries()) {
    const issues = questionDraftIssues(question);
    if (Object.keys(issues).length) validation[index] = issues;
  }
  const firstInvalid = Object.keys(validation).map(Number)[0];
  if (firstInvalid !== undefined) {
    const firstField = Object.keys(validation[firstInvalid])[0];
    updateQuestionDraft(questions, {
      kind: "error", message: "Complete the highlighted fields before saving.",
    }, {activeQuestion: firstInvalid, dirty: true, validation});
    requestAnimationFrame(() => {
      const form = document.querySelector(`.qedit[data-question-index="${firstInvalid}"]`);
      const target = firstField === "options"
        ? form?.querySelector("[data-option-text]")
        : form?.querySelector(`[data-qfield="${firstField}"]`);
      target?.focus();
      target?.scrollIntoView({block: "center", behavior: "smooth"});
    });
    return;
  }
  updateQuestionDraft(questions, {kind: "saving", message: "Saving questions…"}, {
    dirty: true,
  });
  try {
    const result = await apiFetch(DATA.questionEditing, DATA.questionEditing.endpoint, {
      method: "POST",
      body: JSON.stringify({task: t.questionTask, source: t.questionSource,
                            base_hash: t.questionsHash, questions}),
    }, "Save");
    t.questions = result.questions;
    t.questionsHash = result.hash;
    if (!state.questionEditor || state.questionEditor.task !== t.name) return;
    updateQuestionDraft(structuredClone(result.questions), {
      kind: "success", message: "Saved. These questions will be used in the next run.",
    }, {activeQuestion: state.questionEditor.activeQuestion, dirty: false});
  } catch (err) {
    questionDraftError(err);
  }
}

function renderQuiz(t) {
  const editor = state.questionEditor && state.questionEditor.task === t.name
    ? state.questionEditor : null;
  if (editor) return renderQuestionEditor(t, editor);
  const editButton = questionEditingEnabled(t) ? html`<div class="qedit-launch">
    <span>${t.questions.length
      ? "Review and update the quiz questions here."
      : "Add at least one quiz question so runs can be graded."}</span>
    <button type="button" data-action="edit-questions">${t.questions.length
      ? "Edit questions" : "Add questions"}</button>
  </div>` : "";
  if (!t.questions.length) return html`${editButton}${emptyState("No questions.")}`;
  return html`${editButton}${t.questions.map((q, index) => {
    const expanded = state.openQuizQuestion === q.id;
    return html`<div class="qcard qview-card ${expanded ? "qview-open" : ""}" id="q-${q.id}">
      <button type="button" class="qview-summary" data-action="toggle-quiz-question"
        data-question="${q.id}" aria-expanded="${expanded}">
        <span class="qview-number">Question ${index + 1}</span>
        <span class="qview-text">${q.question}</span>
        <span class="badge badge-${q.type}">${questionTypeLabel(q.type)}</span>
        <span class="qview-chevron" aria-hidden="true">⌄</span>
      </button>
      <div class="qview-body" ${expanded ? "" : "hidden"}>
        ${questionBody(q)}
        ${questionEvidence(q)}
      </div>
    </div>`;
  })}`;
}
