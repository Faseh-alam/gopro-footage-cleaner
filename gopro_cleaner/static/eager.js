const state = {
  phase: "clean", // annotation review: contiguous work/garbage marks (no live trim)
  videos: [],
  index: -1,
  tasks: [],
  scanRoot: "",
  labelRoot: "",
  pendingIn: null,
  pendingClip: null,
  savedClips: [],
  /** Per-source mark/trim state so scrub-bar tints survive navigation. */
  clipsByPath: {},
  batchId: null,
  batchDetail: null,
  cardIdentity: { factory: "", card_badge: "", device_type: "", device_id: "" },
  annotationsByPath: {}, // path -> {segments, duration, complete, pendingWork:null|{start,end}}
  anchorByPath: {},
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
  recentTasks: [],
  busy: false,
  previewToken: 0,
  seekTimer: null,
  pendingSeek: null,
  lastVideoPath: "",
  scrubTime: 0,
  activeClipKey: "",
  labelProgress: null,
  workspaces: [],
  activeWorkspaceId: null,
  perf: {
    lite_mode: false,
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
  taskFocusOverlay: document.getElementById("task-focus-overlay"),
  scrubTrack: document.getElementById("scrub-track"),
  scrubFill: document.getElementById("scrub-fill"),
  scrubPlayhead: document.getElementById("scrub-playhead"),
  previewStatus: document.getElementById("preview-status"),
  loadingOverlay: document.getElementById("loading-overlay"),
  loadingTitle: document.getElementById("loading-title"),
  loadingDetail: document.getElementById("loading-detail"),
  loadingBarFill: document.getElementById("loading-bar-fill"),
  loadingHint: document.getElementById("loading-hint"),
  contextBanner: document.getElementById("context-banner"),
  contextMessage: document.getElementById("context-message"),
  fineBackBtn: document.getElementById("fine-back-btn"),
  fineFwdBtn: document.getElementById("fine-fwd-btn"),
  markStartBtn: document.getElementById("mark-start-btn"),
  markEndBtn: document.getElementById("mark-end-btn"),
  markSection: document.getElementById("mark-section"),
  currentName: document.getElementById("current-name"),
  timeDisplay: document.getElementById("time-display"),
  playbackRate: document.getElementById("playback-rate"),
  undoClipBtn: document.getElementById("undo-clip-btn"),
  pendingIn: document.getElementById("pending-in"),
  gpmfStatus: document.getElementById("gpmf-status"),
  trimProgressPanel: document.getElementById("trim-progress-panel"),
  trimActiveCount: document.getElementById("trim-active-count"),
  trimEtaTotal: document.getElementById("trim-eta-total"),
  trimProgressFill: document.getElementById("trim-progress-fill"),
  trimProgressList: document.getElementById("trim-progress-list"),
  trimProgressByCard: document.getElementById("trim-progress-by-card"),
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
  updateBtn: document.getElementById("update-btn"),
  batchCsvInput: document.getElementById("batch-csv-input"),
  importBatchBtn: document.getElementById("import-batch-btn"),
  refreshBatchBtn: document.getElementById("refresh-batch-btn"),
  finishCardBtn: document.getElementById("finish-card-btn"),
  completeBatchBtn: document.getElementById("complete-batch-btn"),
  downloadReportJson: document.getElementById("download-report-json"),
  downloadReportCsv: document.getElementById("download-report-csv"),
  batchStatus: document.getElementById("batch-status"),
  batchCards: document.getElementById("batch-cards"),
  batchReport: document.getElementById("batch-report"),
  coverageMeta: document.getElementById("coverage-meta"),
  scrubRanges: document.getElementById("scrub-ranges"),
  markWorkBtn: document.getElementById("mark-work-btn"),
  markGarbageBtn: document.getElementById("mark-garbage-btn"),
  undoSegmentBtn: document.getElementById("undo-segment-btn"),
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

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

const RECENT_TASKS_KEY = "gopro_eager_recent_tasks";
const RECENT_TASKS_MAX = 10;

function loadRecentTasks() {
  try {
    const raw = localStorage.getItem(RECENT_TASKS_KEY);
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item) => typeof item === "string" && item.trim());
  } catch {
    return [];
  }
}

function saveRecentTasks(recent) {
  try {
    localStorage.setItem(RECENT_TASKS_KEY, JSON.stringify(recent.slice(0, RECENT_TASKS_MAX)));
  } catch {
    /* private browsing / quota — MRU stays in memory for this session */
  }
}

function touchRecentTask(task) {
  const name = String(task || "").trim();
  if (!name) return;
  const key = name.toLowerCase();
  const next = [name, ...state.recentTasks.filter((item) => item.toLowerCase() !== key)].slice(
    0,
    RECENT_TASKS_MAX,
  );
  state.recentTasks = next;
  saveRecentTasks(next);
}

function recentTaskRank(task) {
  const key = task.toLowerCase();
  const idx = state.recentTasks.findIndex((item) => item.toLowerCase() === key);
  return idx >= 0 ? idx : RECENT_TASKS_MAX + 1;
}

/** Recent tasks first (MRU order), then the rest alphabetically. Filtered search keeps the same priority. */
function orderedTaskGroups() {
  const q = el.taskSearch.value.trim().toLowerCase();
  const pool = q
    ? state.tasks.filter((task) => task.toLowerCase().includes(q))
    : [...state.tasks];
  const recent = [];
  const others = [];
  for (const task of pool) {
    if (recentTaskRank(task) <= RECENT_TASKS_MAX) {
      recent.push(task);
    } else {
      others.push(task);
    }
  }
  recent.sort((a, b) => recentTaskRank(a) - recentTaskRank(b));
  others.sort((a, b) => a.localeCompare(b));
  return { recent, others, matches: [...recent, ...others], filtering: Boolean(q) };
}

function visibleTasks() {
  return orderedTaskGroups().matches;
}

function scrubStepSeconds() {
  return state.phase === "label" ? 3 : 1;
}

function setPhase(_phase) {
  // Unified clean+label flow — marking tools stay available; labeling always on.
  state.phase = "clean";
  if (el.phaseClean) el.phaseClean.classList.add("active");
  if (el.phaseLabel) el.phaseLabel.classList.remove("active");
  if (el.cleanPanel) el.cleanPanel.classList.remove("hidden");
  if (el.labelPanel) el.labelPanel.classList.add("hidden");
  if (el.listTitle) el.listTitle.textContent = "Footage";
  if (el.scanBtn) el.scanBtn.textContent = "Scan";
  // Mark buttons stay hidden — keyboard (I/O/T) drives them; empty buttons
  // were showing as white pills on Windows when this class was removed.
  if (el.markSection) el.markSection.classList.add("hidden");
  if (el.clipList) el.clipList.classList.remove("hidden");
  updateContextHint();
}

