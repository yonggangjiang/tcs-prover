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
  algorithmicFields: $("algorithmicFields"),
  latexFields: $("latexFields"), latexInput: $("latexInput"),
  modelOfComputation: $("modelOfComputation"),
  problemDescription: $("problemDescription"), goal: $("goal"),
  modelPresets: $("modelPresets"), problemPresets: $("problemPresets"),
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
  skipReviewSetting: $("skipReviewSetting"),
  skipStatementReview: $("skipStatementReview"),
  criticRoundSetting: $("criticRoundSetting"),
  thinkingHoursSetting: $("thinkingHoursSetting"),
  editPrompts: $("editPromptsButton"), promptDialog: $("promptDialog"),
  promptTabs: $("promptTabs"), promptEditor: $("promptEditor"),
  promptEditorLabel: $("promptEditorLabel"),
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
  reviewHeading: $("reviewHeading"),
};
ui.problemModes = document.querySelectorAll('input[name="problemMode"]');

let state = {
  phase: "input", problemMode: "statement", skipStatementReview: false,
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
    const title = document.createElement("strong");
    title.textContent = job.title || job.draft?.trim().split("\n")[0]
      || "Untitled problem";
    const status = document.createElement("span");
    status.className = `job-status ${job.phase}`;
    const labels = {
      reviewing: "Checking statement", reviewed: "Waiting for approval",
      running: "Running", stopping: "Stopping", done: "Finished",
    };
    status.textContent = labels[job.phase] || job.phase;
    copy.append(title, status);
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
    const remove = document.createElement("button");
    remove.className = "ghost compact job-delete";
    remove.textContent = "Delete";
    remove.disabled = ["reviewing", "running", "stopping"].includes(job.phase);
    remove.title = remove.disabled ? "Stop this job before deleting it." : "";
    remove.onclick = () => deleteJob(job, title.textContent);
    actions.append(open, separate, remove);
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
  const algorithmic = mode === "algorithmic";
  const latexOnly = mode === "latex";
  const skipReview = !algorithmic && !latexOnly
    && ui.skipStatementReview.checked;
  for (const input of ui.problemModes) input.checked = input.value === mode;
  show(ui.statementFields, !algorithmic && !latexOnly);
  show(ui.algorithmicFields, algorithmic);
  show(ui.latexFields, latexOnly);
  show(ui.reviewModelSetting, !algorithmic && !latexOnly);
  show(ui.authorModelSetting, !latexOnly);
  show(ui.criticModelSetting, !latexOnly);
  show(ui.writerModelSetting, true);
  show(ui.reviewPromptTab, !algorithmic && !latexOnly);
  show(ui.authorPromptTab, !latexOnly);
  show(ui.criticPromptTab, !latexOnly);
  show(ui.finalPromptTab, true);
  show(ui.skipReviewSetting, !algorithmic && !latexOnly);
  show(ui.criticRoundSetting, !latexOnly);
  show(ui.thinkingHoursSetting, !latexOnly);
  ui.problem.required = !algorithmic && !latexOnly;
  for (const field of [
    ui.modelOfComputation, ui.problemDescription, ui.goal,
  ]) field.required = algorithmic;
  ui.latexInput.required = latexOnly;
  ui.check.textContent = latexOnly ? "Polish LaTeX"
    : algorithmic ? "Start proof"
    : skipReview ? "Start proof author" : "Check statement";
  ui.introDescription.textContent = latexOnly
    ? "Provide an existing theorem and proof. Only the final LaTeX editor will run."
    : algorithmic
    ? "Define the computational model, problem, and asymptotic goal. The proof "
      + "author will start immediately, followed by independent audit and LaTeX editing."
    : skipReview
      ? "Enter the exact statement to send directly to the proof author, followed "
        + "by independent audit and LaTeX editing."
      : "Start with a rough TCS problem. The agent will clarify it, ask for approval, "
        + "solve it, audit it, and produce clean LaTeX.";
  if (algorithmic && activePrompt === "review" && ui.promptDialog.open) {
    selectPrompt("author");
  }
  if (latexOnly && activePrompt !== "final" && ui.promptDialog.open) {
    selectPrompt("final");
  }
  updateModelSummary();
}

