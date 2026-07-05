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
  donePaths: new Set(),
  busy: false,
  scrubMode: false,
  previewToken: 0,
  lastScrubX: null,
  scrubRaf: null,
};

const el = {
  phaseClean: document.getElementById("phase-clean"),
  phaseLabel: document.getElementById("phase-label"),
  sourceVolume: document.getElementById("source-volume"),
  sourcePath: document.getElementById("source-path"),
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
  scrubHint: document.getElementById("scrub-hint"),
  previewStatus: document.getElementById("preview-status"),
  currentName: document.getElementById("current-name"),
  currentMeta: document.getElementById("current-meta"),
  timeDisplay: document.getElementById("time-display"),
  undoClipBtn: document.getElementById("undo-clip-btn"),
  pendingIn: document.getElementById("pending-in"),
  clipList: document.getElementById("clip-list"),
  cleanPanel: document.getElementById("clean-panel"),
  labelPanel: document.getElementById("label-panel"),
  deleteSource: document.getElementById("delete-source"),
  trimBtn: document.getElementById("trim-btn"),
  nextCleanBtn: document.getElementById("next-clean-btn"),
  keepWholeBtn: document.getElementById("keep-whole-btn"),
  skipBtn: document.getElementById("skip-btn"),
  taskSearch: document.getElementById("task-search"),
  taskList: document.getElementById("task-list"),
  taskSelect: document.getElementById("task-select"),
  newTaskInput: document.getElementById("new-task-input"),
  addTaskBtn: document.getElementById("add-task-btn"),
  taskAddedMsg: document.getElementById("task-added-msg"),
  labelBtn: document.getElementById("label-btn"),
  skipLabelBtn: document.getElementById("skip-label-btn"),
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

function setPhase(phase) {
  state.phase = phase;
  el.phaseClean.classList.toggle("active", phase === "clean");
  el.phaseLabel.classList.toggle("active", phase === "label");
  el.cleanPanel.classList.toggle("hidden", phase !== "clean");
  el.labelPanel.classList.toggle("hidden", phase !== "label");
  el.listTitle.textContent = phase === "clean" ? "Raw footage" : "Trimmed clips";
  el.scanBtn.textContent = phase === "clean" ? "Scan raw footage" : "Scan trimmed clips";
  exitScrubMode();
  state.videos = [];
  state.index = -1;
  state.donePaths = new Set();
  renderFileList();
  el.currentName.textContent = "No file loaded";
  el.currentMeta.textContent = "";
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
    btn.innerHTML = `<span class="name">${video.name}</span><span class="meta">${video.duration_label || "?"} · ${formatBytes(video.size_bytes)}</span>`;
    btn.addEventListener("click", () => {
      const idx = state.videos.findIndex((item) => item.path === video.path);
      if (idx >= 0) loadVideo(idx);
    });
    el.fileList.appendChild(btn);
  }
}

function renderClips() {
  el.clipList.innerHTML = "";
  for (const clip of state.savedClips) {
    const item = document.createElement("li");
    item.className = "saved";
    item.textContent = `Saved: ${clip.name || clip.output || "clip"}`;
    el.clipList.appendChild(item);
  }
  if (state.pendingClip) {
    const item = document.createElement("li");
    item.className = "pending";
    item.textContent = `Marked: ${formatTime(state.pendingClip.start)} → ${formatTime(state.pendingClip.end)} — press T`;
    el.clipList.appendChild(item);
  }
  if (state.pendingIn !== null) {
    el.pendingIn.textContent = `Start at ${formatTime(state.pendingIn)} — scrub and click to set end`;
  } else if (state.pendingClip) {
    el.pendingIn.textContent = "Press T to trim this clip";
  } else if (state.scrubMode) {
    el.pendingIn.textContent = "Scrubbing — click to mark start";
  } else {
    el.pendingIn.textContent = "Click video to start scrubbing";
  }
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
}

function updateScrubUi() {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  const current = el.player.currentTime || 0;
  const pct = duration > 0 ? (current / duration) * 100 : 0;
  el.scrubFill.style.width = `${pct}%`;
  el.scrubPlayhead.style.left = `${pct}%`;
  el.timeDisplay.textContent = `${formatTime(current)} / ${formatTime(duration)}`;
}

function seekToFraction(fraction) {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;
  const clamped = Math.min(0.999, Math.max(0, fraction));
  el.player.pause();
  el.player.currentTime = clamped * duration;
  updateScrubUi();
}

function scrubByPosition(clientX) {
  const rect = el.playerWrap.getBoundingClientRect();
  const width = rect.width || 1;
  const rel = (clientX - rect.left) / width;
  const center = 0.5;
  const delta = rel - center;
  if (Math.abs(delta) < 0.06) return;

  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;

  const speed = delta * 2;
  const step = speed * Math.max(0.15, duration / 800);
  el.player.pause();
  el.player.currentTime = Math.min(duration - 0.04, Math.max(0, el.player.currentTime + step));
  updateScrubUi();
}

