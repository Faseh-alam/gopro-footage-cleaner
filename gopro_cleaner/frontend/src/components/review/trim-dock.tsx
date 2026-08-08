import { useEffect, useState } from "react";
import { Loader2, Minus, Scissors, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { ProgressBar } from "@/components/wc/panel";
import type { ReviewController } from "./useReviewController";

const ACTIVE = new Set(["queued", "running"]);

/**
 * Floating dock for background trim jobs.
 * - Minimize collapses to a small pill that reopens the dock.
 * - Close (only once nothing is active) dismisses until new trims start.
 * - Cancel a single job, or cancel every active job at once.
 */
export function TrimDock({ c }: { c: ReviewController }) {
  const { active, jobs, etaTotal } = c.globalTrim;
  const [minimized, setMinimized] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  // A new batch of work re-opens the dock even after a previous dismiss.
  useEffect(() => {
    if (active > 0) setDismissed(false);
  }, [active]);

  const hasJobs = jobs.length > 0;
  if (!hasJobs || dismissed) return null;

  const done = jobs.filter((j) => j.status === "completed").length;
  const failed = jobs.filter((j) => j.status === "failed").length;
  const cancelled = jobs.filter((j) => j.status === "cancelled").length;
  const overall = jobs.length
    ? jobs.reduce((a, j) => a + (j.status === "completed" ? 100 : Number(j.progress) || 0), 0) /
      jobs.length
    : 0;
  const allDone = active === 0;

  if (minimized) {
    return (
      <button
        type="button"
        onClick={() => setMinimized(false)}
        className="bg-background fixed bottom-4 right-4 z-50 flex items-center gap-2 rounded-full border border-border bg-surface-1 px-3 py-2 text-xs font-medium shadow-lg transition-colors hover:bg-surface-2"
        title="Show background trims"
      >
        {active > 0 ? (
          <Loader2 className="size-3.5 animate-spin text-accent" />
        ) : (
          <Scissors className="size-3.5 text-muted-foreground" />
        )}
        {active > 0 ? `Trimming ${active}… ${Math.round(overall)}%` : "Trims finished"}
      </button>
    );
  }

  const visible = [...jobs]
    .sort((a, b) => {
      const rank = (s: string) => (s === "running" ? 0 : s === "queued" ? 1 : s === "failed" ? 2 : 3);
      return rank(a.status) - rank(b.status);
    })
    .slice(0, 12);

  return (
    <div className="bg-background fixed bottom-4 right-4 z-50 w-[22rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-border bg-surface-1 shadow-2xl">
      <header className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {active > 0 ? (
            <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" />
          ) : (
            <Scissors className="size-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate text-xs font-semibold">Background trims</span>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {active > 0 && (
            <button
              type="button"
              onClick={() => c.cancelAllTrims()}
              className="rounded-sm px-1.5 py-0.5 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/15"
              title="Cancel all running and queued trims"
            >
              Cancel all
            </button>
          )}
          <button
            type="button"
            onClick={() => setMinimized(true)}
            className="grid size-6 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground"
            title="Minimize"
            aria-label="Minimize background trims"
          >
            <Minus className="size-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setDismissed(true)}
            disabled={!allDone}
            className={cn(
              "grid size-6 place-items-center rounded-sm transition-colors",
              allDone
                ? "text-muted-foreground hover:bg-surface-2 hover:text-foreground"
                : "cursor-not-allowed text-muted-foreground/30",
            )}
            title={allDone ? "Close" : "Cancel or wait for trims to finish before closing"}
            aria-label="Close background trims"
          >
            <X className="size-3.5" />
          </button>
        </div>
      </header>

      <div className="px-3 py-2">
        <div className="mb-1.5 flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
          <span>
            {active > 0 ? `${active} active · ~${c.formatDurationShort(etaTotal)}` : "All done"}
          </span>
          <span>
            {done ? `${done} done` : ""}
            {failed ? ` · ${failed} failed` : ""}
            {cancelled ? ` · ${cancelled} cancelled` : ""}
          </span>
        </div>
        <ProgressBar value={overall} />

        <ul className="mt-2 grid max-h-64 gap-1.5 overflow-auto">
          {visible.map((job) => {
            const isActive = ACTIVE.has(job.status);
            return (
              <li key={job.job_id} className="grid gap-0.5">
                <div className="flex items-center justify-between gap-2 text-[11px]">
                  <span className="min-w-0 truncate text-muted-foreground">
                    {c.basenamePath(job.output || job.source_path || "")}
                    {job.task ? <span className="ml-1 text-[10px] opacity-70">· {job.task}</span> : null}
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    <span
                      className={cn(
                        "font-mono text-[10px]",
                        job.status === "failed" && "text-destructive",
                        job.status === "completed" && "text-success",
                        job.status === "cancelled" && "text-muted-foreground/60",
                      )}
                      title={job.error || job.message || undefined}
                    >
                      {job.status === "running"
                        ? `${Math.round(Number(job.progress) || 0)}%`
                        : job.status}
                    </span>
                    {isActive && (
                      <button
                        type="button"
                        onClick={() => c.cancelTrim(job.job_id)}
                        className="grid size-5 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-destructive/15 hover:text-destructive"
                        title="Cancel this trim"
                        aria-label="Cancel this trim"
                      >
                        <X className="size-3" />
                      </button>
                    )}
                  </span>
                </div>
                {job.status === "running" && <ProgressBar value={Number(job.progress) || 0} />}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
