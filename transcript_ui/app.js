"use strict";

const token = location.hash.slice(1) || sessionStorage.getItem("transcript-token") || "";
if (location.hash) {
  sessionStorage.setItem("transcript-token", token);
  history.replaceState(null, "", location.pathname);
}

const $ = (id) => document.getElementById(id);
const ui = {
  liveDot: $("liveDot"), loadState: $("loadState"), runName: $("runName"),
  runPath: $("runPath"), elapsed: $("elapsed"), entryCount: $("entryCount"),
  fileSize: $("fileSize"), stageList: $("stageList"), allStages: $("allStages"),
  categoryList: $("categoryList"), rootOnly: $("rootOnly"),
  compactMode: $("compactMode"), search: $("search"),
  visibleSummary: $("visibleSummary"), jumpLatest: $("jumpLatest"),
  timeline: $("timeline"), emptyState: $("emptyState"), loadMore: $("loadMore"),
  scanProgress: $("scanProgress"), scanBar: $("scanBar"), template: $("entryTemplate"),
};

const categories = {
  reasoning: "Reasoning", message: "Model messages", critic: "Critic checks",
  result: "Results", tool: "Tools & subagents", status: "Status",
  prompt: "Prompts", diagnostic: "Diagnostics",
};
const state = {
  meta: null, entries: [], ids: new Set(), offset: 0, eof: false,
  loading: false, loadingAll: false, pauseRequested: false, autoLoad: true,
  selectedStage: "", query: "", rootOnly: false,
  enabledCategories: new Set(Object.keys(categories).filter((name) => name !== "prompt")),
  timer: null,
};

