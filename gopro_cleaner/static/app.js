const state = {
  currentPath: null,
  selectedPath: null,
  selectedInfo: null,
  trackedBatches: new Map(),
  queuePollTimer: null,
  importReady: false,
  importFile: null,
};

const el = {
  volumeSelect: document.getElementById("volume-select"),
  refreshBtn: document.getElementById("refresh-btn"),
  breadcrumb: document.getElementById("breadcrumb"),
  fileList: document.getElementById("file-list"),
  selectedVideo: document.getElementById("selected-video"),
  videoDetails: document.getElementById("video-details"),
  clearSelectionBtn: document.getElementById("clear-selection-btn"),
  videoName: document.getElementById("video-name"),
  videoMeta: document.getElementById("video-meta"),
  gpmfStatus: document.getElementById("gpmf-status"),
  clipsInput: document.getElementById("clips-input"),
  deleteOriginalCheckbox: document.getElementById("delete-original-checkbox"),
  queueBtn: document.getElementById("queue-btn"),
  addRowBtn: document.getElementById("add-row-btn"),
  sheetFileInput: document.getElementById("sheet-file-input"),
  importDriveSelect: document.getElementById("import-drive-select"),
  generateDateSelect: document.getElementById("generate-date-select"),
  generateSheetBtn: document.getElementById("generate-sheet-btn"),
  importDeleteOriginal: document.getElementById("import-delete-original"),
  previewSheetBtn: document.getElementById("preview-sheet-btn"),
  queueSheetBtn: document.getElementById("queue-sheet-btn"),
  importPreview: document.getElementById("import-preview"),
  importSummary: document.getElementById("import-summary"),
  importErrors: document.getElementById("import-errors"),
  importWarnings: document.getElementById("import-warnings"),
  importTableBody: document.getElementById("import-table-body"),
  queueList: document.getElementById("queue-list"),
  queueSummary: document.getElementById("queue-summary"),
  statusLine: document.getElementById("status-line"),
  appVersion: document.getElementById("app-version"),
  fileRowTemplate: document.getElementById("file-row-template"),
};

function setStatus(message) {
  el.statusLine.textContent = message;
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

async function loadVolumes() {
  const data = await api("/api/volumes");
  el.volumeSelect.innerHTML = '<option value="">Choose a drive or folder...</option>';
  el.importDriveSelect.innerHTML = '<option value="">Choose drive...</option>';
  el.generateDateSelect.innerHTML = '<option value="">All dates on drive</option>';
  for (const volume of data.volumes) {
    const option = document.createElement("option");
    option.value = volume.path;
    option.textContent = volume.name;
    el.volumeSelect.appendChild(option);

    if (volume.path.startsWith("/Volumes/")) {
      const driveOption = document.createElement("option");
      driveOption.value = volume.name;
      driveOption.textContent = volume.name;
      el.importDriveSelect.appendChild(driveOption);
    }
  }
}

async function loadDateFolders(driveName) {
  el.generateDateSelect.innerHTML = '<option value="">All dates on drive</option>';
  if (!driveName) return;
  try {
    const ytPath = `/Volumes/${driveName}/archive/YT`;
    const data = await api(`/api/browse?path=${encodeURIComponent(ytPath)}`);
    for (const entry of data.entries) {
      if (!entry.is_dir) continue;
      const option = document.createElement("option");
      option.value = entry.name;
      option.textContent = entry.name;
      el.generateDateSelect.appendChild(option);
    }
  } catch {
    // drive may be offline
  }
}

function downloadGeneratedSheet() {
  const drive = el.importDriveSelect.value.trim();
  if (!drive) {
    alert("Choose a drive first");
    return;
  }
  const date = el.generateDateSelect.value.trim();
  const params = new URLSearchParams({ drive });
  if (date) params.set("date", date);
  window.location.href = `/api/generate-sheet?${params.toString()}`;
}

function clearSelection() {
  state.selectedPath = null;
  state.selectedInfo = null;
  el.videoDetails.classList.add("hidden");
  el.selectedVideo.classList.remove("hidden");
  el.clearSelectionBtn.classList.add("hidden");
  document.querySelectorAll(".file-row.selected").forEach((node) => node.classList.remove("selected"));
}

function renderBreadcrumb(path) {
  el.breadcrumb.innerHTML = "";
  if (!path) return;

  const parts = path.split("/").filter(Boolean);
  let running = path.startsWith("/") ? "" : "";
  parts.forEach((part, index) => {
    running += `/${part}`;
    const targetPath = running;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "crumb";
    button.textContent = part;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      browse(targetPath);
    });
    el.breadcrumb.appendChild(button);
    if (index < parts.length - 1) {
      const sep = document.createElement("span");
      sep.textContent = "/";
      sep.className = "crumb-sep";
      el.breadcrumb.appendChild(sep);
    }
  });
}