function createWorkspace(title) {
  const n = state.workspaces.length + 1;
  return {
    id: `ws-${Date.now()}-${n}`,
    title: title || `Footage ${n}`,
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

function captureWorkspace() {
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
    el.player.removeAttribute("src");
    updateContextHint();
  }
}

function saveActiveWorkspace() {
  const ws = state.workspaces.find((w) => w.id === state.activeWorkspaceId);
  if (!ws) return;
  Object.assign(ws, captureWorkspace());
  if (state.scanRoot) {
    const label = selectedSdCardLabel() || state.scanRoot.split(/[/\\]/).filter(Boolean).pop();
    if (label) ws.title = shortCardTitle(label);
  }
}

function renderCardTabs() {
  if (!el.cardTabList) return;
  el.cardTabList.innerHTML = "";
  for (const ws of state.workspaces) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "card-tab" + (ws.id === state.activeWorkspaceId ? " active" : "");
    btn.textContent = shortCardTitle(ws.title);
    btn.title = ws.scanRoot || "No footage selected";
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

function ensureWorkspaces() {
  if (state.workspaces.length) return;
  const ws = createWorkspace("Footage");
  state.workspaces.push(ws);
  state.activeWorkspaceId = ws.id;
}

function remainingUnlabeledCount() {
  if (state.labelProgress && Number.isFinite(state.labelProgress.unlabeled)) {
    return Math.max(0, state.labelProgress.unlabeled);
  }
  return state.videos.filter(
    (video) =>
      !state.donePaths.has(video.path)
      && !state.labeledTasks[video.path]
      && !state.trimmingPaths.has(video.path),
  ).length;
}

function isHandledPath(path) {
  return Boolean(state.annotationsByPath[path]?.complete);
}

function annotationFor(path) {
  if (!path) return null;
  return state.annotationsByPath[path] || null;
}

function currentAnnotation() {
  return annotationFor(currentVideo()?.path);
}

function currentPendingWork() {
  return currentAnnotation()?.pendingWork || null;
}

function computeAnchor(segments) {
  if (!segments?.length) return 0;
  const last = segments[segments.length - 1];
  const end = Number(last?.end);
  return Number.isFinite(end) ? end : 0;
}

function normalizeMarkEnd(time) {
  const video = currentVideo();
  const duration =
    el.player.duration
    || video?.duration
    || annotationFor(video?.path)?.duration
    || 0;
  let value = Math.max(0, Number(time) || 0);
  if (duration > 0 && value >= duration - 0.05) value = duration;
  return value;
}

function annotationContext() {
  const id = state.cardIdentity || {};
  const batch = state.batchDetail || {};
  const video = currentVideo();
  const duration =
    el.player.duration
    || video?.duration
    || annotationFor(video?.path)?.duration
    || undefined;
  return {
    batch_name: batch.batch_name || "",
    factory: id.factory || "",
    card_badge: id.card_badge || "",
    device_type: id.device_type || "",
    device_id: id.device_id || "",
    duration,
  };
}

function applyAnnotationPayload(path, payload, { keepPending = false } = {}) {
  if (!path) return null;
  const annotation = payload?.annotation || payload || {};
  const summary = payload?.summary || {};
  const prev = state.annotationsByPath[path];
  const segments = Array.isArray(annotation.segments) ? annotation.segments : [];
  const duration =
    annotation.duration != null
      ? Number(annotation.duration)
      : summary.duration != null
        ? Number(summary.duration)
        : prev?.duration ?? null;
  const complete = Boolean(
    summary.complete != null ? summary.complete : annotation.complete,
  );
  const next = {
    segments,
    duration: Number.isFinite(duration) ? duration : null,
    complete,
    pendingWork: keepPending ? prev?.pendingWork || null : null,
    summary,
  };
  state.annotationsByPath[path] = next;
  state.anchorByPath[path] = computeAnchor(segments);
  return next;
}

async function loadAnnotationForPath(path, { keepPending = false } = {}) {
  if (!path) return null;
  try {
    const data = await api(`/api/eager/annotations?path=${encodeURIComponent(path)}`);
    return applyAnnotationPayload(path, data, { keepPending });
  } catch (error) {
    if (!state.annotationsByPath[path]) {
      state.annotationsByPath[path] = {
        segments: [],
        duration: null,
        complete: false,
        pendingWork: null,
      };
      state.anchorByPath[path] = 0;
    }
    setStatus(error.message || "Could not load annotations", "error");
    return state.annotationsByPath[path];
  }
}

function updateCoverageMeta() {
  if (!el.coverageMeta) return;
  const video = currentVideo();
  const ann = annotationFor(video?.path);
  const duration =
    ann?.duration
    || el.player.duration
    || video?.duration
    || 0;
  const covered = (ann?.segments || []).reduce((sum, seg) => {
    const start = Number(seg.start);
    const end = Number(seg.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return sum;
    return sum + (end - start);
  }, 0);
  const unreviewed = Math.max(0, (duration || 0) - covered);
  const pct = duration > 0 ? Math.round((covered / duration) * 100) : 0;
  el.coverageMeta.textContent =
    duration > 0
      ? `${pct}% covered · ${formatDurationShort(unreviewed)} unreviewed`
      : "0% covered";
}

function stashClipState(path) {
  if (!path) return;
  state.clipsByPath[path] = {
    savedClips: state.savedClips.map((job) => ({ ...job })),
    pendingIn: state.pendingIn,
    pendingClip: state.pendingClip ? { ...state.pendingClip } : null,
  };
}

function restoreClipState(path) {
  const cached = path ? state.clipsByPath[path] : null;
  if (cached) {
    state.savedClips = (cached.savedClips || []).map((job) => ({ ...job }));
    state.pendingIn = cached.pendingIn ?? null;
    state.pendingClip = cached.pendingClip ? { ...cached.pendingClip } : null;
  } else {
    state.savedClips = [];
    state.pendingIn = null;
    state.pendingClip = null;
  }
}

function migrateClipState(oldPath, newPath) {
  if (!oldPath || !newPath || oldPath === newPath) return;
  if (state.clipsByPath[oldPath]) {
    state.clipsByPath[newPath] = state.clipsByPath[oldPath];
    delete state.clipsByPath[oldPath];
  }
}

/** Keep labeled / deleted / mid-trim rows when a scan returns only unlabeled files. */
function mergeScanVideos(fresh) {
  const freshByPath = new Map((fresh || []).map((v) => [v.path, v]));
  const merged = [];
  const seen = new Set();

  for (const video of state.videos) {
    if (freshByPath.has(video.path)) {
      merged.push(freshByPath.get(video.path));
      seen.add(video.path);
      continue;
    }
    if (
      state.labeledTasks[video.path]
      || state.donePaths.has(video.path)
    ) {
      merged.push(video);
      seen.add(video.path);
    }
  }
  for (const video of fresh || []) {
    if (seen.has(video.path)) continue;
    if (state.donePaths.has(video.path)) continue;
    merged.push(video);
    seen.add(video.path);
  }
  return merged;
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
  const prevScroll = el.fileList.scrollTop;
  el.fileList.innerHTML = "";
  const completeCount = items.filter((v) => state.annotationsByPath[v.path]?.complete).length;
  el.listSummary.textContent = items.length
    ? `${completeCount}/${items.length} complete`
    : "";

  for (const video of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "file-item";
    if (state.videos[state.index]?.path === video.path) btn.classList.add("active");
    const ann = state.annotationsByPath[video.path];
    if (ann?.complete) {
      btn.classList.add("labeled");
    } else if (ann?.segments?.length || ann?.pendingWork) {
      btn.classList.add("trimming");
    }
    btn.innerHTML = `<span class="name">${video.name}</span><span class="meta">${video.duration_label || "?"} · ${formatBytes(video.size_bytes)}</span>`;
    btn.addEventListener("click", () => {
      const idx = state.videos.findIndex((item) => item.path === video.path);
      if (idx >= 0) loadVideo(idx);
    });
    el.fileList.appendChild(btn);
  }
  el.fileList.scrollTop = prevScroll;
}

function renderClips() {
  el.clipList.innerHTML = "";
  const ann = currentAnnotation();
  for (let index = 0; index < (ann?.segments || []).length; index += 1) {
    const seg = ann.segments[index];
    const item = document.createElement("li");
    item.dataset.segmentIndex = String(index);
    item.dataset.start = String(seg.start);
    item.dataset.end = String(seg.end);
    const range = `${formatTime(seg.start)} → ${formatTime(seg.end)}`;
    if (seg.kind === "garbage") {
      item.className = "failed";
      item.textContent = `Garbage ${range}`;
    } else {
      item.className = "saved";
      const task = seg.task ? ` · ${seg.task}` : "";
      item.textContent = `Work ${range}${task}`;
    }
    el.clipList.appendChild(item);
  }
  if (ann?.pendingWork) {
    const item = document.createElement("li");
    item.className = "pending";
    item.dataset.pending = "1";
    item.dataset.start = String(ann.pendingWork.start);
    item.dataset.end = String(ann.pendingWork.end);
    item.textContent = `Pending work ${formatTime(ann.pendingWork.start)} → ${formatTime(ann.pendingWork.end)} — choose task then Enter`;
    el.clipList.appendChild(item);
  }
  if (el.pendingIn) {
    el.pendingIn.textContent = "";
    el.pendingIn.className = "hidden";
  }
  updateCoverageMeta();
  syncClipListToPlayhead({ forceScroll: true });
}

function playheadClipKey(time, ann) {
  if (!ann) return "";
  for (let index = 0; index < (ann.segments || []).length; index += 1) {
    const seg = ann.segments[index];
    const start = Number(seg.start);
    const end = Number(seg.end);
    if (Number.isFinite(start) && Number.isFinite(end) && time >= start && time < end) {
      return `seg:${index}`;
    }
  }
  if (ann.pendingWork) {
    const start = Number(ann.pendingWork.start);
    const end = Number(ann.pendingWork.end);
    if (Number.isFinite(start) && Number.isFinite(end) && time >= start && time < end) {
      return "pending";
    }
  }
  return "";
}

function syncClipListToPlayhead({ forceScroll = false } = {}) {
  if (!el.clipList) return;
  const ann = currentAnnotation();
  const time = state.scrubTime;
  const nextKey = playheadClipKey(time, ann);
  const keyChanged = nextKey !== state.activeClipKey;
  state.activeClipKey = nextKey;

  let activeEl = null;
  for (const item of el.clipList.querySelectorAll("li")) {
    const start = Number(item.dataset.start);
    const end = Number(item.dataset.end);
    const active =
      nextKey &&
      Number.isFinite(start) &&
      Number.isFinite(end) &&
      time >= start &&
      time < end;
    item.classList.toggle("at-playhead", active);
    if (active) activeEl = item;
  }

  if (activeEl && (forceScroll || keyChanged)) {
    activeEl.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}

function basenamePath(path) {
  if (!path) return "";
  return String(path).split(/[/\\]/).pop() || "";
}

function cardLabelFromPath(path) {
  const parts = String(path || "").split(/[/\\]/).filter(Boolean);
  for (const part of parts) {
    if (/^C\d{4}$/i.test(part)) return part.toUpperCase();
  }
  for (const ws of state.workspaces) {
    const root = ws.scanRoot || ws.labelRoot || "";
    if (root && String(path || "").startsWith(root)) {
      return shortCardTitle(ws.title || "Card");
    }
  }
  return shortCardTitle(selectedSdCardLabel() || "Card");
}

function shortCardTitle(title) {
  const raw = String(title || "Card").trim();
  const match = raw.match(/C\d{4}/i);
  if (match) return match[0].toUpperCase();
  // Drop long path suffixes — keep first token only.
  return raw.split(/[\s·—-]/)[0] || "Card";
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
        `${active} clip(s) trimming · ~${formatDurationShort(state.trimEtaTotal)} left`;
    }
  }

  if (!el.trimProgressPanel) return;

  const activeJobs = jobs.filter((j) => j.status === "queued" || j.status === "running");
  if (active === 0) {
    el.trimProgressPanel.classList.add("hidden");
    if (el.trimProgressFill) el.trimProgressFill.style.width = "0%";
    if (el.trimProgressList) el.trimProgressList.innerHTML = "";
    if (el.trimProgressByCard) el.trimProgressByCard.innerHTML = "";
    return;
  }

  el.trimProgressPanel.classList.remove("hidden");
  if (el.trimActiveCount) el.trimActiveCount.textContent = String(active);
  if (el.trimEtaTotal) {
    el.trimEtaTotal.textContent = `~${formatDurationShort(state.trimEtaTotal)}`;
  }

  if (!activeJobs.length) {
    // Counts say busy but job payloads missing — keep panel visible with a hint.
    if (el.trimProgressByCard) el.trimProgressByCard.innerHTML = "";
    if (el.trimProgressList) {
      el.trimProgressList.innerHTML = "";
      const row = document.createElement("div");
      row.textContent = `${active} trim(s) running — refreshing…`;
      el.trimProgressList.appendChild(row);
    }
    return;
  }

  // Combined overall progress across every active trim.
  const totalDuration = activeJobs.reduce(
    (sum, j) => sum + (j.duration_seconds || Math.max(0, (j.end_seconds || 0) - (j.start_seconds || 0)) || 0),
    0,
  );
  const doneDuration = activeJobs.reduce((sum, j) => {
    const dur = j.duration_seconds || Math.max(0, (j.end_seconds || 0) - (j.start_seconds || 0)) || 0;
    if (j.status === "running") return sum + (dur * (j.progress || 0)) / 100;
    if (j.status === "queued") return sum;
    return sum + dur;
  }, 0);
  const overallPct = totalDuration > 0 ? (doneDuration / totalDuration) * 100 : 0;
  if (el.trimProgressFill) {
    el.trimProgressFill.style.width = `${Math.min(100, Math.max(2, overallPct))}%`;
  }

  // Per-card combined bars.
  if (el.trimProgressByCard) {
    el.trimProgressByCard.innerHTML = "";
    const byCard = new Map();
    for (const job of activeJobs) {
      const card = cardLabelFromPath(job.source_path);
      if (!byCard.has(card)) byCard.set(card, []);
      byCard.get(card).push(job);
    }
    for (const [card, cardJobs] of byCard) {
      const block = document.createElement("div");
      block.className = "trim-card-block";
      const tot = cardJobs.reduce(
        (sum, j) => sum + (j.duration_seconds || Math.max(0, (j.end_seconds || 0) - (j.start_seconds || 0)) || 0),
        0,
      );
      const done = cardJobs.reduce((sum, j) => {
        const dur = j.duration_seconds || Math.max(0, (j.end_seconds || 0) - (j.start_seconds || 0)) || 0;
        if (j.status === "running") return sum + (dur * (j.progress || 0)) / 100;
        return sum;
      }, 0);
      const pct = tot > 0 ? Math.min(100, (done / tot) * 100) : 0;
      const running = cardJobs.filter((j) => j.status === "running").length;
      const queued = cardJobs.filter((j) => j.status === "queued").length;
      block.innerHTML = `
        <div class="trim-card-head"><strong>${card}</strong><span>${Math.round(pct)}% · ${running} run · ${queued} queued</span></div>
        <div class="trim-progress-bar"><div class="trim-progress-fill" style="width:${pct}%"></div></div>
      `;
      el.trimProgressByCard.appendChild(block);
    }
  }

  if (el.trimProgressList) {
    el.trimProgressList.innerHTML = "";
    for (const job of activeJobs.slice(0, 16)) {
      const row = document.createElement("div");
      const outName = basenamePath(job.output);
      const sourceName = job.source_name || basenamePath(job.source_path) || "clip";
      const card = cardLabelFromPath(job.source_path);
      if (job.status === "queued") {
        row.textContent = `${card} · Queued · ${sourceName}`;
      } else {
        const pct = Math.round(job.progress || 0);
        row.textContent = `${card} · ${pct}% · ${outName || sourceName}`;
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
    const fresh = data.videos || [];
    // Drop trimming markers for sources that are no longer busy / gone from disk.
    const freshPaths = new Set(fresh.map((v) => v.path));
    for (const p of [...state.trimmingPaths]) {
      if (!freshPaths.has(p)) state.trimmingPaths.delete(p);
    }
    state.videos = mergeScanVideos(fresh);
    state.labelProgress = data.progress || state.labelProgress;
    el.scanSummary.textContent = selectedSdCardLabel() || "";
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
        kind: job.kind || "trim",
        task: job.task || null,
        source_has_gpmf: job.source_has_gpmf,
        output_has_gpmf: job.output_has_gpmf,
      });
    }
  }
  state.savedClips.sort((a, b) => (a.start || 0) - (b.start || 0));
  const path = currentVideo()?.path;
  if (path) stashClipState(path);
  renderClips();
  renderMarkTints();
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

async function loadMediaProbe(_path) {
  // IMU/GPMF badge intentionally hidden — keep probe optional/off for a clean UI.
  if (el.gpmfStatus) el.gpmfStatus.classList.add("hidden");
  state.currentHasGpmf = null;
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
  // Pipeline hint stays fixed in the header. Status goes to the status line only.
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

/** Repaint the work/garbage bands on the scrub bar. */
function renderMarkTints() {
  updateScrubRangeTints(keptClipRanges());
}

function updateScrubRangeTints(ranges) {
  const host = el.scrubRanges || el.scrubTrack;
  if (!host) return;
  if (el.scrubRanges) {
    el.scrubRanges.innerHTML = "";
    el.scrubRanges.style.position = "absolute";
    el.scrubRanges.style.inset = "0";
    el.scrubRanges.style.pointerEvents = "none";
    el.scrubRanges.style.zIndex = "0";
  } else {
    host.querySelectorAll(".scrub-range").forEach((node) => node.remove());
  }
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration || !ranges.length) return;

  for (const r of ranges) {
    const start = Math.max(0, Number(r.start) || 0);
    const end = Math.min(duration, Number(r.end) || 0);
    if (end <= start) continue;
    const seg = document.createElement("div");
    const kindClass = r.kind === "garbage" ? "garbage" : "kept";
    seg.className = `scrub-range ${kindClass}`;
    seg.style.left = `${(start / duration) * 100}%`;
    seg.style.width = `${((end - start) / duration) * 100}%`;
    host.appendChild(seg);
  }
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

function appendTaskListItem(task, { selected, recent = false } = {}) {
  const option = document.createElement("option");
  option.value = task;
  option.textContent = task;
  el.taskSelect.appendChild(option);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `task-item${recent ? " recent" : ""}`;
  btn.textContent = task;
  if (task === selected) btn.classList.add("active");
  btn.addEventListener("click", () => {
    el.taskSelect.value = task;
    state.lastLabelTask = task;
    touchRecentTask(task);
    renderTasks(task);
  });
  el.taskList.appendChild(btn);
}

function renderTasks(preferred = "") {
  const { recent, others, matches, filtering } = orderedTaskGroups();
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

  if (recent.length && !filtering) {
    const label = document.createElement("div");
    label.className = "task-section-label";
    label.textContent = "Recent";
    el.taskList.appendChild(label);
    for (const task of recent) {
      appendTaskListItem(task, { selected, recent: true });
    }
    if (others.length) {
      const allLabel = document.createElement("div");
      allLabel.className = "task-section-label";
      allLabel.textContent = "All tasks";
      el.taskList.appendChild(allLabel);
    }
    for (const task of others) {
      appendTaskListItem(task, { selected, recent: false });
    }
  } else {
    for (const task of matches) {
      appendTaskListItem(task, {
        selected,
        recent: recentTaskRank(task) <= RECENT_TASKS_MAX,
      });
    }
  }

  if (selected && [...el.taskSelect.options].some((opt) => opt.value === selected)) {
    el.taskSelect.value = selected;
  } else if (el.taskSelect.options.length) {
    el.taskSelect.selectedIndex = 0;
  }
  if (el.taskSelectedHint) {
    const active = selectedTask();
    el.taskSelectedHint.textContent = active
      ? `Selected: ${active} — Enter assigns pending work`
      : state.recentTasks.length
        ? "Recent tasks pinned above · T then Enter to repeat last task"
        : "T marks work and focuses search · Enter assigns · G garbage";
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
  if (!el.taskSearch) return;
  el.taskSearch.value = "";
  renderTasks(state.lastLabelTask || selectedTask() || undefined);
  el.taskSearch.focus();
  const last = state.lastLabelTask;
  setStatus(
    currentPendingWork()
      ? last
        ? `Enter = ${last} · ↑↓ recent tasks · type to filter`
        : "Pick a recent task or type to filter — Enter assigns"
      : last
        ? `Enter selects ${last} · ↑↓ recent tasks · type to filter`
        : "Pick a recent task or type to filter",
    "ok",
  );
}

function setTaskSelectionMode(active) {
  el.playerWrap?.classList.toggle("task-selection", active);
  el.taskFocusOverlay?.setAttribute("aria-hidden", active ? "false" : "true");
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
  updatePlaybackRateUi();
  syncClipListToPlayhead();
}

const PLAYBACK_RATE_MIN = 0.5;
const PLAYBACK_RATE_MAX = 8;
const PLAYBACK_RATE_STEP = 0.5;

function updatePlaybackRateUi() {
  if (!el.playbackRate) return;
  const rate = Number(el.player.playbackRate) || 1;
  el.playbackRate.textContent = `${rate.toFixed(1)}×`;
  el.playbackRate.classList.toggle("boosted", rate > 1.01);
  el.playbackRate.classList.toggle("slow", rate < 0.99);
}

function setPlaybackRate(rate, { announce = true } = {}) {
  const clamped = Math.min(
    PLAYBACK_RATE_MAX,
    Math.max(PLAYBACK_RATE_MIN, Math.round(rate / PLAYBACK_RATE_STEP) * PLAYBACK_RATE_STEP),
  );
  el.player.playbackRate = clamped;
  updatePlaybackRateUi();
  if (announce) {
    setStatus(`Playback ${clamped.toFixed(1)}×`, "ok");
  }
}

function bumpPlaybackRate(delta) {
  if (!currentVideo()) return;
  setPlaybackRate((Number(el.player.playbackRate) || 1) + delta);
  // Speeding up while paused means "skim ahead" — start rolling instead of
  // forcing a second Space press, which would reset the rate to 1×.
  if (el.player.paused) el.player.play().catch(() => {});
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
  markWork();
}

function markEnd() {
  markWork();
}

async function undoMark() {
  await undoSegment();
}

function jumpToClipStart() {
  if (!currentVideo()) return;
  scheduleSeek(0, true);
  setStatus("At start of clip (0:00)", "ok");
}

/** Annotation ranges for scrub-bar tints (work=green/kept, garbage=red). */
function keptClipRanges() {
  const video = currentVideo();
  const ann = annotationFor(video?.path);
  const ranges = [];
  for (const seg of ann?.segments || []) {
    const start = Number(seg.start);
    const end = Number(seg.end);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
      ranges.push({ start, end, kind: seg.kind === "garbage" ? "garbage" : "work" });
    }
  }
  if (ann?.pendingWork) {
    ranges.push({
      start: ann.pendingWork.start,
      end: ann.pendingWork.end,
      kind: "work",
      pending: true,
    });
  }
  ranges.sort((a, b) => a.start - b.start);
  return ranges;
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
    stashClipState(previous.path);
    await cancelPreviewJob(previous.path);
  }

  state.index = index;
  restoreClipState(state.videos[index]?.path);
  stopTrimPolling();
  state.pendingSeek = null;
  state.scrubTime = 0;
  state.activeClipKey = "";
  if (state.seekTimer) {
    clearTimeout(state.seekTimer);
    state.seekTimer = null;
  }

  const video = state.videos[index];
  const token = ++state.previewToken;
  state.lastVideoPath = video.path;
  await loadAnnotationForPath(video.path, { keepPending: true });
  setTaskSelectionMode(Boolean(currentPendingWork()));

  el.currentName.textContent = video.name;
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
  updateCoverageMeta();
  renderMarkTints();

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
    setPlaybackRate(1, { announce: false });
    updateScrubUi();
    renderMarkTints();
    hideLoading();
    if (state.phase === "clean") {
      startTrimPolling();
    }
    updateContextHint();
    setStatus(`Ready — ${video.name} (Space to play, ← → speed)`, "ok");
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
  setStatus("Choose footage on this computer or an external drive…");
  try {
    const initial = el.sourcePath.value.trim() || el.sdCardSelect?.value || "";
    const query = initial ? `?initial=${encodeURIComponent(initial)}` : "";
    const data = await api(`/api/eager/pick-folder${query}`, { method: "POST" });
    if (data.cancelled) {
      setStatus("Folder selection cancelled");
      return;
    }

    saveActiveWorkspace();
    const current = state.workspaces.find((ws) => ws.id === state.activeWorkspaceId);
    if (current?.scanRoot || current?.videos?.length) {
      const ws = createWorkspace();
      state.workspaces.push(ws);
      applyWorkspace(ws);
    }

    const folderName = data.path.split(/[/\\]/).filter(Boolean).pop() || "Footage";
    applySelectedPath(data.path, { label: folderName, manual: true });
    setStatus(`Scanning ${folderName}…`);
    updateContextHint();
    await scanSource();
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
    ws.title = shortCardTitle(label || selectedSdCardLabel() || ws.title);
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

async function refreshSdCards({ quiet = false, autoScan = false } = {}) {
  if (!el.sdCardSelect) return;
  const previous = el.sdCardSelect.value;
  if (el.refreshSdBtn) el.refreshSdBtn.disabled = true;
  if (!quiet) setStatus("Detecting SD cards…");
  try {
    const data = await api("/api/eager/sd-cards");
    const cards = data.cards || [];
    el.sdCardSelect.innerHTML = "";

    if (!cards.length) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "No card";
      el.sdCardSelect.appendChild(empty);
      el.sourcePath.value = "";
      if (!quiet) setStatus("No SD card detected", "error");
      updateContextHint();
      return;
    }

    for (const card of cards) {
      const option = document.createElement("option");
      option.value = card.scan_path || card.path;
      option.dataset.label = card.id || card.label || "";
      option.dataset.volume = card.path || "";
      option.textContent = card.id || card.label || "Card";
      el.sdCardSelect.appendChild(option);
    }

    let chosen = "";
    if (previous && [...el.sdCardSelect.options].some((opt) => opt.value === previous)) {
      chosen = previous;
    } else if (cards.length === 1) {
      chosen = cards[0].scan_path || cards[0].path;
    } else {
      chosen = cards[0].scan_path || cards[0].path;
    }

    if (chosen) {
      el.sdCardSelect.value = chosen;
      applySelectedPath(chosen, { label: selectedSdCardLabel() });
    }

    if (!quiet) {
      setStatus(chosen ? `${selectedSdCardLabel() || "Card"} ready` : "Pick an SD card", "ok");
    }
    updateContextHint();

    if (autoScan && chosen) {
      await scanSource();
    }
  } catch (error) {
    el.sdCardSelect.innerHTML = '<option value="">Detection failed</option>';
    if (!quiet) setStatus(error.message, "error");
  } finally {
    if (el.refreshSdBtn) el.refreshSdBtn.disabled = false;
  }
}

function onSdCardChanged() {
  const path = (el.sdCardSelect?.value || "").trim();
  if (!path) return;
  applySelectedPath(path, { label: selectedSdCardLabel() });
  setIdentityFromSelectedCard();
  scanSource();
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
  if (!quiet) setStatus("Annotation mode active", "ok");
  return null;
}

async function loadBatchDetail(id = state.batchId) {
  if (!id) return null;
  const data = await api(`/api/eager/batches/${encodeURIComponent(id)}`);
  state.batchDetail = data.batch || null;
  renderBatchUi();
  return state.batchDetail;
}

function cardFromBadge(badge) {
  const cards = state.batchDetail?.cards || [];
  const b = String(badge || "").toUpperCase();
  return cards.find((c) => String(c.card_badge || "").toUpperCase() === b) || null;
}

function setIdentityFromSelectedCard() {
  const badge = selectedSdCardLabel();
  const card = cardFromBadge(badge);
  state.cardIdentity = card
    ? {
        factory: card.factory || state.batchDetail?.factory || "",
        card_badge: card.card_badge || "",
        device_type: card.device_type || "",
        device_id: card.device_id || "",
      }
    : { factory: "", card_badge: "", device_type: "", device_id: "" };
}

function renderBatchUi() {
  const b = state.batchDetail;
  if (!el.batchStatus) return;
  if (!b) {
    el.batchStatus.textContent = "No active batch — paste CSV and start.";
    if (el.batchCards) el.batchCards.innerHTML = "";
    if (el.batchReport) {
      el.batchReport.innerHTML = "";
      el.batchReport.classList.add("hidden");
    }
    if (el.downloadReportJson) el.downloadReportJson.classList.add("hidden");
    if (el.downloadReportCsv) el.downloadReportCsv.classList.add("hidden");
    return;
  }
  const done = b.cards_done ?? (b.cards || []).filter((c) => c.status === "complete").length;
  const total = b.card_count ?? (b.cards || []).length;
  el.batchStatus.textContent = `${b.batch_name} · ${b.factory || "?"} · ${done}/${total} cards · ${b.status || "open"}`;
  if (el.batchCards) {
    el.batchCards.innerHTML = (b.cards || [])
      .map((c) => {
        const vids = (c.assets || []).length;
        return `<div class="batch-card-row ${c.status || ""}"><strong>${escapeHtml(c.card_badge)}</strong> · ${escapeHtml(
          c.device_type || "?",
        )} / ${escapeHtml(c.device_id || "?")} · ${vids} video(s) · ${escapeHtml(c.status || "expected")}</div>`;
      })
      .join("");
  }
  const report = b.report || {};
  const t = report.totals || {};
  const blocking = report.blocking || b.blocking || [];
  if (el.batchReport) {
    el.batchReport.classList.remove("hidden");
    const taskLines = (report.tasks || [])
      .slice(0, 8)
      .map((row) => `${escapeHtml(row.task)}: ${Number(row.hours || 0).toFixed(2)}h`)
      .join(" · ");
    const deviceLines = (report.devices || [])
      .map(
        (d) =>
          `${escapeHtml(d.device_type)}: ${Number(d.raw_hours || 0).toFixed(2)}h raw / ${Number(
            d.clean_hours || 0,
          ).toFixed(2)}h clean`,
      )
      .join("<br>");
    el.batchReport.innerHTML = `
      <div><strong>Raw</strong> ${Number(t.raw_hours || 0).toFixed(2)}h ·
      <strong>Clean</strong> ${Number(t.clean_hours || 0).toFixed(2)}h ·
      <strong>Garbage</strong> ${Number(t.garbage_hours || 0).toFixed(2)}h ·
      <strong>Unreviewed</strong> ${Number(t.unreviewed_hours || 0).toFixed(2)}h</div>
      <div>Videos ${t.video_count || 0} · Tasks ${t.task_count || 0} · Device types ${t.device_type_count || 0}</div>
      ${taskLines ? `<div class="hint">Tasks: ${taskLines}</div>` : ""}
      ${deviceLines ? `<div class="hint">${deviceLines}</div>` : ""}
      ${
        blocking.length
          ? `<div class="batch-blocking"><strong>Blocking completion</strong><ul>${blocking
              .map((x) => `<li>${escapeHtml(x)}</li>`)
              .join("")}</ul></div>`
          : `<div class="hint ok-text">No blockers</div>`
      }
    `;
  }
  if (el.downloadReportJson) {
    el.downloadReportJson.href = `/api/eager/batches/${encodeURIComponent(b.id)}/report.json`;
    el.downloadReportJson.classList.remove("hidden");
  }
  if (el.downloadReportCsv) {
    el.downloadReportCsv.href = `/api/eager/batches/${encodeURIComponent(b.id)}/report.csv`;
    el.downloadReportCsv.classList.remove("hidden");
  }
}

async function scanSource() {
  const path = scanTargetPath();
  if (!path) {
    setStatus("Open a footage folder first", "error");
    return;
  }

  state.scanRoot = path;
  state.labelRoot = path;
  ensureWorkTimerRunning(path);
  const mode = "annotate";

  setStatus("Scanning...");
  el.scanBtn.disabled = true;
  try {
    const data = await api("/api/eager/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive: true, mode }),
    });

    if (state.batchId) {
      const badge = selectedSdCardLabel();
      if (badge && cardFromBadge(badge)) {
        const bind = await api(`/api/eager/batches/${encodeURIComponent(state.batchId)}/bind-card`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            card_badge: badge,
            mount_path: el.sdCardSelect?.selectedOptions?.[0]?.dataset?.volume || path,
            scan_path: path,
          }),
        });
        state.batchDetail = bind.batch || state.batchDetail;
      }
    }

    state.videos = data.videos || [];
    state.index = -1;
    await Promise.all(state.videos.map((v) => loadAnnotationForPath(v.path)));
    setIdentityFromSelectedCard();
    renderBatchUi();
    saveActiveWorkspace();
    renderCardTabs();
    renderFileList();
    el.scanSummary.textContent = selectedSdCardLabel() || "";

    const first = nextIncompleteIndex(0);
    if (first >= 0) {
      showLoading("Loading folder", `Found ${state.videos.length} files`, 10);
      await loadVideo(first);
      hideLoading();
      setStatus(`Found ${state.videos.length} files — T/G annotate, S/Enter assign, N next unfinished`, "ok");
    } else if (state.videos.length) {
      await loadVideo(0);
      setStatus(`All ${state.videos.length} files complete`, "ok");
    } else {
      setStatus("No footage found", "error");
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.scanBtn.disabled = false;
  }
}

