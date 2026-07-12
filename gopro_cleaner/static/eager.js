const state = {
  phase: "clean", // unified review: trim marks + label in one pass
  videos: [],
  index: -1,
  tasks: [],
  scanRoot: "",
  labelRoot: "",
  pendingIn: null,
  pendingClip: null,
  savedClips: [],
  trimPollTimer: null,
  globalTrimPollTimer: null,
  globalTrimActive: 0,
  globalTrimJobs: [],
  trimEtaTotal: 0,
  labelRefreshTimer: null,
  labelScanToken: 0,
  currentHasGpmf: null,
  donePaths: new Set(),
  labeledTasks: {},
  trimmingPaths: new Set(),
  lastLabelTask: "",
  busy: false,
  previewToken: 0,
  seekTimer: null,
  pendingSeek: null,
  lastVideoPath: "",
  snapshots: null,
  snapshotIndex: 0,
  snapshotBuildToken: 0,
  scrubTime: 0,
  snapshotPurpose: "clean",
  labelProgress: null,
  workspaces: [],
  activeWorkspaceId: null,
  perf: {
    lite_mode: false,
    prefetch: true,
    snapshot_poll_ms: 1000,
    trim_poll_ms: 1200,
    hint: "",
  },
};

const el = {
  phaseClean: document.getElementById("phase-clean"),
  phaseLabel: document.getElementById("phase-label"),
  sourcePath: document.getElementById("source-path"),
  browseFolderBtn: document.getElementById("browse-folder-btn"),
  sdCardSelect: document.getElementById("sd-card-select"),
  refreshSdBtn: document.getElementById("refresh-sd-btn"),
  sdCardHint: document.getElementById("sd-card-hint"),
  cameraSelect: document.getElementById("camera-select"),
  scanBtn: document.getElementById("scan-btn"),
  scanSummary: document.getElementById("scan-summary"),
  fileFilter: document.getElementById("file-filter"),
  fileList: document.getElementById("file-list"),
  listTitle: document.getElementById("list-title"),
  listSummary: document.getElementById("list-summary"),
  playerWrap: document.getElementById("player-wrap"),
  player: document.getElementById("player"),
  scrubTrack: document.getElementById("scrub-track"),
  scrubFill: document.getElementById("scrub-fill"),
  scrubPlayhead: document.getElementById("scrub-playhead"),
  previewStatus: document.getElementById("preview-status"),
  loadingOverlay: document.getElementById("loading-overlay"),
  loadingTitle: document.getElementById("loading-title"),
  loadingDetail: document.getElementById("loading-detail"),
  loadingBarFill: document.getElementById("loading-bar-fill"),
  loadingHint: document.getElementById("loading-hint"),
  filmstripPanel: document.getElementById("filmstrip-panel"),
  filmstripMeta: document.getElementById("filmstrip-meta"),
  filmstrip: document.getElementById("filmstrip"),
  contextBanner: document.getElementById("context-banner"),
  contextMessage: document.getElementById("context-message"),
  snapPrevBtn: document.getElementById("snap-prev-btn"),
  snapNextBtn: document.getElementById("snap-next-btn"),
  fineBackBtn: document.getElementById("fine-back-btn"),
  fineFwdBtn: document.getElementById("fine-fwd-btn"),
  markStartBtn: document.getElementById("mark-start-btn"),
  markEndBtn: document.getElementById("mark-end-btn"),
  markSection: document.getElementById("mark-section"),
  currentName: document.getElementById("current-name"),
  currentMeta: document.getElementById("current-meta"),
  timeDisplay: document.getElementById("time-display"),
  undoClipBtn: document.getElementById("undo-clip-btn"),
  pendingIn: document.getElementById("pending-in"),
  gpmfStatus: document.getElementById("gpmf-status"),
  trimProgressPanel: document.getElementById("trim-progress-panel"),
  trimActiveCount: document.getElementById("trim-active-count"),
  trimEtaTotal: document.getElementById("trim-eta-total"),
  trimProgressFill: document.getElementById("trim-progress-fill"),
  trimProgressList: document.getElementById("trim-progress-list"),
  labelTrimBanner: document.getElementById("label-trim-banner"),
  labelTrimBannerText: document.getElementById("label-trim-banner-text"),
  clipList: document.getElementById("clip-list"),
  cleanPanel: document.getElementById("clean-panel"),
  labelPanel: document.getElementById("label-panel"),
  trimBtn: document.getElementById("trim-btn"),
  deleteFileBtn: document.getElementById("delete-file-btn"),
  nextCleanBtn: document.getElementById("next-clean-btn"),
  taskSearch: document.getElementById("task-search"),
  taskList: document.getElementById("task-list"),
  taskSelect: document.getElementById("task-select"),
  taskSelectedHint: document.getElementById("task-selected-hint"),
  newTaskInput: document.getElementById("new-task-input"),
  addTaskBtn: document.getElementById("add-task-btn"),
  taskAddedMsg: document.getElementById("task-added-msg"),
  labelBtn: document.getElementById("label-btn"),
  labelProgress: document.getElementById("label-progress"),
  labelProgressCount: document.getElementById("label-progress-count"),
  labelProgressLabel: document.getElementById("label-progress-label"),
  labelProgressDetail: document.getElementById("label-progress-detail"),
  recheckLabelBtn: document.getElementById("recheck-label-btn"),
  workTimer: document.getElementById("work-timer"),
  workTimerStatus: document.getElementById("work-timer-status"),
  workCleanTime: document.getElementById("work-clean-time"),
  workLabelTime: document.getElementById("work-label-time"),
  workTotalTime: document.getElementById("work-total-time"),
  workTimerReset: document.getElementById("work-timer-reset"),
  statusLine: document.getElementById("status-line"),
  footerHints: document.getElementById("footer-hints"),
  appVersion: document.getElementById("app-version"),
  cardTabList: document.getElementById("card-tab-list"),
  addCardTab: document.getElementById("add-card-tab"),
};

el.player.muted = true;
el.player.pause();

const WORK_IDLE_MS = 90_000;
const workTimer = {
  root: "",
  cleanMs: 0,
  labelMs: 0,
  active: false,
  lastActivityAt: 0,
  lastTickAt: 0,
  tickHandle: null,
  saveHandle: null,
};

function formatWorkHms(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function workStorageKey(root) {
  return `eager-work-timer:${root || "default"}`;
}

function loadWorkTimer(root) {
  if (!root) return;
  try {
    const raw = localStorage.getItem(workStorageKey(root));
    if (!raw) {
      workTimer.root = root;
      workTimer.cleanMs = 0;
      workTimer.labelMs = 0;
      return;
    }
    const data = JSON.parse(raw);
    workTimer.root = root;
    workTimer.cleanMs = Math.max(0, Number(data.cleanMs) || 0);
    workTimer.labelMs = Math.max(0, Number(data.labelMs) || 0);
  } catch {
    workTimer.root = root;
    workTimer.cleanMs = 0;
    workTimer.labelMs = 0;
  }
}

function persistWorkTimer() {
  if (!workTimer.root) return;
  try {
    localStorage.setItem(
      workStorageKey(workTimer.root),
      JSON.stringify({
        cleanMs: workTimer.cleanMs,
        labelMs: workTimer.labelMs,
        updatedAt: Date.now(),
      }),
    );
  } catch {
    /* ignore quota */
  }
}

function renderWorkTimer() {
  if (!el.workTimer) return;
  const working = workTimer.active;
  el.workTimer.classList.toggle("working", working);
  el.workTimer.classList.toggle("idle", !working);
  if (el.workTimerStatus) {
    el.workTimerStatus.textContent = working ? "Working" : "Paused (idle)";
  }
  if (el.workCleanTime) el.workCleanTime.textContent = formatWorkHms(workTimer.cleanMs);
  if (el.workLabelTime) el.workLabelTime.textContent = formatWorkHms(workTimer.labelMs);
  if (el.workTotalTime) {
    el.workTotalTime.textContent = formatWorkHms(workTimer.cleanMs + workTimer.labelMs);
  }
}

function workTimerTick() {
  const now = Date.now();
  if (!workTimer.lastTickAt) workTimer.lastTickAt = now;

  if (workTimer.active && now - workTimer.lastActivityAt > WORK_IDLE_MS) {
    workTimer.active = false;
  }

  if (workTimer.active) {
    const delta = now - workTimer.lastTickAt;
    if (state.phase === "label") workTimer.labelMs += delta;
    else workTimer.cleanMs += delta;
  }

  workTimer.lastTickAt = now;
  renderWorkTimer();
}

function noteWorkActivity() {
  const now = Date.now();
  if (!workTimer.root) return;
  const wasActive = workTimer.active;
  workTimer.lastActivityAt = now;
  if (!wasActive) {
    workTimer.active = true;
    workTimer.lastTickAt = now;
    renderWorkTimer();
  }
}

function ensureWorkTimerRunning(root) {
  if (!root) return;
  if (workTimer.root !== root) {
    if (workTimer.root) persistWorkTimer();
    loadWorkTimer(root);
  }
  if (!workTimer.tickHandle) {
    workTimer.lastTickAt = Date.now();
    workTimer.tickHandle = setInterval(workTimerTick, 1000);
  }
  if (!workTimer.saveHandle) {
    workTimer.saveHandle = setInterval(persistWorkTimer, 5000);
  }
  noteWorkActivity();
  renderWorkTimer();
}

function resetWorkTimer({ confirmReset = true } = {}) {
  if (confirmReset && !window.confirm("Reset work timer for this folder?")) return;
  workTimer.cleanMs = 0;
  workTimer.labelMs = 0;
  workTimer.active = false;
  workTimer.lastActivityAt = 0;
  workTimer.lastTickAt = Date.now();
  persistWorkTimer();
  renderWorkTimer();
  setStatus("Work timer reset", "ok");
}

async function saveWorkSession(eventName) {
  const root = workTimer.root || state.scanRoot || state.labelRoot || scanTargetPath();
  if (!root) return;
  persistWorkTimer();
  try {
    await api("/api/eager/work-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        root,
        event: eventName,
        phase: state.phase,
        clean_seconds: Math.floor(workTimer.cleanMs / 1000),
        label_seconds: Math.floor(workTimer.labelMs / 1000),
        files_total: state.videos.length,
        files_done: state.donePaths.size,
      }),
    });
  } catch {
    /* non-blocking */
  }
}

