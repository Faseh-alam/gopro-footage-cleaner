const state = {
  videos: [],
  index: -1,
  tasks: [],
  clips: [],
  pendingIn: null,
  donePaths: new Set(),
  busy: false,
  scrubbing: false,
  previewToken: 0,
};

const el = {
  sourceVolume: document.getElementById("source-volume"),
  sourcePath: document.getElementById("source-path"),
  outputVolume: document.getElementById("output-volume"),
  outputPath: document.getElementById("output-path"),
  scanBtn: document.getElementById("scan-btn"),
  fileFilter: document.getElementById("file-filter"),
  fileList: document.getElementById("file-list"),
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
  markInBtn: document.getElementById("mark-in-btn"),
  markOutBtn: document.getElementById("mark-out-btn"),
  undoClipBtn: document.getElementById("undo-clip-btn"),
  pendingIn: document.getElementById("pending-in"),
  clipList: document.getElementById("clip-list"),
  taskSearch: document.getElementById("task-search"),
  taskList: document.getElementById("task-list"),
  taskSelect: document.getElementById("task-select"),
  newTaskInput: document.getElementById("new-task-input"),
  addTaskBtn: document.getElementById("add-task-btn"),
  taskAddedMsg: document.getElementById("task-added-msg"),
  deleteSource: document.getElementById("delete-source"),
  finishBtn: document.getElementById("finish-btn"),
  skipBtn: document.getElementById("skip-btn"),
  statusLine: document.getElementById("status-line"),
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

function qualityMode() {
  return document.querySelector('input[name="quality"]:checked')?.value || "keep";
}

function outputRoot() {
  return el.outputPath.value.trim();
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
  state.clips.forEach((clip, index) => {
    const item = document.createElement("li");
    item.textContent = `Clip ${index + 1}: ${formatTime(clip.start)} → ${formatTime(clip.end)}`;
    el.clipList.appendChild(item);
  });
  if (state.pendingIn === null) {
    el.pendingIn.textContent = "No start marked";
  } else {
    el.pendingIn.textContent = `Start marked at ${formatTime(state.pendingIn)} — now mark end`;
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

function scrubByDelta(deltaPx, width) {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration || !width) return;
  const secondsPerPixel = duration / width;
  const next = el.player.currentTime + deltaPx * secondsPerPixel;
  el.player.pause();
  el.player.currentTime = Math.min(duration - 0.04, Math.max(0, next));
  updateScrubUi();
}

function scrubByWheel(delta) {
  const duration = el.player.duration || currentVideo()?.duration || 0;
  if (!duration) return;
  const base = Math.max(0.08, duration / 1200);
  const step = Math.sign(delta) * Math.max(base, Math.abs(delta) * base * 0.35);
  el.player.pause();
  el.player.currentTime = Math.min(duration - 0.04, Math.max(0, el.player.currentTime + step));
  updateScrubUi();
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
          pct > 0 ? `Building 1080p preview: ${pct}%` : "Building 1080p preview in background...";
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
        el.previewStatus.textContent = "Using 1080p preview for smoother scrubbing";
        return;
      }
      if (status.status === "error") {
        el.previewStatus.textContent = "Preview skipped — using original file";
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
  state.clips = [];
  state.pendingIn = null;
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
    setStatus(`Ready — scroll on video to scrub (${video.name})`, "ok");
    el.playerWrap.focus();
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
  for (const select of [el.sourceVolume, el.outputVolume]) {
    select.innerHTML = '<option value="">Choose...</option>';
    for (const volume of data.volumes) {
      const option = document.createElement("option");
      option.value = volume.path;
      option.textContent = volume.name;
      select.appendChild(option);
    }
  }
}

function markIn() {
  if (!currentVideo()) return;
  state.pendingIn = el.player.currentTime;
  renderClips();
}

function markOut() {
  if (!currentVideo() || state.pendingIn === null) return;
  const end = el.player.currentTime;
  if (end <= state.pendingIn + 0.05) {
    setStatus("End must be after start", "error");
    return;
  }
  state.clips.push({ start: state.pendingIn, end });
  state.pendingIn = null;
  document.querySelector('input[name="quality"][value="trim"]').checked = true;
  renderClips();
}

function undoClip() {
  if (state.pendingIn !== null) {
    state.pendingIn = null;
  } else {
    state.clips.pop();
  }
  renderClips();
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
  el.taskAddedMsg.textContent = "";
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
    el.taskSelect.value = name;
    el.taskAddedMsg.textContent = `Added: ${name}`;
    setStatus(`Task added: ${name}`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.addTaskBtn.disabled = false;
  }
}

async function scanSource() {
  const path = el.sourcePath.value.trim();
  if (!path) {
    setStatus("Choose a source folder first", "error");
    return;
  }
  setStatus("Scanning for MP4 files...");
  el.scanBtn.disabled = true;
  try {
    const data = await api("/api/eager/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive: true }),
    });
    state.videos = data.videos || [];
    state.donePaths = new Set();
    state.index = -1;
    renderFileList();
    if (state.videos.length) {
      await loadVideo(0);
      setStatus(`Found ${state.videos.length} MP4 files`, "ok");
    } else {
      setStatus("No MP4 files found in that folder", "error");
    }
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.scanBtn.disabled = false;
  }
}

async function finishCurrent(advance = true) {
  if (state.busy) return;
  const video = currentVideo();
  if (!video) return;

  let task = selectedTask();
  const out = outputRoot();
  if (!task) {
    setStatus("Choose or add a task first", "error");
    return;
  }
  if (!out) {
    setStatus("Set an output folder first", "error");
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

  const keepEntire = qualityMode() === "keep";
  if (!keepEntire && !state.clips.length) {
    setStatus("Mark clips or switch to Keep entire file", "error");
    return;
  }

  state.busy = true;
  el.finishBtn.disabled = true;
  setStatus(keepEntire ? "Copying full-quality file..." : `Trimming ${state.clips.length} clip(s)...`);

  try {
    const payload = {
      path: video.path,
      output_root: out,
      task,
      keep_entire: keepEntire,
      delete_source: el.deleteSource.checked,
      clips: state.clips.map((clip) => [clip.start, clip.end]),
    };
    const data = await api("/api/eager/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.donePaths.add(video.path);
    setStatus(`Saved to ${data.task_dir}`, "ok");
    if (advance) advanceToNext();
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    state.busy = false;
    el.finishBtn.disabled = false;
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
    setStatus("All files reviewed", "ok");
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

function moveSelection(delta) {
  if (!state.videos.length) return;
  const next = Math.min(state.videos.length - 1, Math.max(0, state.index + delta));
  loadVideo(next);
}

let dragScrubbing = false;
let lastDragX = 0;

el.playerWrap.addEventListener(
  "wheel",
  (event) => {
    if (!currentVideo()) return;
    event.preventDefault();
    const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    scrubByWheel(delta);
  },
  { passive: false },
);

el.playerWrap.addEventListener("mousedown", (event) => {
  if (!currentVideo()) return;
  dragScrubbing = true;
  lastDragX = event.clientX;
  const rect = el.scrubTrack.getBoundingClientRect();
  seekToFraction((event.clientX - rect.left) / rect.width);
});

window.addEventListener("mousemove", (event) => {
  if (!dragScrubbing) return;
  const deltaX = event.clientX - lastDragX;
  lastDragX = event.clientX;
  scrubByDelta(deltaX, el.scrubTrack.clientWidth);
});

window.addEventListener("mouseup", () => {
  dragScrubbing = false;
});

el.scrubTrack.addEventListener("mousedown", (event) => {
  if (!currentVideo()) return;
  event.stopPropagation();
  dragScrubbing = true;
  lastDragX = event.clientX;
  const rect = el.scrubTrack.getBoundingClientRect();
  seekToFraction((event.clientX - rect.left) / rect.width);
});

el.sourceVolume.addEventListener("change", () => {
  if (el.sourceVolume.value) el.sourcePath.value = el.sourceVolume.value;
});
el.outputVolume.addEventListener("change", () => {
  if (el.outputVolume.value) el.outputPath.value = el.outputVolume.value;
});
el.scanBtn.addEventListener("click", scanSource);
el.fileFilter.addEventListener("input", renderFileList);
el.markInBtn.addEventListener("click", markIn);
el.markOutBtn.addEventListener("click", markOut);
el.undoClipBtn.addEventListener("click", undoClip);
el.taskSearch.addEventListener("input", () => renderTasks());
el.addTaskBtn.addEventListener("click", addTask);
el.newTaskInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    addTask();
  }
});
el.finishBtn.addEventListener("click", () => finishCurrent(true));
el.skipBtn.addEventListener("click", skipCurrent);
el.player.addEventListener("timeupdate", updateScrubUi);
el.player.addEventListener("loadedmetadata", updateScrubUi);
el.player.addEventListener("seeked", updateScrubUi);

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, textarea, select")) return;
  const key = event.key.toLowerCase();
  if (key === " ") {
    event.preventDefault();
    if (el.player.paused) el.player.play();
    else el.player.pause();
  }
  if (key === "i") markIn();
  if (key === "o") markOut();
  if (key === "n") finishCurrent(true);
  if (key === "s") skipCurrent();
  if (event.key === "ArrowDown") moveSelection(1);
  if (event.key === "ArrowUp") moveSelection(-1);
  if (event.key === "ArrowLeft") scrubByWheel(-40);
  if (event.key === "ArrowRight") scrubByWheel(40);
});

loadVolumes()
  .then(loadTasks)
  .then(() => api("/api/health"))
  .then((data) => {
    if (el.appVersion) el.appVersion.textContent = `v${data.version || "?"}`;
  })
  .catch((error) => setStatus(error.message, "error"));
