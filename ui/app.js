"use strict";

// Keep the secret launch token in this tab, not in the visible URL.
const sessionToken = location.hash.slice(1)
  || sessionStorage.getItem("tcs-prover-token") || "";
if (location.hash) {
  sessionStorage.setItem("tcs-prover-token", sessionToken);
  history.replaceState(null, "", location.pathname + location.search);
}
let currentJob = new URLSearchParams(location.search).get("job") || "";
let currentBatch = new URLSearchParams(location.search).get("batch") || "";
let composingProblem = new URLSearchParams(location.search).get("new") === "1" && !currentJob;
let batchCatalog = [];

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
  retrievalSetting: $("retrievalSetting"), retrievalMode: $("retrievalMode"),
  retrievalCutoffSetting: $("retrievalCutoffSetting"), retrievalCutoffUtc: $("retrievalCutoffUtc"),
  retrievalSettingHelp: $("retrievalSettingHelp"), retrievalConfigurationNotice: $("retrievalConfigurationNotice"),
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
  jobsHeading: $("jobsHeading"), jobsEmpty: $("jobsEmpty"),
  batchesPanel: $("batchesPanel"), batchesList: $("batchesList"), batchesStatus: $("batchesStatus"),
  batchNavigation: $("batchNavigation"), allBatches: $("allBatchesButton"),
  batchProblems: $("batchProblemsButton"), selectedBatchBadge: $("selectedBatchBadge"),
  batchReadOnlyNotice: $("batchReadOnlyNotice"), newProblem: $("newProblemButton"),
  check: $("checkButton"), recheck: $("recheckButton"), approve: $("approveButton"),
  runFolder: $("runFolderButton"), runFolderHelp: $("runFolderHelp"), folderInput: $("folderInput"),
  batchStatus: $("batchStatus"),
  stop: $("stopButton"),
  pause: $("pauseButton"), resume: $("resumeButton"), downloadTex: $("downloadTexButton"),
  downloadPdf: $("downloadPdfButton"),
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
  runOverview: $("runOverview"), runProblemTitle: $("runProblemTitle"),
  runStage: $("runStage"), runActiveTime: $("runActiveTime"), runBudget: $("runBudget"),
  runHealth: $("runHealth"), runNextStep: $("runNextStep"), runLatestAction: $("runLatestAction"),
  statementHeading: $("statementHeading"), runStatement: $("runStatement"),
  originalStatementDetails: $("originalStatementDetails"), runOriginalStatement: $("runOriginalStatement"),
  runMetadata: $("runMetadata"), resultPanel: $("resultPanel"), resultHeading: $("resultHeading"),
  criticReviewHistory: $("criticReviewHistory"),
  resultDescription: $("resultDescription"), resultOutput: $("resultOutput"),
  retrievalPanel: $("retrievalPanel"), retrievalHeading: $("retrievalHeading"),
  retrievalStatus: $("retrievalStatus"), retrievalDescription: $("retrievalDescription"),
  retrievalCutoffDisplay: $("retrievalCutoffDisplay"), retrievalCutoffValue: $("retrievalCutoffValue"),
  retrievalCutoffLabel: $("retrievalCutoffLabel"),
  retrievalPolicyNote: $("retrievalPolicyNote"), retrievalCounts: $("retrievalCounts"),
  retrievalDetails: $("retrievalDetails"), retrievalLimitation: $("retrievalLimitation"),
  retrievalCountNote: $("retrievalCountNote"),
  retrievalRequestSummary: $("retrievalRequestSummary"), retrievalFailures: $("retrievalFailures"),
  globalStatus: $("globalStatus"), liveDot: $("liveDot"), elapsed: $("elapsed"),
  modelSummary: $("modelSummary"),
  lastActivity: $("lastActivity"), timeline: $("timelineList"), traceNotice: $("traceNotice"),
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
let stateReceivedAt = Date.now();
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
let memoryFile = "APPROACHES";
let memoryAnchor = "";
let memoryVersion = "";
let memoryTimer;
let memoryRequest = 0;
let approachGraphData = { nodes: [], files: [], indexFile: "" };
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
    if (body !== undefined && (state.readOnly || selectedBatch()?.readOnly)) {
      throw new Error("This saved batch is read-only. Open the current batch or create a new problem from All batches.");
    }
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

// Batch identity keeps equal run names in different folders separate.
function jobPath(path, values = {}, runId = currentJob) {
  const query = new URLSearchParams(values);
  if (runId) query.set("job", runId);
  if (currentBatch) query.set("batch", currentBatch);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function show(element, visible) {
  element.hidden = !visible;
}

function jobUrl(runId, token = false) {
  const hash = token ? `#${sessionToken}` : "";
  return `${jobPath(location.pathname, {}, runId)}${hash}`;
}

function batchUrl(batchId) {
  return batchId ? `${location.pathname}?batch=${encodeURIComponent(batchId)}` : location.pathname;
}

function selectedBatch() {
  return batchCatalog.find(batch => batch.id === currentBatch);
}

function renderBatchNavigation() {
  const batch = selectedBatch();
  const browsing = !currentJob && !composingProblem;
  show(ui.batchesPanel, browsing && !currentBatch);
  show(ui.jobsPanel, browsing && Boolean(currentBatch));
  show(ui.batchNavigation, Boolean(currentJob || currentBatch || composingProblem));
  show(ui.batchProblems, Boolean(currentBatch));
  ui.batchProblems.textContent = batch?.label || currentBatch;
  ui.batchProblems.disabled = !currentJob;
  show(ui.selectedBatchBadge, Boolean(batch?.current));
  ui.selectedBatchBadge.textContent = "Current batch";
  show(ui.batchReadOnlyNotice, Boolean(batch?.readOnly || state.readOnly));
  ui.home.textContent = currentBatch ? "Back to problems" : "All batches";
  ui.reviewHome.textContent = ui.home.textContent;
  if (!currentJob) {
    show(ui.input, composingProblem);
    for (const panel of [ui.review, ui.run, ui.runOverview, ui.workflowRail, ui.activityToggle]) show(panel, false);
    ui.globalStatus.textContent = composingProblem ? "New problem" : currentBatch ? "Batch problems" : "Problem batches";
    ui.elapsed.textContent = "";
    ui.liveDot.classList.toggle("active", false);
  }
}

function renderBatches(batches) {
  batchCatalog = batches;
  const signature = JSON.stringify(batches);
  if (ui.batchesList.dataset.signature === signature) {
    renderBatchNavigation();
    return;
  }
  ui.batchesList.dataset.signature = signature;
  ui.batchesList.replaceChildren();
  ui.batchesStatus.textContent = batches.length ? "" : "No batches yet. Create a problem to get started.";
  show(ui.batchesStatus, !batches.length);
  for (const batch of batches) {
    const item = document.createElement("li"), link = document.createElement("a");
    item.className = `batch-card${batch.current ? " current" : ""}`;
    item.dataset.batch = batch.id;
    link.className = "batch-card-link";
    link.href = batchUrl(batch.id);
    link.onclick = event => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      selectBatch(batch.id);
    };
    const top = document.createElement("div"), mark = document.createElement("span");
    top.className = "batch-card-top";
    mark.className = "batch-folder-mark"; mark.textContent = "▤"; mark.setAttribute("aria-hidden", "true");
    top.append(mark);
    if (batch.current || batch.readOnly) {
      const badge = document.createElement("span");
      badge.className = `batch-badge${batch.current ? "" : " archive"}`;
      badge.textContent = batch.current ? "Current batch" : "Previous batch · read-only";
      top.append(badge);
    }
    const copy = document.createElement("div"), title = document.createElement("h2"), count = document.createElement("p");
    title.textContent = batch.label || batch.id;
    count.className = "batch-card-count";
    const total = Number(batch.jobCount || 0);
    count.textContent = `${total} ${total === 1 ? "problem" : "problems"}`;
    copy.append(title, count);
    const progress = document.createElement("p"); progress.className = "batch-progress";
    for (const [key, label] of [["running", "running"], ["paused", "paused"], ["done", "finished"]]) {
      const value = Number(batch.counts?.[key] || 0);
      if (!value) continue;
      const stat = document.createElement("span"); stat.className = key;
      stat.textContent = `${value} ${label}`; progress.append(stat);
    }
    const time = document.createElement("span"), open = document.createElement("span");
    time.className = "batch-card-time";
    time.textContent = batch.lastActivityAt ? `Last activity ${localDateTime(batch.lastActivityAt)}`
      : batch.startedAt ? `Started ${localDateTime(batch.startedAt)}` : "Saved research workspace";
    open.className = "batch-card-open"; open.textContent = "Open problems →";
    link.append(top, copy, progress, time, open); item.append(link); ui.batchesList.append(item);
  }
  renderBatchNavigation();
}

async function selectBatch(batchId) {
  currentBatch = batchId;
  currentJob = "";
  composingProblem = false;
  history.pushState(null, "", batchUrl(batchId));
  clearTimeout(timer); clearInterval(clock); clearTimeout(jobsTimer);
  clearJobView(); show(ui.notice, false); renderBatchNavigation();
  ui.jobsList.replaceChildren();
  ui.jobsHeading.textContent = selectedBatch()?.label || currentBatch;
  ui.jobsCount.textContent = "Loading…";
  show(ui.jobsEmpty, false);
  await loadJobs();
}

async function newProblem() {
  currentBatch = ""; currentJob = ""; composingProblem = true;
  history.pushState(null, "", `${location.pathname}?new=1`);
  clearTimeout(timer); clearInterval(clock); clearTimeout(jobsTimer);
  clearJobView(); show(ui.notice, false); renderBatchNavigation();
  await refresh();
}

function adoptJob(next) {
  currentJob = next.runId;
  currentBatch = next.batchId || next.batch?.id || "";
  composingProblem = false;
  history.pushState(null, "", jobUrl(currentJob));
}

function clearJobView() {
  previousPhase = "";
  ++memoryRequest;
  clearTimeout(memoryTimer);
  memoryJob = null;
  ui.memoryPanel.open = false;
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
  composingProblem = false;
  history.pushState(null, "", jobUrl(runId));
  clearJobView();
  clearTimeout(timer);
  clearInterval(clock);
  clearTimeout(jobsTimer);
  refresh();
}

async function deleteJob(job, title) {
  const question = `Delete “${title}”?\n\nIts files will move to runs/.trash/.`;
  if (!confirm(question)) return;
  try {
    const result = await request(
      jobPath("/delete-job", {}, job.runId), {}
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
      jobPath("/resume-critic", {}, job.runId), {}
    );
    adoptJob(next);
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
      jobPath("/resume-checkpoint", {}, job.runId),
      { checkpoint: checkpoint.id },
    );
    adoptJob(next);
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
      jobPath("/continue-stopped", {}, job.runId), {}
    );
    adoptJob(next);
    clearTimeout(jobsTimer);
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function goHome() {
  await selectBatch(currentBatch);
}

function retrievalConfiguration(source) {
  // A persisted archive policy remains authoritative, including older runs.
  const mode = source.retrievalPolicyPath ? "historical"
    : source.retrieval?.mode || source.retrievalMode || "live";
  return { mode, cutoff: mode === "disabled" ? "-1"
    : source.retrieval?.cutoffExclusiveUtc || source.retrievalCutoffUtc || "" };
}

function retrievalSummary(source) {
  const { mode, cutoff } = retrievalConfiguration(source);
  return mode === "historical" ? `Cutoff on · historical archives before ${cutoff || "fixed cutoff"} · proof author only`
    : mode === "live" ? "Cutoff off · unrestricted model web search"
    : "Cutoff on · -1 · web-search tools disabled";
}

