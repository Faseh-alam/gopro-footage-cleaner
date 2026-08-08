import { cn } from "@/lib/utils";
import { Badge } from "@/components/wc/panel";
import type { ReviewController } from "./useReviewController";

export function PlayerPanel({ c }: { c: ReviewController }) {
  const video = c.currentVideo();
  const ann = c.currentAnnotation();
  const duration = c.duration || video?.duration || 0;
  const segments = ann?.segments || [];

  const covered = segments.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
  const coverage = duration > 0 ? Math.min(100, Math.round((covered / duration) * 100)) : 0;
  const playFraction = duration > 0 ? Math.min(1, c.scrubTime / duration) : 0;

  return (
    <section className="panel-surface flex min-h-0 flex-col">
      <header className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="eyebrow mb-0.5">Now reviewing</div>
          <h2 className="truncate text-sm font-semibold">{video?.name || "No file loaded"}</h2>
        </div>
        <div className="flex shrink-0 items-center gap-2 font-mono text-[11px] text-muted-foreground">
          <Badge tone={coverage >= 100 ? "ok" : "muted"}>{coverage}% covered</Badge>
          {c.previewNote ? <Badge tone="muted">{c.previewNote}</Badge> : null}
          <span>{c.playbackRate.toFixed(1)}×</span>
          <span>
            {c.formatTime(c.scrubTime)} / {c.formatTime(duration)}
          </span>
        </div>
      </header>

      <div
        ref={c.playerWrapRef}
        tabIndex={0}
        className="relative bg-black outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <video
          ref={c.videoRef}
          playsInline
          muted
          preload="metadata"
          className="aspect-video w-full bg-black object-contain"
        />

        {c.taskSelectionMode && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center gap-1 bg-background/70 text-center backdrop-blur-[2px]">
            <strong className="text-sm">Select task</strong>
            <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
              Type to filter · ↑↓ select · Enter assign
            </span>
          </div>
        )}

        {c.loadingVideo && (
          <div className="pointer-events-none absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            Loading…
          </div>
        )}

        {/* Scrub track */}
        <div
          className="relative h-8 cursor-pointer border-t border-border bg-surface-2"
          onClick={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            c.seekToFraction((e.clientX - rect.left) / rect.width);
          }}
        >
          {segments.map((s, i) => (
            <div
              key={i}
              title={`${s.kind}${s.task ? ` · ${s.task}` : ""}`}
              className={cn(
                "absolute inset-y-0",
                s.kind === "work" ? "bg-accent/35" : "bg-destructive/30",
              )}
              style={{
                left: duration ? `${(s.start / duration) * 100}%` : "0%",
                width: duration ? `${Math.max(0.4, ((s.end - s.start) / duration) * 100)}%` : "0%",
              }}
            />
          ))}
          {ann?.pendingWork && duration > 0 && (
            <div
              className="absolute inset-y-0 border-x border-warning bg-warning/25"
              style={{
                left: `${(ann.pendingWork.start / duration) * 100}%`,
                width: `${Math.max(0.4, ((ann.pendingWork.end - ann.pendingWork.start) / duration) * 100)}%`,
              }}
            />
          )}
          <div
            className="absolute inset-y-0 w-px bg-foreground"
            style={{ left: `${playFraction * 100}%` }}
          />
        </div>
      </div>

      {ann?.pendingWork && (
        <p className="border-b border-border bg-warning/10 px-4 py-2 font-mono text-[11px] text-warning">
          Pending work {c.formatTime(ann.pendingWork.start)} → {c.formatTime(ann.pendingWork.end)} — pick a task and
          press Enter
        </p>
      )}

      <ul className="max-h-52 min-h-0 flex-1 divide-y divide-border overflow-auto">
        {segments.length === 0 && (
          <li className="px-4 py-6 text-center text-xs text-muted-foreground">
            No segments yet — press T to end a work segment, G to mark garbage.
          </li>
        )}
        {segments.map((s, i) => (
          <li key={i} className="flex items-center justify-between gap-3 px-4 py-2 text-xs">
            <span className="flex items-center gap-2">
              <Badge tone={s.kind === "work" ? "accent" : "danger"}>{s.kind}</Badge>
              <span className="truncate text-muted-foreground">{s.task || "—"}</span>
            </span>
            <span className="font-mono text-[11px] text-muted-foreground">
              {c.formatTime(s.start)} → {c.formatTime(s.end)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
