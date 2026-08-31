// Shared types for the Review Station (World Context batch annotation).

export interface VideoItem {
  path: string;
  name: string;
  duration?: number | null;
  duration_label?: string | null;
  size_bytes?: number | null;
  relative?: string | null;
  parent_task?: string | null;
}

export interface Segment {
  id?: string;
  start: number;
  end: number;
  kind: "work" | "garbage";
  task?: string | null;
}

export const UNLABELED_TASK_LABEL = "Unlabeled task";
/** WhatsApp share clips cannot be longer than this (matches backend MAX_SHARE_SECONDS). */
export const SHARE_CLIP_MAX_SECONDS = 300;

export type ScaleAiFocusRange = { start: number; end: number };

export type ScaleAiHighlightOptions = {
  /** Jump the playhead to this time. When set, the highlight stays on. */
  seekTo?: number;
  /** Keep the highlight on even if this task is already selected. */
  keepOn?: boolean;
  /** When false, highlight without moving the playhead. */
  seek?: boolean;
  /** Zoom/highlight only this span (the mark just assigned), not every clip with the same label. */
  focusStart?: number;
  focusEnd?: number;
};

export interface PendingWork {
  start: number;
  end: number;
}

export interface ScaleAiSegment {
  id: string | number;
  start: number;
  end: number;
  duration: number;
  type: "subtask" | "garbage" | string;
  label: string;
  subtask_id?: string;
  clip_serial?: number;
  clip_filename?: string;
  camera_serial?: string;
}

/** Per-video subtask counts, keyed by lowercased label. */
export function scaleAiSubtaskCountsByLabel(
  segments: ScaleAiSegment[] | undefined | null,
): Map<string, number> {
  const counts = new Map<string, number>();
  for (const segment of segments || []) {
    if (String(segment.type || "").toLowerCase() !== "subtask") continue;
    const label = String(segment.label || "").trim();
    if (!label) continue;
    const key = label.toLowerCase();
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return counts;
}

/** True when this segment is the focused mark (or no focus range is set). */
export function scaleAiSegmentInFocus(
  segment: { start: number; end: number },
  range: ScaleAiFocusRange | null | undefined,
): boolean {
  if (!range) return true;
  return (
    Math.abs(Number(segment.start) - range.start) <= 0.15 &&
    Math.abs(Number(segment.end) - range.end) <= 0.15
  );
}

/** Distinct subtask labels in this video with their segment counts. */
export function scaleAiSubtaskCountRows(
  segments: ScaleAiSegment[] | undefined | null,
): { label: string; count: number }[] {
  const rows = new Map<string, { label: string; count: number }>();
  for (const segment of segments || []) {
    if (String(segment.type || "").toLowerCase() !== "subtask") continue;
    const label = String(segment.label || "").trim();
    if (!label) continue;
    const key = label.toLowerCase();
    const existing = rows.get(key);
    if (existing) existing.count += 1;
    else rows.set(key, { label, count: 1 });
  }
  return Array.from(rows.values());
}

export interface ScaleAiAnnotation {
  version: number;
  source_video: string;
  source_path: string;
  parent_task: string;
  camera_serial?: string | null;
  cl_number?: string | null;
  duration_seconds: number | null;
  media_meta?: MediaMeta | null;
  segments: ScaleAiSegment[];
  updated_at?: string;
}

export interface ScaleAiTaskProgress {
  task: string;
  target_hours: number | null;
  labeled_hours: number;
  remaining_hours: number | null;
  percent_complete: number | null;
  complete: boolean;
  video_count: number;
  labeled_video_count: number;
  labels: string[];
}

export interface ScaleAiProgress {
  version: number;
  root: string;
  updated_at?: string;
  tasks: ScaleAiTaskProgress[];
}

/** Camera / IMU metadata extracted from the GoPro file (GPMF + ffprobe). */
export interface MediaMeta {
  recorded_at?: string | null;
  /** Set when the user manually overrides the timestamp from the UI. */
  recorded_at_manual?: string | null;
  /** Original camera timestamp, preserved while an override is active. */
  recorded_at_camera?: string | null;
  camera_model?: string | null;
  camera_serial?: string | null;
  firmware?: string | null;
  media_uid?: string | null;
  lens_serial?: string | null;
  location?: string | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  video_codec?: string | null;
  has_gpmf?: boolean;
  sensors?: string[];
}

export interface Annotation {
  segments: Segment[];
  duration: number | null;
  complete: boolean;
  pendingWork: PendingWork | null;
  summary?: any;
  mediaMeta?: MediaMeta | null;
}

export interface TrimJob {
  job_id: string;
  status: string;
  start?: number;
  end?: number;
  duration_seconds?: number;
  progress?: number;
  remaining_seconds?: number;
  output?: string | null;
  name?: string | null;
  error?: string | null;
  kind?: string;
  task?: string | null;
  source_path?: string;
  source_name?: string;
  source_has_gpmf?: boolean;
  output_has_gpmf?: boolean;
  start_seconds?: number;
  end_seconds?: number;
}

/** Counts for the last Trim click, plus labeled-vs-disk audit for this video. */
export interface ClipDownloadAudit {
  ok: boolean;
  source_name: string;
  labeled: number;
  downloaded: number;
  missing: number;
  extra: number;
}

export interface TrimExportBatch {
  downloaded: number;
  not_downloaded: number;
  failed: number;
  cancelled: number;
  total: number;
  all_done: boolean;
  all_success: boolean;
  source_path?: string;
  folder_wide?: boolean;
  audit?: ClipDownloadAudit | null;
}

export function sameMediaPath(left: string, right: string): boolean {
  const norm = (value: string) =>
    value.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  return Boolean(left) && Boolean(right) && norm(left) === norm(right);
}

export function exportBatchBelongsToVideo(
  batch: TrimExportBatch | null | undefined,
  videoPath: string | undefined,
): boolean {
  if (!batch) return false;
  if (batch.folder_wide) return true;
  return sameMediaPath(String(batch.source_path || ""), String(videoPath || ""));
}

export interface Workspace {
  id: string;
  title: string;
  scanRoot: string;
  labelRoot: string;
  videos: VideoItem[];
  index: number;
  donePaths: string[];
}

export interface SdCard {
  scan_path?: string;
  path?: string;
  id?: string;
  label?: string;
}

export interface BatchCard {
  card_badge: string;
  factory?: string;
  device_type?: string;
  device_id?: string;
  status?: string;
  assets?: any[];
}

export interface BatchDetail {
  id: string;
  batch_name: string;
  factory?: string;
  status?: string;
  cards?: BatchCard[];
  cards_done?: number;
  card_count?: number;
  report?: any;
  blocking?: string[];
}

export interface CardIdentity {
  factory: string;
  card_badge: string;
  device_type: string;
  device_id: string;
}

export type StatusKind = "" | "ok" | "error";
