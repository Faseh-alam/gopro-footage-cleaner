// Sheets integration state
let sheetsProcess = null;          // { processId, sheetName, cards: [...] }
let sheetsCurrentCardId = null;    // rowId of the card currently being worked on

const cardPushSheetsBtn = document.getElementById("push-card-data-btn")

/** Fetch today’s process (auto‑creates if needed) and update UI */
async function refreshSheetsProcess() {
    try {
        const data = await api("/api/sheets/process/current");
        sheetsProcess = data.currentProcess;
        // Display the sheet name somewhere (e.g., status line, header)
        if (sheetsProcess) {
            setStatus(`Google Sheet: ${sheetsProcess.sheetName} (${sheetsProcess.cards.length} cards)`, "ok");
        } else {
            setStatus("No active sheet process", "error");
        }
        // Check if the current card (by path) is already in the process
        // and update sheetsCurrentCardId accordingly.
        const currentPath = scanTargetPath();
        if (currentPath && sheetsProcess) {
            const found = sheetsProcess.cards.find(c => c.cardPath === currentPath);
            if (found) {
                sheetsCurrentCardId = found.rowId;
            } else {
                sheetsCurrentCardId = null;
            }
        }
        return data;
    } catch (error) {
        setStatus(error.message, "error");
        return null;
    }
}

/** Add a new card (SD card) to today’s process */
async function addCardToSheets(cardPath, cardName) {
    if (!cardPath) return;
    // Avoid duplicates – check if already added
    // if (sheetsProcess && sheetsProcess.cards.some(c => c.cardPath === cardPath)) {
    //     // Already present, just update the current card id
    //     const found = sheetsProcess.cards.find(c => c.cardPath === cardPath);
    //     sheetsCurrentCardId = found.rowId;
    //     setStatus(`Card "${cardName}" already in sheet`, "ok");
    // }

    try {
        const data = await api("/api/sheets/process/card", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cardPath, cardName }),
        });

        if (data.already_exists) {
            console.log(data)
            // Card already exists – just set the current card ID
            sheetsCurrentCardId = data.card.card_name;
            setStatus(`Card "${data.card.card_name}" already in sheet`, "ok");
        } else {
            // New card added
            sheetsCurrentCardId = data.card.card_name;
            setStatus(`Card "${data.card.card_name}" added to sheet`, "ok");
        }

        // Refresh process to get updated cards list
        await refreshSheetsProcess();
        // Set the current card id
        if (sheetsProcess) {
            const found = sheetsProcess.cards.find(c => c.cardName === cardName);
            if (found) sheetsCurrentCardId = found.cardName;
        }
        setStatus(`Card "${cardName}" added to sheet (row ${data.sheetRowIndex})`, "ok");
    } catch (error) {
        setStatus(`Failed to add card to sheet: ${error.message}`, "error");
    }
}

/** Finish the current card – updates sheet and marks as done */
async function finishCurrentCard() {
    if (!sheetsCurrentCardId) {
        setStatus("No active card to finish", "error");
        return;
    }

    // Collect final metrics – you may want to compute these from your current labeling state
    const finalDuration = 0; // TODO: compute from trimmed clips or ask user
    const usedSpaceAfterLabelingGb = 0; // TODO: compute from file sizes after labeling

    try {
        const data = await api("/api/sheets/process/card/finish", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                cardName: sheetsCurrentCardId,
                finalDuration,
                usedSpaceAfterLabelingGb,
            }),
        });
        await refreshSheetsProcess(); // refresh to update card status
        sheetsCurrentCardId = null;
        setStatus("Card finished and updated in sheet", "ok");
    } catch (error) {
        setStatus(`Failed to finish card: ${error.message}`, "error");
    }
}

refreshSheetsProcess().catch(() => { });
cardPushSheetsBtn.addEventListener("click",async () => {
    if (!sheetsCurrentCardId) {
        setStatus("No card is currently selected", "error");
        return;
    }
    if (confirm(`Finish card ${sheetsCurrentCardId}? This will update the sheet.`)) {
        try {
            await finishCurrentCard();
        } catch (error) {
            if (error.message.includes("removed from the sheet")) {
                setStatus("Card was deleted from the sheet – refreshing...", "error");
                await refreshSheetsProcess();
            }
        }
    }
});