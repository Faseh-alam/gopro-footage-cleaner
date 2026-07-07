const state = {
  phase: "clean",
  videos: [],
  index: -1,
  tasks: [],
  scanRoot: "",
  labelRoot: "",
  pendingIn: null,
  pendingClip: null,
  savedClips: [],
  trimPollTimer: null,
  trimEtaTotal: 0,
  currentHasGpmf: null,
  donePaths: new Set(),
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
};

const el = {
  phaseClean: document.getElementById("phase-clean"),
  phaseLabel: document.getElementById("phase-label"),
  sourcePath: document.getElementById("source-path"),
  browseFolderBtn: document.getElementById("browse-folder-btn"),
  cameraSelect: document.getElementById("camera-select"),
  refreshCamerasBtn: document.getElementById("refresh-cameras-btn"),
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
  clipList: document.getElementById("clip-list"),
  cleanPanel: document.getElementById("clean-panel"),
  labelPanel: document.getElementById("label-panel"),
  trimBtn: document.getElementById("trim-btn"),
  nextCleanBtn: document.getElementById("next-clean-btn"),
  taskSearch: document.getElementById("task-search"),
  taskList: document.getElementById("task-list"),
  taskSelect: document.getElementById("task-select"),
  newTaskInput: document.getElementById("new-task-input"),
  addTaskBtn: document.getElementById("add-task-btn"),
  taskAddedMsg: document.getElementById("task-added-msg"),
  labelBtn: document.getElementById("label-btn"),
  statusLine: document.getElementById("status-line"),
  footerHints: document.getElementById("footer-hints"),
  appVersion: document.getElementById("app-version"),
};

el.player.muted = true;
el.player.pause();

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
  const camera = el.cameraSelect.value.trim();
  if (camera) return camera;
  return el.sourcePath.value.trim();
}

function filteredVideos() {
  const q = el.fileFilter.value.trim().toLowerCase();
  if (!q) return state.videos;
  return state.videos.filter((video) => video.name.toLowerCase().includes(q));
}

function currentVideo() {
  return state.index >= 0 ? state.videos[state.index] : null;
}

function selectedTask() {
  const picked = el.taskSelect.value.trim();
  if (picked) return picked;
  return el.newTaskInput.value.trim();
}

function snapshotQuery(video, { priority } = {}) {
  const purpose = state.snapshotPurpose || "clean";
  let q = `path=${encodeURIComponent(video.path)}&purpose=${purpose}`;
  if (priority) q += `&priority=${encodeURIComponent(priority)}`;
  return q;
}

function scrubStepSeconds() {
  return state.phase === "label" ? 3 : 1;
}

function setPhase(phase) {
  state.phase = phase;
  state.snapshotPurpose = phase === "clean" ? "clean" : "label";
  el.phaseClean.classList.toggle("active", phase === "clean");
  el.phaseLabel.classList.toggle("active", phase === "label");
  el.cleanPanel.classList.toggle("hidden", phase !== "clean");
  el.labelPanel.classList.toggle("hidden", phase !== "label");
  el.listTitle.textContent = phase === "clean" ? "Raw footage" : "Footage to label";
  el.scanBtn.textContent = phase === "clean" ? "Scan raw footage" : "Scan footage";
  if (el.markSection) el.markSection.classList.toggle("hidden", phase !== "clean");
  if (el.clipList) el.clipList.classList.toggle("hidden", phase !== "clean");
  if (el.trimProgressPanel && phase !== "clean") el.trimProgressPanel.classList.add("hidden");
  state.snapshots = null;
  state.snapshotIndex = 0;
  state.videos = [];
  state.index = -1;
  state.donePaths = new Set();
  renderFileList();
  el.currentName.textContent = "No file loaded";
  el.currentMeta.textContent = "";
  updateContextHint();
}

