// --------------------------------------------------------------
// Database status modal (Supabase)
// --------------------------------------------------------------
const sheetsModal = document.getElementById("sheets-modal");
const sheetsStatus = document.getElementById("sheets-status");
const sheetsResult = document.getElementById("sheets-result");
const sheetsCloseBtn = document.getElementById("sheets-modal-close");
const sheetsCancelBtn = document.getElementById("sheets-modal-cancel");
const sheetsSetupBtn = document.getElementById("sheets-setup-btn");
const statusText = document.getElementById("setup-status-text");

const Loader = (width, height) =>
  `<svg class="animate-spin" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="${width}" height="${height}" color="currentColor" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
    <path d="M21.9961 12C21.9961 17.5228 17.5189 22 11.9961 22C6.47325 22 1.99609 17.5228 1.99609 12C1.99609 6.47715 6.47325 2 11.9961 2"></path>
</svg>`;

const LoaderWithContainer = (...props) =>
  `<div style="display:flex;align-items:center;justify-content:center;width:100%;">${Loader(...props)}</div>`;

async function testDbConnection() {
  try {
    const data = await api("/api/cards/test");
    return data.ok === true;
  } catch {
    return false;
  }
}

async function updateSheetsIndicator() {
  const indicator = document.getElementById("sheets-indicator");
  if (!indicator || !sheetsSetupBtn) return;

  try {
    const status = await api("/api/cards/status");
    if (!status.configured) {
      sheetsSetupBtn.className = "sheets-btn error";
      indicator.title = "Supabase env vars missing";
      if (statusText) statusText.textContent = "DB not configured";
      return;
    }
    const connected = await testDbConnection();
    if (connected) {
      if (statusText) statusText.textContent = "DB connected";
      sheetsSetupBtn.className = "sheets-btn connected";
      indicator.title = "Connected to Supabase";
    } else {
      sheetsSetupBtn.className = "sheets-btn error";
      if (statusText) statusText.textContent = "DB error";
      indicator.title = "Supabase connection failed";
    }
  } catch {
    if (statusText) statusText.textContent = "Error";
    sheetsSetupBtn.className = "sheets-btn error";
    indicator.title = "Error checking database";
  }
}

async function renderDbStatus() {
  if (!sheetsStatus) return;
  try {
    const status = await api("/api/cards/status");
    let html = `<div>${status.configured ? "✅" : "❌"} Supabase configured</div>`;
    if (status.configured) {
      const test = await api("/api/cards/test");
      if (test.ok) {
        html += `<div class="ok">✅ Connection valid</div>`;
      } else {
        html += `<div class="error">❌ Connection failed: ${test.error || "unknown error"}</div>`;
      }
    } else {
      html += `<div class="hint">Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env</div>`;
    }
    sheetsStatus.innerHTML = html;
  } catch (error) {
    sheetsStatus.innerHTML = `<div class="error">❌ ${error.message}</div>`;
  }
}

async function openSheetsModal() {
  if (!sheetsModal) return;
  sheetsModal.classList.remove("hidden");
  if (sheetsResult) {
    sheetsResult.textContent = "";
    sheetsResult.className = "sheets-result";
  }
  if (sheetsStatus) sheetsStatus.innerHTML = LoaderWithContainer(24, 24);
  await renderDbStatus();
  updateSheetsIndicator();
}

function closeSheetsModal() {
  if (!sheetsModal) return;
  sheetsModal.classList.add("hidden");
  if (sheetsResult) {
    sheetsResult.textContent = "";
    sheetsResult.className = "sheets-result";
  }
}

if (sheetsSetupBtn) sheetsSetupBtn.addEventListener("click", openSheetsModal);
if (sheetsCloseBtn) sheetsCloseBtn.addEventListener("click", closeSheetsModal);
if (sheetsCancelBtn) sheetsCancelBtn.addEventListener("click", closeSheetsModal);
if (sheetsModal) {
  sheetsModal.addEventListener("click", (e) => {
    if (e.target === sheetsModal) closeSheetsModal();
  });
}

updateSheetsIndicator();
