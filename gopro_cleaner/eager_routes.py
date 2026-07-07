"""Eager Review Station API routes."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

from .core.eager import (
    assign_clip_to_task,
    list_camera_folders,
    process_reviewed_video,
    scan_mp4_files,
    task_directory,
)
from .core.eager_trim_queue import eager_trim_queue
from .core.folder_picker import pick_folder
from .core.preview_proxy import cancel_preview, preview_status, resolve_preview
from .core.snapshot_strip import cancel_snapshots, resolve_snapshot_frame, snapshot_plan, snapshot_status
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

    @eager.post("/api/eager/pick-folder")
    def eager_pick_folder():
        initial_raw = str(request.args.get("initial", "")).strip()
        initial = None
        if initial_raw:
            try:
                initial = normalize_path(initial_raw)
            except (OSError, RuntimeError):
                initial = None
        try:
            chosen = pick_folder(initial)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        if chosen is None:
            return jsonify({"ok": True, "cancelled": True})
        return jsonify({"ok": True, "path": str(chosen), "cancelled": False})

    @eager.get("/api/eager/tasks")
    def eager_tasks():
        return jsonify({"tasks": load_tasks()})

    @eager.post("/api/eager/tasks")
    def eager_add_task():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        raw_root = str(payload.get("label_root", "")).strip()
        try:
            tasks = add_task(name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        task_dir = None
        if raw_root and name:
            root = normalize_path(raw_root)
            task_dir = task_directory(root, name)
            task_dir.mkdir(parents=True, exist_ok=True)
        return jsonify({"tasks": tasks, "task_dir": str(task_dir) if task_dir else None})

    @eager.get("/api/eager/cameras")
    def eager_cameras():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            root = normalize_path(raw_path)
            cameras = list_camera_folders(root)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"root": str(root), "cameras": cameras})

    @eager.post("/api/eager/scan")
    def eager_scan():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path", "")).strip()
        recursive = bool(payload.get("recursive", True))
        mode = str(payload.get("mode", "all")).strip().lower()
        if mode not in {"all", "raw", "clips", "label"}:
            return jsonify({"error": "mode must be all, raw, clips, or label"}), 400
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            root = normalize_path(raw_path)
            videos = scan_mp4_files(root, recursive=recursive, mode=mode)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"root": str(root), "count": len(videos), "videos": videos, "mode": mode})

    @eager.get("/api/eager/preview/status")
    def eager_preview_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        start = request.args.get("start", "0").strip().lower() in {"1", "true", "yes"}
        try:
            return jsonify(preview_status(Path(raw_path), start=start))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

    @eager.post("/api/eager/preview/cancel")
    def eager_preview_cancel():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            payload = request.get_json(silent=True) or {}
            raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        cancel_preview(Path(raw_path))
        return jsonify({"ok": True})

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

    @eager.post("/api/eager/trim")
    def eager_trim():
        payload = request.get_json(silent=True) or {}
        raw_source = str(payload.get("path", "")).strip()
        try:
            start = float(payload.get("start", 0))
            end = float(payload.get("end", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "start and end must be numbers"}), 400
        if not raw_source:
            return jsonify({"error": "path is required"}), 400
        try:
            record = eager_trim_queue.submit(Path(raw_source), start, end)
        except FileExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "job_id": record.job_id,
                "status": record.status,
                "start_seconds": record.start_seconds,
                "end_seconds": record.end_seconds,
                "source_has_gpmf": record.source_has_gpmf,
            }
        )

    @eager.get("/api/eager/trim/status")
    def eager_trim_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        return jsonify(eager_trim_queue.status_for_source(Path(raw_path)))

    @eager.post("/api/eager/clean")
    def eager_clean():
        payload = request.get_json(silent=True) or {}
        raw_source = str(payload.get("path", "")).strip()
        if not raw_source:
            return jsonify({"error": "path is required"}), 400
        try:
            result = eager_trim_queue.schedule_source_finish(Path(raw_source))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/label")
    def eager_label():
        payload = request.get_json(silent=True) or {}
        raw_clip = str(payload.get("path", "")).strip()
        raw_root = str(payload.get("label_root", "")).strip()
        task_name = str(payload.get("task", "")).strip()
        if not raw_clip or not raw_root or not task_name:
            return jsonify({"error": "path, label_root, and task are required"}), 400
        try:
            result = assign_clip_to_task(Path(raw_clip), Path(raw_root), task_name)
        except FileExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

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

    @eager.get("/api/eager/snapshots/plan")
    def eager_snapshots_plan():
        raw_path = request.args.get("path", "").strip()
        purpose = request.args.get("purpose", "clean")
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            return jsonify(snapshot_plan(Path(raw_path), purpose=purpose))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

    @eager.get("/api/eager/snapshots/status")
    def eager_snapshots_status():
        raw_path = request.args.get("path", "").strip()
        purpose = request.args.get("purpose", "clean")
        priority = request.args.get("priority")
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        start = request.args.get("start", "0").strip().lower() in {"1", "true", "yes"}
        try:
            return jsonify(
                snapshot_status(Path(raw_path), start=start, purpose=purpose, priority=priority)
            )
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

    @eager.post("/api/eager/snapshots/cancel")
    def eager_snapshots_cancel():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path", request.args.get("path", ""))).strip()
        purpose_raw = str(payload.get("purpose", request.args.get("purpose", ""))).strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        purpose = purpose_raw if purpose_raw else None
        cancel_snapshots(Path(raw_path), purpose=purpose)
        return jsonify({"ok": True})

    @eager.get("/api/eager/snapshots/frame")
    def eager_snapshots_frame():
        raw_path = request.args.get("path", "").strip()
        purpose = request.args.get("purpose", "clean")
        try:
            index = int(request.args.get("index", "0"))
        except ValueError:
            return jsonify({"error": "index must be an integer"}), 400
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            path = resolve_snapshot_frame(Path(raw_path), index, purpose=purpose)
        except FileNotFoundError:
            return jsonify({"error": "Frame not found"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return send_file(path, mimetype="image/jpeg", conditional=True)

    return eager