async function trimMarkedClip() {
  setStatus("Trim disabled in annotation mode", "error");
}

async function saveTaskSnippet() {
  focusTaskSearch();
}

function nextIncompleteIndex(startAt = state.index + 1) {
  for (let i = startAt; i < state.videos.length; i += 1) {
    const v = state.videos[i];
    if (!state.annotationsByPath[v.path]?.complete) return i;
  }
  for (let i = 0; i < startAt; i += 1) {
    const v = state.videos[i];
    if (!state.annotationsByPath[v.path]?.complete) return i;
  }
  return -1;
}

async function finishCleaningFile() {
  const next = nextIncompleteIndex(state.index + 1);
  if (next >= 0 && next !== state.index) {
    await loadVideo(next);
    setStatus("Moved to next unfinished video", "ok");
    return;
  }
  setStatus("All videos complete", "ok");
}

async function deleteCurrentFile() {
  const video = currentVideo();
  if (!video || state.busy) return;
  const confirmed = window.confirm(
    `Move ${video.name} to Trash?\n\nUse this for accidental or unusable footage. This also removes it from the active batch.`,
  );
  if (!confirmed) return;

  state.busy = true;
  el.deleteFileBtn.disabled = true;
  el.player.pause();
  setTaskSelectionMode(false);
  setStatus(`Moving ${video.name} to Trash…`);
  try {
    const data = await api("/api/eager/video/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: video.path,
        batch_id: state.batchId,
        confirmed: true,
      }),
    });

    const deletedIndex = state.index;
    delete state.annotationsByPath[video.path];
    delete state.anchorByPath[video.path];
    delete state.clipsByPath[video.path];
    state.donePaths.delete(video.path);
    state.videos.splice(deletedIndex, 1);
    state.index = -1;
    if (data.batch) {
      state.batchDetail = data.batch;
      renderBatchStatus();
    }

    if (state.videos.length) {
      await loadVideo(Math.min(deletedIndex, state.videos.length - 1));
    } else {
      el.player.removeAttribute("src");
      el.player.load();
      el.currentName.textContent = "No file loaded";
      renderFileList();
      renderClips();
    }
    setStatus(`${video.name} moved to Trash`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.deleteFileBtn.disabled = false;
  }
}

