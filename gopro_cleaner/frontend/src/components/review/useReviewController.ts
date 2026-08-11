import { useCallback, useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { toast } from "sonner";
import { api, formatClock, host, openDownloadUrl } from "@/lib/api";
import type {
  Annotation,
  BatchDetail,
  CardIdentity,
  SdCard,
  Segment,
  StatusKind,
  TrimJob,
  VideoItem,
  Workspace,
} from "./types";

const RECENT_TASKS_KEY = "gopro_eager_recent_tasks";
const RECENT_TASKS_MAX = 10;
const PLAYBACK_RATE_MIN = 0.5;
const PLAYBACK_RATE_MAX = 8;
const PLAYBACK_RATE_STEP = 0.5;

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

  const previewTokenRef = useRef(0);
  const previewPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prefetchedPreviewRef = useRef<string>("");
  // hls.js instance + the playlist URL it is playing (previews stream as HLS
  // so playback can start a few seconds into the build).
  const hlsRef = useRef<Hls | null>(null);
  const hlsUrlRef = useRef<string>("");
  const seekTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSeekRef = useRef<number | null>(null);
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
  };

  const setStatus = useCallback((message: string, kind: StatusKind = "") => {
    setStatusState({ message: message || "", kind });
    if (kind === "error" && message) toast.error(message);
  }, []);

  const currentVideo = useCallback((): VideoItem | null => {
    const s = stateRef.current;
    return s.index >= 0 ? s.videos[s.index] || null : null;
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

  const annotationContext = useCallback(() => {
    const id = stateRef.current.cardIdentity as CardIdentity;
    const batch = stateRef.current.batchDetail as BatchDetail | null;
    const video = currentVideo();
    const dur = videoRef.current?.duration || video?.duration || annotationFor(video?.path)?.duration || undefined;
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
    // While an HLS preview is still encoding, the element duration only covers
    // the segments built so far — keep the scrub bar at the known full length.
    const el = Number.isFinite(v.duration) ? v.duration : 0;
    const video = currentVideo();
    const knownFile = Number(video?.duration) || 0;
    const knownAnn = Number(stateRef.current.annotationsByPath[video?.path || ""]?.duration) || 0;
    setDuration(Math.max(el, knownFile, knownAnn) || 0);
  }, [currentVideo]);

  /** Full clip length even when HLS only exposes partial duration. */
  const knownDurationSec = useCallback(() => {
    const v = videoRef.current;
    const video = currentVideo();
    const el = v && Number.isFinite(v.duration) ? v.duration : 0;
    const knownFile = Number(video?.duration) || 0;
    const knownAnn = Number(stateRef.current.annotationsByPath[video?.path || ""]?.duration) || 0;
    return Math.max(el, knownFile, knownAnn) || 0;
  }, [currentVideo]);

  // Guard against "play() interrupted by pause()" and hung play() promises.
  // At high playback rates (5–8×) Chrome can stall progressive MP4 streams;
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
        if (el.ended || (Number.isFinite(el.duration) && el.currentTime >= el.duration - 0.05)) {
          el.currentTime = 0;
        }
      } catch {
        /* ignore */
      }

      try {
        el.playbackRate = playbackRateRef.current || 1;
      } catch {
        /* ignore */
      }

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
          if (cur) {
            try {
              cur.playbackRate = playbackRateRef.current || 1;
            } catch {
              /* ignore */
            }
          }
        })
        .catch(() => {
          if (generation !== playGenerationRef.current || !wantPlayingRef.current) return;
          if (isRetry) return;
          // Decoder/network stall recovery: nudge timeline then retry once.
          const cur = videoRef.current;
          if (!cur) return;
          try {
            const t = Number.isFinite(cur.currentTime) ? cur.currentTime : 0;
            cur.pause();
            cur.currentTime = Math.max(0, t);
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
        try {
          v.currentTime = target;
        } catch {
          /* ignore */
        }
      }
      pendingSeekRef.current = null;
    }
  }, [safePause]);

  const currentScrubTime = useCallback(() => {
    flushSeek();
    return stateRef.current.scrubTime;
  }, [flushSeek]);

  const scheduleSeek = useCallback(
    (time: number, immediate = false) => {
      const dur = knownDurationSec();
      if (!dur) return;
      // Always stop at the true end — never past it, even when HLS duration is short.
      const clamped = Math.min(Math.max(0, time), Math.max(0, dur - 0.04));
      setScrubTime(clamped);
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
      scheduleSeek(stateRef.current.scrubTime + seconds, true);
    },
    [currentVideo, scheduleSeek],
  );

  const setPlaybackRate = useCallback((rate: number, announce = true) => {
    const v = videoRef.current;
    // Allow full 0.5–8× even while the 720p preview is still building (original stream).
    const clamped = Math.min(
      PLAYBACK_RATE_MAX,
      Math.max(PLAYBACK_RATE_MIN, Math.round(rate / PLAYBACK_RATE_STEP) * PLAYBACK_RATE_STEP),
    );
    playbackRateRef.current = clamped;
    if (v) {
      const wasPlaying = wantPlayingRef.current || !v.paused;
      try {
        v.playbackRate = clamped;
      } catch {
        /* ignore */
      }
      if (wasPlaying && v.paused) safePlay();
      else if (wasPlaying) {
        try {
          v.playbackRate = clamped;
        } catch {
          /* ignore */
        }
      }
    }
    setPlaybackRateState(clamped);
    if (announce) setStatus(`Playback ${clamped.toFixed(1)}×`, "ok");
  }, [safePlay, setStatus]);

  const bumpPlaybackRate = useCallback(
    (delta: number) => {
      if (!currentVideo()) return;
      const v = videoRef.current;
      setPlaybackRate((v?.playbackRate || playbackRateRef.current || 1) + delta);
      if (v?.paused || !wantPlayingRef.current) {
        // Speeding up while paused/stalled means "skim ahead".
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
    scheduleSeek(0, true);
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

  /** List: current clip, or any clip already done. Never open unfinished others. */
  const canOpenVideo = useCallback(
    (i: number) => {
      const s = stateRef.current;
      if (i < 0 || i >= s.videos.length) return false;
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
    const ann = stateRef.current.annotationsByPath[path];
    if (!ann || ann.complete) return 0;
    const segments = ann.segments || [];
    if (!segments.length) return 0;
    const anchor = anchorByPathRef.current[path] ?? computeAnchor(segments);
    return Number.isFinite(anchor) && anchor > 0 ? anchor : 0;
  }, []);

  // ---------------------------------------------------------------------
  // Load video (prefer 720p proxy; stream original while proxy builds)
  // ---------------------------------------------------------------------
  const stopPreviewPoll = useCallback(() => {
    if (previewPollRef.current) {
      clearInterval(previewPollRef.current);
      previewPollRef.current = null;
    }
  }, []);

  const cancelPreviewJob = useCallback(async (path: string) => {
    if (!path) return;
    try {
      await api("/api/eager/preview/cancel", {
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

  // Warm the next clip's 720p encode while the current one plays — critical for
  // snappy N / auto-advance. Safe to call repeatedly (idempotent start=1).
  const prefetchNextPreview = useCallback(
    (fromIndex: number) => {
      const s = stateRef.current;
      const next = nextPreviewTargetIndex(fromIndex);
      if (next < 0) return;
      const path = s.videos[next]?.path;
      if (!path || path === s.videos[fromIndex]?.path) return;
      prefetchedPreviewRef.current = path;
      api(`/api/eager/preview/status?path=${encodeURIComponent(path)}&start=1`).catch(() => {
        /* best-effort warmup */
      });
    },
    [nextPreviewTargetIndex],
  );

  const destroyHls = useCallback(() => {
    hlsUrlRef.current = "";
    if (hlsRef.current) {
      try {
        hlsRef.current.destroy();
      } catch {
        /* ignore */
      }
      hlsRef.current = null;
    }
  }, []);

  /**
   * Point the player at a preview playlist (via hls.js, or natively on Safari).
   * Works both for finished previews and ones still being encoded — the
   * playlist keeps growing and hls.js re-polls it until the encode ends.
   * Returns false when the browser can't play HLS at all.
   */
  const attachHlsMedia = useCallback(
    (v: HTMLVideoElement, sourcePath: string, hlsUrl: string, startAt: number, token: number) => {
      destroyHls();
      const absolute = host + hlsUrl;
      if (Hls.isSupported()) {
        const hls = new Hls({
          // Explicit start position — without it hls.js treats the growing
          // playlist as live TV and would start at the encode frontier.
          startPosition: Math.max(0, startAt),
          maxBufferLength: 30,
          backBufferLength: 30,
        });
        hlsRef.current = hls;
        hlsUrlRef.current = absolute;
        hls.on(Hls.Events.ERROR, (_evt, data) => {
          if (!data?.fatal || token !== previewTokenRef.current) return;
          // Fatal stream error → drop back to the original file.
          destroyHls();
          const el = videoRef.current;
          if (!el) return;
          const t = Number.isFinite(el.currentTime) ? el.currentTime : startAt;
          el.src = host + `/api/eager/stream?path=${encodeURIComponent(sourcePath)}`;
          el.load();
          el.addEventListener(
            "loadedmetadata",
            () => {
              if (token !== previewTokenRef.current) return;
              try {
                el.currentTime = t;
              } catch {
                /* ignore */
              }
            },
            { once: true },
          );
          setPreviewNote("Original file");
        });
        v.removeAttribute("src");
        hls.loadSource(absolute);
        hls.attachMedia(v);
        return true;
      }
      if (v.canPlayType("application/vnd.apple.mpegurl")) {
        hlsUrlRef.current = absolute;
        v.src = absolute;
        v.load();
        return true;
      }
      return false;
    },
    [destroyHls],
  );

  /** Swap mid-session from the original stream to the (possibly still building) preview. */
  const swapToPreview = useCallback(
    (path: string, token: number, hlsUrl: string, building: boolean) => {
      const v = videoRef.current;
      if (!v || token !== previewTokenRef.current || !hlsUrl) return;
      if (hlsUrlRef.current === host + hlsUrl) return;
      const t = Number.isFinite(v.currentTime) ? v.currentTime : stateRef.current.scrubTime || 0;
      const resume = wantPlayingRef.current || !v.paused;
      const rate = playbackRateRef.current || 1;

      const onReady = () => {
        if (token !== previewTokenRef.current) return;
        try {
          v.playbackRate = rate;
        } catch {
          /* ignore */
        }
        setScrubTime(t);
        setPreviewNote(building ? "720p preview · still encoding" : "720p preview");
        setStatus(
          building
            ? `720p preview playing while it builds — smooth ${rate.toFixed(1)}× from here`
            : `Switched to 720p preview — smooth ${rate.toFixed(1)}× scrubbing`,
          "ok",
        );
        if (resume) safePlay();
        // Current is playable — keep the next clip encoding in the background.
        prefetchNextPreview(stateRef.current.index);
      };
      v.addEventListener("loadedmetadata", onReady, { once: true });
      if (!attachHlsMedia(v, path, hlsUrl, t, token)) {
        v.removeEventListener("loadedmetadata", onReady);
        setPreviewNote("Original file");
      }
    },
    [attachHlsMedia, prefetchNextPreview, safePlay, setStatus],
  );

  const pollPreviewReady = useCallback(
    (path: string, token: number) => {
      stopPreviewPoll();
      previewPollRef.current = setInterval(async () => {
        if (token !== previewTokenRef.current) {
          stopPreviewPoll();
          return;
        }
        try {
          // start=1 is idempotent: keeps a running job, restarts a lost one
          // (e.g. after a backend restart or a cancelled build).
          const st = await api(
            `/api/eager/preview/status?path=${encodeURIComponent(path)}&start=1`,
          );
          if (token !== previewTokenRef.current) return;
          if (st.status === "running") {
            const pct = Number(st.progress) || 0;
            const onPreview = Boolean(hlsUrlRef.current);
            // Attach as soon as the first segments exist — no more waiting
            // for the whole encode before smooth 5–8× playback.
            if (!onPreview && st.playable && st.hls) {
              swapToPreview(path, token, String(st.hls), true);
            }
            // While current encodes/plays, keep the next 720p job alive.
            prefetchNextPreview(stateRef.current.index);
            setPreviewNote(
              hlsUrlRef.current ? `720p preview · encoding ${pct}%` : `Building preview ${pct}%`,
            );
            return;
          }
          if (st.status === "ready") {
            stopPreviewPoll();
            if (hlsUrlRef.current && st.hls && hlsUrlRef.current === host + st.hls) {
              // Already playing this preview — the playlist just gained its
              // end marker; hls.js finishes it on its own.
              setPreviewNote("720p preview");
              prefetchNextPreview(stateRef.current.index);
            } else if (st.hls) {
              swapToPreview(path, token, String(st.hls), false);
            }
            return;
          }
          if (st.status === "error" || st.status === "skipped") {
            stopPreviewPoll();
            setPreviewNote("Original file");
          }
        } catch {
          /* ignore transient poll errors */
        }
      }, 400);
    },
    [prefetchNextPreview, stopPreviewPoll, swapToPreview],
  );

  const loadVideo = useCallback(
    async (i: number, opts: { force?: boolean } = {}) => {
      const s = stateRef.current;
      if (i < 0 || i >= s.videos.length) return;
      const video: VideoItem = s.videos[i];

      // Click/nav without force: current video, or fully labelled (100% covered) only.
      if (!opts.force && i !== s.index && !isVideoFullyDone(video.path)) {
        setStatus("Only finished (100% covered) videos can be opened from the list", "error");
        return;
      }

      const previous = s.index >= 0 ? s.videos[s.index] : null;
      const upcomingIdx = nextPreviewTargetIndex(i);
      const upcomingPath = upcomingIdx >= 0 ? s.videos[upcomingIdx]?.path || "" : "";

      // Free CPU from the video we left — never cancel the clip we're opening
      // (it may already be mid-encode from prefetch) or the one we warm next.
      if (previous?.path && previous.path !== video.path && previous.path !== upcomingPath) {
        void cancelPreviewJob(previous.path);
      }
      if (
        prefetchedPreviewRef.current &&
        prefetchedPreviewRef.current !== video.path &&
        prefetchedPreviewRef.current !== upcomingPath
      ) {
        void cancelPreviewJob(prefetchedPreviewRef.current);
      }

      setLoadingVideo(true);
      setIndex(i);
      setScrubTime(0);
      setShareClipIn(null);
      setShareClipOut(null);
      setPreviewNote("Starting 720p preview…");
      stopPreviewPoll();
      if (seekTimerRef.current) {
        clearTimeout(seekTimerRef.current);
        seekTimerRef.current = null;
      }
      pendingSeekRef.current = null;

      const token = ++previewTokenRef.current;
      setStatus(`Loading ${video.name}...`);

      // CRITICAL: kick current + next 720p encodes immediately (don't wait on UI).
      void api(`/api/eager/preview/status?path=${encodeURIComponent(video.path)}&start=1`).catch(() => null);
      prefetchNextPreview(i);

      const v = videoRef.current;
      if (!v) {
        setLoadingVideo(false);
        return;
      }

      wantPlayingRef.current = false;
      playGenerationRef.current += 1;
      playPromiseRef.current = null;

      const streamUrl = host + `/api/eager/stream?path=${encodeURIComponent(video.path)}`;

      // Annotation + preview status in parallel so switching feels instant.
      const [, previewSt] = await Promise.all([
        loadAnnotationForPath(video.path, { keepPending: true }),
        api(`/api/eager/preview/status?path=${encodeURIComponent(video.path)}&start=1`).catch(() => null),
      ]);
      if (token !== previewTokenRef.current) return;

      // Keep the lookahead encode warm while this clip is reviewed.
      prefetchNextPreview(i);

      const resumeTime = resumeTimeForPath(video.path);
      setScrubTime(resumeTime);
      setTaskSelectionMode(Boolean(stateRef.current.annotationsByPath[video.path]?.pendingWork));

      let usingPreview = false;
      let initialHls = "";
      try {
        const st = previewSt;
        if (st?.status === "ready" && st.hls) {
          initialHls = String(st.hls);
          usingPreview = true;
          setPreviewNote("720p preview");
        } else if (st?.status === "running") {
          if (st.playable && st.hls) {
            initialHls = String(st.hls);
            usingPreview = true;
            setPreviewNote(`720p preview · encoding ${Number(st.progress) || 0}%`);
          } else {
            setPreviewNote(`Building preview ${Number(st.progress) || 0}%`);
          }
          pollPreviewReady(video.path, token);
        } else if (st?.status === "skipped") {
          setPreviewNote("Original file");
        } else {
          setPreviewNote("Building preview…");
          pollPreviewReady(video.path, token);
        }
      } catch {
        setPreviewNote("Original file");
      }

      const onReady = () => {
        if (token !== previewTokenRef.current) return;
        setLoadingVideo(false);
        v.pause();
        const startAt = resumeTimeForPath(video.path);
        setScrubTime(startAt);
        try {
          v.currentTime = startAt;
        } catch {
          /* ignore */
        }
        setPlaybackRate(1, false);
        updateScrubUiFromEl();
        // Current is on screen — ensure next encode is already running.
        prefetchNextPreview(i);
        const mode = usingPreview ? "720p preview" : "original (proxy building)";
        setStatus(
          startAt > 0
            ? `Ready — ${video.name} at ${formatTime(startAt)} · ${mode}`
            : `Ready — ${video.name} · ${mode}`,
          "ok",
        );
      };
      const onError = () => {
        if (token !== previewTokenRef.current) return;
        setLoadingVideo(false);
        setStatus("Could not load video", "error");
      };
      v.addEventListener("loadedmetadata", onReady, { once: true });
      v.addEventListener("error", onError, { once: true });
      if (!initialHls || !attachHlsMedia(v, video.path, initialHls, resumeTime, token)) {
        usingPreview = false;
        destroyHls();
        v.src = streamUrl;
        v.load();
      }
    },
    [
      attachHlsMedia,
      cancelPreviewJob,
      destroyHls,
      isVideoFullyDone,
      loadAnnotationForPath,
      nextPreviewTargetIndex,
      pollPreviewReady,
      prefetchNextPreview,
      resumeTimeForPath,
      setPlaybackRate,
      setStatus,
      stopPreviewPoll,
      updateScrubUiFromEl,
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

  // ---------------------------------------------------------------------
  // Mark work / garbage / undo / assign
  // ---------------------------------------------------------------------
  const markWork = useCallback(async () => {
    const video = currentVideo();
    if (!video) return;
    let ann = currentAnnotation();
    if (!ann) {
      await loadAnnotationForPath(video.path);
      ann = stateRef.current.annotationsByPath[video.path];
    }
    const anchor = anchorByPathRef.current[video.path] ?? computeAnchor(ann?.segments || []);
    const dur = knownDurationSec() || video.duration || annotationFor(video.path)?.duration || 0;
    let end = Math.max(0, currentScrubTime());
    if (dur > 0 && end >= dur - 0.05) end = dur;
    if (end <= anchor + 0.05) {
      setStatus(`Playhead must be after ${formatTime(anchor)} to mark work`, "error");
      return;
    }
    setAnnotationsByPath((prev) => ({
      ...prev,
      [video.path]: {
        ...(prev[video.path] || { segments: [], duration: null, complete: false }),
        pendingWork: { start: anchor, end },
      },
    }));
    safePause();
    setTaskSelectionMode(true);
    const last = stateRef.current.lastLabelTask;
    setStatus(
      last
        ? `Pending work ${formatTime(anchor)} → ${formatTime(end)} — Enter for ${last}, or pick from recent`
        : `Pending work ${formatTime(anchor)} → ${formatTime(end)} — choose a task and press Enter`,
      "ok",
    );
    focusTaskSearch();
  }, [annotationFor, currentAnnotation, currentScrubTime, currentVideo, focusTaskSearch, knownDurationSec, loadAnnotationForPath, setStatus]);

  const assignPendingWork = useCallback(async (taskOverride?: string) => {
    const video = currentVideo();
    if (!video) return;
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

    await api("/api/eager/annotations/append", {
      method: "POST",
      body: JSON.stringify({
        path: video.path,
        kind: "work",
        end: pending.end,
        task,
        ...annotationContext(),
      }),
    });
    setLastLabelTask(task);
    touchRecentTask(task);
    await loadAnnotationForPath(video.path);
    setTaskSelectionMode(false);
    leaveTaskSearch({ clear: true });
    setSelectedTaskValue("");
    setStatus(`Assigned work to ${task}`, "ok");
    if (stateRef.current.annotationsByPath[video.path]?.complete) await finishCleaningFile();
  }, [
    annotationContext,
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

  const markGarbage = useCallback(async () => {
    const video = currentVideo();
    if (!video) return;
    let ann = currentAnnotation();
    if (!ann) {
      await loadAnnotationForPath(video.path);
      ann = stateRef.current.annotationsByPath[video.path];
    }
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
    await api("/api/eager/annotations/append", {
      method: "POST",
      body: JSON.stringify({ path: video.path, kind: "garbage", end, ...annotationContext() }),
    });
    await loadAnnotationForPath(video.path);
    setStatus(`Marked garbage ${formatTime(anchor)} → ${formatTime(end)}`, "ok");
    if (stateRef.current.annotationsByPath[video.path]?.complete) await finishCleaningFile();
  }, [annotationContext, annotationFor, currentAnnotation, currentScrubTime, currentVideo, finishCleaningFile, knownDurationSec, loadAnnotationForPath, setStatus]);

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
  }, [currentAnnotation, currentVideo, deleteSegmentAt, leaveTaskSearch, setStatus]);

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
        destroyHls();
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
  }, [currentVideo, destroyHls, loadVideo, setStatus]);

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
      const data = await api("/api/eager/scan", {
        method: "POST",
        body: JSON.stringify({ path, recursive: true, mode: "annotate" }),
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

      // Start 720p for the first two files while annotations load — biggest win.
      for (const v of freshVideos.slice(0, 2)) {
        if (v?.path) {
          void api(`/api/eager/preview/status?path=${encodeURIComponent(v.path)}&start=1`).catch(() => null);
        }
      }

      await Promise.all(freshVideos.map((v) => loadAnnotationForPath(v.path)));
      setIdentityFromSelectedCard();

      const first = freshVideos.findIndex((v) => !stateRef.current.annotationsByPath[v.path]?.complete);
      if (first >= 0 || freshVideos.length) {
        const openAt = first >= 0 ? first : 0;
        // Warm openAt + openAt+1 explicitly in case the first incomplete isn't index 0.
        for (const v of [freshVideos[openAt], freshVideos[openAt + 1]]) {
          if (v?.path) {
            void api(`/api/eager/preview/status?path=${encodeURIComponent(v.path)}&start=1`).catch(() => null);
          }
        }
        await loadVideo(openAt, { force: true });
        setStatus(
          first >= 0
            ? `Found ${freshVideos.length} files — T/G annotate, Enter assign, N next unfinished`
            : `All ${freshVideos.length} files complete`,
          "ok",
        );
      } else {
        setStatus("No footage found", "error");
      }
    } catch (error: any) {
      setStatus(error.message, "error");
    } finally {
      setScanning(false);
    }
  }, [loadAnnotationForPath, loadVideo, scanTargetPath, selectedSdCardLabel, setIdentityFromSelectedCard, setStatus]);

  const applySelectedPath = useCallback(
    (path: string) => {
      setScanRoot(path);
      setLabelRoot(path);
      setSdCardValue((prev) => {
        const exists = stateRef.current.sdCards.some((c: SdCard) => (c.scan_path || c.path) === path);
        return exists ? path : prev;
      });
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
    setStatus("Choose footage on this computer or an external drive…");
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
      if (previewPollRef.current) clearInterval(previewPollRef.current);
      destroyHls();
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
  }, []);

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
      try {
        const data = await api("/api/eager/tasks", {
          method: "POST",
          body: JSON.stringify({ name: trimmed, label_root: stateRef.current.labelRoot || stateRef.current.scanRoot || scanTargetPath() }),
        });
        setTasks(data.tasks || []);
        setDefaultTasks(data.default_tasks || defaultTasks);
        setTaskSearch("");
        setStatus(`Task added: ${trimmed}`, "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [defaultTasks, scanTargetPath, setStatus],
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
        if (stateRef.current.selectedTaskValue.toLowerCase() === trimmed.toLowerCase()) {
          setSelectedTaskValue("");
        }
        setStatus(`Removed task: ${trimmed}`, "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [defaultTasks, isUserDefinedTask, setStatus],
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

  // Init on mount.
  useEffect(() => {
    setRecentTasks(loadRecentTasks());
    let cancelled = false;
    (async () => {
      try {
        await loadTasks();
        const ws = createWorkspace("Footage");
        setWorkspaces([ws]);
        setActiveWorkspaceId(ws.id);
        const [health, perfData, batches] = await Promise.all([
          api("/api/health"),
          api("/api/eager/config"),
          api("/api/eager/batches"),
          refreshSdCards({ quiet: true, autoScan: true }),
        ]);
        if (cancelled) return;
        setAppVersion(health.version || "");
        setPerf((prev) => ({ ...prev, ...perfData }));
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
      if (!v.paused && Number.isFinite(v.currentTime) && v.currentTime > 0) {
        // Throttle React updates — high rates otherwise churn the whole review UI.
        const now = performance.now();
        if (now - lastUiTick < 80) return;
        lastUiTick = now;
        setScrubTime(v.currentTime);
      }
    };
    const onLoadedMeta = () => updateScrubUiFromEl();
    const onPlay = () => {
      wantPlayingRef.current = true;
      setIsPlaying(true);
      try {
        v.playbackRate = playbackRateRef.current || 1;
      } catch {
        /* ignore */
      }
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
      // Progressive MP4 + 5–8× often underflows. Soft nudge if still meant to play.
      if (!wantPlayingRef.current) return;
      if ((playbackRateRef.current || 1) < 2.5) return;
      window.setTimeout(() => {
        const el = videoRef.current;
        if (!el || !wantPlayingRef.current) return;
        if (!el.paused && el.readyState >= 2) return;
        try {
          const t = el.currentTime;
          el.currentTime = Math.max(0, t);
        } catch {
          /* ignore */
        }
        if (el.paused) safePlay();
      }, 350);
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
  }, [safePlay, updateScrubUiFromEl]);

  return {
    videoRef,
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
