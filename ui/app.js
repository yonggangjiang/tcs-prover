"use strict";

// Appearance, before the first paint: <html data-theme> names the palette (Blue, Brown, Crimson
// or Ink) and data-mode its light or dark set. Both choices live in this browser only; storage
// may be blocked, and then the defaults hold (Blue, following the system's light or dark).
// ?theme=crimson, ?theme=dark or ?theme=ink-light previews a choice without saving it. The
// header's theme menu (near the end of this file) changes them through appearance.choose().
const appearance = (() => {
  const root = document.documentElement;
  const themes = ["blue", "brown", "crimson", "ink"];
  const modes = ["light", "dark", "system"];
  // Each theme's page canvas, for the browser chrome around the page.
  const canvas = {
    blue: { light: "#f3f5f8", dark: "#0c0e11" },
    brown: { light: "#f6f6f3", dark: "#131312" },
    crimson: { light: "#fcf8f7", dark: "#170d13" },
    ink: { light: "#fafaf9", dark: "#151515" },
  };
  const keys = { theme: "tcs-prover-theme", mode: "tcs-prover-mode" };
  const systemDark = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const stored = (key) => {
    try { return localStorage.getItem(key) || ""; } catch (_) { return ""; }
  };
  let theme = stored(keys.theme);
  let mode = stored(keys.mode);
  // Earlier versions kept light, dark or system under the theme key: that was the mode.
  if (modes.includes(theme)) {
    if (!modes.includes(mode)) mode = theme;
    theme = "";
  }
  const saved = { theme: themes.includes(theme) ? theme : "blue", mode: modes.includes(mode) ? mode : "system" };
  const preview = {};
  const asked = (new URLSearchParams(location.search).get("theme") || "").trim().toLowerCase();
  const parts = /^(blue|brown|crimson|ink)?(?:[-_.,:+ ]?(light|dark|system))?$/.exec(asked);
  if (asked && parts) {
    if (parts[1]) preview.theme = parts[1];
    if (parts[2]) preview.mode = parts[2];
  }
  const listeners = [];
  const current = () => ({ theme: preview.theme || saved.theme, mode: preview.mode || saved.mode });
  const resolve = (choice) => (choice === "system" ? (systemDark && systemDark.matches ? "dark" : "light") : choice);
  function paint() {
    const choice = current();
    const shown = resolve(choice.mode);
    // A switch is instant: colour transitions stay off until the new theme has been drawn.
    if (root.dataset.theme && typeof requestAnimationFrame === "function") {
      root.classList.add("appearance-switching");
      requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("appearance-switching")));
    }
    root.dataset.theme = choice.theme;
    root.dataset.mode = shown;
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.setAttribute("content", canvas[choice.theme][shown]);
    }
    for (const listener of listeners) listener();
  }
  const followSystem = () => { if (current().mode === "system") paint(); };
  if (systemDark && systemDark.addEventListener) systemDark.addEventListener("change", followSystem);
  else if (systemDark && systemDark.addListener) systemDark.addListener(followSystem);
  paint();
  return {
    themes,
    modes,
    current,
    resolve,
    // A choice made in the menu replaces the preview of that part and is remembered.
    choose(kind, value) {
      if (!({ theme: themes, mode: modes }[kind] || []).includes(value)) return;
      saved[kind] = value;
      delete preview[kind];
      try {
        localStorage.setItem(keys.theme, saved.theme);
        localStorage.setItem(keys.mode, saved.mode);
      } catch (_) {}
      paint();
    },
    onChange(listener) { listeners.push(listener); },
  };
})();

// Keep the secret launch token in this tab, not in the visible URL. Only a
// token-shaped fragment is one (launch tokens are 43 URL-safe characters):
// an in-page target such as #main must never replace the stored token.
function launchToken(fragment) {
  return /^[A-Za-z0-9_-]{32,}$/.test(fragment) && !document.getElementById(fragment) ? fragment : "";
}
const launchFragment = launchToken(location.hash.slice(1));
const sessionToken = launchFragment || sessionStorage.getItem("tcs-prover-token") || "";
if (launchFragment) {
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
  reviewSource: $("reviewSource"), authorSource: $("authorSource"),
  criticSource: $("criticSource"), writerSource: $("writerSource"),
  reviewModelSetting: $("reviewModelSetting"),
  authorModelSetting: $("authorModelSetting"),
  criticModelSetting: $("criticModelSetting"),
  writerModelSetting: $("writerModelSetting"),
  reviewEffort: $("reviewEffort"), authorEffort: $("authorEffort"),
  criticEffort: $("criticEffort"), writerEffort: $("writerEffort"),
  reviewModelProvider: $("reviewModelProvider"), authorModelProvider: $("authorModelProvider"),
  criticModelProvider: $("criticModelProvider"), writerModelProvider: $("writerModelProvider"),
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
  speedModeSetting: $("speedModeSetting"),
  criticRoundSetting: $("criticRoundSetting"),
  thinkingHoursSetting: $("thinkingHoursSetting"),
  editPrompts: $("editPromptsButton"), promptDialog: $("promptDialog"),
  promptTabs: $("promptTabs"), promptEditor: $("promptEditor"),
  promptEditorLabel: $("promptEditorLabel"),
  promptEditorHelp: $("promptEditorHelp"), promptError: $("promptEditorError"),
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
  runLabel: $("runLabel"), runTitle: $("runTitle"),
  runDescription: $("runDescription"), roundBadge: $("roundBadge"),
  runOverview: $("runOverview"), runProblemTitle: $("runProblemTitle"),
  runStage: $("runStage"), runActiveTime: $("runActiveTime"), runBudget: $("runBudget"),
  runHealth: $("runHealth"), runNextStep: $("runNextStep"), runLatestAction: $("runLatestAction"),
  runSettingsWarning: $("runSettingsWarning"),
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
  jobRecovery: $("jobRecovery"),
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
  ui.batchProblems.setAttribute("aria-current", currentJob ? "false" : "page");
  show(ui.selectedBatchBadge, Boolean(batch?.current));
  ui.selectedBatchBadge.textContent = "Current batch";
  show(ui.batchReadOnlyNotice, Boolean(batch?.readOnly || state.readOnly));
  ui.home.textContent = currentBatch ? "Back to problems" : "All batches";
  ui.reviewHome.textContent = ui.home.textContent;
  if (!currentJob) {
    show(ui.input, composingProblem);
    for (const panel of [ui.review, ui.run, ui.runOverview, ui.workflowRail, ui.activityToggle]) show(panel, false);
    ui.globalStatus.textContent = composingProblem ? "New problem" : currentBatch ? "Batch problems" : "Problem batches";
    document.title = composingProblem ? "New problem — TCS Prover"
      : currentBatch ? `${batch?.label || currentBatch} — TCS Prover` : "TCS Prover";
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
    // Work that needs the user comes first; transient phases count as running.
    const counts = batch.counts || {};
    for (const [key, label] of [["reviewed", "awaiting approval"], ["prepared", "ready to start"],
      ["paused", "paused"], ["running", "running"], ["done", "finished"]]) {
      const value = Number(counts[key] || 0) + (key === "running"
        ? Number(counts.reviewing || 0) + Number(counts.pausing || 0) + Number(counts.stopping || 0) : 0);
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
  clearJobView(); show(ui.notice, false); show(ui.batchStatus, false); renderBatchNavigation();
  ui.jobsList.replaceChildren();
  ui.jobsList.dataset.signature = "";
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
  ui.jobRecovery.replaceChildren();
  ui.jobRecovery.dataset.key = "";
  show(ui.jobRecovery, false);
  show(ui.batchStatus, false); // a folder-run result belongs to the view it was started from
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
  ui.jobsList.dataset.signature = "";
  const remaining = ui.jobsList.children.length;
  // The last problem took its batch with it: return to All batches, not to a missing batch.
  if (!remaining) return selectBatch("");
  ui.jobsCount.textContent = `${remaining} ${remaining === 1 ? "problem" : "problems"}`;
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
    clearJobView(); // also reached from a job page: start the new job on a clean view
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
    clearJobView(); // also reached from a job page: start the new job on a clean view
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
    clearJobView(); // also reached from a job page: start the new job on a clean view
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
  show(ui.jobsPanel, true);
  show(ui.jobsEmpty, jobs.length === 0);
  ui.jobsHeading.textContent = selectedBatch()?.label || "Problems and checkpoints";
  // Rebuild only when something visible changed, so polling keeps focus and hover.
  const signature = JSON.stringify([selectedBatch()?.readOnly || false, jobs.map(
    ({ lastActivityAt, activeElapsedSeconds, processAlive, workerActive, retrieval, ...job }) => ({
      ...job, retrieval: retrievalSummary({ ...job, retrieval }),
      lastActivityAt: job.phase === "done" ? lastActivityAt : "",
    }))]);
  if (ui.jobsList.dataset.signature === signature) return;
  ui.jobsList.dataset.signature = signature;
  ui.jobsList.replaceChildren();
  const tally = {};
  for (const job of jobs) {
    const key = ["reviewing", "pausing", "stopping"].includes(job.phase) ? "running" : job.phase;
    tally[key] = (tally[key] || 0) + 1;
  }
  ui.jobsCount.textContent = [`${jobs.length} ${jobs.length === 1 ? "problem" : "problems"}`,
    ...[["reviewed", "awaiting approval"], ["prepared", "ready to start"], ["paused", "paused"],
      ["running", "running"], ["done", "finished"]]
      .filter(([key]) => tally[key]).map(([key, label]) => `${tally[key]} ${label}`)].join(" · ");
  const stoppedStages = { review: "statement review", solve: "proof author", repair: "proof author",
    critic: "critic", final: "LaTeX editor", failure: "failure summary" };
  for (const job of jobs) {
    const readOnly = Boolean(job.readOnly || selectedBatch()?.readOnly);
    const active = ["reviewing", "running", "stopping", "pausing"].includes(job.phase);
    const outcome = job.phase === "done" ? completedRunLabel(job) : "";
    const tone = job.manuallyStopped ? "stopped" : active ? "running"
      : job.phase === "paused" ? "paused" : ["reviewed", "prepared"].includes(job.phase) ? "waiting"
      : ["Answer ready", "Review complete"].includes(outcome) ? "done" : "failed";
    const item = document.createElement("li");
    item.className = `job-card tone-${tone}`;
    item.dataset.job = job.runId;
    const copy = document.createElement("div");
    copy.className = "job-copy";
    const title = document.createElement("strong");
    title.textContent = job.title || job.draft?.trim().split("\n")[0]
      || "Untitled problem";
    const status = document.createElement("span");
    status.className = `job-status ${job.phase} tone-${tone}`;
    const labels = {
      reviewing: "Checking statement", reviewed: "Waiting for approval",
      running: "Running", stopping: "Stopping", pausing: "Pausing", paused: "Paused", done: outcome,
      prepared: "Ready to start",
    };
    status.textContent = job.launchHold ? "Prepared · awaiting review"
      : job.manuallyStopped
      ? `Stopped at ${stoppedStages[job.stoppedStage] || "saved stage"}`
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
      : `Started ${localTime(job.startedAt)}${finished ? ` · Finished ${localTime(finished)}` : ""}`;
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
    retrieval.className = `job-retrieval ${retrievalConfiguration(job).mode}`;
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
    // The card's primary button is the action the job is waiting for, if any.
    const open = document.createElement("button");
    open.className = job.phase === "reviewed" && !readOnly ? "primary compact" : "secondary compact";
    open.textContent = job.phase === "reviewed" && !readOnly ? "Review statement" : "Open";
    open.onclick = () => selectJob(job.runId);
    const separate = document.createElement("button");
    separate.className = "ghost compact job-new-window";
    separate.textContent = "Open in new window";
    separate.onclick = () => window.open(jobUrl(job.runId, true), "_blank", "noopener");
    const resume = document.createElement("button");
    resume.className = "secondary compact";
    resume.textContent = "Resume at critic";
    resume.hidden = readOnly || !job.canResumeCritic || checkpoints.length > 0;
    resume.onclick = () => resumeCritic(job, title.textContent);
    const continueButton = document.createElement("button");
    continueButton.className = ["paused", "prepared"].includes(job.phase) ? "primary compact" : "secondary compact";
    continueButton.textContent = job.continueStoppedLabel
      || "Continue stopped job";
    continueButton.hidden = readOnly || !job.canContinueStopped;
    continueButton.title = job.continueStoppedDescription || "";
    continueButton.onclick = () => continueStopped(job, title.textContent);
    // Running jobs must be stopped before they can be deleted.
    const remove = document.createElement("button");
    remove.className = "ghost compact job-delete";
    remove.textContent = "Delete";
    remove.hidden = readOnly || active;
    remove.onclick = () => deleteJob(job, title.textContent);
    actions.append(open, separate, continueButton, resume, remove);
    item.append(copy, actions);
    ui.jobsList.append(item);
  }
}

// The connection error that the last failed poll (of a list or a job) put in #notice.
let pollNotice = "";

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
    // A poll succeeded again: drop its earlier error, but keep errors from actions.
    if (pollNotice && ui.notice.textContent === pollNotice) show(ui.notice, false);
    pollNotice = "";
  } catch (error) {
    ui.notice.textContent = pollNotice = error.message;
    show(ui.notice, true);
  }
  if (!currentJob && !composingProblem && batch === currentBatch) jobsTimer = setTimeout(loadJobs, 2500);
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
    selectPrompt("author");
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

// The CLI group of a model id: "codex", "claude" or "api" ("" when unknown). The server's
// catalog decides; without one (an older server) the built-in ids follow a fixed pattern.
function modelProvider(model) {
  const row = (state.workflow?.settings?.model_catalog || []).find((item) => item.id === model);
  if (row) return row.provider;
  model = String(model || "");
  return model.startsWith("api:") ? "api" : /^gpt-/.test(model) ? "codex"
    : /^claude-/.test(model) ? "claude" : "";
}

// Web cutoff timestamps run only on native Codex CLI models: not on Claude CLI models and not
// on API models, even OpenAI-format ones that the Codex CLI runs.
function cutoffBlockedRoles() {
  return ["author", "critic", "writer"].filter((role) => modelProvider(ui[`${role}Model`].value) !== "codex");
}

// The server's rule word for word, then the roles that still need a Codex CLI model.
function cutoffModelMessage(roles = cutoffBlockedRoles()) {
  const names = roles.map((role) => ({ author: "proof author", critic: "independent critic",
    writer: "LaTeX writer" })[role]);
  const listed = names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0];
  return "Web cutoff timestamps require Codex CLI models for every proof role."
    + (listed ? ` Choose a Codex CLI model for the ${listed}.` : "");
}