async function api(path) {
  const response = await fetch(path, { headers: { "X-Transcript-Token": token } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not read transcript.");
  return data;
}

function formatBytes(value) {
  let size = Number(value || 0);
  const units = ["B", "KB", "MB", "GB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function relativeTime(value) {
  if (!value || !state.meta?.startedAt) return "—";
  const seconds = Math.max(0, Math.floor((new Date(value) - new Date(state.meta.startedAt)) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function currentElapsed() {
  if (!state.meta?.startedAt) return "—";
  const end = state.entries.at(-1)?.time || state.meta.startedAt;
  return relativeTime(end);
}

function controlButton(label, count, active, color) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = active ? "active" : "";
  const name = document.createElement("span");
  const dot = document.createElement("i");
  if (color) dot.style.background = color;
  name.append(dot, document.createTextNode(label));
  const total = document.createElement("small");
  total.textContent = count.toLocaleString();
  button.append(name, total);
  return button;
}

function renderControls() {
  const stageCounts = {};
  const categoryCounts = {};
  for (const entry of state.entries) {
    stageCounts[entry.stage] = (stageCounts[entry.stage] || 0) + 1;
    categoryCounts[entry.category] = (categoryCounts[entry.category] || 0) + 1;
  }
  ui.stageList.replaceChildren();
  for (const [stage, label] of Object.entries(state.meta?.stages || {})) {
    if (!stageCounts[stage]) continue;
    const button = controlButton(label, stageCounts[stage], state.selectedStage === stage);
    button.classList.add("stage-button");
    button.onclick = () => {
      state.selectedStage = state.selectedStage === stage ? "" : stage;
      render();
    };
    ui.stageList.append(button);
  }
  ui.categoryList.replaceChildren();
  for (const [category, label] of Object.entries(categories)) {
    const button = controlButton(
      label, categoryCounts[category] || 0, state.enabledCategories.has(category)
    );
    button.classList.add("category-button");
    button.onclick = () => {
      if (state.enabledCategories.has(category)) state.enabledCategories.delete(category);
      else state.enabledCategories.add(category);
      render();
    };
    ui.categoryList.append(button);
  }
}

function matches(entry) {
  if (state.selectedStage && entry.stage !== state.selectedStage) return false;
  if (!state.enabledCategories.has(entry.category)) return false;
  if (state.rootOnly && !entry.root) return false;
  if (!state.query) return true;
  const checkText = (entry.checks || []).map((check) => Object.values(check).join(" ")).join(" ");
  return `${entry.label} ${entry.text} ${entry.stageLabel} ${checkText}`
    .toLocaleLowerCase().includes(state.query);
}

function appendBody(container, text) {
  const value = String(text || "");
  if (value.length <= 900) {
    container.textContent = value || "No additional details.";
    return;
  }
  const preview = document.createElement("div");
  preview.className = "preview";
  preview.textContent = `${value.slice(0, 760).trimEnd()}…`;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = `Show full entry · ${value.length.toLocaleString()} characters`;
  const full = document.createElement("div");
  full.className = "full";
  full.textContent = value;
  details.append(summary, full);
  container.append(preview, details);
}

function entryNode(entry) {
  const row = ui.template.content.firstElementChild.cloneNode(true);
  row.dataset.entryId = entry.id;
  row.dataset.category = entry.category;
  const category = row.querySelector(".category-badge");
  category.textContent = categories[entry.category] || entry.category;
  row.querySelector("h3").textContent = entry.label;
  row.querySelector(".stage-name").textContent = entry.stageLabel;
  row.querySelector(".actor").textContent = entry.root ? "root model" : "subagent";
  const time = row.querySelector("time");
  time.textContent = relativeTime(entry.time);
  if (entry.time) time.title = new Date(entry.time).toLocaleString();
  appendBody(row.querySelector(".entry-body"), entry.text);

  if (entry.model || entry.effort || entry.speed) {
    const meta = row.querySelector(".model-meta");
    meta.hidden = false;
    meta.textContent = [entry.model, entry.effort && `effort ${entry.effort}`,
      entry.speed && `speed ${entry.speed}`].filter(Boolean).join(" · ");
  }
  if (entry.verdict) {
    const verdict = row.querySelector(".verdict-badge");
    verdict.hidden = false;
    verdict.textContent = entry.fixed ? `${entry.verdict} · repaired` : entry.verdict;
    verdict.classList.add(String(entry.verdict).toLowerCase());
  }
  const checks = row.querySelector(".critic-checks");
  for (const check of entry.checks || []) {
    const item = document.createElement("li");
    item.className = String(check.verdict || "").toLowerCase();
    const verdict = document.createElement("strong");
    verdict.textContent = `${String(check.verdict || "check").toUpperCase()} · ${check.focus || "audit"}`;
    item.append(verdict, document.createTextNode(` — ${check.report || "No report."}`));
    checks.append(item);
  }
  return row;
}

function render() {
  const openEntries = new Set(
    [...ui.timeline.querySelectorAll(".entry-card details[open]")]
      .map((details) => details.closest(".entry-card")?.dataset.entryId)
      .filter(Boolean)
  );
  const scrollPosition = window.scrollY;
  renderControls();
  const visible = state.entries.filter(matches);
  const fragment = document.createDocumentFragment();
  for (const entry of visible) fragment.append(entryNode(entry));
  ui.timeline.replaceChildren(fragment);
  for (const row of ui.timeline.querySelectorAll(".entry-card")) {
    if (openEntries.has(row.dataset.entryId)) {
      const details = row.querySelector("details");
      if (details) details.open = true;
    }
  }
  ui.timeline.classList.toggle("compact", ui.compactMode.checked);
  ui.emptyState.hidden = visible.length > 0 || state.loading;
  ui.visibleSummary.textContent = `Showing ${visible.length.toLocaleString()} of ${state.entries.length.toLocaleString()} loaded entries`;
  ui.entryCount.textContent = state.entries.length.toLocaleString();
  ui.elapsed.textContent = currentElapsed();
  ui.loadMore.hidden = state.eof && !state.loadingAll;
  ui.loadMore.disabled = state.loading && !state.loadingAll;
  window.scrollTo(0, scrollPosition);
}

function updateScanProgress() {
  const size = Number(state.meta?.size || 0);
  const percent = size ? Math.min(100, (state.offset / size) * 100) : 0;
  ui.scanBar.style.width = `${percent}%`;
  ui.scanProgress.textContent = `${formatBytes(state.offset)} of ${formatBytes(size)} read · ${percent.toFixed(1)}%`;
}

async function loadBatch(renderAfter = true) {
  if (state.loading) return false;
  state.loading = true;
  try {
    let batch;
    let attempts = 0;
    do {
      batch = await api(`/api/events?offset=${state.offset}&limit=250`);
      state.offset = batch.nextOffset;
      state.eof = batch.eof;
      for (const entry of batch.entries) {
        if (!state.ids.has(entry.id)) {
          state.ids.add(entry.id);
          state.entries.push(entry);
        }
      }
      attempts += 1;
    } while (!batch.entries.length && !state.eof && attempts < 4);
    state.meta.size = batch.fileSize;
    ui.fileSize.textContent = formatBytes(batch.fileSize);
    updateScanProgress();
    ui.liveDot.className = "live-dot ready";
    if (renderAfter) render();
    return true;
  } catch (error) {
    ui.liveDot.className = "live-dot error";
    ui.loadState.textContent = error.message;
    state.autoLoad = false;
    return false;
  } finally {
    state.loading = false;
  }
}

async function loadEntireTranscript(liveCheck = false) {
  if (state.loadingAll) return;
  state.loadingAll = true;
  state.pauseRequested = false;
  ui.loadMore.hidden = false;
  ui.loadMore.disabled = false;
  ui.loadMore.textContent = "Pause full-file loading";
  ui.loadState.textContent = liveCheck
    ? "Checking for new transcript activity…"
    : "Reading the complete transcript…";
  const initialEntryCount = state.entries.length;
  const initialOffset = state.offset;
  let batches = 0;
  do {
    const previousOffset = state.offset;
    const loaded = await loadBatch(false);
    if (!loaded || state.offset === previousOffset) break;
    batches += 1;
    if (batches === 1 || batches % 8 === 0) {
      render();
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  } while (!state.eof && !state.pauseRequested);
  state.loadingAll = false;
  if (
    !liveCheck || state.pauseRequested
    || state.entries.length !== initialEntryCount
    || state.offset !== initialOffset
  ) {
    render();
  }
  if (state.pauseRequested) {
    state.autoLoad = false;
    ui.loadMore.hidden = false;
    ui.loadMore.textContent = "Resume full-file loading";
    ui.loadState.textContent = "Full-file loading paused";
  } else if (state.eof) {
    ui.loadState.textContent = "Complete transcript loaded · watching for updates";
  }
}

async function pollLive() {
  clearTimeout(state.timer);
  if (state.autoLoad && !state.loadingAll) await loadEntireTranscript(true);
  state.timer = setTimeout(pollLive, 2000);
}

async function start() {
  try {
    state.meta = await api("/api/meta");
    ui.runName.textContent = state.meta.name;
    ui.runPath.textContent = state.meta.path;
    ui.fileSize.textContent = formatBytes(state.meta.size);
    updateScanProgress();
    await loadEntireTranscript(false);
    pollLive();
  } catch (error) {
    ui.liveDot.className = "live-dot error";
    ui.loadState.textContent = error.message;
  }
}

ui.allStages.onclick = () => { state.selectedStage = ""; render(); };
ui.loadMore.onclick = () => {
  if (state.loadingAll) {
    state.pauseRequested = true;
    ui.loadMore.disabled = true;
    ui.loadMore.textContent = "Pausing…";
  } else {
    state.autoLoad = true;
    loadEntireTranscript(false);
  }
};
ui.jumpLatest.onclick = () => window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
ui.search.oninput = () => { state.query = ui.search.value.trim().toLocaleLowerCase(); render(); };
ui.rootOnly.onchange = () => { state.rootOnly = ui.rootOnly.checked; render(); };
ui.compactMode.onchange = render;

start();
