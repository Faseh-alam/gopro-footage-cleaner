import { Logo } from "@/components/wc/logo";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Dropdown } from "@/components/wc/dropdown";
import { cn } from "@/lib/utils";
import { useAuth } from "@/components/auth/AuthProvider";
import { UserAccountMenu } from "@/components/auth/UserAccountMenu";
import { useReviewController } from "@/components/review/useReviewController";
import { useCardTracking } from "@/components/review/useSheetsIntegration";
import { PlayerPanel } from "@/components/review/player-panel";
import { TaskPanel } from "@/components/review/task-panel";
import { FootageList } from "@/components/review/footage-list";
import { TrimDock } from "@/components/review/trim-dock";
import { BatchPanel } from "@/components/review/batch-panel";

export const Route = createFileRoute("/review")({
  head: () => ({
    meta: [
      { title: "Review Station — World Context" },
      {
        name: "description",
        content:
          "Annotate GoPro footage into work and garbage segments, assign tasks and queue trims for physical-AI datasets.",
      },
      { property: "og:title", content: "Review Station — World Context" },
      {
        property: "og:description",
        content:
          "Annotate GoPro footage into work and garbage segments, assign tasks and queue trims for physical-AI datasets.",
      },
    ],
  }),
  component: ReviewPage,
});

const KEYS: [string, string][] = [
  ["Space", "Play / pause (resets to 1×)"],
  ["← →", "Speed −0.5× / +0.5× (max 2× on original, 4× on 720p)"],
  ["[ ]", "Speed −0.5× / +0.5×"],
  [", .", "−1s / +1s (stops at end)"],
  ["I / O", "Mark share-clip in / out"],
  ["T", "End work segment + select task"],
  ["Enter", "Assign task to pending work"],
  ["A", "Focus New task field"],
  ["G", "Mark garbage to playhead"],
  ["U", "Delete last markup"],
  ["N", "Next unfinished (current must be 100% covered)"],
  ["Home", "Jump to 0:00"],
];