function renderFileList() {
  const items = filteredVideos();
  el.fileList.innerHTML = "";
  el.listSummary.textContent = `${state.videos.length} files · ${state.donePaths.size} done`;

  for (const video of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "file-item";
    if (state.videos[state.index]?.path === video.path) btn.classList.add("active");
    if (state.donePaths.has(video.path)) btn.classList.add("done");
    const typeHint =
      state.phase === "label" ? `${video.is_trimmed ? "clip" : "whole"} · ` : "";
    btn.innerHTML = `<span class="name">${video.name}</span><span class="meta">${typeHint}${video.duration_label || "?"} · ${formatBytes(video.size_bytes)}</span>`;
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

function updateTrimEtaPanel(data) {
  state.trimEtaTotal = data?.eta_total_seconds || 0;
  const active = activeTrimCount();
  if (!el.trimProgressPanel) return;

  if (active === 0) {
    el.trimProgressPanel.classList.add("hidden");
    if (el.trimProgressFill) el.trimProgressFill.style.width = "0%";
    return;
  }

  el.trimProgressPanel.classList.remove("hidden");
  if (el.trimActiveCount) el.trimActiveCount.textContent = String(active);
  if (el.trimEtaTotal) {
    el.trimEtaTotal.textContent = `~${formatDurationShort(state.trimEtaTotal)}`;
  }

  const jobs = state.savedClips.filter((j) => j.status === "queued" || j.status === "running");
  const totalDuration = jobs.reduce((sum, j) => sum + (j.duration_seconds || j.end - j.start || 0), 0);
  const doneDuration = jobs.reduce((sum, j) => {
    const dur = j.duration_seconds || j.end - j.start || 0;
    if (j.status === "running") return sum + dur * (j.progress || 0) / 100;
    return sum;
  }, 0);
  const overallPct = totalDuration > 0 ? (doneDuration / totalDuration) * 100 : 0;
  if (el.trimProgressFill) {
    el.trimProgressFill.style.width = `${Math.min(100, overallPct)}%`;
  }
}

function syncTrimJobsFromServer(jobs) {
  const byId = new Map(state.savedClips.map((j) => [j.job_id, j]));
  for (const job of jobs || []) {
    const existing = byId.get(job.job_id);
    if (existing) {
      Object.assign(existing, job);
      if (job.output) existing.name = job.output.split(/[/\\]/).pop();
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
        name: job.output ? job.output.split(/[/\\]/).pop() : null,
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
    updateTrimEtaPanel(data);
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
  state.trimPollTimer = setInterval(pollTrimStatus, 800);
}

function stopTrimPolling() {
  if (state.trimPollTimer) {
    clearInterval(state.trimPollTimer);
    state.trimPollTimer = null;
  }
}

function activeTrimCount() {
  return state.savedClips.filter((j) => j.status === "queued" || j.status === "running").length;
}

function updateContextHint() {
  if (!el.contextMessage) return;

  if (state.phase === "label") {
    if (!currentVideo()) {
      el.contextMessage.textContent = "Choose folder and scan — use ← → filmstrip to identify task";
    } else if (!selectedTask()) {
      el.contextMessage.textContent = "Use , . ±3s to scrub · ← → for opening previews";
    } else {
      el.contextMessage.textContent = `Press N to move to "${selectedTask()}"`;
    }
    return;
  }

  if (!currentVideo()) {
    el.contextMessage.textContent = "Choose a footage folder, then scan";
    return;
  }

  if (state.pendingClip) {
    el.contextMessage.textContent = "Press T to queue trim, then mark the next useful section";
    return;
  }

  if (activeTrimCount() > 0) {
    const eta = state.trimEtaTotal > 0 ? ` (~${formatDurationShort(state.trimEtaTotal)} left)` : "";
    el.contextMessage.textContent = `Trims running in background${eta} — press N when done to move on`;
    return;
  }

  if (state.pendingIn !== null) {
    el.contextMessage.textContent = "Find the end of useful footage, then press O";
    return;
  }

  if (state.savedClips.some((j) => j.status === "completed" || j.output)) {
    el.contextMessage.textContent = "More useful parts? Keep marking. Otherwise press N";
    return;
  }

  el.contextMessage.textContent = "Garbage? I → O → T for each part. All useful? Press N";
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
  el.filmstripMeta.textContent =
    state.snapshotPurpose === "label"
      ? `Opening preview ${idx}/${total} · use , . ±3s to scrub`
      : `Every ${m.interval_seconds}s · snapshot ${idx}/${total} · ${m.garbage_hint || ""}`;
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

async function waitForSnapshots(video, token) {
  const isLabel = state.snapshotPurpose === "label";
  await api(`/api/eager/snapshots/status?${snapshotQuery(video, { priority: "foreground" })}&start=1`);
  let uiOpen = false;
  const maxPolls = isLabel ? 120 : 3600;
  for (let i = 0; i < maxPolls; i += 1) {
    if (token !== state.snapshotBuildToken) return null;
    const status = await api(`/api/eager/snapshots/status?${snapshotQuery(video)}`);
    const partial = status.manifest;
    const frameCount = partial?.frames?.length || 0;
    const minFrames = isLabel ? 1 : 3;

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

    if (status.status === "running" && !uiOpen && !isLabel) {
      showLoading(
        "Building snapshots",
        `${video.name} — ${frameCount}/${status.plan?.snapshot_count || "?"} frames`,
        status.progress || Math.min(95, Math.round((frameCount / (status.plan?.snapshot_count || 1)) * 100)),
        status.plan?.garbage_hint || "",
      );
    }
    if (status.status === "queued" && !uiOpen && !isLabel) {
      showLoading("Queued for snapshots", video.name, 2, "Waiting for other files to finish…");
    }

    if (status.status === "ready" && status.manifest) {
      hideLoading();
      return status.manifest;
    }
    if (status.status === "error") {
      throw new Error(status.error || "Snapshot build failed");
    }
    await new Promise((r) => setTimeout(r, uiOpen ? 250 : 150));
  }
  throw new Error("Snapshot build timed out");
}

async function ensureSnapshots(video, showOverlay = true) {
  const isLabel = state.snapshotPurpose === "label";
  const token = ++state.snapshotBuildToken;
  if (showOverlay && !isLabel) {
    showLoading("Checking snapshots", video.name, 5);
  } else if (isLabel) {
    setStatus(`Loading opening preview for ${video.name}...`);
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
  const manifest = await waitForSnapshots(video, token);
  if (token !== state.snapshotBuildToken || !manifest) return null;
  state.snapshots = manifest;
  renderFilmstrip();
  hideLoading();
  return manifest;
}

function continueSnapshotRefresh(video) {
  if (state.snapshotPurpose === "label") return;
  const token = state.snapshotBuildToken;
  (async () => {
    for (let i = 0; i < 600; i += 1) {
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
        return;
      }
      if (status.status === "error") return;
      await new Promise((r) => setTimeout(r, 500));
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
  const q = el.taskSearch.value.trim().toLowerCase();
  const selected = preferred || el.taskSelect.value;
  el.taskList.innerHTML = "";
  el.taskSelect.innerHTML = "";

  if (!state.tasks.length) {
    el.taskList.innerHTML = '<div class="hint">No tasks yet — add one below.</div>';
    return;
  }

  for (const task of state.tasks) {
    if (q && !task.toLowerCase().includes(q)) continue;
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
      renderTasks(task);
    });
    el.taskList.appendChild(btn);
  }

  if (selected && [...el.taskSelect.options].some((opt) => opt.value === selected)) {
    el.taskSelect.value = selected;
  } else if (el.taskSelect.options.length) {
    el.taskSelect.selectedIndex = 0;
  }
  updateContextHint();
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
      try {
        await ensureSnapshots(video, true);
        if (state.snapshots?.frames?.length) {
          goToSnapshotIndex(0);
        }
        continueSnapshotRefresh(video);
        if (state.phase === "clean") {
          startTrimPolling();
        }
        const readyMsg =
          state.phase === "label"
            ? `Ready — , . ±3s to scrub (${video.name})`
            : `Ready — use snapshot strip (${video.name})`;
        setStatus(readyMsg, "ok");
        updateContextHint();
        if (state.phase === "clean") {
          prefetchSnapshotsBackground(state.index + 1);
        }
      } catch (error) {
        hideLoading();
        setStatus(error.message, "error");
      }
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
    const initial = el.sourcePath.value.trim();
    const query = initial ? `?initial=${encodeURIComponent(initial)}` : "";
    const data = await api(`/api/eager/pick-folder${query}`, { method: "POST" });
    if (data.cancelled) {
      setStatus("Folder selection cancelled");
      return;
    }
    el.sourcePath.value = data.path;
    state.scanRoot = data.path;
    state.labelRoot = data.path;
    await loadCameras();
    setStatus(`Selected ${data.path}`, "ok");
    updateContextHint();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.browseFolderBtn.disabled = false;
  }
}

async function loadCameras() {
  const path = el.sourcePath.value.trim();
  if (!path) return;
  try {
    const data = await api(`/api/eager/cameras?path=${encodeURIComponent(path)}`);
    const selected = el.cameraSelect.value;
    el.cameraSelect.innerHTML = '<option value="">SD card root (MP4s on drive)</option>';
    for (const camera of data.cameras || []) {
      const option = document.createElement("option");
      option.value = camera.path;
      option.textContent = `${camera.name} (${camera.raw_count} raw · ${camera.clip_count} clips)`;
      el.cameraSelect.appendChild(option);
    }
    if (selected && [...el.cameraSelect.options].some((opt) => opt.value === selected)) {
      el.cameraSelect.value = selected;
    }
  } catch {
    el.cameraSelect.innerHTML = '<option value="">SD card root (MP4s on drive)</option>';
  }
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

async function scanSource() {
  const path = scanTargetPath();
  if (!path) {
    setStatus("Choose a footage folder first", "error");
    return;
  }

  state.scanRoot = path;
  state.labelRoot = path;
  const mode = state.phase === "clean" ? "raw" : "label";

  setStatus("Scanning...");
  el.scanBtn.disabled = true;
  try {
    const data = await api("/api/eager/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive: true, mode }),
    });
    state.videos = data.videos || [];
    state.donePaths = new Set();
    state.index = -1;
    renderFileList();
    const summaryLabel =
      mode === "raw" ? "raw files" : mode === "label" ? "files to label" : "files";
    el.scanSummary.textContent = `${data.count} ${summaryLabel}`;
    if (state.videos.length) {
      if (mode === "raw") {
        showLoading("Loading folder", `Found ${state.videos.length} files`, 10);
      }
      await loadVideo(0);
      if (mode === "raw") hideLoading();
      setStatus(`Found ${state.videos.length} files`, "ok");
      if (mode === "raw") prefetchSnapshotsBackground(1);
    } else {
      setStatus(mode === "label" ? "No footage to label found" : `No ${mode} MP4 files found`, "error");
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.scanBtn.disabled = false;
  }
}

const PREFETCH_AHEAD = 3;

function prefetchSnapshotsBackground(fromIndex) {
  if (state.phase !== "clean") return;
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

  const hasClips = state.savedClips.some(
    (j) => j.status === "completed" || j.output || j.status === "queued" || j.status === "running",
  );

  if (!hasClips) {
    setStatus("Raw file kept — next file", "ok");
    state.donePaths.add(video.path);
    stopTrimPolling();
    advanceToNext();
    return;
  }

  try {
    const data = await api("/api/eager/clean", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: video.path }),
    });
    state.donePaths.add(video.path);
    stopTrimPolling();
    if (data.scheduled) {
      setStatus(`Next file — ${data.active} trim(s) still finishing, raw will be removed when done`, "ok");
    } else if (data.deleted_source) {
      setStatus(`Finished ${video.name} — raw file removed`, "ok");
    } else {
      setStatus(`Finished ${video.name}`, "ok");
    }
    advanceToNext();
    if (state.phase === "clean") prefetchSnapshotsBackground(state.index + 1);
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function labelCurrentClip() {
  if (state.busy) return;
  const video = currentVideo();
  if (!video) return;

  let task = selectedTask();
  if (!task) {
    setStatus("Choose or add a task first", "error");
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
    state.donePaths.add(video.path);
    setStatus(`Moved to ${data.task_dir}`, "ok");
    advanceToNext();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.labelBtn.disabled = false;
  }
}

function advanceToNext() {
  let next = state.index + 1;
  while (next < state.videos.length && state.donePaths.has(state.videos[next].path)) {
    next += 1;
  }
  if (next < state.videos.length) {
    loadVideo(next);
  } else {
    setStatus(state.phase === "clean" ? "All files cleaned" : "All clips labeled", "ok");
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
el.refreshCamerasBtn.addEventListener("click", loadCameras);
el.cameraSelect.addEventListener("change", () => {
  if (el.cameraSelect.value) setStatus(`Camera folder: ${el.cameraSelect.value}`);
});
el.scanBtn.addEventListener("click", scanSource);
el.fileFilter.addEventListener("input", renderFileList);
el.undoClipBtn.addEventListener("click", undoMark);
el.trimBtn.addEventListener("click", trimMarkedClip);
el.nextCleanBtn.addEventListener("click", finishCleaningFile);
el.phaseClean.addEventListener("click", () => setPhase("clean"));
el.phaseLabel.addEventListener("click", () => setPhase("label"));
el.taskSearch.addEventListener("input", () => renderTasks());
el.addTaskBtn.addEventListener("click", addTask);
el.newTaskInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addTask();
  }
});
el.labelBtn.addEventListener("click", labelCurrentClip);
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

  if (state.phase === "clean" || state.phase === "label") {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (state.phase === "label" && !state.snapshots?.frames?.length) {
        fineTune(-scrubStepSeconds());
      } else {
        goToSnapshot(-1);
      }
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      if (state.phase === "label" && !state.snapshots?.frames?.length) {
        fineTune(scrubStepSeconds());
      } else {
        goToSnapshot(1);
      }
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
  }

  if (state.phase === "clean") {
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
    if (key === " ") {
      event.preventDefault();
      if (el.player.paused) el.player.play();
      else el.player.pause();
    }
  }

  if (state.phase === "label") {
    if (key === "n" || key === "enter") {
      event.preventDefault();
      labelCurrentClip();
    }
  }
});

loadTasks()
  .then(() => api("/api/health"))
  .then((data) => {
    if (el.appVersion) el.appVersion.textContent = `v${data.version || "?"}`;
  })
  .catch((error) => setStatus(error.message, "error"));
