// Shared types for the Review Station (World Context batch annotation).

export interface VideoItem {
  path: string;
  name: string;
  duration?: number | null;
  duration_label?: string | null;
  size_bytes?: number | null;
}

export interface Segment {
  id?: string;
  start: number;
  end: number;
  kind: "work" | "garbage";
  task?: string | null;
}

export interface PendingWork {
  start: number;
  end: number;
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