function ReviewPage() {
  const c = useReviewController();
  const cards = useCardTracking(c.setStatus);
  const { refreshToday } = useAuth();
  const [updateState, setUpdateState] = useState<"idle" | "pulling" | "restarting">("idle");

  // One-click updater: pull the checked-out branch from GitHub, let the backend
  // relaunch itself (run.bat / run.sh), then reload once it's back.
  const runUpdate = async () => {
    if (updateState !== "idle") return;
    if (!window.confirm("Pull the latest version from GitHub and restart the app?")) return;
    setUpdateState("pulling");
    c.setStatus("Checking GitHub for updates…");
    try {
      const res = await api("/api/update", { method: "POST" });
      if (!res.restarting) {
        setUpdateState("idle");
        const detail = `${res.branch} @ ${res.after}`;
        c.setStatus(`Already up to date (${detail})`, "ok");
        toast.success("Already up to date", { description: detail });
        return;
      }
      setUpdateState("restarting");
      c.setStatus(`Updated ${res.branch}: ${res.before} → ${res.after} — restarting server…`, "ok");
      // The server exits shortly after replying and run.bat/run.sh relaunches
      // it (deps reinstall included). Poll until it's back, then hard-reload
      // so the browser picks up the new UI bundle.
      await new Promise((resolve) => setTimeout(resolve, 4000));
      const deadline = Date.now() + 180_000;
      while (Date.now() < deadline) {
        try {
          await api("/api/health");
          window.location.reload();
          return;
        } catch {
          await new Promise((resolve) => setTimeout(resolve, 1500));
        }
      }
      setUpdateState("idle");
      c.setStatus("Server did not come back — start it with run.bat, then reload this page", "error");
    } catch (error: any) {
      setUpdateState("idle");
      c.setStatus(error?.message || "Update failed", "error");
    }
  };

  // On reload: if GitHub has newer commits than this checkout, offer Update.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const check = await api<{
          behind?: boolean;
          local?: string;
          remote?: string;
          branch?: string;
          version?: string;
        }>("/api/update/check");
        if (cancelled || !check?.behind) return;
        toast.message("New version available", {
          description: `${check.branch || "branch"} ${check.local} → ${check.remote}`,
          duration: 12_000,
          action: {
            label: "Update",
            onClick: () => {
              void runUpdate();
            },
          },
        });
      } catch {
        /* offline / no git — ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
    // Intentionally once per page load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // First encounter today → register card in DB (server derives C#### from camera serial).
  const addedCardRef = useRef<string | null>(null);
  useEffect(() => {
    const path = c.sdCardValue;
    if (!path) return;
    const card = c.sdCards.find((x: any) => (x.scan_path || x.path) === path);
    const name = String(card?.id || card?.label || "").trim();
    // sdCards only contains detector hits (DCIM/###GOPRO) — never folder picks.
    if (!name || !card) return;
    if (addedCardRef.current === path) return;
    addedCardRef.current = path;
    (async () => {
      await cards.addCard(path, name);
      refreshToday().catch(() => undefined);
    })();
  }, [c.sdCardValue, c.sdCards, cards.addCard, refreshToday]);


  // Global review shortcuts — mirror the original keyboard model.
  useEffect(() => {
    const isField = (t: EventTarget | null) =>
      t instanceof HTMLElement && t.matches("input, textarea, select");

    const onKey = (event: KeyboardEvent) => {
      const target = event.target;
      const inTaskSearch = target === c.taskSearchRef.current;

      if (inTaskSearch) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const matches = c.orderedTaskGroups().matches;
          if (!matches.length) return;
          const at = matches.indexOf(c.selectedTaskValue);
          const next = Math.min(
            matches.length - 1,
            Math.max(0, (at < 0 ? 0 : at) + (event.key === "ArrowDown" ? 1 : -1)),
          );
          c.setSelectedTaskValue(matches[next] ?? "");
          return;
        }
        if (event.key === "Enter") {
          event.preventDefault();
          // Filter field only filters — never invent a task from typed text.
          const query = c.taskSearch.trim();
          const matches = c.orderedTaskGroups().matches;
          const exact = matches.find((t) => t.toLowerCase() === query.toLowerCase());
          const highlighted =
            c.selectedTaskValue && matches.includes(c.selectedTaskValue)
              ? c.selectedTaskValue
              : "";
          const task =
            exact || highlighted || (!query ? c.lastLabelTask.trim() : "") || "";
          if (!task) {
            c.setStatus(
              query
                ? "No matching task — pick from the list or add via New task"
                : "Choose a task first",
              "error",
            );
            return;
          }
          c.setSelectedTaskValue(task);
          if (c.currentAnnotation()?.pendingWork) c.labelCurrentClip(task);
          else c.leaveTaskSearch({ clear: false });
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          c.leaveTaskSearch({ clear: true });
          return;
        }
        if (event.key.length === 1 && !(event.key.toLowerCase() === "u" && !c.taskSearch.trim())) return;
      } else if (isField(target)) {
        return;
      }

      const key = event.key.toLowerCase();
      let handled = true;

      if (event.key === "ArrowLeft" || event.key === "[" || event.key === "{") c.bumpPlaybackRate(-0.5);
      else if (event.key === "ArrowRight" || event.key === "]" || event.key === "}") c.bumpPlaybackRate(0.5);
      else if (event.key === ",") c.fineTune(-1);
      else if (event.key === ".") c.fineTune(1);
      else if (key === "i") c.markShareIn();
      else if (key === "o") c.markShareOut();
      else if (key === "t") c.markWork();
      else if (key === "g") c.markGarbage();
      else if (key === "u") c.undoSegment();
      else if (key === "a") c.focusNewTask();
      else if (event.key === "Home") c.jumpToClipStart();
      else if (key === "n") c.finishCleaningFile();
      else if (event.key === " ") c.togglePlay();
      else if (event.key === "Enter" && !isField(target)) c.labelCurrentClip();
      else handled = false;

      if (handled) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };

    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [c]);

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="border-b border-border px-6 py-4">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
          <div className="flex min-w-0 items-center gap-3">
            <Logo className="size-7 shrink-0" />
            <div className="min-w-0">
              <div className="eyebrow">World Context</div>
              <h1 className="text-lg font-bold tracking-tight">Review Station</h1>
            </div>
          </div>

          <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-x-3 gap-y-2">
            <div className="flex flex-wrap items-center gap-1.5">
              <Dropdown
                className="w-44"
                size="sm"
                value={c.sdCardValue}
                onChange={(v) => c.setSdCardValue(v)}
                options={c.sdCards.map((card: any) => ({
                  value: card.scan_path || card.path || "",
                  label: card.label || card.id || card.path || "Card",
                }))}
                placeholder="SD card…"
              />
              <Button
                size="sm"
                variant="outline"
                disabled={c.detecting}
                title="Detect connected SD cards and scan their footage"
                onClick={() => c.refreshSdCards({ autoScan: true })}
              >
                {c.detecting ? "Scanning…" : "Scan"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => c.chooseFootageFolder()}
                title="Open a folder on this computer or an external drive"
              >
                Open
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={runUpdate}
                disabled={updateState !== "idle"}
                title="Pull the latest version from GitHub and restart the app"
              >
                <RefreshCw className={cn("size-3.5", updateState !== "idle" && "animate-spin")} />
                {updateState === "idle"
                  ? "Update"
                  : updateState === "pulling"
                    ? "Updating…"
                    : "Restarting…"}
              </Button>
            </div>

            <div className="flex items-center gap-2 border-l border-border pl-3">
              <UserAccountMenu />
            </div>

            <nav className="flex items-center gap-3 border-l border-border pl-3 font-mono text-[11px] uppercase tracking-[0.14em] text-[#b96d72]">
              <Link to="/metadata" className="inline-flex items-center gap-1 transition-opacity hover:opacity-75">
                Metadata <ArrowUpRight className="size-3" />
              </Link>
              <Link to="/" className="inline-flex items-center gap-1 transition-opacity hover:opacity-75">
                Cleaner <ArrowUpRight className="size-3" />
              </Link>
            </nav>
          </div>
        </div>
      </header>

      <main className="grid min-h-0 flex-1 gap-4 p-6 lg:grid-cols-[minmax(0,1.7fr)_minmax(320px,1fr)]">
        <div className="grid min-h-0 content-start gap-4">
          <PlayerPanel c={c} />
          <BatchPanel c={c} />
        </div>

        <aside className="panel-surface flex min-h-0 flex-col">
          <TaskPanel c={c} />
          <FootageList c={c} />
          <details className="border-b border-border px-4 py-3">
            <summary className="eyebrow cursor-pointer">Keys</summary>
            <ul className="mt-2 grid gap-1">
              {KEYS.map(([k, label]) => (
                <li key={k} className="flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
                  <kbd className="font-mono text-[10px] text-foreground">{k}</kbd>
                  <span className="truncate">{label}</span>
                </li>
              ))}
            </ul>
          </details>
        </aside>
      </main>

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-6 py-4 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        <span>{c.ffmpegHint || `IMU / GPMF preserved · App v${c.appVersion || "…"}`}</span>
        <span
          className={cn(
            "max-w-[55%] truncate normal-case tracking-normal",
            c.status.kind === "error" && "text-destructive",
            c.status.kind === "ok" && "text-success",
          )}
        >
          {c.status.message}
        </span>
      </footer>

      <TrimDock c={c} />
    </div>
  );
}
