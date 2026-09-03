const video = document.getElementById("video");
const fileInput = document.getElementById("file");
const empty = document.getElementById("empty");
const recBadge = document.getElementById("recBadge");
const pauseHint = document.getElementById("pauseHint");
const playBar = document.getElementById("playBar");
const playFill = document.getElementById("playFill");
const playHead = document.getElementById("playHead");
const resumeMark = document.getElementById("resumeMark");
const coverFill = document.getElementById("coverFill");
const liveFill = document.getElementById("liveFill");
const timeLabel = document.getElementById("timeLabel");
const coverLabel = document.getElementById("coverLabel");
const statusEl = document.getElementById("status");
const micClock = document.getElementById("micClock");
const takesEl = document.getElementById("takes");
const exportLink = document.getElementById("exportLink");
const exportBtn = document.getElementById("exportBtn");
const folderBtn = document.getElementById("folderBtn");

const state = {
  fileName: "",
  duration: 0,
  resumeAt: 0,
  recording: false,
  sessionStart: 0,
  recStartedAt: 0,
  pausedMs: 0,
  pauseBegan: 0,
  audioElapsed: 0,
  takes: [],
  recorder: null,
  chunks: [],
  stream: null,
  mime: "audio/webm",
  seeking: false,
  segments: [],
  segKind: null,
  segStartedWall: 0,
  segVideoStart: 0,
  exporting: false,
};

function fmt(seconds) {
  const s = Math.max(0, seconds || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const core = `${m}:${String(sec).padStart(2, "0")}`;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : core;
}

function pct(value, total) {
  if (!total) return 0;
  return Math.min(100, Math.max(0, (value / total) * 100));
}

function setStatus(text) {
  statusEl.textContent = text;
}

function savedVoice() {
  return state.takes.reduce((sum, take) => sum + (Number(take.audio_elapsed) || 0), 0);
}

function liveVoice() {
  return state.recording ? state.audioElapsed : 0;
}

function pickMime() {
  const types = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
  ];
  return types.find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || "audio/webm";
}

function renderTakes() {
  if (!state.takes.length) {
    takesEl.innerHTML = `<li class="muted">No takes yet</li>`;
    return;
  }
  takesEl.innerHTML = state.takes
    .map(
      (t) => `<li>
        Take ${t.index} · video ${fmt(t.start)}–${fmt(t.end)}
        <span>voice ${fmt(t.audio_elapsed)}${
          t.audio_elapsed > t.end - t.start + 0.2 ? " · longer than picture" : ""
        }</span>
      </li>`,
    )
    .join("");
}

function paint() {
  const now = video.currentTime || 0;
  const duration = state.duration || video.duration || 0;
  const saved = savedVoice();
  const live = liveVoice();
  const voice = saved + live;
  const scale = Math.max(duration, voice, 0.001);

  playFill.style.width = `${pct(now, duration)}%`;
  playHead.style.left = `${pct(now, duration)}%`;
  coverFill.style.left = "0%";
  coverFill.style.width = `${pct(saved, scale)}%`;
  liveFill.style.left = `${pct(saved, scale)}%`;
  liveFill.style.width = `${pct(live, scale)}%`;

  resumeMark.style.display = duration ? "block" : "none";
  resumeMark.style.left = `${pct(state.resumeAt, duration)}%`;

  timeLabel.textContent = `${fmt(now)} / ${fmt(duration)}`;
  coverLabel.textContent = `${fmt(voice)} voice`;

  if (state.recording) {
    let extra = state.pausedMs;
    if (state.pauseBegan) extra += performance.now() - state.pauseBegan;
    state.audioElapsed = (performance.now() - state.recStartedAt) / 1000;
    micClock.hidden = false;
    micClock.textContent = `Mic ${fmt(state.audioElapsed)}`;
  } else {
    micClock.hidden = true;
  }

  recBadge.hidden = !state.recording;
  pauseHint.hidden = !(state.recording && video.paused);
  playBar.classList.toggle("is-locked", state.recording);
  playBar.classList.toggle("is-seeking", state.seeking);
  playBar.setAttribute("aria-valuenow", String(Math.round(now)));
  playBar.setAttribute("aria-valuemax", String(Math.round(duration)));
}

