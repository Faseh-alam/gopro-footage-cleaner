import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

export type CardsIndicatorState = "connecting" | "connected" | "error";

export function useCardTracking(setStatus: (msg: string, kind?: "" | "ok" | "error") => void) {
  const [indicator, setIndicator] = useState<CardsIndicatorState>("connecting");
  const [statusText, setStatusText] = useState("Connecting...");
  const currentCardIdRef = useRef<string | null>(null);
  const registeringRef = useRef<string | null>(null);

  const updateIndicator = useCallback(async () => {
    try {
      const status = await api("/api/cards/status");
      if (!status.configured) {
        setIndicator("error");
        setStatusText("DB not configured");
        return;
      }
      const test = await api("/api/cards/test");
      if (test.ok) {
        setIndicator("connected");
        setStatusText("DB connected");
      } else {
        setIndicator("error");
        setStatusText("DB error");
      }
    } catch {
      setIndicator("error");
      setStatusText("DB error");
    }
  }, []);

  const refreshProcess = useCallback(
    async (cardName?: string) => {
      try {
        const data = await api("/api/cards/today");
        const cards = data.cards || [];
        setStatus(`Today: ${cards.length} card${cards.length === 1 ? "" : "s"}`, "ok");
        if (cardName) {
          const found = cards.find(
            (c: any) => String(c.cardName || c.card_name || "").toLowerCase() === cardName.toLowerCase(),
          );
          currentCardIdRef.current = found ? found.cardName || found.card_name : null;
        }
        return data;
      } catch (error: any) {
        setStatus(error.message, "error");
        return null;
      }
    },
    [setStatus],
  );

  const addCard = useCallback(
    async (cardPath: string, cardName: string) => {
      if (!cardPath || !cardName) return;
      const key = `${cardName}::${cardPath}`;
      if (registeringRef.current === key) return;
      registeringRef.current = key;
      try {
        setStatus(`Saving card "${cardName}" to database…`);
        const data = await api("/api/cards/register", {
          method: "POST",
          body: JSON.stringify({ cardPath, cardName }),
        });
        currentCardIdRef.current = data.card?.card_name || data.card?.cardName || cardName;
        if (data.already_exists) {
          setStatus(`Card "${cardName}" already tracked today`, "ok");
        } else {
          setStatus(`Card "${cardName}" saved to database`, "ok");
        }
        await refreshProcess(cardName);
      } catch (error: any) {
        setStatus(`Failed to save card: ${error.message}`, "error");
      } finally {
        if (registeringRef.current === key) registeringRef.current = null;
      }
    },
    [refreshProcess, setStatus],
  );

  const finishCurrentCard = useCallback(
    async (cardName?: string) => {
      const name = (cardName || currentCardIdRef.current || "").trim();
      if (!name) {
        setStatus("No active card to finish", "error");
        return false;
      }
      try {
        setStatus(`Finishing card "${name}" in database…`);
        await api("/api/cards/finish", {
          method: "POST",
          body: JSON.stringify({ cardName: name }),
        });
        await refreshProcess();
        if (currentCardIdRef.current === name) currentCardIdRef.current = null;
        setStatus(`Card "${name}" finished and summary updated`, "ok");
        return true;
      } catch (error: any) {
        setStatus(`Failed to finish card: ${error.message}`, "error");
        return false;
      }
    },
    [refreshProcess, setStatus],
  );

  const pushCardData = useCallback(async () => {
    const name = currentCardIdRef.current;
    if (!name) {
      setStatus("No card is currently selected", "error");
      return;
    }
    if (typeof window !== "undefined") {
      const ok = window.confirm(
        `Finish card ${name}? This will update the database and daily summary.`,
      );
      if (!ok) return;
    }
    await finishCurrentCard(name);
  }, [finishCurrentCard, setStatus]);

  useEffect(() => {
    updateIndicator();
    // Ensures today's empty daily summary exists even with no SD card.
    refreshProcess().catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return {
    indicator,
    statusText,
    addCard,
    refreshProcess,
    finishCurrentCard,
    pushCardData,
    currentCardIdRef,
  };
}

export type CardTracking = ReturnType<typeof useCardTracking>;

/** @deprecated Use useCardTracking */
export const useSheetsIntegration = useCardTracking;
export type SheetsIntegration = CardTracking;
export type SheetsIndicatorState = CardsIndicatorState;
