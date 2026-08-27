"""Eager Review Station API routes."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file, send_from_directory

from .core import annotation_store, batch_registry
from .core.eager import (
    assign_clip_to_task,
    label_progress,
    list_camera_folders,
    process_reviewed_video,
    scan_mp4_files,
    task_directory,
    task_output_directory,
)
from .core.eager_trim_queue import eager_trim_queue
from .core.folder_picker import pick_folder
from .core.preview_proxy import (
    cancel_other_previews,
    cancel_preview,
    preview_cache_root,
    preview_status,
    resolve_preview,
)
from .core.skim_proxy import cancel_skim, skim_cache_root, skim_status
from .core.fast_proxy import cancel_fast, fast_cache_root, fast_status
from .core.lite_mode import performance_config
from .core.snapshot_strip import cancel_snapshots
from .core.task_store import (
    add_task,
    bundled_tasks,
    get_profile,
    load_tasks,
    remove_task,
    set_profile,
)
from .core.share_clip import prepare_share_download, take_prepared_download
from .core.trimmer import move_to_trash
from .core.work_log import append_work_session, list_work_sessions
from .core.volumes import list_sd_cards, list_volume_roots, normalize_path
from .core.scaleai_stitch import (
    discover_task_dirs,
    list_task_clips,
    plan_stitch,
    stitch_all_tasks,
    stitch_task_clips,
)


def create_eager_blueprint() -> Blueprint:
    """API-only blueprint — UI is served from gopro_cleaner/web (or Vite in dev)."""
    eager = Blueprint("eager", __name__)

    @eager.get("/api/eager/config")
    def eager_config():
        return jsonify(performance_config())

    @eager.get("/api/eager/work-log")
    def eager_work_log_list():
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        return jsonify({"sessions": list_work_sessions(limit=limit)})

    @eager.post("/api/eager/work-log")
    def eager_work_log_save():
        payload = request.get_json(silent=True) or {}
        try:
            row = append_work_session(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "session": row})

    @eager.get("/api/eager/volumes")
    def eager_volumes():
        return jsonify({"volumes": list_volume_roots()})

    @eager.get("/api/eager/sd-cards")
    def eager_sd_cards():
        try:
            cards = list_sd_cards()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500
        return jsonify({"cards": cards, "count": len(cards)})

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
        return jsonify(
            {
                "tasks": load_tasks(),
                "default_tasks": bundled_tasks(),
                "profile": get_profile(),
            }
        )

    @eager.post("/api/eager/tasks/profile")
    def eager_tasks_profile():
        """Switch task list: default (textile) or scaleai (empty, add live)."""
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("profile") or payload.get("name") or "").strip()
        try:
            profile = set_profile(name)
            tasks = load_tasks()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "ok": True,
                "profile": profile,
                "tasks": tasks,
                "default_tasks": bundled_tasks(),
            }
        )

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
        return jsonify(
            {
                "tasks": tasks,
                "default_tasks": bundled_tasks(),
                "profile": get_profile(),
                "task_dir": str(task_dir) if task_dir else None,
            }
        )

    @eager.delete("/api/eager/tasks")
    def eager_remove_task():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        try:
            tasks = remove_task(name)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "tasks": tasks,
                "default_tasks": bundled_tasks(),
                "profile": get_profile(),
            }
        )

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
        if mode not in {"all", "raw", "clips", "label", "annotate"}:
            return jsonify({"error": "mode must be all, raw, clips, label, or annotate"}), 400
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            root = normalize_path(raw_path)
            videos = scan_mp4_files(root, recursive=recursive, mode=mode)
            progress = label_progress(root, recursive=recursive) if mode == "label" else None
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        payload_out = {"root": str(root), "count": len(videos), "videos": videos, "mode": mode}
        if progress is not None:
            payload_out["progress"] = progress
        return jsonify(payload_out)

    @eager.get("/api/eager/label-progress")
    def eager_label_progress():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        recursive = request.args.get("recursive", "1").strip().lower() not in {"0", "false", "no"}
        try:
            root = normalize_path(raw_path)
            return jsonify(label_progress(root, recursive=recursive))
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

    @eager.get("/api/eager/preview/status")
    def eager_preview_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        start = request.args.get("start", "0").strip().lower() in {"1", "true", "yes"}
        # Foreground = video currently being reviewed — cancels other encodes.
        preempt = request.args.get("preempt", "0").strip().lower() in {"1", "true", "yes"}
        try:
            return jsonify(preview_status(Path(raw_path), start=start, preempt=preempt))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "error": str(exc)}), 200

    @eager.post("/api/eager/preview/cancel")
    def eager_preview_cancel():
        payload = request.get_json(silent=True) or {}
        raw_path = request.args.get("path", "").strip() or str(payload.get("path", "")).strip()
        if payload.get("others"):
            cancelled = cancel_other_previews(Path(raw_path) if raw_path else None)
            return jsonify({"ok": True, "cancelled": cancelled})
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        cancel_preview(Path(raw_path))
        return jsonify({"ok": True})

    @eager.get("/api/eager/fast/status")
    def eager_fast_status():
        """Best zero-encode review source (LRV proxy / SSD copy / original)."""
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        start = request.args.get("start", "0").strip().lower() in {"1", "true", "yes"}
        try:
            return jsonify(fast_status(Path(raw_path), start=start))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "error": str(exc), "ready": False}), 200

    @eager.post("/api/eager/fast/cancel")
    def eager_fast_cancel():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            payload = request.get_json(silent=True) or {}
            raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        cancel_fast(Path(raw_path))
        return jsonify({"ok": True})

    @eager.get("/api/eager/skim/status")
    def eager_skim_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        start = request.args.get("start", "0").strip().lower() in {"1", "true", "yes"}
        try:
            return jsonify(skim_status(Path(raw_path), start=start))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "error": str(exc), "ready": False}), 200

    @eager.post("/api/eager/skim/cancel")
    def eager_skim_cancel():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            payload = request.get_json(silent=True) or {}
            raw_path = str(payload.get("path", "")).strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        cancel_skim(Path(raw_path))
        return jsonify({"ok": True})

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
        # .LRV is GoPro's own low-res proxy — an MP4 container the player can
        # stream as-is for smooth high-rate skim (no transcode anywhere).
        if path.suffix.upper() not in {".MP4", ".LRV"}:
            return jsonify({"error": "Only MP4/LRV streaming is supported"}), 400
        mime = "video/mp4" if path.suffix.upper() == ".LRV" else (
            mimetypes.guess_type(path.name)[0] or "video/mp4"
        )
        # conditional=True enables HTTP Range / 206 responses so the browser can
        # seek large originals without downloading the whole 4–8 GB file.
        response = send_file(
            path,
            mimetype=mime,
            conditional=True,
            etag=True,
            max_age=0,
            last_modified=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @eager.get("/api/eager/preview")
    def eager_preview():
        """Legacy endpoint — previews are HLS now; point callers at the playlist."""
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            resolve_preview(Path(raw_path))
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        st = preview_status(Path(raw_path), start=False)
        return jsonify({"ok": True, "hls": st.get("hls"), "status": st.get("status")})

    _HLS_KEY_RE = re.compile(r"^[0-9a-f]{20}$")
    _HLS_NAME_RE = re.compile(r"^(index\.m3u8|seg\d{5}\.ts)$")
    _SKIM_NAME_RE = re.compile(r"^skim_5x\.mp4$")
    _FAST_NAME_RE = re.compile(r"^(proxy|original)\.mp4$")

    @eager.get("/api/eager/fast/<key>/<name>")
    def eager_fast_file(key: str, name: str):
        """Serve SSD-cached review media (LRV proxy or original copy) with Range."""
        if not _HLS_KEY_RE.fullmatch(key) or not _FAST_NAME_RE.fullmatch(name):
            return jsonify({"error": "Not found"}), 404
        path = fast_cache_root() / key / name
        if not path.is_file():
            return jsonify({"error": "Not found"}), 404
        response = send_file(
            path,
            mimetype="video/mp4",
            conditional=True,
            etag=True,
            max_age=86400,
            last_modified=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
        return response

    @eager.get("/api/eager/preview/hls/<key>/<name>")
    def eager_preview_hls(key: str, name: str):
        """Serve preview playlist/segments — playable while the encode runs."""
        if not _HLS_KEY_RE.fullmatch(key) or not _HLS_NAME_RE.fullmatch(name):
            return jsonify({"error": "Not found"}), 404
        directory = preview_cache_root() / key
        if not (directory / name).is_file():
            return jsonify({"error": "Not found"}), 404
        if name.endswith(".m3u8"):
            # The playlist grows during the build — the player must re-fetch it.
            response = send_from_directory(
                directory, name, mimetype="application/vnd.apple.mpegurl", max_age=0
            )
            response.headers["Cache-Control"] = "no-store"
        else:
            # Segments are immutable once written (hls_flags temp_file).
            response = send_from_directory(directory, name, mimetype="video/mp2t", max_age=86400)
        return response

    @eager.get("/api/eager/skim/<key>/<name>")
    def eager_skim_file(key: str, name: str):
        """Serve baked 5× skim MP4 with Range support for seeks."""
        if not _HLS_KEY_RE.fullmatch(key) or not _SKIM_NAME_RE.fullmatch(name):
            return jsonify({"error": "Not found"}), 404
        path = skim_cache_root() / key / name
        if not path.is_file():
            return jsonify({"error": "Not found"}), 404
        response = send_file(
            path,
            mimetype="video/mp4",
            conditional=True,
            etag=True,
            max_age=86400,
            last_modified=True,
        )
        response.headers["Accept-Ranges"] = "bytes"
        return response

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

    @eager.post("/api/eager/share-clip")
    def eager_share_clip_prepare():
        """Encode a share clip and return a short-lived GET download URL.

        Body: {"path": "...mp4", "start": 12.5, "end": 28.0, "quality": "1080p"}

        Two-step flow so download managers (IDM) can re-GET the same URL. POST
        only returns JSON; the MP4 is served by GET /api/eager/share-clip/<token>.
        """
        payload = request.get_json(silent=True) or {}
        raw_source = str(payload.get("path", "")).strip()
        quality = str(payload.get("quality") or "1080p").strip()
        try:
            start = float(payload.get("start", 0))
            end = float(payload.get("end", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "start and end must be numbers"}), 400
        if not raw_source:
            return jsonify({"error": "path is required"}), 400

        try:
            source = Path(raw_source).expanduser().resolve(strict=True)
            prepared = prepare_share_download(source, start, end, quality=quality)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

        return jsonify({"ok": True, **prepared})

    @eager.get("/api/eager/share-clip/<token>")
    def eager_share_clip_download(token: str):
        """Serve a previously encoded share clip (IDM-safe; may be fetched twice)."""
        try:
            path, filename = take_prepared_download(token)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404

        response = send_file(
            path,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=filename,
            max_age=0,
            conditional=True,
        )
        # Help cross-origin download managers / browser saves from Vite origin.
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
        response.headers["Cache-Control"] = "no-store"
        return response

    @eager.post("/api/eager/trim/queue-work")
    def eager_trim_queue_work():
        """Queue every annotated work segment for the given videos.

        Body: {"paths": ["...mp4", ...], "delete_source": false}.
        Segments already queued/running/completed for the same range are skipped.
        When ``delete_source`` is true, each source is moved to Trash after its
        trims finish successfully (never while jobs are still active/cancelled).
        """
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths") or []
        delete_source = bool(payload.get("delete_source", False))
        if not isinstance(raw_paths, list) or not raw_paths:
            return jsonify({"error": "paths (non-empty list) is required"}), 400

        queued = 0
        skipped = 0
        files: list[dict] = []
        errors: list[str] = []
        finish_paths: list[Path] = []
        for raw in raw_paths:
            source = Path(str(raw)).expanduser()
            try:
                source = source.resolve(strict=True)
            except FileNotFoundError:
                errors.append(f"Not found: {raw}")
                continue
            annotation = annotation_store.load_annotation(source)
            segments = [
                s for s in (annotation or {}).get("segments") or []
                if str(s.get("kind") or "").lower() == "work"
            ]
            if not segments:
                continue
            file_queued = 0
            file_skipped = 0
            for seg in segments:
                try:
                    start = float(seg.get("start", 0))
                    end = float(seg.get("end", 0))
                except (TypeError, ValueError):
                    continue
                if end <= start + 0.05:
                    continue
                task_name = str(seg.get("task") or "").strip()
                if not task_name:
                    errors.append(f"{source.name}: work segment missing task name")
                    continue
                if eager_trim_queue.has_equivalent_job(source, start, end):
                    skipped += 1
                    file_skipped += 1
                    continue
                try:
                    # Save under DCIM/###GOPRO/{task-name}/ — one folder per task.
                    output_dir = task_output_directory(source, task_name)
                    eager_trim_queue.submit(
                        source,
                        start,
                        end,
                        output_dir=output_dir,
                        task=task_name,
                    )
                    queued += 1
                    file_queued += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{source.name}: {exc}")
            if file_queued:
                files.append({"path": str(source), "name": source.name, "queued": file_queued})
            if delete_source and (file_queued or file_skipped):
                finish_paths.append(source)

        deleted_now = 0
        finish_scheduled = 0
        for source in finish_paths:
            try:
                result = eager_trim_queue.schedule_source_finish(source, delete_source=True)
                if result.get("deleted_source"):
                    deleted_now += 1
                elif result.get("scheduled"):
                    finish_scheduled += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source.name}: finish/delete — {exc}")

        return jsonify(
            {
                "ok": True,
                "queued": queued,
                "skipped": skipped,
                "delete_source": delete_source,
                "deleted_now": deleted_now,
                "finish_scheduled": finish_scheduled,
                "files": files,
                "errors": errors,
            }
        )

    @eager.post("/api/eager/snippet")
    def eager_snippet():
        """Save the marked I→O range as a task-folder sample (any length)."""
        payload = request.get_json(silent=True) or {}
        raw_source = str(payload.get("path", "")).strip()
        raw_root = str(payload.get("label_root", "")).strip()
        task_name = str(payload.get("task", "")).strip()
        try:
            start = float(payload.get("start", 0))
            end = float(payload.get("end", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "start and end must be numbers"}), 400
        if not raw_source or not raw_root or not task_name:
            return jsonify({"error": "path, label_root, and task are required"}), 400
        if end <= start + 0.05:
            return jsonify({"error": "Snippet end must be after start"}), 400

        try:
            source = Path(raw_source).expanduser().resolve(strict=True)
            root = Path(raw_root).expanduser().resolve(strict=True)
            add_task(task_name)
            task_dir = task_directory(root, task_name)
            record = eager_trim_queue.submit(
                source,
                start,
                end,
                output_dir=task_dir,
                kind="snippet",
                task=task_name,
            )
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
                "duration_seconds": record.end_seconds - record.start_seconds,
                "output": record.output,
                "task": task_name,
                "task_dir": str(task_dir),
                "source_has_gpmf": record.source_has_gpmf,
            }
        )

    @eager.get("/api/eager/trim/status")
    def eager_trim_status():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        return jsonify(eager_trim_queue.status_for_source(Path(raw_path)))

    @eager.get("/api/eager/trim/active")
    def eager_trim_active():
        """Global trim queue snapshot (survives clean↔label switch and reload)."""
        return jsonify(eager_trim_queue.status_all())

    @eager.post("/api/eager/trim/cancel")
    def eager_trim_cancel():
        payload = request.get_json(silent=True) or {}
        job_id = str(payload.get("job_id", "")).strip()
        if not job_id:
            return jsonify({"error": "job_id is required"}), 400
        cancelled = eager_trim_queue.cancel_job(job_id)
        return jsonify({"ok": True, "cancelled": cancelled, **eager_trim_queue.status_all()})

    @eager.post("/api/eager/trim/cancel-all")
    def eager_trim_cancel_all():
        count = eager_trim_queue.cancel_all()
        return jsonify({"ok": True, "cancelled_count": count, **eager_trim_queue.status_all()})

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

    @eager.post("/api/eager/video/delete")
    def eager_delete_video():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        batch_id = str(payload.get("batch_id") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        if not payload.get("confirmed"):
            return jsonify({"error": "Deletion must be confirmed"}), 400

        try:
            source = Path(raw_path).expanduser().resolve(strict=True)
            cancel_preview(source)
            cancel_skim(source)
            cancel_fast(source)
            cancel_snapshots(source)
            move_to_trash(source)

            sidecar = annotation_store.sidecar_path_for(source)
            text_sidecar = sidecar.with_suffix("").with_suffix(".segments.txt")
            for companion in (sidecar, text_sidecar):
                if companion.is_file():
                    move_to_trash(companion)

            batch = batch_registry.remove_asset(batch_id, str(source)) if batch_id else None
        except FileNotFoundError:
            return jsonify({"error": "Video not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "ok": True,
                "message": f"Moved to Trash: {source.name}",
                "batch": batch,
            }
        )

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
        # Raw footage is never deleted after trimming, regardless of payload.
        delete_source = False
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

    # ---- Batch annotation (sidecar timestamps, no live trim) ----------------

    @eager.get("/api/eager/batches")
    def eager_batches():
        return jsonify({"batches": batch_registry.list_batches()})

    @eager.post("/api/eager/batches/import-csv")
    def eager_batches_import_csv():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("csv") or payload.get("text") or "")
        if not text.strip() and request.files.get("file"):
            text = request.files["file"].read().decode("utf-8-sig", errors="replace")
        if not text.strip():
            return jsonify({"error": "csv text is required"}), 400
        try:
            if bool(payload.get("preview_only")):
                return jsonify({"ok": True, "preview": batch_registry.parse_batch_csv(text)})
            detail = batch_registry.create_batch_from_csv(text)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "batch": detail})

    @eager.get("/api/eager/batches/<batch_id>")
    def eager_batch_detail(batch_id: str):
        try:
            detail = batch_registry.sync_asset_annotations(batch_id)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "batch": detail})

    @eager.get("/api/eager/batches/<batch_id>/match-cards")
    def eager_batch_match_cards(batch_id: str):
        try:
            result = batch_registry.match_detected_cards(batch_id)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/batches/<batch_id>/bind-card")
    def eager_batch_bind_card(batch_id: str):
        payload = request.get_json(silent=True) or {}
        card_badge = str(payload.get("card_badge") or "").strip()
        mount_path = str(payload.get("mount_path") or payload.get("path") or "").strip()
        scan_path = str(payload.get("scan_path") or mount_path).strip()
        if not card_badge or not scan_path:
            return jsonify({"error": "card_badge and scan_path are required"}), 400
        try:
            root = normalize_path(scan_path)
            videos = scan_mp4_files(root, recursive=True, mode="annotate")
            detail = batch_registry.bind_card(
                batch_id,
                card_badge=card_badge,
                mount_path=mount_path or str(root),
                scan_path=str(root),
                videos=videos,
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "batch": detail, "videos": videos, "count": len(videos)})

    @eager.post("/api/eager/batches/<batch_id>/finish-card")
    def eager_batch_finish_card(batch_id: str):
        payload = request.get_json(silent=True) or {}
        card_badge = str(payload.get("card_badge") or "").strip()
        if not card_badge:
            return jsonify({"error": "card_badge is required"}), 400
        try:
            detail = batch_registry.finish_card(batch_id, card_badge)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "batch": detail})

    @eager.post("/api/eager/batches/<batch_id>/complete")
    def eager_batch_complete(batch_id: str):
        try:
            detail = batch_registry.complete_batch(batch_id)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "batch": detail})

    @eager.get("/api/eager/batches/<batch_id>/report.json")
    def eager_batch_report_json(batch_id: str):
        try:
            detail = batch_registry.sync_asset_annotations(batch_id)
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify(detail.get("report") or {})

    @eager.get("/api/eager/batches/<batch_id>/report.csv")
    def eager_batch_report_csv(batch_id: str):
        data = batch_registry.get_batch(batch_id)
        if not data:
            return jsonify({"error": "Batch not found"}), 404
        batch_registry.sync_asset_annotations(batch_id)
        data = batch_registry.get_batch(batch_id) or data
        csv_text = batch_registry.report_csv(data)
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{data.get("batch_name", batch_id)}-report.csv"'
            },
        )

    @eager.get("/api/eager/annotations")
    def eager_annotations_get():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        path = Path(raw_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        annotation = annotation_store.load_annotation(path)
        if annotation is None:
            annotation = annotation_store.empty_annotation(source=path.name)
        # Always resolve duration from the source MP4 — heals sidecars that
        # stored a short HLS/player duration while the preview was encoding.
        try:
            true_dur = annotation_store.resolve_media_duration(path, annotation.get("duration"))
            if true_dur is not None:
                prev = annotation.get("duration")
                annotation["duration"] = true_dur
                # Rewrite sidecar when we heal a meaningfully short duration.
                if prev is None or float(prev or 0) + 0.5 < true_dur:
                    try:
                        annotation_store.save_annotation(path, annotation)
                        annotation = annotation_store.load_annotation(path) or annotation
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        # Camera / IMU metadata: cached per file, shown in the UI even before
        # the first segment is saved to the sidecar.
        if not annotation.get("media_meta"):
            try:
                from .core.gopro_meta import get_media_meta

                annotation["media_meta"] = get_media_meta(path)
            except Exception:  # noqa: BLE001
                annotation["media_meta"] = {}
        summary = annotation_store.coverage_summary(annotation)
        return jsonify({"ok": True, "annotation": annotation, "summary": summary, "path": str(path)})

    @eager.post("/api/eager/media-meta/recorded-at")
    def eager_media_meta_recorded_at():
        """Manually set (or clear with empty value) the recording timestamp."""
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        raw_ts = str(payload.get("recorded_at") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404

        value = ""
        if raw_ts:
            from datetime import datetime

            try:
                value = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).isoformat()
            except ValueError:
                return jsonify({"error": "recorded_at must be an ISO date-time"}), 400

        annotation = annotation_store.load_annotation(path) or annotation_store.empty_annotation(
            source=path.name
        )
        meta = dict(annotation.get("media_meta") or {})
        # Empty string is an explicit clear — save_annotation treats key
        # presence as authoritative over the stored sidecar value.
        meta["recorded_at_manual"] = value
        annotation["media_meta"] = meta
        try:
            result = annotation_store.save_annotation(path, annotation)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/annotations")
    def eager_annotations_save():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            result = annotation_store.save_annotation(
                Path(raw_path),
                payload.get("annotation") or payload,
                require_complete=bool(payload.get("require_complete")),
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/annotations/append")
    def eager_annotations_append():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        task = str(payload.get("task") or "").strip()
        try:
            end = float(payload.get("end", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "end must be a number"}), 400
        if not raw_path or not kind:
            return jsonify({"error": "path and kind are required"}), 400
        context = {
            "batch_name": payload.get("batch_name"),
            "factory": payload.get("factory"),
            "card_badge": payload.get("card_badge"),
            "device_type": payload.get("device_type"),
            "device_id": payload.get("device_id"),
            "duration": payload.get("duration"),
        }
        try:
            result = annotation_store.append_segment(
                Path(raw_path),
                kind=kind,
                end=end,
                task=task,
                context=context,
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/annotations/undo")
    def eager_annotations_undo():
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            result = annotation_store.undo_last_segment(Path(raw_path))
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @eager.post("/api/eager/annotations/delete-segment")
    def eager_annotations_delete_segment():
        """Delete a segment and all segments after it (contiguous timeline)."""
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        segment_id = str(payload.get("segment_id") or payload.get("id") or "").strip() or None
        index = payload.get("index")
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            result = annotation_store.delete_segment(
                Path(raw_path),
                segment_id=segment_id,
                index=None if index is None else int(index),
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    # ---- ScaleAI micro-task stitch export ----------------------------------

    @eager.post("/api/eager/scaleai/process-video")
    def eager_scaleai_process_video():
        """Queue trims for one annotated video; optionally stitch task folders after.

        Body: {"path": "...mp4", "stitch": false, "overwrite": false, "delete_source": false}

        Label PCs should use stitch=false (JSON already saved). Strong PCs can set
        stitch=true to cut clips then build ``*__stitched.MP4`` per micro-task.
        """
        payload = request.get_json(silent=True) or {}
        raw_path = str(payload.get("path") or "").strip()
        do_stitch = bool(payload.get("stitch", False))
        overwrite = bool(payload.get("overwrite", False))
        delete_source = bool(payload.get("delete_source", False))
        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        try:
            source = Path(raw_path).expanduser().resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": f"Not found: {raw_path}"}), 404

        annotation = annotation_store.load_annotation(source)
        segments = [
            s
            for s in (annotation or {}).get("segments") or []
            if str(s.get("kind") or "").lower() == "work"
        ]
        if not segments:
            return jsonify({"error": "No work segments on this video — mark tasks first"}), 400

        queued = 0
        skipped = 0
        errors: list[str] = []
        task_dirs: set[Path] = set()
        for seg in segments:
            try:
                start = float(seg.get("start", 0))
                end = float(seg.get("end", 0))
            except (TypeError, ValueError):
                continue
            if end <= start + 0.05:
                continue
            task_name = str(seg.get("task") or "").strip()
            if not task_name:
                errors.append("work segment missing task name")
                continue
            if eager_trim_queue.has_equivalent_job(source, start, end):
                skipped += 1
                task_dirs.add(task_output_directory(source, task_name))
                continue
            try:
                output_dir = task_output_directory(source, task_name)
                eager_trim_queue.submit(
                    source,
                    start,
                    end,
                    output_dir=output_dir,
                    task=task_name,
                )
                task_dirs.add(output_dir)
                queued += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        if delete_source and (queued or skipped):
            try:
                eager_trim_queue.schedule_source_finish(source, delete_source=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"finish/delete — {exc}")

        stitch_scheduled = False
        if do_stitch and task_dirs:

            def _wait_and_stitch(dirs: list[Path], src: Path) -> None:
                import time

                # Wait until this source has no active trim jobs (max ~2h).
                for _ in range(7200):
                    if eager_trim_queue.active_count_for_source(src) == 0:
                        break
                    time.sleep(1)
                for task_dir in dirs:
                    try:
                        stitch_task_clips(task_dir, overwrite=overwrite)
                    except Exception:  # noqa: BLE001
                        pass

            import threading

            threading.Thread(
                target=_wait_and_stitch,
                args=(list(task_dirs), source),
                daemon=True,
                name="scaleai-stitch-after-trim",
            ).start()
            stitch_scheduled = True

        return jsonify(
            {
                "ok": True,
                "path": str(source),
                "queued": queued,
                "skipped": skipped,
                "errors": errors,
                "task_dirs": [str(p) for p in sorted(task_dirs)],
                "stitch_scheduled": stitch_scheduled,
                "message": (
                    f"Queued {queued} trim(s)"
                    + ("; stitch will run when trims finish" if stitch_scheduled else "")
                    + ("; JSON-only marks are already saved" if queued == 0 and skipped else "")
                ),
            }
        )

    @eager.post("/api/eager/scaleai/stitch-task")
    def eager_scaleai_stitch_task():
        """Concatenate all clips in one task folder into a GPMF-preserving MP4."""
        payload = request.get_json(silent=True) or {}
        raw_dir = str(payload.get("task_dir") or payload.get("path") or "").strip()
        task_name = str(payload.get("task") or "").strip() or None
        overwrite = bool(payload.get("overwrite", False))
        if not raw_dir:
            return jsonify({"error": "task_dir is required"}), 400
        try:
            result = stitch_task_clips(
                Path(raw_dir),
                task_name=task_name,
                overwrite=overwrite,
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except FileExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        status = 200 if result.ok else 400
        return jsonify(
            {
                "ok": result.ok,
                "task": result.task,
                "output": result.output,
                "clip_count": result.clip_count,
                "duration": result.duration,
                "has_gpmf": result.has_gpmf,
                "message": result.message,
                "error": result.error,
                "manifest": result.manifest,
            }
        ), status

    @eager.post("/api/eager/scaleai/stitch-all")
    def eager_scaleai_stitch_all():
        """Stitch every task folder under a footage root (ScaleAI delivery pack)."""
        payload = request.get_json(silent=True) or {}
        raw_root = str(payload.get("root") or payload.get("path") or "").strip()
        overwrite = bool(payload.get("overwrite", False))
        if not raw_root:
            return jsonify({"error": "root is required"}), 400
        try:
            root = normalize_path(raw_root)
            results = stitch_all_tasks(root, overwrite=overwrite)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        ok_count = sum(1 for r in results if r.ok)
        return jsonify(
            {
                "ok": ok_count == len(results) and len(results) > 0,
                "root": str(root),
                "task_count": len(results),
                "ok_count": ok_count,
                "results": [
                    {
                        "ok": r.ok,
                        "task": r.task,
                        "output": r.output,
                        "clip_count": r.clip_count,
                        "duration": r.duration,
                        "has_gpmf": r.has_gpmf,
                        "message": r.message,
                        "error": r.error,
                    }
                    for r in results
                ],
            }
        )

    @eager.get("/api/eager/scaleai/preview")
    def eager_scaleai_preview():
        """List task folders + clip counts under a root before stitching."""
        raw_root = str(request.args.get("root") or request.args.get("path") or "").strip()
        if not raw_root:
            return jsonify({"error": "root is required"}), 400
        try:
            root = normalize_path(raw_root)
            dirs = discover_task_dirs(root)
            tasks = []
            for task_dir in dirs:
                clips = list_task_clips(task_dir)
                try:
                    plan = plan_stitch(task_dir)
                    tasks.append(
                        {
                            "task": plan.task,
                            "task_dir": str(task_dir),
                            "clip_count": plan.clip_count,
                            "total_duration": plan.total_duration,
                            "all_have_gpmf": plan.all_have_gpmf,
                            "output": str(plan.output),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    tasks.append(
                        {
                            "task": task_dir.name,
                            "task_dir": str(task_dir),
                            "clip_count": len(clips),
                            "error": str(exc),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "root": str(root), "tasks": tasks})

    return eager
