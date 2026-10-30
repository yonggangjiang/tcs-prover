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
  criticCount: $("criticCount"),
  authorSubagents: $("authorSubagents"),
  thinkingHours: $("thinkingHours"),
  speedMode: $("speedMode"),
  reasoningSummary: $("reasoningSummary"),
  retrievalSetting: $("retrievalSetting"), retrievalMode: $("retrievalMode"),
  retrievalCutoffSetting: $("retrievalCutoffSetting"), retrievalCutoffUtc: $("retrievalCutoffUtc"),
  retrievalSettingHelp: $("retrievalSettingHelp"), retrievalConfigurationNotice: $("retrievalConfigurationNotice"),
  workflowReview: $("workflowReview"), workflowAuthor: $("workflowAuthor"),
  workflowCritic: $("workflowCritic"), workflowWriting: $("workflowWriting"),
  workflowStageHelp: $("workflowStageHelp"), problemLabel: $("problemLabel"),
  speedModeSetting: $("speedModeSetting"),
  criticRoundSetting: $("criticRoundSetting"),
  criticCountSetting: $("criticCountSetting"),
  thinkingHoursSetting: $("thinkingHoursSetting"),
  editPrompts: $("editPromptsButton"), promptDialog: $("promptDialog"),
  promptTabs: $("promptTabs"), promptEditor: $("promptEditor"),
  promptEditorLabel: $("promptEditorLabel"),
  promptEditorHelp: $("promptEditorHelp"), promptError: $("promptEditorError"),
  resetPrompt: $("resetPromptButton"), savePrompts: $("savePromptsButton"),
  reviewPromptTab: $("reviewPromptTab"),
  authorPromptTab: $("authorPromptTab"),
  criticPromptTab: $("criticPromptTab"),
  conventionPromptTab: $("conventionPromptTab"),
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
  attentionBanner: $("attentionBanner"), attentionList: $("attentionList"),
  stop: $("stopButton"),
  pause: $("pauseButton"), resume: $("resumeButton"), downloadTex: $("downloadTexButton"),
  downloadPdf: $("downloadPdfButton"), downloadProof: $("downloadProofButton"),
  downloadSource: $("downloadSourceButton"),
  authorTimeLimitControl: $("authorTimeLimitControl"),
  authorLimitSummary: $("authorLimitSummary"),
  authorLimitHours: $("authorLimitHours"),
  setAuthorTimeLimit: $("setAuthorTimeLimitButton"),
  authorLimitHint: $("authorLimitHint"),
  authorSteerControl: $("authorSteerControl"),
  authorSteerLabel: $("authorSteerLabel"),
  authorSteerInstruction: $("authorSteerInstruction"),
  authorSteerCount: $("authorSteerCount"),
  sendAuthorSteer: $("sendAuthorSteerButton"), sendAuthorSteerText: $("sendAuthorSteerText"),
  requestSummary: $("requestSummaryButton"), stopReply: $("stopReplyButton"),
  authorQuestion: $("authorQuestion"), authorQuestionMeta: $("authorQuestionMeta"),
  authorQuestionList: $("authorQuestionList"), authorQuestionSkipHint: $("authorQuestionSkipHint"),
  sendAuthorAnswer: $("sendAuthorAnswerButton"),
  dismissAuthorQuestion: $("dismissAuthorQuestionButton"),
  authorConversation: $("authorConversation"), authorConversationHeading: $("authorConversationHeading"),
  authorConversationNote: $("authorConversationNote"), authorConversationList: $("authorConversationList"),
  conversationSummaryLink: $("conversationSummaryLink"),
  relatedWorksPanel: $("relatedWorksPanel"), relatedWorksHeading: $("relatedWorksHeading"),
  relatedWorksNote: $("relatedWorksNote"), relatedWorksList: $("relatedWorksList"),
  relatedWorksEmpty: $("relatedWorksEmpty"), relatedWorksActions: $("relatedWorksActions"),
  addRelatedFiles: $("addRelatedFilesButton"), addRelatedNote: $("addRelatedNoteButton"),
  relatedNoteForm: $("relatedNoteForm"), relatedNoteTitle: $("relatedNoteTitle"), relatedNoteText: $("relatedNoteText"),
  saveRelatedNote: $("saveRelatedNoteButton"), cancelRelatedNote: $("cancelRelatedNoteButton"),
  relatedWorksStatus: $("relatedWorksStatus"), relatedFilesInput: $("relatedFilesInput"),
  relatedWorksField: $("relatedWorksField"), relatedWorksLabel: $("relatedWorksLabel"),
  composerRelatedList: $("composerRelatedList"), composerAddFiles: $("composerAddFilesButton"),
  composerAddNote: $("composerAddNoteButton"), composerNoteForm: $("composerNoteForm"),
  composerNoteTitle: $("composerNoteTitle"), composerNoteText: $("composerNoteText"),
  composerSaveNote: $("composerSaveNoteButton"), composerCancelNote: $("composerCancelNoteButton"),
  relatedWorksHelp: $("relatedWorksHelp"), composerRelatedFiles: $("composerRelatedFiles"),
  batchRelatedWorks: $("batchRelatedWorks"), batchRelatedWorksHeading: $("batchRelatedWorksHeading"),
  batchRelatedWorksHelp: $("batchRelatedWorksHelp"), batchRelatedGroups: $("batchRelatedGroups"),
  batchNoteForm: $("batchNoteForm"), batchNoteTarget: $("batchNoteTarget"), batchNoteTitle: $("batchNoteTitle"),
  batchNoteText: $("batchNoteText"), batchSaveNote: $("batchSaveNoteButton"),
  batchCancelNote: $("batchCancelNoteButton"), batchRelatedStatus: $("batchRelatedStatus"),
  batchRelatedFiles: $("batchRelatedFiles"),
  runLabel: $("runLabel"), runTitle: $("runTitle"),
  runDescription: $("runDescription"), roundBadge: $("roundBadge"),
  runOverview: $("runOverview"), runProblemTitle: $("runProblemTitle"),
  runStage: $("runStage"), runActiveTime: $("runActiveTime"), runBudget: $("runBudget"),
  runHealth: $("runHealth"), runNextStep: $("runNextStep"), runLatestAction: $("runLatestAction"),
  runSettingsWarning: $("runSettingsWarning"), runAttention: $("runAttention"),
  runTimeLimitNote: $("runTimeLimitNote"), runActivityAge: $("runActivityAge"),
  conventionWarning: $("conventionWarning"), conventionWarningRound: $("conventionWarningRound"),
  conventionWarningLoophole: $("conventionWarningLoophole"),
  conventionWarningStatement: $("conventionWarningStatement"), conventionWarningCopy: $("conventionWarningCopy"),
  conventionWarningRerun: $("conventionWarningRerun"), conventionWarningIgnore: $("conventionWarningIgnore"),
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
  downloadTranscript: $("downloadTranscriptButton"),
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