function recordingHref(data, fileName) {
  const file = data.export || data.file;
  if (!file) return "";
  if (String(file).includes("/")) return `/recordings/${file}`;
  const folder = data.folder || String(fileName || "").replace(/\.[^.]+$/, "");
  return `/recordings/${folder}/${file}`;
}

async function loadSidecar(name) {
  const res = await fetch(`/api/takes?video=${encodeURIComponent(name)}`);
  if (!res.ok) return;
  const data = await res.json();
  state.resumeAt = Number(data.resume_at) || 0;
  state.takes = data.takes || [];
  renderTakes();
  if (data.export) {
    const href = recordingHref(data, name);
    exportLink.hidden = false;
    exportLink.classList.add("export-link");
    exportLink.innerHTML = `Last export: <a href="${href}" download>${data.export}</a>`;
  }
  video.currentTime = state.resumeAt;
  paint();
}

async function openNamedVideo(src, fileName, revokeOld = true) {
  if (state.recording) await stopSession(false);
  if (revokeOld && video.src && video.src.startsWith("blob:")) URL.revokeObjectURL(video.src);
  state.fileName = fileName;
  state.takes = [];
  state.resumeAt = 0;
  video.src = src;
  video.muted = true;
  empty.hidden = true;
  setStatus(`${fileName} loaded. Drag the red bar to scrub. Space plays from here. R records from last R.`);
  await loadSidecar(fileName);
  document.activeElement?.blur?.();
}

fileInput.addEventListener("change", async () => {
  const file = fileInput.files?.[0];
  if (!file) return;
  setStatus(`Saving ${file.name} for export…`);
  try {
    await registerSource(file.name, { file });
  } catch (err) {
    setStatus(err.message || "Could not save the source video.");
    return;
  }
  await openNamedVideo(URL.createObjectURL(file), file.name);
});

document.getElementById("demoBtn").addEventListener("click", async () => {
  try {
    await registerSource("sample.mp4", { demo: true });
  } catch (err) {
    setStatus(err.message || "Could not register the demo clip.");
    return;
  }
  await openNamedVideo("/demo/sample.mp4", "sample.mp4", false);
});

video.addEventListener("loadedmetadata", () => {
  state.duration = video.duration || 0;
  video.currentTime = state.resumeAt;
  paint();
});

video.addEventListener("timeupdate", paint);
video.addEventListener("play", paint);
video.addEventListener("pause", paint);
video.addEventListener("ended", async () => {
  if (state.recording) await stopSession(true);
  else paint();
});

function closeSeg() {
  if (!state.segKind) return;
  const duration = (performance.now() - state.segStartedWall) / 1000;
  if (duration >= 0.02) {
    if (state.segKind === "play") {
      state.segments.push({
        kind: "play",
        video_start: round3(state.segVideoStart),
        video_end: round3(video.currentTime || state.segVideoStart),
        duration: round3(duration),
      });
    } else {
      state.segments.push({
        kind: "hold",
        video_at: round3(state.segVideoStart),
        duration: round3(duration),
      });
    }
  }
  state.segKind = null;
}

function openSeg(kind) {
  closeSeg();
  state.segKind = kind;
  state.segStartedWall = performance.now();
  state.segVideoStart = video.currentTime || 0;
}

function round3(value) {
  return Math.round((Number(value) || 0) * 1000) / 1000;
}