function renderPresetOptions(container, entries, field) {
  const buttons = (entries || []).map((entry) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "preset-option";
    button.textContent = entry.name;
    button.setAttribute("aria-label", `${entry.name}: fill this description`);
    button.onclick = () => {
      field.value = entry.description;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.focus();
    };
    button._description = entry.description;
    return button;
  });
  const syncSelection = () => {
    for (const button of buttons) {
      const selected = field.value === button._description;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    }
  };
  field.oninput = syncSelection;
  container.replaceChildren(...buttons);
  syncSelection();
}

function renderAlgorithmicPresets(source) {
  const presets = source.workflow?.settings?.algorithmic_presets || {};
  renderPresetOptions(ui.modelPresets, presets.models, ui.modelOfComputation);
  renderPresetOptions(ui.problemPresets, presets.problems, ui.problemDescription);
}

// Keep the compact footer label synchronized with every model setting.
function updateModelSummary() {
  const name = (model) => model.split("-").at(-1)
    .replace(/^./, (letter) => letter.toUpperCase());
  const review = (
    selectedProblemMode() === "algorithmic" || ui.skipStatementReview.checked
  ) ? ""
    : `${name(ui.reviewModel.value)}/${name(ui.reviewEffort.value)} review · `;
  const speed = ui.speedMode.value === "standard"
    ? "Standard speed" : "Fast 1.5×";
  if (selectedProblemMode() === "latex") {
    ui.modelSummary.textContent = `${speed} · `
      + `${name(ui.writerModel.value)}/${name(ui.writerEffort.value)} writer`;
    return;
  }
  ui.modelSummary.textContent = `${speed} · ` + review
    + `${name(ui.authorModel.value)}/${name(ui.authorEffort.value)} author · `
    + `${name(ui.criticModel.value)}/${name(ui.criticEffort.value)} critic · `
    + `${name(ui.writerModel.value)}/${name(ui.writerEffort.value)} writer`;
}

const promptLabels = {
  review: "Reviewer prompt", author: "Author prompt",
  critic: "Critic prompt", final: "Final writer prompt",
};

