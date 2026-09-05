"use strict";

// Keep the secret launch token in this tab, not in the visible URL.
const sessionToken = location.hash.slice(1)
  || sessionStorage.getItem("tcs-prover-token") || "";
if (location.hash) {
  sessionStorage.setItem("tcs-prover-token", sessionToken);
  history.replaceState(null, "", location.pathname + location.search);
}
let currentJob = new URLSearchParams(location.search).get("job") || "";

// Cache the small set of elements that the state renderer controls.
const $ = (id) => document.getElementById(id);
const ui = {
  input: $("inputPanel"), review: $("reviewPanel"), run: $("runPanel"),
  notice: $("notice"), problem: $("problem"), proposed: $("proposed"),
  introDescription: $("introDescription"),
  statementFields: $("statementFields"),
  latexFields: $("latexFields"), latexInput: $("latexInput"),
  feedback: $("feedback"), notes: $("notes"), editHint: $("editHint"),
  reviewModel: $("reviewModel"), authorModel: $("authorModel"),
  criticModel: $("criticModel"), writerModel: $("writerModel"),
  reviewModelSetting: $("reviewModelSetting"),
  authorModelSetting: $("authorModelSetting"),
  criticModelSetting: $("criticModelSetting"),
  writerModelSetting: $("writerModelSetting"),
  reviewEffort: $("reviewEffort"), authorEffort: $("authorEffort"),
  criticEffort: $("criticEffort"), writerEffort: $("writerEffort"),
  criticRounds: $("criticRounds"),
  thinkingHours: $("thinkingHours"),
  speedMode: $("speedMode"),
  reasoningSummary: $("reasoningSummary"),
  skipReviewSetting: $("skipReviewSetting"),
  skipStatementReview: $("skipStatementReview"),
  reviewOnlySetting: $("reviewOnlySetting"),
  statementReviewOnly: $("statementReviewOnly"),
  speedModeSetting: $("speedModeSetting"),
  criticRoundSetting: $("criticRoundSetting"),
  thinkingHoursSetting: $("thinkingHoursSetting"),
  editPrompts: $("editPromptsButton"), promptDialog: $("promptDialog"),
  promptTabs: $("promptTabs"), promptEditor: $("promptEditor"),
  promptEditorLabel: $("promptEditorLabel"),
  promptEditorHelp: $("promptEditorHelp"),
  resetPrompt: $("resetPromptButton"), savePrompts: $("savePromptsButton"),
  reviewPromptTab: $("reviewPromptTab"),
  authorPromptTab: $("authorPromptTab"),
  criticPromptTab: $("criticPromptTab"),
  finalPromptTab: $("finalPromptTab"),
  homeLink: $("homeLink"), home: $("homeButton"), reviewHome: $("reviewHomeButton"),
  jobsPanel: $("jobsPanel"), jobsList: $("jobsList"), jobsCount: $("jobsCount"),
  check: $("checkButton"), recheck: $("recheckButton"), approve: $("approveButton"),
  stop: $("stopButton"),
  authorTimeLimitControl: $("authorTimeLimitControl"),
  authorLimitSummary: $("authorLimitSummary"),
  authorLimitHours: $("authorLimitHours"),
  setAuthorTimeLimit: $("setAuthorTimeLimitButton"),
  authorSteerControl: $("authorSteerControl"),
  authorSteerInstruction: $("authorSteerInstruction"),
  sendAuthorSteer: $("sendAuthorSteerButton"),
  runLabel: $("runLabel"), runTitle: $("runTitle"),
  runDescription: $("runDescription"), roundBadge: $("roundBadge"),
  globalStatus: $("globalStatus"), liveDot: $("liveDot"), elapsed: $("elapsed"),
  modelSummary: $("modelSummary"),
  lastActivity: $("lastActivity"), timeline: $("timelineList"),
  jump: $("jumpLatest"), filters: $("filters"),
  workflowRail: $("workflowRail"), workflowNodes: $("workflowNodes"),
  activityToggle: $("activityToggle"), activityPanel: $("activityPanel"),
  activityClose: $("activityClose"),
  drawerScrim: $("drawerScrim"), activityList: $("activityList"),
  reviewEyebrow: $("reviewEyebrow"), reviewHeading: $("reviewHeading"),
  reviewDescription: $("reviewDescription"),
  reviewFeedbackControls: $("reviewFeedbackControls"),
};
ui.problemModes = document.querySelectorAll('input[name="problemMode"]');

let state = {
  phase: "input", problemMode: "statement", skipStatementReview: false,
  statementReviewOnly: false,
  trace: [], traceVersion: 0,
  workflow: { nodes: {}, edges: [] },
};
let previousPhase = "";
let timer;
let clock;
let jobsTimer;
let requests = Promise.resolve();
let reviewPending = false;
let activeFilter = "all";
let activePrompt = "review";
let promptValues = {};
let promptDrafts = {};
let promptStorageFallback = null;
const promptStorageKey = "tcs-prover-role-prompts";
const timelineRows = new Map();
const detailRows = new Map();
const pinnedKinds = new Set([
  "request", "review_result", "critic_result", "author_result",
  "final_result", "failure_result", "partial_result", "diagnostic", "error",
]);

