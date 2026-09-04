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
  updateApp: document.getElementById("update-app"),
  maxParallel: document.getElementById("max-parallel"),
  capacityPanel: document.getElementById("capacity-panel"),
  readerMap: document.getElementById("reader-map"),
  diskState: document.getElementById("disk-state"),
};

let updateState = "idle";
const UPDATE_POPUP_KEY = "sdOffloaderUpdatePopup";
let noticeSeq = 0;
const seenNoticeKeys = new Set();

function setUpdateState(state) {
  updateState = state;
  if (!el.updateApp) return;
  el.updateApp.disabled = state !== "idle";
  el.updateApp.textContent =
    state === "idle" ? "Update" : state === "pulling" ? "Updating…" : "Restarting…";
}

function showNotice(message, kind = "ok", actions = [], key = "") {
  const stack = document.getElementById("notice-stack");
  if (!stack) return;
  if (key) {
    if (seenNoticeKeys.has(key)) return null;
    seenNoticeKeys.add(key);
    if (seenNoticeKeys.size > 80) {
      const first = seenNoticeKeys.values().next().value;
      seenNoticeKeys.delete(first);
    }
  }
  const id = `notice-${++noticeSeq}`;
  const card = document.createElement("div");
  card.className = `notice ${kind === "error" ? "error" : kind === "ok" ? "ok" : ""}`.trim();
  card.id = id;
  const actionHtml = actions.length
    ? `<div class="notice-actions">${actions
        .map(
          (a, i) =>
            `<button type="button" class="${a.primary ? "primary" : "secondary"}" data-action="${i}">${escapeHtml(
              a.label,
            )}</button>`,
        )
        .join("")}</div>`
    : "";
  card.innerHTML = `
    <div class="notice-head">
      <p>${escapeHtml(message)}</p>
      <div class="notice-tools">
        <button type="button" class="notice-min" title="Minimize">–</button>
        <button type="button" class="notice-close" title="Close">×</button>
      </div>
    </div>
    ${actionHtml}
  `;
  card.querySelector(".notice-close")?.addEventListener("click", () => card.remove());
  card.querySelector(".notice-min")?.addEventListener("click", () => {
    const minimized = card.classList.toggle("minimized");
    const btn = card.querySelector(".notice-min");
    if (btn) {
      btn.textContent = minimized ? "+" : "–";
      btn.title = minimized ? "Expand" : "Minimize";
    }
  });
  card.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fn = actions[Number(btn.getAttribute("data-action"))]?.onClick;
      card.remove();
      if (typeof fn === "function") fn();
    });
  });
  stack.appendChild(card);
  return id;
}

function showUpdatePopup(message, kind = "ok") {
  showNotice(message, kind);
}

