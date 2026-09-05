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
          "Record egocentric narration onto USB GoPro clips with pause-and-describe while preserving GPMF/IMU.",
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
};

type ClassRow = {
  name: string;
  clip_count: number;
  done_count: number;
  clips: Clip[];
};

const MIC_KEY = "wc-voiceover-mic-id";
const NARRATOR_KEY = "wc-voiceover-narrator";
const GEMINI_KEY = "wc-voiceover-gemini-key";

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

  const [root, setRoot] = useState("");
  const [classes, setClasses] = useState<ClassRow[]>([]);
  const [activeClass, setActiveClass] = useState("");
  const [index, setIndex] = useState(0);
  const [bust, setBust] = useState(0);
  const [status, setStatus] = useState(
    "Open the folder with your clips — recording rewrites that same file in place",
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

  recordingRef.current = recording;

  const clips = useMemo(() => {
    const row = classes.find((c) => c.name === activeClass);
    return row?.clips || [];
  }, [classes, activeClass]);

  const current = clips[index] || null;

  const streamUrl = current
    ? `${apiUrl(`/api/voiceover/stream?path=${encodeURIComponent(current.path)}`)}&v=${bust}`
    : "";

  const refreshMics = useCallback(async () => {
    try {
      // Permission so labels appear.
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

  const stopMeter = useCallback(() => {
    if (meterRafRef.current) cancelAnimationFrame(meterRafRef.current);
    meterRafRef.current = 0;
    setLevel(0);
  }, []);

  const startMeter = useCallback((stream: MediaStream) => {
    stopMeter();
    const ctx = audioCtxRef.current || new AudioContext();
    audioCtxRef.current = ctx;
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
      setLevel(Math.min(1, avg * 1.8));
      meterRafRef.current = requestAnimationFrame(tick);
    };
    tick();
  }, [stopMeter]);

  const releaseMic = useCallback(() => {
    stopMeter();
    mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    mediaStreamRef.current = null;
    mediaRecorderRef.current = null;
    chunksRef.current = [];
  }, [stopMeter]);

  useEffect(() => () => releaseMic(), [releaseMic]);

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
      `Loaded ${data.clip_count} clip(s) · ${data.done_count} done · rewrites files under ${data.root}`,
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

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      void v.play().catch(() => undefined);
    } else {
      v.pause();
    }
  }, []);

  const seekStart = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = 0;
    setScrub(0);
  }, []);

  const nudge = useCallback((delta: number) => {
    if (recordingRef.current) return;
    const v = videoRef.current;
    if (!v) return;
    const next = Math.max(0, Math.min(v.duration || 0, (v.currentTime || 0) + delta));
    v.currentTime = next;
    setScrub(next);
  }, []);

  const nextClip = useCallback(() => {
    if (recordingRef.current) {
      toast.message("Stop recording (R) before changing clips");
      return;
    }
    if (!clips.length) return;
    setIndex((i) => (i + 1) % clips.length);
    setBust((b) => b + 1);
    setScript("");
  }, [clips.length]);

  const releasePlayer = useCallback(async () => {
    const v = videoRef.current;
    if (!v) return;
    try {
      v.pause();
    } catch {
      /* ignore */
    }
    // Drop the HTTP stream so Windows/USB unlocks the MP4 for mux replace.
    v.removeAttribute("src");
    v.load();
    await new Promise((r) => window.setTimeout(r, 400));
  }, []);

  const stopRecording = useCallback(
    async (opts: { discard?: boolean } = {}) => {
      const recorder = mediaRecorderRef.current;
      if (!recorder || recorder.state === "inactive") {
        setRecording(false);
        releaseMic();
        return;
      }
      setRecording(false);
      const blob: Blob = await new Promise((resolve) => {
        recorder.onstop = () => {
          resolve(new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" }));
        };
        recorder.stop();
      });
      releaseMic();
      setRecElapsed(0);

      if (opts.discard) {
        setStatus("Take discarded — original video untouched");
        return;
      }
      if (!current) return;
      if (blob.size < 512) {
        toast.error("Recording was empty — check the mic and try again");
        setStatus("Empty take — nothing muxed");
        return;
      }

      setSaving(true);
      setStatus(`Attaching narration into ${current.name} (GPMF preserved)…`);
      await releasePlayer();
      try {
        const form = new FormData();
        form.append("path", current.path);
        form.append("root", root);
        form.append("narrator", narrator);
        const micLabel = mics.find((m) => m.deviceId === micId)?.label || micId || "default";
        form.append("mic", micLabel);
        form.append("audio", blob, "take.webm");
        const response = await fetch(apiUrl("/api/voiceover/save-take"), {
          method: "POST",
          body: form,
        });
        const result = (await response.json().catch(() => ({}))) as {
          ok?: boolean;
          pending?: boolean;
          message?: string;
          path?: string;
          error?: string;
        };
        if (response.ok && result.ok !== false) {
          const savedPath = result.path || current.path;
          setStatus(`Rewrote original clip · play from 0:00 · ${savedPath}`);
          toast.success("Voiceover attached — play from the start to review");
          await scanRoot(root, { keepClass: activeClass, keepPath: current.path });
          const v = videoRef.current;
          if (v) {
            v.muted = false;
            v.currentTime = 0;
            setScrub(0);
          }
        } else if (result.pending || response.status === 409) {
          setStatus(
            result.message ||
              "Take kept on USB — click Attach voiceover (player released the file)",
          );
          toast.message("Take saved — click Attach voiceover to finish");
          await scanRoot(root, { keepClass: activeClass, keepPath: current.path });
        } else {
          throw new Error(result.error || result.message || `Save failed (${response.status})`);
        }
      } catch (error: any) {
        toast.error(error.message || "Mux failed — original left untouched");
        setStatus(error.message || "Mux failed");
        setBust((b) => b + 1);
      } finally {
        setSaving(false);
      }
    },
    [
      activeClass,
      current,
      micId,
      mics,
      narrator,
      releaseMic,
      releasePlayer,
      root,
      scanRoot,
    ],
  );

  const attachPending = useCallback(async () => {
    if (!current) return;
    setSaving(true);
    setStatus(`Attaching pending take into ${current.name}…`);
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
      toast.success("Voiceover attached — play from the start to review");
      setStatus(result.message || `Attached · ${result.path || current.path}`);
      await scanRoot(root, { keepClass: activeClass, keepPath: current.path });
      const v = videoRef.current;
      if (v) {
        v.muted = false;
        v.currentTime = 0;
        setScrub(0);
      }
    } catch (error: any) {
      toast.error(error.message || "Attach failed — try again in a moment");
      setStatus(error.message || "Attach failed");
      setBust((b) => b + 1);
    } finally {
      setSaving(false);
    }
  }, [
    activeClass,
    current,
    micId,
    mics,
    narrator,
    releasePlayer,
    root,
    scanRoot,
  ]);

  const startRecording = useCallback(async () => {
    if (!current) {
      toast.error("Select a clip first");
      return;
    }
    if (saving) return;
    try {
      const constraints: MediaStreamConstraints = {
        audio: micId ? { deviceId: { exact: micId } } : true,
      };
      const stream = await navigator.mediaDevices.getUserMedia(constraints);
      mediaStreamRef.current = stream;
      startMeter(stream);
      chunksRef.current = [];
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
      // Narration always lines up from t=0 of the MP4 — restart the picture there.
      const v = videoRef.current;
      if (v) {
        v.muted = true;
        v.currentTime = 0;
        setScrub(0);
      }
      recorder.start(250);
      setRecording(true);
      setRecElapsed(0);
      setStatus(
        `Recording · will rewrite ${current.path} · Space pauses video only`,
      );
      if (v) {
        try {
          await v.play();
        } catch {
          /* ignore */
        }
      }
    } catch (error: any) {
      releaseMic();
      toast.error(error?.message || "Could not start microphone");
    }
  }, [current, micId, releaseMic, saving, startMeter]);

  const toggleRecord = useCallback(() => {
    if (recording) void stopRecording();
    else void startRecording();
  }, [recording, startRecording, stopRecording]);

  useEffect(() => {
    if (!recording) return;
    const started = Date.now();
    const id = window.setInterval(() => {
      setRecElapsed((Date.now() - started) / 1000);
    }, 200);
    return () => clearInterval(id);
  }, [recording]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) {
        return;
      }
      if (event.key === " " || event.code === "Space") {
        event.preventDefault();
        togglePlay();
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
        if (recordingRef.current) {
          event.preventDefault();
          void stopRecording({ discard: true });
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
  }, [nextClip, nudge, seekStart, stopRecording, togglePlay, toggleRecord]);

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
          whole_clip?: boolean;
          uploads_video?: boolean;
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
        toast.success(
          wholeClip
            ? "Whole-clip draft ready (small proxy uploaded, not the original file)"
            : `Draft for ~1 min from ${formatClock(data.start_seconds ?? start)}`,
        );
      } catch (error: any) {
        toast.error(error.message || "Gemini failed");
      } finally {
        setScriptBusy(false);
      }
    },
    [current, geminiKey, scrub],
  );

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-6 py-4">
        <div className="flex items-center gap-4">
          <Logo />
          <div>
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
              Voiceover station
            </p>
            <h1 className="font-[Syne] text-lg font-semibold tracking-tight">
              Narrate · pause-and-describe · IMU safe
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

      <main className="grid gap-4 p-4 lg:grid-cols-[260px_minmax(0,1fr)_300px]">
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
                  if (recording) return;
                  setActiveClass(row.name);
                  setIndex(0);
                  setBust((b) => b + 1);
                  setScript("");
                }}
                className={cn(
                  "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-left text-sm",
                  activeClass === row.name ? "bg-surface-2 text-foreground" : "text-muted-foreground hover:bg-surface-2/60",
                )}
              >
                <span className="truncate">{row.name}</span>
                <span className="font-mono text-[10px]">
                  {row.done_count}/{row.clip_count}
                </span>
              </button>
            ))}
          </div>
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground pt-2">
            Clips
          </p>
          <div className="max-h-[50vh] space-y-1 overflow-y-auto">
            {clips.map((clip, i) => (
              <button
                key={clip.path}
                type="button"
                onClick={() => {
                  if (recording) return;
                  setIndex(i);
                  setBust((b) => b + 1);
                  setScript("");
                }}
                className={cn(
                  "flex w-full flex-col rounded-sm px-2 py-1.5 text-left",
                  i === index ? "bg-accent/20 text-foreground" : "hover:bg-surface-2/60 text-muted-foreground",
                )}
              >
                <span className="truncate text-sm">{clip.name}</span>
                <span className="font-mono text-[10px] uppercase tracking-wider">
                  {clip.done ? "done" : clip.pending ? "pending" : "todo"}
                  {clip.has_gpmf ? " · gpmf" : ""}
                </span>
              </button>
            ))}
          </div>
        </aside>

        <section className="space-y-3">
          <div className="relative overflow-hidden rounded-sm border border-border bg-black">
            {current ? (
              <video
                ref={videoRef}
                key={streamUrl}
                src={streamUrl}
                className="aspect-video w-full bg-black"
                playsInline
                preload="metadata"
                muted={recording || !current.done}
                onTimeUpdate={(e) => setScrub(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => {
                  setDuration(e.currentTarget.duration || 0);
                  // Mute GoPro ambient until a take is muxed; unmute done clips for review.
                  e.currentTarget.muted = recording || !current.done;
                }}
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
              />
            ) : (
              <div className="flex aspect-video items-center justify-center text-sm text-muted-foreground">
                Open a voiceover folder and select a clip
              </div>
            )}
            {recording && (
              <div className="absolute left-3 top-3 flex items-center gap-2 rounded-sm bg-red-600/90 px-2 py-1 font-mono text-[11px] uppercase tracking-wider text-white">
                <Circle className="size-2.5 fill-white" /> Rec {formatClock(recElapsed)}
              </div>
            )}
            {saving && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm">
                Attaching narration into MP4…
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
              disabled={!current || saving}
            >
              {recording ? <Square className="size-3.5" /> : <Mic className="size-3.5" />}
              {recording ? "Stop (R)" : "Record (R)"}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void attachPending()}
              disabled={!current || saving || recording || !current.pending}
            >
              Attach voiceover
            </Button>            <Button size="sm" variant="ghost" onClick={seekStart} disabled={!current || recording}>
              Start (S)
            </Button>
            <Button size="sm" variant="ghost" onClick={nextClip} disabled={!clips.length || recording}>
              <SkipForward className="size-3.5" /> Next (N)
            </Button>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatClock(scrub)} / {formatClock(duration)}
            </span>
          </div>

          <p className="font-mono text-[11px] text-muted-foreground">{status}</p>
          {current && (
            <p className="break-all font-mono text-[10px] text-muted-foreground">
              Original file (rewritten on save): {current.path}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            When you stop recording, your voice is written into the same file you are playing.
            On slow USBs the app releases the player first; if attach fails, your take is kept and
            you can press Attach voiceover (then play from 0:00 to check). Esc discards a live take.
          </p>
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
              onChange={(e) => setMicId(e.target.value)}
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
                className={cn("h-full transition-[width] duration-75", level > 0.85 ? "bg-red-500" : "bg-emerald-500")}
                style={{ width: `${Math.round(level * 100)}%` }}
              />
            </div>
            <p className="mt-1 font-mono text-[10px] text-muted-foreground">Input level</p>
          </div>

          <div className="space-y-2 rounded-sm border border-border/70 p-2 text-xs text-muted-foreground">
            <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-foreground">
              Narration checklist
            </p>
            <ul className="list-disc space-y-1 pl-4">
              <li>First person only for YOUR hands/tools (camera wearer).</li>
              <li>Other people in frame → third person (“another worker is kneeling…”).</li>
              <li>Granular motor steps, not summaries (“I install the gearbox”).</li>
              <li>Also describe environment as if to a blind person.</li>
              <li>Aim for ≥25 words per minute; pause-and-describe on fast actions.</li>
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
            <p className="mt-1 text-[11px] text-muted-foreground">
              Follows client egocentric PDF rules. Sends a tiny silent proxy only — never your
              original 250–500MB file. Not Whisper delivery transcripts.
            </p>
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
                className="mt-2 h-48 w-full rounded-sm border border-border bg-background p-2 text-sm leading-relaxed"
              />
            )}
          </div>

          {current?.has_gpmf && (
            <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-emerald-500">
              Source has GPMF / IMU
            </p>
          )}
        </aside>
      </main>
    </div>
  );
}
