import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

export type SheetsIndicatorState = "connecting" | "connected" | "partial" | "error";

export function useSheetsIntegration(setStatus: (msg: string, kind?: "" | "ok" | "error") => void) {
  const [indicator, setIndicator] = useState<SheetsIndicatorState>("connecting");
  const [statusText, setStatusText] = useState("Connecting...");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalStatusHtml, setModalStatusHtml] = useState<{ hasCreds: boolean; hasSheet: boolean; connOk: boolean | null; connError: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<{ message: string; ok: boolean } | null>(null);

  const currentCardIdRef = useRef<string | null>(null);
  const processRef = useRef<any>(null);

  const testConnection = useCallback(async () => {
    try {
      const data = await api("/api/sheets/test");
      return data.ok === true;
    } catch {
      return false;
    }
  }, []);

  const updateIndicator = useCallback(async () => {
    try {
      const status = await api("/api/sheets/status");
      const hasCreds = status.credentialsExists === true;
      const hasSheet = status.spreadsheetIdExists === true;
      if (!hasCreds) {
        setIndicator("error");
        setStatusText("Disconnected");
        return;
      }
      if (!hasSheet) {
        setIndicator("partial");
        setStatusText("Disconnected");
        return;
      }
      const connected = await testConnection();
      if (connected) {
        setIndicator("connected");
        setStatusText("Connected");
      } else {
        setIndicator("error");
        setStatusText("Invalid");
      }
    } catch {
      setIndicator("error");
      setStatusText("Error");
    }
  }, [testConnection]);

  const refreshProcess = useCallback(
    async (cardName?: string) => {
      try {
        const data = await api("/api/sheets/process/current");
        processRef.current = data.currentProcess;
        if (data.currentProcess) {
          setStatus(`Google Sheet: ${data.currentProcess.sheetName} (${data.currentProcess.cards.length} cards)`, "ok");
        } else {
          setStatus("No active sheet process", "error");
        }
        if (cardName && data.currentProcess) {
          // Card names are unique; paths are shared across cards.
          const found = data.currentProcess.cards.find((c: any) => c.cardName === cardName);
          currentCardIdRef.current = found ? found.cardName : null;
        }
        return data;
      } catch (error: any) {
        setStatus(error.message, "error");
        return null;
      }
    },
    [setStatus],
  );


  const addCardToSheets = useCallback(
    async (cardPath: string, cardName: string) => {
      if (!cardPath) return;
      try {
        const data = await api("/api/sheets/process/card", {
          method: "POST",
          body: JSON.stringify({ cardPath, cardName }),
        });
        if (data.already_exists) {
          currentCardIdRef.current = data.card.card_name;
          setStatus(`Card "${data.card.card_name}" already in sheet`, "ok");
        } else {
          currentCardIdRef.current = data.card.card_name;
        }
        await refreshProcess(cardName);

        setStatus(`Card "${cardName}" added to sheet (row ${data.sheetRowIndex})`, "ok");
      } catch (error: any) {
        setStatus(`Failed to add card to sheet: ${error.message}`, "error");
      }
    },
    [refreshProcess, setStatus],
  );

  const finishCurrentCard = useCallback(async () => {
    if (!currentCardIdRef.current) {
      setStatus("No active card to finish", "error");
      return;
    }
    try {
      await api("/api/sheets/process/card/finish", {
        method: "POST",
        body: JSON.stringify({ cardName: currentCardIdRef.current, finalDuration: 0, usedSpaceAfterLabelingGb: 0 }),
      });
      await refreshProcess();
      currentCardIdRef.current = null;
      setStatus("Card finished and updated in sheet", "ok");
    } catch (error: any) {
      setStatus(`Failed to finish card: ${error.message}`, "error");
      if (String(error.message).includes("removed from the sheet")) {
        setStatus("Card was deleted from the sheet – refreshing...", "error");
        await refreshProcess();
      }
    }
  }, [refreshProcess, setStatus]);

  const pushCardData = useCallback(async () => {
    if (!currentCardIdRef.current) {
      setStatus("No card is currently selected", "error");
      return;
    }
    if (typeof window !== "undefined") {
      const ok = window.confirm(`Finish card ${currentCardIdRef.current}? This will update the sheet.`);
      if (!ok) return;
    }
    await finishCurrentCard();
  }, [finishCurrentCard, setStatus]);

  const openModal = useCallback(async () => {
    setModalOpen(true);
    setResult(null);
    setModalStatusHtml(null);
    try {
      const status = await api("/api/sheets/status");
      const hasCreds = status.credentialsExists === true;
      const hasSheet = status.spreadsheetIdExists === true;
      let connOk: boolean | null = null;
      let connError = "";
      if (hasCreds && hasSheet) {
        try {
          const test = await api("/api/sheets/test");
          connOk = Boolean(test.ok);
          if (!connOk) connError = test.error || "unknown error";
        } catch (error: any) {
          connOk = false;
          connError = error.message;
        }
      }
      setModalStatusHtml({ hasCreds, hasSheet, connOk, connError });
    } catch {
      /* ignore */
    }
    updateIndicator();
  }, [updateIndicator]);

  const closeModal = useCallback(() => {
    setModalOpen(false);
    setResult(null);
  }, []);

  const submitSetup = useCallback(
    async (formData: FormData) => {
      setSubmitting(true);
      setResult({ message: "Connecting...", ok: true });
      try {
        const response = await fetch("/api/sheets/setup", { method: "POST", body: formData });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Setup failed");
        setResult({ message: `✅ Connected! Spreadsheet ID: ${data.spreadsheetId}`, ok: true });
        await updateIndicator();
        if (typeof window !== "undefined") {
          setTimeout(() => window.location.reload(), 1500);
        }
      } catch (error: any) {
        setResult({ message: `❌ ${error.message}`, ok: false });
      } finally {
        setSubmitting(false);
      }
    },
    [updateIndicator],
  );

  useEffect(() => {
    updateIndicator();
    refreshProcess().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    indicator,
    statusText,
    modalOpen,
    modalStatusHtml,
    submitting,
    result,
    openModal,
    closeModal,
    submitSetup,
    addCardToSheets,
    refreshProcess,
    pushCardData,
    currentCardIdRef,
  };
}

export type SheetsIntegration = ReturnType<typeof useSheetsIntegration>;