async function markWork() {
  const video = currentVideo();
  if (!video) return;
  const ann = currentAnnotation() || await loadAnnotationForPath(video.path);
  const anchor = state.anchorByPath[video.path] ?? computeAnchor(ann?.segments || []);
  const end = normalizeMarkEnd(currentScrubTime());
  if (end <= anchor + 0.05) {
    setStatus(`Playhead must be after ${formatTime(anchor)} to mark work`, "error");
    return;
  }
  state.annotationsByPath[video.path] = {
    ...(ann || { segments: [], duration: null, complete: false }),
    pendingWork: { start: anchor, end },
  };
  el.player.pause();
  setTaskSelectionMode(true);
  const last = state.lastLabelTask;
  setStatus(
    last
      ? `Pending work ${formatTime(anchor)} → ${formatTime(end)} — Enter for ${last}, or pick from recent`
      : `Pending work ${formatTime(anchor)} → ${formatTime(end)} — choose a task and press Enter`,
    "ok",
  );
  renderClips();
  renderMarkTints();
  focusTaskSearch();
}

async function assignPendingWork() {
  const video = currentVideo();
  if (!video) return;
  const ann = currentAnnotation() || await loadAnnotationForPath(video.path);
  const pending = ann?.pendingWork;
  if (!pending) {
    setStatus("No pending work — press T to mark", "error");
    return;
  }

  let task = selectedTask();
  if (!task) {
    setStatus("Choose a task first", "error");
    focusTaskSearch();
    return;
  }
  if (!state.tasks.some((item) => item.toLowerCase() === task.toLowerCase())) {
    const data = await api("/api/eager/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: task,
        label_root: state.labelRoot || state.scanRoot || scanTargetPath(),
      }),
    });
    state.tasks = data.tasks || [];
  }

  await api("/api/eager/annotations/append", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: video.path,
      kind: "work",
      end: pending.end,
      task,
      ...annotationContext(),
    }),
  });
  state.lastLabelTask = task;
  touchRecentTask(task);
  applyAnnotationPayload(video.path, { annotation: { ...(ann || {}), pendingWork: null }, summary: ann?.summary || {} });
  await loadAnnotationForPath(video.path);
  setTaskSelectionMode(false);
  leaveTaskSearch({ clear: true });
  setStatus(`Assigned work to ${task}`, "ok");
  renderTasks(task);
  renderClips();
  renderMarkTints();
  renderFileList();
  if (state.annotationsByPath[video.path]?.complete) await finishCleaningFile();
}

