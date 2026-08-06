// --------------------------------------------------------------
// Sheets Setup Modal
// --------------------------------------------------------------
const sheetsModal = document.getElementById('sheets-modal');
const sheetsStatus = document.getElementById('sheets-status');
const sheetsForm = document.getElementById('sheets-form');
const sheetsResult = document.getElementById('sheets-result');
const sheetsCloseBtn = document.getElementById('sheets-modal-close');
const sheetsCancelBtn = document.getElementById('sheets-modal-cancel');
const sheetsSetupBtn = document.getElementById('sheets-setup-btn');
const statusText = document.getElementById("setup-status-text");

const Loader = (width, height) => `<svg class="animate-spin" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="${width}" height="${height}" color="currentColor" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
    <path d="M21.9961 12C21.9961 17.5228 17.5189 22 11.9961 22C6.47325 22 1.99609 17.5228 1.99609 12C1.99609 6.47715 6.47325 2 11.9961 2"></path>
</svg>`

const LoaderWithContainer = (...props) => `<div style="display:flex;align-items:center;justify-content:center;width:100%;">${Loader(...props)}</div>`

/** Fetch connection test status */
async function testSheetsConnection() {
  try {
    const data = await api('/api/sheets/test');
    return data.ok === true;
  } catch (error) {
    return false;
  }
}

async function fetchSheetsStatus() {
  try {
    const data = await api('/api/sheets/status');
    return data;
  } catch (error) {
    return { error: error.message };
  }
}

/** Fetch full status (from /status) and update indicator */
async function updateSheetsIndicator() {
  const indicator = document.getElementById('sheets-indicator');
  if (!indicator) return;

  try {
    const status = await api('/api/sheets/status');
    const hasCreds = status.credentialsExists === true;
    const hasSheet = status.spreadsheetIdExists === true;

    // If missing credentials, red
    if (!hasCreds) {
      sheetsSetupBtn.className = 'sheets-btn error'
      indicator.title = 'Missing credentials.json';
      statusText.textContent = "Disconnected";
      return;
    }
    
    // If credentials exist but no sheet, yellow
    if (!hasSheet) {
      sheetsSetupBtn.className = 'sheets-btn partial';
      indicator.title = 'Credentials exist, but no spreadsheet ID set';
      statusText.textContent = "Disconnected";
      return;
    }

    // Both exist: test actual connection
    const connected = await testSheetsConnection();
    if (connected) {
      statusText.textContent = "Connected";
      sheetsSetupBtn.className = 'sheets-btn connected';
      indicator.title = 'Connected to Google Sheets';
    } else {
      sheetsSetupBtn.className = 'sheets-btn error';
      statusText.textContent = "Invalid";
      indicator.title = 'Credentials or spreadsheet ID invalid';
    }
  } catch (error) {
    statusText.textContent = "Error";
    sheetsSetupBtn.className = 'sheets-btn error';
    indicator.title = 'Error checking connection';
  }
}

/** Render detailed status in the modal (including connection test) */
async function renderSheetsStatus(status) {
  const statusDiv = document.getElementById('sheets-status');
  if (!statusDiv) return;

  const hasCreds = status.credentialsExists === true;
  const hasSheet = status.spreadsheetIdExists === true;

  let html = `<div>${hasCreds ? '✅' : '❌'} credentials.json ${hasCreds ? 'exists' : 'missing'}</div>`;
  html += `<div>${hasSheet ? '✅' : '❌'} spreadsheet ID ${hasSheet ? 'set' : 'not set'}</div>`;

  if (hasCreds && hasSheet) {
    // Test connection
    try {
      const test = await api('/api/sheets/test');
      if (test.ok) {
        html += `<div class="ok">✅ Connection valid</div>`;
      } else {
        html += `<div class="error">❌ Connection failed: ${test.error || 'unknown error'}</div>`;
      }
    } catch (error) {
      html += `<div class="error">❌ Connection test error: ${error.message}</div>`;
    }
  } else {
    html += `<div class="hint">Complete the setup to test connection</div>`;
  }

  statusDiv.innerHTML = html;
}

// Override openSheetsModal to refresh status properly
async function openSheetsModal() {
  sheetsModal.classList.remove('hidden');
  sheetsResult.textContent = '';
  sheetsResult.className = 'sheets-result';
  sheetsStatus.innerHTML = LoaderWithContainer(24, 24);
  // Load current status
  const status = await fetchSheetsStatus();
  await renderSheetsStatus(status);
  // Also update indicator
  updateSheetsIndicator();
}
function closeSheetsModal() {
  sheetsModal.classList.add('hidden');
  sheetsResult.textContent = '';
  sheetsResult.className = 'sheets-result';
  sheetsForm.reset();
}

sheetsSetupBtn.addEventListener('click', openSheetsModal);
sheetsCloseBtn.addEventListener('click', closeSheetsModal);
sheetsCancelBtn.addEventListener('click', closeSheetsModal);
sheetsModal.addEventListener('click', (e) => {
  if (e.target === sheetsModal) closeSheetsModal();
});

sheetsForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const formData = new FormData(sheetsForm);
  const fileInput = document.getElementById('sheets-credentials');
  if (!fileInput.files.length) {
    sheetsResult.textContent = 'Please select a credentials.json file.';
    sheetsResult.className = 'sheets-result error';
    return;
  }
  const submitBtn = sheetsForm.querySelector('button[type="submit"]');
  submitBtn.disabled = true;
  submitBtn.innerHTML = Loader(16, 16) + "<span>Conneting...</span>";
  sheetsResult.textContent = 'Connecting...';
  sheetsResult.className = 'sheets-result';

  try {
    const response = await fetch('/api/sheets/setup', {
      method: 'POST',
      body: formData,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || 'Setup failed');
    }
    sheetsResult.textContent = `✅ Connected! Spreadsheet ID: ${data.spreadsheetId}`;
    sheetsResult.className = 'sheets-result success';
    // Refresh status and indicator
    const status = await api('/api/sheets/status');
    await renderSheetsStatus(status);
    updateSheetsIndicator();
    // Optionally close after delay
    setTimeout(closeSheetsModal, 2000);
  } catch (error) {
    sheetsResult.textContent = `❌ ${error.message}`;
    sheetsResult.className = 'sheets-result error';
  } finally {
    submitBtn.disabled = false;
    location.reload(); // Reload the page to reflect changes
  }
});