["mousemove", "mousedown", "keydown", "wheel", "touchstart", "scroll"].forEach((name) => {
  document.addEventListener(name, noteWorkActivity, { passive: true });
});
window.addEventListener("blur", () => {
  workTimer.active = false;
  persistWorkTimer();
  renderWorkTimer();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    workTimer.active = false;
    persistWorkTimer();
    renderWorkTimer();
  } else {
    noteWorkActivity();
  }
});

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "00:00:00";
  const whole = Math.max(0, Math.floor(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

function formatDurationShort(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s";
  const whole = Math.ceil(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function setStatus(message, kind = "") {
  el.statusLine.textContent = message || "";
  el.statusLine.className = `status-line ${kind}`.trim();
}

function scanTargetPath() {
  const card = (el.sdCardSelect?.value || "").trim();
  if (card) return card;
  return (el.sourcePath?.value || "").trim();
}

function selectedSdCardLabel() {
  const opt = el.sdCardSelect?.selectedOptions?.[0];
  return opt?.dataset?.label || opt?.textContent?.split(" — ")[0] || "";
}

function filteredVideos() {
  return state.videos;
}

function currentVideo() {
  return state.index >= 0 ? state.videos[state.index] : null;
}

function selectedTask() {
  const picked = el.taskSelect.value.trim();
  if (picked) return picked;
  return el.newTaskInput.value.trim();
}

function visibleTasks() {
  const q = el.taskSearch.value.trim().toLowerCase();
  if (!q) return [...state.tasks];
  return state.tasks.filter((task) => task.toLowerCase().includes(q));
}

function snapshotQuery(video, { priority } = {}) {
  const purpose = state.snapshotPurpose || "clean";
  let q = `path=${encodeURIComponent(video.path)}&purpose=${purpose}`;
  if (priority) q += `&priority=${encodeURIComponent(priority)}`;
  const duration = Number(video?.duration);
  if (Number.isFinite(duration) && duration > 0) {
    q += `&duration=${encodeURIComponent(String(duration))}`;
  }
  return q;
}

function scrubStepSeconds() {
  return state.phase === "label" ? 3 : 1;
}

function setPhase(_phase) {
  // Unified clean+label flow — marking tools stay available; labeling always on.
  state.phase = "clean";
  state.snapshotPurpose = "clean";
  if (el.phaseClean) el.phaseClean.classList.add("active");
  if (el.phaseLabel) el.phaseLabel.classList.remove("active");
  if (el.cleanPanel) el.cleanPanel.classList.remove("hidden");
  if (el.labelPanel) el.labelPanel.classList.add("hidden");
  if (el.listTitle) el.listTitle.textContent = "Footage";
  if (el.scanBtn) el.scanBtn.textContent = "Scan";
  if (el.markSection) el.markSection.classList.remove("hidden");
  if (el.clipList) el.clipList.classList.remove("hidden");
  updateContextHint();
}

function createWorkspace(title) {
  const n = state.workspaces.length + 1;
  return {
    id: `ws-${Date.now()}-${n}`,
    title: title || `Card ${n}`,
    scanRoot: "",
    labelRoot: "",
    videos: [],
    index: -1,
    donePaths: [],
    labeledTasks: {},
    trimmingPaths: [],
    lastLabelTask: "",
    labelProgress: null,
  };
}

function snapshotWorkspace() {
  return {
    scanRoot: state.scanRoot,
    labelRoot: state.labelRoot,
    videos: state.videos,
    index: state.index,
    donePaths: [...state.donePaths],
    labeledTasks: { ...state.labeledTasks },
    trimmingPaths: [...state.trimmingPaths],
    lastLabelTask: state.lastLabelTask,
    labelProgress: state.labelProgress,
  };
}

function applyWorkspace(ws) {
  state.activeWorkspaceId = ws.id;
  state.scanRoot = ws.scanRoot || "";
  state.labelRoot = ws.labelRoot || ws.scanRoot || "";
  state.videos = ws.videos || [];
  state.index = Number.isFinite(ws.index) ? ws.index : -1;
  state.donePaths = new Set(ws.donePaths || []);
  state.labeledTasks = { ...(ws.labeledTasks || {}) };
  state.trimmingPaths = new Set(ws.trimmingPaths || []);
  state.lastLabelTask = ws.lastLabelTask || "";
  state.labelProgress = ws.labelProgress || null;
  state.pendingIn = null;
  state.pendingClip = null;
  state.savedClips = [];
  state.snapshots = null;
  el.sourcePath.value = state.scanRoot || "";
  if (state.scanRoot && el.sdCardSelect) {
    const opt = [...el.sdCardSelect.options].find((o) => o.value === state.scanRoot);
    if (opt) el.sdCardSelect.value = state.scanRoot;
  }
  renderCardTabs();
  renderFileList();
  renderTasks(state.lastLabelTask || undefined);
  if (state.index >= 0 && state.index < state.videos.length) {
    loadVideo(state.index);
  } else {
    el.currentName.textContent = "No file loaded";
    el.currentMeta.textContent = "";
    el.player.removeAttribute("src");
    updateContextHint();
  }
}

function saveActiveWorkspace() {
  const ws = state.workspaces.find((w) => w.id === state.activeWorkspaceId);
  if (!ws) return;
  Object.assign(ws, snapshotWorkspace());
  if (state.scanRoot) {
    const label = selectedSdCardLabel() || state.scanRoot.split(/[/\\]/).filter(Boolean).pop();
    if (label) ws.title = label;
  }
}

function renderCardTabs() {
  if (!el.cardTabList) return;
  el.cardTabList.innerHTML = "";
  for (const ws of state.workspaces) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-tab" + (ws.id === state.activeWorkspaceId ? " active" : "");
    btn.textContent = ws.title;
    btn.title = ws.scanRoot || "No folder scanned yet";
    btn.addEventListener("click", () => switchWorkspace(ws.id));
    el.cardTabList.appendChild(btn);
  }
}

function switchWorkspace(id) {
  if (id === state.activeWorkspaceId) return;
  saveActiveWorkspace();
  const ws = state.workspaces.find((w) => w.id === id);
  if (!ws) return;
  applyWorkspace(ws);
  setStatus(`Switched to ${ws.title}`, "ok");
}

function addWorkspaceTab() {
  saveActiveWorkspace();
  const ws = createWorkspace();
  state.workspaces.push(ws);
  applyWorkspace(ws);
  setStatus("New card tab — pick an SD card and Scan", "ok");
}

function ensureWorkspaces() {
  if (state.workspaces.length) return;
  const ws = createWorkspace("Card 1");
  state.workspaces.push(ws);
  state.activeWorkspaceId = ws.id;
}

function remainingUnlabeledCount() {
  if (state.phase !== "label") return 0;
  if (state.labelProgress && Number.isFinite(state.labelProgress.unlabeled)) {
    return Math.max(0, state.labelProgress.unlabeled);
  }
  return state.videos.filter((video) => !state.donePaths.has(video.path)).length;
}

function renderLabelProgress() {
  if (!el.labelProgress) return;
  if (state.phase !== "label") {
    el.labelProgress.className = "label-progress idle";
    return;
  }

  const progress = state.labelProgress;
  const remaining = remainingUnlabeledCount();
  const labeled = progress?.labeled ?? 0;

  if (!progress && !state.videos.length) {
    el.labelProgress.className = "label-progress idle";
    if (el.labelProgressCount) el.labelProgressCount.textContent = "—";
    if (el.labelProgressLabel) el.labelProgressLabel.textContent = "Scan to check unlabeled footage";
    if (el.labelProgressDetail) el.labelProgressDetail.textContent = "";
    return;
  }

  if (remaining === 0) {
    el.labelProgress.className = "label-progress ok";
    if (el.labelProgressCount) el.labelProgressCount.textContent = "0";
    if (el.labelProgressLabel) el.labelProgressLabel.textContent = "All footage labeled";
    if (el.labelProgressDetail) {
      el.labelProgressDetail.textContent =
        labeled > 0 ? `${labeled} file(s) inside task folders` : "Nothing left outside task folders";
    }
    return;
  }

  el.labelProgress.className = "label-progress warn";
  if (el.labelProgressCount) el.labelProgressCount.textContent = String(remaining);
  if (el.labelProgressLabel) {
    el.labelProgressLabel.textContent =
      remaining === 1 ? "unlabeled file left" : "unlabeled files left";
  }
  if (el.labelProgressDetail) {
    el.labelProgressDetail.textContent =
      labeled > 0
        ? `${labeled} already in task folders · S search · Enter/N move`
        : "Still outside task folders — S to search, Enter to move";
  }
}

function renderFileList() {
  const items = filteredVideos();
  el.fileList.innerHTML = "";
  const doneCount = state.donePaths.size;
  const trimCount = state.trimmingPaths.size;
  el.listSummary.textContent = `${state.videos.length} left · ${doneCount} done · ${trimCount} trimming`;

  for (const video of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "file-item";
    if (state.videos[state.index]?.path === video.path) btn.classList.add("active");
    const assignedTask = state.labeledTasks[video.path];
    if (state.donePaths.has(video.path)) btn.classList.add("done");
    else if (state.trimmingPaths.has(video.path)) btn.classList.add("trimming");
    else btn.classList.add("unlabeled");
    const typeHint = `${video.is_trimmed ? "clip" : "whole"} · `;
    const taskBadge = assignedTask
      ? `<span class="task-badge" title="Moved to ${assignedTask}">${assignedTask}</span>`
      : "";
    btn.innerHTML = `<span class="name">${video.name}${taskBadge}</span><span class="meta">${typeHint}${video.duration_label || "?"} · ${formatBytes(video.size_bytes)}</span>`;
    btn.addEventListener("click", () => {
      const idx = state.videos.findIndex((item) => item.path === video.path);
      if (idx >= 0) loadVideo(idx);
    });
    el.fileList.appendChild(btn);
  }
}

function renderClips() {
  el.clipList.innerHTML = "";
  for (const job of state.savedClips) {
    const item = document.createElement("li");
    let cls = "saved";
    let label = "";
    const range = `${formatTime(job.start)} → ${formatTime(job.end)}`;
    if (job.status === "queued") {
      cls = "queued";
      label = `Queued: ${range}`;
    } else if (job.status === "running") {
      cls = "running";
      const pct = Number.isFinite(job.progress) ? ` · ${Math.round(job.progress)}%` : "";
      const left =
        job.remaining_seconds > 0 ? ` · ~${formatDurationShort(job.remaining_seconds)} left` : "";
      label = `Trimming: ${range}${pct}${left}`;
    } else if (job.status === "failed") {
      cls = "failed";
      label = `Failed: ${job.error || "trim error"}`;
    } else {
      const imu =
        job.output_has_gpmf === true
          ? " · IMU ✓"
          : job.source_has_gpmf === true && job.output_has_gpmf === false
            ? " · IMU missing"
            : "";
      label = `Saved: ${job.name || job.output?.split(/[/\\]/).pop() || "clip"}${imu}`;
    }
    item.className = cls;
    if (job.status === "running" && job.progress > 0) {
      const bar = document.createElement("div");
      bar.className = "clip-progress";
      bar.innerHTML = `<div class="clip-progress-fill" style="width:${Math.min(100, job.progress)}%"></div>`;
      item.appendChild(document.createTextNode(label));
      item.appendChild(bar);
    } else {
      item.textContent = label;
    }
    el.clipList.appendChild(item);
  }
  if (state.pendingClip) {
    const item = document.createElement("li");
    item.className = "pending";
    item.textContent = `Marked: ${formatTime(state.pendingClip.start)} → ${formatTime(state.pendingClip.end)} — press T to queue`;
    el.clipList.appendChild(item);
  }
  const activeTrims = state.savedClips.filter((j) => j.status === "queued" || j.status === "running").length;
  if (state.pendingIn !== null) {
    el.pendingIn.textContent = `Start locked at ${formatTime(state.pendingIn)} — step forward, then Mark end`;
    el.pendingIn.className = "pending-status active";
  } else if (state.pendingClip) {
    el.pendingIn.textContent = "Press T to queue trim — you can mark the next clip immediately after";
    el.pendingIn.className = "pending-status warn";
  } else if (activeTrims > 0) {
    const eta = state.trimEtaTotal > 0 ? ` · ~${formatDurationShort(state.trimEtaTotal)} left` : "";
    el.pendingIn.textContent = `${activeTrims} trim(s) in background${eta} — keep marking or press N when done`;
    el.pendingIn.className = "pending-status active";
  } else if (state.savedClips.some((j) => j.status === "completed" || j.output)) {
    const done = state.savedClips.filter((j) => j.status === "completed" || j.output).length;
    el.pendingIn.textContent = `${done} clip(s) saved — mark more or press N for next file`;
    el.pendingIn.className = "pending-status active";
  } else {
    el.pendingIn.textContent = "At useful footage? Press I or Mark start";
    el.pendingIn.className = "pending-status";
  }
  updateContextHint();
}

function basenamePath(path) {
  if (!path) return "";
  return String(path).split(/[/\\]/).pop() || "";
}

function applyGlobalTrimUi(data) {
  const active = Number(data?.active || 0);
  const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
  state.globalTrimActive = active;
  state.globalTrimJobs = jobs;
  state.trimEtaTotal = Number(data?.eta_total_seconds || 0);

  if (el.labelTrimBanner) {
    el.labelTrimBanner.classList.toggle("hidden", active === 0);
    if (el.labelTrimBannerText && active > 0) {
      el.labelTrimBannerText.textContent =
        `${active} clip(s) still trimming · ~${formatDurationShort(state.trimEtaTotal)} left. ` +
        "Use + to open another card while you wait.";
    }
  }

  if (!el.trimProgressPanel) return;

  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running");
  if (active === 0 || activeJobs.length === 0) {
    el.trimProgressPanel.classList.add("hidden");
    if (el.trimProgressFill) el.trimProgressFill.style.width = "0%";
    if (el.trimProgressList) el.trimProgressList.innerHTML = "";
    return;
  }

  el.trimProgressPanel.classList.remove("hidden");
  if (el.trimActiveCount) el.trimActiveCount.textContent = String(active);
  if (el.trimEtaTotal) {
    el.trimEtaTotal.textContent = `~${formatDurationShort(state.trimEtaTotal)}`;
  }

  const runningJobs = activeJobs.filter((j) => j.status === "running");
  let overallPct = 0;
  if (runningJobs.length) {
    // Bar tracks active ffmpeg work (queued jobs made the old bar look stuck at ~0).
    overallPct =
      runningJobs.reduce((sum, j) => sum + (j.progress || 0), 0) / runningJobs.length;
  }
  if (el.trimProgressFill) {
    el.trimProgressFill.style.width = `${Math.min(100, overallPct)}%`;
  }

  if (el.trimProgressList) {
    el.trimProgressList.innerHTML = "";
    for (const job of activeJobs.slice(0, 12)) {
      const row = document.createElement("div");
      const outName = basenamePath(job.output);
      const sourceName = job.source_name || basenamePath(job.source_path) || "clip";
      if (job.status === "queued") {
        row.textContent = `Queued · ${sourceName} (${formatTime(job.start_seconds)} → ${formatTime(job.end_seconds)})`;
      } else {
        const pct = Math.round(job.progress || 0);
        row.textContent = `Trimming ${pct}% · ${outName || sourceName}`;
      }
      el.trimProgressList.appendChild(row);
    }
  }
}

function scheduleLabelListRefresh() {
  if (state.labelRefreshTimer) clearTimeout(state.labelRefreshTimer);
  state.labelRefreshTimer = setTimeout(() => {
    state.labelRefreshTimer = null;
    softRefreshLabelScan();
  }, 900);
}

async function softRefreshLabelScan() {
  if (state.busy) return;
  const path = state.labelRoot || state.scanRoot || scanTargetPath();
  if (!path) return;
  const currentPath = currentVideo()?.path || "";
  const token = ++state.labelScanToken;
  try {
    const data = await api("/api/eager/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive: true, mode: "label" }),
    });
    if (token !== state.labelScanToken) return;
    if (state.busy) return;
    const fresh = (data.videos || []).filter((video) => !state.donePaths.has(video.path));
    // Drop trimming markers for sources that are no longer busy / gone from disk.
    const freshPaths = new Set(fresh.map((v) => v.path));
    for (const p of [...state.trimmingPaths]) {
      if (!freshPaths.has(p)) state.trimmingPaths.delete(p);
    }
    state.videos = fresh;
    state.labelProgress = data.progress || state.labelProgress;
    el.scanSummary.textContent = `${fresh.length} files`;
    saveActiveWorkspace();
    const idx = currentPath ? state.videos.findIndex((v) => v.path === currentPath) : -1;
    if (idx >= 0) {
      state.index = idx;
      renderFileList();
      updateContextHint();
    } else if (state.videos.length) {
      const fallback = Math.min(Math.max(0, state.index), state.videos.length - 1);
      await loadVideo(fallback);
    } else {
      state.index = -1;
      el.currentName.textContent = "No file loaded";
      el.currentMeta.textContent = "";
      renderFileList();
      updateContextHint();
    }
  } catch {
    /* ignore background refresh */
  }
}

function syncTrimJobsFromServer(jobs) {
  const byId = new Map(state.savedClips.map((j) => [j.job_id, j]));
  for (const job of jobs || []) {
    const existing = byId.get(job.job_id);
    if (existing) {
      Object.assign(existing, job);
      if (job.output) existing.name = basenamePath(job.output);
      existing.start = job.start_seconds;
      existing.end = job.end_seconds;
    } else {
      state.savedClips.push({
        job_id: job.job_id,
        status: job.status,
        start: job.start_seconds,
        end: job.end_seconds,
        duration_seconds: job.duration_seconds,
        progress: job.progress,
        remaining_seconds: job.remaining_seconds,
        output: job.output,
        name: job.output ? basenamePath(job.output) : null,
        error: job.error,
        source_has_gpmf: job.source_has_gpmf,
        output_has_gpmf: job.output_has_gpmf,
      });
    }
  }
  state.savedClips.sort((a, b) => (a.start || 0) - (b.start || 0));
  renderClips();
}

async function pollTrimStatus() {
  const video = currentVideo();
  if (!video || state.phase !== "clean") return;
  try {
    const data = await api(`/api/eager/trim/status?path=${encodeURIComponent(video.path)}`);
    syncTrimJobsFromServer(data.jobs);
  } catch {
    /* ignore */
  }
}

async function pollGlobalTrims() {
  try {
    const data = await api("/api/eager/trim/active");
    const prevActive = state.globalTrimActive;
    applyGlobalTrimUi(data);

    const video = currentVideo();
    if (video && state.phase === "clean") {
      const forSource = (data.jobs || []).filter((j) => j.source_path === video.path);
      if (forSource.length) syncTrimJobsFromServer(forSource);
    }

    if (prevActive !== (data.active || 0)) {
      updateContextHint();
    }

    if (prevActive > 0 && (data.active || 0) < prevActive) {
      scheduleLabelListRefresh();
    }
  } catch {
    /* ignore */
  }
}

async function loadMediaProbe(path) {
  if (!el.gpmfStatus) return;
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(path)}`);
    state.currentHasGpmf = info.has_gpmf;
    el.gpmfStatus.classList.remove("hidden");
    if (info.has_gpmf) {
      el.gpmfStatus.textContent = "IMU / GPMF detected — copied with clip timestamps on trim";
      el.gpmfStatus.className = "gpmf-badge ok";
    } else {
      el.gpmfStatus.textContent = "No IMU track on this file";
      el.gpmfStatus.className = "gpmf-badge warn";
    }
  } catch {
    state.currentHasGpmf = null;
    el.gpmfStatus.classList.add("hidden");
  }
}

function startTrimPolling() {
  stopTrimPolling();
  pollTrimStatus();
  state.trimPollTimer = setInterval(pollTrimStatus, state.perf.trim_poll_ms || 1200);
}

function stopTrimPolling() {
  if (state.trimPollTimer) {
    clearInterval(state.trimPollTimer);
    state.trimPollTimer = null;
  }
}

function startGlobalTrimPolling() {
  if (state.globalTrimPollTimer) return;
  pollGlobalTrims();
  state.globalTrimPollTimer = setInterval(
    pollGlobalTrims,
    Math.max(1500, state.perf.trim_poll_ms || 1200),
  );
}

function activeTrimCount() {
  return state.savedClips.filter((j) => j.status === "queued" || j.status === "running").length;
}

function updateContextHint() {
  if (!el.contextMessage) return;

  if (state.globalTrimActive > 0) {
    const eta = state.trimEtaTotal > 0 ? ` · ~${formatDurationShort(state.trimEtaTotal)} left` : "";
    el.contextMessage.textContent =
      `${state.globalTrimActive} trim(s) running${eta} — finished clips show up for labeling · + for another card`;
    if (!currentVideo()) return;
  }

  if (!currentVideo()) {
    el.contextMessage.textContent = scanTargetPath()
      ? "Press Scan to load files — clean files: Enter to label · garbage: I→O→T then N"
      : "Plug in a C#### SD card, then Scan · use + for a second card";
    return;
  }

  if (state.trimmingPaths.has(currentVideo().path)) {
    el.contextMessage.textContent = "This file is still trimming — pick another, or wait for clips to appear";
    return;
  }

  if (state.pendingClip) {
    el.contextMessage.textContent = "Press T to queue trim, then mark the next useful section";
    return;
  }

  if (activeTrimCount() > 0) {
    const eta = state.trimEtaTotal > 0 ? ` (~${formatDurationShort(state.trimEtaTotal)} left)` : "";
    el.contextMessage.textContent = `Trims queued for this file${eta} — press N when done marking`;
    return;
  }

  if (state.pendingIn !== null) {
    el.contextMessage.textContent = "Find the end of useful footage, then press O";
    return;
  }

  if (state.savedClips.some((j) => j.status === "completed" || j.output || j.status === "queued" || j.status === "running")) {
    el.contextMessage.textContent = "More parts? Keep marking. Ready? Press N — then label the new clips when they appear";
    return;
  }

  if (currentVideo().is_trimmed) {
    el.contextMessage.textContent = "Trimmed clip — type a task and press Enter to label";
    return;
  }

  el.contextMessage.textContent =
    "Clean? Type task + Enter. Garbage? I → O → T for each part, then N";
}

function showLoading(title, detail, pct = 0, hint = "") {
  if (!el.loadingOverlay) return;
  el.loadingOverlay.classList.remove("hidden");
  el.loadingTitle.textContent = title;
  el.loadingDetail.textContent = detail || "";
  el.loadingBarFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  el.loadingHint.textContent = hint || "";
}

function hideLoading() {
  el.loadingOverlay?.classList.add("hidden");
}

function updateFilmstripMeta() {
  if (!el.filmstripMeta || !state.snapshots) return;
  const m = state.snapshots;
  const idx = state.snapshotIndex + 1;
  const total = m.frames?.length || 0;
  if (state.snapshotPurpose === "label") {
    el.filmstripMeta.textContent = `Opening preview ${idx}/${total} · use , . ±3s to scrub`;
    return;
  }
  const parts = [];
  if (m.duration) parts.push(`Clip ${formatDurationShort(m.duration)}`);
  parts.push(`snapshot every ${formatDurationShort(m.interval_seconds)} (${idx}/${total})`);
  if (m.max_garbage_seconds > 0) {
    parts.push(`garbage allowed ~${formatDurationShort(m.max_garbage_seconds)}`);
  }
  el.filmstripMeta.textContent = parts.join(" · ");
}

function attachSnapshotImage(img, video, frameIndex) {
  let attempt = 0;
  const maxAttempts = 4;
  const load = () => {
    const retry = attempt > 0 ? `&retry=${attempt}&t=${Date.now()}` : "";
    img.src = `/api/eager/snapshots/frame?${snapshotQuery(video)}&index=${frameIndex}${retry}`;
  };
  img.onerror = () => {
    attempt += 1;
    if (attempt < maxAttempts) {
      setTimeout(load, 250 * attempt);
    }
  };
  load();
}

function renderFilmstrip() {
  if (!el.filmstrip) return;
  el.filmstrip.innerHTML = "";
  if (!state.snapshots?.frames?.length) {
    el.filmstrip.innerHTML = '<div class="hint">No snapshots yet</div>';
    return;
  }
  const video = currentVideo();
  if (!video) return;

  state.snapshots.frames.forEach((frame) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "filmstrip-item";
    if (frame.index === state.snapshotIndex) btn.classList.add("active");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = formatTime(frame.t);
    attachSnapshotImage(img, video, frame.index);
    const label = document.createElement("span");
    label.textContent = formatTime(frame.t);
    btn.appendChild(img);
    btn.appendChild(label);
    btn.addEventListener("click", () => goToSnapshotIndex(frame.index));
    el.filmstrip.appendChild(btn);
  });
  updateFilmstripMeta();
  const active = el.filmstrip.querySelector(".filmstrip-item.active");
  active?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
}

async function cancelSnapshotJob(path) {
  if (!path) return;
  try {
    await api("/api/eager/snapshots/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } catch {
    /* ignore */
  }
}

async function waitForSnapshots(video, token, { showOverlay = true } = {}) {
  const isLabel = state.snapshotPurpose === "label";
  await api(`/api/eager/snapshots/status?${snapshotQuery(video, { priority: "foreground" })}&start=1`);
  let uiOpen = !showOverlay || isLabel;
  if (!showOverlay && !isLabel) {
    hideLoading();
  }
  const maxPolls = isLabel ? 120 : 3600;
  for (let i = 0; i < maxPolls; i += 1) {
    if (token !== state.snapshotBuildToken) return null;
    const status = await api(`/api/eager/snapshots/status?${snapshotQuery(video)}`);
    const partial = status.manifest;
    const frameCount = partial?.frames?.length || 0;
    const minFrames = isLabel ? 1 : state.perf.lite_mode ? 2 : 3;

    if (partial?.frames?.length) {
      state.snapshots = partial;
      renderFilmstrip();
      if (!uiOpen && (status.status === "ready" || frameCount >= minFrames)) {
        uiOpen = true;
        hideLoading();
        if (!isLabel && frameCount >= 3 && status.status === "running") {
          setStatus(`Review started — loading remaining snapshots (${frameCount}/${partial.snapshot_count || "?"})`, "ok");
        }
      }
    }

    if (showOverlay && status.status === "running" && !uiOpen && !isLabel) {
      showLoading(
        "Building snapshots",
        `${video.name} — ${frameCount}/${status.plan?.snapshot_count || status.manifest?.snapshot_count || "?"} frames`,
        status.progress || Math.min(95, Math.round((frameCount / (status.plan?.snapshot_count || status.manifest?.snapshot_count || 1)) * 100)),
        status.plan?.garbage_hint || status.manifest?.garbage_hint || "",
      );
    }
    if (showOverlay && status.status === "queued" && !uiOpen && !isLabel) {
      if (i >= 4) {
        hideLoading();
        uiOpen = true;
        setStatus(
          `Snapshots loading in background — use the scrub bar below the video (${video.name})`,
          "ok",
        );
      } else {
        const pos = status.queue_position;
        const waitHint =
          typeof pos === "number" && pos > 0
            ? `Position ${pos + 1} in queue`
            : "Starting soon…";
        showLoading("Queued for snapshots", video.name, 5, waitHint);
      }
    }

    if (status.status === "ready" && status.manifest) {
      hideLoading();
      return status.manifest;
    }
    if (uiOpen && partial?.frames?.length >= minFrames && (status.status === "running" || status.status === "ready")) {
      return partial;
    }
    if (status.status === "error") {
      hideLoading();
      throw new Error(status.error || "Snapshot build failed");
    }
    await new Promise((r) => setTimeout(r, uiOpen ? state.perf.snapshot_poll_ms : Math.min(400, state.perf.snapshot_poll_ms)));
  }
  hideLoading();
  throw new Error("Snapshot build timed out");
}

async function ensureSnapshots(video, showOverlay = true) {
  const isLabel = state.snapshotPurpose === "label";
  const token = ++state.snapshotBuildToken;
  if (showOverlay && !isLabel) {
    showLoading("Checking snapshots", video.name, 5);
  } else if (isLabel) {
    setStatus(`Loading opening preview for ${video.name}...`);
  } else {
    hideLoading();
  }
  const status = await api(`/api/eager/snapshots/status?${snapshotQuery(video)}`);
  if (status.status === "ready" && status.manifest) {
    if (token !== state.snapshotBuildToken) return null;
    state.snapshots = status.manifest;
    state.snapshotIndex = 0;
    renderFilmstrip();
    hideLoading();
    return status.manifest;
  }
  if (showOverlay && !isLabel) {
    showLoading("Building snapshots", video.name, 0, status.plan?.garbage_hint || "");
  }
  const manifest = await waitForSnapshots(video, token, { showOverlay });
  if (token !== state.snapshotBuildToken || !manifest) return null;
  state.snapshots = manifest;
  renderFilmstrip();
  hideLoading();
  return manifest;
}

function continueSnapshotRefresh(video) {
  if (state.snapshotPurpose === "label") return;
  const token = state.snapshotBuildToken;
  const pollMs = state.perf.snapshot_poll_ms || 1000;
  (async () => {
    for (let i = 0; i < 180; i += 1) {
      if (token !== state.snapshotBuildToken) return;
      if (currentVideo()?.path !== video.path) return;
      const status = await api(`/api/eager/snapshots/status?${snapshotQuery(video)}`);
      if (status.manifest?.frames?.length) {
        const prev = state.snapshots?.frames?.length || 0;
        state.snapshots = status.manifest;
        if (status.manifest.frames.length !== prev) renderFilmstrip();
      }
      if (status.status === "ready") {
        state.snapshots = status.manifest;
        renderFilmstrip();
        updateFilmstripMeta();
        // Only prefetch the next clip once this one is fully done — otherwise the
        // next file sits behind leftover frames and N shows "Starting soon…".
        if (state.phase === "clean" && state.perf.prefetch) {
          prefetchSnapshotsBackground(state.index + 1);
        }
        return;
      }
      if (status.status === "error") return;
      await new Promise((r) => setTimeout(r, pollMs));
    }
  })();
}

function goToSnapshotIndex(index) {
  if (!state.snapshots?.frames?.length) return;
  const frames = state.snapshots.frames;
  const clamped = Math.max(0, Math.min(frames.length - 1, index));
  state.snapshotIndex = clamped;
  const t = frames[clamped].t;
  scheduleSeek(t, true);
  renderFilmstrip();
  updateFilmstripMeta();
}

function goToSnapshot(delta) {
  goToSnapshotIndex(state.snapshotIndex + delta);
}

function fineTune(seconds) {
  if (!currentVideo()) return;
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;
  scheduleSeek(state.scrubTime + seconds, true);
}

function flushSeek() {
  if (state.seekTimer) {
    clearTimeout(state.seekTimer);
    state.seekTimer = null;
  }
  if (state.pendingSeek !== null) {
    state.scrubTime = state.pendingSeek;
    el.player.pause();
    try {
      el.player.currentTime = state.pendingSeek;
    } catch {
      /* large files may not support seek — scrub time still updates */
    }
    state.pendingSeek = null;
    updateScrubUi();
  }
}

function currentScrubTime() {
  flushSeek();
  return state.scrubTime;
}

function renderTasks(preferred = "") {
  const matches = visibleTasks();
  const current = el.taskSelect.value;
  const preferredTask = preferred || state.lastLabelTask || "";
  const selected =
    preferredTask && matches.includes(preferredTask)
      ? preferredTask
      : matches.includes(current)
        ? current
        : matches[0] || "";
  el.taskList.innerHTML = "";
  el.taskSelect.innerHTML = "";

  if (!state.tasks.length) {
    el.taskList.innerHTML = '<div class="hint">No tasks yet — add one below.</div>';
    if (el.taskSelectedHint) el.taskSelectedHint.textContent = "";
    return;
  }

  if (!matches.length) {
    el.taskList.innerHTML = '<div class="hint">No matching tasks — keep typing or clear search.</div>';
    if (el.taskSelectedHint) {
      el.taskSelectedHint.textContent = state.lastLabelTask
        ? `Last used: ${state.lastLabelTask} — clear search or press Esc, then N`
        : "";
    }
    updateContextHint();
    return;
  }

  for (const task of matches) {
    const option = document.createElement("option");
    option.value = task;
    option.textContent = task;
    el.taskSelect.appendChild(option);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "task-item";
    btn.textContent = task;
    if (task === selected) btn.classList.add("active");
    btn.addEventListener("click", () => {
      el.taskSelect.value = task;
      state.lastLabelTask = task;
      renderTasks(task);
    });
    el.taskList.appendChild(btn);
  }

  if (selected && [...el.taskSelect.options].some((opt) => opt.value === selected)) {
    el.taskSelect.value = selected;
  } else if (el.taskSelect.options.length) {
    el.taskSelect.selectedIndex = 0;
  }
  if (el.taskSelectedHint) {
    const active = selectedTask();
    el.taskSelectedHint.textContent = active
      ? `Selected: ${active} — Enter / N moves current file`
      : "Press S to search, arrows to choose, Enter to move";
  }
  updateContextHint();
}

function moveTaskSelection(delta) {
  const options = [...el.taskSelect.options].map((opt) => opt.value);
  if (!options.length) return false;
  const current = el.taskSelect.value;
  const idx = Math.max(0, options.indexOf(current));
  const next = Math.max(0, Math.min(options.length - 1, idx + delta));
  const chosen = options[next];
  el.taskSelect.value = chosen;
  state.lastLabelTask = chosen;
  renderTasks(chosen);
  const active = el.taskList.querySelector(".task-item.active");
  active?.scrollIntoView({ block: "nearest" });
  return true;
}

function focusTaskSearch() {
  if (!el.taskSearch || state.phase !== "label") return;
  el.taskSearch.focus();
  el.taskSearch.select();
  setStatus("Search a task — ↑↓ to choose, Enter to move", "ok");
}

function leaveTaskSearch({ clear = false } = {}) {
  if (clear) el.taskSearch.value = "";
  const keep = selectedTask() || state.lastLabelTask;
  renderTasks(keep);
  el.taskSearch.blur();
  el.playerWrap?.focus?.();
}

function updateScrubUi() {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  const current = state.scrubTime;
  const pct = duration > 0 ? (current / duration) * 100 : 0;
  el.scrubFill.style.width = `${pct}%`;
  el.scrubPlayhead.style.left = `${pct}%`;
  el.timeDisplay.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
}

function scheduleSeek(time, immediate = false) {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;
  const clamped = Math.min(duration - 0.04, Math.max(0, time));
  state.scrubTime = clamped;
  state.pendingSeek = clamped;
  updateScrubUi();

  if (immediate) {
    flushSeek();
    return;
  }

  if (state.seekTimer) return;
  state.seekTimer = setTimeout(() => {
    state.seekTimer = null;
    flushSeek();
  }, 120);
}

function seekToFraction(fraction) {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;
  scheduleSeek(fraction * duration, true);
}

function markStart() {
  if (!currentVideo()) return;
  state.pendingIn = currentScrubTime();
  state.pendingClip = null;
  setStatus(`Start marked at ${formatTime(state.pendingIn)}`, "ok");
  renderClips();
}

function markEnd() {
  if (!currentVideo()) return;
  if (state.pendingIn === null) {
    setStatus("Mark start first", "error");
    return;
  }
  const end = currentScrubTime();
  if (end <= state.pendingIn + 0.05) {
    setStatus("End must be after start — step forward first", "error");
    return;
  }
  state.pendingClip = { start: state.pendingIn, end };
  state.pendingIn = null;
  setStatus(`Marked ${formatTime(state.pendingClip.start)} → ${formatTime(end)}`, "ok");
  renderClips();
}

function undoMark() {
  if (state.pendingClip) {
    state.pendingClip = null;
  } else if (state.pendingIn !== null) {
    state.pendingIn = null;
  } else {
    state.savedClips.pop();
  }
  renderClips();
}

async function cancelPreviewJob(path) {
  if (!path) return;
  try {
    await fetch("/api/eager/preview/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } catch {
    /* ignore */
  }
}

async function loadVideo(index) {
  if (index < 0 || index >= state.videos.length) return;

  const previous = currentVideo();
  if (previous?.path && previous.path !== state.videos[index]?.path) {
    await cancelPreviewJob(previous.path);
    // Free the snapshot worker so the next clip isn't stuck behind leftover frames.
    await cancelSnapshotJob(previous.path);
  }

  state.index = index;
  state.pendingIn = null;
  state.pendingClip = null;
  state.savedClips = [];
  stopTrimPolling();
  state.pendingSeek = null;
  state.snapshots = null;
  state.snapshotIndex = 0;
  state.scrubTime = 0;
  if (state.seekTimer) {
    clearTimeout(state.seekTimer);
    state.seekTimer = null;
  }

  const video = state.videos[index];
  const token = ++state.previewToken;
  state.lastVideoPath = video.path;

  el.currentName.textContent = video.name;
  el.currentMeta.textContent = `${video.relative || video.path} · ${video.duration_label || "?"}`;
  el.previewStatus.textContent = "";
  el.playerWrap.classList.add("loading");
  setStatus(`Loading ${video.name}...`);
  if (state.phase === "clean") {
    loadMediaProbe(video.path);
  } else if (el.gpmfStatus) {
    el.gpmfStatus.classList.add("hidden");
  }

  renderFileList();
  renderClips();

  el.player.src = `/api/eager/stream?path=${encodeURIComponent(video.path)}`;
  el.player.load();

  const onReady = async () => {
    if (token !== state.previewToken) return;
    el.playerWrap.classList.remove("loading");
    el.player.pause();
    state.scrubTime = 0;
    try {
      el.player.currentTime = 0;
    } catch {
      /* ignore */
    }
    updateScrubUi();
    if (state.phase === "clean" || state.phase === "label") {
      hideLoading();
      if (state.phase === "clean") {
        startTrimPolling();
      }
      setStatus(`Ready — ${video.name} (loading snapshots…)`, "ok");
      updateContextHint();
      ensureSnapshots(video, false)
        .then((manifest) => {
          if (token !== state.previewToken || !manifest) return;
          if (manifest.frames?.length) {
            goToSnapshotIndex(0);
          }
          continueSnapshotRefresh(video);
          const readyMsg =
            state.phase === "label"
              ? `Ready — , . ±3s to scrub (${video.name})`
              : `Ready — use snapshot strip (${video.name})`;
          setStatus(readyMsg, "ok");
        })
        .catch((error) => {
          if (token !== state.previewToken) return;
          hideLoading();
          setStatus(error.message || "Snapshot build failed — use scrub bar", "error");
        });
    } else {
      setStatus(`Ready — ${video.name}`, "ok");
    }
  };

  el.player.addEventListener("loadedmetadata", onReady, { once: true });
  el.player.addEventListener(
    "error",
    () => {
      if (token !== state.previewToken) return;
      el.playerWrap.classList.remove("loading");
      setStatus("Could not load video", "error");
    },
    { once: true },
  );
}

async function chooseFootageFolder() {
  el.browseFolderBtn.disabled = true;
  setStatus("Choose a folder in the dialog…");
  try {
    const initial = el.sourcePath.value.trim() || el.sdCardSelect?.value || "";
    const query = initial ? `?initial=${encodeURIComponent(initial)}` : "";
    const data = await api(`/api/eager/pick-folder${query}`, { method: "POST" });
    if (data.cancelled) {
      setStatus("Folder selection cancelled");
      return;
    }
    applySelectedPath(data.path, { label: "Manual folder", manual: true });
    setStatus(`Selected ${data.path}`, "ok");
    updateContextHint();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.browseFolderBtn.disabled = false;
  }
}

function applySelectedPath(scanPath, { label = "", manual = false } = {}) {
  el.sourcePath.value = scanPath;
  state.scanRoot = scanPath;
  state.labelRoot = scanPath;
  ensureWorkTimerRunning(scanPath);
  const ws = state.workspaces.find((w) => w.id === state.activeWorkspaceId);
  if (ws) {
    ws.scanRoot = scanPath;
    ws.labelRoot = scanPath;
    ws.title = label || selectedSdCardLabel() || ws.title;
    renderCardTabs();
  }

  if (!el.sdCardSelect) return;

  const existing = [...el.sdCardSelect.options].find((opt) => opt.value === scanPath);
  if (existing) {
    el.sdCardSelect.value = scanPath;
    return;
  }

  if (manual) {
    // Keep detected cards, add/replace a manual option at the top.
    let manualOpt = [...el.sdCardSelect.options].find((opt) => opt.dataset.manual === "1");
    if (!manualOpt) {
      manualOpt = document.createElement("option");
      manualOpt.dataset.manual = "1";
      el.sdCardSelect.insertBefore(manualOpt, el.sdCardSelect.firstChild);
    }
    manualOpt.value = scanPath;
    manualOpt.dataset.label = label || "Manual";
    manualOpt.textContent = `${label || "Manual"} — ${scanPath}`;
    el.sdCardSelect.value = scanPath;
  }
}

async function refreshSdCards({ quiet = false } = {}) {
  if (!el.sdCardSelect) return;
  const previous = el.sdCardSelect.value;
  el.refreshSdBtn.disabled = true;
  if (!quiet) setStatus("Detecting SD cards…");
  try {
    const data = await api("/api/eager/sd-cards");
    const cards = data.cards || [];
    el.sdCardSelect.innerHTML = "";

    if (!cards.length) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No C#### SD cards found";
      el.sdCardSelect.appendChild(empty);
      el.sourcePath.value = "";
      if (el.sdCardHint) {
        el.sdCardHint.textContent =
          "No card detected. Plug in a card named C1234, then press Refresh — or use Browse…";
      }
      if (!quiet) setStatus("No SD cards detected", "error");
      updateContextHint();
      return;
    }

    for (const card of cards) {
      const option = document.createElement("option");
      option.value = card.scan_path || card.path;
      option.dataset.label = card.id || card.label || "";
      option.dataset.volume = card.path || "";
      const goproNote =
        card.gopro_root && card.scan_path === card.gopro_root ? " · DCIM/GoPro" : "";
      option.textContent = `${card.id || card.label}${goproNote}`;
      el.sdCardSelect.appendChild(option);
    }

    let chosen = "";
    if (previous && [...el.sdCardSelect.options].some((opt) => opt.value === previous)) {
      chosen = previous;
    } else if (cards.length === 1) {
      chosen = cards[0].scan_path || cards[0].path;
    }

    if (cards.length > 1 && !chosen) {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = `Select SD card (${cards.length} found)…`;
      el.sdCardSelect.insertBefore(placeholder, el.sdCardSelect.firstChild);
      el.sdCardSelect.value = "";
      el.sourcePath.value = "";
    } else if (chosen) {
      el.sdCardSelect.value = chosen;
      applySelectedPath(chosen, { label: selectedSdCardLabel() });
    }

    if (el.sdCardHint) {
      el.sdCardHint.textContent =
        cards.length === 1
          ? `Selected ${cards[0].id} automatically — press Scan footage`
          : `${cards.length} SD cards found — pick one, then Scan footage`;
    }
    if (!quiet) {
      setStatus(
        cards.length === 1
          ? `SD card ${cards[0].id} ready`
          : `${cards.length} SD cards detected — pick one`,
        "ok",
      );
    }
    updateContextHint();
  } catch (error) {
    el.sdCardSelect.innerHTML = '<option value="">SD card detection failed</option>';
    if (!quiet) setStatus(error.message, "error");
  } finally {
    el.refreshSdBtn.disabled = false;
  }
}

function onSdCardChanged() {
  const path = (el.sdCardSelect?.value || "").trim();
  if (!path) {
    el.sourcePath.value = "";
    updateContextHint();
    return;
  }
  applySelectedPath(path, { label: selectedSdCardLabel() });
  setStatus(`Selected ${selectedSdCardLabel() || path}`, "ok");
  updateContextHint();
}

async function loadTasks() {
  const data = await api("/api/eager/tasks");
  state.tasks = data.tasks || [];
  renderTasks();
  updateContextHint();
}

async function addTask() {
  const name = el.newTaskInput.value.trim();
  if (!name) {
    setStatus("Type a task name first", "error");
    return;
  }
  el.addTaskBtn.disabled = true;
  try {
    const data = await api("/api/eager/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        label_root: state.labelRoot || state.scanRoot || scanTargetPath(),
      }),
    });
    state.tasks = data.tasks || [];
    el.taskSearch.value = "";
    el.newTaskInput.value = "";
    renderTasks(name);
    el.taskAddedMsg.textContent = `Added: ${name}`;
    setStatus(`Task added: ${name}`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.addTaskBtn.disabled = false;
  }
}

async function refreshLabelProgress({ quiet = false } = {}) {
  const path = state.labelRoot || state.scanRoot || scanTargetPath();
  if (!path) return null;
  try {
    const data = await api(`/api/eager/label-progress?path=${encodeURIComponent(path)}`);
    state.labelProgress = data;
    renderFileList();
    updateContextHint();
    if (!quiet) {
      setStatus(data.message || `${data.unlabeled} unlabeled left`, data.complete ? "ok" : "");
    }
    return data;
  } catch (error) {
    if (!quiet) setStatus(error.message, "error");
    return null;
  }
}

async function scanSource() {
  const path = scanTargetPath();
  if (!path) {
    setStatus("Select an SD card first (or press Refresh)", "error");
    return;
  }

  state.scanRoot = path;
  state.labelRoot = path;
  ensureWorkTimerRunning(path);
  // Unified list: raw wholes + finished trimmed clips (busy outputs excluded server-side).
  const mode = "label";

  setStatus("Scanning...");
  el.scanBtn.disabled = true;
  try {
    const data = await api("/api/eager/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive: true, mode }),
    });
    const keepDone = state.donePaths;
    const keepTrim = state.trimmingPaths;
    const keepLabeled = state.labeledTasks;
    state.videos = (data.videos || []).filter((v) => !keepDone.has(v.path));
    state.donePaths = keepDone;
    state.trimmingPaths = keepTrim;
    state.labeledTasks = keepLabeled;
    state.index = -1;
    state.labelProgress = data.progress || null;
    saveActiveWorkspace();
    renderCardTabs();
    renderFileList();
    el.scanSummary.textContent = `${state.videos.length} files`;
    if (state.videos.length) {
      showLoading("Loading folder", `Found ${state.videos.length} files`, 10);
      await loadVideo(0);
      hideLoading();
      setStatus(`Found ${state.videos.length} files — Enter to label · I/O/T then N to trim`, "ok");
    } else {
      setStatus(
        state.labelProgress?.complete
          ? "All footage is inside task folders"
          : "No footage found",
        state.labelProgress?.complete ? "ok" : "error",
      );
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.scanBtn.disabled = false;
  }
}

const PREFETCH_AHEAD = 2;

function prefetchSnapshotsBackground(fromIndex) {
  if (state.phase !== "clean" || !state.perf.prefetch) return;
  const start = fromIndex ?? state.index + 1;
  const end = Math.min(state.videos.length, start + PREFETCH_AHEAD);
  (async () => {
    for (let i = start; i < end; i += 1) {
      if (state.phase !== "clean") return;
      const video = state.videos[i];
      if (!video) continue;
      try {
        const q = snapshotQuery(video, { priority: "background" });
        const status = await api(`/api/eager/snapshots/status?${q}`);
        if (
          status.status !== "ready"
          && status.status !== "running"
          && status.status !== "queued"
        ) {
          await api(`/api/eager/snapshots/status?${q}&start=1`);
        }
      } catch {
        /* background prefetch — ignore */
      }
    }
  })();
}

async function trimMarkedClip() {
  const video = currentVideo();
  if (!video || !state.pendingClip) {
    setStatus("Mark a clip first (Mark start + Mark end)", "error");
    return;
  }

  const clip = { start: state.pendingClip.start, end: state.pendingClip.end };
  state.pendingClip = null;
  renderClips();

  try {
    const data = await api("/api/eager/trim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: video.path,
        start: clip.start,
        end: clip.end,
      }),
    });
    state.savedClips.push({
      job_id: data.job_id,
      status: data.status || "queued",
      start: clip.start,
      end: clip.end,
      duration_seconds: clip.end - clip.start,
      progress: 0,
      remaining_seconds: clip.end - clip.start,
      output: null,
      name: null,
      source_has_gpmf: data.source_has_gpmf,
    });
    renderClips();
    startTrimPolling();
    const imuNote =
      data.source_has_gpmf === true ? " (IMU will be verified)" : "";
    setStatus(`Queued trim ${formatTime(clip.start)} → ${formatTime(clip.end)}${imuNote} — mark next clip`, "ok");
  } catch (error) {
    state.pendingClip = clip;
    renderClips();
    setStatus(error.message, "error");
  }
}

async function finishCleaningFile() {
  const video = currentVideo();
  if (!video) return;

  if (state.pendingClip) {
    setStatus("Press T to queue the marked clip first", "error");
    return;
  }
  if (state.pendingIn !== null) {
    setStatus("Finish the mark (O then T) or Undo before Next", "error");
    return;
  }

  const hasClips = state.savedClips.some(
    (j) => j.status === "completed" || j.output || j.status === "queued" || j.status === "running",
  );

  if (!hasClips) {
    setStatus("No trims queued — type a task and press Enter to label this clean file", "error");
    focusTaskSearch();
    return;
  }

  try {
    const data = await api("/api/eager/clean", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: video.path }),
    });
    state.trimmingPaths.add(video.path);
    stopTrimPolling();
    saveActiveWorkspace();
    if (data.scheduled) {
      setStatus(
        `Trimming in background (${data.active}) — label those clips when they appear · + for another card`,
        "ok",
      );
    } else if (data.deleted_source) {
      setStatus(`Finished ${video.name} — raw removed; label new clips when listed`, "ok");
    } else {
      setStatus(`Finished ${video.name}`, "ok");
    }
    renderFileList();
    advanceToNext();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function deleteCurrentFile() {
  const video = currentVideo();
  if (!video) return;
  if (state.pendingClip || state.pendingIn !== null) {
    setStatus("Clear the current mark before deleting file", "error");
    return;
  }
  if (activeTrimCount() > 0) {
    setStatus("Cannot delete while trim jobs are running for this file", "error");
    return;
  }
  if (!window.confirm(`Delete whole file?\n\n${video.name}`)) return;

  try {
    const data = await api("/api/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: video.path, confirmed: true }),
    });
    state.donePaths.add(video.path);
    stopTrimPolling();
    setStatus(data.message || `Deleted ${video.name}`, "ok");
    advanceToNext();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function labelCurrentClip() {
  if (state.busy) return;
  const video = currentVideo();
  if (!video) return;

  if (state.trimmingPaths.has(video.path)) {
    setStatus("This file is still trimming — wait for clips, then label those", "error");
    return;
  }
  if (state.pendingIn !== null || state.pendingClip) {
    setStatus("Finish or undo the current mark before labeling", "error");
    return;
  }
  if (activeTrimCount() > 0 || state.savedClips.some((j) => j.status === "queued" || j.status === "running")) {
    setStatus("Press N to finish trims first — label the new clips when they appear", "error");
    return;
  }
  if (!video.is_trimmed && state.savedClips.some((j) => j.status === "completed" || j.output)) {
    setStatus("Press N to finish this file — then label the trimmed clips", "error");
    return;
  }

  let task = selectedTask();
  if (!task) {
    setStatus("Type or choose a task first", "error");
    focusTaskSearch();
    return;
  }

  if (!state.tasks.some((item) => item.toLowerCase() === task.toLowerCase())) {
    try {
      const data = await api("/api/eager/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: task,
          label_root: state.labelRoot || state.scanRoot || scanTargetPath(),
        }),
      });
      state.tasks = data.tasks || [];
      renderTasks(task);
    } catch (error) {
      setStatus(error.message, "error");
      return;
    }
  }

  state.busy = true;
  el.labelBtn.disabled = true;
  setStatus(`Moving to ${task}...`);
  try {
    const data = await api("/api/eager/label", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: video.path,
        label_root: state.labelRoot || state.scanRoot,
        task,
      }),
    });
    // Invalidate any in-flight soft refresh that could put this path back.
    state.labelScanToken += 1;
    state.donePaths.add(video.path);
    state.labeledTasks[video.path] = task;
    state.lastLabelTask = task;
    state.videos = state.videos.filter((item) => item.path !== video.path);
    saveActiveWorkspace();
    el.taskSearch.value = "";
    renderTasks(task);
    el.taskSearch.blur();
    if (state.labelProgress) {
      const nextUnlabeled = Math.max(0, (state.labelProgress.unlabeled || 0) - 1);
      const nextLabeled = (state.labelProgress.labeled || 0) + 1;
      state.labelProgress = {
        ...state.labelProgress,
        unlabeled: nextUnlabeled,
        labeled: nextLabeled,
        complete: nextUnlabeled === 0,
        message:
          nextUnlabeled === 0
            ? "All footage is inside task folders"
            : `${nextUnlabeled} file(s) still outside task folders`,
      };
    }
    const already = data.already_there
      ? `Already in ${task} — removed leftover copy`
      : `Moved to ${task}`;
    setStatus(
      `${already} · ${remainingUnlabeledCount()} left · N/Enter reuses "${task}" · S to search`,
      "ok",
    );
    // Remove from list now; the same index becomes the next unlabeled clip.
    const nextIndex = state.index;
    state.videos = state.videos.filter((item) => item.path !== video.path);
    renderFileList();
    if (nextIndex < state.videos.length) {
      await loadVideo(nextIndex);
    } else if (state.videos.length) {
      await loadVideo(state.videos.length - 1);
    } else {
      state.index = -1;
      el.currentName.textContent = "No file loaded";
      el.currentMeta.textContent = "";
      updateContextHint();
      const remaining = remainingUnlabeledCount();
      if (remaining === 0) {
        setStatus(
          `All clips labeled — work ${formatWorkHms(workTimer.cleanMs + workTimer.labelMs)}`,
          "ok",
        );
        saveWorkSession("label_complete");
      }
    }
    refreshLabelProgress({ quiet: true });
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.labelBtn.disabled = false;
  }
}

function advanceToNext() {
  let next = state.index + 1;
  while (
    next < state.videos.length
    && (state.donePaths.has(state.videos[next].path) || state.trimmingPaths.has(state.videos[next].path))
  ) {
    next += 1;
  }
  if (next < state.videos.length) {
    loadVideo(next);
  } else {
    if (state.videos.some((v) => !state.donePaths.has(v.path) && !state.trimmingPaths.has(v.path))) {
      // wrap to first unfinished
      const first = state.videos.findIndex(
        (v) => !state.donePaths.has(v.path) && !state.trimmingPaths.has(v.path),
      );
      if (first >= 0) {
        loadVideo(first);
        return;
      }
    }
    const remaining = state.videos.filter(
      (v) => !state.donePaths.has(v.path) && !state.trimmingPaths.has(v.path),
    ).length;
    setStatus(
      remaining === 0
        ? state.trimmingPaths.size
          ? `All files handled — ${state.trimmingPaths.size} still trimming in background`
          : "All files done"
        : `${remaining} file(s) left`,
      remaining === 0 ? "ok" : "",
    );
    renderFileList();
  }
}

el.snapPrevBtn?.addEventListener("click", () => goToSnapshot(-1));
el.snapNextBtn?.addEventListener("click", () => goToSnapshot(1));
el.fineBackBtn?.addEventListener("click", () => fineTune(-3));
el.fineFwdBtn?.addEventListener("click", () => fineTune(3));
el.markStartBtn.addEventListener("click", markStart);
el.markEndBtn.addEventListener("click", markEnd);

el.scrubTrack.addEventListener("mousedown", (event) => {
  if (!currentVideo()) return;
  event.stopPropagation();
  const rect = el.scrubTrack.getBoundingClientRect();
  seekToFraction((event.clientX - rect.left) / rect.width);
});

el.browseFolderBtn.addEventListener("click", chooseFootageFolder);
el.refreshSdBtn?.addEventListener("click", () => refreshSdCards());
el.sdCardSelect?.addEventListener("change", onSdCardChanged);
el.scanBtn.addEventListener("click", scanSource);
el.undoClipBtn.addEventListener("click", undoMark);
el.trimBtn.addEventListener("click", trimMarkedClip);
el.deleteFileBtn?.addEventListener("click", deleteCurrentFile);
el.nextCleanBtn.addEventListener("click", finishCleaningFile);
el.addCardTab?.addEventListener("click", addWorkspaceTab);
el.taskSearch.addEventListener("input", () => renderTasks());
el.taskSearch.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    moveTaskSelection(1);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    moveTaskSelection(-1);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    if (!selectedTask() && state.lastLabelTask) {
      renderTasks(state.lastLabelTask);
    }
    leaveTaskSearch({ clear: true });
    labelCurrentClip();
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    leaveTaskSearch({ clear: true });
  }
});
el.addTaskBtn.addEventListener("click", addTask);
el.newTaskInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addTask();
  }
});
el.labelBtn.addEventListener("click", labelCurrentClip);
el.recheckLabelBtn?.addEventListener("click", () => refreshLabelProgress());
el.player.addEventListener("timeupdate", () => {
  if (!el.player.paused && Number.isFinite(el.player.currentTime) && el.player.currentTime > 0) {
    state.scrubTime = el.player.currentTime;
    updateScrubUi();
  }
});
el.player.addEventListener("loadedmetadata", updateScrubUi);

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select")) return;
  const key = event.key.toLowerCase();

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    goToSnapshot(-1);
    return;
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    goToSnapshot(1);
    return;
  }
  if (event.key === ",") {
    event.preventDefault();
    fineTune(-scrubStepSeconds());
    return;
  }
  if (event.key === ".") {
    event.preventDefault();
    fineTune(scrubStepSeconds());
    return;
  }

  if (key === "i") {
    event.preventDefault();
    markStart();
  }
  if (key === "o") {
    event.preventDefault();
    markEnd();
  }
  if (key === "t") {
    event.preventDefault();
    trimMarkedClip();
  }
  if (key === "n") {
    event.preventDefault();
    finishCleaningFile();
  }
  if (key === "d" || event.key === "Delete") {
    event.preventDefault();
    deleteCurrentFile();
  }
  if (key === " ") {
    event.preventDefault();
    if (el.player.paused) el.player.play();
    else el.player.pause();
  }
  if (key === "s") {
    event.preventDefault();
    focusTaskSearch();
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    if (!selectedTask() && state.lastLabelTask) {
      renderTasks(state.lastLabelTask);
    }
    labelCurrentClip();
  }
});

loadTasks()
  .then(() => Promise.all([api("/api/health"), api("/api/eager/config"), refreshSdCards({ quiet: true })]))
  .then(([health, perf]) => {
    if (el.appVersion) el.appVersion.textContent = `v${health.version || "?"}`;
    state.perf = { ...state.perf, ...perf };
    ensureWorkspaces();
    renderCardTabs();
    setPhase("clean");
    startGlobalTrimPolling();
    if (health.ffmpeg_ok === false) {
      setStatus(health.ffmpeg_hint || "FFmpeg missing — install and restart", "error");
    } else if (perf.lite_mode && perf.hint) {
      setStatus(perf.hint, "ok");
    }
  })
  .catch((error) => setStatus(error.message, "error"));
