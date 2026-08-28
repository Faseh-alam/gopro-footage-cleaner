import { useCallback, useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { toast } from "sonner";
import { api, formatClock, host, openDownloadUrl } from "@/lib/api";
import type {
  Annotation,
  BatchDetail,
  CardIdentity,
  MediaMeta,
  PendingWork,
  ScaleAiAnnotation,
  ScaleAiProgress,
  ScaleAiTaskProgress,
  SdCard,
  Segment,
  StatusKind,
  TrimJob,
  VideoItem,
  Workspace,
} from "./types";

const RECENT_TASKS_KEY = "gopro_eager_recent_tasks";
const RECENT_TASKS_MAX = 10;
const SESSION_KEY = "gopro_eager_review_session";
const SEEN_GIT_SHA_KEY = "gopro_eager_seen_git_sha";
const PLAYBACK_RATE_MIN = -4;
/** Up to 5× on every source — playback never waits on an encode. */
const PLAYBACK_RATE_MAX = 5;
const PLAYBACK_RATE_STEP = 0.5;

/**
 * Review sources are all 1:1 with the original timeline (no overspeed baking),
 * so annotations / resume / share marks are always original seconds:
 *  - `fast`     GoPro LRV proxy or an SSD copy (zero encode, smooth at 5×)
 *  - `original` the untouched file streamed directly
 */
type ActiveSource = "original" | "fast" | "preview";

type ReviewSession = {
  path: string;
  name?: string;
  scrubTime: number;
  scanRoot?: string;
  updatedAt: number;
};

function normalizeMediaPath(path: string) {
  return String(path || "")
    .trim()
    .replace(/\\/g, "/")
    .toLowerCase();
}

function mediaBasename(path: string) {
  const norm = normalizeMediaPath(path);
  const parts = norm.split("/");
  return parts[parts.length - 1] || "";
}

/** Match full path, or same filename if the card remounted on another letter/path. */
function sessionMatchesPath(session: ReviewSession | null, path: string) {
  if (!session?.path || !path) return false;
  if (normalizeMediaPath(session.path) === normalizeMediaPath(path)) return true;
  const a = mediaBasename(session.path);
  const b = mediaBasename(path);
  if (a && b && a === b) return true;
  if (session.name && mediaBasename(session.name) === b) return true;
  return false;
}

function loadReviewSession(): ReviewSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.path !== "string") return null;
    return {
      path: parsed.path,
      name: typeof parsed.name === "string" ? parsed.name : mediaBasename(parsed.path),
      scrubTime: Number(parsed.scrubTime) || 0,
      scanRoot: typeof parsed.scanRoot === "string" ? parsed.scanRoot : undefined,
      updatedAt: Number(parsed.updatedAt) || 0,
    };
  } catch {
    return null;
  }
}

function saveReviewSession(
  session: { path: string; scrubTime: number; scanRoot?: string; name?: string },
  opts: { force?: boolean } = {},
) {
  try {
    const nextT = Math.max(0, Number(session.scrubTime) || 0);
    const existing = loadReviewSession();
    // Never wipe a real resume point with 0 while the player is still settling.
    if (
      !opts.force &&
      nextT < 0.25 &&
      sessionMatchesPath(existing, session.path) &&
      (existing?.scrubTime || 0) >= 0.25
    ) {
      return;
    }
    const payload: ReviewSession = {
      path: session.path,
      name: session.name || mediaBasename(session.path),
      scrubTime: nextT,
      scanRoot: session.scanRoot || undefined,
      updatedAt: Date.now(),
    };
    localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
  } catch {
    /* ignore */
  }
}

function formatTime(seconds: number) {
  return formatClock(seconds);
}

function formatDurationShort(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s";
  const whole = Math.ceil(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function basenamePath(path?: string | null) {
  if (!path) return "";
  return String(path).split(/[/\\]/).pop() || "";
}

function shortCardTitle(title: string) {
  const raw = String(title || "Card").trim();
  const match = raw.match(/C\d{4}/i);
  if (match) return match[0].toUpperCase();
  return raw.split(/[\s·—-]/)[0] || "Card";
}

function computeAnchor(segments: Segment[]) {
  if (!segments?.length) return 0;
  const last = segments[segments.length - 1];
  const end = Number(last?.end);
  return Number.isFinite(end) ? end : 0;
}

function coverageEnd(segments: Segment[]) {
  if (!segments?.length) return 0;
  return segments.reduce((max, segment) => Math.max(max, Number(segment.end) || 0), 0);
}

function normalizeBoundary(value: number, duration?: number | null) {
  let next = Math.max(0, Number(value) || 0);
  if (duration != null && Number(duration) > 0) {
    const dur = Number(duration);
    if (next >= dur - 0.05) return dur;
    next = Math.min(next, dur);
  }
  return next;
}

function loadRecentTasks(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_TASKS_KEY);
    const parsed = JSON.parse(raw || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item) => typeof item === "string" && item.trim());
  } catch {
    return [];
  }
}

function saveRecentTasks(recent: string[]) {
  try {
    localStorage.setItem(RECENT_TASKS_KEY, JSON.stringify(recent.slice(0, RECENT_TASKS_MAX)));
  } catch {
    /* ignore */
  }
}

export interface ClipRange {
  start: number;
  end: number;
  kind: "work" | "garbage";
  pending?: boolean;
}

let workspaceCounter = 0;
function createWorkspace(title?: string): Workspace {
  workspaceCounter += 1;
  return {
    id: `ws-${Date.now()}-${workspaceCounter}`,
    title: title || `Footage ${workspaceCounter}`,
    scanRoot: "",
    labelRoot: "",
    videos: [],
    index: -1,
    donePaths: [],
  };
}

