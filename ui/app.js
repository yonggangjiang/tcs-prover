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
  fileManagement: $("fileManagement"), fileManagementSetting: $("fileManagementSetting"),
  fileManagementHelp: $("fileManagementHelp"),
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
  pause: $("pauseButton"), resume: $("resumeButton"), downloadTex: $("downloadTexButton"),
  authorTimeLimitControl: $("authorTimeLimitControl"),
  authorLimitSummary: $("authorLimitSummary"),
  authorLimitHours: $("authorLimitHours"),
  setAuthorTimeLimit: $("setAuthorTimeLimitButton"),
  authorSteerControl: $("authorSteerControl"),
  authorSteerInstruction: $("authorSteerInstruction"),
  sendAuthorSteer: $("sendAuthorSteerButton"),
  researchAuditsSetting: $("researchAuditsSetting"),
  researchAuditInterval: $("researchAuditInterval"),
  researchAuditModels: [$("researchAuditOne"), $("researchAuditTwo"), $("researchAuditThree")],
  researchAuditsControl: $("researchAuditsControl"),
  liveResearchAuditInterval: $("liveResearchAuditInterval"),
  liveResearchAuditModels: [$("liveResearchAuditOne"), $("liveResearchAuditTwo"), $("liveResearchAuditThree")],
  researchAuditSummary: $("researchAuditSummary"), researchAuditStatus: $("researchAuditStatus"),
  researchAuditCountdown: $("researchAuditCountdown"),
  researchAuditToolbar: $("researchAuditToolbar"),
  researchAuditActivity: $("researchAuditActivity"),
  researchAuditCountdownLabel: $("researchAuditCountdownLabel"),
  researchAuditTimerState: $("researchAuditTimerState"),
  startResearchAudit: $("startResearchAuditButton"),
  applyResearchAudits: $("applyResearchAuditsButton"),
  runLabel: $("runLabel"), runTitle: $("runTitle"),
  runDescription: $("runDescription"), roundBadge: $("roundBadge"),
  globalStatus: $("globalStatus"), liveDot: $("liveDot"), elapsed: $("elapsed"),
  modelSummary: $("modelSummary"),
  lastActivity: $("lastActivity"), timeline: $("timelineList"),
  memoryPanel: $("memoryPanel"), memoryFilename: $("memoryFilename"),
  memoryUpdated: $("memoryUpdated"), memoryDocument: $("memoryDocument"),
  memoryMessage: $("memoryMessage"), memoryContent: $("memoryContent"),
  memoryApproachPicker: $("memoryApproachPicker"), memoryApproach: $("memoryApproach"),
  approachGraph: $("approachGraph"), approachGraphSummary: $("approachGraphSummary"),
  approachGraphCanvas: $("approachGraphCanvas"), approachGraphInspector: $("approachGraphInspector"),
  approachGraphWarning: $("approachGraphWarning"), approachIndex: $("approachIndexButton"),
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
let promptOriginals = {};
let promptOverrides = {};
// Retire browser-persisted prompts: every new job starts from the workflow files.
try { localStorage.removeItem("tcs-prover-role-prompts"); } catch (_) {}
const timelineRows = new Map();
const detailRows = new Map();
const memoryTabs = [...document.querySelectorAll("[data-memory]")];
let memoryJob = null;
let memoryFile = "INITIAL_PROMPT.md";
let memoryAnchor = "";
let memoryVersion = "";
let memoryTimer;
let memoryRequest = 0;
let approachIndex = { version: "", content: "", files: [] };
let approachGraphSignature = "";
let researchAuditJob = null;
let researchAuditDirty = false;
let researchAuditSaving = false;
let researchAuditStarting = false;
let researchAuditFeedback = "";
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
    + `${detail}${settingsWarning}\n\nA new job will be created. The source job will not be changed, `
    + "and an interrupted model response or private reasoning cannot be resumed.";
  if (!["paused", "prepared"].includes(job.phase) && !confirm(question)) return;
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
      running: "Running", stopping: "Stopping", pausing: "Pausing", paused: "Paused", done: "Finished",
      prepared: "Ready to start",
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
    times.textContent = job.phase === "prepared" ? "Prepared research workspace · No model session started"
      : `Started: ${localTime(job.startedAt)} · Finished: ${finished ? localTime(finished) : "Not finished"}`;
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
          continueButton.disabled = ["reviewing", "running", "stopping", "pausing", "paused"]
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
    remove.disabled = ["reviewing", "running", "stopping", "pausing"].includes(job.phase);
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

function managesResearchFiles(source = state) {
  // Runs saved before this option existed used the managed author by default.
  return source.runId ? source.fileManagement !== false : ui.fileManagement.checked;
}