function showPendingUpdatePopup() {
  try {
    const pending = sessionStorage.getItem(UPDATE_POPUP_KEY);
    if (pending !== "success") return;
    sessionStorage.removeItem(UPDATE_POPUP_KEY);
    showUpdatePopup("Updated successfully", "ok");
  } catch {
    /* private mode / blocked storage */
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runUpdate() {
  if (updateState !== "idle") return;
  showNotice(
    "Pull the latest code from this PC's GitHub branch and restart the offloader?",
    "",
    [
      { label: "Cancel" },
      { label: "Update", primary: true, onClick: () => { void performUpdate(); } },
    ],
  );
}

async function performUpdate() {
  if (updateState !== "idle") return;
  setUpdateState("pulling");
  setStatus("Checking GitHub for updates…");
  try {
    const res = await api("/api/update", { method: "POST", timeoutMs: 180000 });
    if (!res.restarting) {
      setUpdateState("idle");
      setStatus(`Already up to date (${res.branch} @ ${res.after})`, "ok");
      showUpdatePopup("Already up to date", "ok");
      return;
    }
    setUpdateState("restarting");
    setStatus(
      `Updated ${res.branch}: ${res.before} → ${res.after} — restarting…`,
      "ok",
    );
    try {
      sessionStorage.setItem(UPDATE_POPUP_KEY, "success");
    } catch {
      /* ignore */
    }
    await sleep(4000);
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      try {
        await api("/api/ping", { timeoutMs: 3000 });
        window.location.reload();
        return;
      } catch {
        await sleep(1500);
      }
    }
    try {
      sessionStorage.removeItem(UPDATE_POPUP_KEY);
    } catch {
      /* ignore */
    }
    setUpdateState("idle");
    setStatus("Server did not come back — start it with run.bat, then reload this page", "error");
    showUpdatePopup("Failed", "error");
  } catch (error) {
    setUpdateState("idle");
    setStatus(error.message || "Update failed", "error");
    showUpdatePopup("Failed", "error");
  }
}

async function checkForUpdates() {
  if (!el.updateApp) return;
  try {
    const check = await api("/api/update/check", { timeoutMs: 30000 });
    if (check.branch) {
      el.updateApp.title = check.behind
        ? `New code on ${check.branch}: ${check.local} → ${check.remote}`
        : `Pull latest ${check.branch} from GitHub and restart`;
    }
    el.updateApp.classList.toggle("behind", Boolean(check.behind));
    if (check.behind) {
      setStatus(
        `New version on ${check.branch} (${check.local} → ${check.remote}) — press Update`,
        "ok",
      );
    }
  } catch {
    /* offline / git missing — button still works if they retry */
  }
}

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
  await refreshReaders();
  return data.volumes || [];
}

async function refreshReaders() {
  if (!el.readerMap) return;
  try {
    const data = await api("/api/readers", { timeoutMs: 20000 });
    renderReaders(data);
  } catch {
    /* mapping is optional */
  }
}

function renderReaders(data) {
  const box = el.readerMap;
  if (!box) return;
  const mapped = (data && data.mapped) || {};
  const volumes = (data && data.volumes) || [];
  box.innerHTML = [1, 2, 3]
    .map((slot) => {
      const row = mapped[String(slot)] || {};
      const options = volumes
        .map((v) => {
          const path = v.path || "";
          const label = `${path}${v.label ? ` · ${v.label}` : ""}${
            v.reader_slot ? ` (Reader ${v.reader_slot})` : ""
          }`;
          return `<option value="${escapeHtml(path)}">${escapeHtml(label)}</option>`;
        })
        .join("");
      const status = row.usb_id
        ? `Mapped${row.letter ? ` · ${row.letter}:` : ""}`
        : "Not mapped";
      return `<div class="reader-row">
        <span class="reader-slot">Reader ${slot}</span>
        <span class="hint">${escapeHtml(status)}</span>
        <select data-reader-slot="${slot}">
          <option value="">Pick inserted card drive…</option>
          ${options}
        </select>
        <button type="button" class="secondary reader-map-btn" data-reader-slot="${slot}">Map</button>
      </div>`;
    })
    .join("");
  box.querySelectorAll(".reader-map-btn").forEach((btn) => {
    btn.addEventListener("click", () => mapReader(btn.getAttribute("data-reader-slot")));
  });
}

async function mapReader(slot) {
  const select = el.readerMap?.querySelector(`select[data-reader-slot="${slot}"]`);
  const path = select?.value || "";
  if (!path) {
    setStatus("Pick the drive of the card in that reader first", "error");
    return;
  }
  try {
    const data = await api("/api/readers/map", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot, path }),
    });
    renderReaders(data);
    showNotice(`Mapped Reader ${slot}`, "ok");
    setStatus(`Reader ${slot} mapped`, "ok");
  } catch (error) {
    setStatus(error.message, "error");
    showNotice(error.message || "Reader map failed", "error");
  }
}

function batchStateLabel(item) {
  if (!item) return "";
  if (item.role === "active" || item.state === "active") return "Active · offloading";
  const aws = item.aws || item.state || "waiting";
  const map = {
    waiting: "Closed · waiting for upload",
    uploading: "Closed · uploading",
    verifying: "Closed · verifying",
    verified: "Closed · verified",
    failed: "Closed · failed / waiting",
    cleaned: "Completed · cleaned",
    completed: "Completed",
  };
  return map[aws] || `Closed · ${aws}`;
}

