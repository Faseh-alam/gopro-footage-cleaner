#!/usr/bin/env python3
"""Local server for the voiceover video player."""

from __future__ import annotations

import json
import os
import shutil
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from paths import (
    RECORDINGS,
    STATIC,
    migrate_flat_recordings,
    safe_filename,
    safe_stem,
    sidecar_path,
    video_dir,
)

MAX_AUDIO_BYTES = 80 * 1024 * 1024
MAX_VIDEO_BYTES = 15 * 1024 * 1024 * 1024
CHUNK = 8 * 1024 * 1024


def load_sidecar(video_name: str) -> dict:
    path = sidecar_path(video_name)
    if not path.is_file():
        return {"video": Path(video_name).name, "resume_at": 0.0, "takes": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"video": Path(video_name).name, "resume_at": 0.0, "takes": []}


def save_sidecar(data: dict) -> None:
    name = data.get("video") or "video"
    folder = video_dir(name)
    data["folder"] = safe_stem(name)
    path = sidecar_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read_chunks(handler: SimpleHTTPRequestHandler, length: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    remaining = length
    with dest.open("wb") as out:
        while remaining > 0:
            chunk = handler.rfile.read(min(CHUNK, remaining))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, download: bool = False) -> None:
        suffix = path.suffix.lower()
        mime = {
            ".mp4": "video/mp4",
            ".webm": "audio/webm",
            ".ogg": "audio/ogg",
            ".json": "application/json",
        }.get(suffix, "application/octet-stream")
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/takes":
            name = (parse_qs(parsed.query).get("video") or [""])[0]
            self._json(200, load_sidecar(name))
            return
        if parsed.path.startswith("/recordings/"):
            rel = unquote(parsed.path[len("/recordings/") :]).lstrip("/")
            path = (RECORDINGS / rel).resolve()
            root = RECORDINGS.resolve()
            if root not in path.parents or not path.is_file():
                self.send_error(404)
                return
            self._file(path, download=path.suffix.lower() in {".mp4", ".json", ".webm", ".ogg"})
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/source":
            self._save_source()
            return
        if parsed.path == "/api/takes":
            self._save_take()
            return
        if parsed.path == "/api/export":
            self._export()
            return
        if parsed.path == "/api/open-recordings":
            self._open_recordings()
            return
        self.send_error(404)

    def _save_source(self) -> None:
        video_name = self.headers.get("X-Video-Name") or "video.mp4"
        data = load_sidecar(video_name)
        data["video"] = Path(video_name).name
        folder = video_dir(video_name)
        data["folder"] = safe_stem(video_name)
        if (self.headers.get("X-Demo") or "") == "1":
            dest = folder / "sample.mp4"
            demo = STATIC / "demo" / "sample.mp4"
            if demo.is_file() and not dest.is_file():
                shutil.copy2(demo, dest)
            data["source"] = dest.name if dest.is_file() else str(demo)
            save_sidecar(data)
            self._json(200, {"ok": True, **data})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_VIDEO_BYTES:
            self._json(400, {"ok": False, "error": "Video file is missing or too large."})
            return
        dest = folder / safe_filename(video_name)
        _read_chunks(self, length, dest)
        data["source"] = dest.name
        save_sidecar(data)
        self._json(200, {"ok": True, **data})

    def _save_take(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_AUDIO_BYTES:
            self._json(400, {"ok": False, "error": "Audio take is missing or too large."})
            return

        video_name = self.headers.get("X-Video-Name") or "video"
        try:
            start = float(self.headers.get("X-Start") or 0)
            end = float(self.headers.get("X-End") or 0)
            audio_elapsed = float(self.headers.get("X-Audio-Elapsed") or 0)
        except ValueError:
            self._json(400, {"ok": False, "error": "Invalid take timestamps."})
            return

        timeline_raw = self.headers.get("X-Timeline") or "[]"
        try:
            segments = json.loads(unquote(timeline_raw))
            if not isinstance(segments, list):
                segments = []
        except json.JSONDecodeError:
            segments = []

        mime = (self.headers.get("Content-Type") or "audio/webm").split(";")[0]
        ext = "ogg" if "ogg" in mime else "webm"
        folder = video_dir(video_name)
        body_path = folder / "_incoming.bin"
        _read_chunks(self, length, body_path)

        data = load_sidecar(video_name)
        take_n = len(data.get("takes") or []) + 1
        stem = safe_stem(video_name)
        filename = f"{stem}_take{take_n:02d}.{ext}"
        dest = folder / filename
        body_path.replace(dest)

        take = {
            "index": take_n,
            "start": round(start, 3),
            "end": round(max(start, end), 3),
            "audio_elapsed": round(audio_elapsed, 3),
            "audio": filename,
            "segments": segments,
        }
        data["video"] = Path(video_name).name
        data.setdefault("takes", []).append(take)
        data["resume_at"] = round(max(float(data.get("resume_at") or 0), take["end"]), 3)
        save_sidecar(data)
        self._json(200, {"ok": True, **data, "saved": take})

    def _export(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        video_name = payload.get("video") or self.headers.get("X-Video-Name") or "video"
        sidecar = load_sidecar(video_name)
        try:
            from export import export_voiceover

            out = export_voiceover(video_name, sidecar)
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        sidecar["export"] = out.name
        sidecar["folder"] = safe_stem(video_name)
        save_sidecar(sidecar)
        rel = f"{sidecar['folder']}/{out.name}"
        self._json(
            200,
            {
                "ok": True,
                "file": out.name,
                "folder": sidecar["folder"],
                "url": f"/recordings/{rel}",
                "path": str(out),
            },
        )

    def _open_recordings(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        video_name = payload.get("video") or ""
        folder = video_dir(video_name) if video_name else RECORDINGS
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
        except AttributeError:
            os.system(f'xdg-open "{folder}"')
        self._json(200, {"ok": True, "path": str(folder)})


def main() -> None:
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    migrate_flat_recordings()
    host, port = "127.0.0.1", 8765
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Voiceover player at http://{host}:{port}")
    print(f"Takes and exports are saved in {RECORDINGS}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