function createFileRow(entry) {
  const node = el.fileRowTemplate.content.cloneNode(true);
  const row = node.querySelector(".file-row");
  const icon = node.querySelector(".file-icon");
  const name = node.querySelector(".file-name");
  const meta = node.querySelector(".file-meta");

  name.textContent = entry.name;
  if (entry.is_dir) {
    icon.textContent = "📁";
    meta.textContent = "Folder";
    row.addEventListener("click", (event) => {
      event.preventDefault();
      browse(entry.path);
    });
  } else if (entry.is_video) {
    icon.textContent = entry.is_gopro ? "🎥" : "🎬";
    meta.textContent = formatBytes(entry.size_bytes);
    row.classList.add("video");
    if (entry.is_gopro) row.classList.add("gopro");
    row.addEventListener("click", (event) => {
      event.preventDefault();
      selectVideo(entry.path, row);
    });
  } else {
    icon.textContent = "📄";
    meta.textContent = formatBytes(entry.size_bytes);
    row.disabled = true;
    row.style.opacity = "0.55";
  }

  if (state.selectedPath === entry.path) {
    row.classList.add("selected");
  }
  return row;
}

async function browse(path) {
  try {
    setStatus(`Opening ${path}`);
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    state.currentPath = data.path;
    renderBreadcrumb(data.path);
    el.fileList.innerHTML = "";

    if (data.parent) {
      const up = document.createElement("button");
      up.type = "button";
      up.className = "file-row";
      up.innerHTML =
        '<span class="file-icon">⬆️</span><span class="file-name">..</span><span class="file-meta">Up</span>';
      up.addEventListener("click", (event) => {
        event.preventDefault();
        browse(data.parent);
      });
      el.fileList.appendChild(up);
    }

    for (const entry of data.entries) {
      el.fileList.appendChild(createFileRow(entry));
    }
    setStatus(`Showing ${data.entries.length} items`);
  } catch (error) {
    setStatus(error.message);
    alert(error.message);
  }
}

async function selectVideo(path, row = null) {
  try {
    const info = await api(`/api/probe?path=${encodeURIComponent(path)}`);
    state.selectedPath = path;
    state.selectedInfo = info;

    document.querySelectorAll(".file-row.selected").forEach((node) => node.classList.remove("selected"));
    if (row) row.classList.add("selected");

    el.selectedVideo.classList.add("hidden");
    el.videoDetails.classList.remove("hidden");
    el.clearSelectionBtn.classList.remove("hidden");
    el.videoName.textContent = info.name;
    el.videoMeta.textContent = `${info.duration_label || "Unknown duration"} · ${formatBytes(info.size_bytes)}`;
    el.gpmfStatus.textContent = info.has_gpmf
      ? "IMU / GPMF metadata detected"
      : "Warning: no GPMF metadata track detected";
    el.gpmfStatus.className = `badge ${info.has_gpmf ? "ok" : "warn"}`;

    if (!el.clipsInput.value.trim()) {
      el.clipsInput.value = "";
    }
    setStatus(`Selected ${info.name}`);
  } catch (error) {
    setStatus(error.message);
    alert(error.message);
  }
}

