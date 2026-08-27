import { Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/wc/panel";
import { Button } from "@/components/ui/button";
import type { MediaMeta } from "./types";
import type { ReviewController } from "./useReviewController";
import { taskColor } from "./task-color";

const MOTION_SENSORS: [RegExp, string][] = [
  [/acceler/i, "Accel"],
  [/gyro/i, "Gyro"],
  [/gps/i, "GPS"],
  [/magnet/i, "Mag"],
];

/** Compact IMU label: core motion sensors + a count for the remaining streams. */
function imuSummary(sensors: string[]) {
  const motion: string[] = [];
  for (const [pattern, short] of MOTION_SENSORS) {
    if (sensors.some((s) => pattern.test(s)) && !motion.includes(short)) motion.push(short);
  }
  const extra = sensors.length - sensors.filter((s) => MOTION_SENSORS.some(([p]) => p.test(s))).length;
  if (!motion.length) return `${sensors.length} streams`;
  return extra > 0 ? `${motion.join(", ")} +${extra}` : motion.join(", ");
}

function MediaMetaStrip({ meta }: { meta?: MediaMeta | null }) {
  if (!meta) return null;
  const items: { label: string; value: string; title?: string }[] = [];

  if (meta.camera_model) items.push({ label: "CAM", value: meta.camera_model });
  if (meta.camera_serial) items.push({ label: "SN", value: meta.camera_serial });
  if (meta.width && meta.height) {
    const fps = meta.fps ? ` ${Math.round(meta.fps)}fps` : "";
    items.push({ label: "VID", value: `${meta.width}×${meta.height}${fps}` });
  }
  if (meta.sensors?.length) {
    items.push({ label: "IMU", value: imuSummary(meta.sensors), title: meta.sensors.join(" · ") });
  } else if (meta.has_gpmf) {
    items.push({ label: "IMU", value: "GPMF present" });
  }

  if (!items.length) return null;
  return (
    <div className="border-b border-border px-4 py-2">
      <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-x-5 gap-y-1 rounded-md border border-border bg-surface-2/60 px-3 py-2 font-mono text-[10px] text-muted-foreground">
        {items.map((item) => (
          <div key={item.label} title={item.title || item.value} className="flex min-w-0 items-baseline gap-1.5">
            <span className="shrink-0 uppercase tracking-[0.14em] opacity-60">{item.label}</span>
            <span className="min-w-0 truncate text-foreground/80">{item.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function PlayerPanel({ c }: { c: ReviewController }) {
  const video = c.currentVideo();
  const ann = c.currentAnnotation();
  const duration = c.duration || video?.duration || 0;
  const segments = ann?.segments || [];
  // Latest-first indices into `segments` (chronological array).
  const recent = segments
    .map((s, i) => ({ s, i }))
    .slice(-2)
    .reverse();

  const covered = segments.reduce((acc, s) => acc + Math.max(0, s.end - s.start), 0);
  const coverage = duration > 0 ? Math.min(100, Math.round((covered / duration) * 100)) : 0;
  const playFraction = duration > 0 ? Math.min(1, c.scrubTime / duration) : 0;
  const shareReady = c.shareClipIn != null && c.shareClipOut != null && c.shareClipOut > c.shareClipIn;

  return (
    <section className="panel-surface flex min-h-0 flex-col">
      <MediaMetaStrip meta={ann?.mediaMeta} />

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
          <Badge tone={coverage >= 100 ? "ok" : "muted"}>{coverage}% covered</Badge>
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

        {/* Scrub track — display only; clicks never seek (use , . or ← →). */}
        <div
          className="pointer-events-none relative h-8 select-none border-t border-border bg-surface-2"
          aria-hidden
          title="Use , . or ← → to step through the timeline"
        >
          {segments.map((s, i) => {
            const color = s.kind === "work" ? taskColor(s.task) : null;
            return (
              <div
                key={s.id || i}
                title={`${s.kind}${s.task ? ` · ${s.task}` : ""}`}
                className={cn("absolute inset-y-0", s.kind !== "work" && "bg-destructive/30")}
                style={{
                  left: duration ? `${(s.start / duration) * 100}%` : "0%",
                  width: duration
                    ? `${Math.max(0.4, ((s.end - s.start) / duration) * 100)}%`
                    : "0%",
                  ...(color ? { backgroundColor: color.fill } : null),
                }}
              />
            );
          })}
          {ann?.pendingWork && duration > 0 && (
            <div
              className="absolute inset-y-0 border-x border-warning bg-warning/25"
              style={{
                left: `${(ann.pendingWork.start / duration) * 100}%`,
                width: `${Math.max(0.4, ((ann.pendingWork.end - ann.pendingWork.start) / duration) * 100)}%`,
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
      </div>

      {/* Last two segments — compact strip so the player stays full-width. */}
      {recent.length > 0 && (
        <div className="border-b border-border px-4 py-3">
          <div className="mb-2 flex items-baseline justify-between gap-3">
            <span className="eyebrow">Recent</span>
            <span className="font-mono text-[10px] text-muted-foreground">last {recent.length}</span>
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
          Pending work {c.formatTime(ann.pendingWork.start)} → {c.formatTime(ann.pendingWork.end)} — pick a task and
          press Enter
        </p>
      )}

      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
        <span className="eyebrow">All segments</span>
        <span className="font-mono text-[10px] text-muted-foreground">
          {segments.length ? `${segments.length} · latest first` : "none yet"}
        </span>
      </div>
      <ul className="max-h-52 min-h-0 flex-1 divide-y divide-border overflow-auto">
        {segments.length === 0 && (
          <li className="px-4 py-6 text-center text-xs text-muted-foreground">
            No segments yet — press T to end a work segment, G to mark garbage.
          </li>
        )}
        {[...segments].reverse().map((s, revI) => {
          const i = segments.length - 1 - revI;
          return (
            <li key={s.id || i} className="flex items-center justify-between gap-3 px-4 py-2 text-xs">
              <span className="flex min-w-0 items-center gap-2">
                {s.kind === "work" ? (
                  <span
                    className="inline-flex max-w-[11rem] items-center gap-1.5 truncate rounded-sm border border-border/60 px-1.5 py-0.5 text-[10px] font-medium"
                    style={{
                      backgroundColor: taskColor(s.task).fill,
                      boxShadow: `inset 3px 0 0 ${taskColor(s.task).solid}`,
                    }}
                  >
                    {s.task || "work"}
                  </span>
                ) : (
                  <Badge tone="danger">garbage</Badge>
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
            </li>
          );
        })}
      </ul>
    </section>
  );
}
