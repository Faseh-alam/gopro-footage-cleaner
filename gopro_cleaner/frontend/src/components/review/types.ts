// Shared types for the Review Station (World Context batch annotation).

export interface VideoItem {
  path: string;
  name: string;
  duration?: number | null;
  duration_label?: string | null;
  size_bytes?: number | null;
}

export interface Segment {
  start: number;
  end: number;
  kind: "work" | "garbage";
  task?: string | null;
}

export interface PendingWork {
  start: number;
  end: number;
}

export interface Annotation {
  segments: Segment[];
  duration: number | null;
  complete: boolean;
  pendingWork: PendingWork | null;
  summary?: any;
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