function scrubLoop() {
  if (!state.scrubMode || state.lastScrubX === null) {
    state.scrubRaf = null;
    return;
  }
  scrubByPosition(state.lastScrubX);
  state.scrubRaf = requestAnimationFrame(scrubLoop);
}

function enterScrubMode() {
  if (!currentVideo() || state.phase !== "clean") return;
  state.scrubMode = true;
  document.body.classList.add("scrub-cursor-hidden");
  el.playerWrap.classList.add("scrub-active");
  el.scrubHint.textContent = "Move mouse left/right · click mark · T trim · Esc exit";
  renderClips();
}

function exitScrubMode() {
  state.scrubMode = false;
  state.lastScrubX = null;
  document.body.classList.remove("scrub-cursor-hidden");
  el.playerWrap.classList.remove("scrub-active");
  if (state.scrubRaf) {
    cancelAnimationFrame(state.scrubRaf);
    state.scrubRaf = null;
  }
  renderClips();
}

function handleScrubClick() {
  if (!currentVideo() || state.phase !== "clean") return;
  const time = el.player.currentTime;

  if (state.pendingIn === null) {
    state.pendingIn = time;
    state.pendingClip = null;
  } else {
    const end = time;
    if (end <= state.pendingIn + 0.05) {
      setStatus("End must be after start", "error");
      return;
    }
    state.pendingClip = { start: state.pendingIn, end };
    state.pendingIn = null;
  }
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

async function upgradePreviewInBackground(video, token) {
  try {
    await api(`/api/eager/preview/status?path=${encodeURIComponent(video.path)}`);
    for (let attempt = 0; attempt < 600; attempt += 1) {
      if (token !== state.previewToken) return;
      const status = await api(`/api/eager/preview/status?path=${encodeURIComponent(video.path)}`);
      if (status.status === "running") {
        const pct = status.progress || 0;
        el.previewStatus.textContent =
          pct > 0 ? `Building preview: ${pct}%` : "Building preview in background...";
      }
      if (status.status === "ready") {
        if (token !== state.previewToken) return;
        const savedTime = el.player.currentTime || 0;
        el.player.src = `/api/eager/preview?path=${encodeURIComponent(video.path)}&t=${Date.now()}`;
        el.player.load();
        el.player.addEventListener(
          "loadedmetadata",
          () => {
            el.player.currentTime = Math.min(savedTime, el.player.duration || savedTime);
            updateScrubUi();
          },
          { once: true },
        );
        el.previewStatus.textContent = "Using preview for smoother scrubbing";
        return;
      }
      if (status.status === "error") {
        el.previewStatus.textContent = "";
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  } catch {
    el.previewStatus.textContent = "";
  }
}

async function loadVideo(index) {
  if (index < 0 || index >= state.videos.length) return;
  state.index = index;
  state.pendingIn = null;
  state.pendingClip = null;
  state.savedClips = [];
  exitScrubMode();

  const video = state.videos[index];
  const token = ++state.previewToken;

  el.currentName.textContent = video.name;
  el.currentMeta.textContent = `${video.relative || video.path} · ${video.duration_label || "?"}`;
  el.previewStatus.textContent = "";
  el.playerWrap.classList.add("loading");
  setStatus(`Loading ${video.name}...`);

  renderFileList();
  renderClips();

  el.player.src = `/api/eager/stream?path=${encodeURIComponent(video.path)}`;
  el.player.load();

  const onReady = () => {
    if (token !== state.previewToken) return;
    el.playerWrap.classList.remove("loading");
    el.player.pause();
    el.player.currentTime = 0;
    updateScrubUi();
    setStatus(`Ready — ${video.name}`, "ok");
    upgradePreviewInBackground(video, token);
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

async function loadVolumes() {
  const data = await api("/api/eager/volumes");
  el.sourceVolume.innerHTML = '<option value="">Choose drive...</option>';
  for (const volume of data.volumes) {
    const option = document.createElement("option");
    option.value = volume.path;
    option.textContent = volume.name;
    el.sourceVolume.appendChild(option);
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
      body: JSON.stringify({ name }),
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
    setStatus("Choose a folder first", "error");
    return;
  }

  state.scanRoot = path;
  state.labelRoot = path;
  const mode = state.phase === "clean" ? "raw" : "clips";

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
    el.scanSummary.textContent = `${data.count} ${mode === "raw" ? "raw" : "trimmed"} files`;
    if (state.videos.length) {
      await loadVideo(0);
      setStatus(`Found ${state.videos.length} files`, "ok");
    } else {
      setStatus(`No ${mode} MP4 files found`, "error");
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.scanBtn.disabled = false;
  }
}

async function trimMarkedClip() {
  if (state.busy) return;
  const video = currentVideo();
  if (!video || !state.pendingClip) {
    setStatus("Mark a clip first (click start, click end)", "error");
    return;
  }

  state.busy = true;
  el.trimBtn.disabled = true;
  setStatus("Trimming clip...");
  try {
    const data = await api("/api/eager/trim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: video.path,
        start: state.pendingClip.start,
        end: state.pendingClip.end,
      }),
    });
    state.savedClips.push({
      output: data.output,
      name: data.output.split(/[/\\]/).pop(),
      start: data.start_seconds,
      end: data.end_seconds,
    });
    state.pendingClip = null;
    renderClips();
    setStatus(`Saved ${data.output.split(/[/\\]/).pop()}`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.trimBtn.disabled = false;
  }
}

async function finishCleaningFile() {
  if (state.busy) return;
  const video = currentVideo();
  if (!video) return;

  if (state.pendingClip) {
    setStatus("Press T to trim the marked clip first", "error");
    return;
  }

  state.busy = true;
  el.nextCleanBtn.disabled = true;
  setStatus("Finishing file...");
  try {
    if (el.deleteSource.checked && state.savedClips.length > 0) {
      await api("/api/eager/clean", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: video.path, delete_source: true }),
      });
    } else if (el.deleteSource.checked) {
      setStatus("No clips saved — raw file kept", "ok");
    }
    state.donePaths.add(video.path);
    setStatus(`Finished ${video.name}`, "ok");
    advanceToNext();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.nextCleanBtn.disabled = false;
  }
}

