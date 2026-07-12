const el = {
  batchSelect: document.getElementById("batch-select"),
  batchName: document.getElementById("batch-name"),
  newBatchRow: document.getElementById("new-batch-row"),
  batchHint: document.getElementById("batch-hint"),
  mode: document.getElementById("mode"),
  ssd1: document.getElementById("ssd1"),
  ssd2: document.getElementById("ssd2"),
  s3Uri: document.getElementById("s3-uri"),
  refreshVolumes: document.getElementById("refresh-volumes"),
  startSession: document.getElementById("start-session"),
  stopSession: document.getElementById("stop-session"),
  uploadBatch: document.getElementById("upload-batch"),
  testAws: document.getElementById("test-aws"),
  sessionStatus: document.getElementById("session-status"),
  cards: document.getElementById("cards"),
  cardsSummary: document.getElementById("cards-summary"),
  awsJobs: document.getElementById("aws-jobs"),
  log: document.getElementById("log"),
  awsCliStatus: document.getElementById("aws-cli-status"),
  appVersion: document.getElementById("app-version"),
};

async function api(url, options = {}) {
  const { timeoutMs = 15000, ...fetchOptions } = options;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...fetchOptions, signal: ctrl.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error(`Timed out talking to server (${url})`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatEta(seconds) {
  if (!Number.isFinite(seconds) || seconds == null) return "—";
  const s = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${r}s`;
  return `${r}s`;
}

function setStatus(message, kind = "") {
  el.sessionStatus.textContent = message || "";
  el.sessionStatus.className = `status ${kind}`.trim();
}

function fillVolumeSelect(select, volumes, selected) {
  const current = selected || select.value;
  select.innerHTML = '<option value="">— not selected —</option>';
  for (const vol of volumes) {
    const option = document.createElement("option");
    option.value = vol.path;
    const free = formatBytes(vol.free_bytes);
    const tag = vol.is_card_candidate ? " · SD?" : "";
    option.textContent = `${vol.label} (${vol.path}) · ${free} free${tag}`;
    select.appendChild(option);
  }
  if (current && [...select.options].some((o) => o.value === current)) {
    select.value = current;
  }
}

function selectedBatchName() {
  const pick = el.batchSelect.value;
  if (pick === "__new__") return el.batchName.value.trim();
  return (pick || "").trim();
}

function onBatchSelectChange() {
  const isNew = el.batchSelect.value === "__new__";
  el.newBatchRow.classList.toggle("hidden", !isNew);
  if (!isNew && el.batchSelect.value) {
    el.batchHint.textContent =
      el.batchSelect.selectedOptions[0]?.dataset?.detail ||
      "Selected batch — Start SD→SSD and/or Upload to AWS.";
  } else if (isNew) {
    el.batchHint.textContent = "Type a new batch name (e.g. batch 6). Folder is created on the SSDs when you start.";
  }
}

async function refreshBatches(preferred) {
  const ssd1 = el.ssd1.value;
  const ssd2 = el.ssd2.value;
  const data = await api(
    `/api/batches?ssd1=${encodeURIComponent(ssd1)}&ssd2=${encodeURIComponent(ssd2)}`,
  );
  const batches = data.batches || [];
  const keep = preferred || selectedBatchName() || el.batchSelect.value;
  el.batchSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = batches.length
    ? `Select a batch (${batches.length} found)…`
    : "No batches on SSDs yet — create new";
  el.batchSelect.appendChild(placeholder);

  for (const batch of batches) {
    const option = document.createElement("option");
    option.value = batch.name;
    const cards = batch.cards ? `${batch.cards} card(s)` : "empty";
    const size = batch.bytes ? ` · ${formatBytes(batch.bytes)}` : "";
    option.textContent = `${batch.name} · ${cards}${size}`;
    option.dataset.detail = `${batch.name}: ${cards}${
      batch.bytes ? `, ${formatBytes(batch.bytes)} on SSD` : ""
    } — continue SD copy or upload to AWS`;
    el.batchSelect.appendChild(option);
  }

  const create = document.createElement("option");
  create.value = "__new__";
  create.textContent = "+ Create new batch…";
  el.batchSelect.appendChild(create);

  if (keep && keep !== "__new__" && [...el.batchSelect.options].some((o) => o.value === keep)) {
    el.batchSelect.value = keep;
  } else if (keep === "__new__") {
    el.batchSelect.value = "__new__";
  } else if (batches.length === 1) {
    el.batchSelect.value = batches[0].name;
  }
  onBatchSelectChange();
  return batches;
}

async function refreshVolumes() {
  const data = await api("/api/volumes", { timeoutMs: 45000 });
  fillVolumeSelect(el.ssd1, data.volumes || [], el.ssd1.value);
  fillVolumeSelect(el.ssd2, data.volumes || [], el.ssd2.value);
  await refreshBatches();
  return data.volumes || [];
}

function renderCards(cards) {
  el.cards.innerHTML = "";
  if (!cards.length) {
    el.cards.innerHTML =
      '<div class="hint">Waiting for Cxxxx cards with DCIM/100GOPRO/task folders…</div>';
    el.cardsSummary.textContent = "No cards yet";
    return;
  }
  const active = cards.filter((c) =>
    ["copying", "verifying", "wiping", "ejecting", "uploading", "queued", "scanning"].includes(
      c.status,
    ),
  ).length;
  const done = cards.filter((c) => c.status === "completed").length;
  el.cardsSummary.textContent = `${cards.length} seen · ${active} active · ${done} done`;

  for (const card of cards) {
    const pct = card.bytes_total ? Math.min(100, (card.bytes_done / card.bytes_total) * 100) : 0;
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `
      <div class="card-top">
        <span class="card-id">${card.card_id || "?"}</span>
        <span class="phase ${card.status || ""}">${card.status || ""}</span>
      </div>
      <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
      <div class="meta">
        <span>${formatBytes(card.bytes_done || 0)} / ${formatBytes(card.bytes_total || 0)}</span>
        <span>${Number(card.speed_mbps || 0).toFixed(1)} MB/s</span>
        <span>ETA ${formatEta(card.eta_seconds)}</span>
        <span>${card.files_done || 0}/${card.files_total || 0} files</span>
        <span>${pct.toFixed(0)}%</span>
      </div>
      <div class="message">${card.message || ""}</div>
      ${card.dest ? `<div class="hint">SSD dest: ${card.dest}</div>` : ""}
    `;
    el.cards.appendChild(div);
  }
}

function renderAwsJobs(jobs) {
  el.awsJobs.innerHTML = "";
  if (!jobs.length) {
    el.awsJobs.innerHTML =
      '<div class="hint">No AWS uploads yet — use “Upload this batch to AWS (CMD)” or SSD+AWS mode</div>';
    return;
  }
  for (const job of jobs.slice(0, 12)) {
    const pct = job.bytes_total ? Math.min(100, (job.bytes_done / job.bytes_total) * 100) : 0;
    const statusLabel =
      job.status === "running"
        ? job.console
          ? "live + console"
          : "uploading"
        : job.status || "";
    const recent = (job.log || []).slice(-4);
    const div = document.createElement("div");
    div.className = "job";
    div.innerHTML = `
      <div class="job-top">
        <span><strong>${job.batch || "?"}</strong>${
          job.card_id ? " / " + job.card_id : " · full batch"
        }</span>
        <span class="phase ${job.status || ""}">${statusLabel}</span>
      </div>
      <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
      <div class="meta">
        <span>${formatBytes(job.bytes_done || 0)} / ${formatBytes(job.bytes_total || 0)}</span>
        <span>${Number(job.speed_mbps || 0).toFixed(1)} MB/s</span>
        <span>ETA ${formatEta(job.eta_seconds)}</span>
        <span>${
          job.files_remaining != null
            ? `${job.files_remaining} file(s) remaining`
            : `${job.files_done || 0} file(s) sent`
        }</span>
        <span>${pct.toFixed(0)}%</span>
      </div>
      <div class="message">${job.message || job.dest || ""}</div>
      ${
        recent.length
          ? `<div class="job-console">${recent
              .map((line) => `<div>${escapeHtml(String(line))}</div>`)
              .join("")}</div>`
          : ""
      }
    `;
    el.awsJobs.appendChild(div);
  }
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderLog(lines) {
  el.log.innerHTML = "";
  for (const line of (lines || []).slice().reverse()) {
    const div = document.createElement("div");
    div.className = `log-line ${line.kind || ""}`;
    const t = new Date((line.t || 0) * 1000).toLocaleTimeString();
    div.textContent = `[${t}] ${line.message || ""}`;
    el.log.appendChild(div);
  }
}

async function pollStatus() {
  try {
    const data = await api("/api/status");
    const session = data.session || {};
    if (session.active) {
      setStatus(
        `Watching · batch "${session.batch}" · ${
          session.mode === "ssd_and_aws" ? "SSD+AWS (CMD survives restart)" : "SSD only"
        }`,
        "ok",
      );
    }
    renderCards(data.cards || []);
    renderAwsJobs(data.aws_jobs || []);
    renderLog(data.log || []);
  } catch {
    /* ignore transient */
  }
}

function sessionPayload() {
  return {
    batch: selectedBatchName(),
    mode: el.mode.value,
    ssd1: el.ssd1.value,
    ssd2: el.ssd2.value,
    s3_uri: el.s3Uri.value.trim(),
  };
}

async function bootstrap() {
  setStatus("Connecting to offloader…");
  try {
    const health = await api("/api/ping", { timeoutMs: 5000 });
    el.appVersion.textContent = `v${health.version || "?"}`;
    setStatus(`Connected · v${health.version || "?"}`, "ok");
  } catch (error) {
    setStatus(`Cannot reach server: ${error.message}`, "error");
    return;
  }

  // Non-blocking AWS CLI check
  api("/api/health/full", { timeoutMs: 8000 })
    .then((health) => {
      el.awsCliStatus.textContent = health.aws_cli ? "AWS CLI ready" : "AWS CLI missing";
      el.awsCliStatus.className = `pill ${health.aws_cli ? "ok" : "warn"}`;
    })
    .catch(() => {
      el.awsCliStatus.textContent = "AWS CLI ?";
      el.awsCliStatus.className = "pill warn";
    });

  let config = {};
  try {
    config = await api("/api/config", { timeoutMs: 5000 });
    el.mode.value = config.mode || "ssd_only";
    el.s3Uri.value = config.s3_uri || "";
  } catch (error) {
    setStatus(`Config load failed: ${error.message}`, "error");
  }

  setStatus("Loading drives…");
  try {
    await refreshVolumes();
    if (config.ssd1) el.ssd1.value = config.ssd1;
    if (config.ssd2) el.ssd2.value = config.ssd2;
    await refreshBatches(config.last_batch || "");
    if (config.last_batch && ![...el.batchSelect.options].some((o) => o.value === config.last_batch)) {
      el.batchSelect.value = "__new__";
      el.batchName.value = config.last_batch;
      onBatchSelectChange();
    }
    setStatus("Ready — click Start SD → SSD when you want to watch cards", "ok");
  } catch (error) {
    setStatus(`Drive list failed: ${error.message} — click Refresh drives`, "error");
  }

  pollStatus().catch(() => {});
  setInterval(() => pollStatus().catch(() => {}), 1000);
}

el.refreshVolumes.addEventListener("click", async () => {
  try {
    await refreshVolumes();
    setStatus("Drives & batches refreshed", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.ssd1.addEventListener("change", () => refreshBatches().catch(() => {}));
el.ssd2.addEventListener("change", () => refreshBatches().catch(() => {}));
el.batchSelect.addEventListener("change", onBatchSelectChange);

el.startSession.addEventListener("click", async () => {
  try {
    const payload = sessionPayload();
    if (!payload.batch) {
      setStatus("Select an existing batch or create a new one", "error");
      return;
    }
    await api("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setStatus(`Watching for SD cards → batch "${payload.batch}"`, "ok");
    await refreshBatches(payload.batch);
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.stopSession.addEventListener("click", async () => {
  try {
    await api("/api/session/stop", { method: "POST" });
    setStatus("Stopped watching for new cards", "");
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.uploadBatch.addEventListener("click", async () => {
  try {
    const payload = sessionPayload();
    if (!payload.batch) {
      setStatus("Select the batch that is already on the SSDs", "error");
      return;
    }
    if (!payload.s3_uri) {
      setStatus("Paste S3 URI first", "error");
      return;
    }
    setStatus(`Opening AWS Command Prompt for "${payload.batch}"…`);
    const data = await api("/api/aws/upload-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setStatus(
      data.job?.message ||
        `AWS upload started for ${payload.batch} — watch progress here and in the console`,
      "ok",
    );
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.testAws?.addEventListener("click", async () => {
  el.testAws.disabled = true;
  setStatus("Testing AWS — uploading empty file…");
  try {
    const data = await api("/api/aws/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ s3_uri: el.s3Uri.value.trim() }),
    });
    setStatus(data.message || "AWS connection OK", "ok");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    el.testAws.disabled = false;
  }
});

bootstrap().catch((error) => setStatus(error.message, "error"));
