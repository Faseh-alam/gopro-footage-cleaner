import { useEffect, useState } from "react";
import { Pencil } from "lucide-react";
import { Badge, EmptyState, Panel } from "@/components/wc/panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ReviewController } from "./useReviewController";

function formatTimestamp(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** ISO / camera timestamp → value usable by <input type="datetime-local">. */
function toLocalInputValue(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

function Row({ label, value, title, action }: { label: string; value?: string | null; title?: string; action?: React.ReactNode }) {
  if (!value && !action) return null;
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3 py-1">
      <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </span>
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="min-w-0 truncate text-right font-mono text-[11px] text-foreground/90" title={title || value || undefined}>
          {value || "—"}
        </span>
        {action}
      </span>
    </div>
  );
}

export function MetadataCard({ c }: { c: ReviewController }) {
  const video = c.currentVideo();
  const meta = c.currentAnnotation()?.mediaMeta || null;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  // Reset the editor whenever the file (or its timestamp) changes.
  useEffect(() => {
    setEditing(false);
    setDraft(toLocalInputValue(meta?.recorded_at));
  }, [video?.path, meta?.recorded_at]);

  if (!video) return null;

  const isManual = Boolean(meta?.recorded_at_manual);
  const videoLine =
    meta?.width && meta?.height
      ? `${meta.width}×${meta.height}${meta.fps ? ` · ${Math.round(meta.fps)}fps` : ""}${meta.video_codec ? ` · ${meta.video_codec}` : ""}`
      : meta?.video_codec || "";

  const submitDraft = async () => {
    if (!draft) return;
    const date = new Date(draft);
    if (Number.isNaN(date.getTime())) {
      c.setStatus("Invalid date/time", "error");
      return;
    }
    await c.updateRecordedAt(date.toISOString());
    setEditing(false);
  };

  return (
    <Panel
      eyebrow="GoPro · GPMF"
      title="File metadata"
      actions={
        meta?.has_gpmf != null ? (
          <Badge tone={meta.has_gpmf ? "ok" : "muted"}>{meta.has_gpmf ? "IMU data" : "No IMU"}</Badge>
        ) : undefined
      }
      bodyClassName="p-4 pt-2"
    >
      {!meta ? (
        <EmptyState>No metadata extracted for this file.</EmptyState>
      ) : (
        <div className="grid gap-x-8 lg:grid-cols-2">
          <div className="min-w-0">
            <Row
              label="Recorded"
              value={formatTimestamp(meta.recorded_at)}
              title={meta.recorded_at || undefined}
              action={
                <span className="flex shrink-0 items-center gap-1">
                  {isManual && <Badge tone="warn">edited</Badge>}
                  <Button
                    size="icon"
                    variant="ghost"
                    className="size-5"
                    title="Edit recording timestamp"
                    onClick={() => {
                      setDraft(toLocalInputValue(meta.recorded_at));
                      setEditing((v) => !v);
                    }}
                  >
                    <Pencil className="size-3" />
                  </Button>
                </span>
              }
            />
            {editing && (
              <div className="mb-1 flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-2/60 p-2">
                <Input
                  type="datetime-local"
                  step="1"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  className="h-7 w-auto min-w-0 flex-1 font-mono text-[11px]"
                />
                <Button size="sm" className="h-7" onClick={submitDraft} disabled={!draft}>
                  Save
                </Button>
                {isManual && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7"
                    title={`Restore camera time${meta.recorded_at_camera ? `: ${formatTimestamp(meta.recorded_at_camera)}` : ""}`}
                    onClick={async () => {
                      await c.updateRecordedAt("");
                      setEditing(false);
                    }}
                  >
                    Reset
                  </Button>
                )}
                <Button size="sm" variant="ghost" className="h-7" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            )}
            {isManual && meta.recorded_at_camera && (
              <Row
                label="Camera time"
                value={formatTimestamp(meta.recorded_at_camera)}
                title={meta.recorded_at_camera || undefined}
              />
            )}
            <Row label="Camera" value={meta.camera_model} />
            <Row label="Serial" value={meta.camera_serial} />
            <Row label="Firmware" value={meta.firmware} />
          </div>
          <div className="min-w-0">
            <Row label="Video" value={videoLine} />
            <Row label="Media UID" value={meta.media_uid} />
            <Row label="Lens" value={meta.lens_serial} />
            <Row label="Location" value={meta.location} />
            {meta.sensors?.length ? (
              <div className="py-1">
                <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                  Sensor streams ({meta.sensors.length})
                </div>
                <div className="flex flex-wrap gap-1">
                  {meta.sensors.map((s) => (
                    <Badge key={s} tone="muted" className="normal-case tracking-normal">
                      {s.split("(")[0].trim()}
                    </Badge>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </Panel>
  );
}