// Under each role menu: the CLI that runs the chosen model, and an API model's format and
// host. With a web cutoff the proof roles' Claude CLI and API groups are disabled, and a
// chosen model that cannot run there is flagged (the choice itself is never changed).
function renderModelChoices() {
  const cutoff = selectedRetrievalMode() === "historical";
  const catalog = state.workflow?.settings?.model_catalog;
  const roles = ["review", "author", "critic", "writer"];
  for (const role of roles) {
    const select = ui[`${role}Model`], chip = ui[`${role}ModelProvider`];
    const model = select.value || "", provider = modelProvider(model);
    const row = (catalog || []).find((item) => item.id === model);
    const restricted = cutoff && role !== "review";
    const source = ui[`${role}Source`];
    for (const option of source?.options || []) {
      option.disabled = restricted && option.value !== "codex";
    }
    for (const group of select.querySelectorAll?.("optgroup") || []) {
      group.disabled = restricted && group.dataset.provider !== "codex";
    }
    let text = { codex: "Codex CLI", claude: "Claude CLI", api: "API" }[provider] || "Unknown model";
    let warning = restricted && provider !== "codex";
    if (provider === "codex" && catalog && !row) text += " · older model";
    if (provider === "api" && !row) {
      text += " · not configured";
      warning = true;
    } else if (provider === "api") {
      text += ` · ${row.format === "anthropic" ? "Anthropic" : "OpenAI"} format · ${row.host}`;
      if (row.hasKey === false) {
        text += " · no key";
        warning = true;
      }
    }
    if (restricted && provider !== "codex") text += " · not with a web cutoff";
    // A long API model name is cut off in the closed menu: hovering shows it whole.
    select.title = select.selectedOptions?.[0]?.textContent || "";
    chip.textContent = text;
    chip.dataset.provider = provider || "unknown";
    chip.dataset.state = warning ? "warning" : "";
  }
}

function renderRetrievalSettings() {
  const mode = selectedRetrievalMode(), historical = mode === "historical";
  const enabled = mode !== "live";
  const capabilities = state.retrievalCapabilities || {};
  show(ui.retrievalCutoffSetting, enabled);
  ui.retrievalCutoffUtc.required = enabled;
  ui.retrievalCutoffUtc.disabled = !enabled;
  ui.retrievalSettingHelp.textContent = historical
    ? "The proof author discovers general web sources through the Codex CLI, then reads verified archived captures before your cutoff. The critic and writer have no external retrieval tools."
    : mode === "disabled" ? "All model web-search tools are disabled. This setting controls search tools; it does not isolate ordinary shell network access."
    : "The model's native web search is enabled without a date restriction.";
  const message = !historical ? ""
    : selectedProblemMode() === "latex" || ui.statementReviewOnly.checked
      ? "A historical cutoff requires a full proof job with Skip statement review. For this workflow, use cutoff Off or enter -1 to disable web-search tools."
    : capabilities.historicalAvailable !== true
    ? capabilities.reason || "Historical search setup is unavailable in this interface. Open the updated TCS Prover interface to create a historical job."
    : !ui.skipStatementReview.checked
      ? `Select Skip statement review under Workflow to start a historical proof job. ${cutoffModelMessage()}`
      : cutoffModelMessage();
  ui.retrievalConfigurationNotice.textContent = message;
  show(ui.retrievalConfigurationNotice, Boolean(message));
  renderModelChoices();
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
  } else if (cutoffBlockedRoles().length) {
    message = cutoffModelMessage();
    focus = ui[`${cutoffBlockedRoles()[0]}Model`];

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
  // Model ids share suffixes ("sol", "luna"), so name every known model.
  const modelNames = new Map([
    ["gpt-6.1-sol", "6.1 Sol"], ["gpt-6-astra", "Astra"],
    ["gpt-6-sol", "6 Sol"], ["gpt-6-luna", "6 Luna"],
    ["gpt-5.6-sol", "5.6 Sol"], ["gpt-5.6-terra", "5.6 Terra"],
    ["gpt-5.6-luna", "5.6 Luna"], ["claude-fable-5-1", "Fable 5.1"],
    ["claude-opus-5-5", "Opus 5.5"], ["claude-sonnet-5-5", "Sonnet 5.5"],
    ["claude-haiku-5-5", "Haiku 5.5"],
  ]);
  // An API model goes by its display name, which its menu option carries as data-short.
  const name = (select) => modelNames.get(select.value)
    || select.selectedOptions?.[0]?.dataset?.short
    || (select.value.startsWith("api:") ? select.value.slice(4)
      : select.value.split("-").at(-1).replace(/^./, (letter) => letter.toUpperCase()));
  const efforts = {
    ultra: "Ultra", max: "Max", xhigh: "Extra high", high: "High", medium: "Medium", low: "Low",
  };
  const role = (select, effort) => `${name(select)}/${efforts[effort.value] || effort.value}`;
  const mode = selectedProblemMode();
  const reviewOnly = mode === "statement" && ui.statementReviewOnly.checked;
  let selectedModels = reviewOnly ? [ui.reviewModel.value]
    : mode === "latex" ? [ui.writerModel.value]
    : [ui.authorModel.value, ui.criticModel.value, ui.writerModel.value];
  if (mode === "statement" && !reviewOnly && !ui.skipStatementReview.checked) {
    selectedModels.push(ui.reviewModel.value);
  }
  // Fast is a ChatGPT service tier: only native Codex CLI models (gpt-… ids) run it.
  const fast = selectedModels.filter((model) => /^gpt-/.test(model)).length;
  const speed = ui.speedMode.value === "standard" ? ["Standard speed"]
    : fast === selectedModels.length ? ["Fast speed"]
    : fast ? ["Fast for Codex CLI", "Standard for others"] : ["Standard speed", "Fast is Codex CLI only"];
  const log = {
    none: "Status-only log",
    concise: "Concise activity log",
    detailed: "Detailed activity log",
  }[ui.reasoningSummary.value] || "Concise activity log";
  const roles = reviewOnly ? [`${role(ui.reviewModel, ui.reviewEffort)} review only`]
    : mode === "latex" ? [`${role(ui.writerModel, ui.writerEffort)} writer`]
    : [...(ui.skipStatementReview.checked ? [] : [`${role(ui.reviewModel, ui.reviewEffort)} review`]),
      `${role(ui.authorModel, ui.authorEffort)} author`, `${role(ui.criticModel, ui.criticEffort)} critic`,
      `${role(ui.writerModel, ui.writerEffort)} writer`];
  // No-break spaces (and a word joiner after each hyphen of an API model's name) keep each part
  // ("6.1 Sol/Max review") on one line: a wrapped summary breaks only after a "·", so no line
  // starts with one and no model name is split. A part longer than a line still wraps (CSS).
  ui.modelSummary.textContent = [...speed, log, ...roles]
    .map((part) => part.replace(/ /g, "\u00a0").replace(/-/g, "-\u2060")).join("\u00a0· ");
}

const promptLabels = {
  review: "Reviewer prompt", author: "Author prompt",
  critic: "Critic prompt", final: "Final writer prompt",
};