async function markGarbage() {
  const video = currentVideo();
  if (!video) return;
  const ann = currentAnnotation() || await loadAnnotationForPath(video.path);
  if (ann?.pendingWork) {
    setStatus("Assign task first", "error");
    return;
  }
  const anchor = state.anchorByPath[video.path] ?? computeAnchor(ann?.segments || []);
  const end = normalizeMarkEnd(currentScrubTime());
  if (end <= anchor + 0.05) {
    setStatus(`Playhead must be after ${formatTime(anchor)} to mark garbage`, "error");
    return;
  }
  await api("/api/eager/annotations/append", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: video.path,
      kind: "garbage",
      end,
      ...annotationContext(),
    }),
  });
  await loadAnnotationForPath(video.path);
  setStatus(`Marked garbage ${formatTime(anchor)} → ${formatTime(end)}`, "ok");
  renderClips();
  renderMarkTints();
  renderFileList();
  if (state.annotationsByPath[video.path]?.complete) await finishCleaningFile();
}

async function undoSegment() {
  const video = currentVideo();
  if (!video) return;
  const ann = currentAnnotation();
  if (ann?.pendingWork) {
    ann.pendingWork = null;
    setTaskSelectionMode(false);
    leaveTaskSearch({ clear: true });
    setStatus("Cleared pending work", "ok");
    renderClips();
    renderMarkTints();
    return;
  }
  try {
    await api("/api/eager/annotations/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: video.path }),
    });
  } catch (error) {
    setStatus(error.message, "error");
    return;
  }
  await loadAnnotationForPath(video.path);
  setTaskSelectionMode(false);
  setStatus("Deleted last markup — choose a new timestamp", "ok");
  renderClips();
  renderMarkTints();
  renderFileList();
}