async function keepWholeFile() {
  if (state.busy) return;
  const video = currentVideo();
  if (!video) return;

  const duration = el.player.duration || video.duration || 0;
  if (!duration) {
    setStatus("Wait for video to load", "error");
    return;
  }

  state.pendingClip = { start: 0, end: duration - 0.05 };
  await trimMarkedClip();
  await finishCleaningFile();
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
        body: JSON.stringify({ name: task }),
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
    exitScrubMode();
    setStatus(state.phase === "clean" ? "All files cleaned" : "All clips labeled", "ok");
    renderFileList();
  }
}

function skipCurrent() {
  const video = currentVideo();
  if (!video) return;
  state.donePaths.add(video.path);
  setStatus(`Skipped ${video.name}`);
  advanceToNext();
}

el.playerWrap.addEventListener("click", (event) => {
  if (!currentVideo()) return;
  if (event.target.closest("#scrub-track")) return;

  if (state.phase === "label") return;

  if (!state.scrubMode) {
    enterScrubMode();
    return;
  }

  handleScrubClick();
});

el.playerWrap.addEventListener("mousemove", (event) => {
  if (!state.scrubMode) return;
  state.lastScrubX = event.clientX;
  if (!state.scrubRaf) {
    state.scrubRaf = requestAnimationFrame(scrubLoop);
  }
});

el.playerWrap.addEventListener("mouseleave", () => {
  state.lastScrubX = null;
});

el.scrubTrack.addEventListener("mousedown", (event) => {
  if (!currentVideo()) return;
  event.stopPropagation();
  const rect = el.scrubTrack.getBoundingClientRect();
  seekToFraction((event.clientX - rect.left) / rect.width);
});

el.sourceVolume.addEventListener("change", () => {
  if (el.sourceVolume.value) {
    el.sourcePath.value = el.sourceVolume.value;
    loadCameras();
  }
});
el.sourcePath.addEventListener("change", loadCameras);
el.refreshCamerasBtn.addEventListener("click", loadCameras);
el.cameraSelect.addEventListener("change", () => {
  if (el.cameraSelect.value) setStatus(`Camera folder: ${el.cameraSelect.value}`);
});
el.scanBtn.addEventListener("click", scanSource);
el.fileFilter.addEventListener("input", renderFileList);
el.undoClipBtn.addEventListener("click", undoMark);
el.trimBtn.addEventListener("click", trimMarkedClip);
el.nextCleanBtn.addEventListener("click", finishCleaningFile);
el.keepWholeBtn.addEventListener("click", keepWholeFile);
el.skipBtn.addEventListener("click", skipCurrent);
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
el.skipLabelBtn.addEventListener("click", skipCurrent);
el.player.addEventListener("timeupdate", updateScrubUi);
el.player.addEventListener("loadedmetadata", updateScrubUi);
el.player.addEventListener("seeked", updateScrubUi);

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select")) return;
  const key = event.key.toLowerCase();

  if (key === "escape") {
    exitScrubMode();
    return;
  }

  if (state.phase === "clean") {
    if (key === "t") {
      event.preventDefault();
      trimMarkedClip();
    }
    if (key === "n") {
      event.preventDefault();
      finishCleaningFile();
    }
    if (key === "k") {
      event.preventDefault();
      keepWholeFile();
    }
    if (key === "s") skipCurrent();
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
    if (key === "s") skipCurrent();
  }
});

loadVolumes()
  .then(loadTasks)
  .then(() => api("/api/health"))
  .then((data) => {
    if (el.appVersion) el.appVersion.textContent = `v${data.version || "?"}`;
  })
  .catch((error) => setStatus(error.message, "error"));