// Serialize requests so a slow poll cannot overwrite a newer action.
function request(path, body) {
  const run = async () => {
    const options = { headers: { "X-TCS-Prover-Token": sessionToken } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed.");
    return data;
  };
  const result = requests.then(run, run);
  requests = result.catch(() => {});
  return result;
}

// Every job action carries its run id; home actions deliberately carry none.
function jobPath(path, values = {}) {
  const query = new URLSearchParams(values);
  if (currentJob) query.set("job", currentJob);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function show(element, visible) {
  element.hidden = !visible;
}

function jobUrl(runId, token = false) {
  const hash = token ? `#${sessionToken}` : "";
  return `${location.pathname}?job=${encodeURIComponent(runId)}${hash}`;
}

function clearJobView() {
  previousPhase = "";
  state = {
    phase: "input", problemMode: "statement", skipStatementReview: false,
    statementReviewOnly: false,
    trace: [], traceVersion: 0,
    workflow: { nodes: {}, edges: [] },
  };
  timelineRows.clear();
  detailRows.clear();
  ui.timeline.replaceChildren();
  ui.activityList.replaceChildren();
}

function selectJob(runId) {
  currentJob = runId;
  history.pushState(null, "", jobUrl(runId));
  clearJobView();
  clearTimeout(jobsTimer);
  refresh();
}

async function deleteJob(job, title) {
  const question = `Delete “${title}”?\n\nIts files will move to runs/.trash/.`;
  if (!confirm(question)) return;
  try {
    const result = await request(
      `/delete-job?job=${encodeURIComponent(job.runId)}`, {}
    );
    if (result.deleted !== job.runId) throw new Error("Deletion was not confirmed.");
  } catch (error) {
    const message = `${error.message}\n\nRestart TCS Prover if it was already open `
      + "when the Delete button was added.";
    ui.notice.textContent = message;
    show(ui.notice, true);
    alert(message);
    return;
  }
  const card = [...ui.jobsList.children].find(
    (item) => item.dataset.job === job.runId
  );
  card?.remove();
  const remaining = ui.jobsList.children.length;
  ui.jobsCount.textContent = `${remaining} ${remaining === 1 ? "job" : "jobs"}`;
  show(ui.jobsPanel, remaining > 0);
  loadJobs();
}

async function resumeCritic(job, title) {
  const question = `Start a new critic job from the complete saved proof for “${title}”?`;
  if (!confirm(question)) return;
  try {
    const next = await request(
      `/resume-critic?job=${encodeURIComponent(job.runId)}`, {}
    );
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    clearTimeout(jobsTimer);
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function resumeCheckpoint(job, checkpoint, title) {
  const question = `Continue “${title}” from checkpoint “${checkpoint.label}”?\n\n`
    + "The selected checkpoint will be copied into a new job.";
  if (!confirm(question)) return;
  try {
    const next = await request(
      `/resume-checkpoint?job=${encodeURIComponent(job.runId)}`,
      { checkpoint: checkpoint.id },
    );
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    clearTimeout(jobsTimer);
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function continueStopped(job, title) {
  const detail = job.continueStoppedDescription || "Restarts from saved state.";
  const settingsWarning = job.settingsWarning
    ? `\n\nLegacy settings warning: ${job.settingsWarning}` : "";
  const question = `${job.continueStoppedLabel || "Continue stopped job"} for “${title}”?\n\n`
    + `${detail}${settingsWarning}\n\nA new job will be created. The stopped source job will not be changed, `
    + "and an interrupted model response or private reasoning cannot be resumed.";
  if (!confirm(question)) return;
  try {
    const next = await request(
      `/continue-stopped?job=${encodeURIComponent(job.runId)}`, {}
    );
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    clearTimeout(jobsTimer);
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function goHome() {
  currentJob = "";
  history.pushState(null, "", location.pathname);
  clearTimeout(timer);
  clearInterval(clock);
  clearJobView();
  await refresh();
}

function renderJobs(jobs) {
  ui.jobsList.replaceChildren();
  ui.jobsCount.textContent = `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}`;
  show(ui.jobsPanel, jobs.length > 0);
  for (const job of jobs) {
    const item = document.createElement("li");
    item.className = "job-card";
    item.dataset.job = job.runId;
    const copy = document.createElement("div");
    copy.className = "job-copy";
    const title = document.createElement("strong");
    title.textContent = job.title || job.draft?.trim().split("\n")[0]
      || "Untitled problem";
    const status = document.createElement("span");
    status.className = `job-status ${job.phase}`;
    const labels = {
      reviewing: "Checking statement", reviewed: "Waiting for approval",
      running: "Running", stopping: "Stopping", done: "Finished",
    };
    status.textContent = job.manuallyStopped
      ? `Stopped at ${job.stoppedStage || "saved stage"}`
      : labels[job.phase] || job.phase;
    const times = document.createElement("small");
    times.className = "job-times";
    const localTime = (value) => value
      ? new Date(value).toLocaleString([], {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      }) : "—";
    const finished = job.finishedAt
      || (job.phase === "done" ? job.lastActivityAt : "");
    times.textContent = `Started: ${localTime(job.startedAt)} · `
      + `Finished: ${finished ? localTime(finished) : "Not finished"}`;
    copy.append(title, status, times);
    const checkpoints = job.checkpoints || [];
    if (checkpoints.length) {
      const checkpointHeading = document.createElement("span");
      checkpointHeading.className = "checkpoint-heading";
      checkpointHeading.textContent = `Checkpoints (${checkpoints.length})`;
      const checkpointList = document.createElement("ol");
      checkpointList.className = "checkpoint-list";
      for (const checkpoint of checkpoints) {
        const checkpointItem = document.createElement("li");
        checkpointItem.className = `checkpoint-row ${checkpoint.status || "ready"}`;
        const checkpointCopy = document.createElement("div");
        const checkpointLabel = document.createElement("span");
        checkpointLabel.className = "checkpoint-label";
        checkpointLabel.textContent = checkpoint.label;
        const checkpointMeta = document.createElement("small");
        checkpointMeta.className = "checkpoint-meta";
        checkpointMeta.textContent = `${checkpoint.status || "ready"} · `
          + localTime(checkpoint.completedAt);
        const checkpointDescription = document.createElement("small");
        checkpointDescription.className = "checkpoint-description";
        checkpointDescription.textContent = checkpoint.description || "";
        checkpointCopy.append(
          checkpointLabel, checkpointMeta, checkpointDescription,
        );
        checkpointItem.append(checkpointCopy);
        if (checkpoint.resumable) {
          const continueButton = document.createElement("button");
          continueButton.className = "secondary compact checkpoint-resume";
          continueButton.textContent = checkpoint.resumeLabel || "Continue";
          continueButton.disabled = ["reviewing", "running", "stopping"]
            .includes(job.phase);
          continueButton.onclick = () => resumeCheckpoint(
            job, checkpoint, title.textContent,
          );
          checkpointItem.append(continueButton);
        }
        checkpointList.append(checkpointItem);
      }
      copy.append(checkpointHeading, checkpointList);
    }
    const actions = document.createElement("div");
    actions.className = "job-actions";
    const open = document.createElement("button");
    open.className = "secondary compact";
    open.textContent = "Open";
    open.onclick = () => selectJob(job.runId);
    const separate = document.createElement("button");
    separate.className = "ghost compact";
    separate.textContent = "Open in new window";
    separate.onclick = () => window.open(jobUrl(job.runId, true), "_blank", "noopener");
    const resume = document.createElement("button");
    resume.className = "secondary compact";
    resume.textContent = "Resume at critic";
    resume.hidden = !job.canResumeCritic || checkpoints.length > 0;
    resume.onclick = () => resumeCritic(job, title.textContent);
    const continueButton = document.createElement("button");
    continueButton.className = "secondary compact";
    continueButton.textContent = job.continueStoppedLabel
      || "Continue stopped job";
    continueButton.hidden = !job.canContinueStopped;
    continueButton.title = job.continueStoppedDescription || "";
    continueButton.onclick = () => continueStopped(job, title.textContent);
    const remove = document.createElement("button");
    remove.className = "ghost compact job-delete";
    remove.textContent = "Delete";
    remove.disabled = ["reviewing", "running", "stopping"].includes(job.phase);
    remove.title = remove.disabled ? "Stop this job before deleting it." : "";
    remove.onclick = () => deleteJob(job, title.textContent);
    actions.append(open, separate, continueButton, resume, remove);
    item.append(copy, actions);
    ui.jobsList.append(item);
  }
}

async function loadJobs() {
  clearTimeout(jobsTimer);
  if (currentJob) return;
  try {
    const result = await request("/jobs");
    if (!currentJob) renderJobs(result.jobs || []);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
  if (!currentJob) jobsTimer = setTimeout(loadJobs, 1500);
}

function selectedProblemMode() {
  return [...ui.problemModes].find((input) => input.checked)?.value || "statement";
}

function setProblemMode(mode) {
  const latexOnly = mode === "latex";
  const statement = !latexOnly;
  mode = latexOnly ? "latex" : "statement";
  const reviewOnly = statement && ui.statementReviewOnly.checked;
  if (reviewOnly) ui.skipStatementReview.checked = false;
  const skipReview = statement && !reviewOnly
    && ui.skipStatementReview.checked;
  for (const input of ui.problemModes) input.checked = input.value === mode;
  show(ui.statementFields, statement);
  show(ui.latexFields, latexOnly);
  show(ui.reviewModelSetting, statement && !skipReview);
  show(ui.authorModelSetting, !latexOnly && !reviewOnly);
  show(ui.criticModelSetting, !latexOnly && !reviewOnly);
  show(ui.writerModelSetting, !reviewOnly);
  show(ui.reviewPromptTab, statement && !skipReview);
  show(ui.authorPromptTab, !latexOnly && !reviewOnly);
  show(ui.criticPromptTab, !latexOnly && !reviewOnly);
  show(ui.finalPromptTab, !reviewOnly);
  show(ui.skipReviewSetting, statement);
  show(ui.reviewOnlySetting, statement);
  show(ui.criticRoundSetting, !latexOnly && !reviewOnly);
  show(ui.thinkingHoursSetting, !latexOnly && !reviewOnly);
  ui.problem.required = statement;
  ui.latexInput.required = latexOnly;
  ui.check.textContent = latexOnly ? "Polish LaTeX"
    : reviewOnly ? "Review statement only"
    : skipReview ? "Start proof author" : "Check statement";
  ui.introDescription.textContent = latexOnly
    ? "Provide an existing writing. Only the final LaTeX editor will run."
    : reviewOnly
      ? "Check and rewrite the statement, save the result and reviewer notes, then "
        + "stop without starting the proof author."
    : skipReview
      ? "Enter the exact statement to send directly to the proof author, followed "
        + "by independent audit and LaTeX editing."
      : "Start with a rough TCS problem. The agent will clarify it, ask for approval, "
        + "solve it, audit it, and produce clean LaTeX.";
  if (reviewOnly && activePrompt !== "review" && ui.promptDialog.open) {
    selectPrompt("review");
  } else if (skipReview && activePrompt === "review" && ui.promptDialog.open) {
    selectPrompt("author");
  }
  if (latexOnly && activePrompt !== "final" && ui.promptDialog.open) {
    selectPrompt("final");
  }
  updateModelSummary();
}

// Keep the compact footer label synchronized with every model setting.
function updateModelSummary() {
  const deepseekModel = "deepseek-v4-pro";
  const name = (model) => model === "deepseek-v4-pro" ? "DeepSeek V4 Pro"
    : model.split("-").at(-1).replace(/^./, (letter) => letter.toUpperCase());
  const effectiveEffort = (model, effort) => model === deepseekModel
    ? (["low", "medium", "high"].includes(effort) ? "high" : "max")
    : effort;
  const role = (model, effort) => `${name(model)}/${name(
    effectiveEffort(model, effort)
  )}`;
  const mode = selectedProblemMode();
  const reviewOnly = mode === "statement" && ui.statementReviewOnly.checked;
  let selectedModels = reviewOnly ? [ui.reviewModel.value]
    : mode === "latex" ? [ui.writerModel.value]
    : [ui.authorModel.value, ui.criticModel.value, ui.writerModel.value];
  if (mode === "statement" && !reviewOnly && !ui.skipStatementReview.checked) {
    selectedModels.push(ui.reviewModel.value);
  }
  const review = (
    ui.skipStatementReview.checked
  ) ? ""
    : `${role(ui.reviewModel.value, ui.reviewEffort.value)} review · `;
  const speed = ui.speedMode.value === "standard"
    ? "Standard speed" : selectedModels.includes(deepseekModel)
      ? "Fast for ChatGPT · Standard for DeepSeek" : "Fast 1.5×";
  const log = {
    none: "Status-only log",
    concise: "Concise activity log",
    detailed: "Detailed activity log",
  }[ui.reasoningSummary.value] || "Concise activity log";
  if (reviewOnly) {
    ui.modelSummary.textContent = `${speed} · ${log} · `
      + `${role(ui.reviewModel.value, ui.reviewEffort.value)} review only`;
    return;
  }
  if (mode === "latex") {
    ui.modelSummary.textContent = `${speed} · ${log} · `
      + `${role(ui.writerModel.value, ui.writerEffort.value)} writer`;
    return;
  }
  ui.modelSummary.textContent = `${speed} · ${log} · ` + review
    + `${role(ui.authorModel.value, ui.authorEffort.value)} author · `
    + `${role(ui.criticModel.value, ui.criticEffort.value)} critic · `
    + `${role(ui.writerModel.value, ui.writerEffort.value)} writer`;
}

const promptLabels = {
  review: "Reviewer prompt", author: "Author prompt",
  critic: "Critic prompt", final: "Final writer prompt",
};

const promptHelp = {
  review: "The full request also includes the statement and any revision feedback.",
  author: "Keep exactly one [STATEMENT]. The workflow inserts the statement and adds proof-history instructions.",
  critic: "The workflow adds the proof, audit assignments, and completed audit reports to these instructions.",
  final: "The full request also includes the statement and proof to polish.",
};

function syncPrompts(source = state) {
  const defaults = source.workflow?.settings?.prompts || {};
  let saved = {};
  if (source.phase === "input") {
    try {
      saved = promptStorageFallback
        || JSON.parse(localStorage.getItem(promptStorageKey) || "{}");
    } catch (_) {
      saved = {};
    }
  }
  const savedText = (name) => typeof saved?.[name] === "string"
    ? saved[name] : "";
  promptValues = {
    review: savedText("review") || source.reviewPrompt || defaults.review || "",
    author: savedText("author") || source.authorPrompt || defaults.author || "",
    critic: savedText("critic") || source.criticPrompt || defaults.critic || "",
    final: savedText("final") || source.finalPrompt || defaults.final || "",
  };
}

function updatePromptHelp() {
  const defaults = state.workflow?.settings?.prompts || {};
  const isDefault = ui.promptEditor.value.trim() === (defaults[activePrompt] || "").trim();
  ui.promptEditorLabel.textContent = `${promptLabels[activePrompt]} — `
    + (isDefault ? "current default" : "saved or edited prompt");
  ui.promptEditorHelp.textContent = (isDefault ? "" :
    "This overrides the current default. Reset restores the default; Save applies it. ")
    + promptHelp[activePrompt];
}

function selectPrompt(name) {
  if (ui.promptDialog.open && promptDrafts[activePrompt] !== undefined) {
    promptDrafts[activePrompt] = ui.promptEditor.value;
  }
  activePrompt = name;
  ui.promptEditor.value = promptDrafts[name] || "";
  updatePromptHelp();
  for (const tab of ui.promptTabs.querySelectorAll(".prompt-tab")) {
    const selected = tab.dataset.prompt === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
  }
}

function openPromptEditor() {
  if (!promptValues.review) syncPrompts();
  promptDrafts = { ...promptValues };
  activePrompt = selectedProblemMode() === "latex" ? "final"
    : ui.skipStatementReview.checked ? "author" : "review";
  selectPrompt(activePrompt);
  ui.promptDialog.showModal();
}

function savePrompts() {
  promptDrafts[activePrompt] = ui.promptEditor.value;
  if (Object.values(promptDrafts).some((prompt) => !prompt.trim())) {
    ui.notice.textContent = "Every role prompt must contain instructions.";
    show(ui.notice, true);
    return;
  }
  if ((promptDrafts.author.match(/\[STATEMENT\]/g) || []).length !== 1) {
    ui.notice.textContent =
      "The author prompt must contain exactly one [STATEMENT].";
    show(ui.notice, true);
    return;
  }
  promptValues = Object.fromEntries(
    Object.entries(promptDrafts).map(([name, prompt]) => [name, prompt.trim()])
  );
  const defaults = state.workflow?.settings?.prompts || {};
  // Persist only custom text so saving one role does not freeze every default.
  // Legacy saved prompts remain available until the user explicitly resets them.
  const overrides = Object.fromEntries(Object.entries(promptValues).filter(
    ([name, prompt]) => prompt !== (defaults[name] || "").trim()
  ));
  try {
    localStorage.setItem(promptStorageKey, JSON.stringify(overrides));
    promptStorageFallback = null;
  } catch (_) {
    // Keep saved customizations across jobs in this tab when storage is blocked.
    promptStorageFallback = overrides;
  }
  ui.notice.textContent = "";
  show(ui.notice, false);
  ui.promptDialog.close();
}

function clockText(date) {
  return date
    ? new Date(date).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    })
    : "";
}

function elapsedText(start) {
  if (!start) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(start)) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${String(rest).padStart(2, "0")}s`;
}

function nodeFromStage(stage) {
  return Object.entries(state.workflow?.nodes || {})
    .find(([, item]) => (item.stages || [item.stage]).includes(stage))?.[0] || "";
}

// Preserve prompts, results, errors, and completed root-model answers.
function importantEntry(entry) {
  if (pinnedKinds.has(entry.kind)) return true;
  if (entry.kind !== "codex_event" || entry.root === false) return false;
  const event = entry.event || {};
  const item = event.params?.item || event.item || {};
  return ["item/completed", "item.completed"].includes(event.method || event.type)
    && ["agentMessage", "agent_message"].includes(item.type);
}

// Bound routine events while retaining every important event in order.
function retainTrace(entries) {
  const recent = Math.max(0, entries.length - 1500);
  return entries.filter((entry, index) => index >= recent || importantEntry(entry));
}

// Convert one raw record into one useful live-timeline card.
function describe(entry) {
  const event = entry.event || {};
  const name = event.method || event.type || entry.kind || "";
  const params = event.params || {};
  const item = params.item || event.item || {};
  const activityLabel = entry.activityLabel || "";
  const rawItemId = params.itemId || item.id || "";
  const itemId = activityLabel && rawItemId
    ? `${activityLabel}:${rawItemId}` : rawItemId;
  const root = entry.root !== false;
  const keyBase = itemId || `${entry.time}:${entry.kind}:${name}`;
  const text = entry.text || "";
  const scopedLabel = (label) => activityLabel
    ? `${activityLabel} · ${label}` : label;
  const turnKey = `${activityLabel ? `${activityLabel}:` : ""}`
    + `${params.turn?.id || event.turn_id || entry.time}`;

  if (entry.kind === "request") {
    return {
      key: `prompt:${keyBase}`, type: "prompt", label: entry.label || "Prompt sent",
      text, time: entry.time, details: true, replace: true, pinned: true,
    };
  }
  if (entry.kind === "critic_result") {
    const report = entry.report || {};
    return {
      key: `critic:${entry.round || entry.time}`, type: "agent",
      label: `${entry.label || "Critic result"} · ${report.verdict || "returned"}`,
      text: report.fixed
        ? "The critic repaired every reported issue. Recheck below the round limit; "
          + "at the limit, proceed to LaTeX editing."
        : (report.bugs || "No repair was needed."),
      time: entry.time, checks: report.checks || [], replace: true, pinned: true,
    };
  }
  if (
    [
      "review_result", "author_result", "final_result",
      "failure_result", "partial_result",
    ]
      .includes(entry.kind)
  ) {
    return {
      key: `result:${keyBase}`, type: "agent", label: entry.label || "Agent result",
      text: ["final_result", "failure_result", "partial_result"].includes(entry.kind)
        ? entry.output : text,
      time: entry.time, replace: true, pinned: true,
    };
  }
  if (entry.kind === "status") {
    return {
      key: `status:${entry.label || keyBase}`, type: "status",
      label: entry.label || "Status", text, time: entry.time, replace: true,
    };
  }
  if (entry.kind === "diagnostic" || entry.kind === "error") {
    return {
      key: `error:${keyBase}`, type: "error", label: "Diagnostic",
      text, time: entry.time, replace: true, pinned: true,
    };
  }
  if (entry.kind !== "codex_event") return null;

  if (["turn/started", "turn.started"].includes(name)) {
    return {
      key: `turn:${turnKey}`,
      type: "status", label: scopedLabel(
        root ? "Model request running" : "Subagent turn running",
      ),
      text: "The request has started and is waiting for the model's next public event.",
      time: entry.time, replace: true,
    };
  }
  if (
    ["item/started", "item.started"].includes(name)
    && item.type === "reasoning"
  ) {
    const summaryLevel = state.reasoningSummary || "concise";
    const summaryMessage = summaryLevel === "none"
      ? "Status-only logging is selected."
      : `${summaryLevel === "detailed" ? "Detailed" : "Concise"} public `
        + "summaries will appear when the provider returns them.";
    return {
      key: `reasoning:${itemId}`, type: "reasoning",
      label: scopedLabel(root ? "Model is thinking" : "Subagent is thinking"),
      text: `Reasoning has started. ${summaryMessage} `
        + "Private chain-of-thought is not displayed.",
      time: entry.time, replace: true,
    };
  }
  if (name === "item/reasoning/summaryTextDelta") {
    return {
      key: `reasoning:${itemId}`, type: "reasoning",
      label: scopedLabel(root ? "Reasoning summary" : "Subagent reasoning"),
      text: params.delta || "", time: entry.time, append: true,
    };
  }
  if (["item/completed", "item.completed"].includes(name) && item.type === "reasoning") {
    return {
      key: `reasoning:${itemId}`, type: "reasoning",
      label: scopedLabel(root ? "Reasoning summary" : "Subagent reasoning"),
      text: item.text || (item.summary || []).join("\n")
        || "Reasoning step completed; no public summary was returned.",
      time: entry.time, replace: true,
    };
  }
  if (name === "item/agentMessage/delta") {
    return {
      key: `agent:${itemId}`, type: "agent",
      label: scopedLabel(root ? "Author" : "Subagent"), text: params.delta || "",
      time: entry.time, append: true,
    };
  }
  if (
    ["item/completed", "item.completed"].includes(name)
    && ["agentMessage", "agent_message"].includes(item.type)
  ) {
    return {
      key: `agent:${itemId}`, type: "agent",
      label: scopedLabel(root ? "Author" : "Subagent"), text: item.text || "",
      time: entry.time, replace: true, pinned: root,
    };
  }

  const toolTypes = new Set([
    "collabAgentToolCall", "collab_agent_tool_call", "subAgentActivity",
    "sub_agent_activity", "commandExecution", "command_execution",
    "fileChange", "file_change", "mcpToolCall", "mcp_tool_call",
    "dynamicToolCall", "webSearch", "web_search",
  ]);
  if (toolTypes.has(item.type)) {
    const action = item.tool || item.name || item.command || item.type;
    const status = item.status || (name.includes("completed") ? "completed" : "started");
    return {
      key: `tool:${itemId || keyBase}`, type: "tool",
      label: scopedLabel(root ? "Tool activity" : "Subagent tool"),
      text: `${action} · ${status}`, time: entry.time, replace: true,
    };
  }
  if (name === "thread/goal/updated") {
    const goal = params.goal || {};
    return {
      key: "goal-status", type: "status", label: "Goal status",
      text: goal.status || "updated", time: entry.time, replace: true,
    };
  }
  if (["turn/completed", "turn.completed"].includes(name)) {
    return {
      key: `turn:${turnKey}`, type: "status",
      label: scopedLabel("Turn completed"),
      text: params.turn?.status || event.status || "completed",
      time: entry.time, replace: true,
    };
  }
  return null;
}

function cardIcon(type) {
  return {
    reasoning: "R", agent: "A", tool: "T", prompt: "P",
    error: "!", status: "·",
  }[type] || "·";
}

// Render Codex's **bold** Markdown safely without accepting arbitrary HTML.
function appendFormattedText(element, value) {
  for (const part of String(value).split(/(\*\*[^*]+\*\*)/g)) {
    if (part.startsWith("**") && part.endsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = part.slice(2, -2);
      element.append(strong);
    } else {
      element.append(document.createTextNode(part));
    }
  }
}

// Update only the affected timeline row; never rebuild the full transcript.
function upsertTimeline(card) {
  if (!card || (!card.text && !card.checks?.length)) return;
  let row = timelineRows.get(card.key);
  const nearBottom = ui.timeline.scrollHeight - ui.timeline.scrollTop
    - ui.timeline.clientHeight < 70;
  if (!row) {
    row = document.createElement("li");
    row.className = `timeline-entry ${card.type}`;
    row.dataset.type = card.type;
    row.dataset.key = card.key;
    timelineRows.set(card.key, row);
    ui.timeline.append(row);
  }
  const old = row._card;
  card.text = card.append && old ? old.text + card.text : card.text;
  row._card = card;
  row.dataset.pinned = String(Boolean(card.pinned || old?.pinned));
  row.replaceChildren();

  const icon = document.createElement("span");
  icon.className = "entry-icon";
  icon.textContent = cardIcon(card.type);
  const content = document.createElement("div");
  const head = document.createElement("div");
  head.className = "entry-head";
  const label = document.createElement("strong");
  label.textContent = card.label;
  const time = document.createElement("time");
  time.dateTime = card.time || "";
  time.textContent = clockText(card.time);
  head.append(label, time);
  content.append(head);

  const body = document.createElement(card.details ? "details" : "div");
  if (card.details) {
    body.className = "entry-details";
    const summary = document.createElement("summary");
    summary.textContent = "Show exact prompt";
    body.append(summary);
  }
  const copy = document.createElement("pre");
  copy.className = "entry-body";
  appendFormattedText(copy, card.text);
  body.append(copy);
  content.append(body);

  if (card.checks?.length) {
    const checks = document.createElement("div");
    checks.className = "critic-checks";
    for (const check of card.checks) {
      const item = document.createElement("div");
      item.className = "critic-check";
      const verdict = document.createElement("b");
      verdict.className = check.verdict;
      verdict.textContent = check.verdict;
      const focus = document.createElement("span");
      focus.textContent = check.focus;
      const report = document.createElement("p");
      appendFormattedText(report, check.report);
      item.append(verdict, focus, report);
      checks.append(item);
    }
    content.append(checks);
  }
  row.append(icon, content);
  row.hidden = activeFilter !== "all" && card.type !== activeFilter;

  while (ui.timeline.children.length > 300) {
    const disposable = [...ui.timeline.children].find(
      (item) => item.dataset.pinned !== "true"
    );
    if (!disposable) break;
    timelineRows.delete(disposable.dataset.key);
    disposable.remove();
  }
  if (nearBottom) ui.timeline.scrollTop = ui.timeline.scrollHeight;
}

// Keep only exact application prompts and root-model response text.
function detailCard(entry) {
  if (entry.kind === "request" && entry.text) {
    return {
      key: `detail-prompt:${entry.time}`, label: "Prompt to model",
      text: entry.text,
    };
  }
  if (
    ["review_result", "critic_result", "final_result", "failure_result"].includes(entry.kind)
    && entry.text
  ) {
    return {
      key: `detail-result:${entry.time}`, label: "Returned text from model",
      text: entry.text,
    };
  }
  if (entry.kind !== "codex_event" || entry.root === false) return null;
  const event = entry.event || {};
  const params = event.params || {};
  const item = params.item || {};
  const rawItemId = params.itemId || item.id || entry.time;
  const itemId = entry.activityLabel
    ? `${entry.activityLabel}:${rawItemId}` : rawItemId;
  if (event.method === "item/agentMessage/delta" && params.delta) {
    return {
      key: `detail-response:${itemId}`, label: "Returned text from model",
      text: params.delta, append: true,
    };
  }
  if (
    event.method === "item/completed"
    && ["agentMessage", "agent_message"].includes(item.type)
    && item.text
  ) {
    return {
      key: `detail-response:${itemId}`, label: "Returned text from model",
      text: item.text,
    };
  }
  return null;
}

function upsertDetail(card) {
  if (!card) return;
  let row = detailRows.get(card.key);
  if (!row) {
    row = document.createElement("li");
    row.className = "detail-entry";
    row.dataset.key = card.key;
    detailRows.set(card.key, row);
    ui.activityList.append(row);
  }
  const oldText = row._text || "";
  row._text = card.append ? oldText + card.text : card.text;
  const label = document.createElement("strong");
  label.textContent = card.label;
  const body = document.createElement("pre");
  body.textContent = row._text;
  row.replaceChildren(label, body);
}

function ingest(entries, reset = false) {
  if (reset) {
    timelineRows.clear();
    detailRows.clear();
    ui.timeline.replaceChildren();
    ui.activityList.replaceChildren();
  }
  for (const entry of entries) {
    upsertTimeline(describe(entry));
    upsertDetail(detailCard(entry));
  }
}

// Render the critic/repair cycle as a real loop, not five linear steps.
function renderWorkflow() {
  const nodes = state.workflow?.nodes || {};
  const latexOnly = state.problemMode === "latex";
  const criticResume = state.problemMode === "critic-resume";
  const startsAtAuthor = state.problemMode === "algorithmic"
    || state.skipStatementReview;
  const seenInThisJob = new Set((state.trace || []).map(
    (entry) => entry.node || nodeFromStage(entry.stage)
  ));
  const seen = new Set(seenInThisJob);
  if (criticResume) {
    seen.add("statement_reviewer");
    seen.add("author");
  }
  const makeNode = (name, number) => {
    const item = nodes[name];
    const row = document.createElement("li");
    const active = state.phase !== "done" && state.activeNode === name;
    const failed = state.phase === "done" && state.activeNode === name
      && (state.error || name === "failure_summary");
    const status = active ? "active" : failed ? "failed"
      : seen.has(name) ? "complete" : "";
    row.className = `workflow-node ${status}`;
    row.dataset.node = name;
    if (active) row.setAttribute("aria-current", "step");
    const dot = document.createElement("span");
    dot.className = "node-dot";
    dot.textContent = failed ? "!" : seen.has(name) && !active ? "✓" : number;
    const copy = document.createElement("div");
    copy.className = "node-copy";
    const title = document.createElement("strong");
    title.textContent = item.label;
    const description = document.createElement("span");
    description.textContent = item.description;
    copy.append(title, description);
    if (name === "critic" && state.round) {
      const round = document.createElement("span");
      round.className = "node-round";
      round.textContent = `Round ${state.round} of ${state.criticRounds}`;
      copy.append(round);
    }
    if (criticResume && (name === "statement_reviewer"
      || (name === "author" && !seenInThisJob.has("author")))) {
      const loaded = document.createElement("span");
      loaded.className = "node-resume-note";
      loaded.textContent = "Loaded from the source job";
      copy.append(loaded);
    }
    if (name === "failure_summary") {
      const condition = document.createElement("span");
      condition.className = "failure-condition";
      condition.textContent = "At total time limit";
      copy.append(condition);
    }
    row.append(dot, copy);
    return row;
  };
  if (state.statementReviewOnly) {
    ui.workflowNodes.replaceChildren(makeNode("statement_reviewer", "1"));
    return;
  }
  if (latexOnly) {
    const editor = makeNode("latex_editor", "1");
    ui.workflowNodes.replaceChildren(editor);
    return;
  }
  const arrow = (text, pass = false) => {
    const row = document.createElement("li");
    row.className = `flow-arrow${pass ? " pass" : ""}`;
    row.textContent = `${text} ↓`;
    return row;
  };

  const loop = document.createElement("li");
  loop.className = "workflow-loop";
  const loopTitle = document.createElement("strong");
  loopTitle.className = "loop-title";
  loopTitle.textContent = "Repeat until accepted";
  const loopNodes = document.createElement("ol");
  loopNodes.className = "loop-nodes";
  const candidateRoute = document.createElement("li");
  candidateRoute.className = "loop-forward";
  candidateRoute.textContent = "Candidate or revised proof ↓";
  const failureNode = makeNode("failure_summary", "!");
  failureNode.classList.add("failure-branch");
  const failureRoute = document.createElement("li");
  failureRoute.className = "failure-route";
  failureRoute.setAttribute(
    "aria-label",
    "At the total time limit, an active author stops; an active critic finishes, but rejection returns a failure summary",
  );
  const rejectRoute = document.createElement("li");
  rejectRoute.className = "loop-back";
  rejectRoute.setAttribute(
    "aria-label",
    `On rejection, step ${startsAtAuthor ? 2 : 3} returns unresolved bugs to step ${startsAtAuthor ? 1 : 2}`,
  );
  const rejectLabel = document.createElement("span");
  rejectLabel.textContent = "REJECT";
  rejectRoute.append(rejectLabel);
  const selfRoute = document.createElement("li");
  selfRoute.className = "loop-self";
  selfRoute.textContent = "↻ Critic fixes → recheck below limit";
  const author = makeNode("author", startsAtAuthor ? "1" : "2");
  const critic = makeNode("critic", startsAtAuthor ? "2" : "3");
  const passStem = document.createElement("li");
  passStem.className = "critic-pass-stem";
  passStem.setAttribute("aria-hidden", "true");
  loopNodes.append(
    author, candidateRoute, critic, selfRoute, passStem, rejectRoute,
  );
  loop.append(loopTitle, loopNodes);

  // Failure branches left; accepted proofs run directly from critic to editor.
  const branch = document.createElement("li");
  branch.className = "workflow-branch";
  const passRoute = arrow("PASS or all fixes at limit", true);
  passRoute.classList.add("critic-pass");
  const editor = makeNode("latex_editor", startsAtAuthor ? "3" : "4");
  editor.classList.add("post-loop");
  branch.append(failureNode, failureRoute, loop, passRoute, editor);

  if (startsAtAuthor) {
    ui.workflowNodes.replaceChildren(branch);
  } else {
    const reviewer = makeNode("statement_reviewer", "1");
    reviewer.classList.add("pre-loop");
    const approved = arrow("Approved");
    approved.classList.add("pre-loop-arrow");
    ui.workflowNodes.replaceChildren(reviewer, approved, branch);
  }
}

function renderClock() {
  ui.elapsed.textContent = state.startedAt && state.phase !== "input"
    ? elapsedText(state.startedAt) : "";
  if (state.lastActivityAt) {
    const ago = Math.max(0, Math.floor(
      (Date.now() - new Date(state.lastActivityAt)) / 1000
    ));
    const working = ["reviewing", "running", "stopping"].includes(state.phase);
    const activity = working
      ? (ago < 2 ? "Working · public activity now"
        : `Still working · waiting ${ago}s for the next public event`)
      : `Updated ${ago < 2 ? "just now" : `${ago}s ago`}`;
    ui.lastActivity.textContent = `${activity} · `
      + `${state.traceVersion || 0} events recorded`;
  }
}

// Merge incremental polling state, then update the visible phase.
function render(next) {
  const incremental = next.traceFrom === state.traceVersion;
  const newEntries = next.trace || [];
  const combined = retainTrace(
    incremental ? [...(state.trace || []), ...newEntries] : newEntries
  );
  state = { ...next, trace: combined };
  ingest(newEntries, !incremental);

  const phase = state.phase;
  const working = ["reviewing", "running", "stopping"].includes(phase);
  const reviewOnlyResult = phase === "done"
    && state.statementReviewOnly && Boolean(state.review);
  const reviewReady = phase === "reviewed" || reviewOnlyResult;
  show(ui.input, phase === "input");
  show(ui.review, reviewReady);
  show(
    ui.run,
    ["reviewing", "running", "stopping", "done"].includes(phase)
      && !reviewOnlyResult,
  );
  show(ui.workflowRail, phase !== "input");
  show(ui.activityToggle, Boolean(currentJob));
  ui.notice.textContent = state.error || "";
  show(ui.notice, Boolean(state.error));

  if (phase === "input" && previousPhase !== "input") {
    const rounds = state.workflow?.settings?.critic_rounds || {};
    const hours = state.workflow?.settings?.thinking_hours || {};
    ui.problem.value = state.draft || "";
    ui.latexInput.value = state.latexInput || "";
    ui.reviewModel.value = state.reviewModel || "gpt-6-astra";
    ui.authorModel.value = state.authorModel || "gpt-6-astra";
    ui.criticModel.value = state.criticModel || "gpt-6-astra";
    ui.writerModel.value = state.writerModel || "gpt-6-astra";
    ui.reviewEffort.value = state.reviewEffort || "ultra";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "ultra";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "ultra";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "ultra";
    ui.speedMode.value = state.speedMode || "fast";
    ui.reasoningSummary.value = state.reasoningSummary || "concise";
    ui.skipStatementReview.checked = Boolean(state.skipStatementReview);
    ui.statementReviewOnly.checked = Boolean(state.statementReviewOnly);
    syncPrompts(state);
    updateModelSummary();
    ui.criticRounds.value = state.criticRounds || 2;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.thinkingHours.value = state.thinkingHours || 168;
    ui.thinkingHours.min = hours.minimum || 0.01;
    ui.thinkingHours.max = hours.maximum || 168;
    setProblemMode(state.problemMode || "statement");
  }
  if (reviewReady && (previousPhase !== phase || reviewPending)) {
    ui.reviewModel.value = state.reviewModel || "gpt-6-astra";
    ui.authorModel.value = state.authorModel || "gpt-6-astra";
    ui.criticModel.value = state.criticModel || "gpt-6-astra";
    ui.writerModel.value = state.writerModel || "gpt-6-astra";
    ui.reviewEffort.value = state.reviewEffort || "ultra";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "ultra";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "ultra";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "ultra";
    ui.speedMode.value = state.speedMode || "fast";
    ui.reasoningSummary.value = state.reasoningSummary || "concise";
    syncPrompts(state);
    updateModelSummary();
    ui.proposed.value = state.review.statement;
    ui.notes.replaceChildren();
    appendFormattedText(ui.notes, state.review.notes);
    ui.feedback.value = "";
    ui.criticRounds.value = state.criticRounds || 2;
    ui.thinkingHours.value = state.thinkingHours || 168;
    checkEdited();
    reviewPending = false;
  }

  ui.reviewEyebrow.textContent = reviewOnlyResult
    ? "REVIEW ONLY COMPLETE" : "REVIEW COMPLETE";
  ui.reviewHeading.textContent = reviewOnlyResult
    ? "Statement review complete" : "Approve the precise statement";
  ui.reviewDescription.textContent = reviewOnlyResult
    ? "The checked statement and reviewer notes are saved. No proof stages were started."
    : "Edit it directly and approve it, or explain what should change and ask for "
      + "another independent review.";
  ui.proposed.readOnly = reviewOnlyResult;
  show(ui.reviewFeedbackControls, !reviewOnlyResult);
  show(ui.recheck, !reviewOnlyResult);
  show(ui.approve, !reviewOnlyResult);

  const node = state.workflow?.nodes?.[state.activeNode] || {};
  const done = phase === "done";
  ui.liveDot.classList.toggle("active", working);
  ui.globalStatus.textContent = phase === "input" ? "Ready"
    : phase === "reviewed" ? "Waiting for approval"
      : phase === "stopping" ? "Stopping"
        : done ? "Finished" : (node.label || "Codex is working");
  ui.runLabel.textContent = done ? "RUN COMPLETE" : (node.short_label || "CODEX");
  ui.runTitle.textContent = phase === "reviewing" ? "Checking the statement…"
    : phase === "stopping" ? "Stopping safely…"
      : (state.error || (done ? (node.label || "Final result")
        : (node.label || "Codex is working")));
  ui.runDescription.textContent = state.error
    || (done ? "The output and transcript remain preserved for this job."
      : (node.description || ""));
  show(ui.roundBadge, Boolean(state.round && ["critic", "author"].includes(state.activeNode)));
  ui.roundBadge.textContent = `Round ${state.round} / ${state.criticRounds}`;
  const authorLimit = Number(state.thinkingHours || 168);
  const maximumAuthorLimit = Number(
    state.workflow?.settings?.thinking_hours?.maximum || 168
  );
  const canSetAuthorLimit = phase === "running"
    && ["solve", "repair"].includes(state.stage) && state.activeNode === "author";
  show(ui.authorSteerControl, canSetAuthorLimit);
  ui.sendAuthorSteer.disabled = !canSetAuthorLimit;
  show(ui.authorTimeLimitControl, canSetAuthorLimit);
  const authorLimitText = authorLimit.toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  ui.authorLimitSummary.textContent = `Total limit: ${authorLimitText} hours`;
  ui.authorLimitHours.max = String(maximumAuthorLimit);
  if (canSetAuthorLimit && document.activeElement !== ui.authorLimitHours) {
    ui.authorLimitHours.value = String(authorLimit);
  }
  ui.setAuthorTimeLimit.disabled = !canSetAuthorLimit;
  show(ui.stop, !done);
  ui.stop.disabled = phase === "stopping";
  ui.run.setAttribute("aria-busy", String(!done));

  renderWorkflow();
  renderClock();
  if (phase !== previousPhase) {
    const heading = reviewReady ? ui.reviewHeading
      : (phase !== "input" ? ui.runTitle : null);
    heading?.focus({ preventScroll: true });
  }
  previousPhase = phase;

  clearTimeout(timer);
  if (working) timer = setTimeout(refresh, 700);
  clearInterval(clock);
  if (working) clock = setInterval(renderClock, 1000);
}

async function refresh() {
  const job = currentJob;
  try {
    const next = await request(jobPath("/state", {
      after: state.traceVersion || 0,
    }));
    if (job !== currentJob) return;
    render(next);
    if (!currentJob) loadJobs();
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
    timer = setTimeout(refresh, 1200);
  }
}

function checkEdited() {
  if (state.statementReviewOnly) {
    ui.approve.disabled = true;
    show(ui.editHint, false);
    return;
  }
  const edited = ui.proposed.value !== state.review?.statement;
  const hasFeedback = Boolean(ui.feedback.value.trim());
  ui.approve.disabled = hasFeedback || !ui.proposed.value.trim();
  ui.editHint.textContent = hasFeedback
    ? "Retry to apply this feedback before approval."
    : "Your edits will be approved exactly as written.";
  show(ui.editHint, edited || hasFeedback);
}

async function startReview(statement, feedback = "") {
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  const reviewOnly = !feedback && ui.statementReviewOnly.checked;
  const skipReview = !reviewOnly && !feedback && ui.skipStatementReview.checked;
  if (!(skipReview ? promptValues.author : promptValues.review)) syncPrompts();
  reviewPending = !skipReview;
  const job = currentJob;
  try {
    const next = await request(jobPath(skipReview ? "/direct" : "/review"), {
      statement, feedback, reviewModel: ui.reviewModel.value,
      authorModel: ui.authorModel.value,
      criticModel: ui.criticModel.value,
      writerModel: ui.writerModel.value,
      reviewEffort: ui.reviewEffort.value,
      authorEffort: ui.authorEffort.value,
      criticEffort: ui.criticEffort.value,
      writerEffort: ui.writerEffort.value,
      reviewPrompt: promptValues.review,
      authorPrompt: promptValues.author,
      criticPrompt: promptValues.critic,
      finalPrompt: promptValues.final,
      criticRounds: Number(ui.criticRounds.value),
      thinkingHours: Number(ui.thinkingHours.value),
      speedMode: ui.speedMode.value,
      reasoningSummary: ui.reasoningSummary.value,
      statementReviewOnly: reviewOnly,
    });
    if (job !== currentJob) return;
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    render(next);
  } catch (error) {
    reviewPending = false;
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function startLatexOnly() {
  if (!ui.latexInput.value.trim()) {
    ui.notice.textContent = "Enter the theorem and proof.";
    show(ui.notice, true);
    ui.latexInput.focus();
    return;
  }
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  if (!promptValues.final) syncPrompts();
  try {
    const next = await request("/finalize", {
      content: ui.latexInput.value,
      writerModel: ui.writerModel.value,
      writerEffort: ui.writerEffort.value,
      finalPrompt: promptValues.final,
      speedMode: ui.speedMode.value,
      reasoningSummary: ui.reasoningSummary.value,
    });
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function act(path, body = {}) {
  clearTimeout(timer);
  const job = currentJob;
  try {
    const next = await request(jobPath(path), body);
    if (job === currentJob) render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

function setAudit(open) {
  show(ui.activityPanel, open);
  show(ui.drawerScrim, open);
  ui.activityToggle.setAttribute("aria-expanded", String(open));
  if (open) ui.activityClose.focus();
  else ui.activityToggle.focus();
}

// Filters affect the readable timeline only; Show details stays conversational.
ui.filters.onclick = (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  activeFilter = button.dataset.filter;
  for (const item of ui.filters.querySelectorAll(".filter")) {
    item.classList.toggle("active", item === button);
  }
  for (const row of timelineRows.values()) {
    row.hidden = activeFilter !== "all" && row.dataset.type !== activeFilter;
  }
};

ui.timeline.onscroll = () => {
  const away = ui.timeline.scrollHeight - ui.timeline.scrollTop
    - ui.timeline.clientHeight > 100;
  show(ui.jump, away);
};
ui.jump.onclick = () => ui.timeline.scrollTo({
  top: ui.timeline.scrollHeight, behavior: "smooth",
});

ui.activityToggle.onclick = () => setAudit(true);
ui.activityClose.onclick = () => setAudit(false);
ui.drawerScrim.onclick = () => setAudit(false);
document.onkeydown = (event) => {
  if (event.key === "Escape" && !ui.activityPanel.hidden) setAudit(false);
};

// These controls are the complete statement-approval and solve loop.
ui.homeLink.onclick = (event) => {
  event.preventDefault();
  goHome();
};
ui.check.onclick = () => selectedProblemMode() === "latex"
  ? startLatexOnly() : startReview(ui.problem.value);
ui.recheck.onclick = () => startReview(ui.proposed.value, ui.feedback.value);
ui.proposed.oninput = checkEdited;
ui.feedback.oninput = checkEdited;
ui.reviewModel.onchange = updateModelSummary;
ui.authorModel.onchange = updateModelSummary;
ui.criticModel.onchange = updateModelSummary;
ui.writerModel.onchange = updateModelSummary;
ui.reviewEffort.onchange = updateModelSummary;
ui.authorEffort.onchange = updateModelSummary;
ui.criticEffort.onchange = updateModelSummary;
ui.writerEffort.onchange = updateModelSummary;
ui.speedMode.onchange = updateModelSummary;
ui.reasoningSummary.onchange = updateModelSummary;
ui.skipStatementReview.onchange = () => {
  if (ui.skipStatementReview.checked) ui.statementReviewOnly.checked = false;
  setProblemMode(selectedProblemMode());
};
ui.statementReviewOnly.onchange = () => {
  if (ui.statementReviewOnly.checked) ui.skipStatementReview.checked = false;
  setProblemMode(selectedProblemMode());
};
for (const input of ui.problemModes) {
  input.onchange = () => setProblemMode(input.value);
}
ui.editPrompts.onclick = openPromptEditor;
ui.promptTabs.onclick = (event) => {
  const tab = event.target.closest("[data-prompt]");
  if (tab) selectPrompt(tab.dataset.prompt);
};
ui.resetPrompt.onclick = () => {
  const defaults = state.workflow?.settings?.prompts || {};
  promptDrafts[activePrompt] = defaults[activePrompt] || "";
  ui.promptEditor.value = promptDrafts[activePrompt];
  updatePromptHelp();
};
ui.promptEditor.oninput = updatePromptHelp;
ui.savePrompts.onclick = savePrompts;
ui.approve.onclick = () => act("/approve", { statement: ui.proposed.value });
ui.setAuthorTimeLimit.onclick = () => {
  const hours = Number(ui.authorLimitHours.value);
  const maximum = Number(ui.authorLimitHours.max || 168);
  if (!(hours > 0 && hours <= maximum)) {
    ui.notice.textContent = `Set the limit above 0 and at most ${maximum} hours.`;
    show(ui.notice, true);
    ui.authorLimitHours.focus();
    return;
  }
  act("/set-author-time-limit", { hours });
};
ui.sendAuthorSteer.onclick = () => {
  const instruction = ui.authorSteerInstruction.value.trim();
  if (!instruction) {
    ui.notice.textContent = "Enter an instruction for the proof author.";
    show(ui.notice, true);
    ui.authorSteerInstruction.focus();
    return;
  }
  act("/steer-author", { instruction });
};
ui.stop.onclick = () => act("/stop");
ui.home.onclick = goHome;
ui.reviewHome.onclick = goHome;
window.onpopstate = () => {
  currentJob = new URLSearchParams(location.search).get("job") || "";
  clearJobView();
  refresh();
};

refresh();
