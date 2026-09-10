import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUpRight,
  Circle,
  Mic,
  Pause,
  Play,
  SkipForward,
  Square,
} from "lucide-react";
import { toast } from "sonner";
import { api, apiUrl } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Logo } from "@/components/wc/logo";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/voiceover")({
  head: () => ({
    meta: [
      { title: "Voiceover Station — World Context" },
      {
        name: "description",
        content:
          "Record egocentric narration with pause-and-describe freeze-frame export for Lightly P0 samples.",
      },
    ],
  }),
  component: VoiceoverPage,
});

type Clip = {
  path: string;
  name: string;
  class_name: string;
  duration: number | null;
  size_bytes: number;
  has_gpmf: boolean;
  done: boolean;
  pending?: boolean;
  narrated_path?: string | null;
  width?: number | null;
  height?: number | null;
  video_codec?: string | null;
  rotation?: number | null;
};

type ClassRow = {
  name: string;
  clip_count: number;
  done_count: number;
  clips: Clip[];
};

type SessionEvent = {
  type: "play" | "pause" | "resume" | "stop" | "research_pause" | "research_resume";
  session_t: number;
  video_t: number;
};

type QaGate = { id: string; label: string };

type ProbeInfo = {
  duration: number | null;
  width?: number | null;
  height?: number | null;
  video_codec?: string | null;
  rotation?: number | null;
  audio_stream_count?: number;
  warnings?: string[];
  blocks?: string[];
  can_start?: boolean;
};

type ValidationResult = {
  ok: boolean;
  pass: boolean;
  checks: { name: string; ok: boolean; detail: string }[];
  path?: string;
};

const MIC_KEY = "wc-voiceover-mic-id";
const NARRATOR_KEY = "wc-voiceover-narrator";
const GEMINI_KEY = "wc-voiceover-gemini-key";
const ENV_WARN_S = 150;
const ENV_ALERT_S = 170;
const ENV_FAIL_S = 180;
const MIN_MIC_LEVEL = 0.02;

const DEFAULT_QA: QaGate[] = [
  { id: "egocentric", label: "Egocentric POV confirmed" },
  { id: "wearer_task", label: "Wearer is performing the task" },
  { id: "pii_clear", label: "PII reviewed — no visible/audible PII remains" },
  { id: "action_visible", label: "Main action is clearly visible" },
  { id: "not_repetitive", label: "Source clip is not dominated by repeated identical actions" },
  { id: "human_only", label: "Human narration only (no TTS / synthetic voice)" },
  { id: "task_narration", label: "Narration includes task actions" },
  { id: "env_narration", label: "Narration includes environment description" },
  { id: "audio_clear", label: "Audio is clear" },
  { id: "no_reuse", label: "No narration/audio reused from another clip" },
];