function syncPrompts(source = state) {
  const defaults = source.workflow?.settings?.prompts || {};
  let saved = {};
  if (source.phase === "input") {
    try {
      saved = JSON.parse(localStorage.getItem(promptStorageKey) || "{}");
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

function selectPrompt(name) {
  if (ui.promptDialog.open && promptDrafts[activePrompt] !== undefined) {
    promptDrafts[activePrompt] = ui.promptEditor.value;
  }
  activePrompt = name;
  ui.promptEditor.value = promptDrafts[name] || "";
  ui.promptEditorLabel.textContent = promptLabels[name];
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
    : selectedProblemMode() === "algorithmic" ? "author" : "review";
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
  try {
    localStorage.setItem(promptStorageKey, JSON.stringify(promptValues));
  } catch (_) {
    // The current tab still retains them if browser storage is unavailable.
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
  const itemId = params.itemId || item.id || "";
  const root = entry.root !== false;
  const keyBase = itemId || `${entry.time}:${entry.kind}:${name}`;
  const text = entry.text || "";

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
        ? "The critic repaired every reported issue; a fresh critic will recheck it."
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

  if (name === "item/reasoning/summaryTextDelta") {
    return {
      key: `reasoning:${itemId}`, type: "reasoning",
      label: root ? "Reasoning summary" : "Subagent reasoning",
      text: params.delta || "", time: entry.time, append: true,
    };
  }
  if (["item/completed", "item.completed"].includes(name) && item.type === "reasoning") {
    return {
      key: `reasoning:${itemId}`, type: "reasoning",
      label: root ? "Reasoning summary" : "Subagent reasoning",
      text: item.text || (item.summary || []).join("\n"),
      time: entry.time, replace: true,
    };
  }
  if (name === "item/agentMessage/delta") {
    return {
      key: `agent:${itemId}`, type: "agent",
      label: root ? "Author" : "Subagent", text: params.delta || "",
      time: entry.time, append: true,
    };
  }
  if (
    ["item/completed", "item.completed"].includes(name)
    && ["agentMessage", "agent_message"].includes(item.type)
  ) {
    return {
      key: `agent:${itemId}`, type: "agent",
      label: root ? "Author" : "Subagent", text: item.text || "",
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
      label: root ? "Tool activity" : "Subagent tool",
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
  if (name === "turn/completed") {
    return {
      key: `turn:${params.turn?.id || entry.time}`, type: "status",
      label: "Turn completed", text: params.turn?.status || "completed",
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
      key: `detail-prompt:${entry.time}`, label: "Prompt to OpenAI",
      text: entry.text,
    };
  }
  if (
    ["review_result", "critic_result", "final_result", "failure_result"].includes(entry.kind)
    && entry.text
  ) {
    return {
      key: `detail-result:${entry.time}`, label: "Returned text from OpenAI",
      text: entry.text,
    };
  }
  if (entry.kind !== "codex_event" || entry.root === false) return null;
  const event = entry.event || {};
  const params = event.params || {};
  const item = params.item || {};
  const itemId = params.itemId || item.id || entry.time;
  if (event.method === "item/agentMessage/delta" && params.delta) {
    return {
      key: `detail-response:${itemId}`, label: "Returned text from OpenAI",
      text: params.delta, append: true,
    };
  }
  if (
    event.method === "item/completed"
    && ["agentMessage", "agent_message"].includes(item.type)
    && item.text
  ) {
    return {
      key: `detail-response:${itemId}`, label: "Returned text from OpenAI",
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
  const startsAtAuthor = state.problemMode === "algorithmic"
    || state.skipStatementReview;
  const seen = new Set((state.trace || []).map(
    (entry) => entry.node || nodeFromStage(entry.stage)
  ));
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
    if (name === "failure_summary") {
      const condition = document.createElement("span");
      condition.className = "failure-condition";
      condition.textContent = "At time limit";
      copy.append(condition);
    }
    row.append(dot, copy);
    return row;
  };
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
  loopTitle.textContent = "Repeat until a clean PASS";
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
    `At the time limit, step ${startsAtAuthor ? 1 : 2} stops and returns a failure summary`,
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
  selfRoute.textContent = "↻ Critic fixes → fresh critic";
  const author = makeNode("author", startsAtAuthor ? "1" : "2");
  const critic = makeNode("critic", startsAtAuthor ? "2" : "3");
  const passStem = document.createElement("li");
  passStem.className = "critic-pass-stem";
  passStem.setAttribute("aria-hidden", "true");
  loopNodes.append(
    author, candidateRoute, critic, selfRoute, passStem, rejectRoute,
  );
  loop.append(loopTitle, loopNodes);

  // Failure branches left; a clean pass runs directly from critic to editor.
  const branch = document.createElement("li");
  branch.className = "workflow-branch";
  const passRoute = arrow("Clean PASS only", true);
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
    ui.lastActivity.textContent = `Updated ${ago < 2 ? "just now" : `${ago}s ago`} · `
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
  show(ui.input, phase === "input");
  show(ui.review, phase === "reviewed");
  show(ui.run, ["reviewing", "running", "stopping", "done"].includes(phase));
  show(ui.workflowRail, phase !== "input");
  show(ui.activityToggle, Boolean(currentJob));
  ui.notice.textContent = state.error || "";
  show(ui.notice, Boolean(state.error));

  if (phase === "input" && previousPhase !== "input") {
    const rounds = state.workflow?.settings?.critic_rounds || {};
    const hours = state.workflow?.settings?.thinking_hours || {};
    ui.problem.value = state.draft || "";
    ui.modelOfComputation.value = state.modelOfComputation || "";
    ui.problemDescription.value = state.problemDescription || "";
    ui.goal.value = state.goal || "";
    ui.latexInput.value = state.latexInput || "";
    renderAlgorithmicPresets(state);
    ui.reviewModel.value = state.reviewModel || "gpt-5.6-sol";
    ui.authorModel.value = state.authorModel || "gpt-5.6-sol";
    ui.criticModel.value = state.criticModel || "gpt-5.6-sol";
    ui.writerModel.value = state.writerModel || "gpt-5.6-sol";
    ui.reviewEffort.value = state.reviewEffort || state.reasoningEffort || "ultra";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "ultra";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "ultra";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "ultra";
    ui.speedMode.value = state.speedMode || "fast";
    ui.skipStatementReview.checked = Boolean(state.skipStatementReview);
    syncPrompts(state);
    updateModelSummary();
    ui.criticRounds.value = state.criticRounds || 4;
    ui.criticRounds.min = rounds.minimum || 1;
    ui.criticRounds.max = rounds.maximum || 100;
    ui.thinkingHours.value = state.thinkingHours || 24;
    ui.thinkingHours.min = hours.minimum || 0.01;
    ui.thinkingHours.max = hours.maximum || 168;
    setProblemMode(state.problemMode || "statement");
  }
  if (phase === "reviewed" && (previousPhase !== "reviewed" || reviewPending)) {
    ui.reviewModel.value = state.reviewModel || "gpt-5.6-sol";
    ui.authorModel.value = state.authorModel || "gpt-5.6-sol";
    ui.criticModel.value = state.criticModel || "gpt-5.6-sol";
    ui.writerModel.value = state.writerModel || "gpt-5.6-sol";
    ui.reviewEffort.value = state.reviewEffort || state.reasoningEffort || "ultra";
    ui.authorEffort.value = state.authorEffort || state.reasoningEffort || "ultra";
    ui.criticEffort.value = state.criticEffort || state.reasoningEffort || "ultra";
    ui.writerEffort.value = state.writerEffort || state.reasoningEffort || "ultra";
    ui.speedMode.value = state.speedMode || "fast";
    syncPrompts(state);
    updateModelSummary();
    ui.proposed.value = state.review.statement;
    ui.notes.replaceChildren();
    appendFormattedText(ui.notes, state.review.notes);
    ui.feedback.value = "";
    ui.criticRounds.value = state.criticRounds || 4;
    ui.thinkingHours.value = state.thinkingHours || 24;
    checkEdited();
    reviewPending = false;
  }

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
  const authorLimit = Number(state.thinkingHours || 24);
  const maximumAuthorLimit = Number(
    state.workflow?.settings?.thinking_hours?.maximum || 168
  );
  const canSetAuthorLimit = phase === "running"
    && state.stage === "solve" && state.activeNode === "author";
  show(ui.authorTimeLimitControl, canSetAuthorLimit);
  const authorLimitText = authorLimit.toLocaleString(undefined, {
    maximumFractionDigits: 2,
  });
  ui.authorLimitSummary.textContent = `Author limit: ${authorLimitText} hours`;
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
    const heading = phase === "reviewed" ? ui.reviewHeading
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
  const skipReview = !feedback && ui.skipStatementReview.checked;
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

async function startAlgorithmic() {
  const fields = [
    [ui.modelOfComputation, "model of computation"],
    [ui.problemDescription, "problem description"],
    [ui.goal, "asymptotic upper- or lower-bound goal"],
  ];
  const missing = fields.find(([field]) => !field.value.trim());
  if (missing) {
    ui.notice.textContent = `Enter the ${missing[1]}.`;
    show(ui.notice, true);
    missing[0].focus();
    return;
  }
  clearTimeout(timer);
  clearTimeout(jobsTimer);
  if (!promptValues.author) syncPrompts();
  try {
    const next = await request("/algorithmic", {
      modelOfComputation: ui.modelOfComputation.value,
      problemDescription: ui.problemDescription.value,
      goal: ui.goal.value,
      authorModel: ui.authorModel.value,
      criticModel: ui.criticModel.value,
      writerModel: ui.writerModel.value,
      authorEffort: ui.authorEffort.value,
      criticEffort: ui.criticEffort.value,
      writerEffort: ui.writerEffort.value,
      authorPrompt: promptValues.author,
      criticPrompt: promptValues.critic,
      finalPrompt: promptValues.final,
      criticRounds: Number(ui.criticRounds.value),
      thinkingHours: Number(ui.thinkingHours.value),
      speedMode: ui.speedMode.value,
    });
    currentJob = next.runId;
    history.pushState(null, "", jobUrl(currentJob));
    render(next);
  } catch (error) {
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
ui.check.onclick = () => selectedProblemMode() === "algorithmic"
  ? startAlgorithmic() : selectedProblemMode() === "latex"
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
ui.skipStatementReview.onchange = () => setProblemMode(selectedProblemMode());
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
};
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
ui.stop.onclick = () => act("/stop");
ui.home.onclick = goHome;
ui.reviewHome.onclick = goHome;
window.onpopstate = () => {
  currentJob = new URLSearchParams(location.search).get("job") || "";
  clearJobView();
  refresh();
};

refresh();