function renderJobs(jobs) {
  ui.jobsList.replaceChildren();
  ui.jobsCount.textContent = `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}`;
  show(ui.jobsPanel, true);
  show(ui.jobsEmpty, jobs.length === 0);
  ui.jobsHeading.textContent = selectedBatch()?.label || "Problems and checkpoints";
  for (const job of jobs) {
    const readOnly = Boolean(job.readOnly || selectedBatch()?.readOnly);
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
      running: "Running", stopping: "Stopping", pausing: "Pausing", paused: "Paused", done: completedRunLabel(job),
      prepared: "Ready to start",
    };
    status.textContent = job.launchHold ? "Prepared · awaiting review"
      : job.manuallyStopped
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
    if (job.phase === "running" && job.stageLabel && !job.launchHold) status.textContent = `Running · ${job.stageLabel}`;
    if (job.error && !job.manuallyStopped) {
      const reason = document.createElement("small");
      reason.className = "job-error";
      reason.textContent = job.error;
      copy.append(reason);
    }
    if (job.launchHold) {
      const hold = document.createElement("small");
      hold.className = "job-tags";
      hold.textContent = job.launchHold;
      copy.append(hold);
    }
    if (job.sourceFile) {
      const tags = document.createElement("small");
      tags.className = "job-tags";
      tags.textContent = `From ${job.sourceFile}`;
      copy.append(tags);
    }
    const retrieval = document.createElement("small");
    retrieval.className = "job-retrieval";
    retrieval.textContent = retrievalSummary(job);
    copy.append(retrieval);
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
        if (checkpoint.resumable && !readOnly) {
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
    resume.hidden = readOnly || !job.canResumeCritic || checkpoints.length > 0;
    resume.onclick = () => resumeCritic(job, title.textContent);
    const continueButton = document.createElement("button");
    continueButton.className = "secondary compact";
    continueButton.textContent = job.continueStoppedLabel
      || "Continue stopped job";
    continueButton.hidden = readOnly || !job.canContinueStopped;
    continueButton.title = job.continueStoppedDescription || "";
    continueButton.onclick = () => continueStopped(job, title.textContent);
    const remove = document.createElement("button");
    remove.className = "ghost compact job-delete";
    remove.textContent = "Delete";
    remove.hidden = readOnly;
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
  if (currentJob || composingProblem) return;
  const batch = currentBatch;
  try {
    const catalog = await request("/batches");
    if (currentJob || composingProblem || batch !== currentBatch) return;
    renderBatches(catalog.batches || []);
    if (batch) {
      if (!selectedBatch()) throw new Error("This batch is no longer available. Return to All batches.");
      const result = await request(jobPath("/jobs"));
      if (currentJob || composingProblem || batch !== currentBatch) return;
      renderJobs(result.jobs || []);
    }
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
  if (!currentJob && !composingProblem && batch === currentBatch) jobsTimer = setTimeout(loadJobs, 2500);
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
  show(ui.runFolder, statement && !reviewOnly);
  show(ui.runFolderHelp, statement && !reviewOnly);
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
  show(ui.retrievalSetting, true);
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
  renderRetrievalSettings();
}

function selectedRetrievalMode() {
  if (ui.retrievalMode.value === "live") return "live";
  return ui.retrievalMode.value === "disabled" || ui.retrievalCutoffUtc.value.trim() === "-1"
    ? "disabled" : "historical";
}

function selectedRetrievalSettings() {
  const mode = selectedRetrievalMode();
  return { retrievalMode: mode, retrievalCutoffUtc: mode === "disabled" ? "-1"
    : mode === "historical" ? ui.retrievalCutoffUtc.value.trim() : "" };
}

function restoreRetrievalSettings(source) {
  const { mode, cutoff } = retrievalConfiguration(source);
  ui.retrievalMode.value = mode === "live" ? "live" : "historical";
  ui.retrievalCutoffUtc.value = mode === "live" ? "" : cutoff;
}

function renderRetrievalSettings() {
  const mode = selectedRetrievalMode(), historical = mode === "historical";
  const enabled = mode !== "live";
  const capabilities = state.retrievalCapabilities || {};
  show(ui.retrievalCutoffSetting, enabled);
  ui.retrievalCutoffUtc.required = enabled;
  ui.retrievalCutoffUtc.disabled = !enabled;
  ui.retrievalSettingHelp.textContent = historical
    ? "The proof author discovers general web sources through Codex, then reads verified archived captures before your cutoff. The critic and writer have no external retrieval tools."
    : mode === "disabled" ? "All model web-search tools are disabled. This setting controls search tools; it does not isolate ordinary shell network access."
    : "The model's native web search is enabled without a date restriction.";
  const message = !historical ? ""
    : selectedProblemMode() === "latex" || ui.statementReviewOnly.checked
      ? "A historical cutoff requires a full proof job with Skip statement review. For this workflow, use cutoff Off or enter -1 to disable web-search tools."
    : capabilities.historicalAvailable !== true
    ? capabilities.reason || "Historical search setup is unavailable in this interface. Open the updated TCS Prover interface to create a historical job."
    : !ui.skipStatementReview.checked
      ? "Select Skip statement review below to start a historical proof job."
      : "Use Codex models for author, critic and writer, with optional research audits set to None.";
  ui.retrievalConfigurationNotice.textContent = message;
  show(ui.retrievalConfigurationNotice, Boolean(message));
}

function validateRetrievalSettings({ folder = false } = {}) {
  if (selectedRetrievalMode() !== "historical") return true;
  let message = "", focus = ui.retrievalCutoffUtc;
  const value = ui.retrievalCutoffUtc.value.trim();
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?(?:Z|\+00:00)$/.exec(value);
  const base = match && `${match[1]}T${match[2]}Z`;
  const parsed = base && new Date(base);
  if (selectedProblemMode() === "latex" || ui.statementReviewOnly.checked) {
    message = "A historical cutoff requires a full proof job with Skip statement review. For this workflow, use cutoff Off or enter -1 to disable web-search tools.";
    focus = ui.retrievalMode;
  } else if (state.retrievalCapabilities?.historicalAvailable !== true) {
    message = state.retrievalCapabilities?.reason || "This interface cannot create historical jobs. Open the updated TCS Prover interface before starting this job.";
    focus = ui.retrievalMode;
  } else if (!match || !Number.isFinite(parsed?.getTime()) || parsed.toISOString().slice(0, 19) !== base.slice(0, 19)) {
    message = "Enter -1 to disable web-search tools, or a valid UTC cutoff such as 2026-10-02T14:13:12Z. Include seconds and the UTC timezone.";
  } else if (Date.parse(value) >= Date.now()) {
    message = "Choose a historical cutoff in the past.";
  } else if (!folder && !ui.skipStatementReview.checked) {
    message = "Historical search currently requires Skip statement review.";
    focus = ui.skipStatementReview;
  } else if (["author", "critic", "writer"].some(role => ui[`${role}Model`].value === "deepseek-v4-pro")) {
    message = "Historical proof jobs require Codex models for the author, critic and writer.";
    focus = ui.authorModel;
  } else if (managesResearchFiles() && researchAuditValues().models.some(model => model !== "none")) {
    message = "Set each optional research audit to None for a historical proof job.";
    ui.researchAuditsSetting.open = true;
    focus = ui.researchAuditModels[0];
  }
  if (!message) return true;
  ui.notice.textContent = message;
  show(ui.notice, true);
  ui.retrievalSetting.closest("details").open = true;
  focus.focus();
  return false;
}

// Keep the compact footer label synchronized with every model setting.
function updateModelSummary() {
  const deepseekModel = "deepseek-v4-pro";
  // Model ids share suffixes ("sol", "luna"), so name every known model.
  const modelNames = new Map([
    ["gpt-6.1-sol", "6.1 Sol"], ["gpt-6-astra", "Astra"],
    ["gpt-6-sol", "6 Sol"], ["gpt-6-luna", "6 Luna"],
    ["gpt-5.6-sol", "5.6 Sol"], ["gpt-5.6-terra", "5.6 Terra"],
    ["gpt-5.6-luna", "5.6 Luna"], ["deepseek-v4-pro", "DeepSeek V4 Pro"],
  ]);
  const name = (model) => modelNames.get(model)
    || model.split("-").at(-1).replace(/^./, (letter) => letter.toUpperCase());
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
      ? "Fast for ChatGPT · Standard for DeepSeek" : "Fast speed";
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
  const batch = currentBatch;
  const next = await request(jobPath("/state"));
  if (job !== currentJob || batch !== currentBatch) return null;
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
  if (entry.kind === "status" && entry.label === "Critic approved"
      && entry.root !== false && !entry.activityLabel) return true;
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
    const verdict = String(report.verdict || "").toLowerCase();
    const match = criticReviewModel().byResult.get(entry);
    const round = match?.round || collectCriticReviewCycles([entry]).at(-1)?.rounds.at(-1);
    const outcome = criticRoundOutcomeLabel(round || {});
    return {
      key: `critic:${entry.time || entry.round}`, type: "agent",
      label: `${match ? `Review cycle ${match.cycleNumber} · round ${match.roundNumber}` : entry.label || "Critic result"} · ${outcome}`,
      text: verdict === "reject"
        ? (report.bugs || "Unfixable issues return to the proof author.")
        : verdict === "pass" ? outcome + (round?.outcome === "pass_edited_limit"
          ? ". The changed proof is accepted without another critic round because the round limit was reached." : ".")
          : "No valid critic verdict was recorded for this response.",
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
  if (!/^(?:PROVED\.md|APPROACHES\.md|audit\.md|APPROACHES\/(?:index|INDEX|A\d{3,}(?:-[A-Za-z0-9_-]+)?)\.md|(?:AUDITS|audit_history)\/[A-Za-z0-9][A-Za-z0-9_.-]*\.md)$/.test(name)) return null;
  return { name, anchor };
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

// Each node's own Parents field defines its incoming edges; the index is optional.
function parseApproachGraph(records) {
  const nodes = records.map(record => ({...record, parents: [...new Set(record.parents || [])],
    title: plainMarkdown(record.title || record.id), children: [],
    result: plainMarkdown(record.result || "No summary has been saved in this file yet.")}));
  const counts = new Map();
  for (const node of nodes) counts.set(node.id, (counts.get(node.id) || 0) + 1);
  const byId = new Map(nodes.filter(node => counts.get(node.id) === 1).map(node => [node.id, node]));
  const warnings = [];
  if (byId.size !== nodes.length) warnings.push("Some approach files share an ID; their ambiguous connections are not shown.");
  for (const node of nodes) {
    if (node.readStatus && node.readStatus !== "ready") warnings.push(`${node.id}: file temporarily unavailable; retrying on refresh.`);
    else {
      if (node.hasParents === false) warnings.push(`${node.id}: Parents field is missing; its dependencies are unknown.`);
      if (node.status === "UNSPECIFIED") warnings.push(`${node.id}: Status field is missing or unrecognized.`);
    }
  }
  const edges = [];
  const missing = new Set();
  for (const node of nodes) {
    if (!byId.has(node.id)) continue;
    for (const parent of node.parents) {
      if (byId.has(parent)) {
        edges.push({ from: parent, to: node.id });
        byId.get(parent).children.push(node.id);
      } else if (!counts.has(parent)) missing.add(parent);
    }
  }
  if (missing.size) warnings.push(`Parent files missing from the workspace: ${[...missing].join(", ")}.`);
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
    warnings.push("The approach files contain a dependency cycle. Check their Parents fields; cyclic nodes are displayed together.");
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
  const signature = JSON.stringify(approachGraphData);
  if (signature === approachGraphSignature) {
    for (const button of ui.approachGraphCanvas.querySelectorAll("button")) {
      button.setAttribute("aria-current", String(button.dataset.file === memoryFile));
    }
    return;
  }
  approachGraphSignature = signature;
  const graph = parseApproachGraph(approachGraphData.nodes);
  ui.approachGraphSummary.textContent = graph.nodes.length
    ? `${graph.nodes.length} approach files · ${graph.edges.length} connections · Parent → child. Select a node to read its file.`
    : "Saved approach files will appear here. Each file’s Parents field defines its connections.";
  if (approachGraphData.status === "unavailable") graph.warnings.push("The approach folder is temporarily unavailable; retrying on refresh.");
  ui.approachGraphWarning.textContent = graph.warnings.join(" ");
  show(ui.approachGraphWarning, graph.warnings.length > 0);
  show(ui.approachIndex, Boolean(approachGraphData.indexFile));
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
  const graphView = memoryFile.startsWith("APPROACHES");
  show(ui.memoryApproachPicker, files.length > 0);
  if (!files.length) return;
  const selected = ["AUDITS", "audit_history"].includes(memoryFile) ? file.name : memoryFile;
  const available = graphView ? ["APPROACHES", ...files] : files;
  const choices = available.includes(selected) ? available : [...available, selected];
  if (JSON.stringify([...ui.memoryApproach.options].map((option) => option.value)) !== JSON.stringify(choices)) {
    ui.memoryApproach.replaceChildren(...choices.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name === "APPROACHES" ? "Approach graph"
        : /^APPROACHES\/(?:index|INDEX)\.md$/.test(name) ? "Index"
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
  const batch = currentBatch;
  const requestId = ++memoryRequest;
  try {
    if (memoryFile.startsWith("APPROACHES")) {
      const graph = await request(jobPath("/approach-graph"));
      if (job !== currentJob || batch !== currentBatch || requestId !== memoryRequest || !ui.memoryPanel.open) return;
      approachGraphData = graph;
      renderApproachGraph();
      renderMemoryChoices({files: graph.files});
      if (memoryFile === "APPROACHES") {
        ui.memoryMessage.textContent = graph.status === "unavailable"
          ? "The approach folder is unavailable right now. Trying again shortly…"
          : graph.nodes.length ? "Select an approach to read its saved work."
            : "The author has not created any approach files yet.";
        show(ui.memoryMessage, true);
        ui.memoryUpdated.textContent = "";
        return;
      }
    }
    const file = await request(jobPath("/memory", { file: memoryFile, version: memoryVersion }));
    if (job !== currentJob || batch !== currentBatch || requestId !== memoryRequest || !ui.memoryPanel.open) return;
    renderMemoryChoices(memoryFile.startsWith("APPROACHES") ? {...file, files: approachGraphData.files} : file);
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
    if (job !== currentJob || batch !== currentBatch || requestId !== memoryRequest) return;
    ui.memoryUpdated.textContent = "Could not refresh. Retrying…";
    if (!memoryVersion) {
      ui.memoryMessage.textContent = error.message;
      show(ui.memoryMessage, true);
    }
  } finally {
    if (job === currentJob && batch === currentBatch && requestId === memoryRequest && ui.memoryPanel.open && !ui.memoryPanel.hidden) {
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
  const job = currentJob || state.runId || "";
  const identity = job ? JSON.stringify([currentBatch, job]) : "";
  if (memoryJob !== identity) {
    memoryJob = identity;
    ++memoryRequest;
    clearTimeout(memoryTimer);
    approachGraphData = { nodes: [], files: [], indexFile: "" };
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

function completedRunLabel(job) {
  if (job.manuallyStopped) return "Stopped";
  if (job.resultKind === "review" || job.statementReviewOnly) return "Review complete";
  if (job.resultKind === "failure" || job.error) return "Incomplete";
  return job.resultKind === "final" || job.canDownloadTex ? "Answer ready" : "Incomplete";
}

function localDateTime(value) {
  return value ? new Date(value).toLocaleString([], {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }) : "—";
}

function runElapsedSeconds() {
  if (Number.isFinite(state.activeElapsedSeconds)) {
    const ticking = ["reviewing", "running", "stopping", "pausing"].includes(state.phase);
    return state.activeElapsedSeconds + (ticking ? Math.max(0, Date.now() - stateReceivedAt) / 1000 : 0);
  }
  const start = state.resumedAt || state.startedAt;
  if (!start) return 0;
  const end = state.finishedAt || state.lastActivityAt || start;
  return Number(state.elapsedSeconds || 0) + Math.max(0, (new Date(end) - new Date(start)) / 1000);
}

function durationText(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${String(total % 60).padStart(2, "0")}s`;
}

function renderRunOverview() {
  show(ui.runOverview, state.phase !== "input");
  if (state.phase === "input") return;
  const latex = state.problemMode === "latex";
  const original = latex ? state.latexInput : state.draft;
  const statement = (latex ? state.latexInput : state.statement)
    || state.review?.statement || state.reviewStatement || original || "";
  const title = String(original || statement).trim().split("\n").find(line => line.trim()) || "Untitled problem";
  ui.runProblemTitle.textContent = state.sourceFile
    ? String(state.sourceFile).split(/[\\/]/).pop()
    : title.replace(/^#+\s*/, "").slice(0, 220);
  ui.runProblemTitle.title = title;
  const reviewCycles = criticReviewModel().cycles;
  const latestCycle = reviewCycles.at(-1);
  ui.runStage.textContent = state.phase === "reviewed" ? "Waiting for approval"
    : state.phase === "prepared" ? (state.launchHold ? "Awaiting review" : "Ready to start")
    : state.phase === "done" ? completedRunLabel(state)
    : `${state.workflow?.nodes?.[state.activeNode]?.label || state.stage || "Starting"}`
      + (["paused", "pausing"].includes(state.phase) ? ` · ${state.phase}` : "")
      + (state.activeNode === "critic" && latestCycle ? ` · cycle ${reviewCycles.length} · round ${latestCycle.rounds.length}` : "");
  const seconds = runElapsedSeconds(), hours = Number(state.thinkingHours ?? 168), limit = hours * 3600;
  ui.runActiveTime.textContent = durationText(seconds);
  ui.runBudget.textContent = hours === 0 ? "Unlimited" : `${hours}h · ${durationText(Math.max(0, limit - seconds))} left`;
  ui.runHealth.textContent = state.processAlive == null && state.workerActive == null ? "Unknown · telemetry unavailable"
    : state.processAlive ? "Running"
    : state.workerActive ? "Starting / closing"
    : state.phase === "paused" ? "Paused · saved conversation"
    : ["reviewed", "prepared"].includes(state.phase) ? "Waiting for you" : "No active process";
  const nextSteps = {
    review: "After review, approve the checked statement to start the proof author.",
    solve: "The author is searching for a complete proof. Its candidate then goes to the independent critic.",
    repair: "The author is addressing critic objections. A revised candidate returns to independent review.",
    critic: "The critic checks the candidate. Rejected proofs return to the author; accepted proofs go to the LaTeX editor.",
    final: "The accepted proof is being formatted and compiled. Source and PDF downloads appear when compilation succeeds.",
  };
  ui.runNextStep.textContent = state.phase === "reviewed" ? "Statement review finished. Approval is needed before proof search can begin."
    : state.phase === "paused" ? "No final answer yet. Resume continues the saved conversation and preserves the work already done."
    : state.phase === "prepared" ? (state.launchHold || "Start this prepared workspace to begin proof search.")
    : state.phase === "done" ? (state.resultKind === "final" || state.canDownloadTex
      ? "The final answer is saved below. Source and PDF downloads are available for saved artifacts."
      : state.resultKind === "review" ? "The statement review is saved."
        : "No completed answer was produced. Saved partial work and checkpoints remain available; open the home page to continue from a checkpoint.")
    : state.statementReviewOnly ? "This job will save the checked statement and reviewer notes, then finish."
    : nextSteps[state.stage] || "Waiting for the next workflow update.";
  if (latestCycle && ["latex_editor", "latex_compile", "latex_repair"].includes(state.activeNode)) {
    ui.runNextStep.textContent = `${criticCycleOutcomeLabel(latestCycle)}. ${ui.runNextStep.textContent}`;
  }
  const latest = [...(state.trace || [])].reverse().find(entry => !entry.heartbeat
    && !["request", "review_result", "critic_result", "author_result", "final_result", "failure_result", "partial_result"].includes(entry.kind)
    && describe(entry)?.text);
  const card = latest && describe(latest);
  ui.runLatestAction.textContent = card
    ? `Latest activity · ${clockText(latest.time)} · ${card.label}: ${String(card.text).replace(/\s+/g, " ").slice(0, 260)}`
    : "Public activity appears below as the model works.";
  renderRetrievalOverview();
  ui.statementHeading.textContent = latex ? "Original writing"
    : state.phase === "reviewing" ? "Statement being reviewed"
    : state.phase === "reviewed" ? "Reviewed statement · awaiting approval" : "Statement being solved";
  if (ui.runStatement.textContent !== statement) ui.runStatement.textContent = statement;
  const changed = Boolean(original && original.trim() !== statement.trim());
  show(ui.originalStatementDetails, changed);
  if (ui.runOriginalStatement.textContent !== (original || "")) ui.runOriginalStatement.textContent = original || "";
  const metadata = [
    ["Run", state.runId || "—"], ["Started", localDateTime(state.startedAt)],
    ["Last event", localDateTime(state.lastActivityAt)],
    ["Finished", state.phase === "done" ? localDateTime(state.finishedAt) : "Not finished"],
    ...(state.sourceFile ? [["Input file", state.sourceFile]] : []),
    ["Statement reviewer", state.skipStatementReview ? "Skipped" : `${state.reviewModel} · ${state.reviewEffort || state.reasoningEffort}`],
    ["Proof author", `${state.authorModel} · ${state.authorEffort || state.reasoningEffort}`],
    ["Independent critic", `${state.criticModel} · ${state.criticEffort || state.reasoningEffort} · max ${state.criticRounds} consecutive rounds`],
    ["LaTeX writer", `${state.writerModel} · ${state.writerEffort || state.reasoningEffort}`],
    ["Generation", state.speedMode || "standard"], ["Activity summaries", state.reasoningSummary || "concise"],
    ["Research files", state.fileManagement === false ? "Off" : "Managed"],
    ["Web search", retrievalSummary(state)],
    ["Conversation", state.goalThreadId ? "Saved · resumes in this workspace" : "Not started"],
  ];
  const signature = JSON.stringify(metadata);
  if (ui.runMetadata.dataset.signature !== signature) {
    ui.runMetadata.dataset.signature = signature;
    ui.runMetadata.replaceChildren(...metadata.map(([label, value]) => {
      const row = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
      term.textContent = label; description.textContent = value; row.append(term, description); return row;
    }));
  }
  renderCriticReviewHistory();
  const savedOutput = Boolean(state.output && ["done", "paused"].includes(state.phase));
  show(ui.resultPanel, savedOutput);
  const final = state.resultKind === "final" || state.canDownloadTex;
  ui.resultHeading.textContent = final ? (state.canDownloadTex ? "Final answer · LaTeX source" : "Final answer")
    : state.resultKind === "failure" ? "Failure report" : "Saved partial work";
  ui.resultDescription.textContent = final ? "The complete saved output is shown here."
    : "This is preserved work from an incomplete run; it has not completed the proof and critic workflow.";
  if (savedOutput && ui.resultOutput.textContent !== state.output) ui.resultOutput.textContent = state.output;
  const identity = `${state.batchId || ""}:${state.runId}:${state.resultKind || "partial"}`;
  if (ui.resultPanel.dataset.identity !== identity) {
    ui.resultPanel.dataset.identity = identity;
    ui.resultPanel.open = final || state.resultKind === "failure";
  }
}

// Group the recorded critic calls by author submission, retaining every input.
// A PASS verdict alone never establishes that the workflow accepted the proof.
function collectCriticReviewCycles(trace, options = {}) {
  const cycles = [], seen = new Set(), identities = new Map(), keyCounts = new Map();
  let cycle = null, pending = null, visit = null, handoff = null, index = 0;
  const isRoot = entry => !entry.activityLabel && entry.root !== false
    && !/^Independent critic audit\s+\d+\b/i.test(entry.label || "");
  const stamp = entry => String(entry.requestId || entry.id || entry.time || `event-${index}`);
  const uniqueKey = base => {
    const count = (keyCounts.get(base) || 0) + 1;
    keyCounts.set(base, count);
    return count === 1 ? base : `${base}:${count}`;
  };
  const positive = value => Number.isInteger(value) && value > 0 ? value : null;
  const normalized = value => {
    const lines = value.replace(/\r\n?/g, "\n").split("\n").map(line => line.replace(/\s+$/u, ""));
    while (lines.length && !lines[0]) lines.shift();
    while (lines.length && !lines.at(-1)) lines.pop();
    return lines.join("\n");
  };
  const candidateFrom = request => {
    if (typeof request.candidateText === "string") return request.candidateText;
    // Only parse the known first-attempt prompt. Arbitrary prompts and recovery
    // transcripts cannot safely identify the exact submitted candidate.
    if (request.attempt > 1 || typeof request.text !== "string") return null;
    let text = request.text.replace(/\r\n?/g, "\n");
    const marker = "\n\nCANDIDATE SOLUTION:\n";
    if (!text.startsWith("CRITIC INSTRUCTIONS:\n") || !text.includes("\n\nSTATEMENT:\n")
        || text.split(marker).length !== 2) return null;
    text = text.slice(text.indexOf(marker) + marker.length);
    const suffix = "\n\nREAD-ONLY PROOF FILES:\nYour working directory contains proof.md (the exact candidate), "
      + "statement.md (the exact task), and manifest.json (content hashes and provenance). "
      + "Read both files completely; use successive ranges if tool output is truncated. "
      + "Give independent subagents these same files and working directory. "
      + "Review only this snapshot. Do not resolve links into the author's workspace. "
      + "Return the complete proof text in solution, never a file pointer.";
    const contract = "\n\nOUTPUT JSON CONTRACT\nReturn exactly one JSON object and no markdown fences or commentary. "
      + "The object must match this JSON Schema exactly:\n";
    if (text.includes(contract)) {
      const parts = text.split(contract);
      if (parts.length !== 2) return null;
      try { JSON.parse(parts[1]); } catch { return null; }
      text = parts[0];
    }
    if (text.endsWith(suffix)) text = text.slice(0, -suffix.length);
    else if (text.includes("\n\nREAD-ONLY PROOF FILES:")) return null;
    return text;
  };
  const closePending = () => {
    if (pending && !pending.result) pending.interrupted = true;
    pending = null;
  };
  const boundary = () => {
    closePending();
    if (cycle) cycle.closed = true;
    cycle = null; visit = null; handoff = null;
  };
  const ensureCycle = entry => {
    const explicitId = entry.cycleId || entry.reviewCycleId;
    if (explicitId && cycle && cycle.id && cycle.id !== explicitId) boundary();
    if (!cycle || cycle.closed) {
      cycle = {key: uniqueKey(`cycle:${explicitId || stamp(entry)}`), id: explicitId || null,
        number: cycles.length + 1, rounds: [], startTime: entry.time || null, outcome: "pending", closed: false};
      cycles.push(cycle);
    } else if (explicitId) cycle.id = explicitId;
    return cycle;
  };
  const continuePrevious = current => {
    if (!current || current.closed) return;
    const previous = current.rounds.at(-1);
    if (previous?.result && ["pass_unknown", "pass_edited_continue"].includes(previous.outcome)
        && String(previous.result.report?.verdict || "").toLowerCase() === "pass") {
      previous.outcome = "pass_edited_continue";
      previous.transition = "review_again";
      previous.outcomeEvidence = "next_review";
    }
  };
  const ensureRound = entry => {
    const current = ensureCycle(entry);
    continuePrevious(current);
    const round = {key: uniqueKey(`round:${stamp(entry)}`), number: current.rounds.length + 1,
      roundNumber: positive(entry.round) || visit, requests: [], result: null,
      outcome: "pending", outcomeEvidence: null, transition: null,
      candidateText: null, handoff, startTime: entry.time || null, interrupted: false};
    current.rounds.push(round);
    pending = round;
    return round;
  };
  const classify = (round, entry) => {
    const verdict = String(entry.report?.verdict || "").toLowerCase();
    const declared = String(entry.outcome || "").toLowerCase();
    round.verdict = verdict;
    round.outcomeEvidence = "verdict";
    round.transition = entry.transition || null;
    if (verdict === "reject" || declared === "reject") {
      round.outcome = "reject"; round.transition = "author";
    } else if (["pass_clean", "pass_edited_continue", "pass_edited_limit"].includes(declared)) {
      round.outcome = declared; round.outcomeEvidence = "explicit_outcome";
    } else if (declared === "pass") {
      round.outcome = "pass_clean"; round.outcomeEvidence = "explicit_outcome";
    } else if (verdict === "pass" || declared === "fixed") {
      round.outcome = "pass_unknown";
      if (declared === "fixed") {
        round.proofChanged = true; round.outcomeEvidence = "explicit_outcome";
      } else if (round.candidateText !== null && typeof entry.report?.solution === "string") {
        round.proofChanged = normalized(round.candidateText) !== normalized(entry.report.solution);
        round.outcomeEvidence = "candidate_comparison";
        if (!round.proofChanged) round.outcome = "pass_clean";
      }
      if (["review_again", "critic", "continue"].includes(round.transition)) round.outcome = "pass_edited_continue";
    } else round.outcome = "pass_unknown";
    if (round.outcome === "pass_clean") round.transition = "accepted";
    if (round.outcome === "pass_edited_limit") round.transition = "accepted_at_limit";
    if (round.outcome === "pass_edited_continue") round.transition = "review_again";
  };
  for (const entry of trace || []) {
    index += 1;
    if (!entry || !isRoot(entry)) continue;
    const criticRequest = entry.kind === "request" && (entry.stage === "critic" || entry.node === "critic");
    const criticResult = entry.kind === "critic_result";
    const approval = entry.kind === "status" && entry.label === "Critic approved";
    const workflowStep = entry.kind === "status" && /^Workflow step:/.test(entry.label || "");
    const handoffEvent = entry.kind === "proof_handoff";
    if (criticRequest || criticResult || approval || handoffEvent || workflowStep) {
      // Replayed transcript entries must not create extra rounds. Include the
      // attempt and payload because request IDs may be shared across retries.
      if (entry.time || entry.id || entry.requestId) {
        const identity = JSON.stringify([entry.kind, entry.time, entry.id, entry.requestId,
          entry.attempt, entry.round, entry.visit, entry.label, entry.text, entry.report]);
        if (seen.has(identity)) continue;
        seen.add(identity);
      }
    }
    if (criticRequest) {
      const lastAttempt = pending?.requests.at(-1)?.attempt || 1;
      const retry = pending && !pending.result && !pending.interrupted
        && Number.isInteger(entry.attempt) && entry.attempt > lastAttempt;
      if (!retry) {
        if (pending && !pending.result) pending.interrupted = true;
        ensureRound(entry);
      }
      pending.requests.push(entry);
      if (pending.candidateText === null) pending.candidateText = candidateFrom(entry);
      const requestId = entry.requestId || entry.id;
      if (requestId) identities.set(String(requestId), {cycle, round: pending});
    } else if (criticResult) {
      let round = pending;
      if (entry.requestId) {
        const pair = identities.get(String(entry.requestId));
        round = pair && pair.cycle === cycle && !pair.round.result ? pair.round : null;
      }
      if (!round || round.result || round.interrupted && !entry.requestId) round = ensureRound(entry);
      round.result = entry;
      round.interrupted = false;
      round.endTime = entry.time || null;
      round.roundNumber = positive(entry.round) || round.roundNumber;
      classify(round, entry);
      if (round === pending) pending = null;
      // Out-of-order identified decisions belong to their original round.
      if (round === cycle.rounds.at(-1) && ["reject", "pass_clean", "pass_edited_limit"].includes(round.outcome)) {
        cycle.closed = true;
      }
    } else if (approval) {
      const round = cycle?.rounds.findLast(item => item.result);
      if (!round || String(round.result.report?.verdict || "").toLowerCase() !== "pass") continue;
      const clean = entry.acceptanceReason === "clean_pass" || /^Round \d+ passed without edits\.$/.test(entry.text || "");
      const limit = entry.acceptanceReason === "round_limit"
        || /^Reached MAX \(\d+ rounds\); using the latest repaired proof\.$/.test(entry.text || "");
      if (!clean && !limit) continue;
      round.outcome = clean ? "pass_clean" : "pass_edited_limit";
      round.transition = clean ? "accepted" : "accepted_at_limit";
      round.outcomeEvidence = "approval";
      round.approval = entry;
      const match = (entry.text || "").match(/^Reached MAX \((\d+) rounds\)/);
      round.roundLimit = positive(entry.roundLimit) || (match ? Number(match[1]) : null);
      cycle.closed = true;
    } else if (workflowStep && (entry.node === "critic" || entry.label === "Workflow step: critic")) {
      closePending();
      continuePrevious(cycle);
      visit = positive(entry.visit);
      handoff = null;
      // A reset visit number on resume does not mean a new author submission.
    } else if (handoffEvent) {
      closePending();
      continuePrevious(cycle);
      handoff = entry;
      visit = positive(entry.round) || positive(entry.manifest?.visit) || visit;
    } else if (["author_result", "final_result", "failure_result"].includes(entry.kind)
        || entry.kind === "request" && ["solve", "repair", "final"].includes(entry.stage)
        || workflowStep && entry.node !== "critic"
        || entry.kind === "workflow_paused" && entry.node === "author") {
      const latest = cycle?.rounds.at(-1);
      if (latest?.outcome === "pass_unknown" && latest.proofChanged === true
          && (workflowStep && (entry.node === "latex_editor" || entry.label === "Workflow step: latex_editor")
              || entry.kind === "request" && entry.stage === "final" || entry.kind === "final_result")) {
        // In this workflow an edited PASS reaches the writer only on the
        // repeat limit. This observes the actual transition, not today's settings.
        latest.outcome = "pass_edited_limit";
        latest.transition = "accepted_at_limit";
        latest.outcomeEvidence = "final_transition";
      }
      boundary();
    }
  }
  const active = options.phase === undefined || ["running", "paused", "pausing", "stopping"].includes(options.phase)
    && options.activeNode === "critic";
  for (const current of cycles) {
    for (const round of current.rounds) {
      if (!round.result) round.outcome = !round.interrupted && current === cycle && !current.closed && active
        ? "pending" : "interrupted";
    }
    const last = current.rounds.at(-1);
    current.outcome = last?.outcome || "pending";
    current.endTime = last?.endTime || null;
    current.accepted = ["pass_clean", "pass_edited_limit"].includes(current.outcome);
  }
  return cycles;
}

function criticRoundOutcomeLabel(round) {
  round = round || {};
  if (round.outcome === "reject") return "Rejected — returned to author";
  if (round.outcome === "pass_clean") return "Clean pass — proof accepted";
  if (round.outcome === "pass_edited_continue") return "Pass with fixes — another review";
  if (round.outcome === "pass_edited_limit") return "Pass with changes — accepted at review limit";
  if (round.outcome === "pending") return "Awaiting decision";
  if (round.outcome === "interrupted") return "No recorded decision";
  if (round.proofChanged) return "Pass with changes — next step not recorded";
  return round.verdict === "pass" ? "Pass — edits and next step not recorded" : "Verdict unavailable";
}

function criticCycleOutcomeLabel(cycle) {
  return criticRoundOutcomeLabel(cycle?.rounds?.at(-1) || {outcome: cycle?.outcome});
}

function criticReviewModel() {
  const trace = state.trace || [];
  const signature = JSON.stringify([state.batchId, state.runId, state.traceVersion,
    trace.length, state.phase, state.activeNode, state.criticAcceptances || []]);
  const cached = criticReviewModel.cache;
  if (cached?.trace === trace && cached.signature === signature) return cached.value;
  // Older controllers trim routine statuses. The dashboard can recover exact
  // approval records separately, without disturbing incremental trace offsets.
  const approvals = state.criticAcceptances || [];
  const identity = entry => JSON.stringify([entry.kind, entry.time, entry.label, entry.text]);
  const recorded = new Set(trace.map(identity));
  const recovered = approvals.filter(entry => !recorded.has(identity(entry)));
  const events = recovered.length ? [...trace, ...recovered].sort((a, b) =>
    (Date.parse(a.time) || 0) - (Date.parse(b.time) || 0)) : trace;
  const cycles = collectCriticReviewCycles(events, {phase: state.phase, activeNode: state.activeNode});
  const rounds = cycles.flatMap(cycle => cycle.rounds);
  const byResult = new Map();
  for (const [cycleIndex, cycle] of cycles.entries()) {
    for (const [roundIndex, round] of cycle.rounds.entries()) {
      if (round.result) byResult.set(round.result, {cycle, round, cycleNumber: cycleIndex + 1, roundNumber: roundIndex + 1});
    }
  }
  const value = {cycles, rounds, byResult};
  criticReviewModel.cache = {trace, signature, value};
  return value;
}

function renderCriticReviewHistory() {
  const container = ui.criticReviewHistory;
  if (!container) return;
  const {cycles, rounds: reviews} = criticReviewModel();
  const runIdentity = `${state.batchId || ""}:${state.runId || ""}`;
  const signature = JSON.stringify([runIdentity, state.phase, state.activeNode, cycles]);
  if (container.dataset.signature === signature) return;
  const sameRun = container.dataset.runId === runIdentity;
  const expanded = new Set(sameRun ? [...container.querySelectorAll("details")]
    .filter(detail => detail.open).map(detail => detail.dataset.reviewKey) : []);
  if (!sameRun) container.open = false;
  container.dataset.runId = runIdentity;
  container.dataset.signature = signature;
  const heading = document.createElement("summary");
  heading.textContent = `Critic review inputs and decisions · ${cycles.length} cycle${cycles.length === 1 ? "" : "s"} · ${reviews.length} round${reviews.length === 1 ? "" : "s"}`;
  const explanation = document.createElement("p");
  explanation.textContent = "Each author submission starts a review cycle. Changes can lead to another round within that cycle; rejection returns to the author. Expand a round for its exact inputs and decision.";
  const children = [heading, explanation];
  const details = (key, label, content, className = "critic-review-text") => {
    const detail = document.createElement("details");
    detail.className = className;
    detail.dataset.reviewKey = key;
    detail.open = expanded.has(key);
    const summary = document.createElement("summary");
    summary.textContent = label;
    detail.append(summary);
    if (content !== undefined) {
      const copy = document.createElement("pre");
      copy.textContent = content;
      detail.append(copy);
    }
    return detail;
  };
  for (const [cycleIndex, cycle] of cycles.entries()) {
    const cycleRow = details(cycle.key,
      `Review cycle ${cycleIndex + 1} · ${criticCycleOutcomeLabel(cycle)} · ${cycle.rounds.length} round${cycle.rounds.length === 1 ? "" : "s"}`,
      undefined, "critic-cycle");
    cycleRow.dataset.outcome = cycle.outcome;
    for (const [roundIndex, review] of cycle.rounds.entries()) {
    const result = review.result;
    const verdict = String(result?.report?.verdict || "").toLowerCase();
    const first = review.requests[0];
    const stamp = first?.time || result?.time;
    const row = details(review.key,
      `Round ${roundIndex + 1} · ${criticRoundOutcomeLabel(review)}${stamp ? ` · ${localDateTime(stamp)}` : ""}`,
      undefined, "critic-review");
    row.dataset.outcome = review.outcome;
    if (review.outcome === "pass_edited_limit") {
      const note = document.createElement("p");
      note.textContent = "The critic changed the proof. The round limit accepts that revision without another critic round; this is not a clean pass.";
      row.append(note);
    }
    if (!review.requests.length) {
      const missing = document.createElement("p");
      missing.textContent = "The exact critic input was not recorded for this review.";
      row.append(missing);
    }
    for (const [attemptIndex, request] of review.requests.entries()) {
      const label = `Exact critic input${review.requests.length > 1 ? ` · attempt ${request.attempt || attemptIndex + 1}` : ""}`;
      const input = details(`${review.key}:input:${attemptIndex}`, label,
        typeof request.text === "string" && request.text ? request.text : "The exact input text is unavailable.");
      const metadata = document.createElement("p");
      metadata.className = "critic-review-meta";
      metadata.textContent = [request.time ? `Sent ${localDateTime(request.time)}` : "Time unavailable",
        request.model, request.reasoningEffort, request.id || request.requestId]
        .filter(Boolean).join(" · ");
      input.append(metadata);
      row.append(input);
    }
    if (result) {
      if (verdict === "reject" || result.report?.bugs) {
        row.append(details(`${review.key}:bugs`, "Rejection / unresolved bugs",
          typeof result.report?.bugs === "string" && result.report.bugs
            ? result.report.bugs : "No separate rejection text was recorded; see the full decision."));
      }
      const raw = typeof result.text === "string" && result.text
        ? result.text : result.report ? JSON.stringify(result.report, null, 2) : "No decision text was recorded.";
      const decision = details(`${review.key}:decision`, "Full critic decision", raw);
      if (result.time) {
        const metadata = document.createElement("p");
        metadata.className = "critic-review-meta";
        metadata.textContent = `Returned ${localDateTime(result.time)}`;
        decision.append(metadata);
      }
      row.append(decision);
    }
    cycleRow.append(row);
    }
    children.push(cycleRow);
  }
  if (!reviews.length) {
    const empty = document.createElement("p");
    empty.textContent = "No critic input or decision has been recorded yet.";
    children.push(empty);
  }
  container.replaceChildren(...children);
}

function renderRetrievalOverview() {
  const { mode, cutoff } = retrievalConfiguration(state);
  const retrieval = state.retrieval || (state.retrievalPolicyPath
    ? { mode: "historical", fixed: true, available: false }
    : { mode });
  const historical = mode === "historical", live = mode === "live";
  ui.retrievalPanel.dataset.mode = mode;
  ui.retrievalHeading.textContent = historical ? "Historical search"
    : live ? "Unrestricted web search" : "Web-search tools disabled";
  const counts = retrieval.counts || {};
  const errors = Number.isFinite(counts.errors) ? counts.errors : 0;
  const successes = Number.isFinite(counts.successes) ? counts.successes : 0;
  const latest = retrieval.lastCompleted;
  const toolNames = { search: "Search", open: "Open", find: "Find", unknown: "Request" };
  const lastTool = toolNames[latest?.tool] || "Request";
  const latestStatus = latest?.status;
  ui.retrievalStatus.textContent = !historical ? (live ? "Cutoff off" : "Cutoff on · -1")
    : retrieval.available === false ? "Status unavailable"
    : latestStatus === "success" ? `Last ${lastTool.toLowerCase()} succeeded`
    : latestStatus === "error" ? `Last ${lastTool.toLowerCase()} failed`
    : latestStatus === "cancelled" ? "Last request cancelled"
    : errors ? `${errors} failed request${errors === 1 ? "" : "s"} in history`
    : retrieval.fixed ? "Cutoff fixed" : "Cutoff selected";
  ui.retrievalStatus.dataset.tone = historical && (retrieval.available === false
    || latestStatus === "error" || latestStatus === "cancelled" || (!latestStatus && errors)) ? "warning" : "neutral";
  const providers = { codex_wayback: "General web discovery via Codex · Wayback archives",
    scholarly_wayback: "arXiv and Crossref discovery · Wayback archives",
    brave_wayback: "Brave URL discovery · Wayback archives", duckduckgo_wayback: "DuckDuckGo URL discovery · Wayback archives",
    sealed_corpus: "Controller-pinned archive collection" };
  ui.retrievalDescription.textContent = historical
    ? `${providers[retrieval.provider] || "Historical archive retrieval"}. The proof author can search, page through, and find text within verified captures. The critic and writer have no external retrieval tools.`
    : live ? "The model's native web search is enabled without a date restriction. Search activity appears in the run timeline."
    : "All model web-search tools are disabled for this job. This controls search tools; it does not isolate ordinary shell network access.";
  show(ui.retrievalCutoffDisplay, !live);
  ui.retrievalCutoffLabel.textContent = historical ? "Only captures strictly before" : "Cutoff time";
  ui.retrievalCutoffValue.textContent = cutoff || "Not recorded";
  ui.retrievalPolicyNote.textContent = !historical ? "-1 disables web-search tools."
    : retrieval.fixed
    ? "Fixed for this job, including resumes. Times are UTC."
    : "The selected cutoff will be fixed when the job starts. Times are UTC.";
  show(ui.retrievalCounts, historical && retrieval.available !== false && Boolean(retrieval.counts));
  const metrics = [
    ["Search requests", counts.searches], ["Open requests", counts.opens], ["Find requests", counts.finds],
    ["Completed successfully", counts.successes], ["Failed requests (total)", counts.errors],
    ["No terminal event yet", counts.pending], ["Candidates verified", counts.verifiedCandidates],
  ];
  const signature = JSON.stringify(metrics);
  if (ui.retrievalCounts.dataset.signature !== signature) {
    ui.retrievalCounts.dataset.signature = signature;
    ui.retrievalCounts.replaceChildren(...metrics.map(([label, value]) => {
      const row = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
      term.textContent = label;
      description.textContent = Number.isFinite(value) ? value.toLocaleString() : "—";
      row.append(term, description); return row;
    }));
  }
  const hasCounts = historical && retrieval.available !== false && Boolean(retrieval.counts);
  show(ui.retrievalRequestSummary, hasCounts);
  ui.retrievalRequestSummary.textContent = !hasCounts ? ""
    : `${successes} successful and ${errors} failed retrieval request${successes + errors === 1 ? "" : "s"} over this run.`
      + (errors ? " A failed request returned no usable source for that request; it does not by itself mean the proof run failed or the cutoff was bypassed." : "")
      + (latest?.time ? ` Latest completed request: ${lastTool.toLowerCase()} ${latestStatus === "success" ? "succeeded" : latestStatus === "error" ? "failed" : "was cancelled"} at ${localDateTime(latest.time)}.` : "");
  const failures = Array.isArray(retrieval.requestFailures) ? retrieval.requestFailures : [];
  show(ui.retrievalFailures, hasCounts && failures.length > 0);
  const failureSignature = JSON.stringify(failures);
  if (ui.retrievalFailures.dataset.signature !== failureSignature) {
    ui.retrievalFailures.dataset.signature = failureSignature;
    ui.retrievalFailures.replaceChildren(...failures.map(failure => {
      const row = document.createElement("li");
      row.textContent = `${Number.isFinite(failure.count) ? failure.count : "—"} · ${failure.label || "Unclassified retrieval failure"}`;
      return row;
    }));
  }
  show(ui.retrievalDetails, historical);
  ui.retrievalLimitation.textContent = retrieval.limitation
    || "Current discovery influences which URLs are found. Archive coverage and model training knowledge differ from the historical experiment.";
  ui.retrievalCountNote.textContent = (retrieval.countBasis || "Counts come from the retrieval service log. Pending means no terminal event was recorded, rather than confirmed running. Verified candidates can include sources never returned to the author.")
    + " These counts are not a proof or citation audit."
    + (retrieval.lastFailureAt ? ` Last failed request: ${localDateTime(retrieval.lastFailureAt)}.` : "")
    + (retrieval.available === false ? ` ${retrieval.error || "Retrieval log statistics are unavailable."}` : "")
    + (retrieval.lastEventAt ? ` Last recorded retrieval event: ${localDateTime(retrieval.lastEventAt)}.` : "");
}

// Show the author/audit cycle alongside the separate critic/repair cycle.
function renderWorkflow() {
  const nodes = state.workflow?.nodes || {};
  const trace = state.trace || [];
  const {cycles, rounds} = criticReviewModel();
  const completed = rounds.filter(round => round.result);
  const latestRound = completed.at(-1);
  const latestCritic = latestRound?.result;
  const currentCycle = cycles.at(-1);
  const verdict = String(latestCritic?.report?.verdict || "").toLowerCase();
  const rejectedReviews = completed.filter(round => round.outcome === "reject").length;
  const criticHistoryOpen = ui.workflowNodes.querySelector?.(".critic-history")?.open || false;
  const latexOnly = ["latex", "final-resume"].includes(state.problemMode);
  const criticResume = state.problemMode === "critic-resume";
  const startsAtAuthor = state.problemMode === "algorithmic"
    || state.skipStatementReview;
  const seenInThisJob = new Set((state.trace || []).map(
    (entry) => entry.node || nodeFromStage(entry.stage)
  ));
  const seen = new Set(seenInThisJob);
  const auditConfig = state.researchAudits || state.workflow?.settings?.research_audits || {};
  const auditStatus = state.researchAuditStatus || {};
  const selectedAuditors = (auditConfig.models || []).filter(model => model !== "none").length;
  const runningAuditors = Array.isArray(auditStatus.runningSlots)
    ? auditStatus.runningSlots.length : Number(auditStatus.runningSlots || 0);
  const authorStage = state.activeNode === "author" && ["solve", "repair"].includes(state.stage);
  const auditing = authorStage && ["running", "pausing", "paused"].includes(state.phase)
    && Boolean(state.auditHoldingAuthor || auditStatus.batchActive || runningAuditors);
  const compiling = ["latex_compile", "latex_repair"].includes(state.activeNode);
  if (seen.has("latex_repair")) seen.add("latex_compile");
  if (criticResume) {
    seen.add("statement_reviewer");
    seen.add("author");
  }
  const approvedAfterLatestReview = trace.slice(trace.lastIndexOf(latestCritic) + 1)
    .some(entry => entry.kind === "status" && entry.label === "Critic approved");
  const authorRepairing = authorStage && !approvedAfterLatestReview
    && (state.stage === "repair" || verdict === "reject");
  const criticApproved = verdict === "pass" && state.stage !== "repair" && (
    ["pass_clean", "pass_edited_limit"].includes(currentCycle?.outcome)
    || state.finalInputReady || approvedAfterLatestReview
    || ["latex_editor", "latex_compile", "latex_repair"].some(name => seen.has(name))
  );
  const makeNode = (name, number, overrides = {}) => {
    const item = overrides.item || nodes[name];
    const row = document.createElement("li");
    const current = state.activeNode === name || (name === "latex_compile" && compiling);
    const paused = current && ["paused", "pausing"].includes(state.phase);
    const active = ["running", "reviewing", "stopping"].includes(state.phase) && current;
    const failed = state.phase === "done" && current
      && (state.error || name === "failure_summary");
    const settled = name === "critic"
      ? verdict === "reject" ? "rejected" : criticApproved ? "complete"
        : latestCritic || seen.has(name) ? "reviewed" : ""
      : seen.has(name) ? "complete" : "";
    const status = overrides.status ?? (paused ? "paused" : active ? "active" : failed ? "failed"
      : settled);
    row.className = `workflow-node ${status}`;
    row.dataset.node = name;
    if (status === "active") row.setAttribute("aria-current", "step");
    const dot = document.createElement("span");
    dot.className = "node-dot";
    dot.textContent = ["failed", "warning"].includes(status) ? "!" : status === "paused" ? "Ⅱ"
      : status === "rejected" ? "×" : status === "reviewed" ? "·"
      : status === "complete" ? "✓" : status === "disabled" ? "–" : number;
    const copy = document.createElement("div");
    copy.className = "node-copy";
    const title = document.createElement("strong");
    title.textContent = item.label;
    const description = document.createElement("span");
    const descriptions = {
      author: "Explore approaches and build a proof.",
      critic: "Review the candidate; rejected proofs return to the author.",
      latex_compile: "Compile, fix errors, and retry until a PDF is ready.",
    };
    description.textContent = descriptions[name] || item.description;
    copy.append(title, description);
    let noteText = overrides.note || (paused ? (auditing
      ? state.phase === "pausing" ? "Pausing for audits" : "Paused for audits" : "Paused")
      : current && state.phase === "prepared" ? "Ready to start" : "");
    if (name === "author" && authorRepairing && current
        && ["running", "pausing", "paused", "stopping"].includes(state.phase)) {
      noteText = noteText ? `${noteText} · repairing critic objections` : "Repairing critic objections";
    }
    if (name === "critic" && !noteText) {
      noteText = active ? `Reviewing candidate${currentCycle ? ` · cycle ${cycles.length}, round ${currentCycle.rounds.length}` : ""}` : failed ? "Review interrupted"
        : criticApproved ? criticCycleOutcomeLabel(currentCycle || {})
          : verdict === "reject" ? "Candidate needs repair"
            : verdict === "pass" ? criticRoundOutcomeLabel(latestRound)
              : seen.has(name) || latestCritic ? "Verdict unavailable" : "";
    }
    if (noteText) {
      const note = document.createElement("span");
      note.className = "node-status";
      note.textContent = noteText;
      copy.append(note);
    }
    if (name === "critic" && latestCritic) {
      const latest = document.createElement("span");
      latest.className = `node-verdict ${["pass", "reject"].includes(verdict) ? verdict : "unknown"}`;
      latest.dataset.outcome = latestRound.outcome;
      latest.textContent = `Latest completed round: ${criticRoundOutcomeLabel(latestRound)}`;
      const passes = document.createElement("span");
      passes.className = "node-review-count";
      passes.textContent = currentCycle ? `Review cycle ${cycles.length} · ${currentCycle.rounds.length} round${currentCycle.rounds.length === 1 ? "" : "s"}` : "";
      const history = document.createElement("details");
      history.className = "critic-history";
      history.open = criticHistoryOpen;
      const summary = document.createElement("summary");
      summary.textContent = `Recorded: ${cycles.length} cycle${cycles.length === 1 ? "" : "s"} · ${completed.length} completed round${completed.length === 1 ? "" : "s"}`
        + ` · ${rejectedReviews} rejection${rejectedReviews === 1 ? "" : "s"}`;
      const list = document.createElement("ol");
      for (const [index, cycle] of cycles.entries()) {
        const entry = document.createElement("li");
        entry.className = "critic-cycle-summary";
        const title = document.createElement("strong");
        title.textContent = `Cycle ${index + 1} · ${criticCycleOutcomeLabel(cycle)}`;
        const roundList = document.createElement("ol");
        for (const [roundIndex, round] of cycle.rounds.entries()) {
          const item = document.createElement("li");
          item.textContent = `Round ${roundIndex + 1}: ${criticRoundOutcomeLabel(round)}`;
          roundList.append(item);
        }
        entry.append(title, roundList);
        list.append(entry);
      }
      history.append(summary, list);
      copy.append(latest, passes, history);
    }
    if (criticResume && (name === "statement_reviewer"
      || (name === "author" && !seenInThisJob.has("author")))) {
      const loaded = document.createElement("span");
      loaded.className = "node-resume-note";
      loaded.textContent = "Loaded from the source job";
      copy.append(loaded);
    }
    row.append(dot, copy);
    return row;
  };
  if (state.statementReviewOnly) {
    ui.workflowNodes.replaceChildren(makeNode("statement_reviewer", "1"));
    return;
  }
  const arrow = (text, pass = false) => {
    const row = document.createElement("li");
    row.className = `flow-arrow${pass ? " pass" : ""}`;
    row.textContent = `${text} ↓`;
    return row;
  };
  if (latexOnly) {
    ui.workflowNodes.replaceChildren(makeNode("latex_editor", "1"));
    if (nodes.latex_compile) {
      ui.workflowNodes.append(arrow("Compile and check"), makeNode("latex_compile", "2"));
    }
    return;
  }

  const loop = document.createElement("li");
  loop.className = "workflow-loop";
  const loopTitle = document.createElement("strong");
  loopTitle.className = "loop-title";
  loopTitle.textContent = "Research and verification";
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
  selfRoute.textContent = `↻ PASS with changes → next round (limit ${state.criticRounds || 2})`;
  const author = makeNode("author", startsAtAuthor ? "1" : "2");
  let authorGroup = author;
  if (state.fileManagement !== false) {
    authorGroup = document.createElement("li");
    authorGroup.className = "author-audit-group";
    const group = document.createElement("ol");
    group.className = "author-audit-nodes" + (!selectedAuditors && !auditing ? " audits-disabled" : "");
    group.setAttribute("aria-label", "Author and research audits: pause, audit, then resume the same author");
    const warnings = (auditStatus.warnings || []).length;
    const problem = warnings || auditStatus.batchError || auditStatus.lastError;
    const latest = [...(state.trace || [])].reverse().find(entry => entry.kind === "research_audit"
      && ["completed", "cancelled"].includes(entry.status) && (auditConfig.models || []).includes(entry.model));
    const status = auditing ? "active" : !selectedAuditors ? "disabled" : problem ? "warning"
      : latest?.status === "completed" ? "complete" : "";
    const note = auditing ? (state.phase === "pausing" ? "Waiting for author"
      : state.phase === "running" && !state.auditHoldingAuthor ? "Checking auditors" : runningAuditors
        ? `${runningAuditors} auditor${runningAuditors === 1 ? "" : "s"} active` : "Finishing batch")
      : !selectedAuditors ? "Disabled" : problem ? "Needs attention"
        : latest?.status === "completed" ? "Reports saved" : latest?.status === "cancelled" ? "Cancelled"
          : authorStage ? state.phase === "paused" ? "Ready while paused" : "Scheduled" : "Waiting for author";
    const description = auditing ? (state.auditHoldingAuthor
      ? "Review saved work, then resume the author." : "Check availability before pausing the author.")
      : !selectedAuditors ? "Select auditors to review the author's work."
        : `${selectedAuditors} selected · every ${auditConfig.intervalHours ?? 2} author hours or on demand.`;
    const audit = makeNode("research_audit", "↻", {status, note,
      item: {label: "Research audits", description}});
    const connection = document.createElement("li");
    connection.className = "audit-cycle-route";
    connection.textContent = "⇄";
    connection.setAttribute("aria-hidden", "true");
    const caption = document.createElement("li");
    caption.className = "audit-cycle-caption";
    caption.textContent = "Pause author → audit → resume author";
    group.append(author, connection, audit, caption);
    authorGroup.append(group);
  }
  const critic = makeNode("critic", startsAtAuthor ? "2" : "3");
  const passStem = document.createElement("li");
  passStem.className = "critic-pass-stem";
  passStem.setAttribute("aria-hidden", "true");
  loopNodes.append(
    authorGroup, candidateRoute, critic, selfRoute, passStem, rejectRoute,
  );
  loop.append(loopTitle, loopNodes);

  // Accepted proofs continue to editing; interruptions retain saved progress.
  const branch = document.createElement("li");
  branch.className = "workflow-branch";
  const passRoute = arrow("Unchanged PASS or edited PASS at limit", true);
  passRoute.classList.add("critic-pass");
  const editor = makeNode("latex_editor", startsAtAuthor ? "3" : "4");
  editor.classList.add("post-loop");
  branch.append(loop, passRoute, editor);
  if (nodes.latex_compile) {
    const compileRoute = arrow("Compile and check");
    compileRoute.classList.add("compile-route");
    const compiler = makeNode("latex_compile", startsAtAuthor ? "4" : "5");
    compiler.classList.add("compile-node");
    branch.append(compileRoute, compiler);
  }
  failureRoute.className = "flow-arrow interruption-route";
  failureRoute.textContent = "On interruption or time limit ↓";
  branch.append(failureRoute, failureNode);

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
    ? durationText(runElapsedSeconds()) : "";
  renderRunOverview();
  if (state.lastActivityAt) {
    const ago = Math.max(0, Math.floor(
      (Date.now() - new Date(state.lastActivityAt)) / 1000
    ));
    const working = ["reviewing", "running", "stopping", "pausing"].includes(state.phase);
    const age = ago < 2 ? "just now" : `${durationText(ago)} ago`;
    const activity = working && state.processAlive
      ? `Local process running · last public event ${age}`
      : `Last public event ${age}`;
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
  if (state.readOnly || !managesResearchFiles() || state.retrieval?.mode === "historical" || state.retrievalPolicyPath) {
    for (const element of [ui.researchAuditsControl, ui.researchAuditToolbar,
      ui.researchAuditActivity, ui.researchAuditStatus]) show(element, false);
    return;
  }
  const identity = JSON.stringify([state.batchId || "", currentJob || state.runId || ""]);
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
  const auditing = Boolean(state.auditHoldingAuthor || status.batchActive)
    || running > 0;
  const author = state.activeNode === "author" && ["solve", "repair"].includes(state.stage);
  ui.researchAuditCountdownLabel.textContent = auditing ? "Research audits" : "Next audit";
  ui.researchAuditCountdown.textContent = auditing
    ? (state.phase === "pausing" ? "Preparing…" : "In progress") : countdown;
  ui.researchAuditTimerState.textContent = auditing && state.phase !== "running" ? "author paused"
    : state.phase === "running" ? "author time" : "paused";
  show(ui.researchAuditToolbar, Boolean((enabled || auditing) && author
    && ["running", "pausing", "paused"].includes(state.phase)));
  ui.startResearchAudit.disabled = !author || !["running", "paused"].includes(state.phase)
    || state.manuallyStopped || !enabled || auditing || Boolean(status.batchActive)
    || researchAuditDirty || researchAuditSaving || researchAuditStarting;
  ui.startResearchAudit.textContent = researchAuditStarting ? "Starting…" : "Start audit now";
  ui.startResearchAudit.title = researchAuditDirty ? "Apply your audit settings first."
    : state.phase === "paused" ? "Run the selected auditors now, then resume the author automatically."
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
  const live = Boolean(state.auditHoldingAuthor || (["running", "paused"].includes(state.phase) && status.batchActive));
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
  const batch = currentBatch;
  researchAuditFeedback = "";
  researchAuditSaving = true;
  renderResearchAudits(true);
  try {
    const next = await request(jobPath("/set-research-audits"), config);
    if (job !== currentJob || batch !== currentBatch) return;
    researchAuditDirty = JSON.stringify(researchAuditValues(true)) !== JSON.stringify(config);
    researchAuditSaving = false;
    render(next);
  } catch (error) {
    if (job !== currentJob || batch !== currentBatch) return;
    researchAuditSaving = false;
    researchAuditFeedback = error.message;
    renderResearchAudits(!ui.researchAuditsControl.hidden);
  }
}

async function startResearchAudit() {
  if (ui.startResearchAudit.disabled) return;
  const job = currentJob;
  const batch = currentBatch;
  researchAuditStarting = true;
  researchAuditFeedback = "";
  renderResearchAudits(true);
  try {
    const next = await request(jobPath("/start-research-audit"), {});
    if (job !== currentJob || batch !== currentBatch) return;
    researchAuditStarting = false;
    render(next);
  } catch (error) {
    if (job !== currentJob || batch !== currentBatch) return;
    researchAuditStarting = false;
    researchAuditFeedback = error.message === "Not found."
      ? "Pause the author and restart the web UI to enable Start audit now."
      : error.message;
    renderResearchAudits(!ui.researchAuditsControl.hidden);
  }
}

// Merge incremental polling state, then update the visible phase.
function render(next) {
  const incremental = next.traceFrom === state.traceVersion;
  const acceptancesChanged = JSON.stringify(next.criticAcceptances || [])
    !== JSON.stringify(state.criticAcceptances || []);
  const newEntries = next.trace || [];
  const combined = retainTrace(
    incremental ? [...(state.trace || []), ...newEntries] : newEntries
  );
  state = { ...next, trace: combined };
  stateReceivedAt = Date.now();
  ingest(newEntries, !incremental);
  // Acceptance is logged after the PASS verdict. Refresh existing critic cards
  // when later events clarify its route, keeping the timeline in place.
  if (incremental && (acceptancesChanged || newEntries.some(entry => entry.kind === "critic_result"
      || ["proof_handoff", "final_result"].includes(entry.kind)
      || entry.kind === "status" && entry.label === "Critic approved"
      || entry.kind === "status" && /^Workflow step:/.test(entry.label || "")
      || entry.kind === "request" && ["critic", "final"].includes(entry.stage)))) {
    for (const round of criticReviewModel().rounds) {
      if (round.result) upsertTimeline(describe(round.result));
    }
  }

  const phase = state.phase;
  const auditHolding = Boolean(state.auditHoldingAuthor || state.researchAuditStatus?.batchActive)
    && ["pausing", "paused"].includes(phase);
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
  show(ui.workflowRail, phase !== "input");
  show(ui.activityToggle, Boolean(currentJob));
  syncMemoryPanel();
  ui.notice.textContent = state.error || "";
  show(ui.notice, Boolean(state.error));
  ui.traceNotice.textContent = state.traceNotice || "Only the most recent saved activity is shown here.";
  show(ui.traceNotice, Boolean(state.traceTruncated || state.traceNotice));

  if (phase === "input" && previousPhase !== "input") {
    const rounds = state.workflow?.settings?.critic_rounds || {};
    const hours = state.workflow?.settings?.thinking_hours || {};
    ui.problem.value = state.draft || "";
    ui.latexInput.value = state.latexInput || "";
    setChoice(ui.reviewModel, state.reviewModel || "gpt-6.1-sol");
    setChoice(ui.authorModel, state.authorModel || "gpt-6.1-sol");
    setChoice(ui.criticModel, state.criticModel || "gpt-6.1-sol");
    setChoice(ui.writerModel, state.writerModel || "gpt-6.1-sol");
    ui.reviewEffort.value = state.reviewEffort || "max";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "max";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "max";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "max";
    ["review", "author", "critic", "writer"].forEach(syncEffortChoices);
    ui.speedMode.value = state.speedMode || "standard";
    ui.reasoningSummary.value = state.reasoningSummary || "concise";
    restoreRetrievalSettings(state);
    ui.skipStatementReview.checked = Boolean(state.skipStatementReview);
    ui.statementReviewOnly.checked = Boolean(state.statementReviewOnly);
    ui.fileManagement.checked = Boolean(state.fileManagement);
    syncPrompts(state);
    updateModelSummary();
    ui.criticRounds.value = state.criticRounds || 2;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.thinkingHours.value = state.thinkingHours ?? 168;
    ui.thinkingHours.min = hours.minimum ?? 0;
    ui.thinkingHours.max = hours.maximum || 168;
    fillResearchAuditControls();
    setProblemMode(state.problemMode || "statement");
  }
  if (reviewReady && (previousPhase !== phase || reviewPending)) {
    ui.fileManagement.checked = state.fileManagement !== false;
    setChoice(ui.reviewModel, state.reviewModel || "gpt-6.1-sol");
    setChoice(ui.authorModel, state.authorModel || "gpt-6.1-sol");
    setChoice(ui.criticModel, state.criticModel || "gpt-6.1-sol");
    setChoice(ui.writerModel, state.writerModel || "gpt-6.1-sol");
    ui.reviewEffort.value = state.reviewEffort || "max";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "max";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "max";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "max";
    ["review", "author", "critic", "writer"].forEach(syncEffortChoices);
    ui.speedMode.value = state.speedMode || "standard";
    ui.reasoningSummary.value = state.reasoningSummary || "concise";
    restoreRetrievalSettings(state);
    syncPrompts(state);
    updateModelSummary();
    ui.proposed.value = state.review.statement;
    ui.notes.replaceChildren();
    appendFormattedText(ui.notes, state.review.notes);
    ui.feedback.value = "";
    ui.criticRounds.value = state.criticRounds || 2;
    ui.thinkingHours.value = state.thinkingHours ?? 168;
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
        : done ? completedRunLabel(state) : (node.label || "Codex is working");
  ui.runLabel.textContent = auditHolding ? "RESEARCH AUDITS"
    : phase === "prepared" ? "PREPARED RUN" : phase === "paused" ? "RUN PAUSED"
    : done ? (state.resultKind === "final" || state.canDownloadTex ? "ANSWER READY" : "RUN INCOMPLETE") : (node.short_label || "CODEX");
  ui.runTitle.textContent = phase === "reviewing" ? "Checking the statement…"
    : auditHolding ? (phase === "pausing" ? "Pausing author for research audits…" : "Author paused for research audits")
    : phase === "prepared" ? "Prepared research workspace" : phase === "paused" ? "Ready to resume" : phase === "pausing" ? "Pausing Codex…"
    : phase === "stopping" ? "Stopping safely…"
      : (state.error || (done ? (node.label || "Final result")
        : (node.label || "Codex is working")));
  ui.runDescription.textContent = state.error
    || (auditHolding ? "The author will resume automatically in the same conversation after all audit reports are saved. Click Pause to cancel the audits and keep the author paused."
      : phase === "prepared" ? (state.launchHold || "Review the prepared statement and settings, then start a fresh author session in this workspace.")
      : phase === "paused" ? "Resume reopens the saved Codex conversation in this same run folder. You can change your Codex CLI login before resuming."
      : phase === "pausing" ? "Interrupting the current turn and preserving the saved conversation. Wait for Paused before closing the UI."
      : done ? (state.resultKind === "final" || state.canDownloadTex
        ? "The final answer and transcript are saved for this job."
        : "The run ended without a completed answer. Saved work and available checkpoints are preserved.")
        : (node.description || ""));
  if (phase === "paused" && state.autoResumeAt) {
    const retryTime = new Date(state.autoResumeAt * 1000).toLocaleTimeString();
    ui.runDescription.textContent += ` Automatic retry at ${retryTime}. Choose Keep paused to cancel retries.`;
  }
  show(ui.roundBadge, Boolean(state.round && ["critic", "author"].includes(state.activeNode)));
  ui.roundBadge.textContent = `Round ${state.round} / ${state.criticRounds}`;
  const authorLimit = Number(state.thinkingHours ?? 168);
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
  ui.authorLimitSummary.textContent = authorLimit === 0 ? "No workflow time limit" : `Total limit: ${authorLimitText} hours`;
  ui.authorLimitHours.max = String(maximumAuthorLimit);
  if (canSetAuthorLimit && document.activeElement !== ui.authorLimitHours) {
    ui.authorLimitHours.value = String(authorLimit);
  }
  ui.setAuthorTimeLimit.disabled = !canSetAuthorLimit;
  const retryPending = phase === "paused" && Boolean(state.autoResumeAt);
  show(ui.pause, authorRunning || auditHolding || retryPending);
  ui.pause.textContent = retryPending ? "Keep paused" : "Pause";
  ui.pause.title = retryPending ? "Cancel automatic retries"
    : auditHolding ? "Cancel the audits and keep the author paused" : "Pause the author";
  show(ui.resume, phase === "paused" || phase === "prepared");
  ui.resume.textContent = state.launchHold ? "Awaiting review" : phase === "prepared" ? "Start prepared run" : "Resume";
  ui.resume.disabled = auditHolding || Boolean(state.launchHold);
  show(ui.downloadTex, Boolean(state.canDownloadTex));
  show(ui.downloadPdf, Boolean(state.canDownloadPdf));
  show(ui.stop, working);
  ui.stop.disabled = phase === "stopping";
  ui.run.setAttribute("aria-busy", String(working));

  renderWorkflow();
  renderClock();
  renderBatchNavigation();
  if (state.readOnly) {
    for (const control of [ui.stop, ui.pause, ui.resume, ui.authorSteerControl, ui.authorTimeLimitControl,
      ui.researchAuditsControl, ui.researchAuditToolbar, ui.researchAuditStatus, ui.approve, ui.recheck,
      ui.reviewFeedbackControls]) show(control, false);
    ui.proposed.readOnly = true;
  }
  if (phase !== previousPhase) {
    const heading = reviewReady ? ui.reviewHeading
      : (phase !== "input" ? ui.runTitle : null);
    heading?.focus({ preventScroll: true });
  }
  previousPhase = phase;

  clearTimeout(timer);
  const waiting = ["paused", "prepared", "reviewed"].includes(phase);
  if (working || waiting) timer = setTimeout(refresh, working ? 700 : 2500);
  clearInterval(clock);
  if (working || waiting) clock = setInterval(renderClock, 1000);
}

// A saved job may name a model the menus no longer offer (an older GPT-5.6,
// say). Keep that choice visible instead of leaving the menu blank.
function setChoice(select, value) {
  const known = Array.from(select.options || [], (option) => option.value);
  if (value && known.length && !known.includes(value)) {
    select.add(new Option(`${value} (older)`, value));
  }
  select.value = value;
}

// Offer only the reasoning levels the selected model supports in the Codex
// catalog, and move a choice that became invalid to Max.
function syncEffortChoices(role) {
  const supported = state.workflow?.settings?.model_efforts?.[ui[`${role}Model`].value];
  const select = ui[`${role}Effort`];
  if (!supported || !select.options) return;
  Array.from(select.options).forEach((option) => {
    option.disabled = !supported.includes(option.value);
  });
  if (!supported.includes(select.value)) {
    select.value = supported.includes("max") ? "max" : supported.at(-1);
  }
}

async function refresh() {
  const job = currentJob;
  const batch = currentBatch;
  if (!job && !composingProblem) {
    renderBatchNavigation();
    await loadJobs();
    return;
  }
  try {
    if (job && (!batchCatalog.length || (batch && !selectedBatch()))) {
      const catalog = await request("/batches");
      if (job !== currentJob || batch !== currentBatch) return;
      batchCatalog = catalog.batches || [];
    }
    const next = await request(jobPath("/state", {
      after: state.traceVersion || 0,
    }));
    if (job !== currentJob || batch !== currentBatch) return;
    if (job && !currentBatch && next.batchId) {
      currentBatch = next.batchId;
      history.replaceState(null, "", jobUrl(currentJob));
    }
    if (selectedBatch()?.readOnly) next.readOnly = true;
    render(next);
  } catch (error) {
    if (job !== currentJob || batch !== currentBatch) return;
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
  if (!validateRetrievalSettings()) return;
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  const reviewOnly = !feedback && ui.statementReviewOnly.checked;
  const skipReview = !reviewOnly && !feedback && ui.skipStatementReview.checked;
  if (!(skipReview ? promptValues[selectedAuthorPrompt()] : promptValues.review)) syncPrompts();
  reviewPending = !skipReview;
  const job = currentJob;
  const batch = currentBatch;
  try {
    const next = await request(jobPath(skipReview ? "/direct" : "/review"), {
      statement, feedback, reviewModel: ui.reviewModel.value,
      reviewEffort: ui.reviewEffort.value,
      ...proofJobSettings(),
      statementReviewOnly: reviewOnly,
    });
    if (job !== currentJob || batch !== currentBatch) return;
    adoptJob(next);
    render(next);
  } catch (error) {
    reviewPending = false;
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

function proofJobSettings() {
  return {
    authorModel: ui.authorModel.value,
    criticModel: ui.criticModel.value,
    writerModel: ui.writerModel.value,
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
    ...selectedRetrievalSettings(),
  };
}

// Read files directly inside the chosen folder, regardless of extension,
// never its subfolders, in a deterministic order by name.
function folderStatementFiles(files) {
  const byName = (first, second) => {
    const a = first.name.toLowerCase(), b = second.name.toLowerCase();
    if (a !== b) return a < b ? -1 : 1;
    return first.name < second.name ? -1 : first.name > second.name ? 1 : 0;
  };
  return [...(files || [])].filter((file) => {
    const path = file.webkitRelativePath || "";
    return path.split("/").length === 2;
  }).sort(byName);
}

// The server caps one folder request; check before sending megabytes for nothing.
const maxFolderRequestBytes = 8 * 1024 * 1024;

// Read each text file; empty files are reported instead of sent.
async function folderStatements(files) {
  const statements = [], empty = [];
  for (const file of folderStatementFiles(files)) {
    if (file.size > maxFolderRequestBytes) throw new Error(`${file.name} is larger than 8 MB.`);
    const statement = await file.text();
    if (statement.trim()) statements.push({ name: file.name, statement });
    else empty.push(file.name);
  }
  return { statements, empty };
}

// One folder batch at a time: a repeated change event must not start its jobs twice.
let folderBatchBusy = false;

async function startFolderBatch(files) {
  const chosen = [...(files || [])];
  if (!chosen.length || folderBatchBusy) return;
  if (!validateRetrievalSettings({ folder: true })) return;
  folderBatchBusy = true;
  ui.runFolder.disabled = true;
  try {
    await sendFolderBatch(chosen);
  } finally {
    folderBatchBusy = false;
    ui.runFolder.disabled = false;
  }
}

async function sendFolderBatch(chosen) {
  const folderName = (chosen[0].webkitRelativePath || "").split("/")[0];
  const folder = folderName || "the selected folder";
  show(ui.notice, false);
  show(ui.batchStatus, false);
  let found;
  try {
    found = await folderStatements(chosen);
  } catch (error) {
    ui.notice.textContent = `Could not read the files in ${folder}: ${error.message}`;
    show(ui.notice, true);
    return;
  }
  const { statements, empty } = found;
  const skipped = empty.length
    ? `Skipped ${empty.length} empty file${empty.length === 1 ? "" : "s"}: ${empty.join(", ")}.` : "";
  if (!statements.length) {
    ui.notice.textContent = `No statements were found directly inside ${folder}.`
      + (skipped ? ` ${skipped}` : "") + " Choose a folder that contains non-empty text files (any extension or none).";
    show(ui.notice, true);
    return;
  }
  // File reading is asynchronous; recheck the current settings before sending.
  if (!validateRetrievalSettings({ folder: true })) return;
  const count = statements.length;
  const question = `Start ${count} parallel job${count === 1 ? "" : "s"}? `
    + "Each runs the selected workflow and uses its own quota.\n\n"
    + `Folder: ${folder}\n`
    + "Each job sends its statement directly to the proof author "
    + "(statement review is skipped)." + (skipped ? `\n\n${skipped}` : "");
  if (!confirm(question)) return;
  const body = { statements, ...proofJobSettings() };
  const size = new TextEncoder().encode(JSON.stringify(body)).length;
  if (size > maxFolderRequestBytes) {
    ui.notice.textContent = `These statements are too large to send together `
      + `(${(size / 1048576).toFixed(1)} MB; the limit is 8 MB). Split the folder.`;
    show(ui.notice, true);
    return;
  }
  clearTimeout(jobsTimer);
  try {
    const result = await request("/direct-batch", body);
    const started = (result.startedJobs || []).length;
    ui.batchStatus.textContent = `Started ${started} job${started === 1 ? "" : "s"} from ${folder}.`
      + (skipped ? `\n${skipped}` : "");
    show(ui.batchStatus, true);
    if (!currentJob) {
      composingProblem = false;
      currentBatch = result.batchId || result.batch?.id || "__root__";
      history.pushState(null, "", batchUrl(currentBatch));
      renderBatchNavigation();
      renderJobs(result.jobs || []);
      ui.jobsPanel.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
  if (!currentJob) loadJobs();
}

async function startLatexOnly() {
  if (!validateRetrievalSettings()) return;
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
      ...selectedRetrievalSettings(),
    });
    adoptJob(next);
    render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

async function act(path, body = {}) {
  clearTimeout(timer);
  const job = currentJob;
  const batch = currentBatch;
  try {
    const next = await request(jobPath(path), body);
    if (job === currentJob && batch === currentBatch) render(next);
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
  selectBatch("");
};
ui.runFolder.onclick = () => {
  ui.folderInput.value = "";
  ui.folderInput.click();
};
ui.folderInput.onchange = () => startFolderBatch(ui.folderInput.files);

ui.check.onclick = () => selectedProblemMode() === "latex"
  ? startLatexOnly() : startReview(ui.problem.value);
ui.recheck.onclick = () => startReview(ui.proposed.value, ui.feedback.value);
ui.proposed.oninput = checkEdited;
ui.feedback.oninput = checkEdited;
for (const role of ["review", "author", "critic", "writer"]) {
  ui[`${role}Model`].onchange = () => {
    syncEffortChoices(role);
    updateModelSummary();
  };
  ui[`${role}Effort`].onchange = updateModelSummary;
}
ui.speedMode.onchange = updateModelSummary;
ui.reasoningSummary.onchange = updateModelSummary;
ui.retrievalMode.onchange = () => {
  renderRetrievalSettings();
  updateModelSummary();
};
ui.retrievalCutoffUtc.oninput = () => {
  ui.retrievalCutoffUtc.setCustomValidity("");
  renderRetrievalSettings();
};
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
  if (!(Number.isFinite(hours) && hours >= 0 && hours <= maximum)) {
    ui.notice.textContent = `Set 0 for unlimited, or at most ${maximum} hours.`;
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
ui.memoryApproach.onchange = () => selectMemoryDocument(ui.memoryApproach.value);
ui.approachIndex.onclick = () => {
  if (approachGraphData.indexFile) selectMemoryDocument(approachGraphData.indexFile);
};
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
async function downloadArtifact(button, extension) {
  button.disabled = true;
  try {
    const response = await fetch(jobPath(`/download-${extension}`), {
      headers: { "X-TCS-Prover-Token": sessionToken },
    });
    if (!response.ok) throw new Error((await response.json()).error || "Download failed.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = `final.${extension}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  } finally {
    button.disabled = false;
  }
}
ui.downloadTex.onclick = () => downloadArtifact(ui.downloadTex, "tex");
ui.downloadPdf.onclick = () => downloadArtifact(ui.downloadPdf, "pdf");
ui.home.onclick = goHome;
ui.reviewHome.onclick = goHome;
ui.allBatches.onclick = () => selectBatch("");
ui.batchProblems.onclick = goHome;
ui.newProblem.onclick = newProblem;
window.onpopstate = () => {
  currentJob = new URLSearchParams(location.search).get("job") || "";
  currentBatch = new URLSearchParams(location.search).get("batch") || "";
  composingProblem = new URLSearchParams(location.search).get("new") === "1" && !currentJob;
  clearTimeout(timer); clearInterval(clock); clearTimeout(jobsTimer);
  clearJobView();
  refresh();
};

refresh();
