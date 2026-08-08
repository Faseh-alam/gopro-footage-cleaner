import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api, formatClock, host } from "@/lib/api";
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

  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [index, setIndex] = useState(-1);
  const [scanRoot, setScanRoot] = useState("");
  const [labelRoot, setLabelRoot] = useState("");
  const [tasks, setTasks] = useState<string[]>([]);
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
  const [appVersion, setAppVersion] = useState("");
  const [perf, setPerf] = useState<{ trim_poll_ms: number }>({ trim_poll_ms: 1200 });

  const previewTokenRef = useRef(0);
  const previewPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
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
    setDuration(v.duration || currentVideo()?.duration || 0);
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
      const v = videoRef.current;
      const dur = v?.duration || currentVideo()?.duration || 0;
      if (!dur) return;
      const clamped = Math.min(dur - 0.04, Math.max(0, time));
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
    [currentVideo, flushSeek],
  );

  const seekToFraction = useCallback(
    (fraction: number) => {
      const v = videoRef.current;
      const dur = v?.duration || currentVideo()?.duration || 0;
      if (!dur) return;
      scheduleSeek(fraction * dur, true);
    },
    [currentVideo, scheduleSeek],
  );

  const fineTune = useCallback(
    (seconds: number) => {
      if (!currentVideo()) return;
      scheduleSeek(stateRef.current.scrubTime + seconds, true);
    },
    [currentVideo, scheduleSeek],
  );

  const setPlaybackRate = useCallback((rate: number, announce = true) => {
    const v = videoRef.current;
    const onOriginal = Boolean(v?.src && v.src.includes("/api/eager/stream?"));
    let clamped = Math.min(
      PLAYBACK_RATE_MAX,
      Math.max(PLAYBACK_RATE_MIN, Math.round(rate / PLAYBACK_RATE_STEP) * PLAYBACK_RATE_STEP),
    );
    // Multi‑GB originals can't sustain 5–8×; soft-cap until 720p proxy is ready.
    if (onOriginal && clamped > 2) {
      clamped = 2;
      if (announce) {
        setStatus("Speed capped at 2× on original — wait for 720p preview for 5–8×", "ok");
        announce = false;
      }
    }
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

  // ---------------------------------------------------------------------
  // File list rendering helpers
  // ---------------------------------------------------------------------
  const isHandledPath = useCallback((path: string) => Boolean(annotationsByPath[path]?.complete), [annotationsByPath]);

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

  const swapToPreview = useCallback(
    (path: string, token: number) => {
      const v = videoRef.current;
      if (!v || token !== previewTokenRef.current) return;
      const t = Number.isFinite(v.currentTime) ? v.currentTime : stateRef.current.scrubTime || 0;
      const resume = wantPlayingRef.current || !v.paused;
      const rate = playbackRateRef.current || 1;
      const previewUrl = host + `/api/eager/preview?path=${encodeURIComponent(path)}`;
      if (v.src.includes("/api/eager/preview?")) return;

      const onReady = () => {
        if (token !== previewTokenRef.current) return;
        try {
          v.currentTime = t;
          v.playbackRate = rate;
        } catch {
          /* ignore */
        }
        setScrubTime(t);
        setPreviewNote("720p preview");
        setStatus(`Switched to 720p preview — smooth ${rate.toFixed(1)}× scrubbing`, "ok");
        if (resume) safePlay();
      };
      v.addEventListener("loadedmetadata", onReady, { once: true });
      v.src = previewUrl;
      v.load();
    },
    [safePlay, setStatus],
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
            setPreviewNote(pct >= 99 ? "Finalizing preview…" : `Building preview ${pct}%`);
            return;
          }
          if (st.status === "ready") {
            stopPreviewPoll();
            swapToPreview(path, token);
            return;
          }
          if (st.status === "error" || st.status === "skipped") {
            stopPreviewPoll();
            setPreviewNote("Original file");
          }
        } catch {
          /* ignore transient poll errors */
        }
      }, 1500);
    },
    [stopPreviewPoll, swapToPreview],
  );

  const loadVideo = useCallback(
    async (i: number) => {
      const s = stateRef.current;
      if (i < 0 || i >= s.videos.length) return;
      const video: VideoItem = s.videos[i];
      const previous = s.index >= 0 ? s.videos[s.index] : null;
      if (previous?.path && previous.path !== video.path) {
        await cancelPreviewJob(previous.path);
      }

      setIndex(i);
      setScrubTime(0);
      setPreviewNote("");
      stopPreviewPoll();
      if (seekTimerRef.current) {
        clearTimeout(seekTimerRef.current);
        seekTimerRef.current = null;
      }
      pendingSeekRef.current = null;

      const token = ++previewTokenRef.current;
      await loadAnnotationForPath(video.path, { keepPending: true });
      const resumeTime = resumeTimeForPath(video.path);
      setScrubTime(resumeTime);
      setTaskSelectionMode(Boolean(stateRef.current.annotationsByPath[video.path]?.pendingWork));

      setLoadingVideo(true);
      setStatus(`Loading ${video.name}...`);

      const v = videoRef.current;
      if (!v) return;

      wantPlayingRef.current = false;
      playGenerationRef.current += 1;
      playPromiseRef.current = null;

      let playUrl = host + `/api/eager/stream?path=${encodeURIComponent(video.path)}`;
      let usingPreview = false;
      try {
        const st = await api(
          `/api/eager/preview/status?path=${encodeURIComponent(video.path)}&start=1`,
        );
        if (token !== previewTokenRef.current) return;
        if (st.status === "ready") {
          playUrl = host + `/api/eager/preview?path=${encodeURIComponent(video.path)}`;
          usingPreview = true;
          setPreviewNote("720p preview");
        } else if (st.status === "running") {
          setPreviewNote(`Building preview ${Number(st.progress) || 0}%`);
          pollPreviewReady(video.path, token);
        } else if (st.status === "skipped") {
          setPreviewNote("Original file");
        } else {
          // idle → start was requested via start=1; poll until ready
          setPreviewNote("Building preview…");
          pollPreviewReady(video.path, token);
        }
      } catch {
        setPreviewNote("Original file");
      }

      v.src = playUrl;
      v.load();
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
    },
    [
      cancelPreviewJob,
      loadAnnotationForPath,
      pollPreviewReady,
      resumeTimeForPath,
      setPlaybackRate,
      setStatus,
      stopPreviewPoll,
      updateScrubUiFromEl,
    ],
  );

  const finishCleaningFile = useCallback(async () => {
    const s = stateRef.current;
    const next = nextIncompleteIndex(s.index + 1);
    if (next >= 0 && next !== s.index) {
      await loadVideo(next);
      setStatus("Moved to next unfinished video", "ok");
      return;
    }
    setStatus("All videos complete", "ok");
  }, [loadVideo, nextIncompleteIndex, setStatus]);

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
    const dur = videoRef.current?.duration || video.duration || annotationFor(video.path)?.duration || 0;
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
  }, [annotationFor, currentAnnotation, currentScrubTime, currentVideo, focusTaskSearch, loadAnnotationForPath, setStatus]);

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
    const dur = videoRef.current?.duration || video.duration || annotationFor(video.path)?.duration || 0;
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
  }, [annotationContext, annotationFor, currentAnnotation, currentScrubTime, currentVideo, finishCleaningFile, loadAnnotationForPath, setStatus]);

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
    try {
      await api("/api/eager/annotations/undo", { method: "POST", body: JSON.stringify({ path: video.path }) });
    } catch (error: any) {
      setStatus(error.message, "error");
      return;
    }
    await loadAnnotationForPath(video.path);
    setTaskSelectionMode(false);
    setStatus("Deleted last markup — choose a new timestamp", "ok");
  }, [currentAnnotation, currentVideo, leaveTaskSearch, loadAnnotationForPath, setStatus]);

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
        await loadVideo(Math.min(deletedIndex, nextVideos.length - 1));
      } else {
        setIndex(-1);
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
      await Promise.all(freshVideos.map((v) => loadAnnotationForPath(v.path)));
      setIdentityFromSelectedCard();

      const first = freshVideos.findIndex((v) => !stateRef.current.annotationsByPath[v.path]?.complete);
      if (first >= 0 || freshVideos.length) {
        await loadVideo(first >= 0 ? first : 0);
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

  useEffect(() => {
    return () => {
      if (globalTrimPollRef.current) clearInterval(globalTrimPollRef.current);
      if (trimPollRef.current) clearInterval(trimPollRef.current);
      if (seekTimerRef.current) clearTimeout(seekTimerRef.current);
    };
  }, []);

  // ---------------------------------------------------------------------
  // Tasks / batch / init
  // ---------------------------------------------------------------------
  const loadTasks = useCallback(async () => {
    const data = await api("/api/eager/tasks");
    setTasks(data.tasks || []);
  }, []);

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
        setTaskSearch("");
        setStatus(`Task added: ${trimmed}`, "ok");
      } catch (error: any) {
        setStatus(error.message, "error");
      }
    },
    [scanTargetPath, setStatus],
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

  const queueClips = useCallback(async () => {
    const current = currentVideo();
    if (!current) return;
    let ann = currentAnnotation();
    if (!ann) {
      await loadAnnotationForPath(current.path);
      ann = stateRef.current.annotationsByPath[current.path];
    }
    const clips = (ann?.segments || [])
      .filter((s) => s.kind === "work")
      .map((s) => `${formatTime(s.start)} - ${formatTime(s.end)}`)
      .join("\n");
    try {
      const data = await api("/api/batch", {
        method: "POST",
        body: JSON.stringify({ path: current.path, clips: clips.trim(), delete_original: false }),
      });
      setStatus(`Queued ${data.clip_count} clips for ${data.input_name}`, "ok");
    } catch (error: any) {
      setStatus(error.message, "error");
    }
  }, [currentAnnotation, currentVideo, loadAnnotationForPath, setStatus]);

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
    appVersion,
    currentVideo,
    currentAnnotation,
    annotationFor,
    orderedTaskGroups,
    selectedTask,
    focusTaskSearch,
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
    importBatchCsv,
    refreshBatch,
    finishCard,
    completeBatch,
    queueClips,
    formatTime,
    formatDurationShort,
    basenamePath,
    shortCardTitle,
    isHandledPath,
  };
}

export type ReviewController = ReturnType<typeof useReviewController>;