const promptHelp = {
  review: "The full request also includes the statement and any revision feedback.",
  author: "Keep exactly one [STATEMENT]. The workflow replaces it with your statement.",
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
  show(ui.promptError, false);
  updatePromptHelp();
  for (const tab of ui.promptTabs.querySelectorAll(".prompt-tab")) {
    const selected = tab.dataset.prompt === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
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
      : ui.skipStatementReview.checked ? "author" : "review";
    selectPrompt(activePrompt);
    ui.promptDialog.showModal();
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

// The first draft that cannot be applied, as [role, explanation].
function promptProblem() {
  const empty = Object.keys(promptDrafts).find((name) => !promptDrafts[name].trim());
  if (empty) return [empty, "Every role prompt must contain instructions."];
  return (promptDrafts.author?.match(/\[STATEMENT\]/g) || []).length === 1
    ? null : ["author", "The author prompt must contain exactly one [STATEMENT]."];
}

// The dialog is modal, so its own errors appear inside it, next to the prompt to fix.
function showPromptError(message) {
  ui.promptError.textContent = message;
  show(ui.promptError, true);
}

function savePrompts() {
  promptDrafts[activePrompt] = ui.promptEditor.value;
  const problem = promptProblem();
  if (problem) {
    const [role, message] = problem;
    selectPrompt(role);
    showPromptError(message);
    ui.promptEditor.focus();
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
  // As on the server: the third and later attempts of one retried model call
  // stay in the recent window instead of being pinned through a long outage.
  if (["request", "diagnostic"].includes(entry.kind)
      && Number.isInteger(entry.attempt) && entry.attempt > 2) return false;
  if (pinnedKinds.has(entry.kind)) return true;
  if (entry.kind === "status" && entry.label === "Critic approved"
      && entry.root !== false && !entry.activityLabel) return true;
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
      time: entry.time, replace: true, placeholder: true,
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
    "collabAgentToolCall", "collab_agent_tool_call", "collab_tool_call", "subAgentActivity",
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
          ? "No historical audit reports are saved here."
          : memoryFile === "AUDITS" || memoryFile.startsWith("AUDITS/")
          ? "No saved audit reports are available."
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

function selectMemoryDocument(name, anchor = "") {
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
    && state.problemMode !== "latex" && !state.statementReviewOnly;
  show(ui.memoryPanel, visible);
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
  card.text = card.append && old && !old.placeholder ? old.text + card.text : card.text;
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

  // Bound the routine rows, oldest first; pinned rows stay. Never evict the row just
  // updated, or with 300 pinned rows every new event would vanish as it arrives.
  if (ui.timeline.children.length > 300) {
    const routine = [...ui.timeline.children].filter(
      (item) => item.dataset.pinned !== "true" && item !== row
    );
    while (routine.length >= 300) {
      const disposable = routine.shift();
      timelineRows.delete(disposable.dataset.key);
      disposable.remove();
    }
  }
  if (nearBottom) ui.timeline.scrollTop = ui.timeline.scrollHeight;
}

// Keep only exact application prompts and root-model response text.
function detailCard(entry) {
  if (entry.kind === "request" && entry.text) {
    return {
      key: `detail-prompt:${entry.time}`,
      label: "Prompt to model",
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

// An API model id that the server's current catalog no longer lists. /state rebuilds the catalog
// on every poll, so a model removed while this page is open shows up here at once (the server
// states it in settingsWarning only when it loads the job). Without a catalog (an older
// server), nothing is claimed.
function unconfiguredApiModel(model) {
  const catalog = state.workflow?.settings?.model_catalog;
  return Array.isArray(catalog) && String(model || "").startsWith("api:")
    && !catalog.some((row) => row.id === model);
}

// One role's saved model id and effort; an API model adds the endpoint the job recorded
// (state.apiModels holds that key-free snapshot, kept even after the model is removed).
function modelRecord(model, effort) {
  const api = state.apiModels?.[model];
  const endpoint = api ? ` · ${api.label} · ${api.format === "anthropic" ? "Anthropic" : "OpenAI"} format`
    + ` · ${api.baseUrl} · model ${api.model}` : "";
  return `${model}${unconfiguredApiModel(model) ? " (not configured)" : ""} · ${effort}${endpoint}`;
}

// A saved job cannot change its models, so a removed API model it needs must be added again:
// with the settings the job recorded (any display name then gets the same id back), or else
// with the display name that gives this id. The server's words at load and at Resume.
function unconfiguredJobModelText(model, role) {
  const labels = { review: "statement reviewer", author: "proof author", critic: "critic", writer: "LaTeX editor" };
  const saved = state.apiModels?.[model];
  const how = saved
    ? `with the same settings (${saved.label} · ${saved.format === "anthropic" ? "Anthropic" : "OpenAI"} format · `
      + `${saved.baseUrl} · model ${saved.model})`
    : `with the display name “${model.slice("api:".length)}”`;
  return `The API model ${model} chosen for the ${labels[role]} is not configured. Add it again under `
    + `New problem → Advanced settings → API connections ${how} to continue this job.`;
}

// The settings warning under the job title: the server's, then any API model this job still
// needs that was removed since the server loaded it, in the words the server uses at load (and
// at Resume), so a reload or a restart shows the same text.
function jobSettingsWarning() {
  const warning = String(state.settingsWarning || "");
  if (state.readOnly) return warning;
  const roles = state.statementReviewOnly ? ["review"]
    : ["latex", "final-resume"].includes(state.problemMode) ? ["writer"]
    : [...(state.skipStatementReview || ["algorithmic", "critic-resume"].includes(state.problemMode) ? [] : ["review"]),
      "author", "critic", "writer"];
  const missing = roles.filter((role) => unconfiguredApiModel(state[`${role}Model`]))
    .map((role) => unconfiguredJobModelText(state[`${role}Model`], role))
    .filter((text) => !warning.includes(text));
  return [warning, ...missing].filter(Boolean).join(" ");
}

function renderRunOverview() {
  show(ui.runOverview, state.phase !== "input");
  if (state.phase === "input") return;
  // One status vocabulary for the job header, the now-card and their colours.
  const outcome = state.phase === "done" ? completedRunLabel(state) : "";
  ui.runOverview.dataset.phase = state.phase;
  ui.runOverview.dataset.status = ["reviewing", "running", "stopping", "pausing"].includes(state.phase) ? "running"
    : state.phase === "paused" ? "paused"
    : ["reviewed", "prepared"].includes(state.phase) ? "waiting"
    : outcome === "Stopped" ? "stopped"
    : ["Answer ready", "Review complete"].includes(outcome) ? "done" : "failed";
  ui.runOverview.dataset.retrieval = retrievalConfiguration(state).mode;
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
  let nextStep = state.phase === "reviewed" ? "Statement review finished. Approval is needed before proof search can begin."
    : state.phase === "paused" ? (state.readOnly ? "No final answer yet. This saved job is read-only."
      : "No final answer yet. The work so far is saved.")
    : state.phase === "prepared" ? (state.launchHold || "Start this prepared workspace to begin proof search.")
    : state.phase === "done" ? (state.resultKind === "final" || state.canDownloadTex
      ? "The final answer is saved below. Source and PDF downloads are available for saved artifacts."
      : state.resultKind === "review" ? "The statement review is saved."
        : "No completed answer was produced. Saved partial work and checkpoints remain available.")
    : state.statementReviewOnly ? "This job will save the checked statement and reviewer notes, then finish."
    : nextSteps[state.stage] || "Waiting for the next workflow update.";
  // The critic verdict explains a LaTeX stage, but must not lead an incomplete result.
  if (latestCycle && outcome !== "Incomplete"
      && ["latex_editor", "latex_compile", "latex_repair"].includes(state.activeNode)) {
    nextStep = `${criticCycleOutcomeLabel(latestCycle)}. ${nextStep}`;
  }
  // A role=status region: write only real changes so the 1 s clock does not re-announce it.
  if (ui.runNextStep.textContent !== nextStep) ui.runNextStep.textContent = nextStep;
  const latest = [...(state.trace || [])].reverse().find(entry => !entry.heartbeat
    && !["request", "review_result", "critic_result", "author_result", "final_result", "failure_result", "partial_result"].includes(entry.kind)
    && describe(entry)?.text);
  const card = latest && describe(latest);
  // A streamed delta is one fragment; its timeline row holds the text received so far.
  const text = card?.append ? timelineRows.get(card.key)?._card?.text || card.text : card?.text;
  ui.runLatestAction.textContent = card
    ? `Latest activity · ${clockText(latest.time)} · ${card.label}: ${String(text).replace(/\*\*/g, "").replace(/\s+/g, " ").slice(0, 260)}`
    : "Public activity appears below as the model works.";
  // A saved choice the server had to replace or cannot run (a discontinued model, a deleted
  // API model) is stated in the header, where it is seen before Resume. Written only on change.
  const settingsWarning = jobSettingsWarning();
  if (ui.runSettingsWarning.textContent !== settingsWarning) ui.runSettingsWarning.textContent = settingsWarning;
  show(ui.runSettingsWarning, Boolean(settingsWarning));
  renderRetrievalOverview();
  const tabTitle = ui.runProblemTitle.textContent;
  const pageTitle = `${ui.runStage.textContent} · ${tabTitle.length > 80 ? `${tabTitle.slice(0, 79)}…` : tabTitle} — TCS Prover`;
  if (document.title !== pageTitle) document.title = pageTitle;
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
    ["Statement reviewer", state.skipStatementReview ? "Skipped" : modelRecord(state.reviewModel, state.reviewEffort || state.reasoningEffort)],
    ["Proof author", modelRecord(state.authorModel, state.authorEffort || state.reasoningEffort)],
    ["Independent critic", `${modelRecord(state.criticModel, state.criticEffort || state.reasoningEffort)} · max ${state.criticRounds} consecutive rounds`],
    ["LaTeX writer", modelRecord(state.writerModel, state.writerEffort || state.reasoningEffort)],
    ...(settingsWarning ? [["Settings warning", settingsWarning]] : []),
    ["Generation", state.speedMode || "standard"], ["Activity summaries", state.reasoningSummary || "concise"],
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

// Show the author and critic/repair cycle.
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
  // Polling rebuilds the rail; keep the reader's place in the recorded cycles too.
  const criticHistoryScroll = ui.workflowNodes.querySelector?.(".critic-history > ol")?.scrollTop || 0;
  let criticHistoryList = null;
  const latexOnly = ["latex", "final-resume"].includes(state.problemMode);
  const criticResume = state.problemMode === "critic-resume";
  const startsAtAuthor = state.problemMode === "algorithmic"
    || state.skipStatementReview;
  const seenInThisJob = new Set((state.trace || []).map(
    (entry) => entry.node || nodeFromStage(entry.stage)
  ));
  const seen = new Set(seenInThisJob);
  const authorStage = state.activeNode === "author" && ["solve", "repair"].includes(state.stage);
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
    let noteText = overrides.note || (paused ? "Paused"
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
      criticHistoryList = list;
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
  const critic = makeNode("critic", startsAtAuthor ? "2" : "3");
  const passStem = document.createElement("li");
  passStem.className = "critic-pass-stem";
  passStem.setAttribute("aria-hidden", "true");
  loopNodes.append(
    author, candidateRoute, critic, selfRoute, passStem, rejectRoute,
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
  // Only an attached, open list can scroll, so restore its position after the swap.
  if (criticHistoryList) criticHistoryList.scrollTop = criticHistoryScroll;
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
  } else {
    // Never keep the previous job's subtitle on a job without activity.
    ui.lastActivity.textContent = "Waiting for the first update…";
  }
}

// Merge incremental polling state, then update the visible phase.
function render(next) {
  // A control's reply is a full snapshot (no `after`). When it continues the run on screen,
  // take only its unseen tail so the timeline and drawer keep their rows and open prompts.
  const tail = Boolean(next.runId) && next.runId === state.runId
    && (next.batchId || "") === (state.batchId || "")
    && next.traceFrom < state.traceVersion && state.traceVersion <= next.traceVersion
    && (next.trace || []).length >= next.traceVersion - next.traceFrom;
  const incremental = tail || next.traceFrom === state.traceVersion;
  const acceptancesChanged = JSON.stringify(next.criticAcceptances || [])
    !== JSON.stringify(state.criticAcceptances || []);
  const newEntries = tail ? next.trace.slice(next.trace.length - (next.traceVersion - state.traceVersion))
    : next.trace || [];
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
  const working = ["reviewing", "running", "stopping", "pausing"].includes(phase);
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
  // The job's own error is stated once, in the now-card. While the run panel is hidden
  // (a failed statement review returns to the composer) the notice states it instead.
  // Otherwise the notice keeps action feedback until the phase changes.
  if (ui.run.hidden && state.error) {
    if (ui.notice.textContent !== state.error) ui.notice.textContent = state.error;
    show(ui.notice, true);
  } else if (phase !== previousPhase) show(ui.notice, false);
  ui.traceNotice.textContent = state.traceNotice || "Only the most recent saved activity is shown here.";
  show(ui.traceNotice, Boolean(state.traceTruncated || state.traceNotice));

  if (phase === "input" && previousPhase !== "input") {
    const rounds = state.workflow?.settings?.critic_rounds || {};
    const hours = state.workflow?.settings?.thinking_hours || {};
    ui.problem.value = state.draft || "";
    ui.latexInput.value = state.latexInput || "";
    syncModelMenus();
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
    syncPrompts(state);
    updateModelSummary();
    ui.criticRounds.value = state.criticRounds || 2;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.thinkingHours.value = state.thinkingHours ?? 168;
    ui.thinkingHours.min = hours.minimum ?? 0;
    ui.thinkingHours.max = hours.maximum || 168;

    setProblemMode(state.problemMode || "statement");
  }
  if (reviewReady && (previousPhase !== phase || reviewPending)) {
    syncModelMenus();
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
    : phase === "prepared" ? "Ready to start" : phase === "paused" ? "Paused" : phase === "pausing" ? "Pausing"
    : phase === "reviewed" ? "Waiting for approval"
      : phase === "stopping" ? "Stopping"
        : done ? completedRunLabel(state) : (node.label || "Model is working");
  ui.runLabel.textContent = phase === "prepared" ? "PREPARED RUN" : phase === "paused" ? "RUN PAUSED"
    : done ? (state.resultKind === "final" || state.canDownloadTex ? "ANSWER READY" : "RUN INCOMPLETE") : (node.short_label || "MODEL");
  // A short status title; the error, when there is one, is the description below it.
  ui.runTitle.textContent = phase === "reviewing" ? "Checking the statement…"
    : phase === "prepared" ? "Prepared research workspace" : phase === "paused" ? "Ready to resume" : phase === "pausing" ? "Pausing…"
    : phase === "stopping" ? "Stopping safely…"
      : done ? completedRunLabel(state) : (node.label || "Working…");
  // The header already says what happens next; the now-card adds only the error or what its buttons need.
  ui.runDescription.textContent = (done && state.manuallyStopped ? "You stopped this job." : state.error)
    || (phase === "prepared" ? (state.launchHold || "Review the prepared statement and settings, then start a fresh author session in this workspace.")
      : phase === "paused" ? "Resume reopens the saved author conversation in this same run folder. You can update your CLI login or API model settings before resuming."
      : phase === "pausing" ? "Interrupting the current turn and preserving the saved conversation. Wait for Paused before closing the UI."
      : "");
  if (phase === "paused" && state.autoResumeAt) {
    const retryTime = new Date(state.autoResumeAt * 1000).toLocaleTimeString();
    ui.runDescription.textContent += ` Automatic retry at ${retryTime}. Choose Keep paused to cancel retries.`;
  }
  // A failed model call waits to retry; the job keeps running meanwhile.
  if (["reviewing", "running"].includes(phase) && state.modelRetry?.text && !state.error) {
    const next = Number(state.modelRetry.retryAt);
    ui.runDescription.textContent = state.modelRetry.text
      + (Number.isFinite(next) && next > 0 ? ` Next attempt at ${new Date(next * 1000).toLocaleTimeString()}.` : "");
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

  show(ui.authorSteerControl, authorRunning);
  ui.sendAuthorSteer.disabled = !authorRunning;
  show(ui.authorTimeLimitControl, canSetAuthorLimit);
  const authorLimitText = authorLimit.toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  ui.authorLimitSummary.textContent = authorLimit === 0 ? "No time limit" : `Time limit: ${authorLimitText} hours`;
  ui.authorLimitHours.max = String(maximumAuthorLimit);
  if (canSetAuthorLimit && document.activeElement !== ui.authorLimitHours) {
    ui.authorLimitHours.value = String(authorLimit);
  }
  ui.setAuthorTimeLimit.disabled = !canSetAuthorLimit;
  const retryPending = phase === "paused" && Boolean(state.autoResumeAt);
  show(ui.pause, authorRunning || retryPending);
  ui.pause.textContent = retryPending ? "Keep paused" : "Pause";
  ui.pause.title = retryPending ? "Cancel automatic retries"
    : "Pause the author";
  show(ui.resume, phase === "paused" || phase === "prepared");
  ui.resume.textContent = state.launchHold ? "Awaiting review" : phase === "prepared" ? "Start prepared run" : "Resume";
  ui.resume.disabled = Boolean(state.launchHold);
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
      ui.approve, ui.recheck,
      ui.reviewFeedbackControls]) show(control, false);
    ui.proposed.readOnly = true;
    // Saved batches cannot resume; do not promise an action that is not offered.
    if (phase === "paused") {
      ui.runTitle.textContent = "Paused";
      ui.runDescription.textContent = state.error || "";
    }
  }
  // Announce a new phase by moving focus to its heading, but never out of a field being typed in.
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (phase !== previousPhase && !typing) {
    const heading = reviewReady ? ui.reviewHeading
      : (phase !== "input" ? ui.runTitle : null);
    heading?.focus({ preventScroll: true });
  }
  // The log is a bounded pane filled while hidden; show its newest event on every phase change.
  if (phase !== previousPhase) ui.timeline.scrollTop = ui.timeline.scrollHeight;
  previousPhase = phase;

  clearTimeout(timer);
  const waiting = ["paused", "prepared", "reviewed"].includes(phase);
  if (working || waiting) timer = setTimeout(refresh, working ? 700 : 2500);
  clearInterval(clock);
  if (working || waiting) clock = setInterval(renderClock, 1000);
}

// A saved job may name a model the menus no longer offer (an older GPT-5.6, say,
// or an API model removed since). Keep that choice visible instead of leaving the menu blank.
function setChoice(select, value) {
  const role = ["review", "author", "critic", "writer"].find((name) => ui[`${name}Model`] === select);
  if (role && ui[`${role}Source`]?.options) {
    const row = (state.workflow?.settings?.model_catalog || []).find((item) => item.id === value);
    const source = row ? modelSourceId(row) : modelProvider(value);
    syncRoleModelMenu(role, source, value);
    return;
  }
  // A stand-in belongs to the job that needed it: never offer it to the next
  // job or to a new problem (a retired or removed model would be selectable).
  for (const option of Array.from(select.options || [])) {
    if (option.dataset?.standIn && option.value !== value) option.remove();
  }
  const known = Array.from(select.options || [], (option) => option.value);
  if (value && known.length && !known.includes(value)) {
    const option = new Option(`${value} (${value.startsWith("api:") ? "not configured" : "older"})`, value);
    option.dataset.standIn = "1";
    select.add(option);
  }
  select.value = value;
}

// Offer only the reasoning levels the selected model supports (the server's catalog), and
// move a choice the model does not offer to the nearest lower level it does: Ultra becomes
// Max on Luna, Claude and API models, as the runner itself would run it.
function syncEffortChoices(role) {
  const order = ["low", "medium", "high", "xhigh", "max", "ultra"];
  const known = state.workflow?.settings?.model_efforts;
  const select = ui[`${role}Effort`];
  if (!known || !select.options) return;
  // A model the catalog does not describe (a stand-in for a saved job) is never filtered.
  const supported = known[ui[`${role}Model`].value] || order;
  Array.from(select.options).forEach((option) => {
    option.disabled = option.hidden = !supported.includes(option.value);
  });
  if (!supported.includes(select.value)) {
    const levels = order.filter((level) => supported.includes(level));
    const lower = levels.filter((level) => order.indexOf(level) < order.indexOf(select.value));
    select.value = !order.includes(select.value) ? (supported.includes("max") ? "max" : levels.at(-1))
      : lower.at(-1) || levels[0];
  }
}

// The footer names an API model by its display name, briefly: "·" separates the footer's own
// parts, so it is dropped here. A name over 24 characters (about what a closed menu shows, and
// short enough that "<name>/Extra high critic" fits a phone's line) is cut where a word ends, or
// after a "-", ".", "/", ":" or "_" inside a long one; mid-word only when that leaves too little.
function shortModelName(label) {
  const name = String(label).replace(/\s*·\s*/g, " ").trim();
  if (name.length <= 24) return name;
  const head = name.slice(0, 24);
  const whole = /[\s\-./:_]/.test(name[24]) ? head : head.replace(/[^\s\-./:_]+$/, "");
  const cut = whole.replace(/[\s\-./:_]+$/, "");
  return `${cut.length >= 10 ? cut : name.slice(0, 23).trimEnd()}…`;
}

function modelSourceId(row) {
  return row.sourceId || (row.provider === "api" ? row.id : row.provider);
}

function roleModelCatalog() {
  const catalog = state.workflow?.settings?.model_catalog;
  if (Array.isArray(catalog)) return catalog;
  // Until /state arrives, the default Codex models are available from the markup.
  return [...ui.authorModel.options].map((option) => ({ id: option.value,
    label: option.textContent, provider: modelProvider(option.value) }));
}

function modelSourceChoices(catalog) {
  const saved = state.workflow?.settings?.model_sources;
  if (Array.isArray(saved)) return [...saved];
  const sources = [{ id: "codex", label: "Codex CLI" }, { id: "claude", label: "Claude CLI" }];
  for (const row of catalog.filter((item) => item.provider === "api")) {
    const id = modelSourceId(row);
    if (!sources.some((source) => source.id === id)) {
      sources.push({ id, label: row.sourceLabel || row.label, provider: "api" });
    }
  }
  return sources;
}

function modelSourceLabel(source, sources) {
  if (source.provider !== "api" && !source.id.startsWith("api:")) return source.label;
  const duplicate = sources.filter((item) => item.label === source.label).length > 1;
  return `API key · ${source.label}${duplicate && source.keyHint ? ` · ${source.keyHint}` : ""}`;
}

function syncRoleModelMenu(role, sourceId, preferred = "") {
  const select = ui[`${role}Model`], source = ui[`${role}Source`];
  if (!source) return;
  const catalog = roleModelCatalog(), sources = modelSourceChoices(catalog);
  if (!sources.some((item) => item.id === sourceId)) {
    if (preferred && preferred.startsWith("api:")) {
      sources.push({ id: sourceId || preferred, label: "API connection · not configured" });
      sourceId ||= preferred;
    } else sourceId = "codex";
  }
  const sourceSignature = JSON.stringify(sources.map((item) => [item.id, modelSourceLabel(item, sources)]));
  if (source.dataset.catalog !== sourceSignature) {
    source.replaceChildren(...sources.map((item) => new Option(modelSourceLabel(item, sources), item.id)));
    source.dataset.catalog = sourceSignature;
  }
  source.value = sourceId;
  const rows = catalog.filter((row) => modelSourceId(row) === sourceId);
  const signature = JSON.stringify(rows.map((row) => [row.id, row.label]));
  if (select.dataset.catalog !== signature) {
    select.replaceChildren(...rows.map((row) => {
      const option = new Option(row.label, row.id);
      option.dataset.short = shortModelName(row.label);
      return option;
    }));
    select.dataset.catalog = signature;
  }
  if (preferred && !rows.some((row) => row.id === preferred)) {
    for (const option of [...select.options]) if (option.dataset.standIn) option.remove();
    const option = new Option(`${preferred} (${preferred.startsWith("api:") ? "not configured" : "older"})`, preferred);
    option.dataset.standIn = "1";
    select.append(option);
  }
  if (!rows.length && !preferred) {
    select.replaceChildren(new Option("No models loaded — refresh this API connection", ""));
  }
  select.disabled = !rows.length && !preferred;
  select.value = preferred || rows[0]?.id || "";
  ui[`${role}Effort`].disabled = !select.value;
}

// Each role first picks a source, then sees only that source's models. Refreshing
// a catalog preserves valid choices; removed models fall back to the default.
function syncModelMenus() {
  if (!Array.isArray(state.workflow?.settings?.model_catalog)) return [];
  const catalog = roleModelCatalog();
  const replaced = [], roles = ["review", "author", "critic", "writer"];
  for (const role of roles) {
    const select = ui[`${role}Model`];
    let value = select.value || "gpt-6.1-sol";
    let row = catalog.find((item) => item.id === value);
    if (!row && value.startsWith("api:")) {
      value = "gpt-6.1-sol";
      row = catalog.find((item) => item.id === value);
      replaced.push(role);
    }
    syncRoleModelMenu(role, row ? modelSourceId(row) : modelProvider(value), value);
  }
  return replaced;
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
    // A poll succeeded again: drop the connection error that an earlier poll showed.
    if (pollNotice && ui.notice.textContent === pollNotice) show(ui.notice, false);
    pollNotice = "";
    render(next);
    loadJobRecovery();
  } catch (error) {
    if (job !== currentJob || batch !== currentBatch) return;
    ui.notice.textContent = pollNotice = error.message;
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

// Offer a finished job's continue and checkpoint actions on its own page, using
// the same summaries (and handlers) as the batch list.
async function loadJobRecovery() {
  const job = currentJob, batch = currentBatch;
  const key = JSON.stringify([batch, job, state.phase, state.readOnly, state.manuallyStopped,
    (state.checkpoints || []).map(item => [item.id, item.resumable])]);
  if (ui.jobRecovery.dataset.key === key) return;
  ui.jobRecovery.dataset.key = key;
  ui.jobRecovery.replaceChildren();
  show(ui.jobRecovery, false);
  if (!job || state.readOnly || state.phase !== "done"
    || ["Answer ready", "Review complete"].includes(completedRunLabel(state))) return;
  let summary;
  try {
    summary = ((await request(jobPath("/jobs"))).jobs || []).find(item => item.runId === job);
  } catch (_) {
    return; // The batch list still offers these actions.
  }
  if (!summary || job !== currentJob || batch !== currentBatch) return;
  const title = ui.runProblemTitle.textContent;
  const action = (label, primary, onclick, detail = "") => {
    const row = document.createElement("div"), button = document.createElement("button");
    row.className = "recovery-row";
    button.type = "button";
    button.className = primary ? "primary compact" : "secondary compact";
    button.textContent = label;
    button.onclick = onclick;
    row.append(button);
    if (detail) {
      const text = document.createElement("small");
      text.textContent = detail;
      row.append(text);
    }
    return row;
  };
  const checkpoints = (summary.checkpoints || []).filter(item => item.resumable);
  const rows = [];
  if (summary.canContinueStopped) {
    rows.push(action(summary.continueStoppedLabel || "Continue stopped job", true,
      () => continueStopped(summary, title), summary.continueStoppedDescription || ""));
  }
  for (const checkpoint of checkpoints) {
    rows.push(action(checkpoint.resumeLabel || "Continue", !rows.length,
      () => resumeCheckpoint(summary, checkpoint, title),
      [checkpoint.label, checkpoint.description].filter(Boolean).join(" · ")));
  }
  if (summary.canResumeCritic && !(summary.checkpoints || []).length) {
    rows.push(action("Resume at critic", !rows.length, () => resumeCritic(summary, title),
      "Start a new critic job from the complete saved proof."));
  }
  if (!rows.length) return;
  const heading = document.createElement("h3");
  heading.textContent = "Continue from saved work";
  ui.jobRecovery.replaceChildren(heading, ...rows);
  show(ui.jobRecovery, true);
}

async function startReview(statement, feedback = "") {
  // One create request at a time: a second click must not start a second paid job.
  if (ui.check.disabled) return;
  if (!validateRetrievalSettings()) return;
  // The mode toggles belong to the composer; Retry keeps the reviewed job's own workflow.
  const fromComposer = state.phase === "input";
  const reviewOnly = fromComposer && !feedback && ui.statementReviewOnly.checked;
  const skipReview = fromComposer && !reviewOnly && !feedback && ui.skipStatementReview.checked;
  if (!reviewOnly && !validateWorkflowHours()) return;
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  if (!(skipReview ? promptValues.author : promptValues.review)) syncPrompts();
  reviewPending = !skipReview;
  const job = currentJob;
  const batch = currentBatch;
  const label = ui.check.textContent;
  ui.check.disabled = ui.runFolder.disabled = true;
  ui.check.textContent = "Starting…";
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
    showCreateError(error.message);
    // A failed Retry must not end the review page's polling (the composer does not poll).
    if (job && job === currentJob && batch === currentBatch && state.phase === "reviewed") {
      clearTimeout(timer);
      timer = setTimeout(refresh, 700);
    }
  } finally {
    ui.check.disabled = ui.runFolder.disabled = false;
    // The selected mode may have relabelled the button meanwhile.
    if (ui.check.textContent === "Starting…") ui.check.textContent = label;
  }
}

// Create errors follow a click on the sticky composer footer, often far below
// the notice: bring it into view. It is an alert, so it is not focused (some
// screen readers would announce it twice).
function showCreateError(message) {
  ui.notice.textContent = message;
  show(ui.notice, true);
  ui.notice.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
    criticRounds: Number(ui.criticRounds.value),
    thinkingHours: Number(ui.thinkingHours.value),
    speedMode: ui.speedMode.value,
    reasoningSummary: ui.reasoningSummary.value,
    ...selectedRetrievalSettings(),
  };
}

// An empty or malformed number field reads as "", which Number() would turn into 0 (unlimited).
function validateWorkflowHours() {
  if (ui.thinkingHours.value.trim() !== "") return true;
  ui.notice.textContent = `Enter a time limit: 0 for unlimited, or at most ${Number(ui.thinkingHours.max || 168)} hours.`;
  show(ui.notice, true);
  const advanced = ui.thinkingHours.closest("details");
  if (advanced) advanced.open = true;
  ui.thinkingHours.focus();
  return false;
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
    // Hidden files (.DS_Store, ._a.md, .gitignore) and Windows folder metadata are never statements.
    return path.split("/").length === 2 && !file.name.startsWith(".")
      && !/^(?:thumbs\.db|ehthumbs\.db|desktop\.ini)$/i.test(file.name);
  }).sort(byName);
}

// The server caps one folder request; check before sending megabytes for nothing.
const maxFolderRequestBytes = 8 * 1024 * 1024;

// Read each text file; empty and binary files are reported instead of sent.
async function folderStatements(files) {
  const statements = [], empty = [], binary = [];
  for (const file of folderStatementFiles(files)) {
    if (file.size > maxFolderRequestBytes) throw new Error(`${file.name} is larger than 8 MB.`);
    const statement = await file.text();
    // A NUL marks binary data (images, archives, UTF-16 text), which the server refuses.
    if (statement.includes("\0")) binary.push(file.name);
    else if (statement.trim()) statements.push({ name: file.name, statement });
    else empty.push(file.name);
  }
  return { statements, empty, binary };
}

// One folder batch at a time: a repeated change event must not start its jobs twice.
let folderBatchBusy = false;

async function startFolderBatch(files) {
  const chosen = [...(files || [])];
  // A single job being created also blocks the folder run (and the reverse).
  if (!chosen.length || folderBatchBusy || ui.check.disabled) return;
  if (!validateRetrievalSettings({ folder: true }) || !validateWorkflowHours()) return;
  folderBatchBusy = true;
  ui.runFolder.disabled = ui.check.disabled = true;
  try {
    await sendFolderBatch(chosen);
  } finally {
    folderBatchBusy = false;
    ui.runFolder.disabled = ui.check.disabled = false;
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
    showCreateError(`Could not read the files in ${folder}: ${error.message}`);
    return;
  }
  const { statements, empty, binary } = found;
  const skipped = [
    empty.length ? `Skipped ${empty.length} empty file${empty.length === 1 ? "" : "s"}: ${empty.join(", ")}.` : "",
    binary.length ? `Skipped ${binary.length} non-text file${binary.length === 1 ? "" : "s"}: ${binary.join(", ")}.` : "",
  ].filter(Boolean).join(" ");
  if (!statements.length) {
    showCreateError(`No statements were found directly inside ${folder}.`
      + (skipped ? ` ${skipped}` : "") + " Choose a folder that contains non-empty text files (any extension or none).");
    return;
  }
  // File reading is asynchronous; recheck the current settings before sending.
  if (!validateRetrievalSettings({ folder: true }) || !validateWorkflowHours()) return;
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
    showCreateError(`These statements are too large to send together `
      + `(${(size / 1048576).toFixed(1)} MB; the limit is 8 MB). Split the folder.`);
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
    }
    // The result sits at the top of the batch list (or of the job page that started the run).
    ui.batchStatus.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) {
    showCreateError(error.message);
  }
  if (!currentJob) loadJobs();
}

async function startLatexOnly() {
  // One create request at a time: a second click must not start a second paid job.
  if (ui.check.disabled) return;
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
  const label = ui.check.textContent;
  ui.check.disabled = ui.runFolder.disabled = true;
  ui.check.textContent = "Starting…";
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
    showCreateError(error.message);
  } finally {
    ui.check.disabled = ui.runFolder.disabled = false;
    if (ui.check.textContent === "Starting…") ui.check.textContent = label;
  }
}

async function act(path, body = {}) {
  clearTimeout(timer);
  // A new action replaces the feedback of the previous one.
  show(ui.notice, false);
  const job = currentJob;
  const batch = currentBatch;
  try {
    const next = await request(jobPath(path), body);
    if (job === currentJob && batch === currentBatch) render(next);
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
    // A rejected control (often a race with the run itself) must not end live polling.
    // After navigation the new view polls on its own.
    if (job !== currentJob || batch !== currentBatch) return;
    clearTimeout(timer);
    timer = setTimeout(refresh, 700);
  }
}

// The drawer is a modal <dialog>: the browser traps focus, makes the page inert
// and closes it on Escape; "close" restores the toggle in every case.
function setAudit(open) {
  if (open && !ui.activityPanel.open) {
    ui.activityPanel.showModal();
    ui.activityToggle.setAttribute("aria-expanded", "true");
    ui.activityClose.focus();
  } else if (!open && ui.activityPanel.open) {
    ui.activityPanel.close();
  }
}
ui.activityPanel.addEventListener("close", () => {
  ui.activityToggle.setAttribute("aria-expanded", "false");
  if (!ui.activityToggle.hidden) ui.activityToggle.focus();
});
// A click on the backdrop lands on the <dialog> itself, outside its inner wrapper.
ui.activityPanel.onclick = (event) => {
  if (event.target === ui.activityPanel) setAudit(false);
};

// Filters affect the readable timeline only; Show details stays conversational.
ui.filters.onclick = (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  activeFilter = button.dataset.filter;
  for (const item of ui.filters.querySelectorAll(".filter")) {
    item.classList.toggle("active", item === button);
    item.setAttribute("aria-pressed", String(item === button));
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
  top: ui.timeline.scrollHeight,
  behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
});

ui.activityToggle.onclick = () => setAudit(true);
ui.activityClose.onclick = () => setAudit(false);
ui.drawerScrim.onclick = () => setAudit(false);

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
  ui[`${role}Source`].onchange = () => {
    syncRoleModelMenu(role, ui[`${role}Source`].value);
    syncEffortChoices(role);
    renderRetrievalSettings();
    updateModelSummary();
  };
  ui[`${role}Model`].onchange = () => {
    syncEffortChoices(role);
    renderRetrievalSettings(); // the provider chips and the web cutoff notice follow the model
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
for (const input of ui.problemModes) {
  input.onchange = () => setProblemMode(input.value);
}
ui.editPrompts.onclick = openPromptEditor;
ui.promptTabs.onclick = (event) => {
  const tab = event.target.closest("[data-prompt]");
  if (tab) selectPrompt(tab.dataset.prompt);
};
ui.promptTabs.onkeydown = (event) => {
  const tabs = [...ui.promptTabs.querySelectorAll(".prompt-tab:not([hidden])")];
  const index = tabs.indexOf(document.activeElement);
  const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 }[event.key];
  if (index < 0 || next === undefined) return;
  event.preventDefault();
  const tab = tabs[(next + tabs.length) % tabs.length];
  tab.focus();
  selectPrompt(tab.dataset.prompt);
};
ui.resetPrompt.onclick = async () => {
  try {
    const defaults = await currentPromptDefaults();
    if (!defaults) return;
    promptDrafts[activePrompt] = defaults[activePrompt] || "";
    ui.promptEditor.value = promptDrafts[activePrompt];
    show(ui.promptError, false);
    updatePromptHelp();
  } catch (error) {
    showPromptError(error.message);
  }
};
ui.promptEditor.oninput = () => {
  show(ui.promptError, false);
  updatePromptHelp();
};
ui.savePrompts.onclick = savePrompts;
ui.approve.onclick = () => act("/approve", { statement: ui.proposed.value });
ui.setAuthorTimeLimit.onclick = () => {
  // A blank field must not become 0, which removes the time limit.
  const raw = ui.authorLimitHours.value.trim();
  const hours = raw === "" ? NaN : Number(raw);
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
// In-page links (Skip to content, the web search badges) move focus without
// adding a URL fragment, which would fire popstate and rebuild the job view.
function followInPageLink(event) {
  const link = event.target?.closest?.('a[href^="#"]');
  const target = link && document.getElementById(link.getAttribute("href").slice(1));
  if (!target) return;
  event.preventDefault();
  target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
}
document.addEventListener("click", followInPageLink);
window.onpopstate = () => {
  if (ui.activityPanel.open) ui.activityPanel.close();
  currentJob = new URLSearchParams(location.search).get("job") || "";
  currentBatch = new URLSearchParams(location.search).get("batch") || "";
  composingProblem = new URLSearchParams(location.search).get("new") === "1" && !currentJob;
  clearTimeout(timer); clearInterval(clock); clearTimeout(jobsTimer);
  clearJobView(); show(ui.notice, false);
  refresh();
};

refresh();

// The sticky composer footer (600 px and wider) changes height with the model
// summary; scroll-padding-bottom follows it so keyboard focus is never hidden.
const composerFooter = document.querySelector(".composer-actions");
if (composerFooter && "ResizeObserver" in window) {
  new ResizeObserver(() => document.documentElement.style.setProperty(
    "--composer-footer-h", `${Math.ceil(composerFooter.getBoundingClientRect().height)}px`)).observe(composerFooter);
}

// API models (Advanced settings → API connections): one saved key exposes its model catalog.
// Keys travel only in POST bodies and are never returned by the controller.
const apiUi = {
  group: $("apiModels"), heading: $("apiModelsHeading"), count: $("apiModelCount"),
  status: $("apiModelsStatus"), list: $("apiModelList"), empty: $("apiModelEmpty"),
  form: $("apiModelForm"), formHeading: $("apiModelFormHeading"), formatHelp: $("apiModelFormatHelp"),
  formats: [$("apiModelFormatOpenai"), $("apiModelFormatAnthropic")],
  baseUrl: $("apiModelBaseUrl"), requestSource: $("apiModelRequestSource"), key: $("apiModelKey"),
  keyHelp: $("apiModelKeyHelp"), label: $("apiModelName"), result: $("apiModelTestResult"),
  cancel: $("apiModelCancelButton"), test: $("apiModelTestButton"), save: $("apiModelSaveButton"),
};
const apiFormats = {
  openai: { name: "OpenAI", baseUrl: "https://api.openai.com/v1", key: "sk-…",
    help: "Runs through the Codex CLI with the Responses API. Models load automatically when you save." },
  anthropic: { name: "Anthropic", baseUrl: "https://api.anthropic.com", key: "sk-ant-…",
    help: "Runs through the Claude CLI in bare mode. Models load automatically when you save." },
};
const roleNames = { review: "statement reviewer", author: "proof author", critic: "independent critic", writer: "LaTeX writer" };
let apiModels = [], apiModelEditing = null, apiModelBusy = false;
let apiModelsUnavailable = false, apiModelsLoaded = false;
let apiFormEdits = 0, apiSaveEdits = 0, apiShownTest = null;

async function apiModelRequest(path, body) {
  const options = { headers: { "X-TCS-Prover-Token": sessionToken }, cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (response.status === 404) {
    apiModelsUnavailable = true;
    throw new Error("This TCS Prover server cannot manage API connections. Restart TCS Prover to update it.");
  }
  if (!response.ok) throw new Error(data.error || `The request failed (HTTP ${response.status}).`);
  return data;
}

function apiElement(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function adoptApiModels(data) {
  if (Array.isArray(data.connections || data.models)) apiModels = data.connections || data.models;
  let replaced = [];
  if (Array.isArray(data.model_catalog)) {
    const settings = state.workflow?.settings || {};
    state.workflow = { ...state.workflow, settings: { ...settings, model_catalog: data.model_catalog,
      model_sources: data.model_sources, model_efforts: data.model_efforts || {} } };
    replaced = syncModelMenus();
    for (const role of Object.keys(roleNames)) syncEffortChoices(role);
    renderRetrievalSettings();
    updateModelSummary();
  }
  renderApiModels();
  return replaced;
}

async function loadApiModels() {
  if (apiModelBusy) return;
  setApiBusy(true);
  try {
    adoptApiModels(await apiModelRequest("/api-models"));
    apiModelsLoaded = true;
    showApiStatus("");
  } catch (error) {
    if (!apiModelsLoaded) apiUi.count.textContent = "Unavailable";
    showApiStatus(error.message, true);
  } finally { setApiBusy(false); }
}

function setApiBusy(busy, button = null, label = "") {
  apiModelBusy = busy;
  apiUi.group.setAttribute("aria-busy", String(busy));
  for (const control of [apiUi.test, apiUi.save, apiUi.cancel, ...apiUi.list.querySelectorAll("button")]) {
    control.disabled = busy || apiModelsUnavailable;
  }
  if (button && label) button.textContent = label;
}

function apiFocusLost() { return !document.activeElement || document.activeElement === document.body; }
function restoreApiFocus(target) { if (apiFocusLost()) target?.focus(); }
function showApiStatus(text, warning = false) {
  apiUi.status.className = `api-models-status${warning && text ? " warning" : ""}`;
  apiUi.status.textContent = text;
}
function showApiResult(tone, text, detail = "") {
  apiShownTest = null;
  apiUi.result.className = `api-test-result${tone ? ` ${tone}` : ""}`;
  apiUi.result.replaceChildren(...(text ? [apiElement("strong", "", text)] : []),
    ...(detail ? [apiElement("span", "api-test-detail", detail)] : []));
}
function clearApiResult() { showApiResult("", ""); }
function apiTestText(result) {
  return result.ok ? `Connected${Number.isFinite(result.latencyMs) ? ` · ${Math.round(result.latencyMs)} ms` : ""}`
    : `${result.status && result.status >= 300 ? `${result.status} · ` : ""}${result.message || "Connection failed."}`;
}
function noteApiFormEdit() {
  apiFormEdits += 1;
  apiSaveEdits += 1;
  if (apiShownTest) showApiResult("stale", "Connection details changed", "Test this connection again.");
}
function apiTestTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
}

function renderApiModels() {
  apiUi.count.textContent = apiModels.length ? `${apiModels.length} saved` : "None saved";
  show(apiUi.list, apiModels.length > 0);
  show(apiUi.empty, apiModels.length === 0);
  apiUi.list.replaceChildren(...apiModels.map(apiModelRow));
  setApiBusy(apiModelBusy);
}
function apiModelRow(connection) {
  const item = apiElement("li", "api-model-row");
  if (apiModelEditing?.id === connection.id) item.classList.add("editing");
  const glyph = apiElement("span", `api-model-glyph ${connection.format}`, connection.format === "anthropic" ? "AN" : "OA");
  glyph.setAttribute("aria-hidden", "true");
  const copy = apiElement("div", "api-model-copy");
  const models = connection.models || [];
  const meta = [connection.host, models.length ? `${models.length} model${models.length === 1 ? "" : "s"}` : "Load models to use this key",
    connection.hasKey ? `key ${connection.keyHint || "saved"}` : "no key saved"];
  copy.append(apiElement("strong", "", connection.label), apiElement("span", "api-model-meta", meta.join(" · ")));
  const format = apiElement("span", `format-chip ${connection.format}`, apiFormats[connection.format]?.name || connection.format);
  const test = connection.lastTest;
  const status = apiElement("span", `status-chip ${!test ? "idle" : test.ok ? "ok" : "error"}`,
    !test ? "Not tested" : test.ok ? "Connected" : "Connection failed");
  const actions = apiElement("div", "api-model-actions");
  for (const [action, label] of [["edit", "Edit"], ["refresh", "Load models"], ["remove", "Remove"]]) {
    const button = apiElement("button", `ghost compact api-model-${action}`, label);
    button.type = "button";
    button.dataset.apiAction = action;
    button.dataset.apiModel = connection.id;
    button.setAttribute("aria-label", `${label} ${connection.label}`);
    actions.append(button);
  }
  item.append(glyph, copy, format, status, actions);
  if (test && !test.ok) item.append(apiElement("p", "api-test-result error", apiTestText(test)));
  return item;
}

function selectedApiFormat() { return apiUi.formats.find((input) => input.checked)?.value || "openai"; }
function apiKeyRequired() {
  return !apiModelEditing || selectedApiFormat() !== apiModelEditing.format
    || apiUi.baseUrl.value.trim().replace(/\/+$/, "") !== apiModelEditing.baseUrl;
}
function renderApiForm() {
  const format = apiFormats[selectedApiFormat()], editing = apiModelEditing, required = apiKeyRequired();
  apiUi.formHeading.textContent = editing ? `Edit ${editing.label}` : "Add an API connection";
  apiUi.form.classList.toggle("editing", Boolean(editing));
  if (!apiModelBusy) {
    apiUi.test.textContent = "Test connection";
    apiUi.save.textContent = editing ? "Save changes" : "Save connection";
  }
  show(apiUi.cancel, Boolean(editing));
  apiUi.formatHelp.textContent = format.help;
  apiUi.baseUrl.placeholder = format.baseUrl;
  apiUi.key.required = required;
  apiUi.key.placeholder = !editing ? format.key : required ? "Enter the key again" : `Saved key ${editing.keyHint || ""}`.trim();
  apiUi.keyHelp.textContent = !editing ? "Stored only on this computer and never shown again."
    : required ? "Enter the API key again: a saved key is sent only to the format and base URL it was saved with."
    : "Leave blank to keep the saved key; a new key replaces it.";
}
function resetApiForm() {
  apiFormEdits += 1;
  apiSaveEdits += 1;
  apiModelEditing = null;
  for (const field of [apiUi.baseUrl, apiUi.key, apiUi.requestSource, apiUi.label]) field.value = "";
  renderApiForm();
}
function apiFormValues() {
  const values = { format: selectedApiFormat(), baseUrl: apiUi.baseUrl.value.trim(), apiKey: apiUi.key.value.trim(),
    requestSource: apiUi.requestSource.value.trim(), label: apiUi.label.value.trim() };
  const missing = !values.baseUrl ? [apiUi.baseUrl, "Enter the base URL."]
    : !values.apiKey && apiKeyRequired() ? [apiUi.key, apiModelEditing ? "Enter the API key again for the new connection." : "Enter the API key."] : null;
  if (missing) {
    showApiResult("error", "", missing[1]); missing[0].focus(); return null;
  }
  if (apiModelEditing) values.id = apiModelEditing.id;
  return values;
}

async function testApiForm() {
  if (apiModelBusy) return;
  const values = apiFormValues();
  if (!values) return;
  const edits = apiFormEdits;
  setApiBusy(true, apiUi.test, "Testing…");
  showApiResult("pending", "Checking connection…", "Loading the models available to this key.");
  let result, failure = "";
  try {
    result = await apiModelRequest("/api-models/test", values);
    if (result.connection) apiModels = apiModels.map((item) => item.id === result.connection.id ? result.connection : item);
  }
  catch (error) { failure = error.message; }
  setApiBusy(false); renderApiForm(); renderApiModels(); restoreApiFocus(apiUi.test);
  if (edits !== apiFormEdits) return showApiResult("stale", "Connection details changed", "Test this connection again.");
  showApiResult(result?.ok ? "ok" : "error", result ? apiTestText(result) : "Connection failed",
    result?.ok ? result.message : failure);
  apiShownTest = { ok: result?.ok };
}
async function saveApiModel() {
  if (apiModelBusy) return;
  const values = apiFormValues();
  if (!values) return;
  const editing = Boolean(apiModelEditing), edits = apiSaveEdits;
  setApiBusy(true, apiUi.save, "Loading models and saving…");
  showApiResult("pending", "Loading models and saving…");
  let saved, failure = "";
  try {
    const data = await apiModelRequest("/api-models", values);
    saved = data.connection || data.model;
    if (edits === apiSaveEdits) resetApiForm();
    adoptApiModels(data);
  } catch (error) { failure = error.message; }
  setApiBusy(false); renderApiForm(); restoreApiFocus(apiUi.save);
  showApiResult(saved ? "ok" : "error", saved ? `${editing ? "Updated" : "Saved"} ${saved.label}.` : "Not saved",
    saved ? "Choose this key under Source in any role, then choose a model." : failure);
}
async function refreshApiConnection(connection, button) {
  if (apiModelBusy) return;
  setApiBusy(true, button, "Loading…");
  let message = "", warning = false;
  try {
    const result = await apiModelRequest("/api-models/discover", { id: connection.id });
    const replaced = result.model_catalog || result.connections ? adoptApiModels(result) : [];
    warning = !result.ok;
    message = `${connection.label}: ${result.message || apiTestText(result)}`;
    if (replaced.length) message += ` ${replaced.map((role) => roleNames[role]).join(", ")} now use the default model because their selected model is no longer available.`;
  } catch (error) { message = error.message; warning = true; }
  setApiBusy(false); renderApiModels();
  if (apiFocusLost()) [...apiUi.list.querySelectorAll("button")].find((item) => item.dataset.apiModel === connection.id
    && item.dataset.apiAction === "refresh")?.focus();
  showApiStatus(message, warning);
}
async function removeApiModel(connection, button) {
  if (apiModelBusy || !confirm(`Remove ${connection.label}?\n\nIts saved key and model choices will be removed. Jobs using this connection will need it restored to continue.`)) return;
  setApiBusy(true, button, "Removing…");
  let removed = false, message = "", warning = false;
  try {
    const data = await apiModelRequest("/api-models/delete", { id: connection.id });
    if (apiModelEditing?.id === connection.id) resetApiForm();
    const replaced = adoptApiModels(data); removed = true;
    message = [`Removed ${connection.label}.`, data.warning].filter(Boolean).join(" ");
    if (replaced.length) message += ` ${replaced.map((role) => roleNames[role]).join(", ")} now use the default model.`;
    warning = Boolean(data.warning);
  } catch (error) { message = error.message; warning = true; }
  setApiBusy(false); renderApiForm(); renderApiModels();
  if (removed) apiUi.heading.focus();
  showApiStatus(message, warning);
}
function editApiModel(connection) {
  apiFormEdits += 1;
  apiSaveEdits += 1;
  apiModelEditing = connection;
  for (const input of apiUi.formats) input.checked = input.value === connection.format;
  apiUi.baseUrl.value = connection.baseUrl;
  apiUi.key.value = "";
  apiUi.requestSource.value = connection.requestSource || "";
  apiUi.label.value = connection.label === connection.host ? "" : connection.label;
  clearApiResult(); renderApiForm(); renderApiModels(); apiUi.baseUrl.focus();
}
for (const input of apiUi.formats) input.onchange = () => { noteApiFormEdit(); renderApiForm(); };
apiUi.baseUrl.oninput = () => { noteApiFormEdit(); renderApiForm(); };
apiUi.key.oninput = apiUi.requestSource.oninput = noteApiFormEdit;
apiUi.label.oninput = () => { apiSaveEdits += 1; };
apiUi.test.onclick = testApiForm;
apiUi.save.onclick = saveApiModel;
apiUi.cancel.onclick = () => { resetApiForm(); clearApiResult(); renderApiModels(); apiUi.baseUrl.focus(); };
apiUi.list.onclick = (event) => {
  const button = event.target.closest("button[data-api-action]");
  const connection = apiModels.find((item) => item.id === button?.dataset.apiModel);
  if (!connection || button.disabled) return;
  ({ edit: editApiModel, refresh: refreshApiConnection, remove: removeApiModel })[button.dataset.apiAction](connection, button);
};
ui.input.querySelector("details.advanced")?.addEventListener("toggle", (event) => {
  if (event.target.open) loadApiModels();
});
renderApiForm();

// Appearance: the dark theme or the light one (the mode: Light, Dark, or System, which follows
// the OS setting) in one of four palettes (the theme: Blue, Brown, Crimson or Ink). The header's
// theme menu shows both choices as radio groups; `appearance`, at the top of this file, applies
// and keeps them. The arrow keys move a group's choice; Escape or a click outside closes the menu.
(() => {
  const button = document.getElementById("themeToggle");
  const menu = document.getElementById("themeMenu");
  if (!button || !menu) return;
  const words = { blue: "Blue", brown: "Brown", crimson: "Crimson", ink: "Ink",
    light: "Light", dark: "Dark", system: "System" };
  const groups = [
    { kind: "theme", items: [...menu.querySelectorAll("[data-theme-choice]")], value: (item) => item.dataset.themeChoice },
    { kind: "mode", items: [...menu.querySelectorAll("[data-mode-choice]")], value: (item) => item.dataset.modeChoice },
  ];
  function showChoices() {
    const choice = appearance.current();
    const shown = appearance.resolve(choice.mode);
    const mode = choice.mode === "system" ? `System (${words[shown]})` : words[choice.mode];
    const label = `Appearance: ${words[choice.theme]}, ${mode}`;
    button.dataset.appearanceMode = choice.mode;
    button.setAttribute("aria-label", label);
    button.title = label;
    for (const group of groups) {
      for (const item of group.items) {
        const on = group.value(item) === choice[group.kind];
        item.setAttribute("aria-checked", String(on));
        item.tabIndex = on ? 0 : -1;
      }
    }
  }
  appearance.onChange(showChoices);
  showChoices();
  function setOpen(open, refocus = false) {
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (open) (groups[0].items.find((item) => item.getAttribute("aria-checked") === "true") || groups[0].items[0])?.focus();
    else if (refocus) button.focus();
  }
  const groupOf = (item) => groups.find((group) => group.items.includes(item));
  button.onclick = () => setOpen(menu.hidden);
  button.onkeydown = (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      event.preventDefault();
      setOpen(false);
    }
  };
  menu.onclick = (event) => {
    const item = event.target.closest?.("[data-theme-choice], [data-mode-choice]");
    const group = item && groupOf(item);
    if (!group) return;
    appearance.choose(group.kind, group.value(item));
    item.focus();
  };
  menu.onkeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false, true);
      return;
    }
    const group = groupOf(document.activeElement);
    const move = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!group || !(move || event.key === "Home" || event.key === "End")) return;
    event.preventDefault();
    const list = group.items;
    const index = list.indexOf(document.activeElement);
    const next = list[event.key === "Home" ? 0 : event.key === "End" ? list.length - 1
      : (index + move + list.length) % list.length];
    appearance.choose(group.kind, group.value(next));
    next.focus();
  };
  // Tabbing past the last choice closes the menu (focus has left it for the page).
  menu.addEventListener("focusout", (event) => {
    const next = event.relatedTarget;
    if (!menu.hidden && next && !menu.contains(next) && next !== button) setOpen(false);
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !event.target.closest?.(".theme-menu")) setOpen(false);
  });
})();

