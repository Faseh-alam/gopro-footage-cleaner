const el = {
  batchSelect: document.getElementById("batch-select"),
  batchName: document.getElementById("batch-name"),
  newBatchRow: document.getElementById("new-batch-row"),
  batchHint: document.getElementById("batch-hint"),
  mode: document.getElementById("mode"),
  ssd1: document.getElementById("ssd1"),
  ssd2: document.getElementById("ssd2"),
  customDest: document.getElementById("custom-dest"),
  browseDest: document.getElementById("browse-dest"),
  useCustomDest: document.getElementById("use-custom-dest"),
  useCustomDest2: document.getElementById("use-custom-dest-2"),
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
  logFilter: document.getElementById("log-filter"),
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
    const kind = vol.drive_type === "local" ? " · this computer" : vol.is_card_candidate ? " · SD?" : "";
    option.textContent = `${vol.label} (${vol.path}) · ${free} free${kind}`;
    select.appendChild(option);
  }
  if (current && ![...select.options].some((o) => o.value === current)) {
    const extra = document.createElement("option");
    extra.value = current;
    extra.textContent = `Custom folder (${current})`;
    select.appendChild(extra);
  }
  if (current && [...select.options].some((o) => o.value === current)) {
    select.value = current;
  }
}

async function applyDestination(vol, which) {
  const select = which === "ssd2" ? el.ssd2 : el.ssd1;
  const label = which === "ssd2" ? "SSD 2" : "SSD 1";
  const exists = [...select.options].some((o) => o.value === vol.path);
  if (!exists) {
    const option = document.createElement("option");
    option.value = vol.path;
    option.textContent = `${vol.label} (${vol.path}) · ${formatBytes(vol.free_bytes)} free · custom`;
    select.appendChild(option);
  }
  select.value = vol.path;
  el.customDest.value = vol.path;
  await refreshBatches();
  setStatus(`${label} set to ${vol.path}`, "ok");
}

async function useCustomDestination(which = "ssd1") {
  const path = (el.customDest.value || "").trim();
  if (!path) {
    setStatus("Type a folder path first (e.g. E:\\) or click Browse…", "error");
    return;
  }
  const data = await api("/api/destinations/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  await applyDestination(data.volume, which);
}

async function browseDestination() {
  setStatus("Folder picker is open on this PC — choose the SSD…");
  const data = await api("/api/destinations/browse", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    timeoutMs: 180000,
  });
  await applyDestination(data.volume, "ssd1");
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
    const mp4s = Number(batch.mp4s || 0);
    const jsons = Number(batch.jsons || 0);
    const files = mp4s || jsons
      ? `${mp4s} MP4 / ${jsons} JSON`
      : (batch.cards ? `${batch.cards} file(s)` : "empty");
    const size = batch.bytes ? ` · ${formatBytes(batch.bytes)}` : "";
    option.textContent = `${batch.name} · ${files}${size}`;
    option.dataset.detail = `${batch.name}: ${files}${
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
      '<div class="hint">Waiting for SD cards with MP4s + .segments.json under DCIM/100GOPRO…</div>';
    el.cardsSummary.textContent = "No cards yet";
    return;
  }
  const active = cards.filter((c) =>
    ["copying", "verifying", "wiping", "ejecting", "uploading", "queued", "scanning"].includes(
      c.status,
    ),
  ).length;
  const done = cards.filter((c) => c.status === "completed").length;
  const waiting = cards.filter((c) => c.status === "awaiting_wipe").length;
  const broken = cards.filter((c) =>
    ["error", "needs_fix", "interrupted"].includes(c.status),
  ).length;
  el.cardsSummary.textContent = `${cards.length} seen · ${active} copying · ${waiting} wait wipe · ${broken} problem · ${done} done`;

  const rank = (c) => {
    const s = c.status || "";
    if (s === "error" || s === "needs_fix" || s === "interrupted") return 0;
    if (s === "awaiting_wipe") return 1;
    if (["copying", "verifying", "wiping", "queued", "scanning"].includes(s)) return 2;
    return 3;
  };
  const ordered = cards.slice().sort((a, b) => rank(a) - rank(b));

  for (const card of ordered) {
    const pct = card.bytes_total ? Math.min(100, (card.bytes_done / card.bytes_total) * 100) : 0;
    const issueList = card.issues || [];
    const issues = issueList.length
      ? `<div class="card-issues"><strong>${escapeHtml(card.card_id || "Card")} — ${issueList.length} problem(s)</strong>${issueList
          .map((line) => `<div>${escapeHtml(String(line))}</div>`)
          .join("")}</div>`
      : "";
    const cardLog = (card.card_log || [])
      .slice(-16)
      .map((line) => {
        const t = new Date((line.t || 0) * 1000).toLocaleTimeString();
        return `<div class="${line.kind || ""}">[${t}] ${escapeHtml(line.message || "")}</div>`;
      })
      .join("");
    const wipeBtn = card.can_wipe
      ? `<button type="button" class="danger wipe-card" data-card="${escapeHtml(
          card.card_id || "",
        )}">Wipe copied files on this card</button>`
      : "";
    const phaseLabel =
      card.status === "awaiting_wipe" ? "waiting for wipe" : card.status || "";
    const problem = issueList.length || ["error", "needs_fix", "interrupted"].includes(card.status || "");
    const div = document.createElement("div");
    div.className = problem ? "card has-issues" : "card";
    div.innerHTML = `
      <div class="card-top">
        <span class="card-id">${escapeHtml(card.card_id || "?")}</span>
        <span class="phase ${card.status || ""}">${escapeHtml(phaseLabel)}</span>
      </div>
      <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
      <div class="meta">
        <span>${formatBytes(card.bytes_done || 0)} / ${formatBytes(card.bytes_total || 0)}</span>
        <span>${Number(card.speed_mbps || 0).toFixed(1)} MB/s</span>
        <span>ETA ${formatEta(card.eta_seconds)}</span>
        <span>${card.files_done || 0}/${card.files_total || 0} files</span>
        <span>${pct.toFixed(0)}%</span>
      </div>
      <div class="message">${escapeHtml(card.message || "")}</div>
      ${card.dest ? `<div class="hint">SSD dest: ${escapeHtml(card.dest)}</div>` : ""}
      ${issues}
      ${cardLog ? `<div class="card-log"><div class="card-log-head">${escapeHtml(card.card_id || "Card")} log</div>${cardLog}</div>` : ""}
      ${wipeBtn ? `<div class="job-actions">${wipeBtn}</div>` : ""}
    `;
    el.cards.appendChild(div);
  }
  for (const btn of el.cards.querySelectorAll(".wipe-card")) {
    btn.addEventListener("click", () => wipeCard(btn.getAttribute("data-card")));
  }
  refreshLogFilter(cards);
}

