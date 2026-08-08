// Card tracking via Supabase-backed /api/cards
let cardsProcess = null;
let cardsCurrentCardId = null;

const cardPushBtn = document.getElementById("push-card-data-btn");

async function refreshCardsProcess() {
  try {
    const data = await api("/api/cards/today");
    cardsProcess = data.currentProcess || { cards: data.cards || [] };
    const count = (cardsProcess.cards || []).length;
    setStatus(`Today: ${count} card${count === 1 ? "" : "s"}`, "ok");

    const currentPath = typeof scanTargetPath === "function" ? scanTargetPath() : null;
    if (currentPath && cardsProcess) {
      const found = cardsProcess.cards.find(
        (c) => (c.cardPath || c.card_path) === currentPath,
      );
      cardsCurrentCardId = found ? found.cardName || found.card_name : null;
    }
    return data;
  } catch (error) {
    setStatus(error.message, "error");
    return null;
  }
}

async function addCardToDb(cardPath, cardName) {
  if (!cardPath) return;
  try {
    const data = await api("/api/cards/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cardPath, cardName }),
    });
    cardsCurrentCardId = data.card?.card_name || data.card?.cardName || cardName;
    if (data.already_exists) {
      setStatus(`Card "${cardName}" already tracked today`, "ok");
    } else {
      setStatus(`Card "${cardName}" saved to database`, "ok");
    }
    await refreshCardsProcess();
  } catch (error) {
    setStatus(`Failed to save card: ${error.message}`, "error");
  }
}

async function finishCurrentCard() {
  if (!cardsCurrentCardId) {
    setStatus("No active card to finish", "error");
    return;
  }
  try {
    await api("/api/cards/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cardName: cardsCurrentCardId }),
    });
    await refreshCardsProcess();
    cardsCurrentCardId = null;
    setStatus("Card finished and updated in database", "ok");
  } catch (error) {
    setStatus(`Failed to finish card: ${error.message}`, "error");
  }
}

// Back-compat aliases for eager.js callers
const addCardToSheets = addCardToDb;
const refreshSheetsProcess = refreshCardsProcess;

refreshCardsProcess().catch(() => {});
if (cardPushBtn) {
  cardPushBtn.addEventListener("click", async () => {
    if (!cardsCurrentCardId) {
      setStatus("No card is currently selected", "error");
      return;
    }
    if (confirm(`Finish card ${cardsCurrentCardId}? This will update the database and daily summary.`)) {
      await finishCurrentCard();
    }
  });
}