function formatClock(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
  const whole = Math.floor(seconds);
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function VoiceoverPage() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const meterRafRef = useRef<number>(0);
  const recordingRef = useRef(false);
  const sessionStartRef = useRef(0);
  const eventsRef = useRef<SessionEvent[]>([]);
  const videoWasPlayingRef = useRef(false);
  const peakLevelRef = useRef(0);
  const researchBreakRef = useRef(false);
  const researchPausedAtRef = useRef<number | null>(null);
  const researchAccumulatedMsRef = useRef(0);

  const [root, setRoot] = useState("");
  const [classes, setClasses] = useState<ClassRow[]>([]);
  const [activeClass, setActiveClass] = useState("");
  const [index, setIndex] = useState(0);
  const [bust, setBust] = useState(0);
  const [status, setStatus] = useState(
    "Open a folder · record with freeze-frame pause export (source stays unchanged)",
  );
  const [recording, setRecording] = useState(false);
  const [saving, setSaving] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [scrub, setScrub] = useState(0);
  const [duration, setDuration] = useState(0);
  const [recElapsed, setRecElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [mics, setMics] = useState<MediaDeviceInfo[]>([]);
  const [micId, setMicId] = useState(() => localStorage.getItem(MIC_KEY) || "");
  const [narrator, setNarrator] = useState(() => localStorage.getItem(NARRATOR_KEY) || "");
  const [geminiKey, setGeminiKey] = useState(() => localStorage.getItem(GEMINI_KEY) || "");
  const [script, setScript] = useState("");
  const [scriptBusy, setScriptBusy] = useState(false);
  const [micListening, setMicListening] = useState(false);
  const [micTestBusy, setMicTestBusy] = useState(false);
  const [micOk, setMicOk] = useState(false);
  const [pendingBlob, setPendingBlob] = useState<Blob | null>(null);
  const [pendingMime, setPendingMime] = useState("audio/webm");
  const [sessionEvents, setSessionEvents] = useState<SessionEvent[]>([]);
  const [sessionEnd, setSessionEnd] = useState(0);
  const [envMarkers, setEnvMarkers] = useState<number[]>([]);
  const [qaGates, setQaGates] = useState<QaGate[]>(DEFAULT_QA);
  const [qa, setQa] = useState<Record<string, boolean>>({});
  const [probe, setProbe] = useState<ProbeInfo | null>(null);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [reviewPath, setReviewPath] = useState<string | null>(null);
  const [reviewMode, setReviewMode] = useState(false);
  const [researchBreak, setResearchBreak] = useState(false);

  recordingRef.current = recording;
  researchBreakRef.current = researchBreak;

  const effectiveSessionSeconds = useCallback(() => {
    if (!sessionStartRef.current) return 0;
    const wall = Date.now() - sessionStartRef.current;
    const pausedNow =
      researchPausedAtRef.current != null ? Date.now() - researchPausedAtRef.current : 0;
    return Math.max(0, (wall - researchAccumulatedMsRef.current - pausedNow) / 1000);
  }, []);

  const clips = useMemo(() => {
    const row = classes.find((c) => c.name === activeClass);
    return row?.clips || [];
  }, [classes, activeClass]);

  const current = clips[index] || null;

  const streamUrl = useMemo(() => {
    if (reviewMode && reviewPath) {
      return `${apiUrl(`/api/voiceover/stream?path=${encodeURIComponent(reviewPath)}`)}&v=${bust}`;
    }
    if (!current) return "";
    return `${apiUrl(`/api/voiceover/stream?path=${encodeURIComponent(current.path)}`)}&v=${bust}`;
  }, [bust, current, reviewMode, reviewPath]);

  const sinceEnv = useMemo(() => {
    if (!recording && !pendingBlob) return 0;
    const last = envMarkers.length ? Math.max(...envMarkers) : 0;
    const t = recording ? recElapsed : sessionEnd;
    return Math.max(0, t - last);
  }, [envMarkers, pendingBlob, recElapsed, recording, sessionEnd]);

  const projectedOut = useMemo(() => {
    if (recording) return recElapsed;
    if (pendingBlob) return sessionEnd;
    return duration || 0;
  }, [duration, pendingBlob, recElapsed, recording, sessionEnd]);

  const allQaChecked = useMemo(
    () => qaGates.length > 0 && qaGates.every((g) => Boolean(qa[g.id])),
    [qa, qaGates],
  );

  const refreshMics = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((d) => d.kind === "audioinput");
      setMics(inputs);
      if (!micId && inputs[0]?.deviceId) {
        setMicId(inputs[0].deviceId);
      }
    } catch (error: any) {
      toast.error(error?.message || "Could not access microphone");
    }
  }, [micId]);

  useEffect(() => {
    void refreshMics();
  }, [refreshMics]);

  useEffect(() => {
    localStorage.setItem(MIC_KEY, micId);
  }, [micId]);

  useEffect(() => {
    localStorage.setItem(NARRATOR_KEY, narrator);
  }, [narrator]);

  useEffect(() => {
    localStorage.setItem(GEMINI_KEY, geminiKey);
  }, [geminiKey]);

  useEffect(() => {
    void api<{ gates: QaGate[] }>("/api/voiceover/qa-gates")
      .then((data) => {
        if (data.gates?.length) setQaGates(data.gates);
      })
      .catch(() => undefined);
  }, []);

  const stopMeter = useCallback(() => {
    if (meterRafRef.current) cancelAnimationFrame(meterRafRef.current);
    meterRafRef.current = 0;
    setLevel(0);
  }, []);

  const startMeter = useCallback(
    (stream: MediaStream) => {
      stopMeter();
      peakLevelRef.current = 0;
      const ctx = audioCtxRef.current || new AudioContext();
      audioCtxRef.current = ctx;
      void ctx.resume();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;
      const data = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteFrequencyData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i];
        const avg = sum / data.length / 255;
        const scaled = Math.min(1, avg * 1.8);
        peakLevelRef.current = Math.max(peakLevelRef.current, scaled);
        setLevel(scaled);
        meterRafRef.current = requestAnimationFrame(tick);
      };
      tick();
    },
    [stopMeter],
  );

  const releaseMic = useCallback(() => {
    stopMeter();
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    mediaRecorderRef.current = null;
    chunksRef.current = [];
    setMicListening(false);
  }, [stopMeter]);

  useEffect(() => () => releaseMic(), [releaseMic]);

  const pushEvent = useCallback(
    (type: SessionEvent["type"], videoT: number) => {
      const session_t = Math.max(0, effectiveSessionSeconds());
      const ev: SessionEvent = { type, session_t, video_t: Math.max(0, videoT) };
      eventsRef.current = [...eventsRef.current, ev];
      setSessionEvents(eventsRef.current);
    },
    [effectiveSessionSeconds],
  );

  const scanRoot = useCallback(async (path: string, opts?: { keepClass?: string; keepPath?: string }) => {
    const data = await api<{
      root: string;
      classes: ClassRow[];
      clip_count: number;
      done_count: number;
    }>(`/api/voiceover/scan?root=${encodeURIComponent(path)}`);
    setRoot(data.root);
    setClasses(data.classes || []);
    const keepClass =
      opts?.keepClass && data.classes?.some((c) => c.name === opts.keepClass)
        ? opts.keepClass
        : data.classes?.[0]?.name || "";
    setActiveClass(keepClass);
    const row = data.classes?.find((c) => c.name === keepClass);
    let nextIndex = 0;
    if (opts?.keepPath && row) {
      const found = row.clips.findIndex((c) => c.path === opts.keepPath);
      if (found >= 0) nextIndex = found;
    }
    setIndex(nextIndex);
    setBust((b) => b + 1);
    setStatus(
      `Loaded ${data.clip_count} clip(s) · ${data.done_count} done · exports write .narrated.mp4 beside source`,
    );
  }, []);

  const openFolder = useCallback(async () => {
    try {
      const picked = await api<{ cancelled?: boolean; path?: string }>(
        "/api/voiceover/pick-folder",
        { method: "POST", body: JSON.stringify({}) },
      );
      if (picked.cancelled || !picked.path) {
        setStatus("Folder pick cancelled");
        return;
      }
      await scanRoot(picked.path);
    } catch (error: any) {
      toast.error(error.message || "Could not open folder");
    }
  }, [scanRoot]);

  useEffect(() => {
    if (!current?.path || recording) return;
    let cancelled = false;
    void api<ProbeInfo>(`/api/voiceover/probe?path=${encodeURIComponent(current.path)}`)
      .then((data) => {
        if (cancelled) return;
        setProbe(data);
        if (data.blocks?.length) toast.error(data.blocks[0]);
        else if (data.warnings?.length) toast.message(data.warnings[0]);
      })
      .catch(() => {
        if (!cancelled) setProbe(null);
      });
    return () => {
      cancelled = true;
    };
  }, [current?.path, recording]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (researchBreakRef.current) {
      toast.message("Research break — press R to resume mic + video");
      return;
    }
    if (recordingRef.current) {
      // SPACE = video only; mic keeps running. Log pause/resume for freeze export.
      if (v.paused) {
        void v.play().catch(() => undefined);
        pushEvent("resume", v.currentTime || 0);
      } else {
        v.pause();
        pushEvent("pause", v.currentTime || 0);
      }
      return;
    }
    if (v.paused) void v.play().catch(() => undefined);
    else v.pause();
  }, [pushEvent]);

  const seekStart = useCallback(() => {
    if (recordingRef.current) {
      toast.message("Seeking disabled while recording — Cancel Session to restart");
      return;
    }
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = 0;
    setScrub(0);
  }, []);

  const nudge = useCallback((delta: number) => {
    if (recordingRef.current) {
      toast.message("Seeking disabled while recording");
      return;
    }
    const v = videoRef.current;
    if (!v) return;
    const next = Math.max(0, Math.min(v.duration || 0, (v.currentTime || 0) + delta));
    v.currentTime = next;
    setScrub(next);
  }, []);

  const nextClip = useCallback(() => {
    if (recordingRef.current || pendingBlob) {
      toast.message("Finish or cancel the current session first");
      return;
    }
    if (!clips.length) return;
    setIndex((i) => (i + 1) % clips.length);
    setBust((b) => b + 1);
    setScript("");
    setReviewMode(false);
    setReviewPath(null);
    setValidation(null);
  }, [clips.length, pendingBlob]);

  const releasePlayer = useCallback(async () => {
    const v = videoRef.current;
    if (!v) return;
    try {
      v.pause();
    } catch {
      /* ignore */
    }
    v.removeAttribute("src");
    v.load();
    await new Promise((r) => window.setTimeout(r, 400));
  }, []);

  const cancelSession = useCallback(() => {
    const recorder = mediaRecorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      try {
        recorder.onstop = null;
        recorder.stop();
      } catch {
        /* ignore */
      }
    }
    releaseMic();
    setRecording(false);
    setResearchBreak(false);
    researchBreakRef.current = false;
    researchPausedAtRef.current = null;
    researchAccumulatedMsRef.current = 0;
    setPendingBlob(null);
    setSessionEvents([]);
    eventsRef.current = [];
    setSessionEnd(0);
    setEnvMarkers([]);
    setRecElapsed(0);
    setQa({});
    setValidation(null);
    const v = videoRef.current;
    if (v) {
      v.pause();
      v.currentTime = 0;
      v.muted = false;
    }
    setStatus("Session cancelled — source video untouched");
    toast.message("Session discarded");
  }, [releaseMic]);

  const enterResearchBreak = useCallback(() => {
    if (!recordingRef.current || researchBreakRef.current) return;
    const recorder = mediaRecorderRef.current;
    const v = videoRef.current;
    if (!recorder || recorder.state !== "recording") {
      toast.error("Start a session before using research break (B)");
      return;
    }
    if (typeof recorder.pause !== "function") {
      toast.error("This browser cannot pause the mic mid-take — use Chrome/Edge");
      return;
    }
    try {
      recorder.pause();
    } catch (error: any) {
      toast.error(error?.message || "Could not pause microphone");
      return;
    }
    if (v && !v.paused) v.pause();
    pushEvent("research_pause", v?.currentTime || 0);
    researchPausedAtRef.current = Date.now();
    researchBreakRef.current = true;
    setResearchBreak(true);
    setStatus("RESEARCH BREAK — mic + video paused. Look it up, then press R to resume both.");
    toast.message("Research break — press R when ready to continue");
  }, [pushEvent]);

  const resumeFromResearchBreak = useCallback(() => {
    if (!recordingRef.current || !researchBreakRef.current) return;
    const recorder = mediaRecorderRef.current;
    const v = videoRef.current;
    if (!recorder) return;
    if (researchPausedAtRef.current != null) {
      researchAccumulatedMsRef.current += Date.now() - researchPausedAtRef.current;
      researchPausedAtRef.current = null;
    }
    try {
      if (recorder.state === "paused" && typeof recorder.resume === "function") {
        recorder.resume();
      }
    } catch (error: any) {
      toast.error(error?.message || "Could not resume microphone");
      return;
    }
    pushEvent("research_resume", v?.currentTime || 0);
    researchBreakRef.current = false;
    setResearchBreak(false);
    if (v) {
      void v.play().catch(() => undefined);
    }
    setStatus("MIC RECORDING · Space = video pause only · B = research break · Esc cancels");
    toast.success("Resumed mic + video");
  }, [pushEvent]);

  const markEnvironment = useCallback(() => {
    const t = recording ? recElapsed : sessionEnd;
    if (!recording && !pendingBlob) {
      toast.message("Start a narration session first");
      return;
    }
    setEnvMarkers((prev) => [...prev, t]);
    toast.success("Environment description marked");
  }, [pendingBlob, recElapsed, recording, sessionEnd]);

  const armMicListen = useCallback(async () => {
    if (!micId && mics.length === 0) {
      toast.error("Select a microphone first");
      return;
    }
    try {
      releaseMic();
      const constraints: MediaStreamConstraints = {
        audio: micId ? { deviceId: { exact: micId } } : true,
      };
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      mediaStreamRef.current = stream;
      startMeter(stream);
      setMicListening(true);
      setMicOk(false);
      peakLevelRef.current = 0;
      setStatus("Mic live — speak to see the meter, then Test Mic or Start");
    } catch (error: any) {
      releaseMic();
      toast.error(error?.message || "Could not open microphone");
    }
  }, [micId, mics.length, releaseMic, startMeter]);

  const testMic = useCallback(async () => {
    if (recording || saving) return;
    setMicTestBusy(true);
    try {
      const constraints: MediaStreamConstraints = {
        audio: micId ? { deviceId: { exact: micId } } : true,
      };
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      startMeter(stream);
      setMicListening(true);
      mediaStreamRef.current = stream;
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "";
      const chunks: Blob[] = [];
      const recorder = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      recorder.ondataavailable = (ev) => {
        if (ev.data.size) chunks.push(ev.data);
      };
      recorder.start(100);
      await new Promise((r) => window.setTimeout(r, 5000));
      const blob: Blob = await new Promise((resolve) => {
        recorder.onstop = () => resolve(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
        recorder.stop();
      });
      const peak = peakLevelRef.current;
      stream.getTracks().forEach((t) => t.stop());
      mediaStreamRef.current = null;
      stopMeter();
      setMicListening(false);
      if (blob.size < 200 || peak < MIN_MIC_LEVEL) {
        setMicOk(false);
        toast.error("No usable mic signal — check device / permission and try again");
        setStatus("Mic test failed — no input detected");
        return;
      }
      setMicOk(true);
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      void audio.play().catch(() => undefined);
      audio.onended = () => URL.revokeObjectURL(url);
      toast.success("Mic test OK — playing back 5s sample");
      setStatus("Mic test passed — you can start the session");
    } catch (error: any) {
      setMicOk(false);
      toast.error(error?.message || "Mic test failed");
    } finally {
      setMicTestBusy(false);
    }
  }, [micId, recording, saving, startMeter, stopMeter]);

  const finishExport = useCallback(async () => {
    if (!current || !pendingBlob) {
      toast.error("Record a take first");
      return;
    }
    if (!allQaChecked) {
      toast.error("Tick every QA checkbox before export");
      return;
    }
    if (probe?.blocks?.length) {
      toast.error(probe.blocks[0]);
      return;
    }
    setSaving(true);
    setStatus(`Building freeze-frame export for ${current.name}…`);
    await releasePlayer();
    try {
      const form = new FormData();
      form.append("path", current.path);
      form.append("root", root);
      form.append("narrator", narrator);
      const micLabel = mics.find((m) => m.deviceId === micId)?.label || micId || "default";
      form.append("mic", micLabel);
      form.append("events", JSON.stringify(eventsRef.current));
      form.append("session_end", String(sessionEnd || recElapsed));
      form.append("qa", JSON.stringify(qa));
      const ext = pendingMime.includes("wav") ? "wav" : "webm";
      form.append("audio", pendingBlob, `take.${ext}`);
      const response = await fetch(apiUrl("/api/voiceover/export-narration"), {
        method: "POST",
        body: form,
      });
      const result = (await response.json().catch(() => ({}))) as {
        ok?: boolean;
        path?: string;
        message?: string;
        error?: string;
        validation?: ValidationResult;
        wav_path?: string;
        events_path?: string;
      };
      if (!response.ok || result.ok === false) {
        throw new Error(result.error || result.message || `Export failed (${response.status})`);
      }
      setValidation(result.validation || null);
      setPendingBlob(null);
      setReviewPath(result.path || null);
      setReviewMode(Boolean(result.path));
      setBust((b) => b + 1);
      setStatus(result.message || `Exported · ${result.path}`);
      if (result.validation?.pass) toast.success("Export PASS — review the narrated MP4");
      else toast.message("Exported — check validation results");
      await scanRoot(root, { keepClass: activeClass, keepPath: current.path });
    } catch (error: any) {
      toast.error(error.message || "Export failed — source left untouched");
      setStatus(error.message || "Export failed");
      setBust((b) => b + 1);
    } finally {
      setSaving(false);
    }
  }, [
    activeClass,
    allQaChecked,
    current,
    micId,
    mics,
    narrator,
    pendingBlob,
    pendingMime,
    probe,
    qa,
    recElapsed,
    releasePlayer,
    root,
    scanRoot,
    sessionEnd,
  ]);

  const stopRecording = useCallback(
    async (opts: { discard?: boolean } = {}) => {
      const recorder = mediaRecorderRef.current;
      if (!recorder || recorder.state === "inactive") {
        setRecording(false);
        releaseMic();
        return;
      }
      const v = videoRef.current;
      if (v && !opts.discard) {
        pushEvent("stop", v.currentTime || 0);
        v.pause();
      }
      setRecording(false);
      const blob: Blob = await new Promise((resolve) => {
        recorder.onstop = () => {
          resolve(new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" }));
        };
        recorder.stop();
      });
      const endT = Math.max(0, effectiveSessionSeconds());
      releaseMic();
      setRecElapsed(endT);
      setSessionEnd(endT);
      setResearchBreak(false);
      researchBreakRef.current = false;
      researchPausedAtRef.current = null;

      if (opts.discard) {
        setPendingBlob(null);
        eventsRef.current = [];
        setSessionEvents([]);
        setEnvMarkers([]);
        setStatus("Take discarded — original video untouched");
        return;
      }
      if (blob.size < 512) {
        toast.error("Recording was empty — check the mic and try again");
        setStatus("Empty take — nothing exported");
        setPendingBlob(null);
        return;
      }
      setPendingBlob(blob);
      setPendingMime(recorder.mimeType || "audio/webm");
      setStatus(
        `Take ready (${formatClock(endT)}) — tick QA boxes, then Finish & Export. Source not modified yet.`,
      );
      toast.message("Recording stopped — complete QA and export");
    },
    [effectiveSessionSeconds, pushEvent, releaseMic],
  );

  const attachPending = useCallback(async () => {
    if (!current) return;
    setSaving(true);
    setStatus(`Attaching pending take into ${current.name} (legacy in-place)…`);
    await releasePlayer();
    try {
      const result = await api<{ path?: string; message?: string }>(
        "/api/voiceover/attach-pending",
        {
          method: "POST",
          body: JSON.stringify({
            path: current.path,
            root,
            narrator,
            mic: mics.find((m) => m.deviceId === micId)?.label || micId || "",
          }),
        },
      );
      toast.success("Legacy attach done — prefer Finish & Export for freeze-frame sync");
      setStatus(result.message || `Attached · ${result.path || current.path}`);
      await scanRoot(root, { keepClass: activeClass, keepPath: current.path });
    } catch (error: any) {
      toast.error(error.message || "Attach failed");
      setStatus(error.message || "Attach failed");
      setBust((b) => b + 1);
    } finally {
      setSaving(false);
    }
  }, [activeClass, current, micId, mics, narrator, releasePlayer, root, scanRoot]);

  const startRecording = useCallback(async () => {
    if (!current) {
      toast.error("Select a clip first");
      return;
    }
    if (saving || pendingBlob) {
      toast.message("Export or cancel the current take first");
      return;
    }
    if (!micId) {
      toast.error("Select a microphone before starting");
      return;
    }
    if (probe?.can_start === false) {
      toast.error(probe.blocks?.[0] || "Source clip cannot be used");
      return;
    }
    try {
      const constraints: MediaStreamConstraints = {
        audio: { deviceId: { exact: micId } },
      };
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      mediaStreamRef.current = stream;
      startMeter(stream);
      // Brief settle to confirm signal (or rely on prior mic test).
      await new Promise((r) => window.setTimeout(r, 400));
      if (peakLevelRef.current < MIN_MIC_LEVEL && !micOk) {
        releaseMic();
        toast.error("No mic signal detected — run Test Mic or speak louder, then start");
        return;
      }
      chunksRef.current = [];
      eventsRef.current = [];
      setSessionEvents([]);
      setEnvMarkers([]);
      setQa({});
      setValidation(null);
      setReviewMode(false);
      setReviewPath(null);
      setResearchBreak(false);
      researchBreakRef.current = false;
      researchPausedAtRef.current = null;
      researchAccumulatedMsRef.current = 0;
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : MediaRecorder.isTypeSupported("audio/webm")
          ? "audio/webm"
          : "";
      const recorder = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      mediaRecorderRef.current = recorder;
      recorder.ondataavailable = (ev) => {
        if (ev.data.size) chunksRef.current.push(ev.data);
      };
      const v = videoRef.current;
      if (v) {
        v.muted = true;
        v.currentTime = 0;
        setScrub(0);
      }
      sessionStartRef.current = Date.now();
      recorder.start(250);
      setRecording(true);
      setRecElapsed(0);
      setPendingBlob(null);
      toast.message("Describe the environment near the beginning");
      setStatus(
        `MIC RECORDING · Space = pause video only · B = research break (mic+video) · E = environment · Esc cancels`,
      );
      if (v) {
        try {
          await v.play();
          pushEvent("play", 0);
          videoWasPlayingRef.current = true;
        } catch {
          pushEvent("play", 0);
        }
      } else {
        pushEvent("play", 0);
      }
    } catch (error: any) {
      releaseMic();
      toast.error(error?.message || "Could not start microphone");
    }
  }, [
    current,
    micId,
    micOk,
    pendingBlob,
    probe,
    pushEvent,
    releaseMic,
    saving,
    startMeter,
  ]);

  const toggleRecord = useCallback(() => {
    if (researchBreakRef.current) {
      resumeFromResearchBreak();
      return;
    }
    if (recording) void stopRecording();
    else void startRecording();
  }, [recording, resumeFromResearchBreak, startRecording, stopRecording]);

  useEffect(() => {
    if (!recording) return;
    const id = window.setInterval(() => {
      setRecElapsed(effectiveSessionSeconds());
    }, 200);
    return () => clearInterval(id);
  }, [effectiveSessionSeconds, recording]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (event.key === " " || event.code === "Space") {
        event.preventDefault();
        togglePlay();
        return;
      }
      if (event.key === "b" || event.key === "B") {
        event.preventDefault();
        if (recordingRef.current && !researchBreakRef.current) enterResearchBreak();
        return;
      }
      if (event.key === "e" || event.key === "E") {
        event.preventDefault();
        markEnvironment();
        return;
      }
      if (event.key === "r" || event.key === "R") {
        event.preventDefault();
        toggleRecord();
        return;
      }
      if (event.key === "s" || event.key === "S") {
        event.preventDefault();
        seekStart();
        return;
      }
      if (event.key === "n" || event.key === "N") {
        event.preventDefault();
        nextClip();
        return;
      }
      if (event.key === "Escape") {
        if (recordingRef.current || pendingBlob) {
          event.preventDefault();
          cancelSession();
        }
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        nudge(-2);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        nudge(2);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [
    cancelSession,
    enterResearchBreak,
    markEnvironment,
    nextClip,
    nudge,
    pendingBlob,
    seekStart,
    togglePlay,
    toggleRecord,
  ]);

  const draftScript = useCallback(
    async (opts: { wholeClip?: boolean } = {}) => {
      if (!current) return;
      setScriptBusy(true);
      const wholeClip = Boolean(opts.wholeClip);
      const start = wholeClip ? 0 : scrub || 0;
      try {
        const data = await api<{
          script: string;
          start_seconds?: number;
          end_seconds?: number;
        }>("/api/voiceover/gemini-script", {
          method: "POST",
          body: JSON.stringify({
            path: current.path,
            class_name: current.class_name,
            api_key: geminiKey,
            start_seconds: start,
            window_seconds: 60,
            whole_clip: wholeClip,
            with_video: true,
          }),
        });
        const chunk = (data.script || "").trim();
        const label = wholeClip
          ? `[whole clip · proxy only]`
          : `[${formatClock(data.start_seconds ?? start)}–${formatClock(data.end_seconds ?? start + 60)}]`;
        setScript((prev) => {
          const block = `${label}\n${chunk}`;
          return wholeClip ? block : prev.trim() ? `${prev.trim()}\n\n${block}` : block;
        });
        toast.success(wholeClip ? "Whole-clip draft ready" : "Draft ready");
      } catch (error: any) {
        toast.error(error.message || "Gemini failed");
      } finally {
        setScriptBusy(false);
      }
    },
    [current, geminiKey, scrub],
  );

  const envClass =
    sinceEnv >= ENV_FAIL_S
      ? "bg-red-600 text-white"
      : sinceEnv >= ENV_ALERT_S
        ? "bg-red-500/90 text-white"
        : sinceEnv >= ENV_WARN_S
          ? "bg-amber-400 text-black"
          : "bg-surface-2 text-muted-foreground";

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-6 py-4">
        <div className="flex items-center gap-4">
          <Logo />
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Voiceover station · Lightly P0
            </p>
            <h1 className="font-[Syne] text-lg font-semibold tracking-tight">
              Narrate · pause-and-freeze · human voice only
            </h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Link
            to="/review"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-[#b96d72] transition-opacity hover:opacity-75"
          >
            Review <ArrowUpRight className="size-3.5" />
          </Link>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-[#b96d72] transition-opacity hover:opacity-75"
          >
            Home <ArrowUpRight className="size-3.5" />
          </Link>
          <Button size="sm" variant="accent" onClick={() => void openFolder()}>
            Open voiceover folder
          </Button>
        </div>
      </header>

      <main className="grid gap-4 p-4 lg:grid-cols-[260px_minmax(0,1fr)_320px]">
        <aside className="space-y-3 rounded-sm border border-border bg-surface p-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
            Classes
          </p>
          {!classes.length && (
            <p className="text-sm text-muted-foreground">
              Expect <code className="text-xs">voiceover/ClassName/*.MP4</code> on the USB.
            </p>
          )}
          <div className="space-y-1">
            {classes.map((row) => (
              <button
                key={row.name}
                type="button"
                onClick={() => {
                  if (recording || pendingBlob) return;
                  setActiveClass(row.name);
                  setIndex(0);
                  setBust((b) => b + 1);
                  setScript("");
                  setReviewMode(false);
                }}
                className={cn(
                  "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-left text-sm",
                  activeClass === row.name
                    ? "bg-surface-2 text-foreground"
                    : "text-muted-foreground hover:bg-surface-2/60",
                )}
              >
                <span className="truncate">{row.name}</span>
                <span className="font-mono text-[10px]">
                  {row.done_count}/{row.clip_count}
                </span>
              </button>
            ))}
          </div>
          <p className="pt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
            Clips
          </p>
          <div className="max-h-[50vh] space-y-1 overflow-y-auto">
            {clips.map((clip, i) => (
              <button
                key={clip.path}
                type="button"
                onClick={() => {
                  if (recording || pendingBlob) return;
                  setIndex(i);
                  setBust((b) => b + 1);
                  setScript("");
                  setReviewMode(false);
                  setReviewPath(clip.narrated_path || null);
                }}
                className={cn(
                  "flex w-full flex-col rounded-sm px-2 py-1.5 text-left",
                  i === index
                    ? "bg-accent/20 text-foreground"
                    : "text-muted-foreground hover:bg-surface-2/60",
                )}
              >
                <span className="truncate text-sm">{clip.name}</span>
                <span className="font-mono text-[10px] uppercase tracking-wider">
                  {clip.done ? "done" : clip.pending ? "pending" : "todo"}
                  {clip.has_gpmf ? " · gpmf" : ""}
                  {clip.narrated_path ? " · narrated" : ""}
                </span>
              </button>
            ))}
          </div>
        </aside>

        <section className="space-y-3">
          <div className="relative overflow-hidden rounded-sm border border-border bg-black">
            {current || (reviewMode && reviewPath) ? (
              <video
                ref={videoRef}
                key={streamUrl}
                src={streamUrl}
                className="aspect-video w-full bg-black"
                playsInline
                preload="metadata"
                controls={reviewMode}
                muted={recording || (!reviewMode && !current?.done && !current?.narrated_path)}
                onTimeUpdate={(e) => setScrub(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => {
                  setDuration(e.currentTarget.duration || 0);
                  if (!reviewMode) {
                    e.currentTarget.muted =
                      recording || !(current?.done || current?.narrated_path);
                  }
                }}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
                onRateChange={(e) => {
                  if (recordingRef.current && e.currentTarget.playbackRate !== 1) {
                    e.currentTarget.playbackRate = 1;
                    toast.message("Playback speed locked during recording");
                  }
                }}
              />
            ) : (
              <div className="flex aspect-video items-center justify-center text-sm text-muted-foreground">
                Open a voiceover folder and select a clip
              </div>
            )}
            {recording && (
              <div className="absolute left-3 top-3 flex flex-col gap-1">
                <div className="flex items-center gap-2 rounded-sm bg-red-600/95 px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-white">
                  <Circle className="size-2.5 fill-white" /> Mic recording · {formatClock(recElapsed)}
                </div>
                <div
                  className={cn(
                    "rounded-sm px-2 py-1 font-mono text-[11px] uppercase tracking-wider",
                    researchBreak
                      ? "bg-sky-500 text-white"
                      : playing
                        ? "bg-emerald-600/90 text-white"
                        : "bg-amber-500 text-black",
                  )}
                >
                  {researchBreak
                    ? "Research break — mic + video paused · press R to resume"
                    : `Video ${playing ? "playing" : "paused"} · src ${formatClock(scrub)}`}
                </div>
              </div>
            )}
            {saving && (
              <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/55 px-6 text-center text-sm">
                <p>Building freeze-frame narrated MP4…</p>
                <p className="font-mono text-[11px] text-white/70">
                  This can take several minutes on USB. Leave this window open.
                </p>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={togglePlay} disabled={!current || saving}>
              {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
              Space
            </Button>
            <Button
              size="sm"
              variant={recording ? "danger" : "accent"}
              onClick={() => void toggleRecord()}
              disabled={!current || saving || Boolean(pendingBlob)}
            >
              {recording ? <Square className="size-3.5" /> : <Mic className="size-3.5" />}
              {researchBreak
                ? "Resume mic+video (R)"
                : recording
                  ? "Stop (R)"
                  : "Start session (R)"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={enterResearchBreak}
              disabled={!recording || researchBreak || saving}
              title="Pause mic + video to look something up"
            >
              Research break (B)
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={markEnvironment}
              disabled={!recording && !pendingBlob}
            >
              Environment described (E)
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={cancelSession}
              disabled={!recording && !pendingBlob}
            >
              Cancel session
            </Button>
            <Button
              size="sm"
              variant="accent"
              onClick={() => void finishExport()}
              disabled={!pendingBlob || saving || !allQaChecked}
            >
              Finish &amp; Export
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (current?.narrated_path || reviewPath) {
                  setReviewPath(current?.narrated_path || reviewPath);
                  setReviewMode(true);
                  setBust((b) => b + 1);
                } else toast.message("No narrated MP4 yet — export first");
              }}
              disabled={!current?.narrated_path && !reviewPath}
            >
              Review output
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void attachPending()}
              disabled={!current || saving || recording || !current.pending}
              title="Legacy in-place mux without freeze frames"
            >
              Attach (legacy)
            </Button>
            <Button size="sm" variant="ghost" onClick={seekStart} disabled={!current || recording}>
              Start (S)
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={nextClip}
              disabled={!clips.length || recording || Boolean(pendingBlob)}
            >
              <SkipForward className="size-3.5" /> Next (N)
            </Button>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              src {formatClock(scrub)} / {formatClock(duration)} · out ~{formatClock(projectedOut)}
            </span>
          </div>

          <div className={cn("rounded-sm px-3 py-2 font-mono text-xs", envClass)}>
            Time since last environment description: {formatClock(sinceEnv)}
            {sinceEnv >= ENV_FAIL_S
              ? " — FAIL WARNING: describe environment and press E"
              : sinceEnv >= ENV_WARN_S
                ? " — reminder: describe the environment"
                : recording
                  ? " — describe environment near the beginning"
                  : ""}
          </div>

          <p className="font-mono text-[11px] text-muted-foreground">{status}</p>
          {current && (
            <div className="space-y-1 break-all font-mono text-[10px] text-muted-foreground">
              <p>Source (unchanged on export): {current.path}</p>
              <p>
                Probe: {probe?.width || current.width || "?"}×{probe?.height || current.height || "?"}{" "}
                · {probe?.video_codec || current.video_codec || "?"} · rot{" "}
                {probe?.rotation ?? current.rotation ?? 0} ·{" "}
                {formatClock(probe?.duration || current.duration || 0)}
              </p>
              {reviewPath && <p>Narrated output: {reviewPath}</p>}
            </div>
          )}

          {validation && (
            <div className="rounded-sm border border-border bg-surface p-3 text-xs">
              <p
                className={cn(
                  "font-mono text-[11px] uppercase tracking-wider",
                  validation.pass ? "text-emerald-500" : "text-red-500",
                )}
              >
                ffprobe {validation.pass ? "PASS" : "FAIL"}
              </p>
              <ul className="mt-2 space-y-1">
                {validation.checks?.map((c) => (
                  <li key={c.name} className={c.ok ? "text-emerald-600" : "text-red-500"}>
                    {c.ok ? "✓" : "✗"} {c.name}: {c.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {pendingBlob && (
            <div className="space-y-2 rounded-sm border border-border bg-surface p-3">
              <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-foreground">
                QA gates (required before export)
              </p>
              {qaGates.map((g) => (
                <label key={g.id} className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={Boolean(qa[g.id])}
                    onChange={(e) => setQa((prev) => ({ ...prev, [g.id]: e.target.checked }))}
                  />
                  <span>{g.label}</span>
                </label>
              ))}
              <p className="text-[11px] text-muted-foreground">
                Events logged: {sessionEvents.length} · session {formatClock(sessionEnd)}
              </p>
            </div>
          )}
        </section>

        <aside className="space-y-4 rounded-sm border border-border bg-surface p-3">
          <div>
            <label className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
              Narrator name
            </label>
            <input
              value={narrator}
              onChange={(e) => setNarrator(e.target.value)}
              className="mt-1 w-full rounded-sm border border-border bg-background px-2 py-1.5 text-sm"
              placeholder="Your name"
            />
          </div>

          <div>
            <div className="flex items-center justify-between">
              <label className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
                Microphone
              </label>
              <button
                type="button"
                className="font-mono text-[10px] uppercase tracking-wider text-[#b96d72]"
                onClick={() => void refreshMics()}
              >
                Refresh
              </button>
            </div>
            <select
              value={micId}
              onChange={(e) => {
                setMicId(e.target.value);
                setMicOk(false);
              }}
              disabled={recording}
              className="mt-1 w-full rounded-sm border border-border bg-background px-2 py-1.5 text-sm"
            >
              {mics.length === 0 && <option value="">No mics found</option>}
              {mics.map((m) => (
                <option key={m.deviceId} value={m.deviceId}>
                  {m.label || m.deviceId}
                </option>
              ))}
            </select>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface-2">
              <div
                className={cn(
                  "h-full transition-[width] duration-75",
                  level > 0.85 ? "bg-red-500" : "bg-emerald-500",
                )}
                style={{ width: `${Math.round(level * 100)}%` }}
              />
            </div>
            <p className="mt-1 font-mono text-[10px] text-muted-foreground">
              Input level {micListening || recording ? "· live" : ""} {micOk ? "· test OK" : ""}
            </p>
            <div className="mt-2 flex gap-2">
              <Button
                size="sm"
                variant="outline"
                className="flex-1"
                disabled={recording || micTestBusy}
                onClick={() => void armMicListen()}
              >
                Listen
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="flex-1"
                loading={micTestBusy}
                disabled={recording || micTestBusy}
                onClick={() => void testMic()}
              >
                Test mic (5s)
              </Button>
            </div>
          </div>

          <div className="space-y-2 rounded-sm border border-border/70 p-2 text-xs text-muted-foreground">
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-foreground">
              Session rules
            </p>
            <ul className="list-disc space-y-1 pl-4">
              <li>SPACE pauses/resumes video only — mic never pauses.</li>
              <li>
                <strong>B</strong> = research break (mic + video both pause). Look it up, then{" "}
                <strong>R</strong> resumes both.
              </li>
              <li>Export freezes the frame for each SPACE pause so narration stays in sync.</li>
              <li>Research-break time is removed from the final video and audio.</li>
              <li>Press E right after you describe the environment.</li>
              <li>Source file is never overwritten — output is *.narrated.mp4.</li>
            </ul>
          </div>

          <div>
            <label className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
              Gemini API key (optional draft script)
            </label>
            <input
              type="password"
              value={geminiKey}
              onChange={(e) => setGeminiKey(e.target.value)}
              className="mt-1 w-full rounded-sm border border-border bg-background px-2 py-1.5 text-sm"
              placeholder="AIza…"
            />
            <Button
              size="sm"
              variant="outline"
              className="mt-2 w-full"
              loading={scriptBusy}
              disabled={!current || scriptBusy}
              onClick={() => void draftScript({ wholeClip: false })}
            >
              Draft next ~1 min
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="mt-2 w-full"
              loading={scriptBusy}
              disabled={!current || scriptBusy}
              onClick={() => void draftScript({ wholeClip: true })}
            >
              Draft whole clip
            </Button>
            {script && (
              <textarea
                value={script}
                onChange={(e) => setScript(e.target.value)}
                className="mt-2 h-40 w-full rounded-sm border border-border bg-background p-2 text-sm leading-relaxed"
              />
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}