let state = {
  workflowStages: { review: true, author: true, critic: true, writing: true },
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
const pinnedKinds = new Set([
  "request", "review_result", "critic_result", "author_result",
  "final_result", "failure_result", "partial_result", "diagnostic", "error", "convention_result",
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
      // The server refuses a job request over its limit: say so before sending megabytes.
      const size = jobRequestPaths.includes(path.split("?")[0]) ? requestBytes(options.body) : 0;
      if (size > maxJobRequestBytes) {
        throw new Error(`This request is ${(size / 1048576).toFixed(1)} MB; the limit is 8 MB. Shorten the text.`);
      }
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

// Requests that create or start a job (a statement, a folder batch, LaTeX, an approved
// statement) may be this large, as the server and the live gateway allow; so may a related-works note.
const maxJobRequestBytes = 8 * 1024 * 1024;
const jobRequestPaths = ["/review", "/direct", "/direct-batch", "/start-workflow", "/finalize", "/approve", "/solve",
  "/uploads/note"];

// A request body's size in UTF-8 bytes (without TextEncoder, which not every context offers);
// a body too short to reach the limit is not measured.
function requestBytes(text) {
  if (text.length * 3 <= maxJobRequestBytes) return text.length;
  const encoded = encodeURIComponent(text);
  return encoded.length - 2 * (encoded.match(/%/g) || []).length;
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
  renderAttention(batches);
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
    // Queued jobs are prepared jobs that wait for a running slot. Jobs stopped at their time limit
    // ("limit") are counted apart from paused ones. Finished jobs count as complete ("done": a successful
    // outcome) or incomplete, as the batch page's Complete and Incomplete filters do.
    const counts = batch.counts || {}, queued = Number(batch.queued || 0);
    for (const [key, label] of [["reviewed", "awaiting approval"], ["prepared", "ready to start"],
      ["queued", "queued"], ["paused", "paused"], ["limit", "time limit reached"], ["running", "running"],
      ["done", "complete"], ["incomplete", "incomplete"]]) {
      const value = key === "queued" ? queued : Number(counts[key] || 0) - (key === "prepared" ? queued : 0)
        + (key === "running" ? Number(counts.reviewing || 0) + Number(counts.pausing || 0) + Number(counts.stopping || 0) : 0);
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

// Overview: the jobs that need you (a CLI login, a usage limit, a rejected API key, a model or
// its settings, a missing saved thread ID, a safety stop). Jobs whose cause can pass
// keep retrying meanwhile; each line says what to do.
function renderAttention(batches) {
  const items = batches.flatMap((batch) => batch.attention || []);
  show(ui.attentionBanner, items.length > 0);
  const signature = JSON.stringify(items);
  if (ui.attentionList.dataset.signature === signature) return;
  ui.attentionList.dataset.signature = signature;
  ui.attentionList.replaceChildren(...items.map((item) => {
    const row = document.createElement("li"), title = document.createElement("strong");
    const link = document.createElement("a"), text = document.createElement("span");
    row.className = `attention-item ${item.kind}`;
    title.textContent = item.title;
    link.href = jobUrl(item.runId);
    link.textContent = item.job || item.runId;
    link.onclick = (event) => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openAttentionJob(item.runId);
    };
    text.textContent = item.text;
    row.append(title, link, text);
    return row;
  }));
}

// Open a job of the running server (the jobs needing attention are all its own).
function openAttentionJob(runId) {
  currentBatch = "";
  selectJob(runId);
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

// Settings and statement the composer opens with once (see rerunWithStatement), or null.
let composerSeed = null;

// Open the New problem composer with `statement` and the current job's settings, with the Proof
// author stage on: a new statement needs a new proof. Nothing starts until the user starts it.
async function rerunWithStatement(statement) {
  const keys = ["runId", "reviewModel", "authorModel", "criticModel", "writerModel", "reviewEffort", "authorEffort",
    "criticEffort", "writerEffort", "reasoningEffort", "speedMode", "reasoningSummary", "retrievalMode",
    "retrievalCutoffUtc", "retrievalPolicyPath", "retrieval", "criticRounds", "criticCount", "authorSubagents",
    "thinkingHours", "skipStatementReview", "statementReviewOnly",
    ...Object.keys(promptLabels).map((name) => `${name}Prompt`)];
  const settings = Object.fromEntries(keys.filter((key) => state[key] !== undefined).map((key) => [key, state[key]]));
  settings.workflowStages = { ...workflowStagesFor(state), author: true };
  composerSeed = { statement, settings };
  await newProblem();
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
  state = {
    workflowStages: { review: true, author: true, critic: true, writing: true },
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
  // Related works listed in the composer belong to the problem being written there.
  if (typeof clearComposerRelatedWorks === "function") clearComposerRelatedWorks(true);
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
  const question = `Delete “${title}”?\n\nIts files will move to runs/.trash/, which keeps deleted jobs for 30 days.`;
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
  // A web-cutoff run's critic continues in its own folder, under its fixed policy.
  const question = job.retrievalPolicyPath
    ? `Run the critic again on the complete saved proof for “${title}” in its own run folder, under its saved web cutoff?`
    : `Start a new critic job from the complete saved proof for “${title}”?`;
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
    + (job.retrievalPolicyPath ? "It continues in the same run folder under its saved web cutoff."
      : "The selected checkpoint will be copied into a new job.");
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
  // A historical run continues in its own folder: its policy fixes that folder and cutoff. A failed
  // statement review runs again in its own job; every other continuation is a new job.
  const retryReview = job.continueStoppedInPlace && ["input", "reviewed"].includes(job.phase);
  const outcome = job.continueStoppedInPlace
    ? "It continues in this same run folder under its saved web cutoff, "
    : "A new job will be created. The source job will not be changed, ";
  const question = retryReview
    ? `${job.continueStoppedLabel || "Retry"} for “${title}”?\n\n${detail}${settingsWarning}`
    : `${job.continueStoppedLabel || "Continue stopped job"} for “${title}”?\n\n`
      + `${detail}${settingsWarning}\n\n${outcome}`
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

// When the server tries a job again by itself: an automatic resume after an error pause, the
// next start of a queued job whose start failed, or the next attempt of a failed model call.
function retryText(job) {
  const at = (seconds) => new Date(seconds * 1000).toLocaleTimeString();
  if (job.phase === "paused" && job.autoResumeAt > 0) return `Automatic retry at ${at(job.autoResumeAt)}`;
  if (job.phase === "prepared" && job.launchQueued && job.autoResumeAt > 0) {
    return `Next start attempt at ${at(job.autoResumeAt)}`;
  }
  if (["reviewing", "running"].includes(job.phase) && job.retryAt > 0) return `Next model call at ${at(job.retryAt)}`;
  return "";
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
    // A job stopped at its time limit is counted apart from paused ones (see jobTone).
    const key = ["reviewing", "pausing", "stopping"].includes(job.phase) ? "running"
      : job.phase === "prepared" && job.launchQueued ? "queued" : timeLimitStopped(job) ? "limit" : job.phase;
    tally[key] = (tally[key] || 0) + 1;
  }
  ui.jobsCount.textContent = [`${jobs.length} ${jobs.length === 1 ? "problem" : "problems"}`,
    ...[["reviewed", "awaiting approval"], ["prepared", "ready to start"], ["queued", "queued"], ["paused", "paused"],
      ["limit", "time limit reached"], ["running", "running"], ["done", "finished"]]
      .filter(([key]) => tally[key]).map(([key, label]) => `${tally[key]} ${label}`)].join(" · ");
  const stoppedStages = { review: "statement review", solve: "proof author", repair: "proof author",
    critic: "critic", final: "LaTeX editor", failure: "failure summary" };
  for (const job of jobs) {
    const readOnly = Boolean(job.readOnly || selectedBatch()?.readOnly);
    const active = ["reviewing", "running", "stopping", "pausing"].includes(job.phase);
    const outcome = job.phase === "done" ? completedRunLabel(job) : "";
    // A queued job waits for a running slot, not for you.
    const queued = job.phase === "prepared" && Boolean(job.launchQueued);
    // The job page's classification: every successful outcome is done (Complete).
    const tone = jobTone(job);
    const attention = job.needsAttention;
    const item = document.createElement("li");
    item.className = `job-card tone-${tone}${attention ? " needs-attention" : ""}`;
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
      running: "Running", stopping: "Stopping", pausing: "Pausing", done: outcome,
      paused: timeLimitStopped(job) ? "Time limit reached" : "Paused",
      prepared: queued ? "Queued" : "Ready to start",
    };
    // A job that is stopping says so, as its page does, until it has stopped.
    status.textContent = job.launchHold ? "Prepared · awaiting review"
      : job.manuallyStopped && !active
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
    times.textContent = queued ? "Queued · starts when a running slot is free"
      : job.phase === "prepared" ? "Prepared research workspace · No model session started"
      : `Started ${localTime(job.startedAt)}${finished ? ` · Finished ${localTime(finished)}` : ""}`;
    copy.append(title, status);
    // The running author asked something; the job page shows the question (answering is optional).
    if (job.authorQuestionOpen) {
      const tag = document.createElement("span");
      tag.className = "badge job-question";
      tag.textContent = "Question";
      tag.title = "The proof author asked a question. Open the job to answer it; it keeps working meanwhile.";
      copy.append(tag);
    }
    copy.append(times);
    if (job.phase === "running" && job.stageLabel && !job.launchHold) status.textContent = `Running · ${job.stageLabel}`;
    if (attention) status.textContent += ` · ${attention.title}`;
    // A stop at the time limit is no failure: its status says it, without the checkpoint's error line.
    if (job.error && !job.manuallyStopped && !timeLimitStopped(job) && job.error !== attention?.text) {
      const reason = document.createElement("small");
      reason.className = "job-error";
      reason.textContent = job.error;
      copy.append(reason);
    }
    // What to do about it (the job keeps retrying meanwhile where it can), and when it tries next.
    if (attention) {
      const line = document.createElement("small");
      line.className = `job-attention ${attention.kind}`;
      line.textContent = `${attention.title}: ${attention.text}`;
      copy.append(line);
    }
    const retry = retryText(job);
    if (retry) {
      const line = document.createElement("small");
      line.className = "job-retry";
      line.textContent = retry;
      copy.append(line);
    }
    if (job.launchHold) {
      const hold = document.createElement("small");
      hold.className = "job-tags";
      hold.textContent = job.launchHold;
      copy.append(hold);
    }
    // Another process owns this run; this server shows it and takes it over once it is free.
    if (job.runningElsewhere) {
      const elsewhere = document.createElement("small");
      elsewhere.className = "job-tags job-elsewhere";
      elsewhere.textContent = job.runningElsewhere;
      copy.append(elsewhere);
    }
    if (job.sourceFile) {
      const tags = document.createElement("small");
      tags.className = "job-tags";
      tags.textContent = `From ${job.sourceFile}`;
      copy.append(tags);
    }
    // The convention critic's loophole warning, until it is ignored on the job page.
    if (job.loopholeWarning) {
      const tag = document.createElement("small");
      tag.className = "job-tags job-loophole";
      tag.textContent = "Loophole warning";
      copy.append(tag);
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
      if (typeof renderBatchRelatedWorks === "function") renderBatchRelatedWorks(result.jobs || []);
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

function selectedWorkflowStages() {
  return { review: ui.workflowReview.checked, author: ui.workflowAuthor.checked,
    critic: ui.workflowCritic.checked, writing: ui.workflowWriting.checked };
}

function workflowStagesFor(source = state) {
  if (source.workflowStages) return source.workflowStages;
  if (source.statementReviewOnly) return { review: true, author: false, critic: false, writing: false };
  if (["latex", "final-resume"].includes(source.problemMode)) {
    return { review: false, author: false, critic: false, writing: true };
  }
  return { review: !source.skipStatementReview && !["algorithmic", "critic-resume"].includes(source.problemMode),
    author: true, critic: true, writing: true };
}

// Read historical mode names when displaying an older saved job.
function selectedProblemMode() {
  const stages = selectedWorkflowStages();
  return stages.writing && !stages.review && !stages.author && !stages.critic ? "latex" : "statement";
}

function restoreWorkflowStages(source = state) {
  setWorkflowStages(workflowStagesFor(source));
}

function setProblemMode(mode) {
  setWorkflowStages(mode === "latex" ? { review: false, author: false, critic: false, writing: true }
    : workflowStagesFor(state));
}

function setWorkflowStages(stages = selectedWorkflowStages()) {
  for (const [stage, control] of Object.entries({ review: ui.workflowReview, author: ui.workflowAuthor,
    critic: ui.workflowCritic, writing: ui.workflowWriting })) control.checked = stages[stage] === true;
  const roles = { review: stages.review, author: stages.author, critic: stages.critic, writer: stages.writing };
  const prompts = { review: "review", author: "author", critic: "critic", writer: "final" };
  for (const [role, enabled] of Object.entries(roles)) {
    show(ui[`${role}ModelSetting`], enabled);
    show(ui[`${prompts[role]}PromptTab`], enabled);
  }
  // The convention critic checks the proof the critic accepts.
  if (ui.conventionPromptTab) show(ui.conventionPromptTab, stages.critic);
  show(ui.criticRoundSetting, stages.critic);
  show(ui.criticCountSetting, stages.critic);
  show(ui.thinkingHoursSetting, stages.author);
  show(ui.runFolder, Object.values(stages).some(Boolean));
  show(ui.runFolderHelp, Object.values(stages).some(Boolean));
  show(ui.retrievalSetting, true);
  const enabled = Object.values(stages).some(Boolean);
  const creating = ui.check.dataset.creating === "1";
  ui.check.disabled = !enabled || creating;
  if (!creating) ui.check.textContent = "Start workflow";
  ui.problem.required = true;
  const existingWork = !stages.author && (stages.critic || stages.writing);
  ui.problemLabel.textContent = existingWork ? "Statement and existing work" : "Problem statement";
  ui.problem.placeholder = existingWork ? "Paste the statement and the proof, draft, or writing to work on…"
    : "Paste a theorem, conjecture, lower bound, or algorithmic task…";
  ui.workflowStageHelp.textContent = !enabled ? "Select at least one stage to start."
    : existingWork ? "Selected stages use the existing work above. Click any stage to turn it on or off."
    : "Click any stage to turn it on or off. Selected stages run from left to right.";
  ui.introDescription.textContent = "Choose the stages for your problem. Review the statement, develop a proof, check it, and polish the writing.";
  const first = Object.keys(roles).find((role) => roles[role]);
  if (ui.promptDialog.open && first && !roles[activePrompt === "final" ? "writer" : activePrompt]) selectPrompt(prompts[first]);
  updateModelSummary();
  renderRetrievalSettings();
  if (typeof renderComposerRelatedWorks === "function") renderComposerRelatedWorks();
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
  const stages = selectedWorkflowStages();
  return ["author", "critic", "writer"].filter((role) => stages[role === "writer" ? "writing" : role]
    && modelProvider(ui[`${role}Model`].value) !== "codex");
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
    : mode === "disabled" ? "All model web-search tools are disabled, and the models' shell commands get no network access."
    : "The model's native web search is enabled without a date restriction.";
  const stages = selectedWorkflowStages();
  const message = !historical ? ""
    : !stages.author
      ? "A historical cutoff requires the Author stage. Use cutoff Off or enter -1 to disable web-search tools."
    : capabilities.historicalAvailable !== true
    ? capabilities.reason || "Historical search setup is unavailable in this interface. Open the updated TCS Prover interface to create a historical job."
    : stages.review
      ? `Turn Statement review off to start a historical proof job. ${cutoffModelMessage()}`
      : cutoffModelMessage();
  ui.retrievalConfigurationNotice.textContent = message;
  show(ui.retrievalConfigurationNotice, Boolean(message));
  renderModelChoices();
  if (typeof renderComposerRelatedWorks === "function") renderComposerRelatedWorks();
}

function validateRetrievalSettings({ folder = false } = {}) {
  if (selectedRetrievalMode() !== "historical") return true;
  let message = "", focus = ui.retrievalCutoffUtc;
  const value = ui.retrievalCutoffUtc.value.trim();
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d{1,6})?(?:Z|\+00:00)$/.exec(value);
  const base = match && `${match[1]}T${match[2]}Z`;
  const parsed = base && new Date(base);
  const stages = selectedWorkflowStages();
  if (!stages.author) {
    message = "A historical cutoff requires the Author stage. Use cutoff Off or enter -1 to disable web-search tools.";
    focus = ui.retrievalMode;
  } else if (state.retrievalCapabilities?.historicalAvailable !== true) {
    message = state.retrievalCapabilities?.reason || "This interface cannot create historical jobs. Open the updated TCS Prover interface before starting this job.";
    focus = ui.retrievalMode;
  } else if (!match || !Number.isFinite(parsed?.getTime()) || parsed.toISOString().slice(0, 19) !== base.slice(0, 19)) {
    message = "Enter -1 to disable web-search tools, or a valid UTC cutoff such as 2026-10-02T14:13:12Z. Include seconds and the UTC timezone.";
  } else if (Date.parse(value) >= Date.now()) {
    message = "Choose a historical cutoff in the past.";
  } else if (stages.review) {
    message = "Turn Statement review off to start a historical proof job.";
    focus = ui.workflowReview;
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
  // A saved job or a script may still say Ultra, which is no longer offered: it runs as Max.
  const efforts = {
    ultra: "Max", max: "Max", xhigh: "Extra high", high: "High", medium: "Medium", low: "Low",
  };
  const role = (select, effort) => `${name(select)}/${efforts[effort.value] || effort.value}`;
  const stages = selectedWorkflowStages();
  const selectedRoles = ["review", "author", "critic", "writer"].filter((role) => stages[role === "writer" ? "writing" : role]);
  const selectedModels = selectedRoles.map((role) => ui[`${role}Model`].value);
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
  const roles = selectedRoles.map((name) => `${role(ui[`${name}Model`], ui[`${name}Effort`])} ${name}`);
  // No-break spaces (and a word joiner after each hyphen of an API model's name) keep each part
  // ("6.1 Sol/Max review") on one line: a wrapped summary breaks only after a "·", so no line
  // starts with one and no model name is split. A part longer than a line still wraps (CSS).
  ui.modelSummary.textContent = [...speed, log, ...roles]
    .map((part) => part.replace(/ /g, "\u00a0").replace(/-/g, "-\u2060")).join("\u00a0· ");
}

const promptLabels = {
  review: "Reviewer prompt", author: "Author prompt",
  critic: "Critic prompt", convention: "Convention critic prompt", final: "Final writer prompt",
};

const promptHelp = {
  review: "The full request also includes the statement and any revision feedback.",
  author: "Keep exactly one [STATEMENT]. The workflow replaces it with your statement.",
  critic: "The critic follows these instructions: it starts the independent critics as its subagents, weighs their reports and decides. {count} is the number of independent critics; write any other brace twice: {{ and }}. The workflow adds the statement, and the proof is in proof.md.",
  convention: "The convention critic checks the proof once the critic has accepted it, before writing; its report goes to you and never changes the run. Keep {statement} where the statement goes, and write any other brace twice: {{ and }}.",
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
    const stages = selectedWorkflowStages();
    activePrompt = ["review", "author", "critic", "writing"].find((name) => stages[name]) || "review";
    if (activePrompt === "writing") activePrompt = "final";
    selectPrompt(activePrompt);
    ui.promptDialog.showModal();
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
  }
}

// The first draft that cannot be applied, as [role, explanation].
function promptProblem() {
  const stages = selectedWorkflowStages();
  const stage = { final: "writing", convention: "critic" };
  const enabled = ["review", "author", "critic", "convention", "final"].filter((name) => stages[stage[name] || name]);
  const empty = enabled.find((name) => !promptDrafts[name]?.trim());
  if (empty) return [empty, "Every role prompt must contain instructions."];
  // Like the workflow's templates: {statement} (or {instructions}) and doubled braces only.
  const fields = String(promptDrafts.convention || "").replace(/\{\{|\}\}/g, "").match(/\{[^{}]*\}|[{}]/g) || [];
  if (stages.critic && fields.some((field) => !["{statement}", "{instructions}"].includes(field))) {
    return ["convention", "Keep {statement} where the statement goes, and write any other brace twice: {{ and }}."];
  }
  return !stages.author || (promptDrafts.author?.match(/\[STATEMENT\]/g) || []).length === 1
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
  // As on the server: the critic's independent critics requested, started or finished (the progress line reads them).
  if (entry.kind === "status" && entry.root !== false && entry.subagentProgress === true) return true;
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
  // A root message is labelled by who wrote it: the author, else the call's own label (one of
  // the independent critics, their coordinator) or the role of its stage.
  const authorMessage = !entry.stage || entry.node === "author"
    || ["solve", "repair", "conversation", "failure"].includes(entry.stage);
  const writer = authorMessage ? scopedLabel("Author")
    : activityLabel || {review: "Statement reviewer", critic: "Critic", final: "LaTeX editor"}[entry.stage] || "Answer";

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
        ? (round ? `${outcome}.` : report.bugs || "Unfixable issues return to the proof author.")
        : verdict === "pass" ? outcome + (round?.outcome === "pass_edited_limit"
          ? ". The changed proof is accepted without another critic round because the round limit was reached." : ".")
          : "No valid critic verdict was recorded for this response.",
      time: entry.time, replace: true, pinned: true,
      // The critic's one decision and the convention critic, as the critic review history shows them.
      critic: round ? {round, next: match?.cycle.rounds[match.roundNumber] || null} : null,
    };
  }
  if (entry.kind === "convention_result") {
    const loophole = entry.verdict === "loophole";
    return {
      key: `convention:${entry.time || entry.round}`, type: "agent",
      label: `${entry.label || "Convention critic"} · ${loophole ? "May exploit a loophole" : "Faithful"}`,
      text: [loophole ? `How it exploits the statement: ${entry.loophole || ""}` : "",
        `What the user most likely wants: ${entry.intended || ""}`, `How the proof relates to it: ${entry.assessment || ""}`,
        loophole ? `Suggested statement: ${entry.suggestedStatement || ""}` : ""].filter(Boolean).join("\n\n"),
      time: entry.time, replace: true, pinned: true,
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
    // A status replaces the previous one of its label, except the progress of the critic's independent
    // critics: every attempt keeps its "N independent critics requested" and "Independent critic i" rows
    // (and an older version's own critics, "Independent audit i", keep theirs).
    const critics = entry.subagentProgress === true || Number.isInteger(entry.auditIndex)
      || Number.isInteger(entry.auditCount);
    return {
      key: critics ? `status:${entry.label}:${entry.time}` : `status:${entry.label || keyBase}`, type: "status",
      label: entry.label || "Status", text, time: entry.time, replace: true,
    };
  }
  if (entry.kind === "diagnostic" || entry.kind === "error") {
    return {
      key: `error:${keyBase}`, type: "error", label: entry.label || "Diagnostic",
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
      label: root ? writer : scopedLabel("Subagent"), text: params.delta || "",
      time: entry.time, append: true,
    };
  }
  if (
    ["item/completed", "item.completed"].includes(name)
    && ["agentMessage", "agent_message"].includes(item.type)
  ) {
    // A question to the user (delivered while the author keeps working) is no answer; the box shows it.
    const question = root && authorMessage && item.delivery === "async";
    return {
      key: `agent:${itemId}`, type: "agent",
      label: question ? scopedLabel("Author question") : root ? writer : scopedLabel("Subagent"), text: item.text || "",
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
    // A failed turn shows the provider's reason, not only "failed".
    const error = params.turn?.error;
    const reason = typeof error === "string" ? error : error?.message;
    return {
      key: `turn:${turnKey}`, type: "status",
      label: scopedLabel("Turn completed"),
      text: [params.turn?.status || event.status || "completed", reason].filter(Boolean).join(": "),
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
  card.text = card.append && old && !old.placeholder ? old.text + card.text : card.text;
  row._card = card;
  row.dataset.pinned = String(Boolean(card.pinned || old?.pinned));
  // A critic result's rows that the reader opened stay open when the card is redrawn.
  const opened = new Set([...(row.querySelectorAll?.("details[data-part]") || [])]
    .filter((detail) => detail.open).map((detail) => detail.dataset.part));
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

  if (card.critic) {
    // A critic result: the critic's one decision (what it fixed, the critical bugs, the changes) and the convention critic.
    const parts = document.createElement("div");
    parts.className = "critic-round-parts";
    parts.append(...criticRoundParts(card.critic.round, card.critic.next, (key, label, content, className) => {
      const detail = document.createElement("details");
      detail.className = className || "critic-review-text";
      detail.dataset.part = key;
      detail.open = opened.has(key);
      const summary = document.createElement("summary");
      summary.textContent = label;
      detail.append(summary);
      if (content !== undefined) {
        const copy = document.createElement("pre");
        copy.textContent = content;
        detail.append(copy);
      }
      return detail;
    }));
    content.append(parts);
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
  return completedRunOutcome(job).label;
}

// What a finished job produced, from its recorded stages and result, in one place for the batch list
// (its status, filter and counts), the job header, the now-card and the workflow rail: its label; its
// tone ("done" for every successful outcome, counted under Complete, else "failed" or "stopped", counted
// under Incomplete); the sentence under the job title, which says what was made and what was skipped;
// where the run ended (a workflow rail node) and why, in plain words.
function completedRunOutcome(job) {
  // Older jobs did not record their stages: every stage ran.
  const stages = {review: true, author: true, critic: true, writing: true, ...(job.workflowStages || {})};
  const node = railNode(job.activeNode, stages);
  const outcome = (label, tone, summary, ended, at = node) => ({label, tone, summary, ended, node: at});
  if (job.manuallyStopped) {
    // The step it stopped at: the active one when it belongs to the stopped stage (Compile PDF in
    // Writing & polishing, the convention critic in the critic's stage), else the stage's first step.
    const steps = {review: ["statement_reviewer"], solve: ["author"], repair: ["author"], critic: ["critic", "convention"],
      final: ["latex_editor", "latex_compile"], failure: ["failure_summary"]}[job.stoppedStage] || [];
    return outcome("Stopped", "stopped", "You stopped this job. Its checkpoints remain available.",
      "stopped by you", steps.includes(node) ? node : steps[0] || node);
  }
  // A saved run whose last checkpoint says it was still running (a crash or a restart).
  if (job.interrupted) {
    return outcome("Interrupted", "failed", "The run stopped part-way, while its last checkpoint says it was "
      + "still running. Its checkpoints remain available.", "the run stopped part-way");
  }
  if (job.resultKind === "review" || job.statementReviewOnly) {
    return outcome("Review complete", "done", "The statement review is saved.",
      "statement checked; no proof stages were selected", "statement_reviewer");
  }
  if (job.resultKind === "needs-revision" || job.revisionRequired) {
    return outcome("Needs revision", "failed",
      "The supplied work needs revision. The candidate and critic feedback are saved below.",
      "the critic found errors it could not fix", "critic");
  }
  if (job.resultKind === "failure" || job.error) {
    return outcome("Incomplete", "failed",
      "No completed answer was produced. Its checkpoints remain available.",
      String(job.error || "").trim() || "the run ended without a complete result");
  }
  if (job.resultKind === "proof") {
    // Writing & polishing was not selected: the proof stages' result is the job's result.
    // The convention critic, when it ran, checked the accepted proof last.
    return stages.critic
      ? outcome("Proof accepted", "done", "The critic accepted the proof. Writing & polishing was not selected "
        + "for this run, so no PDF was made.", "accepted by the critic; writing not selected",
        node === "convention" ? "convention" : "critic")
      : outcome("Proof written (not reviewed)", "done", "The proof author wrote a proof. The critic was not "
        + "selected for this run, so no one reviewed it, and Writing & polishing was not selected, so no PDF "
        + "was made.", "proof written; critic and writing not selected", "author");
  }
  if (job.resultKind === "final" || job.canDownloadTex) {
    const ended = job.canDownloadPdf === false && job.canDownloadTex ? "LaTeX source saved; the PDF was not compiled"
      : "LaTeX document and PDF made";
    if (stages.author && !stages.critic) {
      return outcome("Proof written (not reviewed)", "done", "The proof author wrote a proof and Writing & "
        + "polishing made the LaTeX document. The critic was not selected for this run, so no one reviewed the "
        + "proof.", `${ended}; the proof was not reviewed`, "latex_compile");
    }
    const summary = !stages.author && stages.critic ? "The critic accepted the supplied proof and Writing & "
        + "polishing made the LaTeX document. The proof author was not selected for this run."
      : !stages.author ? "Writing & polishing made the LaTeX document from the supplied writing. The proof "
        + "author and critic were not selected for this run."
      : "The final answer is saved below. Source and PDF downloads are available for saved artifacts.";
    return outcome("Answer ready", "done", summary, ended, "latex_compile");
  }
  return outcome("Incomplete", "failed",
    "No completed answer was produced. Its checkpoints remain available.",
    "the run ended without a complete result");
}

// One status classification for every list, filter and count and for the job page: "running",
// "paused" (by you, a restart, an interruption, a failure, ...), "limit" (stopped at the author time
// limit: "Time limit reached", never counted as paused; raising the limit lets it continue), "queued"
// (waiting for a running slot, not for you), "waiting" (for you), or a finished job's tone (see
// completedRunOutcome).
function jobTone(job) {
  if (["reviewing", "running", "stopping", "pausing"].includes(job.phase)) return "running";
  if (job.phase === "paused") return timeLimitStopped(job) ? "limit" : "paused";
  if (job.phase === "prepared" && job.launchQueued) return "queued";
  if (["reviewed", "prepared"].includes(job.phase)) return "waiting";
  if (job.phase !== "done") return job.manuallyStopped ? "stopped" : "failed";
  return completedRunOutcome(job).tone;
}

// A node as the workflow rail shows it: a LaTeX repair is the Compile PDF step, and a step of a stage
// this run did not select (the author of a prepared critic-only job) is the first selected step, where
// the run starts. Steps outside the stages (the saved progress) stay as they are.
function railNode(node, stages) {
  const name = node === "latex_repair" ? "latex_compile" : node || "";
  const steps = [["statement_reviewer", "review"], ["author", "author"], ["critic", "critic"], ["convention", "critic"],
    ["latex_editor", "writing"], ["latex_compile", "writing"]];
  const stage = steps.find(([step]) => step === name)?.[1];
  if (!stage || stages?.[stage] !== false) return name;
  return steps.find(([, selected]) => stages[selected])?.[0] || name;
}

// What a job that waits to retry waits for (state.modelRetry.kind), as the now-card and the rail say it.
function retryWaitText(retry) {
  return retry?.kind === "command" ? "a command that could not run"
    : retry?.kind === "storage" ? "saving a file" : "a failed model call";
}

// A job the author time limit stopped (its checkpoint's pause cause): every list, count and the job
// page say "Time limit reached", with a clock, and never call it paused (see jobTone).
function timeLimitStopped(job) {
  return job?.phase === "paused" && job.pauseCause === "time-limit";
}

// Whether the stopped author's automatic progress summary at the time limit is queued or being written.
function limitSummaryWriting(job) {
  return (Array.isArray(job?.conversation?.items) ? job.conversation.items : []).some((item) => item.kind === "summary"
    && item.origin === "time-limit" && ["queued", "running"].includes(item.status));
}

// A time limit as the server writes it (hours_text): "1 hour", "1.5 hours", "1,000 hours"; a limit
// under 0.01 hours keeps two significant digits ("0.003 hours"), so it never reads as 0.
function hoursText(hours) {
  if (hours > 0 && hours < 0.01) {
    return `${Number(hours.toPrecision(2)).toLocaleString("en-US", { maximumFractionDigits: 20 })} hours`;
  }
  const value = Math.round(hours * 100) / 100;
  return `${value.toLocaleString("en-US", { maximumFractionDigits: 2 })} hour${value === 1 ? "" : "s"}`;
}

// A duration in words as the runner writes it (_span_text): "45 s", "6 min", "2 h 5 min", "3 days 4 h".
function spanText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 120) return `${minutes} min`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  if (hours < 48) return rest ? `${hours} h ${rest} min` : `${hours} h`;
  const days = Math.floor(hours / 24), left = hours % 24;
  return `${days} days${left ? ` ${left} h` : ""}`;
}

// The author time limit as Run settings states it: "No time limit", "2 hours", "0.003 hours (11 s)".
function limitSettingText(hours) {
  return hours > 0 ? `${hoursText(hours)}${Number.isInteger(hours) ? "" : ` (${spanText(hours * 3600)})`}`
    : "No time limit";
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

// The time limit's history as the server records it (state.timeLimitHistory, oldest first): a change
// of the limit, a pause at it, the progress summary written at that pause, and the Resume after it.
function timeLimitHistory() {
  return Array.isArray(state.timeLimitHistory) ? state.timeLimitHistory : [];
}

// One line per event, for Run settings. A first pause before any change also tells the limit the job
// started with.
function timeLimitHistoryLines(history = timeLimitHistory()) {
  const start = history[0]?.event === "reached" && state.startedAt
    ? [`${localDateTime(state.startedAt)} · Started with ${limitSettingText(history[0].hours)}`] : [];
  return [...start, ...history.map((item) => `${localDateTime(item.time)} · ${
    item.event === "set" ? `Changed to ${item.hours > 0 ? limitSettingText(item.hours) : "no time limit"}`
    : item.event === "reached" ? `Stopped at the time limit (${hoursText(item.hours)}) after `
      + `${spanText(item.elapsedSeconds)} of active work`
    : item.event === "summary" ? "Progress summary written"
    : `Resumed ${item.hours > 0 ? `with a limit of ${limitSettingText(item.hours)}` : "with no time limit"}`}`)];
}

// One line per pause at the time limit, for the job header, also after the job finished: "Paused at
// the time limit (0.003 hours) at 01:09:29 · progress summary written · resumed at 03:50:06 with no limit".
function timeLimitPauseNotes(history = timeLimitHistory()) {
  const notes = [];
  for (const item of history) {
    if (item.event === "reached") notes.push(`Stopped at the time limit (${hoursText(item.hours)}) at ${clockText(item.time)}`);
    else if (notes.length && item.event === "summary") notes[notes.length - 1] += " · progress summary written";
    else if (notes.length && item.event === "resumed") {
      notes[notes.length - 1] += ` · resumed at ${clockText(item.time)} with `
        + (item.hours > 0 ? `a limit of ${hoursText(item.hours)}` : "no limit");
    }
  }
  return notes;
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
  // A saved job or a script may still say ultra, which is no longer offered and runs as max.
  const level = effort === "ultra" ? "max (Ultra is no longer offered)" : effort;
  return `${model}${unconfiguredApiModel(model) ? " (not configured)" : ""} · ${level}${endpoint}`;
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
  // A workflow file that cannot be read runs from its last readable version (state.workflowWarning).
  const warning = [state.settingsWarning, state.workflowWarning].filter(Boolean).join(" ");
  if (state.readOnly) return warning;
  const roles = state.workflowStages ? ["review", "author", "critic", "writer"].filter((role) => state.workflowStages[role === "writer" ? "writing" : role])
    : state.statementReviewOnly ? ["review"]
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
  const finished = state.phase === "done" ? completedRunOutcome(state) : null;
  const outcome = finished?.label || "";
  ui.runOverview.dataset.phase = state.phase;
  ui.runOverview.dataset.status = jobTone(state);
  ui.runOverview.dataset.retrieval = retrievalConfiguration(state).mode;
  const latex = state.problemMode === "latex";
  const stages = state.workflowStages || {review: !state.skipStatementReview, author: !latex && !state.statementReviewOnly,
    critic: !latex && !state.statementReviewOnly, writing: !state.statementReviewOnly};
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
  // A job stopped at its time limit is not paused: it says "time limit reached" (see jobTone).
  const limitStop = timeLimitStopped(state);
  ui.runStage.textContent = state.phase === "reviewed" ? "Waiting for approval"
    : state.phase === "prepared" ? (state.launchHold ? "Awaiting review" : "Ready to start")
    : state.phase === "done" ? completedRunLabel(state)
    : `${state.workflow?.nodes?.[state.activeNode]?.label || state.stage || "Starting"}`
      + (limitStop ? " · time limit reached" : ["paused", "pausing"].includes(state.phase) ? ` · ${state.phase}` : "")
      + (state.activeNode === "critic" && latestCycle ? ` · cycle ${reviewCycles.length} · round ${criticRoundNumber(latestCycle)}` : "");
  const seconds = runElapsedSeconds(), hours = Number(state.thinkingHours ?? 0), limit = hours * 3600;
  ui.runActiveTime.textContent = durationText(seconds);
  ui.runBudget.textContent = hours === 0 ? "Unlimited" : state.timeLimitExhausted ? `${hours}h · used up`
    : `${hours}h · ${durationText(Math.max(0, limit - seconds))} left`;
  const halted = limitStop ? "Stopped at the time limit" : "Paused";
  ui.runHealth.textContent = state.processAlive == null && state.workerActive == null ? "Unknown · telemetry unavailable"
    // The automatic progress summary at the time limit, queued or being written.
    : limitStop && limitSummaryWriting(state) && !state.readOnly ? "Time limit reached · writing progress summary"
    : state.processAlive ? (state.phase === "paused" ? `${halted} · author replying` : "Running")
    : state.workerActive ? "Starting / closing"
    : state.runningElsewhere ? "Running in another process"
    : state.phase === "paused" ? `${halted} · saved conversation`
    : ["reviewed", "prepared"].includes(state.phase) ? "Waiting for you" : "No active process";
  const nextSteps = {
    review: "After review, approve the checked statement to continue the selected stages.",
    solve: stages.critic ? "The author is searching for a complete proof. Its candidate then goes to the independent critics."
      : stages.writing ? "The author is searching for a complete proof. The writing stage will polish its result."
      : "The author is searching for a complete proof. This workflow finishes with its result.",
    repair: "The author is addressing critic objections. A revised candidate returns to independent review.",
    critic: state.activeNode === "convention"
      ? "The convention critic checks whether the accepted proof does what you most likely want. It only warns; "
        + (stages.writing ? "writing follows either way." : "the run then ends either way.")
      : stages.author ? "The critic starts independent critics on the candidate and decides. Rejected proofs return to the author."
      : "The critic starts independent critics on the supplied work and reports any unresolved errors. The Author stage is off.",
    final: "The accepted proof is being formatted and compiled. Source and PDF downloads appear when compilation succeeds.",
  };
  let nextStep = state.phase === "reviewed" ? "Statement review finished. Approve the statement to continue the selected stages."
    : state.phase === "paused" ? (state.readOnly ? "No final answer yet. This saved job is read-only."
      : "No final answer yet. The work so far is saved.")
    : state.phase === "prepared" ? (state.launchHold || "Start this prepared workspace to begin proof search.")
    // What the finished job produced and which stages it skipped (see completedRunOutcome).
    : finished ? finished.summary
    : state.statementReviewOnly ? "This job will save the checked statement and reviewer notes, then finish."
    : nextSteps[state.stage] || "Waiting for the next workflow update.";
  // The critic verdict explains a LaTeX stage, but must not lead an incomplete result.
  if (latestCycle && outcome !== "Incomplete"
      && ["latex_editor", "latex_compile", "latex_repair"].includes(state.activeNode)) {
    nextStep = `${criticCycleOutcomeLabel(latestCycle)}. ${nextStep}`;
  }
  // A role=status region: write only real changes so the 1 s clock does not re-announce it.
  if (ui.runNextStep.textContent !== nextStep) ui.runNextStep.textContent = nextStep;
  // Every pause at the time limit, and what followed it, stays stated here after the job finished.
  const limitNotes = timeLimitPauseNotes().join("\n");
  if (ui.runTimeLimitNote.textContent !== limitNotes) ui.runTimeLimitNote.textContent = limitNotes;
  show(ui.runTimeLimitNote, Boolean(limitNotes));
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
  // Why the job needs you; it keeps retrying meanwhile where it can. Written only on change.
  const attention = state.needsAttention && !state.readOnly
    ? `${state.needsAttention.title}: ${state.needsAttention.text}` : "";
  if (ui.runAttention.textContent !== attention) ui.runAttention.textContent = attention;
  show(ui.runAttention, Boolean(attention));
  renderConventionWarning();
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
    ["Statement reviewer", stages.review ? modelRecord(state.reviewModel, state.reviewEffort || state.reasoningEffort) : "Off"],
    ["Proof author", stages.author ? modelRecord(state.authorModel, state.authorEffort || state.reasoningEffort)
      // An older job, from before the Author subagents setting, is not described with it.
      + (state.authorSubagentsRecorded === false ? ""
        : Number.isInteger(state.authorSubagents) && state.authorSubagents > 0
          ? ` · up to ${state.authorSubagents} subagent${state.authorSubagents === 1 ? "" : "s"} at a time`
          : " · no subagents") : "Off"],
    // The limit now, and every change of it and pause at it (the server's records).
    ["Author time limit", stages.author ? limitSettingText(Number(state.thinkingHours ?? 0)) : "Off"],
    ...(timeLimitHistory().length ? [["Time limit history", timeLimitHistoryLines().join("\n")]] : []),
    ["Independent critic", stages.critic ? `${modelRecord(state.criticModel, state.criticEffort || state.reasoningEffort)}`
      // An older job that did not record its number of critics may never have run them, and one from before
      // the Author subagents setting ran them itself, not as the critic's subagents.
      + (Number.isInteger(state.criticCount) && state.criticCountRecorded !== false
        ? ` · ${state.criticCount} independent critic${state.criticCount === 1 ? "" : "s"}`
          + (state.authorSubagentsRecorded === false ? "" : state.criticCount === 1 ? " as a subagent" : " as subagents")
        : "")
      + ` · max ${state.criticRounds} consecutive rounds` : "Off"],
    ["LaTeX writer", stages.writing ? modelRecord(state.writerModel, state.writerEffort || state.reasoningEffort) : "Off"],
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
  const final = state.resultKind === "final" || state.canDownloadTex;
  // Only a result is shown here; an unfinished run's own text is not (its progress summary says where it stands).
  const savedOutput = Boolean(state.output && ["done", "paused"].includes(state.phase)
    && (final || ["proof", "needs-revision", "failure"].includes(state.resultKind) || state.revisionRequired));
  show(ui.resultPanel, savedOutput);
  ui.resultHeading.textContent = final ? (state.canDownloadTex ? "Final answer · LaTeX source" : "Final answer")
    : state.resultKind === "proof" ? (stages.critic ? "Accepted proof" : "Proof (not reviewed)")
    : state.revisionRequired ? "Candidate and critic feedback"
    : "Failure report";
  ui.resultDescription.textContent = final ? "The complete saved output is shown here."
    : state.resultKind === "proof" ? (stages.critic ? "The complete proof, as the critic accepted it."
      : "The complete proof the author wrote. No critic reviewed it.")
    : state.revisionRequired ? "The critic found unresolved errors in this candidate."
    : "This run ended without a complete result; this is its failure report.";
  if (savedOutput && ui.resultOutput.textContent !== state.output) ui.resultOutput.textContent = state.output;
  // A LaTeX source published without its PDF says so; the activity log has the reason.
  if (final && state.canDownloadTex && !state.canDownloadPdf) {
    ui.resultDescription.textContent = "The complete saved LaTeX source is shown here. The PDF was not compiled; "
      + "the activity log says why.";
  }
  const identity = `${state.batchId || ""}:${state.runId}:${state.resultKind || "partial"}`;
  if (ui.resultPanel.dataset.identity !== identity) {
    ui.resultPanel.dataset.identity = identity;
    ui.resultPanel.open = final || ["proof", "needs-revision", "failure"].includes(state.resultKind);
  }
}

// The convention critic's latest loophole warning, near the top of the job, until the user ignores
// it (the server keeps it, and whether it was ignored, in the job's state). The run goes on as normal.
function renderConventionWarning() {
  const box = ui.conventionWarning;
  if (!box) return;
  const warning = state.conventionWarning;
  const open = Boolean(warning && typeof warning === "object" && !warning.dismissed && warning.loophole);
  show(box, open);
  if (!open) return;
  const round = Number.isInteger(warning.round) ? `Found in critic round ${warning.round}` : "Found by the convention critic";
  const meta = `${round}${warning.time ? ` · ${localDateTime(warning.time)}` : ""}`;
  if (ui.conventionWarningRound.textContent !== meta) ui.conventionWarningRound.textContent = meta;
  if (ui.conventionWarningLoophole.textContent !== warning.loophole) ui.conventionWarningLoophole.textContent = warning.loophole;
  const statement = warning.suggestedStatement || "";
  if (ui.conventionWarningStatement.textContent !== statement) ui.conventionWarningStatement.textContent = statement;
  box.dataset.warning = warning.time || "";
  // Ignoring changes the job; a saved batch is read-only. A rerun only opens the composer.
  show(ui.conventionWarningIgnore, !state.readOnly);
}

// Group the recorded critic calls by author submission, retaining every input.
// A PASS verdict alone never establishes that the workflow accepted the proof.
function collectCriticReviewCycles(trace, options = {}) {
  const cycles = [], seen = new Set(), identities = new Map(), keyCounts = new Map(), anchors = new Map();
  let cycle = null, pending = null, visit = null, handoff = null, index = 0, inConvention = false;
  const isRoot = entry => !entry.activityLabel && entry.root !== false;
  const stamp = entry => String(entry.requestId || entry.id || entry.time || `event-${index}`);
  // A round's key comes from its first input, else its decision, else the start of its critic
  // visit (rank 2, 1, 0), so it stays the same when routine events leave the trace. The keys of
  // its earlier anchors stay its aliases: a row opened under one stays open (see
  // renderCriticReviewHistory).
  const anchor = (round, entry, rank) => {
    const previous = anchors.get(round);
    if ((previous?.rank ?? -1) < rank) {
      anchors.set(round, {rank, stamp: stamp(entry), earlier: previous ? [...previous.earlier, previous.stamp] : []});
    }
  };
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
  // The round its critic visit opened, before its first input: the visit's critics run first. A
  // resumed visit reopens its interrupted round until its coordinator is asked again.
  const opened = () => pending && !pending.result && !pending.interrupted
    && (!pending.requests.length || pending.reopened);
  const boundary = () => {
    closePending();
    if (cycle) cycle.closed = true;
    cycle = null; visit = null; handoff = null;
  };
  const ensureCycle = entry => {
    const explicitId = entry.cycleId || entry.reviewCycleId;
    if (explicitId && cycle && cycle.id && cycle.id !== explicitId) boundary();
    if (!cycle || cycle.closed) {
      // Keys are given once the rounds are known: a cycle's is that of its first round.
      cycle = {key: explicitId ? `cycle:${explicitId}` : null, id: explicitId || null,
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
  const ensureRound = (entry, rank) => {
    const current = ensureCycle(entry);
    continuePrevious(current);
    const round = {key: null, number: current.rounds.length + 1,
      roundNumber: positive(entry.round) || visit, requests: [], result: null,
      outcome: "pending", outcomeEvidence: null, transition: null,
      candidateText: null, handoff, startTime: entry.time || null, interrupted: false,
      // Resumed after an interruption; the positions of its inputs sent after a resume.
      resumed: false, reopened: false, resumedInputs: [],
      // The progress of the critic's independent critics in its latest attempt (see criticRoundProgress),
      // and the convention critic of the round that accepted the proof: its record, or that it runs.
      progress: null, convention: null, conventionState: null};
    anchor(round, entry, rank);
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
      round.outcome = "reject";
      round.transition = declared === "needs_revision" ? "needs_revision" : "author";
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
    // The convention critic's request is not an input of a critic round: it runs once the critic accepted. Nor
    // are the requests of the independent critics that an earlier version ran itself (older transcripts).
    const conventionRequest = entry.kind === "request" && /^Convention critic\b/.test(entry.label || "");
    const legacyAudit = entry.kind === "request" && /^Independent critic audit\s+\d+\b/i.test(entry.label || "");
    const criticRequest = !conventionRequest && !legacyAudit && entry.kind === "request"
      && (entry.stage === "critic" || entry.node === "critic");
    // The progress of the critic's independent critics (its subagents), and the convention critic's report.
    const progressStatus = entry.kind === "status" && entry.subagentProgress === true;
    const conventionResult = entry.kind === "convention_result";
    const criticResult = entry.kind === "critic_result";
    const approval = entry.kind === "status" && entry.label === "Critic approved";
    const workflowStep = entry.kind === "status" && /^Workflow step:/.test(entry.label || "");
    const conventionStep = workflowStep && (entry.node === "convention" || entry.label === "Workflow step: convention");
    // The convention step captures the accepted proof again: no new round.
    const handoffEvent = entry.kind === "proof_handoff" && !inConvention;
    if (criticRequest || criticResult || approval || handoffEvent || workflowStep
        || conventionRequest || progressStatus || conventionResult || legacyAudit) {
      // Replayed transcript entries must not create extra rounds. Include the
      // attempt and payload because request IDs may be shared across retries.
      if (entry.time || entry.id || entry.requestId) {
        const identity = JSON.stringify([entry.kind, entry.time, entry.id, entry.requestId,
          entry.attempt, entry.round, entry.visit, entry.label, entry.text, entry.report]);
        if (seen.has(identity)) continue;
        seen.add(identity);
      }
    }
    if (legacyAudit) {
      // An older version's independent critic: its first request opens its visit's round when the visit's
      // own step and handoff records have left the trace (they are routine events, its requests are not).
      if (!(Number.isInteger(entry.attempt) && entry.attempt > 1) && !opened()) {
        closePending();
        ensureRound(entry, 0);
      }
    } else if (progressStatus) {
      // Each attempt of the critic's call requests its critics anew; the latest attempt's progress counts.
      const round = pending && !pending.result ? pending : null;
      if (!round) continue;
      if (/independent critics? requested$/.test(entry.label || "") || !round.progress) {
        round.progress = {count: Number.isInteger(entry.auditCount) ? entry.auditCount : null, started: {}, waiting: false};
      }
      if (Number.isInteger(entry.auditCount)) round.progress.count = entry.auditCount;
      if (Number.isInteger(entry.auditIndex)) {
        round.progress.started[entry.auditIndex] = / finished$/.test(entry.label || "")
          ? (entry.failed ? "failed" : "finished") : round.progress.started[entry.auditIndex] || "working";
      }
      if (entry.waiting === true) round.progress.waiting = true;
    } else if (conventionStep || conventionRequest || conventionResult) {
      // The convention critic belongs to the round that accepted the proof: the latest decided round.
      const round = cycles.at(-1)?.rounds.findLast(item => item.result) || null;
      if (conventionStep) inConvention = true;
      if (!round) continue;
      if (conventionResult) {
        round.convention = entry;
        round.conventionState = "finished";
      } else if (!round.convention) round.conventionState = "running";
    } else if (criticRequest) {
      const lastAttempt = pending?.requests.at(-1)?.attempt || 1;
      // The first input of a visit (its coordinator's, sent once its critics have reported)
      // belongs to the round the visit opened; a later attempt of the same call is a retry.
      const joins = opened() || pending && !pending.result && !pending.interrupted
        && Number.isInteger(entry.attempt) && entry.attempt > lastAttempt;
      if (!joins) {
        if (pending && !pending.result) pending.interrupted = true;
        ensureRound(entry, 2);
      }
      if (pending.reopened) {
        // The first input after a resume, which follows any input of an earlier attempt of the round.
        pending.reopened = false;
        if (pending.requests.length) pending.resumedInputs.push(pending.requests.length);
      }
      anchor(pending, entry, 2);
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
      if (!round || round.result || round.interrupted && !entry.requestId) round = ensureRound(entry, 1);
      anchor(round, entry, 1);
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
      // A round is one visit of the critic step and starts with it, before the critic's input is
      // recorded. A reset visit number on resume does not mean a new author submission.
      inConvention = false;
      const number = positive(entry.visit);
      const latest = cycle && !cycle.closed ? cycle.rounds.at(-1) : null;
      visit = number;
      handoff = null;
      if (number && latest && !latest.result && latest.roundNumber === number) {
        // The same visit again (a Resume or a restart): it continues its interrupted round.
        pending = latest;
        latest.interrupted = false;
        latest.reopened = latest.resumed = true;
      } else {
        closePending();
        ensureRound(entry, 0);
      }
      // The read-only last round at the round limit (its step record names the verify instruction).
      if (entry.verify) pending.finalCheck = true;
    } else if (handoffEvent) {
      handoff = entry;
      visit = positive(entry.round) || positive(entry.manifest?.visit) || visit;
      if (opened() && (!pending.handoff || pending.reopened)) {
        // The proof captured for the visit that just opened (or reopened) this round.
        pending.handoff = entry;
        pending.roundNumber = visit || pending.roundNumber;
      } else {
        closePending();
        ensureRound(entry, 0);
      }
    } else if (["author_result", "final_result", "failure_result"].includes(entry.kind)
        || entry.kind === "request" && ["solve", "repair", "final"].includes(entry.stage)
        || workflowStep && entry.node !== "critic"
        || entry.kind === "workflow_paused" && entry.node === "author") {
      inConvention = false;
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
  const recorded = [];
  for (const current of cycles) {
    for (const round of current.rounds) {
      if (!round.result) round.outcome = !round.interrupted && current === cycle && !current.closed && active
        ? "pending" : "interrupted";
    }
    // A visit that ended before its coordinator was asked left no input or decision to show.
    current.rounds = current.rounds.filter(round => round.result || round.requests.length || round.outcome === "pending");
    if (!current.rounds.length) continue;
    recorded.push(current);
    current.number = recorded.length;
    const first = anchors.get(current.rounds[0]);
    current.aliases = current.key ? [] : first.earlier.map(earlier => `cycle:${earlier}`);
    current.key = uniqueKey(current.key || `cycle:${first.stamp}`);
    for (const [position, round] of current.rounds.entries()) {
      round.number = position + 1;
      round.key = uniqueKey(`round:${anchors.get(round).stamp}`);
      round.aliases = anchors.get(round).earlier.map(earlier => `round:${earlier}`);
    }
    const last = current.rounds.at(-1);
    current.outcome = last?.outcome || "pending";
    current.endTime = last?.endTime || null;
    current.accepted = ["pass_clean", "pass_edited_limit"].includes(current.outcome);
  }
  return recorded;
}

// The round a review cycle is in, numbered as the round badge and the terminal number it: the
// visit of the critic step (the job's round, from its proof handoff), else the latest round's
// recorded visit, else its place in the cycle.
function criticRoundNumber(cycle) {
  if (Number.isInteger(state.round) && state.round > 0) return state.round;
  return cycle?.rounds.at(-1)?.roundNumber || cycle?.rounds.length || 0;
}

// A round's name in the review history and the rail: its place in its review cycle (the visit
// of the critic step), marked when it continued after an interruption.
function criticRoundName(round, number = round?.number) {
  return `Round ${number}${round?.resumed ? " (resumed)" : ""}`;
}

function criticRoundOutcomeLabel(round) {
  round = round || {};
  if (round.outcome === "reject") return round.transition === "needs_revision"
    ? "Rejected — needs revision" : "Rejected — returned to author";
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

// The small progress line of a running round: what the critic's event stream shows of its independent
// critics (its subagents) in its latest attempt, never their text. Codex reports each critic's start and
// end for most calls; where it shows only that the critic waits for its critics, the line says no more
// than that they were started. Null when nothing was recorded.
function criticRoundProgress(round, count = state.criticCount) {
  const progress = round?.progress;
  if (!progress) return null;
  const total = Number.isInteger(progress.count) ? progress.count : Number.isInteger(count) ? count : null;
  const critics = total ? `its ${total} independent critic${total === 1 ? "" : "s"}` : "its independent critics";
  // A failed call's subagents are gone; its retry starts them again.
  if (criticRetryWaiting()) return `Waiting to retry a failed model call; ${critics} start again with it`;
  const entries = Object.entries(progress.started).map(([index, status]) => [Number(index), status])
    .sort((a, b) => a[0] - b[0]);
  if (entries.length) {
    return entries.map(([index, status]) => `${criticName(index, total)} ${status === "working" ? "working"
      : status === "failed" ? "stopped early" : "finished"}`).join(" · ");
  }
  // Waiting shows that it started them, not that it still waits (nor when each one ends).
  if (progress.waiting) return `The critic has started its independent critics${total ? ` (${total} requested)` : ""}`;
  return total ? `${total} independent critic${total === 1 ? "" : "s"} requested` : "Independent critics requested";
}

// "Critic i of x", or, for a critic beyond the x it was asked for (one started after another finished),
// "Critic i (beyond the x requested)".
function criticName(index, total) {
  if (!total) return `Critic ${index}`;
  return index <= total ? `Critic ${index} of ${total}` : `Critic ${index} (beyond the ${total} requested)`;
}

// Whether the critic's own failed model call waits to retry (state.modelRetry): its subagents ended with it.
function criticRetryWaiting() {
  const retry = state.modelRetry;
  return Boolean(retry && (retry.stage || "critic") === "critic" && !["command", "storage"].includes(retry.kind)
    && Number(retry.retryAt) > 0 && !state.error
    && ["running", "stopping"].includes(state.phase) && state.activeNode === "critic");
}

// The critic's one decision for a round, in words: a clean pass, a pass with fixes (what followed), or
// a rejection; with what it fixed, the critical bugs and the diff of its changes to the proof. `next` is
// the round after it in its review cycle.
function criticRoundDecision(round, next = null) {
  round = round || {};
  const report = round.result?.report || {};
  const changed = typeof report.proofChanged === "boolean" ? report.proofChanged : round.proofChanged;
  const text = (value) => typeof value === "string" ? value.trim() : "";
  const diff = {diff: typeof report.proofDiff === "string" ? report.proofDiff : "",
    diffFile: typeof report.proofDiffFile === "string" ? report.proofDiffFile : "",
    diffNote: typeof report.proofDiffNote === "string" ? report.proofDiffNote : "",
    diffLines: Number.isInteger(report.proofDiffLines) ? report.proofDiffLines : null,
    // An unchanged proof fixed nothing, whatever the fixes field says.
    changed: changed === true, fixes: changed === true ? text(report.fixes) : "", bugs: text(report.bugs)};
  if (round.outcome === "reject") {
    return {kind: "rejected", label: "Rejected", ...diff, follow: round.transition === "needs_revision"
      ? "The proof needs revision." : "The proof returns to the author with the critical bugs."};
  }
  if (round.outcome === "pass_clean") return {kind: "clean", label: "Clean pass", ...diff, changed: false,
    follow: "The proof is unchanged."};
  if (["pass_edited_continue", "pass_edited_limit"].includes(round.outcome) || round.outcome === "pass_unknown" && changed) {
    const follow = round.outcome === "pass_edited_continue"
      ? (next?.finalCheck ? "Another review round follows: the final check, which cannot edit the proof."
        : "Another review round follows.")
      : round.outcome === "pass_edited_limit" ? "Accepted at the round limit, without another critic round."
      : "The next step was not recorded.";
    return {kind: "fixes", label: "Pass with fixes", follow, ...diff};
  }
  if (round.outcome === "pending") {
    // A paused or stopping job keeps its round open, but nothing works on it.
    const halted = {paused: "Paused", pausing: "Pausing", stopping: "Stopping"}[state.phase];
    if (halted) {
      return {kind: "pending", halted: true, label: `${halted} before the critic decided.`,
        follow: state.phase === "stopping" ? "The round ends without a decision."
          : "On Resume, the round starts again with fresh independent critics."};
    }
    if (criticRetryWaiting()) return {kind: "pending", label: "The critic's model call failed; the round is retried."};
    return {kind: "pending", label: round.requests?.length ? "The critic is reviewing the proof." : "Not decided yet."};
  }
  if (round.outcome === "interrupted") return {kind: "unknown", label: "No recorded decision."};
  return {kind: "unknown", label: criticRoundOutcomeLabel(round)};
}

// The convention critic of the round that accepted the proof: its record, or that it is running.
function criticRoundConvention(round) {
  const record = round?.convention;
  if (record && ["faithful", "loophole"].includes(record.verdict)) {
    return {state: "finished", verdict: record.verdict, intended: record.intended, assessment: record.assessment,
      loophole: record.loophole, suggested: record.suggestedStatement};
  }
  if (round?.conventionState === "running") return {state: "running"};
  return null;
}

// The convention critic that has not answered yet, as the job's phase has it: checking, paused, or ended.
function conventionRunningText() {
  if (["running", "reviewing"].includes(state.phase)) return "Checking the accepted proof.";
  if (["paused", "pausing"].includes(state.phase)) {
    return `${state.phase === "paused" ? "Paused" : "Pausing"} before the convention critic finished; it checks `
      + "the accepted proof again on Resume.";
  }
  if (state.phase === "stopping") return "Stopping before the convention critic finished.";
  return "Check interrupted: the convention critic did not finish.";
}

// The parts of one round, shared by the critic review history and the critic result's activity card: the
// critic's one output (its decision; what it fixed; the critical bugs; the changes, collapsed), the progress
// of its independent critics while it runs, and the convention critic. `details(key, label, content,
// className)` makes a collapsed row (the history keeps rows open across redraws).
function criticRoundParts(round, next, details) {
  const part = (className, heading) => {
    const section = document.createElement("section");
    section.className = `critic-round-part ${className}`;
    const title = document.createElement("h4");
    title.textContent = heading;
    section.append(title);
    return section;
  };
  const line = (text, className = "") => {
    const paragraph = document.createElement("p");
    if (className) paragraph.className = className;
    paragraph.textContent = text;
    return paragraph;
  };
  const block = (text, className) => {
    const copy = document.createElement("pre");
    copy.className = className;
    copy.textContent = text;
    return copy;
  };
  const parts = [];
  const decision = criticRoundDecision(round, next);
  const output = part("critic-round-decision", "Coordinator decision");
  output.dataset.decision = decision.kind;
  output.append(line(decision.label, "critic-decision-label"));
  if (decision.follow) output.append(line(decision.follow, "critic-decision-follow"));
  if (decision.kind === "pending" && !decision.halted) {
    const progress = criticRoundProgress(round);
    if (progress) output.append(line(progress, "critic-round-progress"));
  }
  if (decision.fixes) output.append(line("Fixed:", "critic-round-term"), block(decision.fixes, "critic-decision-fixes"));
  if (decision.bugs) output.append(line("Critical bugs:", "critic-round-term"), block(decision.bugs, "critic-decision-bugs"));
  else if (decision.kind === "rejected") output.append(line("No separate rejection text was recorded.", "critic-round-muted"));
  if (decision.changed) {
    if (decision.diff) {
      output.append(details("diff", `Show changes to the proof${decision.diffLines ? ` (${decision.diffLines} diff lines)` : ""}`,
        decision.diff, "critic-round-diff"));
    } else if (decision.diffFile) {
      output.append(line(decision.diffNote || "The changes are too long to show here."));
      const download = document.createElement("button");
      download.type = "button";
      download.className = "secondary compact critic-diff-download";
      download.textContent = "Download the changes";
      download.dataset.diffFile = decision.diffFile;
      download.onclick = () => downloadArtifact(download, "diff", "critic-changes.diff", "/download-critic-diff",
        {file: decision.diffFile});
      output.append(download);
    } else output.append(line("The changes were not recorded for this round.", "critic-round-muted"));
  }
  parts.push(output);
  const convention = criticRoundConvention(round);
  if (convention) {
    const section = part("critic-round-convention", "Convention critic");
    section.dataset.verdict = convention.verdict || convention.state;
    section.append(line("Once the critic accepts a proof, it checks whether the proof does what you most likely "
      + "want from the problem, without exploiting a loophole in its wording. It only warns you; the run goes on "
      + "either way.", "critic-round-muted"));
    if (convention.state === "running") section.append(line(conventionRunningText()));
    else {
      section.append(line(convention.verdict === "loophole"
        ? "Loophole: the proof may exploit a loophole in the problem statement."
        : "Faithful: the proof does what the user most likely wants.", "critic-decision-label"));
      const fields = [["What the user most likely wants:", convention.intended],
        ["How the proof relates to it:", convention.assessment]];
      if (convention.verdict === "loophole") {
        fields.push(["How it exploits the statement:", convention.loophole], ["Suggested statement:", convention.suggested]);
      }
      for (const [term, value] of fields) {
        if (typeof value !== "string" || !value.trim()) continue;
        section.append(line(term, "critic-round-term"), line(value, "critic-round-value"));
      }
    }
    parts.push(section);
  }
  return parts;
}

function renderCriticReviewHistory() {
  const container = ui.criticReviewHistory;
  if (!container) return;
  const {cycles, rounds: reviews} = criticReviewModel();
  const runIdentity = `${state.batchId || ""}:${state.runId || ""}`;
  const signature = JSON.stringify([runIdentity, state.phase, state.activeNode, state.criticCount,
    state.authorSubagentsRecorded, criticRetryWaiting(), cycles]);
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
  const critics = Number.isInteger(state.criticCount) && state.criticCountRecorded !== false ? state.criticCount : 0;
  // The owner's question: the critic (the coordinator) starts the independent critics as its subagents,
  // receives all their reports and makes the round's one decision, which is what is shown. A job from
  // before the Author subagents setting ran its independent critics itself, before its coordinator.
  const combined = state.authorSubagentsRecorded === false
    ? `In each round ${critics || "several"} independent critic${critics === 1 ? "" : "s"} reviewed the proof, and the critic coordinator received ${critics === 1 ? "its report" : "all their reports"} and made the one decision shown here.`
    : critics === 1
    ? "In each round the critic starts one independent critic as its subagent, receives its report and makes the one decision shown here."
    : `In each round the critic starts ${critics || "several"} independent critics as its subagents, receives all ${critics || "their"} reports and makes the one decision shown here.`;
  explanation.textContent = `${combined} Each author submission starts a review cycle. Changes can lead to another round within that cycle; rejection returns to the author. Expand a round for what the critic fixed, the critical bugs, its changes to the proof and its exact input.`;
  const children = [heading, explanation];
  // A row stays open when its key changes (`aliases`: its earlier keys, see collectCriticReviewCycles).
  const details = (key, label, content, className = "critic-review-text", aliases = []) => {
    const detail = document.createElement("details");
    detail.className = className;
    detail.dataset.reviewKey = key;
    detail.open = expanded.has(key) || aliases.some(alias => expanded.has(alias));
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
      undefined, "critic-cycle", cycle.aliases);
    cycleRow.dataset.outcome = cycle.outcome;
    for (const [roundIndex, review] of cycle.rounds.entries()) {
    const result = review.result;
    const first = review.requests[0];
    const stamp = first?.time || result?.time;
    const row = details(review.key,
      `${criticRoundName(review)} · ${criticRoundOutcomeLabel(review)}${stamp ? ` · ${localDateTime(stamp)}` : ""}`,
      undefined, "critic-review", review.aliases);
    row.dataset.outcome = review.outcome;
    if (review.outcome === "pass_edited_limit") {
      const note = document.createElement("p");
      note.textContent = "The critic changed the proof. The round limit accepts that revision without another critic round; this is not a clean pass.";
      row.append(note);
    }
    if (!review.requests.length) {
      const missing = document.createElement("p");
      // A round starts with the critic's input; an interrupted round starts its independent critics again.
      missing.textContent = review.outcome !== "pending" ? "The coordinator's exact input was not recorded for this round."
        : state.phase === "running" ? "The critic is starting. Its exact input appears here once it is sent."
        : state.phase === "stopping" ? "The job is stopping before the critic was asked."
        : "Paused before the critic was asked. The round starts again, with fresh independent critics, when the job resumes.";
      row.append(missing);
    }
    // The coordinator's one output and the convention critic; then, collapsed, the coordinator's exact input
    // and its raw decision.
    row.append(...criticRoundParts(review, cycle.rounds[roundIndex + 1] || null,
      (key, label, content, className) => details(`${review.key}:${key}`, label, content, className)));
    for (const [attemptIndex, request] of review.requests.entries()) {
      const resumed = review.resumedInputs?.includes(attemptIndex) ? " after resume" : "";
      const label = `Coordinator's exact input${review.requests.length > 1 ? ` · attempt ${request.attempt || attemptIndex + 1}${resumed}` : ""}`;
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
      const raw = typeof result.text === "string" && result.text
        ? result.text : result.report ? JSON.stringify(result.report, null, 2) : "No decision text was recorded.";
      const decision = details(`${review.key}:decision`, "Coordinator's raw decision", raw);
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
  // Only a job with a web cutoff (-1 or a timestamp) shows the panel; with the cutoff off,
  // Run settings records the unrestricted search.
  show(ui.retrievalPanel, !live);
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
    : "All model web-search tools are disabled for this job, and its models' shell commands get no network access.";
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
    + (retrieval.attention ? ` ${retrieval.attention}` : "")
    + (retrieval.skippedRecords ? ` ${retrieval.skippedRecords} unreadable or foreign log record${retrieval.skippedRecords === 1 ? " was" : "s were"} skipped.` : "")
    + (retrieval.catchingUp ? " The log is still being read; the totals are catching up." : "")
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
  // The rail always shows the whole workflow; the stages this run did not select are muted. Older
  // jobs that did not record their stages derive them from their mode.
  const stages = state.workflowStages || (state.statementReviewOnly
    ? {review: true, author: false, critic: false, writing: false}
    : latexOnly ? {review: false, author: false, critic: false, writing: true}
    : {review: !startsAtAuthor && !criticResume, author: true, critic: true, writing: true});
  const stageOf = {statement_reviewer: "review", author: "author", critic: "critic", convention: "critic",
    latex_editor: "writing", latex_compile: "writing"};
  const offStage = name => name === "failure_summary" ? !(stages.author || stages.critic) : !stages[stageOf[name]];
  // The selected steps are numbered in the order they run.
  const numbers = new Map(["statement_reviewer", "author", "critic", "convention", "latex_editor", "latex_compile"]
    .filter(name => !offStage(name)).map((name, index) => [name, String(index + 1)]));
  // Labels of the steps that the server's graph of this run leaves out.
  const labels = {statement_reviewer: "Statement reviewer", author: "Proof author", critic: "Independent critic",
    convention: "Convention critic", latex_editor: "LaTeX editor", latex_compile: "Compile PDF",
    failure_summary: "Saved progress"};
  const seenInThisJob = new Set((state.trace || []).map(
    (entry) => entry.node || nodeFromStage(entry.stage)
  ));
  const seen = new Set(seenInThisJob);
  const authorStage = state.activeNode === "author" && ["solve", "repair"].includes(state.stage);
  // The step the run is at, as the rail shows it (see railNode).
  const activeStep = railNode(state.activeNode, stages);
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
  // Where the run is now, waits, paused or ended, and why (see workflowPosition).
  const position = workflowPosition(stages, authorRepairing);
  // A finished job from before the convention critic went past its step without running it.
  const conventionPredatesJob = state.phase === "done" && stages.critic && !seen.has("convention")
    && !trace.some(entry => entry.kind === "convention_result")
    && (["latex_editor", "latex_compile"].some(name => seen.has(name)) || state.resultKind === "proof");
  const makeNode = (name, overrides = {}) => {
    const item = overrides.item || nodes[name] || {label: labels[name], description: ""};
    const number = name === "failure_summary" ? "!" : numbers.get(name) || "–";
    const row = document.createElement("li");
    const unselected = offStage(name);
    const current = activeStep === name;
    const paused = current && ["paused", "pausing"].includes(state.phase);
    // A stop at the time limit is no pause: its step gets the clock (see jobTone).
    const limitStop = current && timeLimitStopped(state);
    const active = ["running", "reviewing", "stopping"].includes(state.phase) && current;
    const failed = state.phase === "done" && current
      && (state.error || name === "failure_summary");
    const settled = name === "critic"
      ? verdict === "reject" ? "rejected" : criticApproved ? "complete"
        : latestCritic || seen.has(name) ? "reviewed" : ""
      : seen.has(name) ? "complete" : "";
    const status = overrides.status ?? (limitStop ? "limit" : paused ? "paused" : active ? "active" : failed ? "failed"
      : (unselected || name === "convention" && conventionPredatesJob) && !seen.has(name) ? "disabled" : settled);
    row.className = `workflow-node ${status}`;
    row.dataset.node = name;
    if (status === "active") row.setAttribute("aria-current", "step");
    const dot = document.createElement("span");
    dot.className = "node-dot";
    // The time limit's clock is drawn by the stylesheet.
    dot.textContent = ["failed", "warning"].includes(status) ? "!" : status === "paused" ? "Ⅱ" : status === "limit" ? ""
      : status === "rejected" ? "×" : status === "reviewed" ? "·"
      : status === "complete" ? "✓" : status === "disabled" ? "–" : number;
    const copy = document.createElement("div");
    copy.className = "node-copy";
    const title = document.createElement("strong");
    title.textContent = item.label;
    copy.append(title);
    if (position?.node === name) {
      // "Now", "Paused here", "Stopped here" (at the time limit), "Ended here" or where it waits, with the
      // step or the reason.
      const mark = document.createElement("span"), tag = document.createElement("span");
      const reason = document.createElement("span");
      mark.className = `node-position ${position.kind}`;
      mark.setAttribute("aria-label", `${position.label}: ${position.text}`);
      tag.className = "position-tag";
      tag.textContent = position.label;
      reason.className = "position-text";
      reason.textContent = position.text;
      mark.append(tag, reason);
      copy.append(mark);
    }
    const description = document.createElement("span");
    const descriptions = {
      author: "Explore approaches and build a proof.",
      critic: stages.author === false
        ? "The critic starts independent critics on the supplied work and reports unresolved errors."
        : "The critic starts independent critics on the candidate and decides; rejected proofs return to the author.",
      convention: "Checks once whether the accepted proof does what you most likely want; it only warns.",
      latex_compile: "Compile, fix errors, and retry until a PDF is ready.",
    };
    description.textContent = descriptions[name] || item.description;
    copy.append(description);
    // The position mark says "Paused here" on the paused step ("Stopped here" at the time limit).
    let noteText = overrides.note || (paused && position?.node !== name
      ? (limitStop ? "Stopped at the time limit" : "Paused") : "");
    if (name === "author" && authorRepairing && current
        && ["running", "pausing", "paused", "stopping"].includes(state.phase)) {
      noteText = noteText ? `${noteText} · repairing critic objections` : "Repairing critic objections";
    }
    // The position mark says what the running (or paused) convention critic does; its note gives its verdict,
    // and points to the warning only while the page shows it.
    if (name === "convention" && !noteText && !unselected) {
      const latestConvention = trace.findLast(entry => entry.kind === "convention_result" && entry.root !== false);
      const warning = state.conventionWarning;
      const warned = Boolean(warning && typeof warning === "object" && !warning.dismissed && warning.loophole);
      noteText = active || paused ? "" : failed ? "Check interrupted"
        : latestConvention?.verdict === "loophole" ? (warned ? "May exploit a loophole: see the warning above"
          : `May exploit a loophole${warning?.dismissed ? " (warning ignored)" : ""}; see the critic review history`)
          : latestConvention?.verdict === "faithful" ? "Faithful to the statement"
            : conventionPredatesJob ? "Not run (older job)" : "";
    }
    // A paused critic step names the round that stays open, not the verdict of an earlier one.
    if (name === "critic" && !noteText && !unselected) {
      const round = currentCycle ? ` · cycle ${cycles.length}, round ${criticRoundNumber(currentCycle)}` : "";
      noteText = active ? `Reviewing candidate${round}` : paused ? `Candidate under review${round}`
        : failed ? "Review interrupted"
        : criticApproved ? criticCycleOutcomeLabel(currentCycle || {})
          : verdict === "reject" ? "Candidate needs repair"
            : verdict === "pass" ? criticRoundOutcomeLabel(latestRound)
              : seen.has(name) || latestCritic ? "Verdict unavailable" : "";
    }
    const loaded = criticResume && (name === "statement_reviewer"
      || (name === "author" && !seenInThisJob.has("author")));
    if (unselected && !noteText && !loaded) noteText = "Not selected for this run";
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
          item.textContent = `${criticRoundName(round, roundIndex + 1)}: ${criticRoundOutcomeLabel(round)}`;
          roundList.append(item);
        }
        entry.append(title, roundList);
        list.append(entry);
      }
      history.append(summary, list);
      copy.append(latest, passes, history);
    }
    if (loaded) {
      const note = document.createElement("span");
      note.className = "node-resume-note";
      note.textContent = "Loaded from the source job";
      copy.append(note);
    }
    row.append(dot, copy);
    return row;
  };
  const arrow = (text, pass = false) => {
    const row = document.createElement("li");
    row.className = `flow-arrow${pass ? " pass" : ""}`;
    row.textContent = `${text} ↓`;
    return row;
  };
  // A route or group that a step this run did not select needs is muted too.
  const element = (tag, className, off = false) => {
    const node = document.createElement(tag);
    node.className = className;
    if (off) node.classList.add("not-selected");
    return node;
  };

  const loop = document.createElement("li");
  loop.className = "workflow-loop";
  const loopTitle = document.createElement("strong");
  loopTitle.className = "loop-title";
  loopTitle.textContent = "Research and verification";
  const loopNodes = document.createElement("ol");
  loopNodes.className = "loop-nodes";
  const candidateRoute = element("li", "loop-forward", !stages.author || !stages.critic);
  candidateRoute.textContent = "Candidate or revised proof ↓";
  const failureNode = makeNode("failure_summary");
  failureNode.classList.add("failure-branch");
  const failureRoute = document.createElement("li");
  failureRoute.className = "failure-route";
  failureRoute.setAttribute(
    "aria-label",
    "Author and critic interruptions preserve saved work for continuation",
  );
  const rejectRoute = element("li", "loop-back", !stages.author || !stages.critic);
  rejectRoute.setAttribute("aria-label", stages.author && stages.critic
    ? `On rejection, step ${numbers.get("critic")} returns unresolved bugs to step ${numbers.get("author")}`
    : "On rejection, the run ends: the critic's report is saved");
  const rejectLabel = document.createElement("span");
  rejectLabel.textContent = "REJECT";
  rejectRoute.append(rejectLabel);
  const selfRoute = element("li", "loop-self", !stages.critic);
  selfRoute.textContent = `↻ PASS with changes → next round (limit ${state.criticRounds || 2})`;
  const author = makeNode("author");
  const critic = makeNode("critic");
  // Every accepted proof goes on to the convention critic.
  const passStem = element("li", "critic-pass-stem", !stages.critic);
  passStem.setAttribute("aria-hidden", "true");
  loopNodes.append(
    author, candidateRoute, critic, selfRoute, passStem, rejectRoute,
  );
  loop.append(loopTitle, loopNodes);
  if (!stages.author && !stages.critic) loop.classList.add("not-selected");

  // Accepted proofs go to the convention critic, then to Writing & polishing; interruptions retain saved
  // progress.
  const branch = document.createElement("li");
  branch.className = "workflow-branch";
  const passRoute = arrow(stages.critic ? "Unchanged PASS or edited PASS at limit"
    : stages.author ? "The author's proof, not reviewed" : "The supplied writing", true);
  passRoute.classList.add("critic-pass");
  // After the critic, the route leads to the convention critic; without it, straight on to writing.
  if (!(stages.critic || (stages.author && stages.writing))) passRoute.classList.add("not-selected");
  // The convention critic checks the accepted proof once, before writing or the end (rows set in styles.css).
  const convention = makeNode("convention");
  convention.classList.add("convention-node");
  const conventionRoute = arrow(stages.critic ? "Whatever its verdict" : "Next stage");
  conventionRoute.classList.add("convention-route");
  if (!stages.writing || !(stages.author || stages.critic)) conventionRoute.classList.add("not-selected");
  const writing = element("li", "workflow-loop writing-group", !stages.writing);
  const writingTitle = document.createElement("strong");
  writingTitle.className = "loop-title";
  writingTitle.textContent = "Writing & polishing";
  const writingNodes = document.createElement("ol");
  writingNodes.className = "group-nodes";
  writingNodes.append(makeNode("latex_editor"));
  if (nodes.latex_compile || !stages.writing) {
    const compileRoute = arrow("Compile and check");
    if (!stages.writing) compileRoute.classList.add("not-selected");
    writingNodes.append(compileRoute, makeNode("latex_compile"));
  }
  writing.append(writingTitle, writingNodes);
  branch.append(loop, passRoute, convention, conventionRoute, writing);
  failureRoute.className = "flow-arrow interruption-route";
  failureRoute.textContent = "On interruption or time limit ↓";
  branch.append(failureRoute, failureNode);

  const reviewer = makeNode("statement_reviewer");
  reviewer.classList.add("pre-loop");
  const approved = arrow("Approved");
  approved.classList.add("pre-loop-arrow");
  if (!stages.review) approved.classList.add("not-selected");
  ui.workflowNodes.replaceChildren(reviewer, approved, branch);
  // Only an attached, open list can scroll, so restore its position after the swap.
  if (criticHistoryList) criticHistoryList.scrollTop = criticHistoryScroll;
}

// Where the run stands on the workflow rail: the step working now, the step it waits or is paused at,
// the step the time limit stopped it at, or the step it ended at, with the reason in plain words.
// {node, kind, label, text}, or null.
function workflowPosition(stages, authorRepairing = false) {
  // Never a step this run did not select (see railNode).
  const current = railNode(state.activeNode, stages);
  const sentence = text => String(text || "").charAt(0).toUpperCase() + String(text || "").slice(1);
  const at = (node, kind, label, text) => (node ? {node, kind, label, text: sentence(text)} : null);
  if (state.phase === "done") {
    const outcome = completedRunOutcome(state);
    return at(railNode(outcome.node, stages) || current, "ended", "Ended here", outcome.ended);
  }
  if (timeLimitStopped(state)) return at(current, "limit", "Stopped here", pauseCauseText());
  if (state.phase === "paused") return at(current, "paused", "Paused here", pauseCauseText());
  if (["reviewing", "running", "stopping", "pausing"].includes(state.phase)) {
    const node = current || (state.phase === "reviewing" ? "statement_reviewer" : "");
    return at(node, "now", "Now", workflowStepText(node, authorRepairing));
  }
  if (state.phase === "reviewed") {
    return at("statement_reviewer", "waiting", "Waiting here", "Approve the checked statement to continue");
  }
  if (state.phase === "prepared") {
    const first = current || [["review", "statement_reviewer"], ["author", "author"], ["critic", "critic"],
      ["writing", "latex_editor"]].find(([stage]) => stages[stage])?.[1] || "";
    return at(first, "waiting", "Starts here", state.launchHold
      || (state.launchQueued ? "Queued: starts when a running slot is free" : "Ready to start"));
  }
  return null;
}

// The step that works now, as the rail's "Now" mark names it.
function workflowStepText(node, authorRepairing = false) {
  if (state.phase === "stopping") return "Stopping";
  if (state.phase === "pausing") return "Pausing: the current turn ends and the conversation is saved";
  const step = node === "statement_reviewer" ? "Checking the statement"
    : node === "author" ? (authorRepairing ? "Repairing critic objections" : "Working on the proof")
    : node === "critic" ? criticStepText()
    : node === "convention" ? "Checking the accepted proof"
    : node === "latex_editor" ? "Writing the LaTeX document"
    : node === "latex_compile" ? (state.activeNode === "latex_repair" ? "Fixing LaTeX errors" : "Compiling the PDF")
    : node === "failure_summary" ? "Saving progress" : "Working";
  if (node === "critic" && criticRetryWaiting()) {
    // The failed call's independent critics ended with it; the retry starts them again.
    const count = Number.isInteger(state.criticProgress?.count) ? state.criticProgress.count : 0;
    return `Waiting to retry a failed model call; its ${count ? `${count} ` : ""}independent critic`
      + `${count === 1 ? "" : "s"} start${count === 1 ? "s" : ""} again with it`;
  }
  const retrying = state.modelRetry && Number(state.modelRetry.retryAt) > 0 && !state.error;
  return retrying ? `${step} · waiting to retry ${retryWaitText(state.modelRetry)}` : step;
}

// The running critic round from the server's count (state.criticProgress): what the critic's event stream
// shows of its independent critics (its subagents), never their text, then the critic deciding once they
// have all finished. Codex reports each critic's start and end for most calls; where it shows only that
// the critic waits for its critics, the text says no more than that they were started. A critic started
// beyond the number requested is named as such, and the finished ones are counted among those started.
function criticStepText() {
  const progress = state.criticProgress;
  const count = Number.isInteger(progress?.count) ? progress.count : 0;
  if (!count) return "Reviewing the candidate";
  const numbers = key => (Array.isArray(progress[key]) ? progress[key] : [])
    .filter(index => Number.isInteger(index) && index >= 1);
  const started = numbers("started"), finished = numbers("finished").filter(index => started.includes(index));
  const failed = numbers("failed").filter(index => finished.includes(index));
  const critics = `${count} independent critic${count === 1 ? "" : "s"}`;
  if (!started.length) {
    return progress.waiting ? `Critic working · independent critics started (${count} requested)`
      : progress.requested ? `${critics} requested` : "Reviewing the candidate";
  }
  const early = failed.length ? ` (${failed.length} stopped early)` : "";
  const working = started.filter(index => !finished.includes(index));
  // Finished critics are counted among the requested ones, or among those started when it started more.
  const of = Math.max(count, started.length);
  if (!working.length) {
    // Every critic it started has finished: it decides, or starts the others it was asked for.
    return finished.length >= count
      ? `Critic coordinator deciding · ${finished.length === 1 ? "the critic finished" : `all ${finished.length} critics finished`}${early}`
      : `Critic coordinator working · ${finished.length} of ${count} critics finished${early}`;
  }
  const beyond = working.some(index => index > count);
  const names = working.length === count && count > 1 && !beyond ? `${count} critics`
    : working.length <= 2 ? working.map(index => criticName(index, count)).join(" and ")
    : beyond ? `Critics ${working.slice(0, -1).join(", ")} and ${working.at(-1)} (${count} requested)`
    : `Critics ${working.slice(0, -1).join(", ")} and ${working.at(-1)} of ${count}`;
  return `${names} working${finished.length ? ` · ${finished.length} of ${of} finished${early}` : ""}`;
}

// Why a paused job (or one stopped at its time limit) stopped where it did, in plain words.
function pauseCauseText() {
  const retry = state.autoResumeAt > 0
    ? `; it retries by itself at ${new Date(state.autoResumeAt * 1000).toLocaleTimeString()}` : "";
  const error = String(state.error || "").trim().replace(/\.$/, "");
  if (state.pauseCause === "time-limit") {
    const reached = (Array.isArray(state.timeLimitHistory) ? state.timeLimitHistory : [])
      .findLast(item => item.event === "reached");
    // Continue waits for the automatic progress summary; a saved batch's job is read only.
    return `Time limit reached (${hoursText(Number(reached?.hours ?? state.thinkingHours ?? 0))})`
      + (state.readOnly ? "" : limitSummaryWriting(state) ? " · writing progress summary" : "; you can continue");
  }
  if (state.pauseCause === "user") return "Paused by you";
  if (state.pauseCause === "interrupted") return `The run process ended part-way${retry}`;
  if (state.pauseCause === "restart") return `Paused when the server closed${retry}`;
  // A saved batch's job is read only: there is no Resume to offer.
  return error ? `${error}${retry}` : `The work so far is saved${state.readOnly ? "" : "; Resume continues it"}`;
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
  renderActivityAge();
  renderAuthorLimitHint();
}

// The now-card says whether a working job is stuck: when its last event arrived and, while a failed
// model call, a command that could not run or a file write waits to retry (state.modelRetry), since
// when it retries. Written only on change.
function renderActivityAge() {
  const working = ["reviewing", "running", "stopping", "pausing"].includes(state.phase);
  const retry = state.modelRetry && Number(state.modelRetry.retryAt) > 0 && !state.error ? state.modelRetry : null;
  const since = new Date(retry?.since || NaN);
  const ago = state.lastActivityAt ? Math.max(0, Math.floor((Date.now() - new Date(state.lastActivityAt)) / 1000)) : NaN;
  const text = !working ? "" : [
    retry ? `Waiting to retry ${retryWaitText(retry)} (retries every minute`
      + (Number.isFinite(since.getTime()) ? ` since ${since.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })})` : ")")
      : "",
    Number.isFinite(ago) ? `Last activity ${ago < 2 ? "just now" : `${durationText(ago)} ago`}` : "",
  ].filter(Boolean).join(" · ");
  if (ui.runActivityAge.textContent !== text) ui.runActivityAge.textContent = text;
  ui.runActivityAge.hidden = !text;
  ui.runActivityAge.dataset.retry = retry ? "1" : "";
}

// Under the time limit field: the active time so far, and what a limit at or below it does. The field
// keeps the active seconds and whether the author runs, which is all Set limit reads.
function renderAuthorLimitHint() {
  const active = runElapsedSeconds();
  const raw = String(ui.authorLimitHours.value ?? "").trim();
  const hours = raw === "" ? NaN : Number(raw);
  const below = hours > 0 && hours * 3600 <= active;
  const text = `Active time so far: ${durationText(active)}.`
    + (below && ["running", "pausing"].includes(state.phase) ? " A limit at or below this stops the author at its next step."
      : below && state.phase === "paused"
        ? ` Set more than this, or 0 for no limit, to ${state.pauseCause === "time-limit" ? "continue" : "resume"}.` : "");
  if (ui.authorLimitHint.textContent !== text) ui.authorLimitHint.textContent = text;
  ui.authorLimitHours.dataset.elapsed = String(Math.floor(active));
  ui.authorLimitHours.dataset.running = state.phase === "running" ? "1" : "";
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
  const merged = incremental ? [...(state.trace || []), ...newEntries] : newEntries;
  const combined = retainTrace(merged);
  // This page drops older routine events too: the notice then says so, as for the server's window.
  const trimmed = combined.length < merged.length || (incremental && Boolean(state.traceTrimmed));
  state = { ...next, trace: combined, traceTrimmed: trimmed };
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
  // The job's own error is stated once, in the now-card. While the run panel is hidden
  // (a failed statement review returns to the composer) the notice states it instead.
  // Otherwise the notice keeps action feedback until the phase changes.
  if (ui.run.hidden && state.error) {
    if (ui.notice.textContent !== state.error) ui.notice.textContent = state.error;
    show(ui.notice, true);
  } else if (phase !== previousPhase) show(ui.notice, false);
  // Older routine events left the live window (the server's or this page's own): say so, and
  // offer the job's complete transcript.jsonl.
  const truncated = Boolean(state.traceTruncated || state.traceNotice || state.traceTrimmed);
  ui.traceNotice.textContent = state.traceNotice || "Older routine events are no longer shown here; every "
    + "prompt, result and error stays listed. The complete history is in this job's transcript.jsonl.";
  show(ui.traceNotice, truncated);
  show(ui.downloadTranscript, truncated);

  if (phase === "input" && previousPhase !== "input") {
    const rounds = state.workflow?.settings?.critic_rounds || {};
    const critics = state.workflow?.settings?.critic_count || {};
    const hours = state.workflow?.settings?.thinking_hours || {};
    // "Rerun with suggested statement" (see rerunWithStatement): that statement, with the job's settings.
    const seed = typeof composerSeed === "undefined" ? null : composerSeed;
    if (seed) composerSeed = null;
    const source = seed ? { ...state, ...seed.settings } : state;
    ui.problem.value = seed ? seed.statement : state.originalContent || state.draft || state.latexInput || "";
    syncModelMenus();
    setChoice(ui.reviewModel, source.reviewModel || "gpt-6.1-sol");
    setChoice(ui.authorModel, source.authorModel || "gpt-6.1-sol");
    setChoice(ui.criticModel, source.criticModel || "gpt-6.1-sol");
    setChoice(ui.writerModel, source.writerModel || "gpt-6.1-sol");
    ui.reviewEffort.value = source.reviewEffort || "max";
    ui.authorEffort.value = source.authorEffort || source.reasoningEffort || "max";
    ui.criticEffort.value = source.criticEffort || source.reasoningEffort || "max";
    ui.writerEffort.value = source.writerEffort || source.reasoningEffort || "max";
    ["review", "author", "critic", "writer"].forEach(syncEffortChoices);
    ui.speedMode.value = source.speedMode || "standard";
    ui.reasoningSummary.value = source.reasoningSummary || "concise";
    restoreRetrievalSettings(source);
    syncPrompts(source);
    if (seed) {
      // The job's own prompts go with the new problem as edits of the defaults.
      const defaults = state.workflow?.settings?.prompts || {};
      for (const [name, prompt] of Object.entries(promptValues)) {
        if (prompt && prompt.trim() !== (defaults[name] || "").trim()) promptOverrides[name] = prompt.trim();
      }
    }
    updateModelSummary();
    ui.criticRounds.value = source.criticRounds || 2;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.criticCount.value = source.criticCount || 3;
    ui.criticCount.min = critics.minimum || 1;
    ui.criticCount.max = critics.maximum || 10;
    const subagents = state.workflow?.settings?.author_subagents || {};
    ui.authorSubagents.value = Number.isInteger(source.authorSubagents) ? source.authorSubagents : subagents.default ?? 0;
    ui.authorSubagents.min = subagents.minimum ?? 0;
    ui.authorSubagents.max = subagents.maximum ?? 10;
    ui.thinkingHours.value = source.thinkingHours ?? 0;
    ui.thinkingHours.min = hours.minimum ?? 0;
    // No maximum: any positive number of hours is a valid limit.
    ui.thinkingHours.max = hours.maximum ?? "";

    restoreWorkflowStages(source);
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
    // A revision that failed comes back here with its feedback, ready to send again.
    ui.feedback.value = state.error && state.reviewFeedback ? state.reviewFeedback : "";
    ui.criticRounds.value = state.criticRounds || 2;
    ui.criticCount.value = state.criticCount || 3;
    ui.authorSubagents.value = Number.isInteger(state.authorSubagents) ? state.authorSubagents : 0;
    ui.thinkingHours.value = state.thinkingHours ?? 0;
    restoreWorkflowStages(state);
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
  // A stop at the time limit is not a pause: it says so everywhere, and a higher limit lets the author
  // continue (see jobTone).
  const timeLimitPause = timeLimitStopped(state);
  ui.liveDot.classList.toggle("active", working);
  ui.globalStatus.textContent = phase === "input" ? "Ready"
    : phase === "prepared" ? "Ready to start" : timeLimitPause ? "Time limit reached"
    : phase === "paused" ? "Paused" : phase === "pausing" ? "Pausing"
    : phase === "reviewed" ? "Waiting for approval"
      : phase === "stopping" ? "Stopping"
        : done ? completedRunLabel(state) : (node.label || "Model is working");
  ui.runLabel.textContent = phase === "prepared" ? "PREPARED RUN" : timeLimitPause ? "TIME LIMIT REACHED"
    : phase === "paused" ? "RUN PAUSED"
    : done ? completedRunLabel(state).toUpperCase() : (node.short_label || "MODEL");
  // A short status title; the error, when there is one, is the description below it.
  ui.runTitle.textContent = phase === "reviewing" ? "Checking the statement…"
    : phase === "prepared" ? "Prepared research workspace"
    : phase === "paused" ? (timeLimitPause ? "Time limit reached" : "Ready to resume") : phase === "pausing" ? "Pausing…"
    : phase === "stopping" ? "Stopping safely…"
      : done ? completedRunLabel(state) : (node.label || "Working…");
  // The header already says what happens next; the now-card adds only the error or what its buttons need.
  ui.runDescription.textContent = timeLimitPause ? timeLimitDescription()
    : (done && state.manuallyStopped ? "You stopped this job." : state.error)
    || (phase === "prepared" ? (state.launchHold || "Review the prepared statement and settings, then start a fresh author session in this workspace.")
      : phase === "paused" ? "Resume reopens the saved author conversation in this same run folder. You can update your CLI login or API model settings before resuming."
      : phase === "pausing" ? "Interrupting the current turn and preserving the saved conversation. Wait for Paused before closing the UI."
      : "");
  const replyBusy = Boolean(state.conversation?.busy);
  if (phase === "paused" && !timeLimitPause && state.conversation?.available && !state.readOnly && !replyBusy) {
    ui.runDescription.textContent += " You can also ask the author questions below.";
  }
  if (phase === "paused" && state.autoResumeAt) {
    // While the author replies, the automatic retry waits for the reply (a reply that keeps failing is stopped).
    const retryTime = new Date(state.autoResumeAt * 1000).toLocaleTimeString();
    ui.runDescription.textContent += replyBusy && state.autoResumeAt * 1000 <= Date.now()
      ? " The automatic retry follows the author's reply. Choose Keep paused to cancel retries."
      : ` Automatic retry at ${retryTime}. Choose Keep paused to cancel retries.`;
  }
  // A failed model call waits to retry; the job keeps running meanwhile.
  if (["reviewing", "running"].includes(phase) && state.modelRetry?.text && !state.error) {
    const next = Number(state.modelRetry.retryAt);
    ui.runDescription.textContent = state.modelRetry.text
      + (Number.isFinite(next) && next > 0 ? ` Next attempt at ${new Date(next * 1000).toLocaleTimeString()}.` : "");
  }
  show(ui.roundBadge, Boolean(state.round && ["critic", "author"].includes(state.activeNode)));
  ui.roundBadge.textContent = roundBadgeText(state);
  const authorLimit = Number(state.thinkingHours ?? 0);
  const maximumAuthorLimit = state.workflow?.settings?.thinking_hours?.maximum ?? null;
  const authorRunning = phase === "running"
    && ["solve", "repair"].includes(state.stage) && state.activeNode === "author";
  // The limit can change until the job finishes (the review page has no control); while the critic
  // or LaTeX steps run it applies at the author's next step, and while paused at Resume.
  const canSetAuthorLimit = !state.readOnly && state.workflowStages?.author !== false
    && ["reviewing", "prepared", "running", "pausing", "paused"].includes(phase);
  // While paused the box asks the author in its saved conversation; one reply is written at a time.
  const conversation = state.conversation || {};
  const replying = phase === "paused" && Boolean(conversation.busy);
  const canTalk = phase === "paused" && Boolean(conversation.available) && !state.readOnly;

  show(ui.authorSteerControl, authorRunning || canTalk);
  renderAuthorSteerMode(canTalk && (timeLimitPause ? "limit" : "paused"));
  ui.sendAuthorSteer.disabled = !(authorRunning || (canTalk && !replying));
  show(ui.requestSummary, canTalk && !replying);
  show(ui.stopReply, replying && !state.readOnly);
  show(ui.authorTimeLimitControl, canSetAuthorLimit);
  ui.authorLimitSummary.textContent = authorLimit === 0 ? "No time limit" : `Time limit: ${limitHoursText(authorLimit)}`;
  ui.authorLimitHours.max = maximumAuthorLimit === null ? "" : String(maximumAuthorLimit);
  if (canSetAuthorLimit && document.activeElement !== ui.authorLimitHours) {
    // A limit typed but not set yet stays (Resume sets it first); otherwise the field follows the job.
    const typed = authorLimitField.run === state.runId && ui.authorLimitHours.value !== authorLimitField.value
      && Number(ui.authorLimitHours.value) !== authorLimit;
    if (!typed) {
      ui.authorLimitHours.value = String(authorLimit);
      authorLimitField = { run: state.runId, value: ui.authorLimitHours.value };
    }
  }
  ui.setAuthorTimeLimit.disabled = !canSetAuthorLimit;
  // The time limit control opens by itself once per run, at the pause the limit caused.
  if (timeLimitPause && canSetAuthorLimit && limitControlOpenedFor !== state.runId) {
    limitControlOpenedFor = state.runId;
    ui.authorTimeLimitControl.open = true;
  }
  const retryPending = phase === "paused" && Boolean(state.autoResumeAt);
  show(ui.pause, authorRunning || retryPending);
  ui.pause.textContent = retryPending ? "Keep paused" : "Pause";
  ui.pause.title = retryPending ? "Cancel automatic retries"
    : "Pause the author";
  show(ui.resume, phase === "paused" || phase === "prepared");
  // After a stop at the time limit the work continues (raising the limit first); a pause resumes.
  ui.resume.textContent = state.launchHold ? "Awaiting review" : phase === "prepared" ? "Start prepared run"
    : timeLimitPause ? "Continue" : "Resume";
  ui.resume.disabled = Boolean(state.launchHold) || replying;
  ui.resume.title = replying ? "The author is replying. Wait for the reply or stop it." : "";
  show(ui.downloadTex, Boolean(state.canDownloadTex));
  show(ui.downloadPdf, Boolean(state.canDownloadPdf));
  // Saved batches serve only final.tex and final.pdf.
  show(ui.downloadProof, Boolean(state.canDownloadProof && !state.readOnly));
  show(ui.downloadSource, Boolean(state.canDownloadSource && !state.readOnly));
  show(ui.stop, working);
  ui.stop.disabled = phase === "stopping";
  ui.run.setAttribute("aria-busy", String(working));

  renderWorkflow();
  renderClock();
  renderBatchNavigation();
  // After the overview is shown, so the conversation can scroll to its newest item.
  renderAuthorConversation();
  renderRelatedWorksPanel();
  renderAuthorQuestion();
  if (state.readOnly || state.runningElsewhere) {
    for (const control of [ui.stop, ui.pause, ui.resume, ui.authorSteerControl, ui.authorTimeLimitControl,
      ui.approve, ui.recheck,
      ui.reviewFeedbackControls]) show(control, false);
    ui.proposed.readOnly = true;
    // Saved batches cannot resume; do not promise an action that is not offered.
    if (phase === "paused") {
      ui.runTitle.textContent = timeLimitPause ? "Time limit reached" : "Paused";
      ui.runDescription.textContent = state.error || "";
    }
    // Another process owns the run: say so instead of offering its controls.
    if (state.runningElsewhere) ui.runDescription.textContent = state.runningElsewhere;
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
  // A reply being written is followed as closely as running work.
  if (working || waiting) timer = setTimeout(refresh, working || replying ? 700 : 2500);
  clearInterval(clock);
  if (working || waiting) clock = setInterval(renderClock, 1000);
}

// The round badge: the job's critic round (the visit of the critic step) out of the edited rounds
// allowed; the round after them is the read-only final check.
function roundBadgeText(job) {
  return job.round > job.criticRounds ? `Round ${job.round} · final check` : `Round ${job.round} / ${job.criticRounds}`;
}

// The job page's time-limit text, steer box mode, author conversation and related works, as render()
// shows them. Without their data they only hide themselves (no rows are built).
let limitControlOpenedFor = "";
let authorLimitField = { run: null, value: "" };
let authorSteerPaused = false;
let conversationSignature = "";
let relatedWorksSignature = "";
let relatedWorksRun = null;

// A time limit as the server writes it: "1 hour", "1.5 hours", "1,000 hours", "0.003 hours".
function limitHoursText(hours) {
  return hoursText(hours);
}

// The now-card at a time-limit pause (a stop at the time limit): why it stopped, any summary being written, and
// what to do.
function timeLimitDescription() {
  const hours = Number(state.thinkingHours ?? 0), active = runElapsedSeconds();
  const summarizing = (state.conversation?.items || [])
    .some((item) => item.kind === "summary" && ["queued", "running"].includes(item.status));
  const text = state.timeLimitExhausted
    ? `The proof author reached the time limit of ${limitHoursText(hours)} after ${durationText(active)} of active `
      + `work and stopped.${summarizing ? " It is writing a progress summary below." : ""} Raise the time limit to continue.`
    : hours === 0 ? "The time limit is now off. Choose Continue to go on."
    : `The time limit is now ${limitHoursText(hours)} (${durationText(Math.max(0, hours * 3600 - active))} left). `
      + "Choose Continue to go on.";
  return text + (state.criticFeedbackPending
    ? " The critic rejected the latest proof; the author receives its report when the run continues." : "");
}

// The steer box guides the running author, or asks the paused one, or the one stopped at its time limit
// ("paused", "limit"; false while it runs). The texts change with the mode only.
function renderAuthorSteerMode(paused) {
  if (paused === authorSteerPaused) return;
  authorSteerPaused = paused;
  ui.authorSteerLabel.textContent = paused === "limit" ? "Ask the author" : paused ? "Ask the paused author"
    : "Guide the running proof author";
  ui.authorSteerInstruction.placeholder = paused ? "For example: which step in the summary is the main obstacle?"
    : "For example: try a potential-function argument before the case analysis.";
  ui.sendAuthorSteerText.textContent = paused ? "Send" : "Send instruction now";
}

// Conversation times are epoch seconds; "" when unknown.
function conversationClock(value, seconds = false) {
  const date = new Date(typeof value === "number" ? value * 1000 : value || NaN);
  if (!value || !Number.isFinite(date.getTime())) return "";
  return seconds ? date.toLocaleTimeString() : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// One summary or message with its reply, and where it stands.
function conversationEntry(item, criticNote) {
  const row = document.createElement("li");
  row.className = "conversation-item";
  row.dataset.status = item.status || "queued";
  const line = (className, text) => {
    const node = document.createElement(className === "conversation-reply" ? "div" : "p");
    node.className = className;
    node.textContent = text;
    row.append(node);
  };
  const summary = item.kind === "summary";
  line("conversation-meta", (summary
    ? (item.origin === "time-limit" ? ["Progress summary at the time limit", conversationClock(item.createdAt)]
      : ["Progress summary", conversationClock(item.createdAt), "requested"])
    : ["You", conversationClock(item.createdAt)]).filter(Boolean).join(" · "));
  if (!summary) line("conversation-text", item.text || "");
  if (item.reply) {
    if (!summary) line("conversation-meta", ["Author", conversationClock(item.finishedAt)].filter(Boolean).join(" · "));
    line("conversation-reply", item.reply);
  }
  const retryAt = conversationClock(item.retry?.retryAt, true);
  const status = item.status === "queued" ? (item.error ? `Waiting to start: ${item.error}` : "Waiting to start…")
    : item.status === "running" ? (item.retry?.text ? `${item.retry.text}${retryAt ? ` Next attempt at ${retryAt}.` : ""}`
      : summary ? "The author is writing the summary…" : "The author is replying…")
    : item.status === "failed" ? (item.error ? `No reply: ${item.error}` : "No reply.")
    : item.status === "stopped" ? "Reply stopped." : "";
  if (status) line("conversation-status", status);
  if (criticNote) {
    line("conversation-status", "The author has not seen the critic's latest report yet; it receives it when the run resumes.");
  }
  return row;
}

// Every summary, message and reply of the run, oldest first, in every phase; shown with items or the
// paused input. Rebuilt only when an item changes, then scrolled to the newest.
function renderAuthorConversation() {
  const conversation = state.conversation;
  const items = Array.isArray(conversation?.items) ? conversation.items : [];
  const canTalk = state.phase === "paused" && Boolean(conversation?.available) && !state.readOnly;
  // A saved conversation too large to show is named instead of listed.
  const notice = typeof conversation?.notice === "string" ? conversation.notice : "";
  show(ui.authorConversation, items.length > 0 || canTalk || Boolean(notice));
  if (!items.length && !canTalk && !notice) return;
  const authorRunning = state.phase === "running" && ["solve", "repair"].includes(state.stage)
    && state.activeNode === "author";
  const note = notice || (state.phase === "paused"
    ? (canTalk ? `The author answers in its saved conversation${conversation.readOnly === false ? "" : " without changing files"}, `
      + "and remembers the exchange when the run resumes. Replies do not use the time limit." : conversation?.reason || "")
    : authorRunning ? "Live instructions reach the author at its next step; it does not reply here." : "");
  if (ui.authorConversationNote.textContent !== note) ui.authorConversationNote.textContent = note;
  show(ui.authorConversationNote, Boolean(note));
  const pending = state.phase === "paused" && Boolean(state.criticFeedbackPending);
  const newestSummary = pending ? items.filter((item) => item.kind === "summary").at(-1) : null;
  const signature = JSON.stringify([items, newestSummary?.id || ""]);
  if (signature === conversationSignature) return;
  conversationSignature = signature;
  const rows = items.map((item) => conversationEntry(item, item === newestSummary));
  ui.authorConversationList.replaceChildren(...rows);
  ui.authorConversationList.scrollTop = ui.authorConversationList.scrollHeight;
  // The box opens at its newest item; a link above it leads to the newest summary written at the time limit.
  const limitSummary = items.findLastIndex((item) => item.kind === "summary" && item.origin === "time-limit");
  show(ui.conversationSummaryLink, limitSummary >= 0);
  ui.conversationSummaryLink.onclick = limitSummary < 0 ? null : () => {
    const list = ui.authorConversationList, row = rows[limitSummary];
    list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top;
    row.focus?.({ preventScroll: true });
  };
  if (limitSummary >= 0) {
    rows[limitSummary].tabIndex = -1;
    const time = conversationClock(items[limitSummary].createdAt);
    ui.conversationSummaryLink.textContent = `Go to the progress summary at the time limit${time ? ` (${time})` : ""}`;
  }
}

// The run page's related works: what the author's related-works folder holds and, while the job can
// take more, Add files… and Add a note. Shown when the job has any, or can get some.
function renderRelatedWorksPanel() {
  const works = state.relatedWorks;
  const items = Array.isArray(works?.items) ? works.items : [];
  const count = Number(works?.count ?? items.length) || 0;
  const visible = Boolean(works) && (count > 0 || Boolean(works.available));
  show(ui.relatedWorksPanel, visible);
  if (!visible) return;
  if (relatedWorksRun !== state.runId) {
    // Another job: its own status line and an empty note form.
    relatedWorksRun = state.runId;
    ui.relatedWorksStatus.textContent = "";
    show(ui.relatedNoteForm, false);
    ui.addRelatedNote.setAttribute("aria-expanded", "false");
  }
  const heading = count ? `Related works · ${count}` : "Related works";
  if (ui.relatedWorksHeading.textContent !== heading) ui.relatedWorksHeading.textContent = heading;
  const jobPhase = state.phase;
  // A job of a folder run adds works for every job of the run on the batch page.
  const note = !works.available ? works.reason || ""
    : (!state.goalThreadId && ["reviewing", "reviewed", "prepared"].includes(jobPhase) ? "The author's first prompt points to these files."
      : jobPhase === "running" ? "Saved in the author's related-works folder. The author is told about new files at its next step."
      : ["pausing", "paused"].includes(jobPhase)
        ? "Saved in the author's related-works folder. The author is told about new files with your next message or when the run resumes."
      : "Saved in the author's related-works folder, which related-works/INDEX.md lists.")
      + (state.folderBatch ? " Works for every problem of this folder run are added under Batch related works on "
        + "the batch page." : "");
  if (ui.relatedWorksNote.textContent !== note) ui.relatedWorksNote.textContent = note;
  show(ui.relatedWorksNote, Boolean(note));
  show(ui.relatedWorksEmpty, !items.length);
  show(ui.relatedWorksList, items.length > 0);
  const signature = JSON.stringify(items);
  if (signature !== relatedWorksSignature) {
    relatedWorksSignature = signature;
    ui.relatedWorksList.replaceChildren(...items.map((item) => relatedWorkRow(item)));
  }
  // A read-only job (a saved batch, or one another process runs) lists its works and adds none.
  const actions = Boolean(works.available) && !state.readOnly && !state.runningElsewhere;
  show(ui.relatedWorksActions, actions);
  if (!actions) show(ui.relatedNoteForm, false);
}

// The running author's open question (state.authorQuestion; the server closes it by its rules): each
// question with its options, the first preselected, and a field whose text replaces the option. The rows
// are rebuilt only for another question, so a choice or an answer being written survives the polls.
let authorQuestionSignature = "";
// The job and question the rows were built for; the question that needs no closing notice (this page
// answered or dismissed it, or the notice was shown for it).
let authorQuestionRows = null, authorQuestionSettled = "";
// While the author still works (a newer question replaced it, or it closed after its turn and ten minutes) the
// answer can go as a live instruction; once the author has stopped working, it cannot.
const AUTHOR_QUESTION_LOST = "The author's question closed before your answer was sent; you can still send it "
  + "as a live instruction.";
const AUTHOR_QUESTION_ENDED = "The author's question closed when the author stopped working; your answer was "
  + "not sent.";
function renderAuthorQuestion() {
  const question = state.authorQuestion;
  const visible = Boolean(question?.id) && Array.isArray(question.questions) && question.questions.length > 0
    && !state.readOnly && !state.runningElsewhere;
  // Another job's rows are left behind with that job.
  if (authorQuestionRows && authorQuestionRows.runId !== state.runId) {
    authorQuestionRows = null;
    authorQuestionSignature = "";
    ui.authorQuestionList.replaceChildren();
  }
  // It closed, or a newer question replaced it, while an answer was being written: say so once.
  const rows = authorQuestionRows;
  if (rows && rows.id !== authorQuestionSettled && (!visible || question.id !== rows.id)
      && authorQuestionWritten()) {
    authorQuestionSettled = rows.id;
    const authorRunning = state.phase === "running" && ["solve", "repair"].includes(state.stage)
      && state.activeNode === "author";
    ui.notice.textContent = authorRunning ? AUTHOR_QUESTION_LOST : AUTHOR_QUESTION_ENDED;
    show(ui.notice, true);
  }
  show(ui.authorQuestion, visible);
  if (!visible) return;
  // When it closes unanswered: once the author's step that asked it has ended, and not before closesAt.
  const asked = conversationClock(question.askedAt), closes = conversationClock(question.closesAt);
  const meta = [asked ? `Asked at ${asked}.` : "", question.turnEnded
    ? `The author has finished the step in which it asked; unanswered, this question closes${closes ? ` at ${closes}` : ""}.`
    : `Unanswered, it closes once the author has finished the step in which it asked${closes ? `, at ${closes} at the earliest` : ""}.`]
    .filter(Boolean).join(" ");
  if (ui.authorQuestionMeta.textContent !== meta) ui.authorQuestionMeta.textContent = meta;
  // With several questions, one can be left out of the answer: its chosen option can be cleared.
  show(ui.authorQuestionSkipHint, question.questions.length > 1
    && question.questions.some((entry) => Array.isArray(entry.options) && entry.options.length > 0));
  const signature = JSON.stringify([state.runId, question.id, question.questions]);
  if (signature === authorQuestionSignature) return;
  authorQuestionSignature = signature;
  authorQuestionRows = { runId: state.runId, id: question.id };
  ui.authorQuestionList.replaceChildren(...question.questions.map(authorQuestionRow));
}

// One question: its title, its options as buttons (at most one chosen) and its own answer field.
function authorQuestionRow(entry, index) {
  const row = document.createElement("li");
  row.className = "author-question-item";
  row.dataset.index = String(index);
  const title = document.createElement("p");
  title.className = "author-question-title";
  title.id = `authorQuestionTitle${index}`;
  title.textContent = entry.title || "";
  row.append(title);
  const options = Array.isArray(entry.options) ? entry.options : [];
  if (options.length) {
    const group = document.createElement("div");
    group.className = "question-options";
    group.setAttribute("role", "group");
    group.setAttribute("aria-labelledby", title.id);
    options.forEach((option, number) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "question-option";
      button.textContent = option;
      button.dataset.option = option;
      button.setAttribute("aria-pressed", String(number === 0));
      // A click on the chosen option clears it: the question is then left unanswered.
      button.onclick = () => {
        const choose = button.getAttribute("aria-pressed") !== "true";
        for (const other of group.children) other.setAttribute("aria-pressed", String(choose && other === button));
      };
      group.append(button);
    });
    row.append(group);
  }
  const label = document.createElement("label"), field = document.createElement("textarea");
  field.id = `authorAnswer${index}`;
  field.className = "author-answer";
  field.rows = 2;
  label.htmlFor = field.id;
  label.textContent = options.length ? "Or write your own answer" : "Your answer";
  row.append(label, field);
  return row;
}

// The answered questions as the server takes them: the written answer, else the chosen option; a
// question with neither is left out.
function authorQuestionAnswers() {
  return [...ui.authorQuestionList.querySelectorAll(".author-question-item")].map((row) => {
    const written = String(row.querySelector("textarea")?.value || "").trim();
    const chosen = [...row.querySelectorAll(".question-option")]
      .find((button) => button.getAttribute("aria-pressed") === "true");
    return { index: Number(row.dataset.index), answer: written || chosen?.dataset.option || "" };
  }).filter((entry) => entry.answer);
}

// Whether a field of the question on screen holds written text.
function authorQuestionWritten() {
  return [...ui.authorQuestionList.querySelectorAll("textarea")].some((field) => String(field.value || "").trim());
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
  const order = ["low", "medium", "high", "xhigh", "max"];
  const known = state.workflow?.settings?.model_efforts;
  const select = ui[`${role}Effort`];
  // Ultra is no longer offered: a saved job or a script that says ultra runs as Max.
  if (select && select.value === "ultra") select.value = "max";
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
  if (!job || state.readOnly || state.phase !== "done" || completedRunOutcome(state).tone === "done") return;
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
  // Retry preserves the reviewed job's selected stages.
  const fromComposer = state.phase === "input";
  const stages = fromComposer ? selectedWorkflowStages() : workflowStagesFor(state);
  if (!Object.values(stages).some(Boolean)) return showCreateError("Select at least one workflow stage.");
  if (!statement.trim()) return showCreateError("Enter the text for this workflow.");
  if (stages.author && !validateWorkflowHours()) return;
  // The composer's related works go with a new problem only; a retried review keeps its job's own.
  const relatedProblem = fromComposer ? composerRelatedWorksProblem(stages) : "";
  if (relatedProblem) return showCreateError(relatedProblem);
  const relatedWorks = fromComposer ? composerRelatedTokens() : [];
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  if (!Object.keys(promptValues).length) syncPrompts();
  reviewPending = stages.review;
  const job = currentJob;
  const batch = currentBatch;
  const label = ui.check.textContent;
  ui.check.dataset.creating = "1";
  ui.check.disabled = ui.runFolder.disabled = true;
  ui.check.textContent = "Starting…";
  try {
    const next = await request(jobPath("/start-workflow"), {
      content: statement, feedback,
      ...proofJobSettings(),
      workflowStages: stages,
      ...(relatedWorks.length ? { relatedWorks } : {}),
    });
    if (job !== currentJob || batch !== currentBatch) return;
    if (relatedWorks.length) clearComposerRelatedWorks();
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
    delete ui.check.dataset.creating;
    ui.check.disabled = !Object.values(selectedWorkflowStages()).some(Boolean);
    ui.runFolder.disabled = false;
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
    workflowStages: selectedWorkflowStages(),
    reviewModel: ui.reviewModel.value,
    reviewEffort: ui.reviewEffort.value,
    authorModel: ui.authorModel.value,
    criticModel: ui.criticModel.value,
    writerModel: ui.writerModel.value,
    authorEffort: ui.authorEffort.value,
    criticEffort: ui.criticEffort.value,
    writerEffort: ui.writerEffort.value,
    promptOverrides,
    criticRounds: Number(ui.criticRounds.value),
    criticCount: Number(ui.criticCount.value),
    authorSubagents: Number(ui.authorSubagents.value),
    thinkingHours: Number(ui.thinkingHours.value),
    speedMode: ui.speedMode.value,
    reasoningSummary: ui.reasoningSummary.value,
    ...selectedRetrievalSettings(),
  };
}

// An empty or malformed number field reads as "", which Number() would turn into 0 (unlimited).
function validateWorkflowHours() {
  if (ui.thinkingHours.value.trim() !== "") return true;
  ui.notice.textContent = ui.thinkingHours.max
    ? `Enter a time limit: 0 for unlimited, or at most ${Number(ui.thinkingHours.max)} hours.`
    : "Enter a time limit: 0 for unlimited, or a positive number of hours.";
  show(ui.notice, true);
  const advanced = ui.thinkingHours.closest("details");
  if (advanced) advanced.open = true;
  ui.thinkingHours.focus();
  return false;
}

// Related works for the next problem, listed in the composer. Each file uploads as soon as it is
// added (one raw request per file, outside request(), so a large file never holds up a poll), and a
// note is staged the same way; Start workflow and Run a folder… then send the upload tokens. The list
// lives on a function property, so it exists before this span has run.
function composerRelatedWorks() {
  return (composerRelatedWorks.state ||= { items: [], notes: 0, generation: 0, signature: "[]" });
}

// The server's limit per file; an empty or larger file is refused before anything is sent.
const maxRelatedFileBytes = 64 * 1024 * 1024;

function relatedFileProblem(file) {
  if (!file.size) return `${file.name} is empty.`;
  return file.size > maxRelatedFileBytes
    ? `${file.name} is ${(file.size / 1048576).toFixed(1)} MB; the limit is 64 MB per file.` : "";
}

// One file's raw bytes to the private staging area; its token then adds it to a job or a batch.
async function uploadRelatedFile(file) {
  const response = await fetch(`/uploads?name=${encodeURIComponent(file.name)}`, {
    method: "POST", body: file,
    headers: { "X-TCS-Prover-Token": sessionToken, "Content-Type": "application/octet-stream" },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.upload?.token) throw new Error(data.error || `The upload failed (HTTP ${response.status}).`);
  return data.upload;
}

// A staged upload that will not be added is dropped at once (the server would keep it for a day).
function discardUpload(token) {
  if (token) request("/uploads/discard", { token }).catch(() => {});
}

// One related work as a row: a type mark, the name, what it is, and Remove where the list can still
// change. `item` is a related work the server lists, or a composer item (with its upload once staged).
function relatedWorkRow(item, remove = null) {
  const work = item.upload || item;
  const row = document.createElement("li"), mark = document.createElement("span");
  const copy = document.createElement("div"), name = document.createElement("strong");
  const meta = document.createElement("span");
  row.className = "related-work-row";
  row.dataset.state = item.status || "ready";
  mark.className = "related-work-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = work.kind === "note" ? "NOTE"
    : (/\.([A-Za-z0-9]{1,4})$/.exec(item.name || "")?.[1] || "file").toUpperCase();
  // The saved name, as related-works/ and INDEX.md show it; a note's title goes in the detail line.
  const label = item.name || work.name || (work.kind === "note" ? "Note" : "File");
  name.textContent = label;
  meta.className = "related-work-meta";
  meta.textContent = item.status === "uploading" ? "uploading…"
    : item.status === "failed" ? `Upload failed: ${item.error}`
    : work.kind === "note" ? [work.title ? `note “${work.title}”` : "note", work.sizeText,
      work.batch ? "from the batch" : ""].filter(Boolean).join(" · ")
    : [work.sizeText, work.textStatus,
      work.originalName && work.originalName !== work.name && !item.upload ? `uploaded as “${work.originalName}”` : "",
      work.batch ? "from the batch" : ""].filter(Boolean).join(" · ");
  copy.className = "related-work-copy";
  copy.append(name, meta);
  row.append(mark, copy);
  if (remove) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost compact related-work-remove";
    button.textContent = "Remove";
    button.setAttribute("aria-label", `Remove ${label}`);
    button.onclick = remove;
    row.append(button);
  }
  return row;
}

// Why the listed related works cannot be sent yet: "" when they can, or when there are none.
function composerRelatedWorksProblem(stages) {
  const list = composerRelatedWorks();
  if (!list.items.length && !list.notes) return "";
  if (list.notes || list.items.some((item) => item.status === "uploading")) {
    return "Wait for the related works to finish uploading.";
  }
  if (list.items.some((item) => item.status === "failed")) {
    return "Remove the related works that could not be uploaded, then start.";
  }
  return stages.author ? "" : "Related works are read by the proof author. Turn on the Author stage or remove them.";
}

function composerRelatedTokens() {
  return composerRelatedWorks().items.map((item) => item.upload?.token).filter(Boolean);
}

// Shown while the Author stage is on or anything is listed; rows are rebuilt only when one changes.
function renderComposerRelatedWorks() {
  const list = composerRelatedWorks(), items = list.items;
  const author = Boolean(ui.workflowAuthor.checked);
  show(ui.relatedWorksField, author || items.length > 0);
  const help = !author && items.length
    ? "Related works are read by the proof author; turn on the Author stage, or remove them."
    : "Papers, drafts or notes the proof author can read while it works. Text is extracted from PDFs for the "
      + "author. Up to 64 MB per file. With Run a folder…, every problem of the folder run gets them.";
  if (ui.relatedWorksHelp.textContent !== help) ui.relatedWorksHelp.textContent = help;
  show(ui.composerRelatedList, items.length > 0);
  const signature = JSON.stringify(items.map(({ file, ...item }) => item));
  if (signature === list.signature) return;
  list.signature = signature;
  ui.composerRelatedList.replaceChildren(...items.map((item) => relatedWorkRow(item,
    () => removeComposerRelatedWork(item))));
}

function removeComposerRelatedWork(item) {
  const list = composerRelatedWorks();
  list.items = list.items.filter((other) => other !== item);
  discardUpload(item.upload?.token);
  renderComposerRelatedWorks();
}

// After a start the server owns the uploads; leaving the composer drops them (`discard`).
function clearComposerRelatedWorks(discard = false) {
  const list = composerRelatedWorks();
  list.generation += 1;
  if (!list.items.length) return;
  if (discard) for (const item of list.items) discardUpload(item.upload?.token);
  list.items = [];
  renderComposerRelatedWorks();
}

async function addComposerFiles(files) {
  const list = composerRelatedWorks();
  const added = [...(files || [])].map((file) => {
    const problem = relatedFileProblem(file);
    return { kind: "file", name: file.name, status: problem ? "failed" : "uploading", error: problem, file };
  });
  if (!added.length) return;
  list.items = [...list.items, ...added];
  renderComposerRelatedWorks();
  for (const item of added) {
    if (item.status !== "uploading") continue;
    try {
      const upload = await uploadRelatedFile(item.file);
      // Removed (or the composer left) while it uploaded: the server need not keep it.
      if (composerRelatedWorks().items.includes(item)) Object.assign(item, { status: "ready", upload });
      else discardUpload(upload.token);
    } catch (error) {
      Object.assign(item, { status: "failed", error: error.message });
    }
    delete item.file;
    renderComposerRelatedWorks();
  }
}

// A note keeps its text in the form until the server has staged it, so a refusal loses nothing.
async function addComposerNote() {
  const list = composerRelatedWorks(), generation = list.generation;
  const title = ui.composerNoteTitle.value.trim(), text = ui.composerNoteText.value;
  if (!text.trim()) {
    showCreateError("Enter the note's text.");
    ui.composerNoteText.focus();
    return;
  }
  list.notes += 1;
  ui.composerSaveNote.disabled = true;
  try {
    const { upload } = await request("/uploads/note", { title, text });
    if (list.generation !== generation) {
      discardUpload(upload?.token);
      return;
    }
    list.items = [...list.items, { kind: "note", name: upload.name, status: "ready", upload }];
    ui.composerNoteTitle.value = ui.composerNoteText.value = "";
    show(ui.composerNoteForm, false);
    ui.composerAddNote.setAttribute("aria-expanded", "false");
  } catch (error) {
    showCreateError(error.message);
  } finally {
    list.notes -= 1;
    ui.composerSaveNote.disabled = false;
    renderComposerRelatedWorks();
  }
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

// One statement of a folder may be this long, as the server allows.
const maxFolderStatementChars = 2000000;

// Read each text file; empty, binary and oversized files are reported instead of sent.
async function folderStatements(files) {
  const statements = [], empty = [], binary = [], large = [];
  for (const file of folderStatementFiles(files)) {
    // Larger than a whole request (often a PDF or an image): skipped unread.
    if (file.size > maxFolderRequestBytes) {
      large.push(file.name);
      continue;
    }
    const statement = await file.text();
    // A NUL marks binary data (images, archives, UTF-16 text), which the server refuses.
    if (statement.includes("\0")) binary.push(file.name);
    else if (statement.trim().length > maxFolderStatementChars) large.push(file.name);
    else if (statement.trim()) statements.push({ name: file.name, statement });
    else empty.push(file.name);
  }
  return { statements, empty, binary, large };
}

// One folder batch at a time: a repeated change event must not start its jobs twice.
let folderBatchBusy = false;

async function startFolderBatch(files) {
  const chosen = [...(files || [])];
  // A single job being created also blocks the folder run (and the reverse).
  if (!chosen.length || folderBatchBusy || ui.check.disabled) return;
  if (!validateRetrievalSettings({ folder: true }) || (selectedWorkflowStages().author && !validateWorkflowHours())) return;
  const relatedProblem = composerRelatedWorksProblem(selectedWorkflowStages());
  if (relatedProblem) return showCreateError(relatedProblem);
  folderBatchBusy = true;
  ui.check.dataset.creating = "1";
  ui.runFolder.disabled = ui.check.disabled = true;
  try {
    await sendFolderBatch(chosen);
  } finally {
    folderBatchBusy = false;
    delete ui.check.dataset.creating;
    ui.runFolder.disabled = false;
    ui.check.disabled = !Object.values(selectedWorkflowStages()).some(Boolean);
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
  const { statements, empty, binary, large = [] } = found;
  const skipped = [
    empty.length ? `Skipped ${empty.length} empty file${empty.length === 1 ? "" : "s"}: ${empty.join(", ")}.` : "",
    binary.length ? `Skipped ${binary.length} non-text file${binary.length === 1 ? "" : "s"}: ${binary.join(", ")}.` : "",
    large.length ? `Skipped ${large.length} file${large.length === 1 ? "" : "s"} too large for one statement `
      + `(over 8 MB or 2,000,000 characters): ${large.join(", ")}.` : "",
  ].filter(Boolean).join(" ");
  if (!statements.length) {
    showCreateError(`No statements were found directly inside ${folder}.`
      + (skipped ? ` ${skipped}` : "") + " Choose a folder that contains non-empty text files (any extension or none).");
    return;
  }
  // File reading is asynchronous; recheck the current settings before sending.
  if (!validateRetrievalSettings({ folder: true }) || (selectedWorkflowStages().author && !validateWorkflowHours())) return;
  const relatedProblem = composerRelatedWorksProblem(selectedWorkflowStages());
  if (relatedProblem) return showCreateError(relatedProblem);
  // The listed related works go to the folder run's own store, which every one of its jobs sees.
  const relatedWorks = composerRelatedTokens();
  const count = statements.length;
  const question = `Start ${count} parallel job${count === 1 ? "" : "s"}? `
    + "Each runs the selected workflow. All of them share the rate and usage limits of the same CLI "
    + "logins and API keys; jobs beyond the running-jobs limit (Models & API) wait in a queue.\n\n"
    + `Folder: ${folder}\n`
    + "Each job uses the selected stages."
    + (selectedWorkflowStages().review ? " Statement review will pause each job for your approval." : "")
    + (relatedWorks.length ? ` Every job gets the ${relatedWorks.length} related work`
      + `${relatedWorks.length === 1 ? "" : "s"} listed above.` : "")
    + (skipped ? `\n\n${skipped}` : "");
  if (!confirm(question)) return;
  const body = { statements, ...proofJobSettings(), ...(relatedWorks.length ? { relatedWorks } : {}) };
  const size = new TextEncoder().encode(JSON.stringify(body)).length;
  if (size > maxFolderRequestBytes) {
    showCreateError(`These statements are too large to send together `
      + `(${(size / 1048576).toFixed(1)} MB; the limit is 8 MB). Split the folder.`);
    return;
  }
  clearTimeout(jobsTimer);
  try {
    const result = await request("/direct-batch", body);
    if (relatedWorks.length) clearComposerRelatedWorks();
    // Jobs beyond the running-jobs limit wait in the queue. The server's notice says so, and names
    // any job it could not create or start yet (it tries again every minute).
    const started = (result.startedJobs || []).length - (result.queuedJobs || []).length;
    ui.batchStatus.textContent = `Started ${started} job${started === 1 ? "" : "s"} from ${folder}.`
      + (result.notice ? ` ${result.notice}` : "") + (skipped ? `\n${skipped}` : "");
    show(ui.batchStatus, true);
    if (!currentJob) {
      composingProblem = false;
      currentBatch = result.batchId || result.batch?.id || "__root__";
      history.pushState(null, "", batchUrl(currentBatch));
      renderBatchNavigation();
      renderJobs(result.jobs || []);
      if (typeof renderBatchRelatedWorks === "function") renderBatchRelatedWorks(result.jobs || []);
    }
    // The result sits at the top of the batch list (or of the job page that started the run).
    ui.batchStatus.scrollIntoView({ block: "nearest", behavior: "smooth" });
  } catch (error) {
    showCreateError(error.message);
  }
  if (!currentJob) loadJobs();
}

// Whether the server accepted the action (its error, if any, is in the notice).
async function act(path, body = {}) {
  clearTimeout(timer);
  // A new action replaces the feedback of the previous one.
  show(ui.notice, false);
  const job = currentJob;
  const batch = currentBatch;
  try {
    const next = await request(jobPath(path), body);
    if (job === currentJob && batch === currentBatch) render(next);
    return true;
  } catch (error) {
    ui.notice.textContent = error.message;
    show(ui.notice, true);
    // A rejected control (often a race with the run itself) must not end live polling.
    // After navigation the new view polls on its own.
    if (job !== currentJob || batch !== currentBatch) return false;
    clearTimeout(timer);
    timer = setTimeout(refresh, 700);
    return false;
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
  // Related works still uploading (or failed) are refused before a folder is chosen.
  const relatedProblem = composerRelatedWorksProblem(selectedWorkflowStages());
  if (relatedProblem) return showCreateError(relatedProblem);
  ui.folderInput.value = "";
  ui.folderInput.click();
};
ui.folderInput.onchange = () => startFolderBatch(ui.folderInput.files);

ui.check.onclick = () => startReview(ui.problem.value);
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
for (const control of [ui.workflowReview, ui.workflowAuthor, ui.workflowCritic, ui.workflowWriting]) {
  control.onchange = () => setWorkflowStages();
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
  const maximum = ui.authorLimitHours.max ? Number(ui.authorLimitHours.max) : Infinity;
  if (!(Number.isFinite(hours) && hours >= 0 && hours <= maximum)) {
    ui.notice.textContent = Number.isFinite(maximum) ? `Set 0 for unlimited, or at most ${maximum} hours.`
      : "Set 0 for unlimited, or a positive number of hours.";
    show(ui.notice, true);
    ui.authorLimitHours.focus();
    return;
  }
  // A limit at or below the active time stops a running author at its next step: ask first. The
  // field holds what renderAuthorLimitHint() last saw of the job.
  const elapsed = Number(ui.authorLimitHours.dataset?.elapsed);
  if (ui.authorLimitHours.dataset?.running && hours > 0 && hours * 3600 <= elapsed) {
    if (!confirm(`Set the time limit to ${limitHoursText(hours)}? That is at or below the ${durationText(elapsed)} of active work `
      + "so far: the proof author stops at its next step and writes a progress summary. The critic and LaTeX "
      + "steps still finish.")) return;
  }
  act("/set-author-time-limit", { hours });
};
ui.sendAuthorSteer.onclick = () => {
  // While the run is paused, render() turns the box into a message to the paused author.
  const paused = typeof authorSteerPaused !== "undefined" && authorSteerPaused;
  const instruction = ui.authorSteerInstruction.value.trim();
  if (!instruction) {
    ui.notice.textContent = paused ? "Enter a message for the author." : "Enter an instruction for the proof author.";
    show(ui.notice, true);
    ui.authorSteerInstruction.focus();
    return;
  }
  const limit = authorSteerLimit();
  if (instruction.length > limit) {
    ui.notice.textContent = `Keep the ${paused ? "message" : "live instruction"} at or below `
      + `${limit.toLocaleString("en-US")} characters `
      + `(it has ${instruction.length.toLocaleString("en-US")}); shorten it or send it in parts.`;
    show(ui.notice, true);
    ui.authorSteerInstruction.focus();
    return;
  }
  if (!paused) {
    act("/steer-author", { instruction });
    return;
  }
  act("/ask-author", { text: instruction }).then((sent) => {
    if (!sent) return;
    ui.authorSteerInstruction.value = "";
    renderAuthorSteerCount();
  });
};
// A visible count instead of a silent cut at the limit the server enforces.
function authorSteerLimit() {
  return Number(ui.authorSteerInstruction.dataset?.maxChars) || 12000;
}
function renderAuthorSteerCount() {
  const limit = authorSteerLimit();
  const length = ui.authorSteerInstruction.value.trim().length;
  ui.authorSteerCount.textContent = length
    ? `${length.toLocaleString("en-US")} of ${limit.toLocaleString("en-US")} characters`
      + (length > limit ? " — too long to send; shorten it or send it in parts." : "")
    : "";
  ui.authorSteerCount.classList.toggle("over", length > limit);
}
ui.authorSteerInstruction.addEventListener("input", renderAuthorSteerCount);
ui.stop.onclick = () => act("/stop");
ui.pause.onclick = () => act("/pause");
// Resume first sets a limit typed in the time limit field, then resumes once the server has it. A
// used-up limit is not sent: the field opens with the reason instead.
ui.resume.onclick = async () => {
  if (state.phase === "prepared") return act("/continue-stopped");
  const hours = pendingAuthorLimit();
  if (hours !== null) {
    // Resume only the job whose limit was set, never one opened while the request ran.
    const job = currentJob, batch = currentBatch, run = state.runId;
    if (await act("/set-author-time-limit", { hours }) && job === currentJob && batch === currentBatch
        && run === state.runId && state.phase === "paused" && Number(state.thinkingHours ?? 0) === hours) {
      await act("/resume");
    }
    return;
  }
  if (state.timeLimitExhausted) {
    ui.authorTimeLimitControl.open = true;
    ui.authorLimitHours.focus();
    ui.notice.textContent = `The time limit is used up (${durationText(runElapsedSeconds())} of `
      + `${limitHoursText(Number(state.thinkingHours ?? 0))}). Raise it, or set 0 for no limit, then choose Continue.`;
    show(ui.notice, true);
    return;
  }
  return act("/resume");
};
// A limit typed in the visible time limit field that the job does not have yet, or null.
function pendingAuthorLimit() {
  if (ui.authorTimeLimitControl.hidden) return null;
  const raw = String(ui.authorLimitHours.value ?? "").trim();
  const hours = raw === "" ? NaN : Number(raw);
  return Number.isFinite(hours) && hours >= 0 && hours !== Number(state.thinkingHours ?? 0) ? hours : null;
}
ui.requestSummary.onclick = () => act("/author-summary", {});
// Stops the summary or reply being written (by its id); the job stays paused.
ui.stopReply.onclick = () => {
  const item = (state.conversation?.items || []).find((entry) => ["queued", "running"].includes(entry.status));
  return act("/stop-author-reply", item ? { id: item.id } : {});
};
async function downloadArtifact(button, extension, filename = `final.${extension}`, path = `/download-${extension}`,
  values = {}) {
  button.disabled = true;
  try {
    const response = await fetch(jobPath(path, values), {
      headers: { "X-TCS-Prover-Token": sessionToken },
    });
    if (!response.ok) throw new Error((await response.json()).error || "Download failed.");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
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
ui.downloadProof.onclick = () => downloadArtifact(ui.downloadProof, "proof", "approved-proof.md");
ui.downloadSource.onclick = () => downloadArtifact(ui.downloadSource, "source", "latex-source.tex");
ui.downloadTranscript.onclick = () => downloadArtifact(ui.downloadTranscript, "jsonl", "transcript.jsonl", "/transcript");
// The convention critic's loophole warning: copy or rerun with its suggested statement, or ignore it.
ui.conventionWarningCopy.onclick = async () => {
  const text = state.conventionWarning?.suggestedStatement || "";
  try {
    await navigator.clipboard.writeText(text);
    ui.conventionWarningCopy.textContent = "Copied";
  } catch (_) {
    ui.conventionWarningCopy.textContent = "Select the text to copy";
  }
  setTimeout(() => { ui.conventionWarningCopy.textContent = "Copy"; }, 2000);
};
ui.conventionWarningRerun.onclick = () => rerunWithStatement(state.conventionWarning?.suggestedStatement || "");
ui.conventionWarningIgnore.onclick = () => act("/dismiss-convention-warning", { time: state.conventionWarning?.time || "" });
ui.home.onclick = goHome;
ui.reviewHome.onclick = goHome;
ui.allBatches.onclick = () => selectBatch("");
ui.batchProblems.onclick = goHome;
ui.newProblem.onclick = newProblem;
// The author's question: Send queues the answered questions as one live instruction; Dismiss closes it
// without sending anything. Either way the server closes the question, and the next state hides the box.
// Both act only on the question whose rows are on screen: a click that meets another question is ignored.
function shownAuthorQuestion() {
  const question = state.authorQuestion;
  return question?.id && authorQuestionRows?.runId === state.runId && authorQuestionRows.id === question.id
    ? question : null;
}
// This page closes the question itself: no closing notice, unless the server refuses.
async function settleAuthorQuestion(question, path, body) {
  authorQuestionSettled = question.id;
  const accepted = await act(path, body);
  if (!accepted && authorQuestionSettled === question.id) authorQuestionSettled = "";
  return accepted;
}
ui.sendAuthorAnswer.onclick = async () => {
  const question = shownAuthorQuestion();
  if (!question) return;
  const answers = authorQuestionAnswers();
  if (!answers.length) {
    ui.notice.textContent = "Choose an option or write an answer first.";
    show(ui.notice, true);
    return;
  }
  ui.sendAuthorAnswer.disabled = true;
  try {
    await settleAuthorQuestion(question, "/answer-author-question", { questionId: question.id, answers });
  } finally {
    ui.sendAuthorAnswer.disabled = false;
  }
};
ui.dismissAuthorQuestion.onclick = () => {
  const question = shownAuthorQuestion();
  if (question) return settleAuthorQuestion(question, "/dismiss-author-question", { questionId: question.id });
};
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

// Related works on the run page and on the batch page. A file uploads in its own request (with the
// launch token, outside request()); one request then adds every staged upload to the job, or to a
// folder run's store, which the server links into each job of the run. Uploads that end up unused
// are discarded at once.
function setNoteForm(form, button, open) {
  show(form, open);
  button?.setAttribute("aria-expanded", String(open));
  if (open) form.querySelector("textarea")?.focus();
}

function relatedWorksCount(count) {
  return `${count} related work${count === 1 ? "" : "s"}`;
}

// An upload refused by the server, as one sentence.
function uploadFailure(name, error) {
  return `Could not upload ${name}: ${String(error.message || error).replace(/\.$/, "")}.`;
}

// Uploads each file in turn, reporting progress; returns the tokens and the sentences for refusals.
async function stageRelatedFiles(files, progress, stillHere) {
  const tokens = [], problems = [];
  for (const [index, file] of files.entries()) {
    progress(`Uploading ${index + 1} of ${files.length}: ${file.name}…`);
    const problem = relatedFileProblem(file);
    if (problem) {
      problems.push(problem);
      continue;
    }
    try {
      tokens.push((await uploadRelatedFile(file)).token);
    } catch (error) {
      problems.push(uploadFailure(file.name, error));
    }
    if (!stillHere()) break;
  }
  return { tokens, problems };
}

let relatedWorksBusy = false;
function setRelatedWorksBusy(busy) {
  relatedWorksBusy = busy;
  ui.addRelatedFiles.disabled = ui.addRelatedNote.disabled = ui.saveRelatedNote.disabled = busy;
}

async function addRelatedFiles(files) {
  const chosen = [...(files || [])];
  if (!chosen.length || relatedWorksBusy) return;
  const job = currentJob, batch = currentBatch;
  const here = () => job === currentJob && batch === currentBatch;
  setRelatedWorksBusy(true);
  try {
    const { tokens, problems } = await stageRelatedFiles(chosen,
      (text) => { if (here()) ui.relatedWorksStatus.textContent = text; }, here);
    if (!here()) {
      tokens.forEach(discardUpload);
      return;
    }
    const added = tokens.length > 0 && await act("/related-works/add", { uploads: tokens });
    if (tokens.length && !added) tokens.forEach(discardUpload);
    if (here()) {
      ui.relatedWorksStatus.textContent = [added ? `Added ${relatedWorksCount(tokens.length)}.` : "", ...problems]
        .filter(Boolean).join(" ");
    }
  } finally {
    setRelatedWorksBusy(false);
  }
}

async function addRelatedNote() {
  if (relatedWorksBusy) return;
  const title = ui.relatedNoteTitle.value.trim(), text = ui.relatedNoteText.value;
  if (!text.trim()) {
    ui.relatedWorksStatus.textContent = "Enter the note's text.";
    ui.relatedNoteText.focus();
    return;
  }
  const job = currentJob, batch = currentBatch;
  setRelatedWorksBusy(true);
  ui.relatedWorksStatus.textContent = "Adding the note…";
  try {
    let token;
    try {
      token = (await request("/uploads/note", { title, text })).upload?.token;
    } catch (error) {
      if (job === currentJob && batch === currentBatch) ui.relatedWorksStatus.textContent = error.message;
      return;
    }
    if (job !== currentJob || batch !== currentBatch) {
      discardUpload(token);
      return;
    }
    if (await act("/related-works/add", { uploads: [token] })) {
      ui.relatedNoteTitle.value = ui.relatedNoteText.value = "";
      setNoteForm(ui.relatedNoteForm, ui.addRelatedNote, false);
      ui.relatedWorksStatus.textContent = `Added ${relatedWorksCount(1)}.`;
    } else {
      discardUpload(token);
      ui.relatedWorksStatus.textContent = "";
    }
  } finally {
    setRelatedWorksBusy(false);
  }
}

// The batch page: one group per folder run of this batch (each /jobs row names its folderBatch) whose jobs can
// still take related works, with what the store holds (GET /batch-related-works, read again every 10
// seconds while the page polls, every minute while the store takes none) and Add files… and Add a note
// for every job of that run. A finished folder run is not listed: its jobs list their related works on
// their own pages.
const batchRelated = { jobs: [], listings: new Map(), target: "", busy: false, signature: "" };

function folderRunText(members) {
  const names = members.map((job) => job.sourceFile || job.title || job.runId);
  return `${members.length} problem${members.length === 1 ? "" : "s"}: `
    + (names.length > 4 ? `${names.slice(0, 4).join(", ")} and ${names.length - 4} more` : names.join(", "));
}

async function loadBatchListing(id, force = false) {
  const entry = batchRelated.listings.get(id) || {};
  const wait = entry.listing && !entry.listing.available ? 60000 : 10000;
  if (entry.loading || (!force && Date.now() - (entry.loadedAt || 0) < wait)) return;
  batchRelated.listings.set(id, { ...entry, loading: true });
  let next;
  try {
    next = { listing: await request(`/batch-related-works?batch=${encodeURIComponent(id)}`), loadedAt: Date.now() };
  } catch (error) {
    next = { listing: entry.listing, error: error.message, loadedAt: Date.now() };
  }
  batchRelated.listings.set(id, next);
  renderBatchRelatedWorks();
}

function batchRelatedGroup(id, members) {
  const { listing, error } = batchRelated.listings.get(id);
  const run = folderRunText(members);
  const group = document.createElement("li"), head = document.createElement("div");
  const title = document.createElement("strong"), count = document.createElement("span");
  const about = document.createElement("p"), actions = document.createElement("div");
  const files = document.createElement("button"), note = document.createElement("button");
  group.className = "batch-related-group";
  group.dataset.batch = id;
  head.className = "batch-related-head";
  title.textContent = "Folder run";
  count.className = "chip";
  count.textContent = relatedWorksCount(Number(listing.count || 0));
  head.append(title, count);
  about.className = "help";
  about.textContent = [run, error].filter(Boolean).join(". ");
  group.append(head, about);
  if (listing.items?.length) {
    const list = document.createElement("ul");
    list.className = "related-works-list";
    list.append(...listing.items.map((item) => relatedWorkRow(item)));
    group.append(list);
  }
  actions.className = "related-works-actions";
  files.type = note.type = "button";
  files.className = "secondary compact";
  files.textContent = "Add files…";
  files.setAttribute("aria-label", `Add files to the batch related works of the folder run of ${run}`);
  note.className = "ghost compact";
  note.textContent = "Add a note";
  note.setAttribute("aria-controls", "batchNoteForm");
  note.setAttribute("aria-label", `Add a note to the batch related works of the folder run of ${run}`);
  files.disabled = note.disabled = batchRelated.busy;
  files.onclick = () => {
    batchRelated.target = id;
    ui.batchRelatedFiles.value = "";
    ui.batchRelatedFiles.click();
  };
  note.onclick = () => {
    batchRelated.target = id;
    ui.batchNoteTarget.textContent = `For every job of the folder run of ${run}.`;
    setNoteForm(ui.batchNoteForm, null, true);
  };
  actions.append(files, note);
  group.append(actions);
  return group;
}

// Called with the batch page's /jobs rows; without folder runs (or on a saved batch) it only hides.
function renderBatchRelatedWorks(jobs = batchRelated.jobs) {
  batchRelated.jobs = jobs;
  const runs = new Map();
  for (const job of selectedBatch()?.readOnly ? [] : jobs) {
    if (job.folderBatch) runs.set(job.folderBatch, [...(runs.get(job.folderBatch) || []), job]);
  }
  // Only a folder run with a job that has not finished can take related works: only those are read.
  const live = [...runs].filter(([, members]) => members.some((job) => job.phase !== "done"));
  for (const [id] of live) loadBatchListing(id);
  const shown = live.filter(([id]) => batchRelated.listings.get(id)?.listing?.available);
  show(ui.batchRelatedWorks, shown.length > 0);
  if (!shown.some(([id]) => id === batchRelated.target)) setNoteForm(ui.batchNoteForm, null, false);
  const signature = JSON.stringify([shown.map(([id, members]) => [id, folderRunText(members),
    batchRelated.listings.get(id)?.listing || null, batchRelated.listings.get(id)?.error || ""]), batchRelated.busy]);
  if (signature === batchRelated.signature) return;
  batchRelated.signature = signature;
  ui.batchRelatedGroups.replaceChildren(...shown.map(([id, members]) => batchRelatedGroup(id, members)));
}

function setBatchRelatedBusy(busy) {
  batchRelated.busy = busy;
  ui.batchSaveNote.disabled = busy;
  batchRelated.signature = "";
  renderBatchRelatedWorks();
}

// Adds staged uploads to a folder run's store; the reply is the store's new listing.
async function addToBatch(id, tokens) {
  const listing = await request(`/batch-related-works/add?batch=${encodeURIComponent(id)}`, { uploads: tokens });
  batchRelated.listings.set(id, { listing, loadedAt: Date.now() });
  return listing;
}

async function addBatchRelatedFiles(files) {
  const chosen = [...(files || [])], id = batchRelated.target;
  if (!chosen.length || !id || batchRelated.busy) return;
  const batch = currentBatch, here = () => batch === currentBatch && !currentJob;
  setBatchRelatedBusy(true);
  let text = "";
  try {
    const { tokens, problems } = await stageRelatedFiles(chosen,
      (line) => { if (here()) ui.batchRelatedStatus.textContent = line; }, here);
    let added = "";
    if (tokens.length) {
      try {
        const listing = await addToBatch(id, tokens);
        added = `Added ${relatedWorksCount(tokens.length)} to the folder run.${listing.notice ? ` ${listing.notice}` : ""}`;
      } catch (error) {
        tokens.forEach(discardUpload);
        added = error.message;
      }
    }
    text = [added, ...problems].filter(Boolean).join(" ");
  } finally {
    setBatchRelatedBusy(false);
    if (here()) ui.batchRelatedStatus.textContent = text;
  }
}

async function addBatchNote() {
  const id = batchRelated.target, title = ui.batchNoteTitle.value.trim(), text = ui.batchNoteText.value;
  if (!id || batchRelated.busy) return;
  if (!text.trim()) {
    ui.batchRelatedStatus.textContent = "Enter the note's text.";
    ui.batchNoteText.focus();
    return;
  }
  setBatchRelatedBusy(true);
  ui.batchRelatedStatus.textContent = "Adding the note…";
  let status = "";
  try {
    const token = (await request("/uploads/note", { title, text })).upload?.token;
    try {
      const listing = await addToBatch(id, [token]);
      status = `Added ${relatedWorksCount(1)} to the folder run.${listing.notice ? ` ${listing.notice}` : ""}`;
      ui.batchNoteTitle.value = ui.batchNoteText.value = "";
      setNoteForm(ui.batchNoteForm, null, false);
    } catch (error) {
      discardUpload(token);
      status = error.message;
    }
  } catch (error) {
    status = error.message;
  } finally {
    setBatchRelatedBusy(false);
    ui.batchRelatedStatus.textContent = status;
  }
}

ui.composerAddFiles.onclick = () => {
  ui.composerRelatedFiles.value = "";
  ui.composerRelatedFiles.click();
};
ui.composerRelatedFiles.onchange = () => addComposerFiles(ui.composerRelatedFiles.files);
ui.composerAddNote.onclick = () => setNoteForm(ui.composerNoteForm, ui.composerAddNote, ui.composerNoteForm.hidden);
ui.composerSaveNote.onclick = addComposerNote;
ui.composerCancelNote.onclick = () => setNoteForm(ui.composerNoteForm, ui.composerAddNote, false);
ui.addRelatedFiles.onclick = () => {
  ui.relatedFilesInput.value = "";
  ui.relatedFilesInput.click();
};
ui.relatedFilesInput.onchange = () => addRelatedFiles(ui.relatedFilesInput.files);
ui.addRelatedNote.onclick = () => setNoteForm(ui.relatedNoteForm, ui.addRelatedNote, ui.relatedNoteForm.hidden);
ui.saveRelatedNote.onclick = addRelatedNote;
ui.cancelRelatedNote.onclick = () => setNoteForm(ui.relatedNoteForm, ui.addRelatedNote, false);
ui.batchRelatedFiles.onchange = () => addBatchRelatedFiles(ui.batchRelatedFiles.files);
ui.batchSaveNote.onclick = addBatchNote;
ui.batchCancelNote.onclick = () => setNoteForm(ui.batchNoteForm, null, false);
ui.authorLimitHours.addEventListener("input", renderAuthorLimitHint);

// API models (Advanced settings → API connections): one saved key exposes its model catalog.
// Keys travel only in POST bodies and are never returned by the controller.
const apiUi = {
  group: $("apiModels"), heading: $("apiModelsHeading"), count: $("apiModelCount"),
  status: $("apiModelsStatus"), list: $("apiModelList"), empty: $("apiModelEmpty"),
  form: $("apiModelForm"), formHeading: $("apiModelFormHeading"), formatHelp: $("apiModelFormatHelp"),
  formats: [$("apiModelFormatOpenai"), $("apiModelFormatAnthropic")],
  baseUrl: $("apiModelBaseUrl"), requestSource: $("apiModelRequestSource"), key: $("apiModelKey"),
  keyHelp: $("apiModelKeyHelp"), label: $("apiModelName"), result: $("apiModelTestResult"),
  contextWindow: $("apiModelContextWindow"), maxOutput: $("apiModelMaxOutput"), problem: $("apiModelsProblem"),
  cancel: $("apiModelCancelButton"), test: $("apiModelTestButton"), save: $("apiModelSaveButton"),
};
const apiFormats = {
  openai: { name: "OpenAI", baseUrl: "https://api.openai.com/v1", key: "sk-…",
    help: "Runs through the Codex CLI with the Responses API. Models load automatically when you save." },
  anthropic: { name: "Anthropic", baseUrl: "https://api.anthropic.com", key: "sk-ant-…",
    help: "Runs through the Claude CLI in a private configuration folder, with subagents and web tools; a claude "
      + "launcher that drops CLAUDE_CONFIG_DIR limits it to bare mode. Models load automatically when you save." },
};
const roleNames = { review: "statement reviewer", author: "proof author", critic: "independent critic", writer: "LaTeX writer" };
let apiModels = [], apiModelEditing = null, apiModelBusy = false;
let apiModelsUnavailable = false, apiModelsLoaded = false;
let apiFormEdits = 0, apiSaveEdits = 0, apiShownTest = null;
// Entries of the store that cannot be used (listed to repair or remove), and why the store or TCS
// Prover's private folder cannot be used, if so (the server's words).
let apiInvalid = [], apiStoreProblem = "", apiStateProblem = "";

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
  if (Array.isArray(data.invalid)) apiInvalid = data.invalid;
  if ("storeProblem" in data) apiStoreProblem = String(data.storeProblem || "");
  if ("stateProblem" in data) apiStateProblem = String(data.stateProblem || "");
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
  const repair = apiInvalid.length ? ` · ${apiInvalid.length} to repair` : "";
  apiUi.count.textContent = apiStoreProblem && !apiModels.length ? "Unreadable"
    : `${apiModels.length ? `${apiModels.length} saved` : "None saved"}${repair}`;
  show(apiUi.list, apiModels.length + apiInvalid.length > 0);
  show(apiUi.empty, apiModels.length + apiInvalid.length === 0 && !apiStoreProblem);
  apiUi.list.replaceChildren(...apiModels.map(apiModelRow), ...apiInvalid.map(apiInvalidRow));
  const problem = [apiStoreProblem, apiStateProblem].filter(Boolean).join(" ");
  if (apiUi.problem.textContent !== problem) apiUi.problem.textContent = problem;
  show(apiUi.problem, Boolean(problem));
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
    connection.hasKey ? `key ${connection.keyHint || "saved"}` : "no key saved", apiLimitsText(connection)].filter(Boolean);
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

// "1,000,000-token context · 64,000 max output", for the limits a connection's models have.
function apiLimitsText(connection) {
  return [connection.contextWindow && `${Number(connection.contextWindow).toLocaleString("en-US")}-token context`,
    connection.maxOutputTokens && `${Number(connection.maxOutputTokens).toLocaleString("en-US")} max output`]
    .filter(Boolean).join(" · ");
}
// An entry of the store that cannot be used (edited by hand, or saved by another version): what is
// readable of it (never its key or URL) and why, with Repair (the form, which saves it under its
// id) and Remove.
function apiInvalidRow(entry) {
  const item = apiElement("li", "api-model-row invalid");
  const glyph = apiElement("span", "api-model-glyph", "!");
  glyph.setAttribute("aria-hidden", "true");
  const copy = apiElement("div", "api-model-copy");
  const meta = [entry.model, entry.host, entry.hasKey ? "key saved" : "no key saved"].filter(Boolean);
  copy.append(apiElement("strong", "", entry.label), apiElement("span", "api-model-meta", meta.join(" · ")));
  const format = apiElement("span", `format-chip ${entry.format || "unknown"}`, apiFormats[entry.format]?.name || "Format unknown");
  const status = apiElement("span", "status-chip error", "Cannot be used");
  const actions = apiElement("div", "api-model-actions");
  for (const [action, label] of [["repair", "Repair"], ["remove", "Remove"]]) {
    const button = apiElement("button", `ghost compact api-model-${action}`, label);
    button.type = "button";
    button.dataset.apiAction = action;
    button.dataset.apiModel = entry.ref;
    button.setAttribute("aria-label", `${label} ${entry.label}`);
    actions.append(button);
  }
  item.append(glyph, copy, format, status, actions,
    apiElement("p", "api-test-result error", `${entry.problem} No job can use it until it is repaired or removed.`));
  return item;
}

function selectedApiFormat() { return apiUi.formats.find((input) => input.checked)?.value || "openai"; }
function apiKeyRequired() {
  return !apiModelEditing || selectedApiFormat() !== apiModelEditing.format
    || apiUi.baseUrl.value.trim().replace(/\/+$/, "") !== apiModelEditing.baseUrl;
}
function renderApiForm() {
  const format = apiFormats[selectedApiFormat()], editing = apiModelEditing, required = apiKeyRequired();
  apiUi.formHeading.textContent = editing ? `${editing.repair ? "Repair" : "Edit"} ${editing.label}` : "Add an API connection";
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
    : editing.repair ? "Enter the API key again to repair this entry."
    : required ? "Enter the API key again: a saved key is sent only to the format and base URL it was saved with."
    : "Leave blank to keep the saved key; a new key replaces it.";
}
function resetApiForm() {
  apiFormEdits += 1;
  apiSaveEdits += 1;
  apiModelEditing = null;
  for (const field of [apiUi.baseUrl, apiUi.key, apiUi.requestSource, apiUi.label, apiUi.contextWindow, apiUi.maxOutput]) {
    field.value = "";
  }
  renderApiForm();
}
function apiFormValues() {
  const values = { format: selectedApiFormat(), baseUrl: apiUi.baseUrl.value.trim(), apiKey: apiUi.key.value.trim(),
    requestSource: apiUi.requestSource.value.trim(), label: apiUi.label.value.trim() };
  // Token limits go when given, or empty to clear the ones the edited connection has.
  for (const [field, key] of [[apiUi.contextWindow, "contextWindow"], [apiUi.maxOutput, "maxOutputTokens"]]) {
    const value = field.value.trim();
    if (value || apiModelEditing?.[key]) values[key] = value;
  }
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
// Repair an entry that cannot be used: the form holds what is readable of it, and the key must be
// entered again. Saved under the entry's id, the repaired connection replaces it (jobs that named
// its models find them again); an entry without a usable id is saved as a new connection.
function repairApiModel(entry) {
  apiFormEdits += 1;
  apiSaveEdits += 1;
  apiModelEditing = entry.id ? { id: entry.id, label: entry.label, format: entry.format, repair: true } : null;
  const format = apiFormats[entry.format] ? entry.format : selectedApiFormat();
  for (const input of apiUi.formats) input.checked = input.value === format;
  for (const field of [apiUi.baseUrl, apiUi.key, apiUi.requestSource, apiUi.contextWindow, apiUi.maxOutput]) field.value = "";
  apiUi.label.value = entry.id && entry.label !== entry.id ? entry.label : "";
  showApiResult("", "", `Enter the base URL${entry.host ? ` (it was saved for ${entry.host})` : ""} and the API key, `
    + "then save.");
  renderApiForm(); renderApiModels(); apiUi.baseUrl.focus();
}
async function removeInvalidApiModel(entry, button) {
  if (apiModelBusy || !confirm(`Remove ${entry.label}?\n\nThis entry cannot be used as it is. Removing it also deletes any key saved with it from this computer.`)) return;
  setApiBusy(true, button, "Removing…");
  let message = "", warning = false;
  try {
    const data = await apiModelRequest("/api-models/delete", { ref: entry.ref });
    if (apiModelEditing?.repair && apiModelEditing.id === entry.id) resetApiForm();
    adoptApiModels(data);
    message = `Removed ${entry.label}.`;
  } catch (error) { message = error.message; warning = true; }
  setApiBusy(false); renderApiForm(); renderApiModels();
  if (!warning) apiUi.heading.focus();
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
  apiUi.contextWindow.value = connection.contextWindow ? String(connection.contextWindow) : "";
  apiUi.maxOutput.value = connection.maxOutputTokens ? String(connection.maxOutputTokens) : "";
  clearApiResult(); renderApiForm(); renderApiModels(); apiUi.baseUrl.focus();
}
for (const input of apiUi.formats) input.onchange = () => { noteApiFormEdit(); renderApiForm(); };
apiUi.baseUrl.oninput = () => { noteApiFormEdit(); renderApiForm(); };
apiUi.key.oninput = apiUi.requestSource.oninput = noteApiFormEdit;
// The Test lists the models; the display name and the token limits only matter when saving.
apiUi.label.oninput = apiUi.contextWindow.oninput = apiUi.maxOutput.oninput = () => { apiSaveEdits += 1; };
apiUi.test.onclick = testApiForm;
apiUi.save.onclick = saveApiModel;
apiUi.cancel.onclick = () => { resetApiForm(); clearApiResult(); renderApiModels(); apiUi.baseUrl.focus(); };
apiUi.list.onclick = (event) => {
  const button = event.target.closest("button[data-api-action]");
  const entry = apiInvalid.find((item) => item.ref === button?.dataset.apiModel);
  if (entry && !button.disabled) {
    ({ repair: repairApiModel, remove: removeInvalidApiModel })[button.dataset.apiAction]?.(entry, button);
    return;
  }
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
  // Finished jobs count as complete ("done": a successful outcome, with the check mark) or
  // incomplete (failed, stopped or interrupted), as on the batch page; the server classifies them.
  // Jobs stopped at their time limit ("limit", with the clock) are never counted as paused.
  function tally(batch) {
    const counts = batch.counts || {};
    const count = (key) => Number(counts[key] || 0);
    return {
      running: count("running") + count("reviewing") + count("pausing") + count("stopping"),
      waiting: count("reviewed") + count("prepared") - Number(batch.queued || 0),
      paused: count("paused"),
      limit: count("limit"),
      done: count("done"),
      incomplete: count("incomplete"),
      total: Number(batch.jobCount || 0),
    };
  }
  const toneWords = { running: "running", waiting: "need you", paused: "paused", limit: "time limit reached",
    done: "complete", incomplete: "incomplete" };
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
      const other = counts.total - counts.running - counts.waiting - counts.paused - counts.limit - counts.done
        - counts.incomplete;
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
    const sum = { running: 0, waiting: 0, paused: 0, limit: 0, done: 0, incomplete: 0, total: 0 };
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
    // Overview: the meta line and the five totals.
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
    for (const key of ["running", "waiting", "paused", "limit", "done"]) {
      const value = shell.overviewStats.querySelector(`[data-stat="${key}"]`);
      if (!value) continue;
      setText(value, String(sum[key]));
      value.closest(".stat")?.classList.toggle("zero", !sum[key]);
    }
    // Complete counts the successful outcomes; its note names the finished jobs that are not.
    const note = shell.overviewStats.querySelector('[data-stat-note="done"]');
    if (note) setText(note, ["finished successfully", sum.incomplete ? `${sum.incomplete} incomplete` : ""]
      .filter(Boolean).join(" · "));
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
  const filterTones = { running: ["running"], waiting: ["waiting"], paused: ["paused"], limit: ["limit"], done: ["done"],
    failed: ["failed", "stopped"] };
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
    // A hidden tab reads the counts only for notifications (when they are on), and less often.
    const due = Date.now() - polledAt > (document.hidden ? 30000 : 5000);
    if (["job", "new", "models"].includes(where) && due && (!document.hidden || attentionAlertsOn())) pollBatches();
    // Until the first catalog arrives, the placeholders stay (an empty list would claim no batches).
    if (polled !== null || batchCatalog.length) {
      const batches = catalog();
      renderNavBatches(batches);
      renderTotals(batches);
      notifyAttention(batches);
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

// Running jobs (Advanced settings, and Models & API): how many jobs may run at once on this
// computer (GET and POST /settings; jobs beyond it wait in a queue), and whether this browser
// shows a notification when a job needs you. The limit belongs to the installation, the
// notification choice to this browser only.
const jobsUi = {
  limit: $("maxRunningJobs"), save: $("saveRunningJobsButton"), status: $("runningJobsStatus"),
  alerts: $("attentionAlerts"), alertsHelp: $("attentionAlertsHelp"),
};
let runningJobsBusy = false;

async function settingsRequest(body) {
  const options = { headers: { "X-TCS-Prover-Token": sessionToken }, cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/settings", options);
  const data = await response.json().catch(() => ({}));
  if (response.status === 404) throw new Error("This TCS Prover server has no running-jobs limit. Restart TCS Prover to update it.");
  if (!response.ok) throw new Error(data.error || `The request failed (HTTP ${response.status}).`);
  return data;
}

function runningJobsText(data) {
  const limit = Number(data.maxRunningJobs || 0);
  return `${limit ? `At most ${limit} job${limit === 1 ? "" : "s"} at once` : "No limit"} · `
    + `${Number(data.runningJobs || 0)} running · ${Number(data.queuedJobs || 0)} queued now.`;
}

async function loadRunningJobs() {
  try {
    const data = await settingsRequest();
    if (document.activeElement !== jobsUi.limit) jobsUi.limit.value = String(data.maxRunningJobs ?? 0);
    jobsUi.status.textContent = runningJobsText(data);
  } catch (error) {
    jobsUi.status.textContent = error.message;
  }
}

async function saveRunningJobs() {
  if (runningJobsBusy) return;
  const value = jobsUi.limit.value.trim();
  if (!/^\d+$/.test(value)) {
    jobsUi.status.textContent = "Enter a whole number of jobs: 0 for no limit.";
    jobsUi.limit.focus();
    return;
  }
  runningJobsBusy = jobsUi.save.disabled = true;
  try {
    const data = await settingsRequest({ maxRunningJobs: Number(value) });
    jobsUi.limit.value = String(data.maxRunningJobs);
    jobsUi.status.textContent = `Saved. ${runningJobsText(data)}`;
  } catch (error) {
    jobsUi.status.textContent = error.message;
  } finally {
    runningJobsBusy = jobsUi.save.disabled = false;
  }
}

// The workbench shell asks while the page loads, before the declarations below have run: no
// constant here.
function attentionAlertsOn() {
  try {
    return typeof Notification !== "undefined" && Notification.permission === "granted"
      && localStorage.getItem("tcs-prover-attention-alerts") === "1";
  } catch (_) {
    return false;
  }
}

function syncAttentionAlerts() {
  const supported = typeof Notification !== "undefined";
  const blocked = supported && Notification.permission === "denied";
  jobsUi.alerts.checked = attentionAlertsOn();
  jobsUi.alerts.disabled = !supported || blocked;
  jobsUi.alertsHelp.textContent = !supported ? "This browser cannot show notifications; the Overview lists these jobs."
    : blocked ? "Notifications are blocked for this page in the browser's site settings; the Overview lists these jobs."
    : "While TCS Prover is open in a tab, a notification says when a job first needs you: a CLI login, a usage "
      + "limit, a rejected API key, or a model or setting to fix. The Overview lists these jobs either way.";
}

// One notification per job and reason, when it first appears (not for those already listed when
// the page opened); a job that needs you again later is announced again.
const attentionSeen = new Set();
let attentionPrimed = false;
function notifyAttention(batches) {
  const items = batches.flatMap((batch) => batch.attention || []);
  const key = (item) => `${item.runId}\n${item.kind}`;
  const fresh = attentionPrimed ? items.filter((item) => !attentionSeen.has(key(item))) : [];
  attentionPrimed = true;
  attentionSeen.clear();
  for (const item of items) attentionSeen.add(key(item));
  if (!fresh.length || !attentionAlertsOn()) return;
  for (const item of fresh) {
    try {
      const note = new Notification(`TCS Prover · ${item.title}`, {
        body: `${item.job || item.runId}: ${item.text}`, tag: `tcs-prover-${item.runId}`,
      });
      note.onclick = () => {
        window.focus();
        openAttentionJob(item.runId);
        note.close();
      };
    } catch (_) {
      // A browser that refuses page notifications here keeps the Overview's list.
    }
  }
}

jobsUi.save.onclick = saveRunningJobs;
jobsUi.limit.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    saveRunningJobs();
  }
});
jobsUi.alerts.onchange = async () => {
  let on = jobsUi.alerts.checked;
  if (on && Notification.permission !== "granted") on = await Notification.requestPermission() === "granted";
  try { localStorage.setItem("tcs-prover-attention-alerts", on ? "1" : "0"); } catch (_) {}
  syncAttentionAlerts();
};
// Read when the settings open, like the API model list.
ui.input.querySelector("details.advanced")?.addEventListener("toggle", (event) => {
  if (event.target.open) loadRunningJobs();
});
syncAttentionAlerts();

// Reduced motion: two page calls ask for a smooth scroll (the create error notice and a folder
// run's result). An explicit behavior wins over the stylesheet's preference, so on these
// elements a smooth scroll jumps instead.
(() => {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  for (const node of [ui.notice, ui.batchStatus]) {
    const scroll = node?.scrollIntoView;
    if (typeof scroll !== "function") continue;
    node.scrollIntoView = (options) => scroll.call(node, reduce.matches && options?.behavior === "smooth"
      ? { ...options, behavior: "auto" } : options);
  }
})();