function selectedAuthorPrompt() {
  return managesResearchFiles() ? "author" : "author_simple";
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
  show(ui.fileManagementSetting, !latexOnly && !reviewOnly);
  show(ui.researchAuditsSetting, !latexOnly && !reviewOnly && ui.fileManagement.checked);
  ui.fileManagementHelp.textContent = ui.fileManagement.checked
    ? "On · Organize approach files, proved lemmas, and optional research audits."
    : "Off · Focus on proving the statement with a simple author prompt.";
  ui.authorPromptTab.dataset.prompt = selectedAuthorPrompt();
  ui.authorPromptTab.textContent = ui.fileManagement.checked ? "Author · managed" : "Author · simple";
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
        + "by independent proof verification and LaTeX editing."
      : "Start with a rough TCS problem. The agent will clarify it, ask for approval, "
        + "solve it, verify the proof, and produce clean LaTeX.";
  if (reviewOnly && activePrompt !== "review" && ui.promptDialog.open) {
    selectPrompt("review");
  } else if (skipReview && activePrompt === "review" && ui.promptDialog.open) {
    selectPrompt(selectedAuthorPrompt());
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
  review: "Reviewer prompt", author: "Managed author prompt", author_simple: "Simple author prompt",
  critic: "Critic prompt", final: "Final writer prompt",
};

const promptHelp = {
  review: "The full request also includes the statement and any revision feedback.",
  author: "Keep exactly one [STATEMENT]. The workflow replaces it with your statement.",
  author_simple: "Used when research file management is off. Keep exactly one [STATEMENT] for your statement.",
  critic: "Each critic call receives the statement and latest proof, then follows these instructions to use fresh independent subagents.",
  final: "The request adds the supplied writing for this editor to polish into LaTeX.",
};

function syncPrompts(source = state) {
  const defaults = source.workflow?.settings?.prompts || {};
  promptOverrides = {};
  promptValues = Object.fromEntries(Object.keys(promptLabels).map((name) => [
    name, savedPrompt(source, name) || defaults[name] || "",
  ]));
}

function savedPrompt(source, name) {
  if (!source.runId) return "";
  if (name === "author" || name === "author_simple") {
    const selected = source.fileManagement === false ? "author_simple" : "author";
    return name === selected ? source.authorPrompt : "";
  }
  return source[`${name}Prompt`];
}

function updatePromptHelp() {
  const defaults = state.workflow?.settings?.prompts || {};
  const isDefault = ui.promptEditor.value.trim() === (defaults[activePrompt] || "").trim();
  ui.promptEditorLabel.textContent = `${promptLabels[activePrompt]} — `
    + (isDefault ? "current default" : "this job's prompt");
  ui.promptEditorHelp.textContent = (isDefault ? "" :
    "This overrides the default for this job only. ")
    + promptHelp[activePrompt]
    + " New jobs load defaults from the workflow files; browser edits are not remembered.";
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

async function currentPromptDefaults() {
  const job = currentJob;
  const next = await request(jobPath("/state"));
  if (job !== currentJob) return null;
  state.workflow = next.workflow;
  return state.workflow.settings.prompts;
}

async function openPromptEditor() {
  try {
    const defaults = await currentPromptDefaults();
    if (!defaults) return;
    if (!currentJob) promptValues = { ...defaults, ...promptOverrides };
    else if (!promptValues.review) syncPrompts();
    promptDrafts = { ...promptValues };
    promptOriginals = { ...promptValues };
    activePrompt = selectedProblemMode() === "latex" ? "final"
      : ui.skipStatementReview.checked ? selectedAuthorPrompt() : "review";
    selectPrompt(activePrompt);
    ui.promptDialog.showModal();
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

function savePrompts() {
  promptDrafts[activePrompt] = ui.promptEditor.value;
  if (Object.values(promptDrafts).some((prompt) => !prompt.trim())) {
    ui.notice.textContent = "Every role prompt must contain instructions.";
    show(ui.notice, true);
    return;
  }
  if (["author", "author_simple"].some((name) => (promptDrafts[name]?.match(/\[STATEMENT\]/g) || []).length !== 1)) {
    ui.notice.textContent =
      "Each author prompt must contain exactly one [STATEMENT].";
    show(ui.notice, true);
    return;
  }
  promptValues = Object.fromEntries(
    Object.entries(promptDrafts).map(([name, prompt]) => [name, prompt.trim()])
  );
  const defaults = state.workflow?.settings?.prompts || {};
  // Send only deliberate edits. Unedited roles are resolved on the server at launch.
  for (const [name, prompt] of Object.entries(promptValues)) {
    if (prompt === (promptOriginals[name] || "").trim()) continue;
    const baseline = (
      savedPrompt(state, name) || defaults[name] || ""
    ).trim();
    if (prompt === baseline) delete promptOverrides[name];
    else promptOverrides[name] = prompt;
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

function elapsedText(start, end = Date.now(), previousSeconds = 0) {
  if (!start) return "";
  const seconds = Math.max(0, Math.floor(previousSeconds + (new Date(end) - new Date(start)) / 1000));
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
  if (entry.kind === "research_audit") return entry.status !== "working" || Boolean(entry.recovered);
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
      text: report.verdict === "reject"
        ? (report.bugs || "Unfixable issues return to the proof author.")
        : "The critic passed this round. An unchanged proof proceeds immediately; an edited proof repeats until the round limit.",
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
  if (entry.kind === "research_audit") {
    return {key: `audit:${entry.slot}:${entry.time}:${entry.status}`, type: entry.status === "warning" ? "error" : "status",
      label: entry.label || `Audit ${entry.slot}`, text, time: entry.time, replace: true};
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

// Resolve only supported research files; arbitrary URLs and HTML stay plain text.
function memoryLinkTarget(href, from = memoryFile) {
  const [raw, anchor = ""] = href.split("#", 2);
  if (!raw) return { name: from, anchor };
  if (from === "APPROACHES" || from === "APPROACHES.md") from = "APPROACHES/index.md";
  if (/^[a-z][a-z\d+.-]*:|^\//i.test(raw)) return null;
  let path;
  try { path = decodeURIComponent(raw); } catch (_) { return null; }
  const parts = from.includes("/") ? from.split("/").slice(0, -1) : [];
  // Root-relative notebook links are common in saved research records.
  if (/^(?:APPROACHES|AUDITS|audit_history)\//.test(path)) parts.length = 0;
  for (const part of path.split("/")) {
    if (part === "." || !part) continue;
    if (part === "..") { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  const name = parts.join("/");
  if (!/^(?:INITIAL_PROMPT\.md|PROVED\.md|APPROACHES\.md|audit\.md|APPROACHES\/(?:index|INDEX|A\d{3,}(?:-[A-Za-z0-9_-]+)?)\.md|(?:AUDITS|audit_history)\/[A-Za-z0-9][A-Za-z0-9_.-]*\.md)$/.test(name)) return null;
  return { name: /^APPROACHES\/(?:index|INDEX)\.md$/.test(name) ? "APPROACHES" : name, anchor };
}

// Build inline Markdown with DOM nodes, never with HTML from a file.
function appendMemoryText(element, value) {
  for (const part of String(value).split(/(\[[^\]\n]+\]\([^()\s]+\)|`[^`\n]+`|\*\*[^*]+\*\*)/g)) {
    const link = part.match(/^\[([^\]\n]+)\]\(([^()\s]+)\)$/);
    const target = link && memoryLinkTarget(link[2], ui.memoryFilename.textContent || memoryFile);
    if (part.startsWith("`") && part.endsWith("`")) {
      const code = document.createElement("code");
      code.textContent = part.slice(1, -1); element.append(code); continue;
    }
    if (!target) { appendFormattedText(element, part); continue; }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "memory-link";
    appendFormattedText(button, link[1]);
    button.onclick = () => selectMemoryDocument(target.name, target.anchor);
    element.append(button);
  }
}

function markdownTableCells(line) {
  const row = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  // A literal escaped pipe (including mathematical \|) is not a cell boundary.
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function markdownTableDivider(line) {
  const cells = markdownTableCells(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function plainMarkdown(value) {
  return value.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\*\*|`/g, "").replace(/<br\s*\/?>/gi, " · ").trim();
}

function memoryHeadingId(value) {
  return plainMarkdown(value).toLowerCase().replace(/[^a-z0-9\s-]/g, "").trim().replace(/\s+/g, "-");
}

function renderMemoryContent(value) {
  const scrollTop = ui.memoryDocument.scrollTop;
  const expanded = new Map([...ui.memoryContent.querySelectorAll("details")]
    .map((section) => [section.dataset.heading, section.open]));
  const fragment = document.createDocumentFragment();
  let target = fragment;
  let paragraph = [];
  let code = null;
  let fence = "";
  let sections = 0;
  let list = null;
  const flush = () => {
    if (!paragraph.length) return;
    const copy = document.createElement("p");
    appendMemoryText(copy, paragraph.join("\n"));
    target.append(copy);
    paragraph = [];
  };
  const lines = value.split(/\r?\n/);
  for (let lineNumber = 0; lineNumber < lines.length; lineNumber += 1) {
    const line = lines[lineNumber];
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (code !== null) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length) {
        const pre = document.createElement("pre");
        pre.textContent = code.join("\n");
        target.append(pre);
        code = null;
      } else code.push(line);
      continue;
    }
    if (marker) { flush(); list = null; code = []; fence = marker[1]; continue; }
    // Saved lemma markers are navigation hints, never arbitrary executable HTML.
    if (/^\s*<a\s+id=["'][A-Za-z][A-Za-z0-9_-]*["']\s*>\s*<\/a>\s*$/.test(line)) {
      flush(); list = null; continue;
    }
    if (/^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$/.test(line)) {
      flush(); list = null; target.append(document.createElement("hr")); continue;
    }
    if (line.includes("|") && lineNumber + 1 < lines.length && markdownTableDivider(lines[lineNumber + 1])) {
      flush(); list = null;
      const wrapper = document.createElement("div"); wrapper.className = "memory-table-wrap";
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const header = document.createElement("tr");
      const headers = markdownTableCells(line);
      for (const value of headers) {
        const cell = document.createElement("th"); cell.scope = "col";
        appendMemoryText(cell, value); header.append(cell);
      }
      head.append(header); table.append(head);
      const body = document.createElement("tbody");
      lineNumber += 1;
      while (lineNumber + 1 < lines.length && lines[lineNumber + 1].includes("|") && lines[lineNumber + 1].trim()) {
        const row = document.createElement("tr");
        const values = markdownTableCells(lines[++lineNumber]);
        for (let column = 0; column < headers.length; column += 1) {
          const cell = document.createElement("td");
          appendMemoryText(cell, values[column] || ""); row.append(cell);
        }
        body.append(row);
      }
      table.append(body); wrapper.append(table); target.append(wrapper); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    if (heading) {
      flush(); list = null;
      if (heading[1].length === 2) {
        const section = document.createElement("details");
        section.className = "memory-section";
        section.dataset.heading = heading[2];
        section.id = `memory-${memoryHeadingId(heading[2])}`;
        section.open = expanded.get(heading[2]) ?? (sections < 2);
        const summary = document.createElement("summary");
        appendMemoryText(summary, heading[2]);
        target = document.createElement("div");
        target.className = "memory-section-body";
        section.append(summary, target);
        fragment.append(section);
        sections += 1;
      } else {
        const title = document.createElement(heading[1].length === 1 ? "h3" : "h4");
        title.id = `memory-${memoryHeadingId(heading[2])}`;
        appendMemoryText(title, heading[2]);
        if (heading[1].length === 1) target = fragment;
        target.append(title);
      }
    } else if (!line.trim()) { flush(); list = null; }
    else if (/^\s*(?:[-*+] |\d+[.)] )/.test(line)) {
      flush();
      const item = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)/);
      const tag = /^\d/.test(item[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = document.createElement(tag);
        if (tag === "ol") list.start = parseInt(item[1], 10);
        target.append(list);
      }
      const entry = document.createElement("li"); appendMemoryText(entry, item[2]); list.append(entry);
    } else if (/^(?:\*\*)?(Parents|Children|Status):(?:\*\*)?\s*/i.test(line)) {
      flush(); list = null;
      const metadata = line.match(/^(?:\*\*)?(Parents|Children|Status):(?:\*\*)?\s*(.*)/i);
      const row = document.createElement("div"); row.className = "memory-record-meta";
      const label = document.createElement("strong"); label.textContent = metadata[1];
      const detail = document.createElement("span"); appendMemoryText(detail, metadata[2]);
      if (metadata[1].toLowerCase() === "status") detail.className = `approach-status ${plainMarkdown(metadata[2]).toLowerCase()}`;
      row.append(label, detail); target.append(row);
    } else { list = null; paragraph.push(line); }
  }
  flush();
  if (code !== null) {
    const pre = document.createElement("pre");
    pre.textContent = code.join("\n");
    target.append(pre);
  }
  ui.memoryContent.replaceChildren(fragment);
  ui.memoryDocument.scrollTop = scrollTop;
}

function revealMemoryAnchor() {
  if (!memoryAnchor) return;
  const anchor = memoryAnchor.toLowerCase();
  memoryAnchor = "";
  const section = [...ui.memoryContent.querySelectorAll("details, h3, h4")].find((item) => {
    const heading = (item.dataset.heading || item.textContent).toLowerCase();
    return heading.match(/^[al]\d+\b/)?.[0] === anchor || memoryHeadingId(heading) === anchor;
  });
  if (section) {
    if (section.tagName === "DETAILS") section.open = true;
    const parent = section.closest("details"); if (parent) parent.open = true;
    section.scrollIntoView({ block: "nearest" });
  }
}

// The index parent column is authoritative. Only existing node files become buttons.
function parseApproachGraph(content, files) {
  const nodes = files.filter((name) => /^APPROACHES\/A\d{3,}(?:-[A-Za-z0-9_-]+)?\.md$/.test(name))
    .map((name) => {
      const filename = name.split("/").at(-1);
      const id = filename.match(/^A\d+/)[0];
      return { id, file: name, title: filename.replace(/\.md$/, "").replace(/^A\d+-?/, "").replace(/[-_]/g, " ") || id,
        parents: [], children: [], status: "UNLISTED", result: "This file is not yet listed in the index.", indexed: false };
    });
  const byFile = new Map(nodes.map((node) => [node.file, node]));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const warnings = [];
  if (byId.size !== nodes.length) warnings.push("Some files share an approach ID; use unique stable IDs in the index.");
  const lines = content.split(/\r?\n/);
  for (let row = 0; row + 1 < lines.length; row += 1) {
    if (!lines[row].includes("|") || !markdownTableDivider(lines[row + 1])) continue;
    const headings = markdownTableCells(lines[row]).map((cell) => plainMarkdown(cell).toLowerCase());
    const parentColumn = headings.findIndex((value) => /parent/.test(value));
    if (parentColumn < 0) continue;
    const statusColumn = headings.findIndex((value) => /status/.test(value));
    const resultColumn = headings.findIndex((value) => /result|question|summary|outcome|obstacle/.test(value));
    const titleColumn = headings.findIndex((value) => /title|approach|node/.test(value));
    row += 1;
    while (row + 1 < lines.length && lines[row + 1].trim() && lines[row + 1].includes("|")) {
      const cells = markdownTableCells(lines[++row]);
      // The node link belongs in its own identity/title cell, never the parent column.
      const identityCells = [cells[0], titleColumn > 0 ? cells[titleColumn] : ""];
      let linkedNode = null;
      let label = "";
      for (const cell of identityCells) {
        for (const match of (cell || "").matchAll(/\[([^\]]+)\]\(([^()\s]+)\)/g)) {
          const target = memoryLinkTarget(match[2], "APPROACHES/index.md");
          if (target && byFile.has(target.name)) { linkedNode = byFile.get(target.name); label = match[1]; break; }
        }
        if (linkedNode) break;
      }
      if (!linkedNode) continue;
      const node = linkedNode;
      const title = plainMarkdown(titleColumn >= 0 ? cells[titleColumn] || label : label)
        .replace(new RegExp(`^${node.id}\\b[\\s:—–-]*`), "").trim();
      node.title = title || node.title;
      node.parents = [...new Set((cells[parentColumn] || "").match(/\bA\d{3,}\b/g) || [])];
      const status = plainMarkdown(cells[statusColumn] || "").toUpperCase();
      node.status = ["ACTIVE", "PARKED", "CLOSED", "RESOLVED"].includes(status) ? status : "UNSPECIFIED";
      node.result = plainMarkdown(cells[resultColumn] || "") || "No result or remaining question recorded in the index yet.";
      node.indexed = true;
    }
  }
  const edges = [];
  const missing = new Set();
  for (const node of nodes) {
    for (const parent of node.parents) {
      if (byId.has(parent)) {
        edges.push({ from: parent, to: node.id });
        byId.get(parent).children.push(node.id);
      } else missing.add(parent);
    }
  }
  if (missing.size) warnings.push(`Parent files missing from the workspace: ${[...missing].join(", ")}.`);
  const unlisted = nodes.filter((node) => !node.indexed).length;
  if (unlisted) warnings.push(`${unlisted} ${unlisted === 1 ? "file is" : "files are"} not linked in the parent table; shown without inferred dependencies.`);
  const indegrees = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) indegrees.set(edge.to, indegrees.get(edge.to) + 1);
  const queue = nodes.filter((node) => indegrees.get(node.id) === 0);
  for (const node of nodes) node.depth = 0;
  let visited = 0;
  for (let index = 0; index < queue.length; index += 1) {
    const node = queue[index]; visited += 1;
    for (const childId of node.children) {
      const child = byId.get(childId);
      child.depth = Math.max(child.depth, node.depth + 1);
      indegrees.set(childId, indegrees.get(childId) - 1);
      if (indegrees.get(childId) === 0) queue.push(child);
    }
  }
  if (visited < nodes.length) {
    warnings.push("The parent table contains a cycle. Check the dependencies; cyclic nodes are displayed together.");
    const depth = Math.max(0, ...queue.map((node) => node.depth)) + 1;
    for (const node of nodes) if (indegrees.get(node.id) > 0) node.depth = depth;
  }
  return { nodes, edges, warnings };
}

function inspectApproachNode(node) {
  ui.approachGraphInspector.replaceChildren();
  const heading = document.createElement("strong"); heading.textContent = `${node.id} — ${node.title}`;
  const result = document.createElement("p"); result.textContent = node.result;
  const dependencies = document.createElement("small");
  dependencies.textContent = `${node.status} · Parents: ${node.parents.join(", ") || "none"} · Children: ${node.children.join(", ") || "none"}`;
  const path = document.createElement("code"); path.textContent = node.file;
  ui.approachGraphInspector.append(heading, result, dependencies, path);
}

function renderApproachGraph() {
  const signature = JSON.stringify([approachIndex.content, approachIndex.files]);
  if (signature === approachGraphSignature) {
    for (const button of ui.approachGraphCanvas.querySelectorAll("button")) {
      button.setAttribute("aria-current", String(button.dataset.file === memoryFile));
    }
    return;
  }
  approachGraphSignature = signature;
  const graph = parseApproachGraph(approachIndex.content, approachIndex.files);
  ui.approachGraphSummary.textContent = graph.nodes.length
    ? `${graph.nodes.length} approach files · ${graph.edges.length} connections · Parent → child. Select a node to read its file.`
    : "Saved approach files will appear here. The index parent table defines their connections.";
  ui.approachGraphWarning.textContent = graph.warnings.join(" ");
  show(ui.approachGraphWarning, graph.warnings.length > 0);
  ui.approachGraphCanvas.replaceChildren();
  ui.approachGraphInspector.textContent = "Hover or focus a node for its result and dependencies. Select it to read the full record.";
  const columnRows = new Map();
  const nodeWidth = 210, nodeHeight = 112, columnGap = 60, rowGap = 30, padding = 20;
  for (const node of graph.nodes) {
    const row = columnRows.get(node.depth) || 0;
    columnRows.set(node.depth, row + 1);
    node.x = padding + node.depth * (nodeWidth + columnGap);
    node.y = padding + row * (nodeHeight + rowGap);
  }
  const width = graph.nodes.length ? Math.max(...graph.nodes.map((node) => node.x)) + nodeWidth + padding : 0;
  const height = graph.nodes.length ? Math.max(...graph.nodes.map((node) => node.y)) + nodeHeight + padding : 0;
  ui.approachGraphCanvas.style.width = `${width}px`;
  ui.approachGraphCanvas.style.height = `${height}px`;
  const svgElement = (tag, attributes = {}) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    return element;
  };
  const svg = svgElement("svg", { width, height, "aria-hidden": "true", focusable: "false" });
  const defs = svgElement("defs");
  const marker = svgElement("marker", { id: "approach-arrow", markerWidth: "8", markerHeight: "8", refX: "7", refY: "4", orient: "auto" });
  marker.append(svgElement("path", { d: "M 0 0 L 8 4 L 0 8 z", fill: "#77958b" }));
  defs.append(marker); svg.append(defs);
  const byId = new Map(graph.nodes.map((node) => [node.id, node]));
  for (const edge of graph.edges) {
    const source = byId.get(edge.from), target = byId.get(edge.to);
    const x1 = source.x + nodeWidth, y1 = source.y + nodeHeight / 2;
    const x2 = target.x - 5, y2 = target.y + nodeHeight / 2;
    const bend = Math.max(24, (x2 - x1) / 2);
    const path = svgElement("path", { d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
      fill: "none", stroke: "#91aaa0", "stroke-width": "1.5", "marker-end": "url(#approach-arrow)" });
    svg.append(path);
  }
  ui.approachGraphCanvas.append(svg);
  for (const node of graph.nodes) {
    const button = document.createElement("button"); button.type = "button";
    button.className = `approach-node ${node.status.toLowerCase()}`;
    button.dataset.file = node.file;
    button.style.left = `${node.x}px`; button.style.top = `${node.y}px`;
    button.setAttribute("aria-current", String(node.file === memoryFile));
    button.setAttribute("aria-label", `${node.id}: ${node.title}. ${node.status}. Open ${node.file}`);
    button.setAttribute("aria-describedby", "approachGraphInspector");
    button.title = `${node.id} — ${node.title}\n${node.status}\n${node.result}\nParents: ${node.parents.join(", ") || "none"}\nChildren: ${node.children.join(", ") || "none"}\n${node.file}`;
    const id = document.createElement("strong"); id.textContent = node.id;
    const title = document.createElement("span"); title.className = "approach-node-title"; title.textContent = node.title;
    const status = document.createElement("small"); status.className = `approach-status ${node.status.toLowerCase()}`; status.textContent = node.status;
    button.append(id, title, status);
    button.onmouseenter = () => inspectApproachNode(node);
    button.onfocus = () => inspectApproachNode(node);
    button.onclick = () => {
      inspectApproachNode(node); selectMemoryDocument(node.file);
      ui.memoryDocument.focus({ preventScroll: true });
      ui.memoryDocument.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    ui.approachGraphCanvas.append(button);
  }
  const selected = graph.nodes.find((node) => node.file === memoryFile);
  if (selected) inspectApproachNode(selected);
}

function renderMemoryChoices(file) {
  const files = file.files || [];
  show(ui.memoryApproachPicker, files.length > 0);
  if (!files.length) return;
  const selected = ["APPROACHES", "AUDITS", "audit_history"].includes(memoryFile) ? file.name : memoryFile;
  const choices = files.includes(selected) ? files : [...files, selected];
  if (JSON.stringify([...ui.memoryApproach.options].map((option) => option.value)) !== JSON.stringify(choices)) {
    ui.memoryApproach.replaceChildren(...choices.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = /^APPROACHES\/(?:index|INDEX)\.md$/.test(name) ? "Index"
        : name === "APPROACHES.md" ? "Historical notebook"
          : name === "audit.md" ? "Historical audit notebook" : name.slice(name.lastIndexOf("/") + 1, -3);
      return option;
    }));
  }
  ui.memoryApproach.value = selected;
}

async function loadMemory() {
  clearTimeout(memoryTimer);
  if (!ui.memoryPanel.open || ui.memoryPanel.hidden || document.hidden) return;
  const job = currentJob;
  const requestId = ++memoryRequest;
  try {
    const file = await request(jobPath("/memory", { file: memoryFile, version: memoryVersion }));
    if (job !== currentJob || requestId !== memoryRequest || !ui.memoryPanel.open) return;
    if (memoryFile.startsWith("APPROACHES")) {
      const index = memoryFile === "APPROACHES" || /^APPROACHES\/(?:index|INDEX)\.md$/.test(memoryFile)
        ? file : await request(jobPath("/memory", { file: "APPROACHES", version: approachIndex.version }));
      if (job !== currentJob || requestId !== memoryRequest || !ui.memoryPanel.open) return;
      if (index.status === "ready" && /^APPROACHES\/(?:index|INDEX)\.md$/.test(index.name)) {
        approachIndex = { version: index.version, files: index.files || [], content: index.unchanged ? approachIndex.content : index.content };
      } else approachIndex = { version: "", content: "", files: index.files || [] };
      renderApproachGraph();
    }
    renderMemoryChoices(file);
    ui.memoryFilename.textContent = file.name;
    if (file.status === "ready") {
      if (!file.unchanged) {
        memoryVersion = file.version;
        renderMemoryContent(file.content);
        ui.memoryMessage.textContent = "This file is still empty.";
        show(ui.memoryMessage, !file.content.trim());
      }
      ui.memoryUpdated.textContent = `Updated ${new Date(file.modifiedAt).toLocaleString()}`;
      revealMemoryAnchor();
    } else {
      memoryVersion = "";
      ui.memoryContent.replaceChildren();
      ui.memoryMessage.textContent = file.status === "missing"
        ? (memoryFile === "audit_history" || memoryFile.startsWith("audit_history/") || memoryFile === "audit.md"
          ? "No previous audit reports yet. Current reports move here when the next audit batch starts."
          : memoryFile === "AUDITS" || memoryFile.startsWith("AUDITS/")
          ? "No audit report has been saved here yet. Reports will appear after an audit completes."
          : "The author has not created this file yet. It will appear here when saved.")
        : "This file is unavailable right now. Trying again shortly…";
      show(ui.memoryMessage, true);
      ui.memoryUpdated.textContent = file.status === "missing" ? "Not created yet" : "Temporarily unavailable";
    }
  } catch (error) {
    if (job !== currentJob || requestId !== memoryRequest) return;
    ui.memoryUpdated.textContent = "Could not refresh. Retrying…";
    if (!memoryVersion) {
      ui.memoryMessage.textContent = error.message;
      show(ui.memoryMessage, true);
    }
  } finally {
    if (job === currentJob && requestId === memoryRequest && ui.memoryPanel.open && !ui.memoryPanel.hidden) {
      memoryTimer = setTimeout(loadMemory, 5000);
    }
  }
}

function selectMemoryFile(tab) {
  show(ui.memoryApproachPicker, false);
  selectMemoryDocument(tab.dataset.memory);
}

function resolveArchivedMemoryFile(name, entries = state.trace || []) {
  if (!/^AUDITS\/[A-Za-z0-9][A-Za-z0-9_.-]*\.md$/.test(name)) return name;
  for (const entry of [...entries].reverse()) {
    if (entry.kind !== "research_audit" || entry.status !== "archived") continue;
    const paths = entry.archivedPaths;
    const archived = paths && Object.hasOwn(paths, name) ? paths[name] : "";
    if (typeof archived === "string" && /^audit_history\/[A-Za-z0-9][A-Za-z0-9_.-]*\.md$/.test(archived)) return archived;
  }
  return name;
}

function selectMemoryDocument(name, anchor = "") {
  name = resolveArchivedMemoryFile(name);
  const category = name.startsWith("APPROACHES") ? "APPROACHES"
    : name === "audit_history" || name.startsWith("audit_history/") || name === "audit.md" ? "audit_history"
    : name === "AUDITS" || name.startsWith("AUDITS/") ? "AUDITS" : name;
  const tab = memoryTabs.find((item) => item.dataset.memory === category);
  for (const item of memoryTabs) {
    item.setAttribute("aria-selected", String(item === tab));
    item.tabIndex = item === tab ? 0 : -1;
  }
  if (tab) ui.memoryDocument.setAttribute("aria-labelledby", tab.id);
  show(ui.approachGraph, category === "APPROACHES");
  ++memoryRequest;
  memoryFile = name;
  memoryAnchor = anchor;
  memoryVersion = "";
  ui.memoryFilename.textContent = name;
  ui.memoryDocument.scrollTop = 0;
  ui.memoryContent.replaceChildren();
  ui.memoryMessage.textContent = "Loading saved work…";
  show(ui.memoryMessage, true);
  ui.memoryUpdated.textContent = "";
  if (category === "APPROACHES") renderApproachGraph();
  loadMemory();
}

function followArchivedMemoryFile() {
  if (!ui.memoryPanel.open || ui.memoryPanel.hidden) return;
  const name = memoryFile === "AUDITS" ? ui.memoryFilename.textContent : memoryFile;
  const archived = resolveArchivedMemoryFile(name);
  if (archived !== name) selectMemoryDocument(archived, memoryAnchor);
}

function syncMemoryPanel() {
  const identity = currentJob || state.runId || "";
  if (memoryJob !== identity) {
    memoryJob = identity;
    ++memoryRequest;
    clearTimeout(memoryTimer);
    approachIndex = { version: "", content: "", files: [] };
    approachGraphSignature = "";
    ui.memoryPanel.open = false;
    selectMemoryFile(memoryTabs[0]);
  }
  const visible = Boolean(identity) && !ui.run.hidden
    && state.problemMode !== "latex" && !state.statementReviewOnly && managesResearchFiles();
  show(ui.memoryPanel, visible);
  if (visible) followArchivedMemoryFile();
  if (!visible) {
    ui.memoryPanel.open = false;
    clearTimeout(memoryTimer);
    ++memoryRequest;
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
      key: `detail-prompt:${entry.time}`,
      label: entry.stage === "audit" ? entry.label || "Audit prompt to model" : "Prompt to model",
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
  const latexOnly = ["latex", "final-resume"].includes(state.problemMode);
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
      condition.textContent = "At an interruption or time limit";
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
    ui.workflowNodes.replaceChildren(makeNode("latex_editor", "1"));
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
  loopTitle.textContent = "Author and critic";
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
    "Author and critic interruptions preserve saved work for continuation",
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
  selfRoute.textContent = "↻ Edited PASS → repeat below limit";
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
  const passRoute = arrow("Unchanged PASS or edited PASS at limit", true);
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
    ? elapsedText(state.resumedAt || state.startedAt, state.finishedAt || Date.now(), Number(state.elapsedSeconds || 0)) : "";
  if (state.lastActivityAt) {
    const ago = Math.max(0, Math.floor(
      (Date.now() - new Date(state.lastActivityAt)) / 1000
    ));
    const working = ["reviewing", "running", "stopping", "pausing"].includes(state.phase)
      || Boolean(state.auditHoldingAuthor);
    const activity = working
      ? (ago < 2 ? "Working · public activity now"
        : `Still working · waiting ${ago}s for the next public event`)
      : `Updated ${ago < 2 ? "just now" : `${ago}s ago`}`;
    ui.lastActivity.textContent = `${activity} · `
      + `${state.traceVersion || 0} events recorded`;
  }
}

function researchAuditValues(live = false) {
  return {
    intervalHours: Number((live ? ui.liveResearchAuditInterval : ui.researchAuditInterval).value),
    models: (live ? ui.liveResearchAuditModels : ui.researchAuditModels).map((select) => select.value),
  };
}

function fillResearchAuditControls(live = false) {
  const catalog = state.workflow?.settings?.research_audits || {};
  const config = state.researchAudits || catalog;
  const choices = catalog.choices || [{ value: "none", label: "None" }];
  const interval = live ? ui.liveResearchAuditInterval : ui.researchAuditInterval;
  const models = live ? ui.liveResearchAuditModels : ui.researchAuditModels;
  const hours = String(config.intervalHours ?? catalog.intervalHours ?? 2);
  if (interval.value !== hours) interval.value = hours;
  models.forEach((select, index) => {
    const signature = JSON.stringify(choices);
    if (select.dataset.choices !== signature) {
      select.replaceChildren(...choices.map((choice) => {
        const option = document.createElement("option");
        option.value = choice.value;
        option.textContent = choice.label;
        return option;
      }));
      select.dataset.choices = signature;
    }
    const selected = config.models?.[index] || "none";
    if (select.value !== selected) select.value = selected;
  });
}

function renderResearchAudits(canEdit) {
  if (!managesResearchFiles()) {
    for (const element of [ui.researchAuditsControl, ui.researchAuditToolbar,
      ui.researchAuditActivity, ui.researchAuditStatus]) show(element, false);
    return;
  }
  const identity = currentJob || state.runId || "";
  if (researchAuditJob !== identity) {
    researchAuditJob = identity;
    researchAuditDirty = false;
    researchAuditSaving = false;
    researchAuditStarting = false;
    researchAuditFeedback = "";
    ui.researchAuditsControl.open = false;
  }
  show(ui.researchAuditsControl, canEdit);
  if (!researchAuditDirty && !researchAuditSaving) fillResearchAuditControls(true);
  for (const input of [ui.liveResearchAuditInterval, ...ui.liveResearchAuditModels]) input.disabled = !canEdit;
  ui.applyResearchAudits.disabled = !canEdit || researchAuditSaving || researchAuditStarting || !researchAuditDirty;
  ui.applyResearchAudits.textContent = researchAuditSaving ? "Applying…" : "Apply";
  const config = state.researchAudits || {};
  const status = state.researchAuditStatus || {};
  const enabled = (config.models || []).filter((model) => model !== "none").length;
  const running = Array.isArray(status.runningSlots) ? status.runningSlots.length : Number(status.runningSlots || 0);
  ui.researchAuditSummary.textContent = enabled
    ? `${enabled} selected · every ${config.intervalHours ?? 2} active author hours` : "Disabled";
  const progress = state.researchAuditProgress || {};
  const remaining = Math.max(0, Math.ceil((config.intervalHours ?? 2) * 3600
    - Math.max(0, (progress.elapsedSeconds || 0) - (progress.lastStartedSeconds || 0))));
  const countdown = [Math.floor(remaining / 3600), Math.floor(remaining / 60) % 60, remaining % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
  const auditing = Boolean(state.auditHoldingAuthor || (state.phase === "running" && status.batchActive))
    || running > 0;
  const author = state.activeNode === "author" && ["solve", "repair"].includes(state.stage);
  ui.researchAuditCountdownLabel.textContent = auditing ? "Research audits" : "Next audit";
  ui.researchAuditCountdown.textContent = auditing
    ? (state.phase === "pausing" ? "Preparing…" : "In progress") : countdown;
  ui.researchAuditTimerState.textContent = auditing && state.phase !== "running" ? "author paused"
    : state.phase === "running" ? "author time" : "paused";
  show(ui.researchAuditToolbar, Boolean((enabled || auditing) && author
    && ["running", "pausing", "paused"].includes(state.phase)));
  ui.startResearchAudit.disabled = !author || state.phase !== "running" || !enabled || auditing
    || researchAuditDirty || researchAuditSaving || researchAuditStarting;
  ui.startResearchAudit.textContent = researchAuditStarting ? "Starting…" : "Start audit now";
  ui.startResearchAudit.title = researchAuditDirty ? "Apply your audit settings first."
    : "Run the selected auditors now and reset the audit interval.";
  renderResearchAuditActivity();
  const modelWarnings = Array.isArray(status.warnings) ? status.warnings : [];
  let batchError = String(status.batchError || status.lastError || "");
  for (const warning of modelWarnings) batchError = batchError.replace(String(warning.message || ""), "");
  const warnings = batchError.trim() ? [batchError.trim()] : [];
  ui.researchAuditStatus.textContent = [
    ...warnings,
    researchAuditDirty ? "Changes have not been applied." : "",
    researchAuditFeedback,
  ].filter(Boolean).join("\n");
  ui.researchAuditStatus.className = "help research-audit-status"
    + (warnings.length || researchAuditFeedback ? " warning" : "");
  show(ui.researchAuditStatus, Boolean(ui.researchAuditStatus.textContent));
}

function renderResearchAuditActivity() {
  const config = state.researchAudits || {};
  const status = state.researchAuditStatus || {};
  const choices = state.workflow?.settings?.research_audits?.choices || [];
  const warnings = Array.isArray(status.warnings) ? status.warnings : [];
  const running = Array.isArray(status.runningSlots) ? status.runningSlots : [];
  const live = Boolean(state.auditHoldingAuthor || (state.phase === "running" && status.batchActive));
  const events = (state.trace || []).filter((entry) => entry.kind === "research_audit");
  const rows = (config.models || []).flatMap((model, index) => {
    if (model === "none") return [];
    const slot = index + 1;
    const log = events.filter((entry) => entry.slot === slot && entry.model === model).slice(-30);
    const last = log[log.length - 1];
    const warning = warnings.find((item) => item.slot === slot && item.model === model);
    const busy = running.includes(slot) && (live || state.phase === "running");
    let phase = last?.status || (warning ? "warning" : "idle");
    if (busy && ["warning", "completed", "cancelled", "idle"].includes(phase)) phase = "starting";
    if (!busy && ["checking", "waiting", "starting", "running", "working", "retrying"].includes(phase)) {
      phase = warning ? "warning" : "idle";
    }
    const labels = {checking: "Checking", waiting: "Waiting for author", starting: "Connecting",
      running: "Connecting", working: "Working", retrying: "Retrying", completed: "Completed",
      cancelled: "Cancelled", warning: "Unavailable", idle: "Waiting"};
    let text = phase === "warning" ? warning?.message || last?.text : last?.text;
    if (phase === "idle") text = "Waiting for the next audit.";
    if (phase === "starting" && last?.status !== "starting") text = "Waiting for a model response.";
    text = String(text || "Waiting for a model response.")
      .replace(/^Warning: Audit-\d+ — .*? skipped: /, "").replace(/\. No report saved\.$/, "");
    return [{slot, model, name: choices.find((item) => item.value === model)?.label || model,
      phase, label: labels[phase] || phase, text, log, time: last?.time, report: last?.report}];
  });
  show(ui.researchAuditActivity, rows.length > 0 && !["input", "reviewed", "reviewing"].includes(state.phase));
  const signature = JSON.stringify([currentJob, rows]);
  if (ui.researchAuditActivity.dataset.signature !== signature) {
    const open = new Set([...ui.researchAuditActivity.querySelectorAll("details[open]")].map((item) => item.dataset.slot));
    const cards = rows.map((row) => {
      const card = document.createElement("section");
      card.className = "research-auditor" + (["warning", "retrying"].includes(row.phase) ? " unavailable" : "");
      const head = document.createElement("div"); head.className = "research-auditor-head";
      const name = document.createElement("strong"); name.textContent = `Audit ${row.slot} · ${row.name}`;
      const badge = document.createElement("span"); badge.className = "research-auditor-badge"; badge.textContent = row.label;
      head.append(name, badge);
      const message = document.createElement("p"); message.textContent = row.text;
      const time = document.createElement("small");
      if (row.time) time.dataset.auditTime = row.time;
      card.append(head, message, time);
      if (row.report && /^AUDITS\/[a-zA-Z0-9_.+-]+\.md$/.test(row.report)) {
        const link = document.createElement("button"); link.className = "memory-link"; link.textContent = "Read report";
        link.onclick = () => { ui.memoryPanel.open = true; selectMemoryDocument(row.report); };
        card.append(link);
      }
      if (row.log.length) {
        const details = document.createElement("details"); details.dataset.slot = String(row.slot);
        details.open = open.has(String(row.slot));
        const summary = document.createElement("summary"); summary.textContent = `Activity · ${row.log.length} recent events`;
        const log = document.createElement("ol");
        for (const entry of row.log) {
          const line = document.createElement("li");
          line.textContent = `${clockText(entry.time)} · ${entry.status}: ${entry.text || ""}`;
          log.append(line);
        }
        details.append(summary, log); card.append(details);
      }
      return card;
    });
    ui.researchAuditActivity.replaceChildren(...cards);
    ui.researchAuditActivity.dataset.signature = signature;
  }
  for (const time of ui.researchAuditActivity.querySelectorAll("[data-audit-time]")) {
    time.textContent = `Last activity ${clockText(time.dataset.auditTime)} · ${elapsedText(time.dataset.auditTime)} ago`;
  }
}

async function applyResearchAudits() {
  const config = researchAuditValues(true);
  if (!(Number.isFinite(config.intervalHours) && config.intervalHours > 0)) {
    researchAuditFeedback = "Set an interval greater than zero hours.";
    renderResearchAudits(!ui.researchAuditsControl.hidden);
    ui.liveResearchAuditInterval.focus();
    return;
  }
  const job = currentJob;
  researchAuditFeedback = "";
  researchAuditSaving = true;
  renderResearchAudits(true);
  try {
    const next = await request(jobPath("/set-research-audits"), config);
    if (job !== currentJob) return;
    researchAuditDirty = JSON.stringify(researchAuditValues(true)) !== JSON.stringify(config);
    researchAuditSaving = false;
    render(next);
  } catch (error) {
    if (job !== currentJob) return;
    researchAuditSaving = false;
    researchAuditFeedback = error.message;
    renderResearchAudits(!ui.researchAuditsControl.hidden);
  }
}

async function startResearchAudit() {
  if (ui.startResearchAudit.disabled) return;
  const job = currentJob;
  researchAuditStarting = true;
  researchAuditFeedback = "";
  renderResearchAudits(true);
  try {
    const next = await request(jobPath("/start-research-audit"), {});
    if (job !== currentJob) return;
    researchAuditStarting = false;
    render(next);
  } catch (error) {
    if (job !== currentJob) return;
    researchAuditStarting = false;
    researchAuditFeedback = error.message === "Not found."
      ? "Pause the author, restart the web UI, and resume to enable Start audit now."
      : error.message;
    renderResearchAudits(!ui.researchAuditsControl.hidden);
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
  const auditHolding = Boolean(state.auditHoldingAuthor) && ["pausing", "paused"].includes(phase);
  const working = ["reviewing", "running", "stopping", "pausing"].includes(phase) || auditHolding;
  const reviewOnlyResult = phase === "done"
    && state.statementReviewOnly && Boolean(state.review);
  const reviewReady = phase === "reviewed" || reviewOnlyResult;
  show(ui.input, phase === "input");
  show(ui.review, reviewReady);
  show(
    ui.run,
    ["reviewing", "running", "stopping", "pausing", "paused", "prepared", "done"].includes(phase)
      && !reviewOnlyResult,
  );
  show(ui.workflowRail, phase !== "input" && managesResearchFiles());
  show(ui.activityToggle, Boolean(currentJob));
  syncMemoryPanel();
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
    ui.fileManagement.checked = Boolean(state.fileManagement);
    syncPrompts(state);
    updateModelSummary();
    ui.criticRounds.value = state.criticRounds || 2;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.thinkingHours.value = state.thinkingHours || 168;
    ui.thinkingHours.min = hours.minimum || 0.01;
    ui.thinkingHours.max = hours.maximum || 168;
    fillResearchAuditControls();
    setProblemMode(state.problemMode || "statement");
  }
  if (reviewReady && (previousPhase !== phase || reviewPending)) {
    ui.fileManagement.checked = state.fileManagement !== false;
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
    fillResearchAuditControls();
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
    : auditHolding ? "Research audits"
    : phase === "prepared" ? "Ready to start" : phase === "paused" ? "Paused" : phase === "pausing" ? "Pausing"
    : phase === "reviewed" ? "Waiting for approval"
      : phase === "stopping" ? "Stopping"
        : done ? "Finished" : (node.label || "Codex is working");
  ui.runLabel.textContent = auditHolding ? "RESEARCH AUDITS"
    : phase === "prepared" ? "PREPARED RUN" : phase === "paused" ? "RUN PAUSED" : done ? "RUN COMPLETE" : (node.short_label || "CODEX");
  ui.runTitle.textContent = phase === "reviewing" ? "Checking the statement…"
    : auditHolding ? (phase === "pausing" ? "Pausing author for research audits…" : "Author paused for research audits")
    : phase === "prepared" ? "Prepared research workspace" : phase === "paused" ? "Ready to resume" : phase === "pausing" ? "Pausing Codex…"
    : phase === "stopping" ? "Stopping safely…"
      : (state.error || (done ? (node.label || "Final result")
        : (node.label || "Codex is working")));
  ui.runDescription.textContent = state.error
    || (auditHolding ? "The author will resume automatically in the same conversation after all audit reports are saved. Click Pause to cancel the audits and keep the author paused."
      : phase === "prepared" ? "Review the reorganized research files, then start a fresh author session in this workspace."
      : phase === "paused" ? "Resume reopens the saved Codex conversation in this same run folder. You can change your Codex CLI login before resuming."
      : phase === "pausing" ? "Interrupting the current turn and preserving the saved conversation. Wait for Paused before closing the UI."
      : done ? "The output and transcript remain preserved for this job."
        : (node.description || ""));
  show(ui.roundBadge, Boolean(state.round && ["critic", "author"].includes(state.activeNode)));
  ui.roundBadge.textContent = `Round ${state.round} / ${state.criticRounds}`;
  const authorLimit = Number(state.thinkingHours || 168);
  const maximumAuthorLimit = Number(
    state.workflow?.settings?.thinking_hours?.maximum || 168
  );
  const authorRunning = phase === "running"
    && ["solve", "repair"].includes(state.stage) && state.activeNode === "author";
  const canSetAuthorLimit = authorRunning || (phase === "paused"
    && ["solve", "repair"].includes(state.stage) && state.activeNode === "author");
  renderResearchAudits(canSetAuthorLimit);
  show(ui.authorSteerControl, authorRunning);
  ui.sendAuthorSteer.disabled = !authorRunning;
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
  show(ui.pause, authorRunning || auditHolding);
  ui.pause.title = auditHolding ? "Cancel the audits and keep the author paused" : "Pause the author";
  show(ui.resume, phase === "paused" || phase === "prepared");
  ui.resume.textContent = phase === "prepared" ? "Start prepared run" : "Resume";
  ui.resume.disabled = auditHolding;
  show(ui.downloadTex, Boolean(state.canDownloadTex));
  show(ui.stop, working);
  ui.stop.disabled = phase === "stopping";
  ui.run.setAttribute("aria-busy", String(working));

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
  if (!(skipReview ? promptValues[selectedAuthorPrompt()] : promptValues.review)) syncPrompts();
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
      promptOverrides,
      fileManagement: managesResearchFiles(),
      criticRounds: Number(ui.criticRounds.value),
      thinkingHours: Number(ui.thinkingHours.value),
      researchAudits: managesResearchFiles() ? researchAuditValues()
        : { intervalHours: 0, models: ["none", "none", "none"] },
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
      promptOverrides: Object.hasOwn(promptOverrides, "final")
        ? { final: promptOverrides.final } : {},
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
ui.fileManagement.onchange = () => {
  setProblemMode(selectedProblemMode());
  if (ui.promptDialog.open && ["author", "author_simple"].includes(activePrompt)) {
    selectPrompt(selectedAuthorPrompt());
  }
};
for (const input of ui.problemModes) {
  input.onchange = () => setProblemMode(input.value);
}
ui.editPrompts.onclick = openPromptEditor;
ui.promptTabs.onclick = (event) => {
  const tab = event.target.closest("[data-prompt]");
  if (tab) selectPrompt(tab.dataset.prompt);
};
ui.resetPrompt.onclick = async () => {
  try {
    const defaults = await currentPromptDefaults();
    if (!defaults) return;
    promptDrafts[activePrompt] = defaults[activePrompt] || "";
    ui.promptEditor.value = promptDrafts[activePrompt];
    updatePromptHelp();
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
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
ui.applyResearchAudits.onclick = applyResearchAudits;
ui.startResearchAudit.onclick = startResearchAudit;
for (const input of [ui.liveResearchAuditInterval, ...ui.liveResearchAuditModels]) {
  input.addEventListener("input", () => {
    researchAuditDirty = true;
    researchAuditFeedback = "";
    renderResearchAudits(true);
  });
}
ui.memoryPanel.addEventListener("toggle", () => {
  ++memoryRequest;
  clearTimeout(memoryTimer);
  if (ui.memoryPanel.open) loadMemory();
});
ui.memoryApproach.onchange = () => selectMemoryDocument(
  /^APPROACHES\/(?:index|INDEX)\.md$/.test(ui.memoryApproach.value)
    ? "APPROACHES" : ui.memoryApproach.value,
);
ui.approachIndex.onclick = () => selectMemoryDocument("APPROACHES");
for (const tab of memoryTabs) {
  tab.onclick = () => selectMemoryFile(tab);
  tab.onkeydown = (event) => {
    const index = memoryTabs.indexOf(tab);
    const count = memoryTabs.length;
    const next = { ArrowRight: (index + 1) % count, ArrowLeft: (index + count - 1) % count,
      Home: 0, End: count - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    memoryTabs[next].focus();
    selectMemoryFile(memoryTabs[next]);
  };
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadMemory();
  else clearTimeout(memoryTimer);
});
ui.stop.onclick = () => act("/stop");
ui.pause.onclick = () => act("/pause");
ui.resume.onclick = () => act(state.phase === "prepared" ? "/continue-stopped" : "/resume");
ui.downloadTex.onclick = async () => {
  ui.downloadTex.disabled = true;
  try {
    const response = await fetch(jobPath("/download-tex"), {
      headers: { "X-TCS-Prover-Token": sessionToken },
    });
    if (!response.ok) throw new Error((await response.json()).error || "Download failed.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "final.tex";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  } finally {
    ui.downloadTex.disabled = false;
  }
};
ui.home.onclick = goHome;
ui.reviewHome.onclick = goHome;
window.onpopstate = () => {
  currentJob = new URLSearchParams(location.search).get("job") || "";
  clearJobView();
  refresh();
};

refresh();