async function wipeCard(cardId) {
  if (!cardId) return;
  const ok = window.confirm(
    `Wipe copied MP4 + JSON on card ${cardId}?\n\nOnly do this after you played the files on the SSD. Unlabeled files stay on the card.`,
  );
  if (!ok) return;
  try {
    await api("/api/cards/wipe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: cardId }),
    });
    setStatus(`${cardId}: wipe confirmed`, "ok");
    await pollStatus();
  } catch (error) {
    setStatus(`${cardId}: ${error.message}`, "error");
  }
}

let lastLogLines = [];

function refreshLogFilter(cards) {
  if (!el.logFilter) return;
  const keep = el.logFilter.value;
  const ids = [...new Set((cards || []).map((c) => c.card_id).filter(Boolean))];
  el.logFilter.innerHTML = '<option value="">All cards</option>';
  for (const id of ids) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    el.logFilter.appendChild(opt);
  }
  if (keep && [...el.logFilter.options].some((o) => o.value === keep)) {
    el.logFilter.value = keep;
  }
}

function renderLog(lines) {
  lastLogLines = lines || [];
  const filter = el.logFilter ? el.logFilter.value : "";
  el.log.innerHTML = "";
  const rows = lastLogLines.filter((line) => !filter || line.card_id === filter || (line.message || "").includes(filter));
  if (!rows.length) {
    el.log.innerHTML = '<div class="hint">No log lines for this card yet.</div>';
    return;
  }
  for (const line of rows.slice().reverse()) {
    const div = document.createElement("div");
    div.className = `log-line ${line.kind || ""}`;
    const t = new Date((line.t || 0) * 1000).toLocaleTimeString();
    const tag = line.card_id ? `${line.card_id} · ` : "";
    div.textContent = `[${t}] ${tag}${line.message || ""}`;
    el.log.appendChild(div);
  }
}