export function useReviewController() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const playPromiseRef = useRef<Promise<void> | null>(null);
  const playGenerationRef = useRef(0);
  const wantPlayingRef = useRef(false);
  const playbackRateRef = useRef(1);
  const playerWrapRef = useRef<HTMLDivElement | null>(null);
  const taskSearchRef = useRef<HTMLInputElement | null>(null);
  const newTaskInputRef = useRef<HTMLInputElement | null>(null);

  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [index, setIndex] = useState(-1);
  const [scanRoot, setScanRoot] = useState("");
  const [labelRoot, setLabelRoot] = useState("");
  const [tasks, setTasks] = useState<string[]>([]);
  const [defaultTasks, setDefaultTasks] = useState<string[]>([]);
  const [annotationsByPath, setAnnotationsByPath] = useState<Record<string, Annotation>>({});
  const anchorByPathRef = useRef<Record<string, number>>({});

  const [batchId, setBatchId] = useState<string | null>(null);
  const [batchDetail, setBatchDetail] = useState<BatchDetail | null>(null);
  const [cardIdentity, setCardIdentity] = useState<CardIdentity>({
    factory: "",
    card_badge: "",
    device_type: "",
    device_id: "",
  });

  const [sdCards, setSdCards] = useState<SdCard[]>([]);
  const [sdCardValue, setSdCardValue] = useState("");

  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null);

  const [recentTasks, setRecentTasks] = useState<string[]>([]);
  const [lastLabelTask, setLastLabelTask] = useState("");
  const [taskSearch, setTaskSearch] = useState("");
  const [selectedTaskValue, setSelectedTaskValue] = useState("");
  const [taskSelectionMode, setTaskSelectionMode] = useState(false);

  const [status, setStatusState] = useState<{ message: string; kind: StatusKind }>({
    message: "",
    kind: "",
  });

  const [scrubTime, setScrubTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [playbackRate, setPlaybackRateState] = useState(1);
  const [isPlaying, setIsPlaying] = useState(false);
  const [loadingVideo, setLoadingVideo] = useState(false);

  const [globalTrim, setGlobalTrim] = useState<{ active: number; jobs: TrimJob[]; etaTotal: number }>({
    active: 0,
    jobs: [],
    etaTotal: 0,
  });

  const [busy, setBusy] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [detecting, setDetecting] = useState(false);
  const [ffmpegHint, setFfmpegHint] = useState<string | null>(null);
  // Off by default — when on, queue-work moves raw footage to Trash after trims finish.
  const [deleteSourceAfterTrim, setDeleteSourceAfterTrim] = useState(false);
  // In/out marks for WhatsApp share clips (independent of work/garbage markup).
  const [shareClipIn, setShareClipIn] = useState<number | null>(null);
  const [shareClipOut, setShareClipOut] = useState<number | null>(null);
  const [shareClipBusy, setShareClipBusy] = useState(false);
  const [shareClipQuality, setShareClipQuality] = useState<"720p" | "1080p">("1080p");
  const [appVersion, setAppVersion] = useState("");
  const [perf, setPerf] = useState<{ trim_poll_ms: number }>({ trim_poll_ms: 1200 });
  /** ScaleAI 50-hour free-form subtask labeling is the default on this branch. */
  const [scaleAiMode, setScaleAiModeState] = useState(() => {
    try {
      return localStorage.getItem("wc-scaleai-mode") !== "0";
    } catch {
      return true;
    }
  });
  const [scaleAiByPath, setScaleAiByPath] = useState<Record<string, ScaleAiAnnotation>>({});
  const [scaleAiPending, setScaleAiPending] = useState<PendingWork | null>(null);
  const [scaleAiProgress, setScaleAiProgress] = useState<ScaleAiProgress | null>(null);
  const [scaleAiGoalShown, setScaleAiGoalShown] = useState<Record<string, boolean>>({});

  /** Bumped per video open — stale async work checks this before touching the player. */
  const previewTokenRef = useRef(0);
  const fastPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const compatiblePreviewPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const compatiblePreviewPathRef = useRef("");
  const hlsRef = useRef<Hls | null>(null);
  /** Paths whose fast source we already asked the backend to warm. */
  const warmedFastRef = useRef<Set<string>>(new Set());
  /** Media URL currently attached (absolute) + which ladder rung it is. */
  const mediaUrlRef = useRef<string>("");
  const activeSourceRef = useRef<ActiveSource>("original");
  const seekTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSeekRef = useRef<number | null>(null);
  /** Hold this scrub target after load so timeupdate/seek races can't snap back to 0. */
  const resumeTargetRef = useRef(0);
  const resumeHoldUntilRef = useRef(0);
  /** Last non-zero playhead — used to recover from false ended / buffer snaps to 0. */
  const lastGoodTimeRef = useRef(0);
  const loadingVideoRef = useRef(false);
  loadingVideoRef.current = loadingVideo;
  /** Freeze-frame canvas shown while the media element swaps sources. */
  const swapCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const trimPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const globalTrimPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [previewNote, setPreviewNote] = useState("");

  // Keep latest state accessible inside stable callbacks / keyboard handler.
  const stateRef = useRef<any>({});
  stateRef.current = {
    videos,
    index,
    scanRoot,
    labelRoot,
    tasks,
    annotationsByPath,
    batchId,
    batchDetail,
    cardIdentity,
    sdCards,
    sdCardValue,
    workspaces,
    activeWorkspaceId,
    recentTasks,
    lastLabelTask,
    taskSearch,
    selectedTaskValue,
    scrubTime,
    duration,
    deleteSourceAfterTrim,
    scaleAiMode,
    scaleAiByPath,
    scaleAiPending,
    scaleAiProgress,
  };

  const setStatus = useCallback((message: string, kind: StatusKind = "") => {
    setStatusState({ message: message || "", kind });
    if (kind === "error" && message) toast.error(message);
  }, []);

  const currentVideo = useCallback((): VideoItem | null => {
    const s = stateRef.current;
    return s.index >= 0 ? s.videos[s.index] || null : null;
  }, []);

  /** Every source shares the original timeline, so times need no conversion. */
  const readOriginalTime = useCallback(() => {
    const v = videoRef.current;
    if (!v || !Number.isFinite(v.currentTime)) return Number(stateRef.current.scrubTime) || 0;
    return v.currentTime;
  }, []);

  const seekMediaToOriginal = useCallback((originalTime: number) => {
    const v = videoRef.current;
    if (!v || !Number.isFinite(originalTime)) return;
    try {
      v.currentTime = Math.max(0, originalTime);
    } catch {
      /* ignore */
    }
  }, []);

  const applyMediaRate = useCallback((uiRate: number) => {
    const v = videoRef.current;
    if (!v) return;
    try {
      v.playbackRate = uiRate;
    } catch {
      /* ignore */
    }
  }, []);

  const annotationFor = useCallback(
    (path?: string | null): Annotation | null => {
      if (!path) return null;
      return stateRef.current.annotationsByPath[path] || null;
    },
    [],
  );

  const currentAnnotation = useCallback(() => annotationFor(currentVideo()?.path), [annotationFor, currentVideo]);

  const scanTargetPath = useCallback(() => {
    // ScaleAI labeling always uses the chosen 50 hours / drive folder — never
    // a detected DCIM/###GOPRO card root that may still be mounted.
    if (stateRef.current.scaleAiMode) {
      return stateRef.current.scanRoot?.trim() || "";
    }
    const card = stateRef.current.sdCardValue?.trim();
    if (card) return card;
    return stateRef.current.scanRoot?.trim() || "";
  }, []);

  const selectedSdCardLabel = useCallback(() => {
    const card = stateRef.current.sdCards.find(
      (c: SdCard) => (c.scan_path || c.path) === stateRef.current.sdCardValue,
    );
    return card?.id || card?.label || "";
  }, []);

  // ---------------------------------------------------------------------
  // Annotation helpers
  // ---------------------------------------------------------------------
  const applyAnnotationPayload = useCallback(
    (path: string, payload: any, { keepPending = false }: { keepPending?: boolean } = {}) => {
      const annotation = payload?.annotation || payload || {};
      const summary = payload?.summary || {};
      const segments: Segment[] = Array.isArray(annotation.segments) ? annotation.segments : [];
      const duration_ =
        annotation.duration != null
          ? Number(annotation.duration)
          : summary.duration != null
            ? Number(summary.duration)
            : null;
      const complete = Boolean(summary.complete != null ? summary.complete : annotation.complete);
      setAnnotationsByPath((prev) => {
        const prevAnn = prev[path];
        const next: Annotation = {
          segments,
          duration: Number.isFinite(duration_) ? duration_ : prevAnn?.duration ?? null,
          complete,
          pendingWork: keepPending ? prevAnn?.pendingWork || null : null,
          summary,
          mediaMeta: annotation.media_meta || prevAnn?.mediaMeta || null,
        };
        anchorByPathRef.current[path] = computeAnchor(segments);
        return { ...prev, [path]: next };
      });
    },
    [],
  );

  const loadAnnotationForPath = useCallback(
    async (path: string, opts: { keepPending?: boolean } = {}) => {
      if (!path) return null;
      try {
        const data = await api(`/api/eager/annotations?path=${encodeURIComponent(path)}`);
        applyAnnotationPayload(path, data, opts);
        return data;
      } catch (error: any) {
        setAnnotationsByPath((prev) => {
          if (prev[path]) return prev;
          anchorByPathRef.current[path] = 0;
          return { ...prev, [path]: { segments: [], duration: null, complete: false, pendingWork: null } };
        });
        setStatus(error.message || "Could not load annotations", "error");
        return null;
      }
    },
    [applyAnnotationPayload, setStatus],
  );

  const applyScaleAiPayload = useCallback((path: string, data: any) => {
    const annotation = data.annotation as ScaleAiAnnotation;
    setScaleAiByPath((prev) => ({ ...prev, [path]: annotation }));
    if (Array.isArray(data.labels)) {
      const labels = data.labels.map((name: string) => String(name).trim()).filter(Boolean);
      if (labels.length) {
        setTasks((prev) => {
          const seen = new Set(prev.map((t) => t.toLowerCase()));
          const merged = [...prev];
          for (const label of labels) {
            if (!seen.has(label.toLowerCase())) {
              merged.push(label);
              seen.add(label.toLowerCase());
            }
          }
          return merged;
        });
      }
    }
    if (data.progress) {
      setScaleAiProgress(data.progress as ScaleAiProgress);
      const parent = annotation?.parent_task;
      const row = (data.progress.tasks || []).find((task: ScaleAiTaskProgress) => task.task === parent);
      if (row?.complete && parent && !scaleAiGoalShown[parent]) {
        setScaleAiGoalShown((prev) => ({ ...prev, [parent]: true }));
        toast.success("GOAL COMPLETED", {
          description: `${parent}\nTarget: ${(row.target_hours ?? 0).toFixed(2)}h\nLabeled: ${row.labeled_hours.toFixed(2)}h+`,
          duration: 8000,
        });
      }
    }
    return annotation;
  }, [scaleAiGoalShown]);

  const loadScaleAiForPath = useCallback(
    async (path: string) => {
      if (!path) return null;
      try {
        const root = stateRef.current.scanRoot || "";
        const query = new URLSearchParams({ path });
        if (root) query.set("root", root);
        const data = await api(`/api/eager/scaleai/annotation?${query.toString()}`);
        return applyScaleAiPayload(path, data);
      } catch (error: any) {
        setStatus(error.message || "Could not load ScaleAI annotation", "error");
        return null;
      }
    },
    [applyScaleAiPayload, setStatus],
  );

  const currentScaleAi = useCallback((): ScaleAiAnnotation | null => {
    const path = currentVideo()?.path;
    return path ? stateRef.current.scaleAiByPath[path] || null : null;
  }, [currentVideo]);

  const scaleAiTaskProgress = useCallback((): ScaleAiTaskProgress | null => {
    const parent = currentScaleAi()?.parent_task;
    const progress = stateRef.current.scaleAiProgress as ScaleAiProgress | null;
    if (!parent || !progress) return null;
    return progress.tasks.find((row) => row.task === parent) || null;
  }, [currentScaleAi]);

  const videosInCurrentParentTask = useCallback((): VideoItem[] => {
    const parent = currentScaleAi()?.parent_task || currentVideo()?.parent_task;
    const all = stateRef.current.videos as VideoItem[];
    if (!parent) return all;
    return all.filter((video) => (video.parent_task || "") === parent);
  }, [currentScaleAi, currentVideo]);

  const annotationContext = useCallback(() => {
    const id = stateRef.current.cardIdentity as CardIdentity;
    const batch = stateRef.current.batchDetail as BatchDetail | null;
    const video = currentVideo();
    // Never send <video>.duration — while 720p HLS is still encoding that
    // value is only the encode frontier and used to corrupt the sidecar.
    // Prefer scan/sidecar duration; the server always re-probes with ffprobe.
    const dur = video?.duration || annotationFor(video?.path)?.duration || undefined;
    return {
      batch_name: batch?.batch_name || "",
      factory: id.factory || "",
      card_badge: id.card_badge || "",
      device_type: id.device_type || "",
      device_id: id.device_id || "",
      duration: dur,
    };
  }, [annotationFor, currentVideo]);

  // ---------------------------------------------------------------------
  // Scrub / playback
  // ---------------------------------------------------------------------
  const updateScrubUiFromEl = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    const el = Number.isFinite(v.duration) ? v.duration : 0;
    const video = currentVideo();
    const knownFile = Number(video?.duration) || 0;
    const knownAnn = Number(stateRef.current.annotationsByPath[video?.path || ""]?.duration) || 0;
    setDuration(Math.max(el, knownFile, knownAnn) || 0);
  }, [currentVideo]);

  /** Clip length in original seconds — sidecar/scan wins over element metadata. */
  const knownDurationSec = useCallback(() => {
    const v = videoRef.current;
    const video = currentVideo();
    const el = v && Number.isFinite(v.duration) ? v.duration : 0;
    const knownFile = Number(video?.duration) || 0;
    const knownAnn = Number(stateRef.current.annotationsByPath[video?.path || ""]?.duration) || 0;
    return Math.max(el, knownFile, knownAnn) || 0;
  }, [currentVideo]);

  // Guard against "play() interrupted by pause()" and hung play() promises.
  // At high playback rates Chrome can stall progressive MP4 streams;
  // play() then never settles and the old pause path waited forever on it,
  // which made Space/play/pause dead until the footage was changed (reload).
  const safePause = useCallback(() => {
    const v = videoRef.current;
    wantPlayingRef.current = false;
    playGenerationRef.current += 1;
    playPromiseRef.current = null;
    if (!v) return;
    try {
      v.pause();
    } catch {
      /* ignore */
    }
  }, []);

  /** Full clip length from sidecar/scan, falling back to element metadata. */
  const fullClipDuration = useCallback(() => {
    const el = videoRef.current;
    const video = stateRef.current.videos[stateRef.current.index];
    const elDur = el && Number.isFinite(el.duration) ? el.duration : 0;
    const knownFile = Number(video?.duration) || 0;
    const knownAnn = Number(stateRef.current.annotationsByPath[video?.path || ""]?.duration) || 0;
    return Math.max(elDur, knownFile, knownAnn) || 0;
  }, []);

  const safePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    wantPlayingRef.current = true;
    const generation = ++playGenerationRef.current;

    const attempt = (isRetry: boolean) => {
      if (generation !== playGenerationRef.current || !wantPlayingRef.current) return;
      const el = videoRef.current;
      if (!el) return;

      try {
        const t = Number.isFinite(el.currentTime) ? el.currentTime : 0;
        const good = Math.max(lastGoodTimeRef.current, Number(stateRef.current.scrubTime) || 0);
        const fullDur = fullClipDuration();
        // Only restart from 0 at the TRUE end of the clip. Progressive underruns
        // can report a short duration or spurious `ended` mid-file — that used to
        // snap fast playback back to 00:00 after a lag.
        const atTrueEnd = fullDur > 1 && t >= fullDur - 0.2;
        if (atTrueEnd) {
          seekMediaToOriginal(0);
          lastGoodTimeRef.current = 0;
        } else if (el.ended || (t < 0.35 && good > 1)) {
          // Unstick false-ended / snapped-to-zero without restarting the review.
          const recover = Math.max(good, t > 0.05 ? t : 0);
          if (recover > 0.05) {
            seekMediaToOriginal(recover);
            lastGoodTimeRef.current = recover;
          }
        }
      } catch {
        /* ignore */
      }

      applyMediaRate(playbackRateRef.current || 1);

      let p: Promise<void> | undefined;
      try {
        p = el.play() as Promise<void>;
      } catch {
        p = Promise.reject(new Error("play() threw"));
      }

      if (!p || typeof p.then !== "function") {
        playPromiseRef.current = null;
        return;
      }

      const tracked = p
        .then(() => {
          if (generation !== playGenerationRef.current) return;
          const cur = videoRef.current;
          if (cur) applyMediaRate(playbackRateRef.current || 1);
        })
        .catch(() => {
          if (generation !== playGenerationRef.current || !wantPlayingRef.current) return;
          if (isRetry) return;
          // Decoder/network stall recovery: nudge timeline then retry once.
          const cur = videoRef.current;
          if (!cur) return;
          try {
            const live = Number.isFinite(cur.currentTime) ? cur.currentTime : 0;
            const good = Math.max(lastGoodTimeRef.current, Number(stateRef.current.scrubTime) || 0);
            const recover = live < 0.35 && good > 1 ? good : Math.max(live, good);
            cur.pause();
            seekMediaToOriginal(Math.max(0, recover));
          } catch {
            /* ignore */
          }
          window.setTimeout(() => attempt(true), 40);
        })
        .finally(() => {
          if (playPromiseRef.current === tracked) playPromiseRef.current = null;
        }) as Promise<void>;

      playPromiseRef.current = tracked;

      // Watchdog — hung play() promises freeze the transport controls otherwise.
      window.setTimeout(() => {
        if (playPromiseRef.current !== tracked) return;
        if (generation !== playGenerationRef.current || !wantPlayingRef.current) return;
        playPromiseRef.current = null;
        if (!isRetry) attempt(true);
      }, 1500);
    };

    attempt(false);
  }, [applyMediaRate, fullClipDuration, seekMediaToOriginal]);

  /**
   * Freeze the current frame over the player while the media source switches
   * (card original → SSD copy). The swap reads as a quality change, not a restart.
   * Returns a function that removes the freeze-frame.
   */
  const showSwapMask = useCallback(() => {
    const v = videoRef.current;
    const cv = swapCanvasRef.current;
    if (!v || !cv || !v.videoWidth || v.readyState < 2) return () => {};
    try {
      cv.width = v.videoWidth;
      cv.height = v.videoHeight;
      const ctx = cv.getContext("2d");
      if (!ctx) return () => {};
      ctx.drawImage(v, 0, 0, cv.width, cv.height);
      cv.style.display = "block";
    } catch {
      return () => {};
    }
    return () => {
      cv.style.display = "none";
    };
  }, []);

  const flushSeek = useCallback(() => {
    if (seekTimerRef.current) {
      clearTimeout(seekTimerRef.current);
      seekTimerRef.current = null;
    }
    if (pendingSeekRef.current !== null) {
      const target = pendingSeekRef.current;
      setScrubTime(target);
      const v = videoRef.current;
      if (v) {
        safePause();
        seekMediaToOriginal(target);
      }
      pendingSeekRef.current = null;
    }
  }, [safePause, seekMediaToOriginal]);

  const currentScrubTime = useCallback(() => {
    flushSeek();
    return stateRef.current.scrubTime;
  }, [flushSeek]);

  const scheduleSeek = useCallback(
    (time: number, immediate = false) => {
      const dur = knownDurationSec();
      if (!dur) return;
      // Always stop at the true end — never past it.
      const clamped = Math.min(Math.max(0, time), Math.max(0, dur - 0.04));
      setScrubTime(clamped);
      if (clamped <= 0.05) lastGoodTimeRef.current = 0;
      else lastGoodTimeRef.current = clamped;
      pendingSeekRef.current = clamped;
      if (immediate) {
        flushSeek();
        return;
      }
      if (seekTimerRef.current) return;
      seekTimerRef.current = setTimeout(() => {
        seekTimerRef.current = null;
        flushSeek();
      }, 120);
    },
    [flushSeek, knownDurationSec],
  );

  // Scrubber clicks are disabled in the UI — keep this as a no-op so nothing
  // can jump the playhead by clicking the timeline.
  const seekToFraction = useCallback((_fraction: number) => {
    /* intentionally disabled */
  }, []);

  const fineTune = useCallback(
    (seconds: number) => {
      if (!currentVideo()) return;
      const target = stateRef.current.scrubTime + seconds;
      scheduleSeek(target, true);
    },
    [currentVideo, scheduleSeek],
  );

  /** ScaleAI: 0.1s steps (textile grabs ~0.2–0.5s). Shift = one frame @30fps. */
  const scrubStepSeconds = useCallback(
    (fineFrame = false) => {
      if (fineFrame) return 1 / 30;
      return stateRef.current.scaleAiMode ? 0.1 : 1;
    },
    [],
  );

  const setPlaybackRate = useCallback((rate: number, announce = true) => {
    const v = videoRef.current;
    const max = PLAYBACK_RATE_MAX;
    let clamped = Math.round(rate / PLAYBACK_RATE_STEP) * PLAYBACK_RATE_STEP;
    clamped = Math.min(max, Math.max(PLAYBACK_RATE_MIN, clamped));
    // Skip 0× — stepping ←/→ through zero continues into reverse / forward.
    if (Math.abs(clamped) < PLAYBACK_RATE_STEP / 2) {
      clamped = rate < 0 || (rate === 0 && (playbackRateRef.current || 1) > 0)
        ? -PLAYBACK_RATE_STEP
        : PLAYBACK_RATE_STEP;
      clamped = Math.min(max, Math.max(PLAYBACK_RATE_MIN, clamped));
    }
    playbackRateRef.current = clamped;
    setPlaybackRateState(clamped);
    if (v) {
      const wasPlaying = wantPlayingRef.current || !v.paused;
      applyMediaRate(clamped);
      if (wasPlaying && v.paused) safePlay();
    }
    if (announce) {
      setStatus(`Playback ${clamped.toFixed(1)}×`, "ok");
    }
  }, [applyMediaRate, safePlay, setStatus]);

  const bumpPlaybackRate = useCallback(
    (delta: number) => {
      if (!currentVideo()) return;
      // Bump the UI rate; the media element follows 1:1.
      setPlaybackRate((playbackRateRef.current || 1) + delta);
      const v = videoRef.current;
      if (v?.paused || !wantPlayingRef.current) {
        safePlay();
      }
    },
    [currentVideo, safePlay, setPlaybackRate],
  );

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    setPlaybackRate(1, false);
    // Prefer wantPlayingRef — after a high-speed stall, paused can lie.
    if (v.paused || !wantPlayingRef.current) {
      safePlay();
      setStatus("Playing at 1.0×", "ok");
    } else {
      safePause();
      setStatus("Paused", "ok");
    }
  }, [safePause, safePlay, setPlaybackRate, setStatus]);


  const jumpToClipStart = useCallback(() => {
    if (!currentVideo()) return;
    lastGoodTimeRef.current = 0;
    resumeTargetRef.current = 0;
    resumeHoldUntilRef.current = 0;
    scheduleSeek(0, true);
    const video = currentVideo();
    if (video?.path) {
      saveReviewSession(
        {
          path: video.path,
          name: video.name,
          scrubTime: 0,
          scanRoot: stateRef.current.scanRoot || undefined,
          force: true,
        },
        { force: true },
      );
    }
    setStatus("At start of clip (0:00)", "ok");
  }, [currentVideo, scheduleSeek, setStatus]);

  // ---------------------------------------------------------------------
  // Tasks
  // ---------------------------------------------------------------------
  const recentTaskRank = useCallback(
    (task: string) => {
      const key = task.toLowerCase();
      const idx = stateRef.current.recentTasks.findIndex((item: string) => item.toLowerCase() === key);
      return idx >= 0 ? idx : RECENT_TASKS_MAX + 1;
    },
    [],
  );

  const touchRecentTask = useCallback((task: string) => {
    const name = String(task || "").trim();
    if (!name) return;
    const key = name.toLowerCase();
    setRecentTasks((prev) => {
      const next = [name, ...prev.filter((item) => item.toLowerCase() !== key)].slice(0, RECENT_TASKS_MAX);
      saveRecentTasks(next);
      return next;
    });
  }, []);

  const orderedTaskGroups = useCallback(() => {
    const q = taskSearch.trim().toLowerCase();
    const pool = q ? tasks.filter((t) => t.toLowerCase().includes(q)) : [...tasks];
    const recent: string[] = [];
    const others: string[] = [];
    for (const t of pool) {
      if (recentTaskRank(t) <= RECENT_TASKS_MAX) recent.push(t);
      else others.push(t);
    }
    recent.sort((a, b) => recentTaskRank(a) - recentTaskRank(b));
    others.sort((a, b) => a.localeCompare(b));
    return { recent, others, matches: [...recent, ...others], filtering: Boolean(q) };
  }, [taskSearch, tasks, recentTaskRank]);

  const selectedTask = useCallback(() => {
    // Filter text must never become a task name — only an explicit list selection.
    return stateRef.current.selectedTaskValue?.trim() || "";
  }, []);

  const focusTaskSearch = useCallback(() => {
    taskSearchRef.current?.focus();
    taskSearchRef.current?.select();
    const last = stateRef.current.lastLabelTask;
    setStatus(
      stateRef.current.annotationsByPath[currentVideo()?.path || ""]?.pendingWork
        ? last
          ? `Enter = ${last} · ↑↓ filter matches · type to filter`
          : "Pick a task from the list — Enter assigns"
        : last
          ? `Enter selects ${last} · ↑↓ filter matches · type to filter`
          : "Pick a task from the list or add via New task",
      "ok",
    );
  }, [currentVideo, setStatus]);

  const leaveTaskSearch = useCallback((opts: { clear?: boolean } = {}) => {
    if (opts.clear) setTaskSearch("");
    taskSearchRef.current?.blur();
    playerWrapRef.current?.focus?.();
  }, []);

  const focusNewTask = useCallback(() => {
    const el = newTaskInputRef.current;
    if (!el) return;
    el.focus();
    el.select();
    setStatus("Type a new task name, then Enter to add", "ok");
  }, [setStatus]);

  // ---------------------------------------------------------------------
  // File list rendering helpers
  // ---------------------------------------------------------------------
  const isHandledPath = useCallback((path: string) => Boolean(annotationsByPath[path]?.complete), [annotationsByPath]);

  /** True when a clip is labelled end-to-end (100% covered / complete). */
  const isVideoFullyDone = useCallback((path?: string | null) => {
    if (!path) return false;
    const s = stateRef.current;
    const ann = s.annotationsByPath[path];
    if (!ann) return false;
    if (ann.complete) return true;
    const dur = Number(ann.duration) || Number(s.videos.find((v: VideoItem) => v.path === path)?.duration) || 0;
    if (dur <= 0) return false;
    const covered = (ann.segments || []).reduce(
      (acc: number, seg: Segment) => acc + Math.max(0, Number(seg.end) - Number(seg.start)),
      0,
    );
    return covered >= dur - 0.05;
  }, []);

  /** List: in ScaleAI, every clip is openable. Otherwise only current or 100% done. */
  const canOpenVideo = useCallback(
    (i: number) => {
      const s = stateRef.current;
      if (i < 0 || i >= s.videos.length) return false;
      if (s.scaleAiMode) return true;
      if (i === s.index) return true;
      return isVideoFullyDone(s.videos[i].path);
    },
    [isVideoFullyDone],
  );

  const nextIncompleteIndex = useCallback(
    (startAt?: number) => {
      const s = stateRef.current;
      const from = startAt ?? s.index + 1;
      for (let i = from; i < s.videos.length; i += 1) {
        if (!s.annotationsByPath[s.videos[i].path]?.complete) return i;
      }
      for (let i = 0; i < from; i += 1) {
        if (!s.annotationsByPath[s.videos[i].path]?.complete) return i;
      }
      return -1;
    },
    [],
  );

  const resumeTimeForPath = useCallback((path: string) => {
    // Prefer the last playhead from this browser session (survives reload).
    const saved = loadReviewSession();
    const savedT =
      sessionMatchesPath(saved, path) && Number.isFinite(saved?.scrubTime)
        ? Math.max(0, Number(saved?.scrubTime) || 0)
        : 0;
    const ann = stateRef.current.annotationsByPath[path];
    if (!ann) return savedT;
    if (ann.complete) return savedT;
    const segments = ann.segments || [];
    const anchor = segments.length
      ? anchorByPathRef.current[path] ?? computeAnchor(segments)
      : 0;
    const anchorT = Number.isFinite(anchor) && anchor > 0 ? anchor : 0;
    // Prefer the last scrub position; fall back to labelling anchor.
    return Math.max(savedT, anchorT);
  }, []);

  // ---------------------------------------------------------------------
  // Load video — zero-encode source ladder (LRV proxy → SSD copy → original)
  // ---------------------------------------------------------------------
  const stopFastPoll = useCallback(() => {
    if (fastPollRef.current) {
      clearInterval(fastPollRef.current);
      fastPollRef.current = null;
    }
  }, []);

  const stopCompatiblePreview = useCallback(() => {
    if (compatiblePreviewPollRef.current) {
      clearInterval(compatiblePreviewPollRef.current);
      compatiblePreviewPollRef.current = null;
    }
    compatiblePreviewPathRef.current = "";
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }
  }, []);

  const cancelFastJob = useCallback(async (path: string) => {
    if (!path) return;
    try {
      await api("/api/eager/fast/cancel", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
    } catch {
      /* ignore */
    }
  }, []);

  /** Index of the video we should warm next (prefer next unfinished, else i+1). */
  const nextPreviewTargetIndex = useCallback(
    (fromIndex: number) => {
      const s = stateRef.current;
      let next = nextIncompleteIndex(fromIndex + 1);
      if (next === fromIndex) next = -1;
      if (next < 0 && fromIndex + 1 < s.videos.length) next = fromIndex + 1;
      if (next < 0 || next === fromIndex) return -1;
      return next;
    },
    [nextIncompleteIndex],
  );

  /**
   * Ask the backend to have the fast source ready for the next few files.
   * Cheap and idempotent: LRV footage needs no work at all, and SSD copies are
   * plain file copies that run while the operator labels the current clip.
   */
  const warmFastSources = useCallback((fromIndex: number, list?: VideoItem[], count = 3) => {
    const videos: VideoItem[] = list || stateRef.current.videos || [];
    if (fromIndex < 0 || !videos.length) return;
    let queued = 0;
    for (let i = fromIndex; i < videos.length && queued < count; i += 1) {
      const path = videos[i]?.path;
      if (!path) continue;
      if (i !== fromIndex && stateRef.current.annotationsByPath[path]?.complete) continue;
      queued += 1;
      if (warmedFastRef.current.has(path)) continue;
      warmedFastRef.current.add(path);
      void api(`/api/eager/fast/status?path=${encodeURIComponent(path)}&start=1`).catch(() => null);
    }
  }, []);

  const sourceLabel = useCallback((kind: string, cached?: boolean) => {
    if (kind === "lrv") return cached ? "GoPro proxy (SSD) · 5× ready" : "GoPro proxy · 5× ready";
    if (kind === "ssd_copy") return "SSD copy · 5× ready";
    return "Original file";
  }, []);

  /**
   * Chrome cannot decode HEVC on Windows unless the optional system codec is
   * installed. If a direct source fails, build and attach the existing H.264
   * HLS preview instead of leaving a black player.
   */
  const startCompatiblePreview = useCallback(
    (path: string, token: number, startAt: number) => {
      if (!path || token !== previewTokenRef.current) return;
      if (compatiblePreviewPathRef.current === path) return;
      stopCompatiblePreview();
      compatiblePreviewPathRef.current = path;
      setLoadingVideo(true);
      setPreviewNote("Preparing browser-compatible preview…");
      setStatus("This video uses HEVC. Preparing a browser-compatible preview…");

      const attach = (url: string) => {
        const v = videoRef.current;
        if (!v || token !== previewTokenRef.current || !url) return;
        if (!Hls.isSupported()) {
          setLoadingVideo(false);
          setStatus("This browser cannot play the HEVC video or its HLS preview", "error");
          return;
        }
        if (compatiblePreviewPollRef.current) {
          clearInterval(compatiblePreviewPollRef.current);
          compatiblePreviewPollRef.current = null;
        }
        hlsRef.current?.destroy();
        const hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          backBufferLength: 30,
        });
        hlsRef.current = hls;
        activeSourceRef.current = "preview";
        const absolute = url.startsWith("http") ? url : host + url;
        mediaUrlRef.current = absolute;
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (token !== previewTokenRef.current) return;
          setLoadingVideo(false);
          setPreviewNote("Compatible 720p preview");
          setStatus(`Ready — ${mediaBasename(path)} · compatible 720p preview`, "ok");
          try {
            if (startAt > 0.05) v.currentTime = startAt;
          } catch {
            /* ignore */
          }
          applyMediaRate(playbackRateRef.current || 1);
          // A muted autoplay also forces Chromium to decode and paint the first
          // MSE frame; otherwise some Windows builds leave a valid HLS stream black.
          void safePlay();
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (!data.fatal || token !== previewTokenRef.current) return;
          setLoadingVideo(false);
          setStatus(data.details || "Could not load compatible preview", "error");
        });
        hls.loadSource(absolute);
        hls.attachMedia(v);
      };

      const tick = async () => {
        if (token !== previewTokenRef.current) {
          stopCompatiblePreview();
          return;
        }
        try {
          const st = await api(
            `/api/eager/preview/status?path=${encodeURIComponent(path)}&start=1&preempt=1`,
          );
          if (token !== previewTokenRef.current) return;
          if (st?.playable && st?.hls) {
            attach(String(st.hls));
            return;
          }
          if (st?.status === "error" || st?.status === "skipped") {
            stopCompatiblePreview();
            setLoadingVideo(false);
            setStatus(st?.error || st?.message || "Could not build compatible preview", "error");
            return;
          }
          const progress = Number(st?.progress) || 0;
          setPreviewNote(
            progress > 0
              ? `Preparing compatible preview · ${progress}%`
              : "Preparing browser-compatible preview…",
          );
        } catch (error: any) {
          stopCompatiblePreview();
          setLoadingVideo(false);
          setStatus(error?.message || "Could not build compatible preview", "error");
        }
      };
      compatiblePreviewPollRef.current = setInterval(tick, 750);
      void tick();
    },
    [applyMediaRate, safePlay, setStatus, stopCompatiblePreview],
  );

  /**
   * Attach a review source at an original-timeline position. Every rung of the
   * ladder is 1:1 with the original, so there is no time remapping.
   */
  const attachFastMedia = useCallback(
    (v: HTMLVideoElement, url: string, kind: string, startAt: number, token: number) => {
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
      const absolute = url.startsWith("http") ? url : host + url;
      mediaUrlRef.current = absolute;
      activeSourceRef.current = kind === "original" ? "original" : "fast";
      v.src = absolute;
      v.load();
      v.addEventListener(
        "loadedmetadata",
        () => {
          if (token !== previewTokenRef.current) return;
          try {
            if (startAt > 0.05) v.currentTime = startAt;
          } catch {
            /* ignore */
          }
          applyMediaRate(playbackRateRef.current || 1);
        },
        { once: true },
      );
    },
    [applyMediaRate],
  );

  /**
   * Hot-swap onto a better source (card original → finished SSD copy) without a
   * visible restart: freeze the frame, re-attach, resume at the same second and
   * the same rate.
   */
  const swapToFast = useCallback(
    (path: string, token: number, url: string, kind: string, cached?: boolean) => {
      const v = videoRef.current;
      if (!v || token !== previewTokenRef.current || !url) return;
      const absolute = url.startsWith("http") ? url : host + url;
      if (mediaUrlRef.current === absolute) return;

      const live = Number.isFinite(v.currentTime) ? v.currentTime : 0;
      const t = Math.max(
        live > 0.05 ? live : 0,
        Number(stateRef.current.scrubTime) || 0,
        lastGoodTimeRef.current,
      );
      const resume = wantPlayingRef.current || !v.paused;
      const rate = playbackRateRef.current || 1;

      const hideMask = showSwapMask();
      const unmask = () => {
        hideMask();
        v.removeEventListener("playing", unmask);
        v.removeEventListener("seeked", unmask);
      };
      v.addEventListener("playing", unmask);
      v.addEventListener("seeked", unmask);
      window.setTimeout(unmask, 3000);

      v.addEventListener(
        "loadedmetadata",
        () => {
          if (token !== previewTokenRef.current) return;
          setLoadingVideo(false);
          applyMediaRate(rate);
          try {
            if (t > 0.05) v.currentTime = t;
          } catch {
            /* ignore */
          }
          setScrubTime(t);
          lastGoodTimeRef.current = t;
          setPreviewNote(sourceLabel(kind, cached));
          saveReviewSession({
            path,
            name: mediaBasename(path),
            scrubTime: t,
            scanRoot: stateRef.current.scanRoot || undefined,
          });
          if (resume) {
            const go = () => {
              if (token === previewTokenRef.current) safePlay();
            };
            if (v.readyState >= 3) go();
            else v.addEventListener("canplay", go, { once: true });
          }
        },
        { once: true },
      );
      attachFastMedia(v, url, kind, t, token);
    },
    [applyMediaRate, attachFastMedia, safePlay, showSwapMask, sourceLabel],
  );

  /**
   * Keep the player on the best zero-encode source. LRV footage is already
   * final on the first response; when a background SSD copy lands we hot-swap
   * to it. Playback never waits for any of this.
   */
  const pollFastReady = useCallback(
    (path: string, token: number) => {
      stopFastPoll();
      const tick = async () => {
        if (token !== previewTokenRef.current) {
          stopFastPoll();
          return;
        }
        try {
          const st = await api(`/api/eager/fast/status?path=${encodeURIComponent(path)}&start=1`);
          if (token !== previewTokenRef.current) return;
          const kind = String(st?.kind || "original");
          const url = st?.url ? String(st.url) : "";
          if (st?.ready && url) {
            stopFastPoll();
            const absolute = url.startsWith("http") ? url : host + url;
            if (mediaUrlRef.current === absolute) {
              setPreviewNote(sourceLabel(kind, Boolean(st.cached)));
            } else {
              swapToFast(path, token, url, kind, Boolean(st.cached));
            }
            warmFastSources(stateRef.current.index + 1);
            return;
          }
          if (st?.status === "copying") {
            const pct = Number(st.progress) || 0;
            setPreviewNote(
              pct > 0 ? `Original file · SSD copy ${pct}%` : "Original file · copying to SSD",
            );
            return;
          }
          if (st?.status === "error") {
            stopFastPoll();
            setPreviewNote(sourceLabel("original"));
          }
        } catch {
          /* ignore transient poll errors */
        }
      };
      fastPollRef.current = setInterval(tick, 1500);
      void tick();
    },
    [sourceLabel, stopFastPoll, swapToFast, warmFastSources],
  );

  const loadVideo = useCallback(
    async (i: number, opts: { force?: boolean } = {}) => {
      const s = stateRef.current;
      if (i < 0 || i >= s.videos.length) return;
      const video: VideoItem = s.videos[i];

      // Click/nav without force: ScaleAI can open any clip; textile stays locked.
      if (!opts.force && !stateRef.current.scaleAiMode && i !== s.index && !isVideoFullyDone(video.path)) {
        setStatus("Only finished (100% covered) videos can be opened from the list", "error");
        return;
      }

      const previous = s.index >= 0 ? s.videos[s.index] : null;
      const upcomingIdx = nextPreviewTargetIndex(i);
      const upcomingPath = upcomingIdx >= 0 ? s.videos[upcomingIdx]?.path || "" : "";

      // Free the copy slot from a clip we've moved away from (finished caches stay).
      if (previous?.path && previous.path !== video.path && previous.path !== upcomingPath) {
        void cancelFastJob(previous.path);
      }

      setLoadingVideo(true);
      setIndex(i);
      setShareClipIn(null);
      setShareClipOut(null);
      setPreviewNote("");
      stopFastPoll();
      stopCompatiblePreview();
      mediaUrlRef.current = "";
      activeSourceRef.current = "original";
      if (seekTimerRef.current) {
        clearTimeout(seekTimerRef.current);
        seekTimerRef.current = null;
      }
      pendingSeekRef.current = null;

      const token = ++previewTokenRef.current;
      const resumeTime = resumeTimeForPath(video.path);
      resumeTargetRef.current = resumeTime;
      resumeHoldUntilRef.current = Date.now() + 5000;
      lastGoodTimeRef.current = resumeTime;
      setScrubTime(resumeTime);

      const knownDur =
        Number(video.duration) ||
        Number(s.annotationsByPath[video.path]?.duration) ||
        0;
      if (knownDur > 0) setDuration(knownDur);

      setStatus(
        resumeTime > 0
          ? `Loading ${video.name} at ${formatTime(resumeTime)}…`
          : `Loading ${video.name}...`,
      );

      const v = videoRef.current;
      if (!v) {
        setLoadingVideo(false);
        return;
      }

      wantPlayingRef.current = false;
      playGenerationRef.current += 1;
      playPromiseRef.current = null;

      const clearLoader = () => {
        if (token === previewTokenRef.current) setLoadingVideo(false);
      };
      const loaderCap = window.setTimeout(clearLoader, 1000);

      // Resolve the cheapest playable source. Nothing is encoded, so this is a
      // stat() on the backend — but never block the open on it.
      let fast: any = null;
      try {
        fast = await Promise.race([
          api(`/api/eager/fast/status?path=${encodeURIComponent(video.path)}&start=1`),
          new Promise((resolve) => window.setTimeout(() => resolve(null), 700)),
        ]);
      } catch {
        fast = null;
      }
      if (token !== previewTokenRef.current) return;

      const sourceKind = String(fast?.kind || "original");
      const sourceUrl = fast?.url
        ? String(fast.url)
        : `/api/eager/stream?path=${encodeURIComponent(video.path)}`;
      const sourceCached = Boolean(fast?.cached);

      const targetResume = () =>
        Math.max(resumeTargetRef.current, resumeTimeForPath(video.path));

      const applyResumeSeek = (startAt: number) => {
        if (token !== previewTokenRef.current || startAt <= 0.05) return;
        seekMediaToOriginal(startAt);
        setScrubTime(startAt);
        resumeTargetRef.current = startAt;
      };

      const onMeta = () => {
        if (token !== previewTokenRef.current) return;
        window.clearTimeout(loaderCap);
        clearLoader();
        v.pause();
        const startAt = targetResume();
        applyResumeSeek(startAt);
        const reseek = () => applyResumeSeek(targetResume());
        v.addEventListener("loadeddata", reseek, { once: true });
        v.addEventListener("canplay", reseek, { once: true });
        window.setTimeout(reseek, 250);
        window.setTimeout(reseek, 1000);
        window.setTimeout(reseek, 2500);
        setPlaybackRate(1, false);
        updateScrubUiFromEl();
        if (startAt > 0.05) {
          saveReviewSession({
            path: video.path,
            name: video.name,
            scrubTime: startAt,
            scanRoot: stateRef.current.scanRoot || undefined,
          });
        }
        const label = sourceLabel(sourceKind, sourceCached);
        setPreviewNote(label);
        setStatus(
          startAt > 0
            ? `Ready — ${video.name} at ${formatTime(startAt)} · ${label}`
            : `Ready — ${video.name} · ${label}`,
          "ok",
        );
      };
      v.addEventListener("loadedmetadata", onMeta, { once: true });
      v.addEventListener(
        "error",
        () => {
          if (token !== previewTokenRef.current) return;
          window.clearTimeout(loaderCap);
          startCompatiblePreview(video.path, token, targetResume());
        },
        { once: true },
      );
      window.setTimeout(clearLoader, 800);
      try {
        v.preload = "auto";
      } catch {
        /* ignore */
      }
      if (stateRef.current.scaleAiMode) {
        // ScaleAI delivery footage may be HEVC. Use the browser-compatible
        // source directly so Chromium never gets stuck on an undecodable MP4.
        v.pause();
        v.removeAttribute("src");
        v.load();
        startCompatiblePreview(video.path, token, resumeTime);
      } else {
        attachFastMedia(v, sourceUrl, sourceKind, resumeTime, token);
      }

      if (stateRef.current.scaleAiMode) {
        // ScaleAI uses only adjacent VIDEO.json — never touch *.segments.json.
        const scaleData = await loadScaleAiForPath(video.path);
        if (token !== previewTokenRef.current) return;
        setScaleAiPending(null);
        const scaleAnn = scaleData || stateRef.current.scaleAiByPath[video.path];
        const codec = String(scaleAnn?.media_meta?.video_codec || "").toLowerCase();
        // Chromium can parse HEVC metadata without being able to decode a
        // frame, so its media element may stay black and never emit `error`.
        if (codec === "hevc" || codec === "h265" || codec === "h.265") {
          startCompatiblePreview(video.path, token, targetResume());
        }
        const scaleDur = Number(scaleAnn?.duration_seconds) || 0;
        if (scaleDur > 0) setDuration(scaleDur);
        // Keep media strip populated without creating textile sidecars.
        setAnnotationsByPath((prev) => ({
          ...prev,
          [video.path]: {
            segments: [],
            duration: scaleDur || prev[video.path]?.duration || null,
            complete: false,
            pendingWork: null,
            mediaMeta: (scaleAnn as any)?.media_meta || prev[video.path]?.mediaMeta || null,
          },
        }));
      } else {
        await loadAnnotationForPath(video.path, { keepPending: true });
        if (token !== previewTokenRef.current) return;
      }

      const startAt = targetResume();
      resumeTargetRef.current = startAt;
      setScrubTime(startAt);
      applyResumeSeek(startAt);
      setTaskSelectionMode(
        stateRef.current.scaleAiMode
          ? Boolean(stateRef.current.scaleAiPending)
          : Boolean(stateRef.current.annotationsByPath[video.path]?.pendingWork),
      );
      if (!stateRef.current.scaleAiMode) {
        const annDur = Number(stateRef.current.annotationsByPath[video.path]?.duration) || 0;
        if (annDur > 0) setDuration(annDur);
      }

      // Upgrade to the SSD copy if one is still landing, and keep the next
      // couple of clips warm so N never waits.
      if (!fast?.ready) pollFastReady(video.path, token);
      warmFastSources(i, undefined, 3);
    },
    [
      attachFastMedia,
      cancelFastJob,
      isVideoFullyDone,
      loadAnnotationForPath,
      loadScaleAiForPath,
      nextPreviewTargetIndex,
      pollFastReady,
      resumeTimeForPath,
      seekMediaToOriginal,
      setPlaybackRate,
      setStatus,
      sourceLabel,
      startCompatiblePreview,
      stopCompatiblePreview,
      stopFastPoll,
      updateScrubUiFromEl,
      warmFastSources,
    ],
  );

  const finishCleaningFile = useCallback(async () => {
    const s = stateRef.current;
    const cur = s.index >= 0 ? s.videos[s.index] : null;
    if (cur && !isVideoFullyDone(cur.path)) {
      setStatus("Cover this video 100% before pressing N for the next one", "error");
      return;
    }
    const next = nextIncompleteIndex(s.index + 1);
    if (next >= 0 && next !== s.index) {
      await loadVideo(next, { force: true });
      setStatus("Moved to next unfinished video", "ok");
      return;
    }
    setStatus("All videos complete", "ok");
  }, [isVideoFullyDone, loadVideo, nextIncompleteIndex, setStatus]);

  const nextScaleAiVideo = useCallback(async () => {
    const s = stateRef.current;
    if (!s.videos.length) return;
    const next = s.index + 1;
    if (next >= s.videos.length) {
      setStatus("All videos in this task folder are marked", "ok");
      return;
    }
    await loadVideo(next, { force: true });
    setStatus("Moved to next video — JSON saved", "ok");
  }, [loadVideo, setStatus]);

  const deleteScaleAiSegment = useCallback(
    async (segmentId: string | number) => {
      const video = currentVideo();
      if (!video) return;
      try {
        const data = await api("/api/eager/scaleai/segments", {
          method: "DELETE",
          body: JSON.stringify({
            path: video.path,
            root: stateRef.current.scanRoot || "",
            segment_id: segmentId,
          }),
        });
        applyScaleAiPayload(video.path, data);
        setStatus("Deleted segment", "ok");
      } catch (error: any) {
        setStatus(error.message || "Could not delete segment", "error");
      }
    },
    [applyScaleAiPayload, currentVideo, setStatus],
  );

  const commitScaleAiSegment = useCallback(
    async (
      label: string,
      segmentType: "subtask" | "garbage" = "subtask",
      pendingOverride?: PendingWork | null,
    ) => {
      const video = currentVideo();
      const pending =
        pendingOverride !== undefined
          ? pendingOverride
          : (stateRef.current.scaleAiPending as PendingWork | null);
      if (!video || !pending) {
        setStatus("Press T to mark a segment first", "error");
        return false;
      }
      const clean = String(label || "").trim();
      if (segmentType === "subtask" && !clean) {
        setStatus("Type a label, then press Enter", "error");
        focusTaskSearch();
        return false;
      }
      try {
        const data = await api("/api/eager/scaleai/segments", {
          method: "POST",
          body: JSON.stringify({
            path: video.path,
            root: stateRef.current.scanRoot || "",
            start: pending.start,
            end: pending.end,
            label: segmentType === "garbage" ? "garbage" : clean,
            type: segmentType,
          }),
        });
        applyScaleAiPayload(video.path, data);
        setScaleAiPending(null);
        setTaskSelectionMode(false);
        if (segmentType === "subtask") {
          const canonical =
            (data.labels || []).find(
              (name: string) => name.toLowerCase() === clean.toLowerCase(),
            ) || clean;
          setSelectedTaskValue(canonical);
          setLastLabelTask(canonical);
          touchRecentTask(canonical);
          setTasks((prev) => {
            if (prev.some((t) => t.toLowerCase() === canonical.toLowerCase())) return prev;
            return [canonical, ...prev];
          });
          setTaskSearch("");
          setStatus(`Saved ${canonical} ${formatTime(pending.start)} → ${formatTime(pending.end)}`, "ok");
        } else {
          setStatus(`Saved garbage ${formatTime(pending.start)} → ${formatTime(pending.end)}`, "ok");
        }
        leaveTaskSearch();
        return true;
      } catch (error: any) {
        setStatus(error.message || "Could not save segment", "error");
        return false;
      }
    },
    [
      applyScaleAiPayload,
      currentVideo,
      focusTaskSearch,
      leaveTaskSearch,
      setStatus,
      touchRecentTask,
    ],
  );

  const markWork = useCallback(async () => {
    const video = currentVideo();
    if (!video) return;
    if (stateRef.current.scaleAiMode) {
      const annotation = currentScaleAi() || (await loadScaleAiForPath(video.path));
      const known =
        Number(annotation?.duration_seconds) ||
        Number(video.duration) ||
        knownDurationSec();
      const end = normalizeBoundary(currentScrubTime(), known);
      const segments = annotation?.segments || [];
      const coverage = segments.length
        ? Math.max(...segments.map((segment) => Number(segment.end) || 0))
        : 0;
      // Leave a 10ms gap after the previous segment so shared boundaries never overlap.
      const rawAnchor = Math.max(coverage, Number(anchorByPathRef.current[video.path]) || 0);
      const anchor = coverage > 0 ? Math.round((rawAnchor + 0.01) * 1000) / 1000 : rawAnchor;
      if (end <= anchor + 0.05) {
        const target = Math.min(Math.max(0, known - 0.04), anchor + 0.1);
        if (target <= anchor + 0.05) {
          setStatus("No unlabeled footage remains after the last marking", "error");
          return;
        }
        scheduleSeek(target, true);
        setStatus(
          `Moved to ${formatTime(target)} after the last marking — move forward, then press T`,
          "ok",
        );
        return;
      }
      setScaleAiPending({ start: anchor, end });
      stateRef.current.scaleAiPending = { start: anchor, end };
      setTaskSelectionMode(true);
      const last = stateRef.current.lastLabelTask || stateRef.current.selectedTaskValue;
      setStatus(
        last
          ? `Pending ${formatTime(anchor)} → ${formatTime(end)} — Enter for ${last}, or type a new label`
          : `Pending ${formatTime(anchor)} → ${formatTime(end)} — type a label and press Enter`,
        "ok",
      );
      focusTaskSearch();
      safePause();
      return;
    }
    await loadAnnotationForPath(video.path);
    const ann = annotationFor(video.path) || currentAnnotation();
    const known = knownDurationSec();
    const end = normalizeBoundary(currentScrubTime(), known);
    const lastEnd = ann?.segments.length
      ? Number(ann.segments[ann.segments.length - 1]?.end) || 0
      : 0;
    const coverage = coverageEnd(ann?.segments || []);
    const anchor = Math.max(lastEnd, coverage, Number(anchorByPathRef.current[video.path]) || 0);
    if (end <= anchor + 0.05) {
      setStatus("Move playhead past the current coverage to mark work", "error");
      return;
    }
    setAnnotationsByPath((prev) => {
      const cur = prev[video.path] || { segments: [], duration: null, complete: false, pendingWork: null };
      return {
        ...prev,
        [video.path]: {
          ...cur,
          pendingWork: { start: anchor, end },
        },
      };
    });
    setTaskSelectionMode(true);
    const last = stateRef.current.lastLabelTask || stateRef.current.selectedTaskValue;
    setStatus(
      last
        ? `Pending work ${formatTime(anchor)} → ${formatTime(end)} — Enter for ${last}, or pick from recent`
        : `Pending work ${formatTime(anchor)} → ${formatTime(end)} — choose a task and press Enter`,
      "ok",
    );
    focusTaskSearch();
  }, [
    annotationFor,
    currentAnnotation,
    currentScaleAi,
    currentScrubTime,
    currentVideo,
    focusTaskSearch,
    knownDurationSec,
    loadAnnotationForPath,
    loadScaleAiForPath,
    safePause,
    scheduleSeek,
    setStatus,
  ]);

  const assignPendingWork = useCallback(async (taskOverride?: string) => {
    const video = currentVideo();
    if (!video) return;
    if (stateRef.current.scaleAiMode) {
      const typed = String(stateRef.current.taskSearch || "").trim();
      const override = String(taskOverride || "").trim();
      const selected = String(selectedTask() || "").trim();
      // Prefer an explicit override / arrow-selected list item over filter text.
      let label = override || selected || typed || stateRef.current.lastLabelTask || "";
      label = String(label).trim();
      if (!label) {
        setStatus("Type a label, then press Enter", "error");
        focusTaskSearch();
        return;
      }
      const existing = stateRef.current.tasks.find(
        (item: string) => item.toLowerCase() === label.toLowerCase(),
      );
      await commitScaleAiSegment(existing || label, "subtask");
      return;
    }
    let ann = currentAnnotation();
    if (!ann) {
      await loadAnnotationForPath(video.path);
      ann = stateRef.current.annotationsByPath[video.path];
    }
    const pending = ann?.pendingWork;
    if (!pending) {
      setStatus("No pending work — press T to mark", "error");
      return;
    }
    let task = (taskOverride ?? selectedTask()).trim() || stateRef.current.lastLabelTask?.trim() || "";
    if (!task) {
      setStatus("Choose a task first", "error");
      focusTaskSearch();
      return;
    }
    const existing = stateRef.current.tasks.find(
      (item: string) => item.toLowerCase() === task.toLowerCase(),
    );
    if (!existing) {
      setStatus("Unknown task — pick from the list or add via New task", "error");
      focusTaskSearch();
      return;
    }
    task = existing;

    // Instant UI — don't wait on disk/API before clearing the task picker.
    const optimistic: Segment = {
      id: `local-${Date.now()}`,
      start: pending.start,
      end: pending.end,
      kind: "work",
      task,
    };
    setAnnotationsByPath((prev) => {
      const cur = prev[video.path] || { segments: [], duration: null, complete: false, pendingWork: null };
      const segments = [...(cur.segments || []), optimistic];
      anchorByPathRef.current[video.path] = pending.end;
      return { ...prev, [video.path]: { ...cur, segments, pendingWork: null } };
    });
    setLastLabelTask(task);
    touchRecentTask(task);
    setTaskSelectionMode(false);
    leaveTaskSearch({ clear: true });
    setSelectedTaskValue("");
    setStatus(`Assigned work to ${task}`, "ok");

    try {
      const data = await api("/api/eager/annotations/append", {
        method: "POST",
        body: JSON.stringify({
          path: video.path,
          kind: "work",
          end: pending.end,
          task,
          ...annotationContext(),
        }),
      });
      applyAnnotationPayload(video.path, data, { keepPending: false });
      if (stateRef.current.annotationsByPath[video.path]?.complete) void finishCleaningFile();
    } catch (error: any) {
      setStatus(error?.message || "Could not save task assignment", "error");
      await loadAnnotationForPath(video.path, { keepPending: true });
    }
  }, [
    annotationContext,
    applyAnnotationPayload,
    commitScaleAiSegment,
    currentAnnotation,
    currentVideo,
    finishCleaningFile,
    focusTaskSearch,
    leaveTaskSearch,
    loadAnnotationForPath,
    selectedTask,
    setStatus,
    touchRecentTask,
  ]);

  const markGarbage = useCallback(() => {
    const video = currentVideo();
    if (!video) return;
    if (stateRef.current.scaleAiMode) {
      if (stateRef.current.scaleAiPending) {
        void commitScaleAiSegment("garbage", "garbage");
        return;
      }
      const annotation = stateRef.current.scaleAiByPath[video.path] as ScaleAiAnnotation | undefined;
      const segments = annotation?.segments || [];
      const coverage = segments.length
        ? Math.max(...segments.map((segment) => Number(segment.end) || 0))
        : Number(anchorByPathRef.current[video.path]) || 0;
      const startAt =
        coverage > 0 ? Math.round((coverage + 0.01) * 1000) / 1000 : coverage;
      const known =
        Number(annotation?.duration_seconds) ||
        Number(video.duration) ||
        knownDurationSec();
      let end = Math.max(0, currentScrubTime());
      if (known > 0 && end >= known - 0.05) end = known;
      if (end <= startAt + 0.05) {
        setStatus(`Playhead must be after ${formatTime(startAt)} to mark garbage`, "error");
        return;
      }
      const pending = { start: startAt, end };
      setScaleAiPending(pending);
      stateRef.current.scaleAiPending = pending;
      void commitScaleAiSegment("garbage", "garbage", pending);
      return;
    }
    // Never block G on a network round-trip — use in-memory annotation only.
    const ann = currentAnnotation() || stateRef.current.annotationsByPath[video.path] || null;
    if (ann?.pendingWork) {
      setStatus("Assign task first", "error");
      return;
    }
    const anchor = anchorByPathRef.current[video.path] ?? computeAnchor(ann?.segments || []);
    const dur = knownDurationSec() || video.duration || annotationFor(video.path)?.duration || 0;
    let end = Math.max(0, currentScrubTime());
    if (dur > 0 && end >= dur - 0.05) end = dur;
    if (end <= anchor + 0.05) {
      setStatus(`Playhead must be after ${formatTime(anchor)} to mark garbage`, "error");
      return;
    }

    const optimistic: Segment = {
      id: `local-${Date.now()}`,
      start: anchor,
      end,
      kind: "garbage",
    };
    setAnnotationsByPath((prev) => {
      const cur = prev[video.path] || { segments: [], duration: null, complete: false, pendingWork: null };
      const segments = [...(cur.segments || []), optimistic];
      anchorByPathRef.current[video.path] = end;
      return { ...prev, [video.path]: { ...cur, segments, pendingWork: null } };
    });
    setStatus(`Marked garbage ${formatTime(anchor)} → ${formatTime(end)}`, "ok");

    const ctx = annotationContext();
    void (async () => {
      try {
        const data = await api("/api/eager/annotations/append", {
          method: "POST",
          body: JSON.stringify({ path: video.path, kind: "garbage", end, ...ctx }),
        });
        applyAnnotationPayload(video.path, data, { keepPending: false });
        if (stateRef.current.annotationsByPath[video.path]?.complete) void finishCleaningFile();
      } catch (error: any) {
        setStatus(error?.message || "Could not save garbage mark", "error");
        void loadAnnotationForPath(video.path);
      }
    })();
  }, [
    annotationContext,
    annotationFor,
    applyAnnotationPayload,
    currentAnnotation,
    currentScrubTime,
    currentVideo,
    finishCleaningFile,
    knownDurationSec,
    loadAnnotationForPath,
    setStatus,
  ]);

  // Manually set (ISO string) or clear ("") the recording timestamp; the
  // camera's own value is preserved server-side and restored on clear.
  const updateRecordedAt = useCallback(
    async (value: string) => {
      const video = currentVideo();
      if (!video) return;
      try {
        const data = await api("/api/eager/media-meta/recorded-at", {
          method: "POST",
          body: JSON.stringify({ path: video.path, recorded_at: value }),
        });
        applyAnnotationPayload(video.path, data, { keepPending: true });
        setStatus(value ? "Recording timestamp updated" : "Timestamp reset to camera value", "ok");
      } catch (error: any) {
        setStatus(error.message || "Could not update timestamp", "error");
      }
    },
    [applyAnnotationPayload, currentVideo, setStatus],
  );

  const deleteSegmentAt = useCallback(
    async (index: number) => {
      const video = currentVideo();
      if (!video) return;
      const ann = currentAnnotation();
      const segments = ann?.segments || [];
      if (index < 0 || index >= segments.length) {
        setStatus("Segment not found", "error");
        return;
      }
      const seg = segments[index];
      try {
        await api("/api/eager/annotations/delete-segment", {
          method: "POST",
          body: JSON.stringify({
            path: video.path,
            segment_id: seg?.id || undefined,
            index,
          }),
        });
        setTaskSelectionMode(false);
        leaveTaskSearch({ clear: true });
        await loadAnnotationForPath(video.path);
        setStatus("Deleted segment and everything after it", "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [currentAnnotation, currentVideo, leaveTaskSearch, loadAnnotationForPath, setStatus],
  );

  const undoSegment = useCallback(async () => {
    const video = currentVideo();
    if (!video) return;
    if (stateRef.current.scaleAiMode) {
      if (stateRef.current.scaleAiPending) {
        setScaleAiPending(null);
        setTaskSelectionMode(false);
        leaveTaskSearch({ clear: true });
        setStatus("Cleared pending segment", "ok");
        return;
      }
      try {
        const data = await api("/api/eager/scaleai/segments/undo", {
          method: "POST",
          body: JSON.stringify({
            path: video.path,
            root: stateRef.current.scanRoot || "",
          }),
        });
        applyScaleAiPayload(video.path, data);
        setStatus("Undid last segment", "ok");
      } catch (error: any) {
        setStatus(error.message || "Nothing to undo", "error");
      }
      return;
    }
    const ann = currentAnnotation();
    if (ann?.pendingWork) {
      setAnnotationsByPath((prev) => {
        const existing = prev[video.path];
        if (!existing) return prev;
        return { ...prev, [video.path]: { ...existing, pendingWork: null } };
      });
      setTaskSelectionMode(false);
      leaveTaskSearch({ clear: true });
      setStatus("Cleared pending work", "ok");
      return;
    }
    const last = (ann?.segments || []).length - 1;
    if (last < 0) {
      setStatus("No segments to undo", "error");
      return;
    }
    await deleteSegmentAt(last);
  }, [
    applyScaleAiPayload,
    currentAnnotation,
    currentVideo,
    deleteSegmentAt,
    leaveTaskSearch,
    setStatus,
  ]);

  const labelCurrentClip = useCallback(
    (taskOverride?: string) => assignPendingWork(taskOverride),
    [assignPendingWork],
  );

  const deleteCurrentFile = useCallback(async () => {
    const video = currentVideo();
    if (!video || stateRef.current.busy) return;
    if (typeof window !== "undefined") {
      const ok = window.confirm(
        `Move ${video.name} to Trash?\n\nUse this for accidental or unusable footage. This also removes it from the active batch.`,
      );
      if (!ok) return;
    }
    setBusy(true);
    safePause();
    setTaskSelectionMode(false);
    setStatus(`Moving ${video.name} to Trash…`);
    try {
      const data = await api("/api/eager/video/delete", {
        method: "POST",
        body: JSON.stringify({ path: video.path, batch_id: stateRef.current.batchId, confirmed: true }),
      });
      const deletedIndex = stateRef.current.index;
      setAnnotationsByPath((prev) => {
        const next = { ...prev };
        delete next[video.path];
        return next;
      });
      delete anchorByPathRef.current[video.path];
      const nextVideos = stateRef.current.videos.filter((v: VideoItem) => v.path !== video.path);
      setVideos(nextVideos);
      if (data.batch) setBatchDetail(data.batch);
      if (nextVideos.length) {
        await loadVideo(Math.min(deletedIndex, nextVideos.length - 1), { force: true });
      } else {
        setIndex(-1);
        mediaUrlRef.current = "";
        if (videoRef.current) {
          videoRef.current.removeAttribute("src");
          videoRef.current.load();
        }
      }
      setStatus(`${video.name} moved to Trash`, "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    } finally {
      setBusy(false);
    }
  }, [currentVideo, loadVideo, setStatus]);

  // ---------------------------------------------------------------------
  // Scan / workspace
  // ---------------------------------------------------------------------
  const setIdentityFromSelectedCard = useCallback(() => {
    const badge = selectedSdCardLabel();
    const cards = stateRef.current.batchDetail?.cards || [];
    const card = cards.find((c: any) => String(c.card_badge || "").toUpperCase() === String(badge || "").toUpperCase());
    setCardIdentity(
      card
        ? {
            factory: card.factory || stateRef.current.batchDetail?.factory || "",
            card_badge: card.card_badge || "",
            device_type: card.device_type || "",
            device_id: card.device_id || "",
          }
        : { factory: "", card_badge: "", device_type: "", device_id: "" },
    );
  }, [selectedSdCardLabel]);

  const loadBatchDetail = useCallback(async (id?: string | null) => {
    const useId = id ?? stateRef.current.batchId;
    if (!useId) return null;
    const data = await api(`/api/eager/batches/${encodeURIComponent(useId)}`);
    setBatchDetail(data.batch || null);
    return data.batch || null;
  }, []);

  const scanSource = useCallback(async (pathOverride?: string) => {
    const path = pathOverride?.trim() || scanTargetPath();
    if (!path) {
      setStatus("Open a footage folder first", "error");
      return;
    }

    setScanRoot(path);
    setLabelRoot(path);
    setStatus("Scanning...");
    
    setScanning(true);
    try {
      const scanMode = stateRef.current.scaleAiMode ? "scaleai" : "annotate";
      const data = await api("/api/eager/scan", {
        method: "POST",
        body: JSON.stringify({ path, recursive: true, mode: scanMode }),
      });

      if (stateRef.current.batchId) {
        const badge = selectedSdCardLabel();
        const cards = stateRef.current.batchDetail?.cards || [];
        const found = cards.find((c: any) => String(c.card_badge || "").toUpperCase() === String(badge || "").toUpperCase());
        if (badge && found) {
          const bind = await api(`/api/eager/batches/${encodeURIComponent(stateRef.current.batchId)}/bind-card`, {
            method: "POST",
            body: JSON.stringify({ card_badge: badge, mount_path: path, scan_path: path }),
          });
          setBatchDetail(bind.batch || stateRef.current.batchDetail);
        }
      }

      const freshVideos: VideoItem[] = data.videos || [];
      setVideos(freshVideos);
      setIndex(-1);
      setIdentityFromSelectedCard();

      if (!freshVideos.length) {
        setStatus("No footage found", "error");
        return;
      }

      const session = loadReviewSession();
      const savedIdx =
        session != null ? freshVideos.findIndex((v) => sessionMatchesPath(session, v.path)) : -1;

      // Open ASAP — do NOT wait to load every sidecar (that was 1–2 min on big cards).
      let openAt = savedIdx;
      if (stateRef.current.scaleAiMode) {
        // ScaleAI: never preload textile *.segments.json — only VIDEO.json via loadVideo.
        openAt = savedIdx >= 0 ? savedIdx : 0;
        warmFastSources(openAt, freshVideos, 3);
      } else if (openAt < 0) {
        openAt = 0;
        for (let i = 0; i < freshVideos.length; i += 1) {
          await loadAnnotationForPath(freshVideos[i].path);
          if (!stateRef.current.annotationsByPath[freshVideos[i].path]?.complete) {
            openAt = i;
            // Warm SSD copies now — before remaining annotation IO / player open.
            warmFastSources(i, freshVideos, 3);
            break;
          }
          openAt = i;
        }
      } else {
        await loadAnnotationForPath(freshVideos[openAt].path);
        warmFastSources(openAt, freshVideos, 3);
      }

      // Remaining textile labels load in the background while the player starts.
      if (!stateRef.current.scaleAiMode) {
        void Promise.all(
          freshVideos.map((v, idx) => (idx === openAt ? Promise.resolve() : loadAnnotationForPath(v.path))),
        );
      }

      await loadVideo(openAt, { force: true });
      // Keep the next unfinished clips warm so N opens instantly.
      warmFastSources(openAt, freshVideos, 3);
      const resumed = savedIdx >= 0 && (session?.scrubTime || 0) > 0;
      const onRemovable = Boolean(
        stateRef.current.sdCards.some(
          (c: SdCard) => (c.scan_path || c.path) === path || path.startsWith(c.path || ""),
        ),
      );
      setStatus(
        resumed
          ? `Resumed ${freshVideos[openAt]?.name} at ${formatTime(session!.scrubTime)}`
          : onRemovable
            ? `Found ${freshVideos.length} files — reviewing off the card (proxies stream direct, copies cached to SSD)`
            : `Found ${freshVideos.length} files — T/G annotate, Enter assign, N next unfinished`,
        "ok",
      );
    } catch (error: any) {
      setStatus(error.message, "error");
    } finally {
      setScanning(false);
    }
  }, [loadAnnotationForPath, loadVideo, scanTargetPath, selectedSdCardLabel, setIdentityFromSelectedCard, setStatus, warmFastSources]);

  const applySelectedPath = useCallback(
    (path: string) => {
      setScanRoot(path);
      setLabelRoot(path);
      setSdCardValue((prev) => {
        const exists = stateRef.current.sdCards.some((c: SdCard) => (c.scan_path || c.path) === path);
        // Opening a PC / external-drive folder must clear any prior DCIM card
        // selection, or later Scan calls keep returning SD footage.
        return exists ? path : "";
      });
      if (!stateRef.current.sdCards.some((c: SdCard) => (c.scan_path || c.path) === path)) {
        stateRef.current.sdCardValue = "";
      }
    },
    [],
  );

  const refreshSdCards = useCallback(
    async ({ quiet = false, autoScan = false }: { quiet?: boolean; autoScan?: boolean } = {}) => {
      if (!quiet) setStatus("Detecting SD cards…");
      setDetecting(true);
      try {
        const data = await api("/api/eager/sd-cards");
        const cards: SdCard[] = data.cards || [];
        setSdCards(cards);
        stateRef.current.sdCards = cards;
        if (!cards.length) {
          setSdCardValue("");
          stateRef.current.sdCardValue = "";
          if (!quiet) setStatus("No SD card detected", "error");
          return;
        }
        const first = cards[0]!;
        const chosen = first.scan_path || first.path || "";
        setSdCardValue(chosen);
        // Keep the ref in sync immediately — stable callbacks below read from it
        // before React has re-rendered, which previously broke the auto-scan.
        stateRef.current.sdCardValue = chosen;
        if (chosen) {
          applySelectedPath(chosen);
          stateRef.current.scanRoot = chosen;
          stateRef.current.labelRoot = chosen;
        }
        if (!quiet) setStatus(chosen ? `${first.id || first.label || "Card"} ready` : "Pick an SD card", "ok");
        if (autoScan && chosen) await scanSource(chosen);
      } catch (error: any) {
        if (!quiet) setStatus(error.message, "error");
      } finally {
        setDetecting(false);
      }
    },
    [applySelectedPath, scanSource, setStatus],
  );


  const chooseFootageFolder = useCallback(async () => {
    setStatus(
      stateRef.current.scaleAiMode
        ? "Choose the 50 hours folder (contains Google Drive and AWS)…"
        : "Choose footage on this computer or an external drive…",
    );
    try {
      const initial = stateRef.current.scanRoot || stateRef.current.sdCardValue || "";
      const query = initial ? `?initial=${encodeURIComponent(initial)}` : "";
      const data = await api(`/api/eager/pick-folder${query}`, { method: "POST" });
      if (data.cancelled) {
        setStatus("Folder selection cancelled");
        return;
      }
      const folderName = data.path.split(/[/\\]/).filter(Boolean).pop() || "Footage";
      applySelectedPath(data.path);
      stateRef.current.scanRoot = data.path;
      stateRef.current.labelRoot = data.path;
      stateRef.current.sdCardValue = "";
      setStatus(`Scanning ${folderName}…`);
      await scanSource(data.path);
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [applySelectedPath, scanSource, setStatus]);

  // ---------------------------------------------------------------------
  // Trim polling
  // ---------------------------------------------------------------------
  const applyGlobalTrimUi = useCallback((data: any) => {
    const active = Number(data?.active || 0);
    const jobs: TrimJob[] = Array.isArray(data?.jobs) ? data.jobs : [];
    setGlobalTrim({ active, jobs, etaTotal: Number(data?.eta_total_seconds || 0) });
  }, []);

  const pollGlobalTrims = useCallback(async () => {
    try {
      const data = await api("/api/eager/trim/active");
      applyGlobalTrimUi(data);
    } catch {
      /* ignore */
    }
  }, [applyGlobalTrimUi]);

  const startGlobalTrimPolling = useCallback(() => {
    if (globalTrimPollRef.current) return;
    pollGlobalTrims();
    globalTrimPollRef.current = setInterval(pollGlobalTrims, Math.max(1500, perf.trim_poll_ms || 1200));
  }, [perf.trim_poll_ms, pollGlobalTrims]);

  const cancelTrim = useCallback(
    async (jobId: string) => {
      if (!jobId) return;
      try {
        const data = await api("/api/eager/trim/cancel", {
          method: "POST",
          body: JSON.stringify({ job_id: jobId }),
        });
        applyGlobalTrimUi(data);
        setStatus("Cancelled trim", "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [applyGlobalTrimUi, setStatus],
  );

  const cancelAllTrims = useCallback(async () => {
    try {
      const data = await api("/api/eager/trim/cancel-all", { method: "POST" });
      applyGlobalTrimUi(data);
      setStatus(`Cancelled ${data.cancelled_count || 0} trim(s)`, "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [applyGlobalTrimUi, setStatus]);

  useEffect(() => {
    return () => {
      if (globalTrimPollRef.current) clearInterval(globalTrimPollRef.current);
      if (trimPollRef.current) clearInterval(trimPollRef.current);
      if (seekTimerRef.current) clearTimeout(seekTimerRef.current);
      if (fastPollRef.current) clearInterval(fastPollRef.current);
      if (compatiblePreviewPollRef.current) clearInterval(compatiblePreviewPollRef.current);
      hlsRef.current?.destroy();
      hlsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------------------------------------------------------------------
  // Tasks / batch / init
  // ---------------------------------------------------------------------
  const loadTasks = useCallback(async () => {
    const data = await api("/api/eager/tasks");
    setTasks(data.tasks || []);
    setDefaultTasks(data.default_tasks || []);
    if (data.profile === "scaleai") {
      setScaleAiModeState(true);
      try {
        localStorage.setItem("wc-scaleai-mode", "1");
      } catch {
        /* ignore */
      }
    }
  }, []);

  const setScaleAiMode = useCallback(
    async (on: boolean) => {
      setScaleAiModeState(on);
      try {
        localStorage.setItem("wc-scaleai-mode", on ? "1" : "0");
      } catch {
        /* ignore */
      }
      if (on) {
        // Leave any auto-detected DCIM card behind before ScaleAI labeling.
        setSdCardValue("");
        stateRef.current.sdCardValue = "";
      }
      try {
        const data = await api("/api/eager/tasks/profile", {
          method: "POST",
          body: JSON.stringify({ profile: on ? "scaleai" : "default" }),
        });
        setTasks(data.tasks || []);
        setDefaultTasks(data.default_tasks || []);
        setSelectedTaskValue("");
        if (on) {
          setScaleAiPending(null);
          const video = currentVideo();
          if (video) await loadScaleAiForPath(video.path);
        }
        setStatus(
          on
            ? "ScaleAI mode — Open 50-hour folder. Mark subtasks with T, Enter to label."
            : "Normal textile task list restored",
          "ok",
        );
      } catch (error: any) {
        setStatus(error.message || "Could not switch task profile", "error");
      }
    },
    [currentVideo, loadScaleAiForPath, setStatus],
  );

  const isUserDefinedTask = useCallback(
    (name: string) => {
      const key = name.trim().toLowerCase();
      if (!key) return false;
      return !defaultTasks.some((t) => t.toLowerCase() === key);
    },
    [defaultTasks],
  );

  const addTask = useCallback(
    async (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) {
        setStatus("Type a task name first", "error");
        return;
      }
      const already = stateRef.current.tasks.some(
        (t: string) => t.toLowerCase() === trimmed.toLowerCase(),
      );
      if (already) {
        setSelectedTaskValue(
          stateRef.current.tasks.find(
            (t: string) => t.toLowerCase() === trimmed.toLowerCase(),
          ) || trimmed,
        );
        setStatus(`Task already exists: ${trimmed}`, "ok");
        return;
      }
      // Show in the list immediately; persist in the background.
      setTasks((prev) => [trimmed, ...prev.filter((t) => t.toLowerCase() !== trimmed.toLowerCase())]);
      setSelectedTaskValue(trimmed);
      setTaskSearch("");
      setStatus(`Task added: ${trimmed}`, "ok");
      try {
        const data = await api("/api/eager/tasks", {
          method: "POST",
          body: JSON.stringify({
            name: trimmed,
            label_root: stateRef.current.labelRoot || stateRef.current.scanRoot || scanTargetPath(),
          }),
        });
        setTasks(data.tasks || []);
        setDefaultTasks(data.default_tasks || defaultTasks);
        const video = currentVideo();
        if (stateRef.current.scaleAiMode && video) {
          const parent = stateRef.current.scaleAiByPath[video.path]?.parent_task || video.parent_task;
          if (parent && stateRef.current.scanRoot) {
            const labels = await api("/api/eager/scaleai/labels", {
              method: "POST",
              body: JSON.stringify({
                root: stateRef.current.scanRoot,
                parent_task: parent,
                label: trimmed,
              }),
            });
            if (Array.isArray(labels.labels)) setTasks(labels.labels);
          }
          if (stateRef.current.scaleAiPending) {
            await commitScaleAiSegment(trimmed, "subtask");
          }
        }
      } catch (error: any) {
        setTasks((prev) => prev.filter((t) => t.toLowerCase() !== trimmed.toLowerCase()));
        setStatus(error.message, "error");
      }
    },
    [applyScaleAiPayload, currentVideo, defaultTasks, scanTargetPath, setStatus],
  );

  const removeTask = useCallback(
    async (name: string) => {
      const trimmed = name.trim();
      if (!trimmed) return;
      if (!isUserDefinedTask(trimmed)) {
        setStatus("Default tasks cannot be removed", "error");
        return;
      }
      try {
        const data = await api("/api/eager/tasks", {
          method: "DELETE",
          body: JSON.stringify({ name: trimmed }),
        });
        setTasks(data.tasks || []);
        setDefaultTasks(data.default_tasks || defaultTasks);
        const video = currentVideo();
        if (stateRef.current.scaleAiMode && video) {
          /* ScaleAI labels are per parent-task under _labeling/; global remove still updates UI list. */
        }
        if (stateRef.current.selectedTaskValue.toLowerCase() === trimmed.toLowerCase()) {
          setSelectedTaskValue("");
        }
        setStatus(`Removed task: ${trimmed}`, "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [applyScaleAiPayload, currentVideo, defaultTasks, isUserDefinedTask, setStatus],
  );

  const importBatchCsv = useCallback(
    async (csv: string) => {
      if (!csv.trim()) {
        setStatus("Paste batch CSV first", "error");
        return;
      }
      try {
        const data = await api("/api/eager/batches/import-csv", { method: "POST", body: JSON.stringify({ csv }) });
        setBatchDetail(data.batch || null);
        setBatchId(data.batch?.id || null);
        setIdentityFromSelectedCard();
        setStatus("Batch imported", "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [setIdentityFromSelectedCard, setStatus],
  );

  const refreshBatch = useCallback(async () => {
    try {
      if (!stateRef.current.batchId) {
        setStatus("No active batch", "error");
        return;
      }
      await loadBatchDetail(stateRef.current.batchId);
      setIdentityFromSelectedCard();
      setStatus("Batch refreshed", "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [loadBatchDetail, setIdentityFromSelectedCard, setStatus]);

  const finishCard = useCallback(async () => {
    const badge = stateRef.current.cardIdentity.card_badge || selectedSdCardLabel();
    if (!stateRef.current.batchId || !badge) {
      setStatus("Select a batch card first", "error");
      return;
    }
    try {
      const data = await api(`/api/eager/batches/${encodeURIComponent(stateRef.current.batchId)}/finish-card`, {
        method: "POST",
        body: JSON.stringify({ card_badge: badge }),
      });
      setBatchDetail(data.batch || stateRef.current.batchDetail);
      setStatus(`Card ${badge} finished`, "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [selectedSdCardLabel, setStatus]);

  const completeBatch = useCallback(async () => {
    if (!stateRef.current.batchId) {
      setStatus("No active batch", "error");
      return;
    }
    try {
      const data = await api(`/api/eager/batches/${encodeURIComponent(stateRef.current.batchId)}/complete`, { method: "POST" });
      setBatchDetail(data.batch || stateRef.current.batchDetail);
      setStatus("Batch complete", "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [setStatus]);

  const queueWorkSegments = useCallback(
    async (paths: string[], label: string) => {
      if (!paths.length) {
        setStatus("No videos to queue", "error");
        return;
      }
      const deleteSource = Boolean(stateRef.current.deleteSourceAfterTrim);
      try {
        const data = await api("/api/eager/trim/queue-work", {
          method: "POST",
          body: JSON.stringify({ paths, delete_source: deleteSource }),
        });
        const queued = Number(data.queued || 0);
        const skipped = Number(data.skipped || 0);
        const parts = [`Queued ${queued} work segment${queued === 1 ? "" : "s"} ${label}`];
        if (skipped) parts.push(`${skipped} already trimmed/queued`);
        if (deleteSource) {
          if (data.deleted_now) parts.push(`deleted ${data.deleted_now} raw`);
          else if (data.finish_scheduled) parts.push("raw will be deleted after trims finish");
          else parts.push("delete source on");
        }
        if (Array.isArray(data.errors) && data.errors.length) parts.push(`${data.errors.length} error(s)`);
        setStatus(parts.join(" · "), data.errors?.length ? "error" : "ok");
        startGlobalTrimPolling();
        await pollGlobalTrims();
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [pollGlobalTrims, setStatus, startGlobalTrimPolling],
  );

  const queueClips = useCallback(async () => {
    const current = currentVideo();
    if (!current) return;
    // Ensure the latest annotation sidecar exists before the server reads it.
    if (!currentAnnotation()) await loadAnnotationForPath(current.path);
    await queueWorkSegments([current.path], `for ${current.name}`);
  }, [currentAnnotation, currentVideo, loadAnnotationForPath, queueWorkSegments]);

  const queueAllClips = useCallback(async () => {
    const paths = stateRef.current.videos.map((v: VideoItem) => v.path);
    await queueWorkSegments(paths, "across all videos");
  }, [queueWorkSegments]);

  /** ScaleAI: trim current video into VIDEO_STEM/subtask/ clips. */
  const processScaleAiVideo = useCallback(async () => {
    const current = currentVideo();
    if (!current) return;
    try {
      const data = await api("/api/eager/scaleai/process-video", {
        method: "POST",
        body: JSON.stringify({
          path: current.path,
          root: stateRef.current.scanRoot || "",
        }),
      });
      setStatus(data.message || `Queued ${data.queued || 0} trim(s)`, "ok");
      startGlobalTrimPolling();
      await pollGlobalTrims();
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [currentVideo, pollGlobalTrims, setStatus, startGlobalTrimPolling]);

  const processCurrentVideoScaleAi = processScaleAiVideo;

  const processScaleAiFolder = useCallback(async () => {
    const root = String(stateRef.current.scanRoot || "").trim();
    if (!root) {
      setStatus("Open a 50-hour folder first", "error");
      return;
    }
    try {
      const data = await api("/api/eager/scaleai/process-folder", {
        method: "POST",
        body: JSON.stringify({ root }),
      });
      setStatus(
        `Queued ${data.queued || 0} subtask trims across ${data.source_count || 0} video(s)`,
        data.errors?.length ? "error" : "ok",
      );
      startGlobalTrimPolling();
      await pollGlobalTrims();
    } catch (error: any) {
      setStatus(error.message || "Could not process ScaleAI folder", "error");
    }
  }, [pollGlobalTrims, setStatus, startGlobalTrimPolling]);

  const stitchScaleAiVideo = useCallback(async () => {
    const current = currentVideo();
    if (!current) return;
    setStatus(`Stitching subtask clips for ${current.name}…`);
    try {
      const data = await api("/api/eager/scaleai/stitch-video", {
        method: "POST",
        body: JSON.stringify({
          path: current.path,
          overwrite: true,
        }),
      });
      const gpmfOk = (data.results || []).every(
        (row: any) => !row.ok || row.has_gpmf !== false,
      );
      setStatus(
        data.message ||
          `Stitched ${data.stitched || 0}/${data.task_count || 0} subtask(s)` +
            (gpmfOk ? " · GPMF/IMU preserved" : " · check GPMF warnings"),
        data.errors?.length ? "error" : "ok",
      );
      if (data.errors?.length) {
        toast.error(String(data.errors[0]));
      }
    } catch (error: any) {
      setStatus(error.message || "Could not stitch video", "error");
    }
  }, [currentVideo, setStatus]);

  const markShareIn = useCallback(() => {
    if (!currentVideo()) return;
    const t = Math.max(0, currentScrubTime());
    setShareClipIn(t);
    setShareClipOut((out) => (out != null && out <= t ? null : out));
    setStatus(`Share in: ${formatTime(t)}`, "ok");
  }, [currentScrubTime, currentVideo, setStatus]);

  const markShareOut = useCallback(() => {
    if (!currentVideo()) return;
    const t = Math.max(0, currentScrubTime());
    const inn = shareClipIn;
    if (inn == null) {
      setStatus("Mark share in first (I)", "error");
      return;
    }
    if (t <= inn + 0.05) {
      setStatus("Share out must be after in", "error");
      return;
    }
    setShareClipOut(t);
    setStatus(`Share out: ${formatTime(t)} (${formatTime(t - inn)} clip)`, "ok");
  }, [currentScrubTime, currentVideo, setStatus, shareClipIn]);

  const clearShareClip = useCallback(() => {
    setShareClipIn(null);
    setShareClipOut(null);
    setStatus("Cleared share clip marks", "ok");
  }, [setStatus]);

  const downloadShareClip = useCallback(async () => {
    const video = currentVideo();
    if (!video) return;
    if (shareClipIn == null || shareClipOut == null) {
      setStatus("Mark share in (I) and out (O) first", "error");
      return;
    }
    if (shareClipOut <= shareClipIn + 0.05) {
      setStatus("Share out must be after in", "error");
      return;
    }
    setShareClipBusy(true);
    setStatus(`Encoding WhatsApp clip (${shareClipQuality})…`, "ok");
    try {
      // POST encodes + returns a short-lived GET URL. Opening that GET lets
      // IDM/browser download managers re-request the same file (POST body cannot).
      const prepared = await api<{
        download_url: string;
        filename?: string;
        quality?: string;
      }>("/api/eager/share-clip", {
        method: "POST",
        body: JSON.stringify({
          path: video.path,
          start: shareClipIn,
          end: shareClipOut,
          quality: shareClipQuality,
        }),
      });
      if (!prepared?.download_url) {
        throw new Error("Encode succeeded but no download URL was returned");
      }
      openDownloadUrl(prepared.download_url);
      setStatus(`WhatsApp clip ready (${prepared.quality || shareClipQuality})`, "ok");
    } catch (error: any) {
      setStatus(error.message || "Share clip failed", "error");
    } finally {
      setShareClipBusy(false);
    }
  }, [currentVideo, setStatus, shareClipIn, shareClipOut, shareClipQuality]);

  // Persist playhead so reload resumes at the same timeline position.
  useEffect(() => {
    if (loadingVideo) return;
    const video = currentVideo();
    if (!video?.path) return;
    const t = stateRef.current.scrubTime || 0;
    // Skip noise while settling at 0:00 right after open.
    if (t < 0.25) return;
    const handle = window.setTimeout(() => {
      saveReviewSession({
        path: video.path,
        name: video.name,
        scrubTime: stateRef.current.scrubTime || 0,
        scanRoot: stateRef.current.scanRoot || undefined,
      });
    }, 200);
    return () => window.clearTimeout(handle);
  }, [scrubTime, index, loadingVideo, currentVideo]);

  // Also persist from the live <video> clock (covers pause + skipped React ticks).
  useEffect(() => {
    const tick = () => {
      if (loadingVideoRef.current) return;
      const video = stateRef.current.videos[stateRef.current.index];
      const el = videoRef.current;
      if (!video?.path || !el) return;
      const live = Number.isFinite(el.currentTime) ? el.currentTime : 0;
      const scrub = Number(stateRef.current.scrubTime) || 0;
      const t = Math.max(live, scrub);
      if (t < 0.25) return;
      if (el.paused && Math.abs(scrub - live) > 0.2 && live > 0.05) {
        setScrubTime(live);
      }
      saveReviewSession({
        path: video.path,
        name: video.name,
        scrubTime: t,
        scanRoot: stateRef.current.scanRoot || undefined,
      });
    };
    const handle = window.setInterval(tick, 1000);
    return () => window.clearInterval(handle);
  }, []);

  useEffect(() => {
    const flush = () => {
      const video = stateRef.current.videos[stateRef.current.index];
      const el = videoRef.current;
      if (!video?.path) return;
      const live = el && Number.isFinite(el.currentTime) ? el.currentTime : 0;
      const t = Math.max(Number(stateRef.current.scrubTime) || 0, live);
      if (t < 0.25) return;
      saveReviewSession({
        path: video.path,
        name: video.name,
        scrubTime: t,
        scanRoot: stateRef.current.scanRoot || undefined,
        force: true,
      });
    };
    window.addEventListener("beforeunload", flush);
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("beforeunload", flush);
      window.removeEventListener("pagehide", flush);
    };
  }, []);

  // Init on mount.
  useEffect(() => {
    setRecentTasks(loadRecentTasks());
    let cancelled = false;
    (async () => {
      try {
        // ScaleAI branch defaults to two-stage mode unless the operator opted out.
        if (localStorage.getItem("wc-scaleai-mode") == null) {
          localStorage.setItem("wc-scaleai-mode", "1");
          setScaleAiModeState(true);
          stateRef.current.scaleAiMode = true;
        }
        await loadTasks();
        // Prefer local ScaleAI preference so weak label PCs stay on micro-task profile.
        if (stateRef.current.scaleAiMode) {
          try {
            const data = await api("/api/eager/tasks/profile", {
              method: "POST",
              body: JSON.stringify({ profile: "scaleai" }),
            });
            setTasks(data.tasks || []);
            setDefaultTasks(data.default_tasks || []);
          } catch {
            /* ignore */
          }
        }
        const ws = createWorkspace("Footage");
        setWorkspaces([ws]);
        setActiveWorkspaceId(ws.id);
        const [health, perfData, batches] = await Promise.all([
          api("/api/health"),
          api("/api/eager/config"),
          api("/api/eager/batches"),
          // ScaleAI stations label from the 50 hours drive folder. Do not
          // auto-open whatever DCIM/###GOPRO card happens to be mounted.
          refreshSdCards({
            quiet: true,
            autoScan: !stateRef.current.scaleAiMode,
          }),
        ]);
        if (cancelled) return;
        setAppVersion(health.version || "");
        setPerf((prev) => ({ ...prev, ...perfData }));
        // After an Update/restart, celebrate the new build once.
        const sha = String(health.git_sha || "").trim();
        if (sha) {
          const prev = localStorage.getItem(SEEN_GIT_SHA_KEY) || "";
          if (prev && prev !== sha) {
            toast.success(`Updated to v${health.version || "?"} (${sha})`);
          }
          localStorage.setItem(SEEN_GIT_SHA_KEY, sha);
        }
        const open = (batches?.batches || []).find((b: any) => b.status !== "complete") || null;
        if (open?.id) {
          setBatchId(open.id);
          await loadBatchDetail(open.id);
          setIdentityFromSelectedCard();
          startGlobalTrimPolling();
          setStatus("Batch workflow ready — insert matching card and annotate contiguously", "ok");
        } else {
          startGlobalTrimPolling();
          setStatus("No active batch — import CSV to start batch workflow", "ok");
        }
        if (health.ffmpeg_ok === false) {
          setFfmpegHint(health.ffmpeg_hint || "FFmpeg missing — install and restart");
        }
      } catch (error: any) {
        setStatus(error?.message || "Failed to initialize", "error");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Video element listeners.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;

    let lastUiTick = 0;
    const onTimeUpdate = () => {
      if (!Number.isFinite(v.currentTime)) return;
      const originalT = v.currentTime;
      const state = stateRef.current;
      const target = resumeTargetRef.current;
      // While resuming, fight browsers that snap the playhead back to 0.
      if (Date.now() < resumeHoldUntilRef.current && target > 0.5 && originalT < 0.35) {
        seekMediaToOriginal(target);
        setScrubTime(target);
        return;
      }
      // If playhead snaps to ~0 mid-review, restore the last good time immediately.
      if (
        !loadingVideoRef.current &&
        Date.now() >= resumeHoldUntilRef.current &&
        originalT < 0.35 &&
        lastGoodTimeRef.current > 1 &&
        wantPlayingRef.current
      ) {
        seekMediaToOriginal(lastGoodTimeRef.current);
        setScrubTime(lastGoodTimeRef.current);
        return;
      }
      if (originalT > 0.5) {
        lastGoodTimeRef.current = originalT;
      }
      if (!v.paused && originalT > 0) {
        // Throttle React updates — every scrub tick re-renders the whole
        // review tree, and that main-thread work competes with playback.
        const now = performance.now();
        if (now - lastUiTick < 150) return;
        lastUiTick = now;
        setScrubTime(originalT);
      }
    };
    const onLoadedMeta = () => updateScrubUiFromEl();
    const onPlay = () => {
      wantPlayingRef.current = true;
      setIsPlaying(true);
      applyMediaRate(playbackRateRef.current || 1);
    };
    const onPause = () => {
      // Ignore transient pauses while a play attempt is still intentional.
      if (wantPlayingRef.current && playPromiseRef.current) return;
      setIsPlaying(false);
    };
    const onEnded = () => {
      wantPlayingRef.current = false;
      playPromiseRef.current = null;
      setIsPlaying(false);
    };
    const onWaiting = () => {
      // Playback underflowed. Give the buffer 450ms to refill on its own,
      // then nudge the decoder back onto the last good position.
      if (!wantPlayingRef.current) return;
      window.setTimeout(() => {
        const el = videoRef.current;
        if (!el || !wantPlayingRef.current) return;
        if (!el.paused && el.readyState >= 2) return;
        const live = Number.isFinite(el.currentTime) ? el.currentTime : 0;
        const good = Math.max(lastGoodTimeRef.current, Number(stateRef.current.scrubTime) || 0);
        const t = live < 0.35 && good > 1 ? good : Math.max(live, 0);
        seekMediaToOriginal(t);
        if (t > 0.5) lastGoodTimeRef.current = t;
        applyMediaRate(playbackRateRef.current || 1);
        safePlay();
      }, 450);
    };

    v.addEventListener("timeupdate", onTimeUpdate);
    v.addEventListener("loadedmetadata", onLoadedMeta);
    v.addEventListener("play", onPlay);
    v.addEventListener("pause", onPause);
    v.addEventListener("ended", onEnded);
    v.addEventListener("waiting", onWaiting);
    v.addEventListener("stalled", onWaiting);
    return () => {
      v.removeEventListener("timeupdate", onTimeUpdate);
      v.removeEventListener("loadedmetadata", onLoadedMeta);
      v.removeEventListener("play", onPlay);
      v.removeEventListener("pause", onPause);
      v.removeEventListener("ended", onEnded);
      v.removeEventListener("waiting", onWaiting);
      v.removeEventListener("stalled", onWaiting);
    };
  }, [applyMediaRate, safePause, safePlay, seekMediaToOriginal, setStatus, updateScrubUiFromEl]);

  return {
    videoRef,
    swapCanvasRef,
    playerWrapRef,
    taskSearchRef,
    newTaskInputRef,
    videos,
    index,
    scanRoot,
    tasks,
    annotationsByPath,
    anchorByPathRef,
    batchId,
    batchDetail,
    cardIdentity,
    sdCards,
    sdCardValue,
    setSdCardValue,
    workspaces,
    activeWorkspaceId,
    recentTasks,
    lastLabelTask,
    setLastLabelTask,
    taskSearch,
    setTaskSearch,
    selectedTaskValue,
    setSelectedTaskValue,
    taskSelectionMode,
    status,
    setStatus,
    scrubTime,
    duration,
    playbackRate,
    isPlaying,
    loadingVideo,
    previewNote,
    globalTrim,
    busy,
    scanning,
    detecting,
    ffmpegHint,
    deleteSourceAfterTrim,
    setDeleteSourceAfterTrim,
    shareClipIn,
    shareClipOut,
    shareClipBusy,
    shareClipQuality,
    setShareClipQuality,
    markShareIn,
    markShareOut,
    clearShareClip,
    downloadShareClip,
    deleteSegmentAt,
    updateRecordedAt,
    appVersion,
    currentVideo,
    currentAnnotation,
    annotationFor,
    orderedTaskGroups,
    selectedTask,
    focusTaskSearch,
    focusNewTask,
    leaveTaskSearch,
    touchRecentTask,
    scheduleSeek,
    seekToFraction,
    fineTune,
    scrubStepSeconds,
    bumpPlaybackRate,
    togglePlay,
    jumpToClipStart,
    loadVideo,
    finishCleaningFile,
    markWork,
    markGarbage,
    assignPendingWork,
    undoSegment,
    labelCurrentClip,
    deleteCurrentFile,
    scanSource,
    chooseFootageFolder,
    refreshSdCards,
    addTask,
    removeTask,
    isUserDefinedTask,
    importBatchCsv,
    refreshBatch,
    finishCard,
    completeBatch,
    queueClips,
    queueAllClips,
    processScaleAiVideo,
    processCurrentVideoScaleAi,
    processScaleAiFolder,
    stitchScaleAiVideo,
    scaleAiMode,
    setScaleAiMode,
    scaleAiByPath,
    scaleAiPending,
    scaleAiProgress,
    currentScaleAi,
    scaleAiTaskProgress,
    videosInCurrentParentTask,
    deleteScaleAiSegment,
    commitScaleAiSegment,
    nextScaleAiVideo,
    cancelTrim,
    cancelAllTrims,
    formatTime,
    formatDurationShort,
    basenamePath,
    shortCardTitle,
    isHandledPath,
    canOpenVideo,
  };
}

export type ReviewController = ReturnType<typeof useReviewController>;
