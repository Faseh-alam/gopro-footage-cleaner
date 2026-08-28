import type { PointerEvent as ReactPointerEvent } from "react";
import { Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/wc/panel";
import { Button } from "@/components/ui/button";
import type { ReviewController } from "./useReviewController";
import { taskColor } from "./task-color";

export function PlayerPanel({
  c,
  highlightedScaleAiTask,
}: {
  c: ReviewController;
  highlightedScaleAiTask?: string;
}) {
  const video = c.currentVideo();
  const ann = c.currentAnnotation();
  const scaleAi = c.scaleAiMode ? c.currentScaleAi() : null;
  const duration = c.duration || video?.duration || 0;
  const segments = c.scaleAiMode ? [] : ann?.segments || [];
  // Latest-first indices into `segments` (chronological array).
  const recent = segments
    .map((s, i) => ({ s, i }))
    .slice(-2)
    .reverse();

  const scaleAiSegments = scaleAi?.segments || [];
  const pendingRange = ann?.pendingWork || c.scaleAiPending || null;
  const covered = c.scaleAiMode
    ? scaleAiSegments
        .filter((segment) => segment.type === "subtask")
        .reduce((acc, range) => acc + Math.max(0, range.end - range.start), 0)
    : segments.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
  const coverage = duration > 0 ? Math.min(100, Math.round((covered / duration) * 100)) : 0;
  const playFraction = duration > 0 ? Math.min(1, c.scrubTime / duration) : 0;
  const shareReady =
    c.shareClipIn != null && c.shareClipOut != null && c.shareClipOut > c.shareClipIn;

  // Zoom window around labeled work so 0.5–1s clips stay readable without lying about end times.
  const zoomBounds = (() => {
    if (!c.scaleAiMode || duration <= 0) return null;
    const points: number[] = [];
    for (const segment of scaleAiSegments) {
      points.push(Number(segment.start) || 0, Number(segment.end) || 0);
    }
    if (pendingRange) {
      points.push(pendingRange.start, pendingRange.end);
    }
    if (!points.length) return null;
    const rawStart = Math.min(...points);
    const rawEnd = Math.max(...points);
    const span = Math.max(0.01, rawEnd - rawStart);
    const pad = Math.max(1.5, span * 0.35);
    const start = Math.max(0, rawStart - pad);
    const end = Math.min(duration, rawEnd + pad);
    if (end - start < 0.05) return null;
    return { start, end };
  })();
  const zoomSpan = zoomBounds ? zoomBounds.end - zoomBounds.start : 0;
  const zoomLeft = (t: number) =>
    zoomBounds && zoomSpan > 0 ? ((t - zoomBounds.start) / zoomSpan) * 100 : 0;
  const zoomWidth = (start: number, end: number) =>
    zoomBounds && zoomSpan > 0 ? Math.max(0.15, ((end - start) / zoomSpan) * 100) : 0;
  const highlightedTask = String(highlightedScaleAiTask || "")
    .trim()
    .toLowerCase();
  const hasHighlightedSegments =
    highlightedTask.length > 0 &&
    scaleAiSegments.some(
      (segment) =>
        segment.type === "subtask" &&
        String(segment.label || "")
          .trim()
          .toLowerCase() === highlightedTask,
    );

  const seekInZoom = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!zoomBounds || zoomSpan <= 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    c.scheduleSeek(zoomBounds.start + fraction * zoomSpan, true);
  };

  const beginZoomScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    seekInZoom(event);
  };

  const continueZoomScrub = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) seekInZoom(event);
  };

  return (
    <section className="panel-surface flex min-h-0 flex-col">
      <header className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="eyebrow mb-0.5">Now reviewing</div>
          <h2 className="truncate text-sm font-semibold">{video?.name || "No file loaded"}</h2>
        </div>
        <div className="flex shrink-0 items-center gap-2 font-mono text-[11px] text-muted-foreground">
          <span className="text-foreground">
            {c.formatTime(c.scrubTime)} / {c.formatTime(duration)}
          </span>
          <span>{c.playbackRate.toFixed(1)}×</span>
          {c.previewNote ? <Badge tone="muted">{c.previewNote}</Badge> : null}
          <Badge tone={coverage >= 100 ? "ok" : "muted"}>
            {c.scaleAiMode
              ? `${scaleAiSegments.filter((s) => s.type === "subtask").length} subtasks`
              : `${coverage}% covered`}
          </Badge>
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

        {/* Freeze-frame shown while the source swaps (card original → SSD copy). */}
        <canvas
          ref={c.swapCanvasRef}
          aria-hidden
          style={{ display: "none" }}
          className="pointer-events-none absolute left-0 top-0 aspect-video w-full bg-black object-contain"
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
          <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center bg-black/55 backdrop-blur-[1px]">
            <div className="flex flex-col items-center gap-2 rounded-md border border-border/60 bg-background/90 px-4 py-3 shadow-lg">
              <Loader2 className="size-5 animate-spin text-accent" />
              <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                Loading video…
              </span>
            </div>
          </div>
        )}

        {/* The full-video timeline is retained for the standard review workflow only. */}
        {!c.scaleAiMode ? (
          <div
            className="pointer-events-none relative h-8 select-none border-t border-border bg-surface-2"
            aria-hidden
            title="Use , . or ← → to step through the timeline"
          >
            {segments.map((s, i) => {
              const color = s.kind === "work" ? taskColor(s.task) : null;
              const pct = duration > 0 ? Math.max(0, ((s.end - s.start) / duration) * 100) : 0;
              return (
                <div
                  key={s.id || i}
                  title={`${s.kind}${s.task ? ` · ${s.task}` : ""}`}
                  className={cn("absolute inset-y-0", s.kind !== "work" && "bg-destructive/30")}
                  style={{
                    left: duration ? `${(s.start / duration) * 100}%` : "0%",
                    width: `${pct}%`,
                    ...(color ? { backgroundColor: color.fill } : null),
                  }}
                />
              );
            })}
            {pendingRange && duration > 0 && (
              <div
                className="absolute top-2 bottom-2 border border-warning bg-warning/35"
                style={{
                  left: `${(pendingRange.start / duration) * 100}%`,
                  width: `${Math.max(
                    0.08,
                    ((pendingRange.end - pendingRange.start) / duration) * 100,
                  )}%`,
                }}
              />
            )}
            {shareReady && duration > 0 && (
              <div
                className="absolute inset-y-0 border-x border-sky-400/80 bg-sky-400/25"
                title="Share clip range"
                style={{
                  left: `${(c.shareClipIn! / duration) * 100}%`,
                  width: `${Math.max(0.4, ((c.shareClipOut! - c.shareClipIn!) / duration) * 100)}%`,
                }}
              />
            )}
            {c.shareClipIn != null && duration > 0 && (
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-sky-400"
                style={{ left: `${(c.shareClipIn / duration) * 100}%` }}
                title={`In ${c.formatTime(c.shareClipIn)}`}
              />
            )}
            {c.shareClipOut != null && duration > 0 && (
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-sky-300"
                style={{ left: `${(c.shareClipOut / duration) * 100}%` }}
                title={`Out ${c.formatTime(c.shareClipOut)}`}
              />
            )}
            <div
              className="absolute inset-y-0 w-px bg-foreground"
              style={{ left: `${playFraction * 100}%` }}
            />
          </div>
        ) : null}

        {c.scaleAiMode && zoomBounds ? (
          <div className="border-t border-border bg-surface px-3 py-2">
            <div className="mb-1 flex items-baseline justify-between gap-2 font-mono text-[10px] text-muted-foreground">
              <span className="eyebrow text-[9px]">Labeled region (zoomed)</span>
              <span>
                {c.formatTime(zoomBounds.start)} → {c.formatTime(zoomBounds.end)}
              </span>
            </div>
            <div
              className="relative h-10 touch-none select-none overflow-hidden rounded-sm border border-border bg-surface-2 cursor-ew-resize"
              onPointerDown={beginZoomScrub}
              onPointerMove={continueZoomScrub}
              onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
              onPointerCancel={(event) => {
                if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                  event.currentTarget.releasePointerCapture(event.pointerId);
                }
              }}
              role="slider"
              aria-label="Labeled region video position"
              aria-valuemin={zoomBounds.start}
              aria-valuemax={zoomBounds.end}
              aria-valuenow={Math.min(zoomBounds.end, Math.max(zoomBounds.start, c.scrubTime))}
              title="Click or drag to move through the labeled region"
            >
              {scaleAiSegments.map((segment) => {
                const isGarbage = segment.type === "garbage";
                const color = isGarbage ? null : taskColor(segment.label);
                const left = zoomLeft(segment.start);
                const width = zoomWidth(segment.start, segment.end);
                const isHighlighted =
                  !isGarbage &&
                  hasHighlightedSegments &&
                  String(segment.label || "")
                    .trim()
                    .toLowerCase() === highlightedTask;
                return (
                  <div key={`zoom-${segment.id}`}>
                    <div
                      title={`${segment.label} · ${c.formatTime(segment.start)} → ${c.formatTime(segment.end)} · ${segment.duration.toFixed(2)}s`}
                      className={cn(
                        "absolute top-2 bottom-2 rounded-[2px] border border-black/50 transition-[opacity,filter,box-shadow]",
                        isGarbage && "bg-destructive/70",
                        hasHighlightedSegments && !isHighlighted && "opacity-25",
                        isHighlighted && "z-10 brightness-150 outline outline-2 outline-white",
                      )}
                      style={{
                        left: `${left}%`,
                        width: `${width}%`,
                        ...(color ? { backgroundColor: color.solid } : null),
                        ...(isHighlighted
                          ? {
                              boxShadow: "inset 0 0 0 2px white, 0 0 12px rgba(255,255,255,.9)",
                            }
                          : null),
                      }}
                    />
                  </div>
                );
              })}
              {pendingRange ? (
                <div
                  className="absolute top-2 bottom-2 rounded-[2px] border border-warning bg-warning/40"
                  style={{
                    left: `${zoomLeft(pendingRange.start)}%`,
                    width: `${zoomWidth(pendingRange.start, pendingRange.end)}%`,
                  }}
                />
              ) : null}
              {c.scrubTime >= zoomBounds.start && c.scrubTime <= zoomBounds.end ? (
                <div
                  className="pointer-events-none absolute inset-y-0 z-20 -ml-2 w-4"
                  style={{ left: `${zoomLeft(c.scrubTime)}%` }}
                >
                  <span className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-white shadow-[0_0_5px_rgba(255,255,255,.85)]" />
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>

      {/* Last two segments — compact strip so the player stays full-width. */}
      {recent.length > 0 && (
        <div className="border-b border-border px-4 py-3">
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <span className="eyebrow">Recent</span>
            <span className="font-mono text-[10px] text-muted-foreground">
              last {recent.length}
            </span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {recent.map(({ s, i }) => (
              <div
                key={s.id || `recent-${i}`}
                className="flex items-center justify-between gap-3 px-1 py-1.5 text-xs"
              >
                <span className="flex min-w-0 items-center gap-2">
                  {s.kind === "work" ? (
                    <span
                      className="inline-flex max-w-[9rem] items-center gap-1.5 truncate rounded-sm border border-border/60 px-1.5 py-0.5 text-[10px] font-medium"
                      style={{
                        backgroundColor: taskColor(s.task).fill,
                        boxShadow: `inset 3px 0 0 ${taskColor(s.task).solid}`,
                      }}
                      title={s.task || "work"}
                    >
                      {s.task || "work"}
                    </span>
                  ) : (
                    <Badge tone="danger">{s.kind}</Badge>
                  )}
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {c.formatTime(s.start)} → {c.formatTime(s.end)}
                  </span>
                  <button
                    type="button"
                    title="Delete this segment and everything after it"
                    aria-label={`Delete segment ${i + 1}`}
                    onClick={() => c.deleteSegmentAt(i)}
                    className="grid size-6 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-destructive/15 hover:text-destructive"
                  >
                    <X className="size-3.5" strokeWidth={2} />
                  </button>
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2">
        <span className="eyebrow mr-1">Share clip</span>
        <Button size="sm" variant="outline" disabled={!video} onClick={() => c.markShareIn()}>
          Mark in <kbd className="ml-1 font-mono text-[10px] text-muted-foreground">I</kbd>
        </Button>
        <Button size="sm" variant="outline" disabled={!video} onClick={() => c.markShareOut()}>
          Mark out <kbd className="ml-1 font-mono text-[10px] text-muted-foreground">O</kbd>
        </Button>
        <div className="flex overflow-hidden rounded-sm border border-border">
          {(["1080p", "720p"] as const).map((q) => (
            <button
              key={q}
              type="button"
              disabled={c.shareClipBusy}
              onClick={() => c.setShareClipQuality(q)}
              className={cn(
                "px-2 py-1 font-mono text-[10px] uppercase tracking-[0.12em] transition-colors",
                c.shareClipQuality === q
                  ? "bg-accent text-accent-foreground"
                  : "bg-background text-muted-foreground hover:bg-surface-2 hover:text-foreground",
              )}
              title={q === "1080p" ? "Sharper, larger file" : "Faster encode, smaller file"}
            >
              {q}
            </button>
          ))}
        </div>
        <Button
          size="sm"
          variant="default"
          disabled={!video || !shareReady || c.shareClipBusy}
          onClick={() => c.downloadShareClip()}
          title="Download a WhatsApp-friendly MP4 of the marked range"
        >
          {c.shareClipBusy ? `Encoding ${c.shareClipQuality}…` : "Download for WhatsApp"}
        </Button>
        {(c.shareClipIn != null || c.shareClipOut != null) && (
          <Button size="sm" variant="ghost" onClick={() => c.clearShareClip()}>
            Clear
          </Button>
        )}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">
          {c.shareClipIn != null ? c.formatTime(c.shareClipIn) : "—"} →{" "}
          {c.shareClipOut != null ? c.formatTime(c.shareClipOut) : "—"}
          {shareReady ? ` · ${c.formatTime(c.shareClipOut! - c.shareClipIn!)}` : ""}
        </span>
      </div>

      {ann?.pendingWork && (
        <p className="border-b border-border bg-warning/10 px-4 py-2 font-mono text-[11px] text-warning">
          Pending work {c.formatTime(ann.pendingWork.start)} → {c.formatTime(ann.pendingWork.end)} —
          pick a task and press Enter
        </p>
      )}
    </section>
  );
}