// The workbench shell around the pages: the sidebar (navigation, every batch with its live
// counts, an icon rail on wide screens, a drawer on phones), the header's location, the overview
// totals, the batch status filter and the Models & API view. It only reads what the pages already
// know (the batch catalog, the route, the job on screen) and never changes how a job runs.
(() => {
  const root = document.documentElement;
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;
  const shell = {
    toggle: document.getElementById("sidebarToggle"),
    close: document.getElementById("sidebarClose"),
    scrim: document.getElementById("sidebarScrim"),
    main: document.querySelector(".main-column"),
    batches: document.getElementById("navBatches"),
    batchTotal: document.getElementById("navBatchTotal"),
    live: document.getElementById("navLiveCount"),
    foot: document.getElementById("navFootStatus"),
    tip: document.getElementById("navTooltip"),
    title: document.getElementById("topbarTitle"),
    overviewMeta: document.getElementById("overviewMeta"),
    overviewStats: document.getElementById("overviewStats"),
    modelsMeta: document.getElementById("modelsMeta"),
    filter: document.getElementById("jobsFilter"),
    filterEmpty: document.querySelector(".jobs-filter-empty"),
    advanced: ui.input.querySelector("details.advanced"),
  };
  const phone = window.matchMedia("(max-width: 860px)");
  const svgNS = "http://www.w3.org/2000/svg";
  // The running count beside Overview is read out in words, not as a bare number.
  const liveWords = document.createElement("span");
  liveWords.className = "visually-hidden";
  shell.live.setAttribute("aria-hidden", "true");
  shell.live.after(liveWords);

  // Rail mode (wide screens) is remembered in this browser; the drawer (phones) never is.
  let rail = false;
  let drawer = false;
  try { rail = localStorage.getItem("tcs-prover-sidebar") === "rail"; } catch (_) {}
  root.dataset.sidebar = rail ? "rail" : "full";
  root.dataset.drawer = "closed";
  sidebar.tabIndex = -1;

  function syncToggle() {
    const mobile = phone.matches;
    shell.toggle.setAttribute("aria-expanded", String(mobile ? drawer : !rail));
    const label = mobile ? (drawer ? "Close navigation" : "Open navigation")
      : rail ? "Expand sidebar" : "Collapse sidebar";
    shell.toggle.setAttribute("aria-label", label);
    shell.toggle.title = mobile ? label : `${label} (${/Mac|iP/.test(navigator.platform) ? "⌘" : "Ctrl+"}B)`;
  }
  function setRail(value) {
    rail = value;
    root.dataset.sidebar = rail ? "rail" : "full";
    try { localStorage.setItem("tcs-prover-sidebar", rail ? "rail" : "full"); } catch (_) {}
    hideTip();
    syncToggle();
  }
  function setDrawer(open, refocus = true) {
    drawer = open && phone.matches;
    root.dataset.drawer = drawer ? "open" : "closed";
    shell.scrim.hidden = !drawer;
    if (shell.main) shell.main.inert = drawer;
    // Focus moves into the drawer itself (no ring); Tab then walks its links.
    if (drawer) sidebar.focus({ preventScroll: true });
    else if (refocus) shell.toggle.focus();
    syncToggle();
  }
  shell.toggle.onclick = () => (phone.matches ? setDrawer(!drawer) : setRail(!rail));
  shell.close.onclick = () => setDrawer(false);
  shell.scrim.onclick = () => setDrawer(false);
  phone.addEventListener?.("change", () => {
    if (!phone.matches && drawer) setDrawer(false, false);
    syncToggle();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer) {
      setDrawer(false);
      return;
    }
    const target = event.target;
    const typing = target?.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target?.tagName);
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && String(event.key || "").toLowerCase() === "b"
        && !typing && !phone.matches && !document.querySelector("dialog[open]")) {
      event.preventDefault();
      setRail(!rail);
    }
  });

  // Rail tooltips: the label and a short description beside an icon (the label stays the
  // accessible name, so the tooltip itself is decorative).
  const tipTitle = shell.tip.querySelector("strong");
  const tipMeta = shell.tip.querySelector("span");
  shell.tip.setAttribute("aria-hidden", "true");
  function showTip(item) {
    if (phone.matches || !item) return;
    // In the full sidebar only a batch whose name is cut short gets the tooltip (with its counts).
    const name = item.querySelector(".nav-text");
    if (!rail && !(item.classList.contains("nav-batch") && name && name.scrollWidth > name.clientWidth + 1)) {
      hideTip();
      return;
    }
    tipTitle.textContent = item.dataset.tip || item.textContent.trim();
    tipMeta.textContent = item.dataset.tipMeta || "";
    tipMeta.hidden = !tipMeta.textContent;
    const box = item.getBoundingClientRect();
    shell.tip.style.top = `${Math.round(box.top + box.height / 2)}px`;
    shell.tip.hidden = false;
  }
  function hideTip() { shell.tip.hidden = true; }
  sidebar.addEventListener("pointerover", (event) => showTip(event.target.closest?.(".nav-item, .brand")));
  sidebar.addEventListener("pointerout", (event) => {
    if (!event.relatedTarget?.closest?.(".nav-item, .brand")) hideTip();
  });
  sidebar.addEventListener("focusin", (event) => showTip(event.target.closest?.(".nav-item, .brand")));
  sidebar.addEventListener("focusout", hideTip);
  sidebar.addEventListener("scroll", hideTip, true);

  // Where the page is: home, a batch, a job, the composer, or Models & API (the composer's
  // settings on their own, at ?new=1#models).
  function route() {
    if (currentJob) return "job";
    if (composingProblem) return location.hash === "#models" ? "models" : "new";
    return currentBatch ? "batch" : "home";
  }

  let advancedWasOpen = null;
  function applyView() {
    const models = route() === "models";
    if (models === (root.dataset.view === "models")) return;
    if (models) {
      root.dataset.view = "models";
      advancedWasOpen = shell.advanced.open;
      shell.advanced.open = true; // its toggle event reads the API model list
      ui.input.setAttribute("aria-labelledby", "modelsHeading");
    } else {
      delete root.dataset.view;
      if (advancedWasOpen !== null) shell.advanced.open = advancedWasOpen;
      advancedWasOpen = null;
      ui.input.setAttribute("aria-labelledby", "composerHeading");
    }
  }

  async function openComposer(models) {
    const url = `${location.pathname}?new=1${models ? "#models" : ""}`;
    if (composingProblem && !currentJob) {
      // Already composing: switch the view only, so the draft and its settings stay as they are.
      if (route() !== (models ? "models" : "new")) history.pushState(null, "", url);
      sync();
      return;
    }
    const loading = newProblem();
    if (models) history.replaceState(null, "", url);
    sync();
    await loading;
    sync();
  }

  async function go(item) {
    const target = item.dataset.nav;
    if (target === "home") await selectBatch("");
    else if (target === "batch") await selectBatch(item.dataset.batchId);
    else if (target === "new" || target === "models") await openComposer(target === "models");
    else if (target === "prompts") {
      // Prompt edits apply to the next problem, so they are made from the composer.
      if (!(composingProblem && !currentJob)) await openComposer(false);
      await openPromptEditor();
    }
    sync();
  }
  sidebar.addEventListener("click", (event) => {
    if (event.target.closest?.("#homeLink")) {
      if (drawer) setDrawer(false, false);
      setTimeout(sync, 0);
      return;
    }
    const item = event.target.closest?.(".nav-item");
    if (!item) return;
    const plain = !(event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
    if (item.tagName === "A" && !plain) return; // a new tab or window follows the link itself
    event.preventDefault();
    hideTip();
    const wasDrawer = drawer;
    if (drawer) setDrawer(false, false);
    go(item).finally(() => {
      // The drawer closed under the focused link: continue from the page instead.
      if (wasDrawer && item.dataset.nav !== "prompts") document.getElementById("main")?.focus({ preventScroll: true });
    });
  });

  // Batch counts: the pages read /batches on the home and batch views; elsewhere the sidebar
  // reads it itself every few seconds (outside the job request queue, like the API model calls).
  let polled = null;
  let polledAt = 0;
  let polling = false;
  async function pollBatches() {
    if (polling) return;
    polling = true;
    try {
      const response = await fetch("/batches", { headers: { "X-TCS-Prover-Token": sessionToken }, cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (response.ok && Array.isArray(data.batches)) polled = data.batches;
    } catch (_) {
      // Keep the last counts; the page's own notice reports a lost connection.
    } finally {
      polledAt = Date.now();
      polling = false;
    }
    sync();
  }
  function catalog() {
    return !currentJob && !composingProblem && batchCatalog.length ? batchCatalog : polled || batchCatalog;
  }
  function tally(batch) {
    const counts = batch.counts || {};
    const count = (key) => Number(counts[key] || 0);
    return {
      running: count("running") + count("reviewing") + count("pausing") + count("stopping"),
      waiting: count("reviewed") + count("prepared"),
      paused: count("paused"),
      done: count("done"),
      total: Number(batch.jobCount || 0),
    };
  }
  const toneWords = { running: "running", waiting: "need you", paused: "paused", done: "finished" };
  const toneText = (key, count) => `${count} ${key === "waiting" && count === 1 ? "needs you" : toneWords[key]}`;

  function setText(node, text) {
    if (node.textContent !== text) node.textContent = text;
  }
  function element(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }
  function folderIcon() {
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("class", "icon nav-icon");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", "M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3.5H3a1 1 0 0 0-1 1Z");
    svg.append(path);
    return svg;
  }

  let batchSignature = "";
  function renderNavBatches(batches) {
    const signature = JSON.stringify(batches.map((batch) => [batch.id, batch.label, batch.current, batch.readOnly,
      batch.jobCount, batch.counts]));
    if (signature === batchSignature) return;
    batchSignature = signature;
    shell.batchTotal.textContent = batches.length ? String(batches.length) : "";
    if (!batches.length) {
      shell.batches.replaceChildren(element("li", "nav-empty", "No batches yet"));
      return;
    }
    shell.batches.replaceChildren(...batches.map((batch) => {
      const counts = tally(batch);
      const item = element("li");
      const link = element("a", `nav-item nav-batch${batch.current ? " current" : ""}${batch.readOnly ? " read-only" : ""}`);
      link.href = batchUrl(batch.id);
      link.dataset.nav = "batch";
      link.dataset.batchId = batch.id;
      const label = batch.label || batch.id;
      const parts = Object.keys(toneWords).filter((key) => counts[key]).map((key) => toneText(key, counts[key]));
      const problems = `${counts.total} ${counts.total === 1 ? "problem" : "problems"}`;
      link.dataset.tip = label;
      link.dataset.tipMeta = [batch.current ? "Current batch" : batch.readOnly ? "Read-only" : "", problems, ...parts]
        .filter(Boolean).join(" · ");
      const icon = element("span", "nav-batch-icon");
      icon.append(folderIcon());
      if (counts.running) icon.append(element("span", "nav-pulse"));
      const name = element("span", "nav-text", label);
      const line = element("span", "nav-counts");
      line.setAttribute("aria-hidden", "true");
      line.append(element("span", "nc-kind", batch.current ? "current" : batch.readOnly ? "read-only" : "saved"));
      for (const key of Object.keys(toneWords)) {
        if (!counts[key]) continue;
        const stat = element("span", `nc ${key}`);
        stat.append(element("span", "nc-glyph"), document.createTextNode(String(counts[key])));
        line.append(stat);
      }
      const badge = element("span", "nav-badge", String(counts.total));
      badge.setAttribute("aria-hidden", "true");
      const meter = element("span", "nav-meter");
      meter.setAttribute("aria-hidden", "true");
      for (const key of Object.keys(toneWords)) {
        if (!counts[key]) continue;
        const segment = element("i", key);
        segment.style.flexGrow = String(counts[key]);
        meter.append(segment);
      }
      const other = counts.total - counts.running - counts.waiting - counts.paused - counts.done;
      if (other > 0) {
        const segment = element("i", "other");
        segment.style.flexGrow = String(other);
        meter.append(segment);
      }
      const spoken = element("span", "visually-hidden",
        `, ${[batch.current ? "current batch" : batch.readOnly ? "read-only" : "", problems, ...parts].filter(Boolean).join(", ")}`);
      link.append(icon, name, badge, line, meter, spoken);
      item.append(link);
      return item;
    }));
  }

  function renderTotals(batches) {
    const sum = { running: 0, waiting: 0, paused: 0, done: 0, total: 0 };
    for (const batch of batches) {
      const counts = tally(batch);
      for (const key of Object.keys(sum)) sum[key] += counts[key];
    }
    shell.live.hidden = !sum.running;
    setText(shell.live, sum.running ? String(sum.running) : "");
    setText(liveWords, sum.running ? `, ${sum.running} running` : "");
    setText(shell.foot, sum.running || sum.waiting
      ? [sum.running ? toneText("running", sum.running) : "", sum.waiting ? toneText("waiting", sum.waiting) : ""]
        .filter(Boolean).join(" · ")
      : "Local workbench · idle");
    shell.foot.dataset.state = sum.running ? "running" : sum.waiting ? "waiting" : "idle";
    // Overview: the meta line and the four totals.
    const meta = [element("span", "", `${batches.length} ${batches.length === 1 ? "batch" : "batches"}`),
      element("span", "", `${sum.total} ${sum.total === 1 ? "problem" : "problems"}`)];
    for (const key of Object.keys(toneWords)) {
      if (sum[key]) meta.push(element("span", `meta-${key}`, toneText(key, sum[key])));
    }
    const metaKey = meta.map((part) => part.textContent).join("|");
    if (shell.overviewMeta.dataset.key !== metaKey) {
      shell.overviewMeta.dataset.key = metaKey;
      shell.overviewMeta.replaceChildren(...meta);
    }
    for (const key of ["running", "waiting", "paused", "done"]) {
      const value = shell.overviewStats.querySelector(`[data-stat="${key}"]`);
      if (!value) continue;
      setText(value, String(sum[key]));
      value.closest(".stat")?.classList.toggle("zero", !sum[key]);
    }
  }

  function renderActive(where) {
    for (const item of sidebar.querySelectorAll(".nav-item")) {
      const target = item.dataset.nav;
      const current = target === "batch"
        ? (["batch", "job"].includes(where) && item.dataset.batchId === currentBatch ? (where === "job" ? "true" : "page") : "")
        : target === where ? "page" : "";
      if ((item.getAttribute("aria-current") || "") === current) continue;
      if (current) item.setAttribute("aria-current", current);
      else item.removeAttribute("aria-current");
    }
  }

  function renderTitle(where) {
    const words = { home: "Overview", new: "New problem", models: "Models & API", batch: "", job: "" };
    const text = where === "job" ? (ui.runProblemTitle.textContent || "Problem") : words[where];
    setText(shell.title, text);
    shell.title.hidden = !text;
    if (shell.title.dataset.route !== where) shell.title.dataset.route = where;
  }

  // Batch page: show one status at a time; the counts follow the rows app.js renders.
  const filterTones = { running: ["running"], waiting: ["waiting"], paused: ["paused"], done: ["done"], failed: ["failed", "stopped"] };
  let filterBatch = null;
  function renderFilter() {
    if (filterBatch !== currentBatch) {
      filterBatch = currentBatch;
      ui.jobsList.dataset.show = "all";
    }
    const show = ui.jobsList.dataset.show || "all";
    const rows = [...ui.jobsList.children];
    // While the batch's rows load (the list is empty and not yet rendered), the counts stay blank.
    const loading = !rows.length && !ui.jobsList.dataset.signature;
    const count = (key) => key === "all" ? rows.length
      : rows.filter((row) => filterTones[key].some((tone) => row.classList.contains(`tone-${tone}`))).length;
    for (const button of shell.filter.querySelectorAll("[data-show]")) {
      const key = button.dataset.show;
      button.setAttribute("aria-pressed", String(key === show));
      const label = button.querySelector(".seg-count");
      const value = loading ? "" : String(count(key));
      if (label && label.textContent !== value) label.textContent = value;
      button.classList.toggle("empty", key !== "all" && value === "0");
    }
    shell.filterEmpty.hidden = loading || !(show !== "all" && rows.length && !count(show));
  }
  shell.filter.onclick = (event) => {
    const button = event.target.closest?.("[data-show]");
    if (!button) return;
    ui.jobsList.dataset.show = button.dataset.show;
    renderFilter();
  };
  // The counts change with the rows themselves: renderJobs rebuilds the list (and signs it) or a
  // deleted row leaves it, and the filter is redrawn before the next paint, not on sync's next tick.
  new MutationObserver(() => {
    if (route() === "batch") renderFilter();
  }).observe(ui.jobsList, { childList: true, attributes: true, attributeFilter: ["data-signature"] });

  function renderModelsMeta() {
    if (route() !== "models") return;
    const catalog = roleModelCatalog();
    const size = (provider) => catalog.filter((model) => model.provider === provider).length;
    const parts = [`${size("codex")} Codex CLI`, `${size("claude")} Claude CLI`,
      apiModelsLoaded ? `${apiModels.length} API ${apiModels.length === 1 ? "key" : "keys"}` : "API keys"];
    const text = parts.join(" · ");
    setText(shell.modelsMeta, text);
  }

  let lastRoute = "";
  function sync() {
    const where = route();
    applyView();
    if (where !== lastRoute) {
      // A new page: the sidebar counts are read again at once outside home and batch pages.
      if (["job", "new", "models"].includes(where) && lastRoute !== "") polledAt = 0;
      lastRoute = where;
    }
    if (["job", "new", "models"].includes(where) && Date.now() - polledAt > 5000 && !document.hidden) pollBatches();
    // Until the first catalog arrives, the placeholders stay (an empty list would claim no batches).
    if (polled !== null || batchCatalog.length) {
      const batches = catalog();
      renderNavBatches(batches);
      renderTotals(batches);
    }
    renderActive(where);
    renderTitle(where);
    if (where === "batch") renderFilter();
    renderModelsMeta();
  }

  // The shortcut hint names this platform's key.
  const key = sidebar.querySelector(".foot-key");
  if (key && !/Mac|iP/.test(navigator.platform)) key.textContent = "Ctrl+B";

  syncToggle();
  pollBatches();
  sync();
  setInterval(sync, 1000);
  window.addEventListener("popstate", () => setTimeout(sync, 0));
  document.addEventListener("click", () => setTimeout(sync, 0));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });
  // Transitions start after the first layout, so a remembered rail does not animate in.
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("shell-ready")));
})();

// Reduced motion: three page calls ask for a smooth scroll (the create error notice, a folder
// run's result and a research file opened from the approach graph). An explicit behavior wins
// over the stylesheet's preference, so on these elements a smooth scroll jumps instead.
(() => {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  for (const node of [ui.notice, ui.batchStatus, ui.memoryDocument]) {
    const scroll = node?.scrollIntoView;
    if (typeof scroll !== "function") continue;
    node.scrollIntoView = (options) => scroll.call(node, reduce.matches && options?.behavior === "smooth"
      ? { ...options, behavior: "auto" } : options);
  }
})();
