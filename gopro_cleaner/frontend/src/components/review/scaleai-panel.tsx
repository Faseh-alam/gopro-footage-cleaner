import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { taskColor } from "./task-color";
import type { ReviewController } from "./useReviewController";

function hoursLabel(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${value.toFixed(2)}h`;
}

export function ScaleAiPanel({
  c,
  highlightedTask,
  onHighlightTask,
}: {
  c: ReviewController;
  highlightedTask?: string;
  onHighlightTask?: (task: string) => void;
}) {
  const annotation = c.currentScaleAi();
  const segments = annotation?.segments || [];
  const progress = c.scaleAiTaskProgress();
  const video = c.currentVideo();
  const videosInTask = c.videosInCurrentParentTask();
  const videoIndex = videosInTask.findIndex((item) => item.path === video?.path);
  const normalizedHighlight = String(highlightedTask || "")
    .trim()
    .toLowerCase();

  return (
    <div className="grid gap-3 rounded-sm border border-border bg-surface-2/40 p-3">
      <div>
        <div className="eyebrow">ScaleAI 50-hour</div>
        <div className="mt-1 truncate text-sm font-semibold">
          {annotation?.parent_task || "Open a 50-hour folder"}
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
          T marks a segment · type a label · Enter assigns (creates if new) · G = garbage · N = next
          · Trim when ready
        </p>
      </div>

      <div className="grid gap-1 rounded-sm border border-border bg-surface px-2 py-2 text-[11px]">
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">VIDEO</span>
          <span className="truncate font-mono">{video?.name || "—"}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">IN TASK</span>
          <span className="font-mono">
            {videoIndex >= 0
              ? `${videoIndex + 1} / ${videosInTask.length}`
              : `— / ${videosInTask.length}`}
          </span>
        </div>
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">CAMERA</span>
          <span className="font-mono">{annotation?.camera_serial || "—"}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">CL</span>
          <span className="font-mono">{annotation?.cl_number || "—"}</span>
        </div>
      </div>

      <div className="grid gap-1 rounded-sm border border-border bg-surface px-2 py-2 text-[11px]">
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">TARGET</span>
          <span className="font-mono">{hoursLabel(progress?.target_hours)}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">COMPLETED</span>
          <span className="font-mono">{hoursLabel(progress?.labeled_hours ?? 0)}</span>
        </div>
        <div className="flex justify-between gap-2">
          <span className="text-muted-foreground">REMAINING</span>
          <span className="font-mono">{hoursLabel(progress?.remaining_hours)}</span>
        </div>
        {progress?.percent_complete != null ? (
          <div className="flex justify-between gap-2">
            <span className="text-muted-foreground">PROGRESS</span>
            <span className={`font-mono ${progress.complete ? "text-success" : ""}`}>
              {progress.percent_complete.toFixed(0)}%{progress.complete ? " · GOAL" : ""}
            </span>
          </div>
        ) : null}
      </div>

      {c.scaleAiPending ? (
        <div className="rounded-sm border border-accent/40 bg-accent/10 px-2 py-2 text-[11px]">
          Pending {c.formatTime(c.scaleAiPending.start)} → {c.formatTime(c.scaleAiPending.end)} —
          type a label and press Enter
        </div>
      ) : (
        <div className="text-[10px] text-muted-foreground">
          Last / selected label:{" "}
          {c.selectedTaskValue ? (
            <button
              type="button"
              aria-pressed={highlightedTask === c.selectedTaskValue}
              title="Highlight this task in the labeled region"
              onClick={() => onHighlightTask?.(c.selectedTaskValue)}
              className={`rounded-sm px-1 font-medium text-foreground underline decoration-dotted underline-offset-2 transition-colors hover:bg-surface ${
                highlightedTask === c.selectedTaskValue ? "bg-accent/15 text-accent" : ""
              }`}
            >
              {c.selectedTaskValue}
            </button>
          ) : (
            <span className="font-medium text-foreground">—</span>
          )}
        </div>
      )}

      <div className="max-h-44 overflow-auto rounded-sm border border-border">
        {!segments.length ? (
          <p className="px-3 py-4 text-center text-xs text-muted-foreground">
            No segments yet. Press T at the end of a subtask.
          </p>
        ) : (
          segments.map((segment) => {
            const color = taskColor(segment.label || segment.type);
            const isGarbage = segment.type === "garbage";
            const isHighlighted =
              !isGarbage &&
              normalizedHighlight.length > 0 &&
              String(segment.label || "")
                .trim()
                .toLowerCase() === normalizedHighlight;
            return (
              <div
                key={String(segment.id)}
                className={cn(
                  "flex items-center gap-2 border-b border-border px-2 py-2 transition-colors last:border-b-0",
                  isHighlighted && "bg-accent/15 ring-1 ring-inset ring-accent",
                )}
              >
                <span
                  className="size-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: isGarbage ? "#6b7280" : color.solid }}
                  aria-hidden
                />
                <div className="min-w-0 flex-1">
                  {isGarbage ? (
                    <div className="truncate text-xs font-medium">garbage</div>
                  ) : (
                    <button
                      type="button"
                      aria-pressed={isHighlighted}
                      title={`Highlight ${segment.label} in the labeled region`}
                      onClick={() => onHighlightTask?.(segment.label)}
                      className="block max-w-full truncate rounded-sm text-left text-xs font-medium underline decoration-dotted underline-offset-2 hover:text-accent"
                    >
                      {segment.label}
                    </button>
                  )}
                  <div className="font-mono text-[10px] text-muted-foreground">
                    {c.formatTime(segment.start)} → {c.formatTime(segment.end)} ·{" "}
                    {segment.duration.toFixed(2)}s
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void c.deleteScaleAiSegment(segment.id)}
                  className="text-[10px] text-muted-foreground hover:text-destructive"
                >
                  Delete
                </button>
              </div>
            );
          })
        )}
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Button size="sm" variant="accent" onClick={() => void c.processScaleAiVideo()}>
          Trim this video
        </Button>
        <Button size="sm" variant="outline" onClick={() => void c.stitchScaleAiVideo()}>
          Stitch this video
        </Button>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Button size="sm" variant="outline" onClick={() => void c.nextScaleAiVideo()}>
          Next video
        </Button>
        <Button size="sm" variant="outline" onClick={() => void c.processScaleAiFolder()}>
          Trim whole folder
        </Button>
      </div>
    </div>
  );
}
