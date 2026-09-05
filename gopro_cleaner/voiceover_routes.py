"""Voiceover Station API routes."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from .core import voiceover_store
from .core.folder_picker import pick_folder
from .core.probe import probe_media


def create_voiceover_blueprint() -> Blueprint:
    vo = Blueprint("voiceover", __name__)

    @vo.post("/api/voiceover/pick-folder")
    def voiceover_pick_folder():
        payload = request.get_json(silent=True) or {}
        initial_raw = str(payload.get("initial") or "").strip()
        initial = Path(initial_raw).expanduser() if initial_raw else None
        try:
            chosen = pick_folder(initial)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        if chosen is None:
            return jsonify({"cancelled": True, "path": None})
        try:
            root = voiceover_store.resolve_voiceover_root(chosen)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"cancelled": False, "path": str(root), "picked": str(chosen)})

    @vo.get("/api/voiceover/scan")
    def voiceover_scan():
        raw = str(request.args.get("root") or "").strip()
        if not raw:
            return jsonify({"error": "root is required"}), 400
        try:
            data = voiceover_store.scan_voiceover_tree(Path(raw))
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **data})

    @vo.get("/api/voiceover/stream")
    def voiceover_stream():
        raw_path = str(request.args.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        path = Path(raw_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        if path.suffix.lower() not in {".mp4", ".mov", ".lrv"}:
            return jsonify({"error": "Only MP4/MOV streaming is supported"}), 400
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        response = send_file(
            path,
            mimetype=mime,
            conditional=True,
            etag=True,
            max_age=0,
            last_modified=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "no-store"
        return response

    @vo.post("/api/voiceover/save-take")
    def voiceover_save_take():
        """Upload recorded audio and rewrite that same video path in place."""
        video_path = str(request.form.get("path") or "").strip()
        root = str(request.form.get("root") or "").strip()
        narrator = str(request.form.get("narrator") or "").strip()
        mic = str(request.form.get("mic") or "").strip()
        audio = request.files.get("audio")
        if not video_path:
            return jsonify({"error": "path is required"}), 400
        if audio is None or not audio.filename:
            return jsonify({"error": "audio file is required"}), 400

        suffix = Path(audio.filename).suffix.lower() or ".webm"
        temp_audio = None
        try:
            source = Path(video_path).expanduser().resolve(strict=True)
            raw = audio.read()
            if not raw:
                return jsonify({"error": "empty audio upload"}), 400
            temp_audio = voiceover_store.save_uploaded_audio(raw, suffix=suffix)
            # Always keep a pending sidecar first so a USB lock can't lose the take.
            pending = voiceover_store.save_pending_take(source, temp_audio)
            try:
                result = voiceover_store.mux_voiceover_inplace(
                    source,
                    temp_audio,
                    root=Path(root) if root else source.parent,
                    narrator=narrator,
                    mic=mic,
                )
                return jsonify(result)
            except Exception as mux_exc:  # noqa: BLE001
                return (
                    jsonify(
                        {
                            "ok": False,
                            "pending": True,
                            "pending_path": str(pending),
                            "path": str(source),
                            "error": str(mux_exc),
                            "message": (
                                "Take saved next to the clip, but attaching failed "
                                f"(often USB file-in-use). Click Attach voiceover. ({mux_exc})"
                            ),
                        }
                    ),
                    409,
                )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        finally:
            if temp_audio is not None:
                try:
                    temp_audio.unlink(missing_ok=True)
                except OSError:
                    pass

    @vo.post("/api/voiceover/attach-pending")
    def voiceover_attach_pending():
        """Mux a saved pending take into the original clip (retry / later attach)."""
        payload = request.get_json(silent=True) or {}
        video_path = str(payload.get("path") or request.form.get("path") or "").strip()
        root = str(payload.get("root") or request.form.get("root") or "").strip()
        narrator = str(payload.get("narrator") or request.form.get("narrator") or "").strip()
        mic = str(payload.get("mic") or request.form.get("mic") or "").strip()
        if not video_path:
            return jsonify({"error": "path is required"}), 400
        try:
            source = Path(video_path).expanduser().resolve(strict=True)
            result = voiceover_store.attach_pending_take(
                source,
                root=Path(root) if root else source.parent,
                narrator=narrator,
                mic=mic,
            )
            return jsonify(result)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

    @vo.get("/api/voiceover/probe")
    def voiceover_probe():
        raw_path = str(request.args.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            media = probe_media(Path(raw_path))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "path": str(media.path),
                "duration": media.duration,
                "size_bytes": media.size_bytes,
                "has_gpmf": media.has_gpmf,
            }
        )

    @vo.post("/api/voiceover/gemini-script")
    def voiceover_gemini_script():
        """Draft narration from client PDF rules.

        By default drafts ~1 minute from the playhead. Set ``whole_clip`` true to
        draft the full video. When ``with_video`` is true (default for whole_clip),
        a small silent low-res proxy is sent — never the original 250–500MB file.
        """
        from .core.narration_guidelines import NARRATION_SYSTEM_RULES

        payload = request.get_json(silent=True) or {}
        video_path = str(payload.get("path") or "").strip()
        api_key = str(payload.get("api_key") or os.environ.get("GEMINI_API_KEY") or "").strip()
        class_name = str(payload.get("class_name") or "").strip()
        whole_clip = bool(payload.get("whole_clip"))
        with_video = payload.get("with_video")
        if with_video is None:
            with_video = whole_clip
        else:
            with_video = bool(with_video)
        try:
            start = float(payload.get("start_seconds") or 0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            window = float(payload.get("window_seconds") or 60)
        except (TypeError, ValueError):
            window = 60.0
        start = max(0.0, start)
        if not whole_clip:
            window = min(180.0, max(15.0, window))
        if not video_path:
            return jsonify({"error": "path is required"}), 400
        if not api_key:
            return jsonify(
                {"error": "Gemini API key required (paste in UI or set GEMINI_API_KEY)"}
            ), 400

        proxy: Path | None = None
        try:
            path = Path(video_path).expanduser().resolve(strict=True)
            media = probe_media(path)
            duration = float(media.duration or 0)
            if whole_clip:
                start = 0.0
                end = duration if duration > 0 else 60.0
            elif duration > 0:
                start = min(start, max(0.0, duration - 1.0))
                end = min(duration, start + window)
            else:
                end = start + window

            span = max(0.5, end - start)
            words_target = max(25, int(span * 25 / 60))
            see_note = (
                "A low-resolution silent PROXY of this time range is attached. "
                "Base the script on what you actually see. "
                if with_video
                else "No video is attached — draft carefully from the topic name only; "
                "prefer environment + plausible task steps; do not invent kneeling as "
                "first-person unless typical for the wearer's own POV. "
            )
            prompt = (
                f"{NARRATION_SYSTEM_RULES}\n\n"
                f"{see_note}\n"
                f"Video file: {path.name}\n"
                f"Class/topic folder: {class_name or path.parent.name}\n"
                f"Full clip duration seconds: {duration:.1f}\n"
                f"Draft spoken narration for {start:.1f}s → {end:.1f}s "
                f"(~{span:.0f}s, at least ~{words_target} words).\n"
                "Output plain spoken script only."
            )

            if with_video:
                # Cap very long clips: still one proxy, but keep fps lower for long spans.
                fps = 3 if span > 180 else 4
                proxy = voiceover_store.build_gemini_proxy(
                    path, start=start, end=end, max_width=640, fps=fps
                )
                script = _call_gemini_with_video(api_key, prompt, proxy)
            else:
                script = _call_gemini(api_key, prompt)

            return jsonify(
                {
                    "ok": True,
                    "script": script,
                    "path": str(path),
                    "start_seconds": start,
                    "end_seconds": end,
                    "window_seconds": span,
                    "whole_clip": whole_clip,
                    "uploads_video": bool(with_video),
                    "uploads_original": False,
                }
            )
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        finally:
            if proxy is not None:
                try:
                    proxy.unlink(missing_ok=True)
                except OSError:
                    pass

    return vo


def _gemini_model_rank(name: str) -> tuple[int, str]:
    lower = name.lower()
    # Prefer current flash models; push overloaded / retired aliases later.
    if "1.5" in lower or lower.endswith("-latest"):
        tier = 3
    elif "2.5" in lower or "3." in lower:
        tier = 0
    elif "flash" in lower:
        tier = 1
    else:
        tier = 2
    return (tier, lower)


def _gemini_model_candidates(api_key: str) -> list[str]:
    """Prefer GEMINI_MODEL, then live ListModels, then known flash ids."""
    import json
    import urllib.error
    import urllib.request

    preferred = os.environ.get("GEMINI_MODEL", "").strip()
    models: list[str] = []
    if preferred:
        models.append(preferred.removeprefix("models/"))

    # Discover what this API key can actually call.
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?key={api_key}&pageSize=100"
        )
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        discovered: list[str] = []
        for item in payload.get("models") or []:
            name = str(item.get("name") or "").removeprefix("models/")
            methods = item.get("supportedGenerationMethods") or []
            if not name or "generateContent" not in methods:
                continue
            lower = name.lower()
            if "embed" in lower or "tts" in lower or "image" in lower:
                continue
            discovered.append(name)
        for name in sorted(discovered, key=_gemini_model_rank):
            if name not in models:
                models.append(name)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        pass

    for name in (
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-flash-latest",
        "gemini-1.5-flash",
    ):
        if name not in models:
            models.append(name)
    return models


def _call_gemini_with_video(api_key: str, prompt: str, video_path: Path) -> str:
    """Upload a small proxy via Gemini Files API, then generateContent."""
    import json
    import mimetypes
    import time
    import urllib.error
    import urllib.request

    raw = video_path.read_bytes()
    mime = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    # Resumable upload (simple single-shot for small proxies).
    start_req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}",
        data=b"",
        method="POST",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(raw)),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        },
    )
    start_req.data = json.dumps({"file": {"display_name": video_path.name}}).encode("utf-8")
    try:
        with urllib.request.urlopen(start_req, timeout=60) as resp:
            upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get(
                "x-goog-upload-url"
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini file upload start failed: {detail[:300]}") from exc
    if not upload_url:
        raise RuntimeError("Gemini file upload did not return an upload URL")

    put_req = urllib.request.Request(
        upload_url,
        data=raw,
        method="POST",
        headers={
            "Content-Length": str(len(raw)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )
    try:
        with urllib.request.urlopen(put_req, timeout=180) as resp:
            file_info = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini file upload failed: {detail[:300]}") from exc

    file_name = (file_info.get("file") or file_info).get("name")
    file_uri = (file_info.get("file") or file_info).get("uri")
    if not file_uri and file_name:
        file_uri = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
    if not file_uri:
        raise RuntimeError("Gemini upload returned no file uri")

    # Wait briefly if processing.
    for _ in range(20):
        state = str((file_info.get("file") or file_info).get("state") or "").upper()
        if state in {"", "ACTIVE"}:
            break
        if state == "FAILED":
            raise RuntimeError("Gemini file processing failed")
        time.sleep(1.0)
        if not file_name:
            break
        status_req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}",
            method="GET",
        )
        try:
            with urllib.request.urlopen(status_req, timeout=30) as resp:
                file_info = {"file": json.loads(resp.read().decode("utf-8"))}
        except urllib.error.HTTPError:
            break

    models = _gemini_model_candidates(api_key)
    body_obj = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"file_data": {"mime_type": mime, "file_uri": file_uri}},
                ]
            }
        ],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 4096},
    }
    body = json.dumps(body_obj).encode("utf-8")
    errors: list[str] = []
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            errors.append(f"{model}: HTTP {exc.code}")
            if exc.code in {404, 400, 403, 429, 503}:
                continue
            raise RuntimeError(f"Gemini HTTP {exc.code} ({model}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        candidates = payload.get("candidates") or []
        if not candidates:
            errors.append(f"{model}: no candidates")
            continue
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [str(p.get("text") or "") for p in parts if p.get("text")]
        script = "\n".join(texts).strip()
        if script:
            return script
        errors.append(f"{model}: empty script")

    # Fallback to text-only if multimodal models reject the file.
    try:
        return _call_gemini(api_key, prompt + "\n(Video attachment unavailable — text draft only.)")
    except Exception as exc:  # noqa: BLE001
        detail = "; ".join(errors[:6]) if errors else str(exc)
        raise RuntimeError(f"Gemini video draft failed: {detail}") from exc


def _call_gemini(api_key: str, prompt: str) -> str:
    """Minimal REST call — no extra SDK dependency."""
    import json
    import urllib.error
    import urllib.request

    models = _gemini_model_candidates(api_key)
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
        }
    ).encode("utf-8")

    errors: list[str] = []
    for model in models:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            errors.append(f"{model}: HTTP {exc.code}")
            # Skip dead / overloaded / forbidden models and try the next one.
            if exc.code in {404, 400, 403, 429, 503}:
                continue
            raise RuntimeError(f"Gemini HTTP {exc.code} ({model}): {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        candidates = payload.get("candidates") or []
        if not candidates:
            errors.append(f"{model}: no candidates")
            continue
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [str(p.get("text") or "") for p in parts if p.get("text")]
        script = "\n".join(texts).strip()
        if script:
            return script
        errors.append(f"{model}: empty script")

    tried = ", ".join(models[:8]) + ("…" if len(models) > 8 else "")
    detail = "; ".join(errors[:6]) if errors else "no models available"
    raise RuntimeError(
        f"Gemini draft failed. Tried: {tried}. Details: {detail}"
    )