function renderDiskState(diskBatches, frozenDisks, session, diskBatchStates) {
  if (!el.diskState) return;
  const states = diskBatchStates || {};
  const ssd1 = session?.ssd1 || states.ssd1?.path || "";
  const ssd2 = session?.ssd2 || states.ssd2?.path || "";
  const cards = [];
  const renderCard = (slot, label, path, row) => {
    if (!path && !row) return;
    const active = row?.active || "";
    const batches = row?.batches || [];
    const list = batches.length
      ? batches
          .map((item) => {
            const kind = item.role === "active" ? "active" : item.aws || "waiting";
            return `<li class="disk-batch disk-batch-${kind}"><strong>${item.name}</strong> — ${batchStateLabel(item)}</li>`;
          })
          .join("")
      : `<li class="disk-batch">${active ? `${active} — Active · offloading` : "no live batch"}</li>`;
    const canClose = Boolean(active);
    const canUpload = Boolean(path);
    cards.push(`
      <div class="disk-card">
        <div class="disk-card-head">
          <div>
            <div class="disk-card-title">${label}</div>
            <div class="disk-card-active">Current batch: <strong>${active || "—"}</strong></div>
          </div>
          <div class="disk-card-actions">
            <button
              type="button"
              class="secondary batch-complete-btn"
              data-complete-ssd="${slot}"
              data-complete-batch="${active || ""}"
              ${canClose ? "" : "disabled"}
              title="Stop adding SD cards to this batch. AWS upload continues separately. Does not delete."
            >Batch Completed</button>
            <button
              type="button"
              class="secondary ssd-upload-btn"
              data-upload-ssd="${slot}"
              ${canUpload ? "" : "disabled"}
              title="Upload the selected batch from this SSD only. Never merges both SSDs into one S3 job. Same names with different sizes become GX010001-1.MP4 on S3."
            >Upload this SSD to AWS</button>
          </div>
        </div>
        <ul class="disk-batch-list">${list}</ul>
      </div>
    `);
  };
  renderCard("1", "SSD 1", ssd1, states.ssd1);
  renderCard("2", "SSD 2", ssd2, states.ssd2);
  if (!cards.length) {
    const batches = diskBatches || {};
    const frozen = frozenDisks || {};
    const parts = [];
    const describe = (label, path) => {
      if (!path) return;
      const key = Object.keys(batches).find((k) => k.toLowerCase() === path.toLowerCase()) || path.toLowerCase();
      const batch = batches[key] || batches[path] || "";
      const isFrozen = Boolean(frozen[key] || frozen[path]);
      parts.push(`${label}: ${batch || "no live batch"}${isFrozen ? " (uploading)" : ""}`);
    };
    describe("SSD1", ssd1);
    describe("SSD2", ssd2);
    el.diskState.textContent = parts.join(" · ");
    return;
  }
  el.diskState.innerHTML = `<div class="disk-card-grid">${cards.join("")}</div>`;
}