function renderAwsJobs(jobs) {
  el.awsJobs.innerHTML = "";
  if (!jobs.length) {
    el.awsJobs.innerHTML =
      '<div class="hint">No AWS uploads yet — dump cards first, then use “Upload this batch to AWS (CMD)”</div>';
    return;
  }
  for (const job of jobs.slice(0, 12)) {
    const pct = job.bytes_total ? Math.min(100, (job.bytes_done / job.bytes_total) * 100) : 0;
    const statusLabel =
      job.status === "running"
        ? job.console
          ? `live ${job.uploader || "sync"}`
          : "uploading"
        : job.status === "verified"
          ? "verified"
          : job.status === "mismatch"
            ? "size mismatch"
            : job.status || "";
    const recent = (job.log || []).slice(-4);
    const canRestart = ["error", "interrupted", "mismatch", "completed", "verified"].includes(
      job.status,
    );
    const canVerify = ["completed", "verified", "mismatch", "error", "interrupted"].includes(
      job.status,
    );
    const canDelete = job.status === "verified" || job.verified;
    const sizeLine =
      job.local_bytes != null || job.s3_bytes != null
        ? `<div class="hint">Local ${formatBytes(job.local_bytes || 0)} · S3 ${formatBytes(
            job.s3_bytes || 0,
          )}${job.size_delta != null ? ` · Δ ${formatBytes(job.size_delta)}` : ""}</div>`
        : "";
    const div = document.createElement("div");
    div.className = "job";
    div.innerHTML = `
      <div class="job-top">
        <span><strong>${job.batch || "?"}</strong>${
          job.card_id ? " / " + job.card_id : " · full batch"
        }${job.uploader ? ` · ${job.uploader}` : ""}</span>
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
      ${sizeLine}
      <div class="job-actions">
        ${
          canRestart
            ? `<button type="button" class="secondary job-restart" data-job="${escapeHtml(
                job.id,
              )}">Restart</button>`
            : ""
        }
        ${
          canVerify
            ? `<button type="button" class="secondary job-verify" data-job="${escapeHtml(
                job.id,
              )}">Verify sizes</button>`
            : ""
        }
        ${
          canDelete
            ? `<button type="button" class="danger job-delete-local" data-job="${escapeHtml(
                job.id,
              )}">Delete local</button>`
            : ""
        }
      </div>
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

  el.awsJobs.querySelectorAll(".job-restart").forEach((btn) => {
    btn.addEventListener("click", () => restartAwsJob(btn.getAttribute("data-job")));
  });
  el.awsJobs.querySelectorAll(".job-verify").forEach((btn) => {
    btn.addEventListener("click", () => verifyAwsJob(btn.getAttribute("data-job")));
  });
  el.awsJobs.querySelectorAll(".job-delete-local").forEach((btn) => {
    btn.addEventListener("click", () => deleteLocalAwsJob(btn.getAttribute("data-job")));
  });
}

async function restartAwsJob(jobId) {
  if (!jobId) return;
  try {
    setStatus(`Restarting AWS upload…`);
    const data = await api("/api/aws/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
    setStatus(data.job?.message || "Upload restarted — resume-safe", "ok");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function verifyAwsJob(jobId) {
  if (!jobId) return;
  try {
    setStatus("Comparing local size vs S3…");
    const data = await api("/api/aws/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
    const ok = data.job?.verified;
    setStatus(data.job?.message || (ok ? "Sizes match" : "Mismatch"), ok ? "ok" : "error");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function deleteLocalAwsJob(jobId) {
  if (!jobId) return;
  if (
    !window.confirm(
      "Delete local SSD copy for this upload?\n\nOnly do this after Verify shows sizes match. This cannot be undone.",
    )
  ) {
    return;
  }
  try {
    setStatus("Deleting local after verify…");
    const data = await api("/api/aws/delete-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, confirmed: true }),
    });
    setStatus(data.job?.message || "Local deleted", "ok");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function pollStatus() {
  try {
    const data = await api("/api/status");
    const session = data.session || {};
    if (session.active) {
      setStatus(
        `Watching · batch "${session.batch}" · SSD only`,
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

  // Non-blocking uploader check (s5cmd preferred, aws fallback)
  api("/api/health/full", { timeoutMs: 8000 })
    .then((health) => {
      if (health.s5cmd) {
        el.awsCliStatus.textContent = "s5cmd ready";
        el.awsCliStatus.className = "pill ok";
      } else if (health.aws_cli) {
        el.awsCliStatus.textContent = "AWS CLI ready";
        el.awsCliStatus.className = "pill ok";
      } else {
        el.awsCliStatus.textContent = "s5cmd/AWS missing";
        el.awsCliStatus.className = "pill warn";
      }
    })
    .catch(() => {
      el.awsCliStatus.textContent = "Uploader ?";
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
el.logFilter?.addEventListener("change", () => renderLog(lastLogLines));
el.batchSelect.addEventListener("change", onBatchSelectChange);
el.useCustomDest?.addEventListener("click", async () => {
  try {
    await useCustomDestination("ssd1");
  } catch (error) {
    setStatus(error.message, "error");
  }
});
el.useCustomDest2?.addEventListener("click", async () => {
  try {
    await useCustomDestination("ssd2");
  } catch (error) {
    setStatus(error.message, "error");
  }
});
el.browseDest?.addEventListener("click", async () => {
  try {
    await browseDestination();
  } catch (error) {
    setStatus(error.message, "error");
  }
});

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
