import { Logo } from "@/components/wc/logo";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dropdown } from "@/components/wc/dropdown";
import { cn } from "@/lib/utils";
import { useReviewController } from "@/components/review/useReviewController";
import { useCardTracking } from "@/components/review/useSheetsIntegration";
import { PlayerPanel } from "@/components/review/player-panel";
import { TaskPanel } from "@/components/review/task-panel";
import { FootageList, TrimProgress } from "@/components/review/footage-list";
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
  ["← →", "Speed −0.5× / +0.5×"],
  [", .", "−1s / +1s"],
  ["T", "End work segment + select task"],
  ["Enter", "Assign task to pending work"],
  ["G", "Mark garbage to playhead"],
  ["U", "Delete last markup"],
  ["N", "Next unfinished video"],
  ["Home", "Jump to 0:00"],
];

function ReviewPage() {
  const c = useReviewController();
  const cards = useCardTracking(c.setStatus);

  // First encounter today → register card in DB (server skips if already saved today).
  const addedCardRef = useRef<string | null>(null);
  useEffect(() => {
    const path = c.sdCardValue;
    if (!path) return;
    const card = c.sdCards.find((x: any) => (x.scan_path || x.path) === path);
    const name = String(card?.id || card?.label || "").trim();
    // Only register detected SD cards (C####), never folder picks / empty selection.
    if (!name || !/^C\d{4}$/i.test(name)) return;
    if (addedCardRef.current === name) return;
    addedCardRef.current = name;
    cards.addCard(path, name);
  }, [c.sdCardValue, c.sdCards, cards.addCard]);


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
      else if (key === "t") c.markWork();
      else if (key === "g") c.markGarbage();
      else if (key === "u") c.undoSegment();
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

  const indicatorTone =
    cards.indicator === "connected"
      ? "bg-success"
      : cards.indicator === "connecting"
        ? "bg-muted-foreground"
        : "bg-destructive";

  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border px-6 py-5">
        <div className="flex items-center gap-3">
          <Logo className="size-7" />
          <div>
            <div className="eyebrow">World Context</div>
            <h1 className="mt-1 text-lg font-bold tracking-tight">Review Station</h1>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Dropdown
            className="w-52"
            size="sm"
            value={c.sdCardValue}
            onChange={(v) => c.setSdCardValue(v)}
            options={c.sdCards.map((card: any) => ({
              value: card.scan_path || card.path || "",
              label: card.label || card.id || card.path || "Card",
            }))}
            placeholder="SD card…"
          />
          <Button size="sm" variant="ghost" onClick={() => c.refreshSdCards({ autoScan: true })}>
            Detect
          </Button>
          <Button size="sm" variant="outline" onClick={() => c.chooseFootageFolder()}>
            Open footage
          </Button>
          <Button size="sm" variant="outline" onClick={() => c.scanSource()}>
            Scan
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              const path = c.sdCardValue;
              const card = c.sdCards.find((x: any) => (x.scan_path || x.path) === path);
              const name =
                String(card?.id || card?.label || cards.currentCardIdRef.current || "").trim();
              await c.finishCard();
              if (name) await cards.finishCurrentCard(name);
            }}
          >
            Finish card
          </Button>
          <Button size="sm" variant="outline" onClick={() => cards.pushCardData()}>
            Save card data
          </Button>
          <Button size="sm" variant="ghost" disabled>
            <span className={cn("size-1.5 rounded-full", indicatorTone)} aria-hidden />
            {cards.statusText}
          </Button>
          <Button size="sm" variant="default" onClick={() => c.queueClips()} disabled={c.busy}>
            Queue clips
          </Button>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 font-mono text-[12px] uppercase tracking-[0.14em] text-[#b96d72] transition-opacity hover:opacity-75 ml-2"
          >
            Cleaner <ArrowUpRight className="size-3.5" />
          </Link>
        </div>
      </header>

      <p className="border-b border-border px-6 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        Space play · ← → speed · , . scrub 1s · T ends work · G garbage · Enter assigns
      </p>

      <main className="grid min-h-0 flex-1 gap-4 p-6 lg:grid-cols-[minmax(0,1.7fr)_minmax(320px,1fr)]">
        <div className="grid min-h-0 content-start gap-4">
          <PlayerPanel c={c} />
          <BatchPanel c={c} />
        </div>

        <aside className="panel-surface flex min-h-0 flex-col">
          <TaskPanel c={c} />
          <FootageList c={c} />
          <TrimProgress c={c} />
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
    </div>
  );
}