async function uploadSsdToAws(slot) {
  const payload = sessionPayload();
  if (!payload.batch) {
    setStatus("Select the batch that is already on the SSDs", "error");
    return;
  }
  if (!payload.s3_uri) {
    setStatus("Paste S3 URI first", "error");
    return;
  }
  const ssd1 = slot === "1" ? payload.ssd1 : "";
  const ssd2 = slot === "2" ? payload.ssd2 : "";
  if (!ssd1 && !ssd2) {
    setStatus(`Pick SSD ${slot} first`, "error");
    return;
  }
  setStatus(`Opening AWS Command Prompt for "${payload.batch}" on SSD ${slot}…`);
  try {
    const data = await api("/api/aws/upload-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        batch: payload.batch,
        s3_uri: payload.s3_uri,
        ssd1,
        ssd2,
        ssd_slot: String(slot),
      }),
    });
    setStatus(
      data.job?.message ||
        `AWS upload started for ${payload.batch} on SSD ${slot}`,
      "ok",
    );
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
    showNotice(error.message || "SSD upload failed", "error");
  }
}
async function completeActiveBatch(slot) {
  const btn = el.diskState?.querySelector(`[data-complete-ssd="${slot}"]`);
  const batch = btn?.dataset?.completeBatch || "this batch";
  const ok = window.confirm(
    `Close ${batch} for NEW offloading on SSD ${slot}?\n\n` +
      "This does NOT delete the folder and does NOT mean AWS upload is finished.\n" +
      "The next batch will become active so you can keep copying SD cards.",
  );
  if (!ok) return;
  try {
    const data = await api("/api/batch/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssd: String(slot) }),
    });
    setStatus(
      `${data.closed} closed for new cards — now offloading to ${data.active} (AWS can finish later)`,
      "ok",
    );
    showNotice(
      `${data.closed} closed — ${data.active} is now active. Old batch was not deleted.`,
      "ok",
    );
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
    showNotice(error.message || "Batch Completed failed", "error");
  }
}

function consumeServerNotices(notices) {
  for (const note of notices || []) {
    const key = note.id || "";
    if (!note.message) continue;
    showNotice(note.message, note.kind || "ok", [], key);
  }
}

function renderCapacity(cap, parallel) {
  if (!el.capacityPanel) return;
  if (!cap) {
    el.capacityPanel.innerHTML = "";
    return;
  }
  const slots = parallel
    ? `${parallel.active || 0} active · ${parallel.waiting || 0} waiting · max ${parallel.max || 3}`
    : `max ${cap.parallel_cards || 3} parallel`;
  el.capacityPanel.innerHTML = `
    <div class="capacity-title">40-card day plan (~${cap.total_tb} TB @ ${cap.gb_per_card} GB/card)</div>
    <div class="capacity-grid">
      <div><span class="k">SD→SSD wall</span><span class="v">~${cap.copy_hours_est} h</span><span class="s">${slots}</span></div>
      <div><span class="k">AWS @ 1 GB/s</span><span class="v">~${cap.upload_hours_at_1_GBps} h</span><span class="s">gigabyte/s pipe</span></div>
      <div><span class="k">AWS @ 1 Gbps</span><span class="v">~${cap.upload_hours_at_1_Gbps} h</span><span class="s">gigabit ≈ 125 MB/s</span></div>
    </div>
    <p class="hint">${escapeHtml(cap.note || "")}</p>
  `;
}

