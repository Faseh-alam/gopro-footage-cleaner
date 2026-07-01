"""Eager Review Station API routes."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from .core.eager import process_reviewed_video, scan_mp4_files
from .core.preview_proxy import preview_status, resolve_preview
from .core.task_store import add_task, load_tasks
from .core.volumes import list_volume_roots, normalize_path


def create_eager_blueprint(template_folder: str, version: str = "1.0.0") -> Blueprint:
    eager = Blueprint("eager", __name__, template_folder=template_folder)

    @eager.get("/review")
    def review_page():
        return render_template("eager.html", version=version)

    @eager.get("/api/eager/volumes")
    def eager_volumes():
        return jsonify({"volumes": list_volume_roots()})

    @eager.get("/api/eager/tasks")
    def eager_tasks():
        return jsonify({"tasks": load_tasks()})

    @eager.post("/api/eager/tasks")
    def eager_add_task():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        try:
            tasks = add_task(name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"tasks": tasks})

    @eager.post("/api/eager/scan")
    def eager_scan():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path", "")).strip()
        recursive = bool(payload.get("recursive", True))
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            root = normalize_path(raw_path)
            videos = scan_mp4_files(root, recursive=recursive)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"root": str(root), "count": len(videos), "videos": videos})

    @eager.get("/api/eager/preview/status")
    def eager_preview_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            return jsonify(preview_status(Path(raw_path)))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

    @eager.get("/api/eager/preview")
    def eager_preview():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            path = resolve_preview(Path(raw_path))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return send_file(path, mimetype="video/mp4", conditional=True)

    @eager.get("/api/eager/stream")
    def eager_stream():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        path = Path(raw_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        if path.suffix.upper() != ".MP4":
            return jsonify({"error": "Only MP4 streaming is supported"}), 400
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        return send_file(path, mimetype=mime, conditional=True)

    @eager.post("/api/eager/finish")
    def eager_finish():
        payload = request.get_json(silent=True) or {}
        raw_source = str(payload.get("path", "")).strip()
        raw_output = str(payload.get("output_root", "")).strip()
        task_name = str(payload.get("task", "")).strip()
        keep_entire = bool(payload.get("keep_entire"))
        delete_source = bool(payload.get("delete_source", True))
        clips_raw = payload.get("clips") or []

        if not raw_source or not raw_output or not task_name:
            return jsonify({"error": "path, output_root, and task are required"}), 400

        clips: list[tuple[float, float]] = []
        for item in clips_raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return jsonify({"error": "Each clip must be [start_seconds, end_seconds]"}), 400
            start = float(item[0])
            end = float(item[1])
            if end <= start:
                return jsonify({"error": "Clip end must be after start"}), 400
            clips.append((start, end))

        try:
            result = process_reviewed_video(
                Path(raw_source),
                Path(raw_output),
                task_name,
                keep_entire=keep_entire,
                clips=clips,
                delete_source=delete_source,
            )
        except FileExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

        return jsonify({"ok": True, **result})

    return eager