async function labelCurrentClip() {
  await assignPendingWork();
}

function advanceToNext() {
  let next = state.index + 1;
  while (next < state.videos.length && isHandledPath(state.videos[next].path)) {
    next += 1;
  }
  if (next < state.videos.length) {
    loadVideo(next);
    return;
  }
  if (state.videos.some((v) => !isHandledPath(v.path))) {
    const first = state.videos.findIndex((v) => !isHandledPath(v.path));
    if (first >= 0) {
      loadVideo(first);
      return;
    }
  }
  const remaining = state.videos.filter((v) => !isHandledPath(v.path)).length;
  setStatus(
    remaining === 0 ? "All files complete" : `${remaining} file(s) left`,
    remaining === 0 ? "ok" : "",
  );
  renderFileList();
}

el.fineBackBtn?.addEventListener("click", () => fineTune(-3));
el.fineFwdBtn?.addEventListener("click", () => fineTune(3));
el.markStartBtn?.addEventListener("click", markStart);
el.markEndBtn?.addEventListener("click", markEnd);

el.scrubTrack.addEventListener("mousedown", (event) => {
  if (!currentVideo()) return;
  event.stopPropagation();
  const rect = el.scrubTrack.getBoundingClientRect();
  seekToFraction((event.clientX - rect.left) / rect.width);
});