async function registerSource(fileName, { demo = false, file = null } = {}) {
  const headers = { "X-Video-Name": fileName };
  if (demo) headers["X-Demo"] = "1";
  const res = await fetch("/api/source", {
    method: "POST",
    headers,
    body: demo ? undefined : file,
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || "Could not save the source video.");
}

function snapToResume() {
  return new Promise((resolve) => {
    if (Math.abs((video.currentTime || 0) - state.resumeAt) < 0.05) {
      resolve();
      return;
    }
    const done = () => {
      video.removeEventListener("seeked", done);
      resolve();
    };
    video.addEventListener("seeked", done);
    video.currentTime = state.resumeAt;
  });
}

async function ensureMic() {
  if (state.stream) return state.stream;
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser cannot access the microphone.");
  }
  state.stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  state.mime = pickMime();
  return state.stream;
}

async function startSession() {
  if (!video.src) {
    setStatus("Open a video first.");
    return;
  }
  if (state.recording) return;
  const stream = await ensureMic();
  state.chunks = [];
  state.recorder = new MediaRecorder(stream, { mimeType: state.mime });
  state.recorder.ondataavailable = (ev) => {
    if (ev.data && ev.data.size) state.chunks.push(ev.data);
  };
  await snapToResume();
  state.sessionStart = state.resumeAt;
  state.recording = true;
  state.recStartedAt = performance.now();
  state.pausedMs = 0;
  state.pauseBegan = 0;
  state.audioElapsed = 0;
  state.segments = [];
  state.segKind = null;
  state.recorder.start(250);
  await video.play();
  openSeg("play");
  setStatus("Recording voiceover. Space pauses the picture only. R stops both.");
  paint();
}

async function stopSession(fromEnded) {
  if (!state.recording) return;
  closeSeg();
  const rawEnd = fromEnded ? state.duration || video.currentTime : video.currentTime;
  const end = Math.max(state.sessionStart, rawEnd || 0);
  video.pause();
  const rec = state.recorder;
  state.recording = false;
  state.pauseBegan = 0;
  const elapsed = (performance.now() - state.recStartedAt) / 1000;
  state.audioElapsed = elapsed;

  const blob = await new Promise((resolve) => {
    if (!rec || rec.state === "inactive") {
      resolve(new Blob(state.chunks, { type: state.mime }));
      return;
    }
    rec.onstop = () => resolve(new Blob(state.chunks, { type: rec.mimeType || state.mime }));
    rec.stop();
  });

  state.resumeAt = Math.max(state.resumeAt, end);
  video.currentTime = state.resumeAt;

  if (blob.size > 0 && state.fileName) {
    const res = await fetch("/api/takes", {
      method: "POST",
      headers: {
        "Content-Type": blob.type || state.mime,
        "X-Video-Name": state.fileName,
        "X-Start": String(state.sessionStart),
        "X-End": String(end),
        "X-Audio-Elapsed": String(elapsed),
        "X-Timeline": encodeURIComponent(JSON.stringify(state.segments)),
      },
      body: blob,
    });
    const data = await res.json();
    if (data.ok) {
      state.takes = data.takes || state.takes;
      state.resumeAt = Number(data.resume_at) || state.resumeAt;
      renderTakes();
      setStatus(
        `Stopped at ${fmt(state.resumeAt)}. Voiceover ${fmt(elapsed)} for video ${fmt(end - state.sessionStart)}. Space or R continue from here.`,
      );
    } else {
      setStatus(data.error || "Could not save take.");
    }
  } else {
    setStatus(`Stopped at ${fmt(state.resumeAt)}. Space or R continue from here.`);
  }
  paint();
}

async function onSpace() {
  if (!video.src) {
    setStatus("Open a video first.");
    return;
  }
  if (state.recording) {
    if (video.paused) {
      closeSeg();
      state.pausedMs += performance.now() - (state.pauseBegan || performance.now());
      state.pauseBegan = 0;
      await video.play();
      openSeg("play");
      setStatus("Video playing. Microphone still recording.");
    } else {
      closeSeg();
      video.pause();
      state.pauseBegan = performance.now();
      openSeg("hold");
      setStatus("Video paused. Microphone still recording — keep talking.");
    }
    paint();
    return;
  }
  if (video.paused) {
    await video.play();
    setStatus(`Playing from ${fmt(video.currentTime)}. Picture only — no microphone.`);
  } else {
    video.pause();
    setStatus("Video paused. Drag the red bar to jump. R records from the last R stop.");
  }
  paint();
}