function renderQueue(batches, summary = null) {
  el.queueList.innerHTML = "";
  if (!batches.length) {
    el.queueSummary.textContent = "No jobs queued";
    el.queueList.innerHTML = '<div class="queue-empty">Queued batches will appear here.</div>';
    return;
  }

  if (summary?.total) {
    const active = (summary.running || 0) + (summary.queued || 0);
    const done = (summary.completed || 0) + (summary.failed || 0);
    el.queueSummary.textContent = `${active} active · ${done} done · ${summary.total} total`;
  } else {
    const active = batches.filter((batch) => ["queued", "running"].includes(batch.status)).length;
    el.queueSummary.textContent = `${active} active · ${batches.length} total`;
  }

  for (const batch of batches) {
    const card = document.createElement("div");
    card.className = `queue-card status-${batch.status}`;

    const title = document.createElement("div");
    title.className = "queue-card-title";
    title.textContent = batch.input_name;

    const meta = document.createElement("div");
    meta.className = "queue-card-meta";
    meta.textContent = `${batch.completed}/${batch.clip_count} clips · ${batch.message || batch.status}`;

    const bar = document.createElement("div");
    bar.className = "progress-bar";
    const fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = `${batch.progress || 0}%`;
    bar.appendChild(fill);

    const jobs = document.createElement("ul");
    jobs.className = "queue-jobs";
    for (const job of batch.jobs) {
      const item = document.createElement("li");
      item.textContent = `${job.output_name}: ${job.start} → ${job.end} · ${job.status}`;
      if (job.error) {
        item.textContent += ` (${job.error.split("\n")[0]})`;
      }
      jobs.appendChild(item);
    }

    card.appendChild(title);
    card.appendChild(meta);
    card.appendChild(bar);
    card.appendChild(jobs);
    el.queueList.appendChild(card);
  }
}

async function refreshQueue() {
  try {
    const data = await api("/api/queue");
    renderQueue(data.batches || [], data.summary || null);

    const currentFolder = state.currentPath;
    const shouldRefreshFolder = (data.batches || []).some((batch) => {
      if (!["completed", "failed"].includes(batch.status)) return false;
      if (!state.trackedBatches.has(batch.batch_id)) return false;
      const previous = state.trackedBatches.get(batch.batch_id);
      return previous !== batch.status && currentFolder;
    });

    for (const batch of data.batches || []) {
      state.trackedBatches.set(batch.batch_id, batch.status);
    }

    if (shouldRefreshFolder && currentFolder) {
      await browse(currentFolder);
    }
  } catch (error) {
    setStatus(error.message);
  }
}

function startQueuePolling() {
  if (state.queuePollTimer) return;
  state.queuePollTimer = setInterval(refreshQueue, 1500);
  refreshQueue();
}

async function queueClips() {
  if (!state.selectedPath) return;

  const clips = el.clipsInput.value.trim();
  if (!clips) {
    alert("Paste at least one clip line, e.g. 00:00 - 7:45");
    return;
  }

  el.queueBtn.disabled = true;
  setStatus("Queueing clips...");

  try {
    const data = await api("/api/batch", {
      method: "POST",
      body: JSON.stringify({
        path: state.selectedPath,
        clips,
        delete_original: el.deleteOriginalCheckbox.checked,
      }),
    });

    state.trackedBatches.set(data.batch_id, data.status);
    el.clipsInput.value = "";
    clearSelection();
    startQueuePolling();
    setStatus(`Queued ${data.clip_count} clips for ${data.input_name}`);
  } catch (error) {
    alert(error.message);
    setStatus(error.message);
  } finally {
    el.queueBtn.disabled = false;
  }
}

function addExampleLine() {
  const example = "00:00 - 7:45";
  el.clipsInput.value = el.clipsInput.value
    ? `${el.clipsInput.value.trim()}\n${example}`
    : example;
  el.clipsInput.focus();
}

