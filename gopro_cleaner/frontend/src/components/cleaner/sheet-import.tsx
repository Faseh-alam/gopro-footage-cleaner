import { useState } from "react";
import { toast } from "sonner";
import { Button, LinkButton } from "@/components/ui/button";
import { Dropdown, type SelectOption } from "@/components/wc/dropdown";
import { Checkbox, Field, FileInput } from "@/components/wc/field";
import { Panel } from "@/components/wc/panel";
import { api } from "@/lib/api";

interface PreviewVideo {
  footage?: string;
  video?: string;
  video_path: string;
  clip_count: number;
  clips?: { start: string; end: string }[];
}

interface PreviewData {
  video_count: number;
  clip_count: number;
  ready?: boolean;
  errors?: string[];
  warnings?: string[];
  videos?: PreviewVideo[];
}

export function SheetImport({
  driveOptions,
  dateOptions,
  drive,
  onDriveChange,
  date,
  onDateChange,
  setStatus,
  onQueued,
}: {
  driveOptions: SelectOption[];
  dateOptions: SelectOption[];
  drive: string;
  onDriveChange: (v: string) => void;
  date: string;
  onDateChange: (v: string) => void;
  setStatus: (s: string) => void;
  onQueued: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [deleteOriginal, setDeleteOriginal] = useState(true);
  const [preview, setPreview] = useState<PreviewData | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  async function uploadSheet(endpoint: string) {
    if (!file) throw new Error("Choose a CSV or JSON sheet first");
    if (!drive.trim()) throw new Error("Choose which drive this sheet is for");
    const formData = new FormData();
    formData.append("file", file);
    formData.append("drive", drive.trim());
    formData.append("delete_original", deleteOriginal ? "yes" : "no");
    return api<PreviewData & { queued_count?: number; failed_count?: number }>(endpoint, {
      method: "POST",
      body: formData,
    });
  }

  async function previewSheet() {
    try {
      setPreviewing(true);
      setStatus("Reading sheet...");
      const data = await uploadSheet("/api/import/preview");
      setPreview(data);
      setReady(Boolean(data.ready));
      const message = data.ready
        ? `Sheet ready: ${data.video_count} videos, ${data.clip_count} clips`
        : "Sheet has errors";
      setStatus(message);
      if (data.ready) toast.success(message);
      else toast.error(message);
    } catch (error: any) {
      toast.error(error.message);
      setStatus(error.message);
    } finally {
      setPreviewing(false);
    }
  }

  async function queueSheet() {
    if (!ready) {
      toast.error("Preview the sheet first and fix any errors.");
      return;
    }
    const summary = preview ? `${preview.video_count} videos · ${preview.clip_count} clips ready` : "";
    if (!window.confirm(`Queue ${summary}? Trimming will run in the background.`)) return;
    try {
      setBusy(true);
      setStatus("Queueing sheet...");
      const data = await uploadSheet("/api/import/queue");
      onQueued();
      const message = `Queued ${data.queued_count} videos (${data.clip_count} clips)`;
      if (data.failed_count) {
        toast.warning(message, { description: `${data.failed_count} videos failed to queue.` });
      } else {
        toast.success(message);
      }
      setStatus(message);
      setReady(false);
      setFile(null);
    } catch (error: any) {
      toast.error(error.message);
      setStatus(error.message);
    } finally {
      setBusy(false);
    }
  }

  function downloadGeneratedSheet() {
    if (!drive.trim()) {
      toast.error("Choose a drive first");
      return;
    }
    const params = new URLSearchParams({ drive: drive.trim() });
    if (date.trim()) params.set("date", date.trim());
    window.location.href = `/api/generate-sheet?${params.toString()}`;
  }

  return (
    <Panel
      eyebrow="03 / Bulk"
      title="Import from sheet"
      actions={
        <LinkButton size="sm" variant="ghost" href="/api/template/guide">
          Helper guide
        </LinkButton>
      }
      bodyClassName="grid gap-5"
    >
      <p className="max-w-2xl text-xs leading-relaxed text-muted-foreground">
        Download a pre-filled list of every video on a drive. Helpers only fill the{" "}
        <code className="font-mono text-foreground">timestamps</code> column, then you upload the
        completed sheet here.
      </p>

      <div className="grid gap-4 border-t border-border pt-4 lg:grid-cols-4">
        <Field label="Drive for this sheet">
          <Dropdown
            value={drive}
            onChange={onDriveChange}
            options={driveOptions}
            placeholder="Choose drive..."
            size="sm"
          />
        </Field>
        <Field label="Inventory date">
          <Dropdown
            value={date}
            onChange={onDateChange}
            options={dateOptions}
            placeholder="All dates on drive"
            size="sm"
          />
        </Field>
        <Field label="Completed sheet">
          <FileInput
            accept=".csv,.json,text/csv,application/json"
            onChange={(e) => {
              setFile(e.target.files?.[0] || null);
              setReady(false);
            }}
          />
        </Field>
        <div className="flex flex-wrap items-end gap-2">
          <Button size="sm" onClick={downloadGeneratedSheet}>
            Download list
          </Button>
          <Button size="sm" loading={previewing} disabled={busy} onClick={previewSheet}>
            Preview
          </Button>
          <Button size="sm" variant="primary" disabled={!ready} loading={busy} onClick={queueSheet}>
            Queue sheet
          </Button>
        </div>
      </div>

      <Checkbox
        label="Delete originals after all clips export"
        checked={deleteOriginal}
        onChange={(e) => setDeleteOriginal(e.target.checked)}
      />

      {preview && (
        <div className="grid gap-3 border-t border-border pt-4">
          <div className="eyebrow">
            {preview.video_count} videos · {preview.clip_count} clips ready
          </div>
          {!!preview.errors?.length && (
            <pre className="whitespace-pre-wrap rounded-sm border border-destructive/40 p-2 font-mono text-[11px] text-destructive">
              {preview.errors.join("\n")}
            </pre>
          )}
          {!!preview.warnings?.length && (
            <pre className="whitespace-pre-wrap rounded-sm border border-warning/40 p-2 font-mono text-[11px] text-warning">
              {preview.warnings.join("\n")}
            </pre>
          )}
          <div className="max-h-72 overflow-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Footage", "Found at", "Clips"].map((h) => (
                    <th key={h} className="eyebrow px-2 py-2 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(preview.videos || []).map((video, i) => (
                  <tr key={i} className="border-b border-border/60 align-top">
                    <td className="px-2 py-2 font-mono text-xs">{video.footage || video.video}</td>
                    <td className="px-2 py-2 font-mono text-[10px] text-muted-foreground">
                      {video.video_path}
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">
                      {video.clip_count}
                      <div className="text-[10px] text-muted-foreground">
                        {(video.clips || []).map((clip, ci) => (
                          <div key={ci}>
                            {clip.start} → {clip.end}
                          </div>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Panel>
  );
}