el.browseFolderBtn?.addEventListener("click", chooseFootageFolder);
el.refreshSdBtn?.addEventListener("click", () => refreshSdCards({ autoScan: true }));
el.sdCardSelect?.addEventListener("change", onSdCardChanged);
el.scanBtn?.addEventListener("click", scanSource);
el.undoClipBtn?.addEventListener("click", undoMark);
el.trimBtn?.addEventListener("click", trimMarkedClip);
el.deleteFileBtn?.addEventListener("click", deleteCurrentFile);
el.markWorkBtn?.addEventListener("click", markWork);
el.markGarbageBtn?.addEventListener("click", markGarbage);
el.undoSegmentBtn?.addEventListener("click", undoSegment);
el.nextCleanBtn.addEventListener("click", finishCleaningFile);
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
    if (currentPendingWork()) {
      labelCurrentClip();
    } else {
      leaveTaskSearch({ clear: false });
    }
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
  el.player.addEventListener("loadedmetadata", () => {
    updateScrubUi();
  });

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select")) return;
  const key = event.key.toLowerCase();

  if (event.key === "ArrowLeft") {
    event.preventDefault();
    bumpPlaybackRate(-PLAYBACK_RATE_STEP);
    return;
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    bumpPlaybackRate(PLAYBACK_RATE_STEP);
    return;
  }
  if (event.key === "[" || event.key === "{") {
    event.preventDefault();
    bumpPlaybackRate(-PLAYBACK_RATE_STEP);
    return;
  }
  if (event.key === "]" || event.key === "}") {
    event.preventDefault();
    bumpPlaybackRate(PLAYBACK_RATE_STEP);
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

  if (key === "t") {
    event.preventDefault();
    markWork();
    return;
  }
  if (key === "g") {
    event.preventDefault();
    markGarbage();
    return;
  }
  if (key === "u") {
    event.preventDefault();
    undoSegment();
    return;
  }
  if (key === "home" || event.key === "Home") {
    event.preventDefault();
    jumpToClipStart();
    return;
  }
  if (key === "n") {
    event.preventDefault();
    finishCleaningFile();
    return;
  }
  if (key === " ") {
    event.preventDefault();
    setPlaybackRate(1, { announce: false });
    if (el.player.paused) {
      el.player.play();
      setStatus("Playing at 1.0×", "ok");
    } else {
      el.player.pause();
      setStatus("Paused", "ok");
    }
    return;
  }
  if (key === "s") {
    event.preventDefault();
    focusTaskSearch();
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    labelCurrentClip();
  }
});