async function uploadSheet(endpoint) {
  const file = state.importFile || el.sheetFileInput.files?.[0];
  if (!file) {
    throw new Error("Choose a CSV or JSON sheet first");
  }
  const drive = el.importDriveSelect.value.trim();
  if (!drive) {
    throw new Error("Choose which drive this sheet is for");
  }

  const formData = new FormData();
  formData.append("file", file);
  formData.append("drive", drive);
  formData.append(
    "delete_original",
    el.importDeleteOriginal.checked ? "yes" : "no"
  );
  const response = await fetch(endpoint, {
    method: "POST",
    body: formData,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Sheet import failed");
  }
  return payload;
}

function renderImportPreview(data) {
  el.importPreview.classList.remove("hidden");
  el.importSummary.textContent = `${data.video_count} videos · ${data.clip_count} clips ready`;

  if (data.errors?.length) {
    el.importErrors.classList.remove("hidden");
    el.importErrors.textContent = data.errors.join("\n");
  } else {
    el.importErrors.classList.add("hidden");
    el.importErrors.textContent = "";
  }

  if (data.warnings?.length) {
    el.importWarnings.classList.remove("hidden");
    el.importWarnings.textContent = data.warnings.join("\n");
  } else {
    el.importWarnings.classList.add("hidden");
    el.importWarnings.textContent = "";
  }

  el.importTableBody.innerHTML = "";
  for (const video of data.videos || []) {
    const row = document.createElement("tr");
    const clipLines = (video.clips || [])
      .map((clip) => `${clip.start} → ${clip.end}`)
      .join("\n");
    row.innerHTML = `
      <td>${video.footage || video.video}</td>
      <td class="path-cell">${video.video_path}</td>
      <td>${video.clip_count}<br><span class="hint">${clipLines.replace(/\n/g, "<br>")}</span></td>
    `;
    el.importTableBody.appendChild(row);
  }

  state.importReady = Boolean(data.ready);
  el.queueSheetBtn.disabled = !state.importReady;
}

async function previewSheet() {
  try {
    setStatus("Reading sheet...");
    const data = await uploadSheet("/api/import/preview");
    renderImportPreview(data);
    setStatus(
      data.ready
        ? `Sheet ready: ${data.video_count} videos, ${data.clip_count} clips`
        : "Sheet has errors"
    );
  } catch (error) {
    alert(error.message);
    setStatus(error.message);
  }
}

async function queueSheet() {
  if (!state.importReady) {
    alert("Preview the sheet first and fix any errors.");
    return;
  }

  const confirmed = window.confirm(
    `Queue ${el.importSummary.textContent}? Trimming will run in the background.`
  );
  if (!confirmed) return;

  try {
    el.queueSheetBtn.disabled = true;
    setStatus("Queueing sheet...");
    const data = await uploadSheet("/api/import/queue");
    startQueuePolling();
    const message = `Queued ${data.queued_count} videos (${data.clip_count} clips)`;
    if (data.failed_count) {
      alert(`${message}\n${data.failed_count} videos failed to queue.`);
    } else {
      alert(message);
    }
    setStatus(message);
    state.importReady = false;
    el.sheetFileInput.value = "";
    state.importFile = null;
  } catch (error) {
    alert(error.message);
    setStatus(error.message);
  } finally {
    el.queueSheetBtn.disabled = !state.importReady;
  }
}

el.volumeSelect.addEventListener("change", (event) => {
  const path = event.target.value;
  if (path) browse(path);
});

el.refreshBtn.addEventListener("click", () => {
  if (state.currentPath) browse(state.currentPath);
  loadVolumes();
  refreshQueue();
});

el.queueBtn.addEventListener("click", queueClips);
el.addRowBtn.addEventListener("click", addExampleLine);
el.clearSelectionBtn.addEventListener("click", clearSelection);
el.sheetFileInput.addEventListener("change", () => {
  state.importFile = el.sheetFileInput.files?.[0] || null;
  state.importReady = false;
  el.queueSheetBtn.disabled = true;
});
el.importDriveSelect.addEventListener("change", () => {
  loadDateFolders(el.importDriveSelect.value.trim());
});
el.generateSheetBtn.addEventListener("click", downloadGeneratedSheet);
el.previewSheetBtn.addEventListener("click", previewSheet);
el.queueSheetBtn.addEventListener("click", queueSheet);

loadVolumes()
  .then(() => browse("/Volumes"))
  .catch((error) => setStatus(error.message));

api("/api/health")
  .then((data) => {
    if (el.appVersion) el.appVersion.textContent = data.version || "?";
  })
  .catch(() => {
    if (el.appVersion) el.appVersion.textContent = "offline";
  });

startQueuePolling();