function renderCards(cards) {
  el.cards.innerHTML = "";
  if (!cards.length) {
    el.cards.innerHTML =
      '<div class="hint">Waiting for GoPro SD cards with DCIM/xxxGOPRO + MP4 + JSON… Name/label does not matter. Plug cards anytime while hotplug is armed.</div>';
    el.cardsSummary.textContent = "No cards yet";
    return;
  }
  const active = cards.filter((c) =>
    ["copying", "verifying", "wiping", "ejecting", "uploading", "queued", "waiting", "scanning", "cancelling"].includes(
      c.status,
    ),
  ).length;
  const done = cards.filter((c) => c.status === "completed").length;
  const removed = cards.filter((c) => c.status === "removed").length;
  const failed = cards.filter((c) => ["error", "interrupted", "cancelled"].includes(c.status)).length;
  el.cardsSummary.textContent = `${cards.length} seen · ${active} active · ${done} done${
    removed ? ` · ${removed} removed` : ""
  }${failed ? ` · ${failed} need Retry` : ""}`;

  for (const card of cards) {
    const pct = card.bytes_total ? Math.min(100, (card.bytes_done / card.bytes_total) * 100) : 0;
    const canCancel = [
      "queued",
      "waiting",
      "copying",
      "verifying",
      "wiping",
      "ejecting",
      "uploading",
      "scanning",
      "cancelling",
    ].includes(card.status);
    const canRetry = ["error", "interrupted", "cancelled"].includes(card.status);
    const phaseLabel =
      card.status === "removed"
        ? "removed — insert next"
        : card.status === "interrupted"
          ? "interrupted — Retry"
          : card.status || "";
    const total = Number(card.files_total || 0);
    const verified = Number(card.files_verified || 0);
    const already = Number(card.files_already_in_batch || 0);
    const copied = Number(card.files_copied || 0);
    const fileLabel = ["verifying", "wiping", "ejecting", "completed"].includes(card.status)
      ? already > 0
        ? `${copied} / ${total} new · ${already} already in batch`
        : `${verified || card.files_done || 0} / ${total} files verified`
      : `${card.files_done || 0}/${total} files`;
    const div = document.createElement("div");
    div.className =
      "card" + (canRetry ? " card-error" : "") + (card.status === "removed" ? " card-removed" : "");
    div.innerHTML = `
      <div class="card-top">
        <span class="card-id">${escapeHtml(card.card_id || "?")}${
          card.reader_slot
            ? ` · ${escapeHtml(card.reader_label || `Reader ${card.reader_slot}`)}`
            : ""
        }</span>
        <span class="phase ${card.status || ""}">${phaseLabel}</span>
      </div>
      <div class="bar"><div style="width:${pct.toFixed(1)}%"></div></div>
      <div class="meta">
        <span>${formatBytes(card.bytes_done || 0)} / ${formatBytes(card.bytes_total || 0)}</span>
        <span>${Number(card.speed_mbps || 0).toFixed(1)} MB/s</span>
        <span>ETA ${formatEta(card.eta_seconds)}</span>
        <span>${fileLabel}</span>
        <span>${pct.toFixed(0)}%</span>
      </div>
      <div class="message">${escapeHtml(card.message || "")}</div>
      ${card.dest ? `<div class="hint">SSD dest: ${escapeHtml(card.dest)}</div>` : ""}
      <div class="job-actions">
        ${
          canRetry
            ? `<button type="button" class="primary card-retry" data-card="${escapeHtml(
                card.card_id || "",
              )}">Retry</button>`
            : ""
        }
        ${
          canCancel
            ? `<button type="button" class="danger card-cancel" data-card="${escapeHtml(
                card.card_id || "",
              )}">Cancel</button>`
            : ""
        }
      </div>
    `;
    el.cards.appendChild(div);
  }

  el.cards.querySelectorAll(".card-cancel").forEach((btn) => {
    btn.addEventListener("click", () => cancelCardJob(btn.getAttribute("data-card")));
  });
  el.cards.querySelectorAll(".card-retry").forEach((btn) => {
    btn.addEventListener("click", () => retryCardJob(btn.getAttribute("data-card")));
  });
}

function renderAwsJobs(jobs) {
  el.awsJobs.innerHTML = "";
  if (!jobs.length) {
    el.awsJobs.innerHTML =
      '<div class="hint">No AWS uploads yet — use “Upload this SSD to AWS” on a disk card, or SSD+AWS mode</div>';
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
            : job.status === "cancelled"
              ? "cancelled"
              : job.status === "cancelling"
                ? "cancelling…"
                : job.status || "";
    const recent = (job.log || []).slice(-4);
    const canCancel = ["running", "checking", "cancelling"].includes(job.status);
    const canRestart = [
      "error",
      "interrupted",
      "mismatch",
      "completed",
      "verified",
      "cancelled",
    ].includes(job.status);
    const canVerify = [
      "completed",
      "verified",
      "mismatch",
      "error",
      "interrupted",
      "cancelled",
    ].includes(job.status);
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
          canCancel
            ? `<button type="button" class="danger job-cancel" data-job="${escapeHtml(
                job.id,
              )}">Cancel</button>`
            : ""
        }
        ${
          canRestart
            ? `<button type="button" class="primary job-restart" data-job="${escapeHtml(
                job.id,
              )}">${
                ["error", "interrupted", "mismatch", "cancelled"].includes(job.status)
                  ? "Retry"
                  : "Restart"
              }</button>`
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

  el.awsJobs.querySelectorAll(".job-cancel").forEach((btn) => {
    btn.addEventListener("click", () => cancelAwsJob(btn.getAttribute("data-job")));
  });
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

async function cancelCardJob(cardId) {
  if (!cardId) return;
  if (!window.confirm(`Cancel SD→SSD copy for ${cardId}?\n\nFiles already on the SSD are kept. Card will not be wiped.`)) {
    return;
  }
  try {
    setStatus(`Cancelling ${cardId}…`);
    const data = await api("/api/card/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: cardId }),
    });
    setStatus(data.card?.message || `Cancelled ${cardId}`, "ok");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function retryCardJob(cardId) {
  if (!cardId) return;
  try {
    setStatus(`Retrying ${cardId}…`);
    const data = await api("/api/card/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: cardId }),
    });
    setStatus(data.card?.message || `Retry started for ${cardId}`, "ok");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function cancelAwsJob(jobId) {
  if (!jobId) return;
  if (
    !window.confirm(
      "Cancel this AWS upload?\n\nStops the CMD / s5cmd window. Partial objects may remain on S3 — Restart later to resume.",
    )
  ) {
    return;
  }
  try {
    setStatus("Cancelling AWS upload…");
    const data = await api("/api/aws/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
      timeoutMs: 45000,
    });
    setStatus(data.job?.message || "Upload cancelled", "ok");
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
}

async function restartAwsJob(jobId) {
  if (!jobId) return;
  try {
    setStatus(`Retrying AWS upload…`);
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
      const par = data.parallel || {};
      setStatus(
        `Hotplug armed · batch "${session.batch}" · ${
          session.mode === "ssd_and_aws" ? "SSD→AWS auto" : "SSD only"
        } · insert/remove SDs anytime · ${par.active || 0}/${par.max || 3} SD slots`,
        "ok",
      );
    }
    renderCards(data.cards || []);
    renderAwsJobs(data.aws_jobs || []);
    renderLog(data.log || []);
    renderCapacity(data.capacity, data.parallel);
    renderDiskState(data.disk_batches, data.frozen_disks, session, data.disk_batch_states);
    consumeServerNotices(data.notices);
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
  showPendingUpdatePopup();
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
    el.mode.value = config.mode || "ssd_and_aws";
    el.s3Uri.value = config.s3_uri || "";
    if (el.maxParallel) {
      el.maxParallel.value = String(config.max_parallel_cards || 3);
    }
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
    setStatus("Ready — Start once, then keep inserting SD cards (hotplug auto SD→SSD→AWS)", "ok");
  } catch (error) {
    setStatus(`Drive list failed: ${error.message} — click Refresh drives`, "error");
  }

  pollStatus().catch(() => {});
  setInterval(() => pollStatus().catch(() => {}), 1000);
  checkForUpdates().catch(() => {});
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
    if (el.maxParallel) {
      await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_parallel_cards: Number(el.maxParallel.value) || 3 }),
      });
    }
    await api("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setStatus(`Auto offload armed · batch "${payload.batch}" · plug SD cards`, "ok");
    await refreshBatches(payload.batch);
    await pollStatus();
  } catch (error) {
    setStatus(error.message, "error");
  }
});

el.maxParallel?.addEventListener("change", async () => {
  try {
    await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_parallel_cards: Number(el.maxParallel.value) || 3 }),
    });
    setStatus(`Max parallel SD cards set to ${el.maxParallel.value}`, "ok");
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

el.updateApp?.addEventListener("click", () => {
  runUpdate().catch((error) => setStatus(error.message, "error"));
});

el.diskState?.addEventListener("click", (ev) => {
  const uploadBtn = ev.target.closest("[data-upload-ssd]");
  if (uploadBtn && !uploadBtn.disabled) {
    void uploadSsdToAws(uploadBtn.dataset.uploadSsd);
    return;
  }
  const btn = ev.target.closest("[data-complete-ssd]");
  if (!btn || btn.disabled) return;
  void completeActiveBatch(btn.dataset.completeSsd);
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