async function runSelfUpdate() {
  if (
    !window.confirm(
      "Update to the latest version?\n\nThe app restarts itself — this takes a minute or two. Finish any trims in progress first.",
    )
  ) {
    return;
  }
  el.updateBtn.disabled = true;
  let oldVersion = "";
  try {
    const health = await api("/api/health");
    oldVersion = health.version || "";
  } catch {
    /* still try to update */
  }
  showLoading("Updating", "Pulling the latest version from GitHub…", 10);
  try {
    const data = await api("/api/update", { method: "POST" });
    const branch = data.branch || "current branch";
    if (data.changed === false) {
      showLoading(
        "Restarting",
        `${branch} is already up to date — restarting anyway…`,
        40,
      );
    } else {
      showLoading(
        "Restarting",
        `Updated ${branch}: ${data.before} → ${data.after} — restarting…`,
        40,
      );
    }
  } catch (error) {
    hideLoading();
    el.updateBtn.disabled = false;
    setStatus(error.message, "error");
    return;
  }

  // Server is going down; poll health until the new process is up, then reload.
  let sawDown = false;
  const startedAt = Date.now();
  const timer = setInterval(async () => {
    const elapsed = Date.now() - startedAt;
    if (elapsed > 5 * 60 * 1000) {
      clearInterval(timer);
      hideLoading();
      setStatus("Update is taking long — check the server window, then reload this page", "error");
      el.updateBtn.disabled = false;
      return;
    }
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      if (!res.ok) throw new Error("down");
      const health = await res.json();
      const cameBack = sawDown || (health.version && oldVersion && health.version !== oldVersion);
      if (cameBack) {
        clearInterval(timer);
        showLoading("Ready", `Now on v${health.version} — reloading…`, 100);
        setTimeout(() => window.location.reload(), 600);
      } else {
        showLoading("Restarting", "Installing dependencies and starting up…", 60);
      }
    } catch {
      sawDown = true;
      showLoading("Restarting", "Server is restarting — this page reloads automatically…", 70);
    }
  }, 2000);
}

el.importBatchBtn?.addEventListener("click", async () => {
  const csv = el.batchCsvInput?.value || "";
  if (!csv.trim()) {
    setStatus("Paste batch CSV first", "error");
    return;
  }
  try {
    const data = await api("/api/eager/batches/import-csv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv }),
    });
    state.batchDetail = data.batch || null;
    state.batchId = state.batchDetail?.id || null;
    renderBatchUi();
    setIdentityFromSelectedCard();
    setStatus("Batch imported", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.refreshBatchBtn?.addEventListener("click", async () => {
  try {
    if (!state.batchId) {
      setStatus("No active batch", "error");
      return;
    }
    await loadBatchDetail(state.batchId);
    setIdentityFromSelectedCard();
    setStatus("Batch refreshed", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.finishCardBtn?.addEventListener("click", async () => {
  const badge = state.cardIdentity.card_badge || selectedSdCardLabel();
  if (!state.batchId || !badge) {
    setStatus("Select a batch card first", "error");
    return;
  }
  try {
    const data = await api(`/api/eager/batches/${encodeURIComponent(state.batchId)}/finish-card`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_badge: badge }),
    });
    state.batchDetail = data.batch || state.batchDetail;
    renderBatchUi();
    setStatus(`Card ${badge} finished`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.completeBatchBtn?.addEventListener("click", async () => {
  if (!state.batchId) {
    setStatus("No active batch", "error");
    return;
  }
  try {
    const data = await api(`/api/eager/batches/${encodeURIComponent(state.batchId)}/complete`, {
      method: "POST",
    });
    state.batchDetail = data.batch || state.batchDetail;
    renderBatchUi();
    setStatus("Batch complete", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.updateBtn?.addEventListener("click", runSelfUpdate);

state.recentTasks = loadRecentTasks();

loadTasks()
  .then(() => {
    ensureWorkspaces();
    renderCardTabs();
    setPhase("clean");
    return Promise.all([
      api("/api/health"),
      api("/api/eager/config"),
      api("/api/eager/batches"),
      refreshSdCards({ quiet: true, autoScan: true }),
    ]);
  })
  .then(([health, perf, batches]) => {
    if (el.appVersion) el.appVersion.textContent = `v${health.version || "?"}`;
    state.perf = { ...state.perf, ...perf };
    const open = (batches?.batches || []).find((b) => b.status !== "complete") || null;
    if (open?.id) {
      state.batchId = open.id;
      return loadBatchDetail(open.id).then(() => {
        setIdentityFromSelectedCard();
        startGlobalTrimPolling();
        setStatus("Batch workflow ready — insert matching card and annotate contiguously", "ok");
        if (health.ffmpeg_ok === false) {
          setStatus(health.ffmpeg_hint || "FFmpeg missing — install and restart", "error");
        }
      });
    }
    renderBatchUi();
    startGlobalTrimPolling();
    setStatus("No active batch — import CSV to start batch workflow", "ok");
    if (health.ffmpeg_ok === false) {
      setStatus(health.ffmpeg_hint || "FFmpeg missing — install and restart", "error");
    }
    return null;
  })
  .catch((error) => setStatus(error.message, "error"));