async function onR() {
  if (!video.src) {
    setStatus("Open a video first.");
    return;
  }
  try {
    if (state.recording) await stopSession(false);
    else await startSession();
  } catch (err) {
    setStatus(err.message || "Microphone permission is required for R.");
  }
}

window.addEventListener(
  "keydown",
  (event) => {
    const tag = event.target?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (event.repeat) return;
    if (event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      onSpace();
    } else if (event.key === "r" || event.key === "R") {
      event.preventDefault();
      event.stopPropagation();
      onR();
    }
  },
  true,
);

setInterval(() => {
  if (state.recording) paint();
}, 200);

function clipDuration() {
  return state.duration || video.duration || 0;
}

function timeFromPointer(event) {
  const rect = playBar.getBoundingClientRect();
  const x = (event.clientX - rect.left) / Math.max(1, rect.width);
  return Math.min(clipDuration(), Math.max(0, x * clipDuration()));
}

function seekTo(time) {
  if (!video.src || !clipDuration()) return false;
  if (state.recording) {
    setStatus("Stop the take with R before scrubbing. Recording always continues from the last R stop.");
    return false;
  }
  video.currentTime = time;
  paint();
  return true;
}

function endSeek(event) {
  if (!state.seeking) return;
  state.seeking = false;
  try {
    playBar.releasePointerCapture(event.pointerId);
  } catch {
    /* already released */
  }
  const time = timeFromPointer(event);
  if (seekTo(time)) {
    setStatus(`Jumped to ${fmt(time)}. Space plays from here. R still records from last R (${fmt(state.resumeAt)}).`);
  }
}

playBar.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  playBar.setPointerCapture(event.pointerId);
  state.seeking = true;
  seekTo(timeFromPointer(event));
});

playBar.addEventListener("pointermove", (event) => {
  if (!state.seeking) return;
  seekTo(timeFromPointer(event));
});

playBar.addEventListener("pointerup", endSeek);
playBar.addEventListener("pointercancel", endSeek);

playBar.addEventListener("keydown", (event) => {
  if (!video.src || state.recording) return;
  const step = event.shiftKey ? 5 : 1;
  if (event.key === "ArrowRight") {
    event.preventDefault();
    seekTo(Math.min(clipDuration(), (video.currentTime || 0) + step));
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    seekTo(Math.max(0, (video.currentTime || 0) - step));
  } else if (event.key === "Home") {
    event.preventDefault();
    seekTo(0);
  } else if (event.key === "End") {
    event.preventDefault();
    seekTo(clipDuration());
  }
});

exportBtn.addEventListener("click", async () => {
  if (!state.fileName) {
    setStatus("Open a video and record a take first.");
    return;
  }
  if (!state.takes.length) {
    setStatus("Record at least one take with R before exporting.");
    return;
  }
  if (state.exporting) return;
  state.exporting = true;
  exportBtn.disabled = true;
  setStatus("Exporting video with voiceover… this can take a minute.");
  try {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video: state.fileName }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "Export failed.");
    exportLink.hidden = false;
    exportLink.classList.add("export-link");
    exportLink.innerHTML = `Saved <a href="${data.url}" download>${data.file}</a> in folder <code>${data.folder || ""}</code>.`;
    setStatus(`Export ready in recordings\\${data.folder}\\${data.file}`);
  } catch (err) {
    setStatus(err.message || "Export failed.");
  } finally {
    state.exporting = false;
    exportBtn.disabled = false;
  }
});

folderBtn.addEventListener("click", async () => {
  const res = await fetch("/api/open-recordings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video: state.fileName || "" }),
  });
  const data = await res.json();
  if (data.ok) setStatus(`Recordings folder: ${data.path}`);
});

paint();
