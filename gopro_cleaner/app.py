"""Local web app for trimming GoPro footage."""

from __future__ import annotations

import os
import re
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from .eager_routes import create_eager_blueprint
from .core.probe import MediaInfo, is_video_file, looks_like_gopro, probe_media
from .core.queue import trim_queue
from .core.sheet_import import parse_sheet, preview_to_dict, queue_import
from .core.timestamps import format_timestamp, parse_clip_lines
from .core.trimmer import job_store, move_to_trash
from .core.volumes import list_volume_roots

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
APP_VERSION = "2.10.0"


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(APP_ROOT / "templates"),
        static_folder=str(APP_ROOT / "static"),
    )

    app.register_blueprint(create_eager_blueprint(str(APP_ROOT / "templates"), APP_VERSION))

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "version": APP_VERSION})

    @app.get("/api/volumes")
    def volumes():
        return jsonify({"volumes": list_volume_roots()})

    @app.get("/api/browse")
    def browse():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "Missing path"}), 400

        path = Path(raw_path).expanduser()
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError:
            return jsonify({"error": "Path not found"}), 404

        if not path.is_dir():
            return jsonify({"error": "Not a directory"}), 400

        parent = str(path.parent) if path.parent != path else None
        entries = []
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403

        for child in children:
            if child.name.startswith("."):
                continue
            item = {
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            }
            if child.is_file() and is_video_file(child):
                item["is_video"] = True
                item["is_gopro"] = looks_like_gopro(child)
                item["size_bytes"] = child.stat().st_size
            entries.append(item)

        return jsonify({"path": str(path), "parent": parent, "entries": entries})

    @app.get("/api/probe")
    def probe():
        raw_path = request.args.get("path", "").strip()
        if not raw_path:
            return jsonify({"error": "Missing path"}), 400

        path = Path(raw_path).expanduser()
        try:
            info = probe_media(path)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(media_info_to_dict(info))

    @app.post("/api/batch")
    def submit_batch():
        payload = request.get_json(silent=True) or {}
        raw_path = payload.get("path", "").strip()
        clips_text = str(payload.get("clips", "")).strip()
        delete_original = bool(payload.get("delete_original"))

        if not raw_path or not clips_text:
            return jsonify({"error": "path and clips are required"}), 400

        try:
            clips = parse_clip_lines(clips_text)
            batch = trim_queue.submit_batch(Path(raw_path), clips, delete_original)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except FileExistsError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

        return jsonify(batch_to_dict(batch))

    @app.get("/api/batch/<batch_id>")
    def batch_status(batch_id: str):
        batch = trim_queue.get_batch(batch_id)
        if batch is None:
            return jsonify({"error": "Batch not found"}), 404
        return jsonify(batch_to_dict(batch))

    @app.get("/api/queue")
    def queue_status():
        batches = [batch_to_dict(batch) for batch in trim_queue.list_batches()]
        return jsonify({"batches": batches, "summary": trim_queue.batch_counts()})

    @app.get("/api/job/<job_id>")
    def job_status(job_id: str):
        job = job_store.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job_to_dict(job))

    @app.post("/api/delete")
    def delete_file():
        payload = request.get_json(silent=True) or {}
        raw_path = payload.get("path", "").strip()
        confirmed = bool(payload.get("confirmed"))

        if not raw_path:
            return jsonify({"error": "path is required"}), 400
        if not confirmed:
            return jsonify({"error": "Deletion must be confirmed"}), 400

        path = Path(raw_path).expanduser()
        try:
            move_to_trash(path)
        except FileNotFoundError:
            return jsonify({"error": "File not found"}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

        return jsonify({"ok": True, "message": f"Moved to Trash: {path.name}"})

    @app.get("/api/generate-sheet")
    def generate_sheet():
        drive = request.args.get("drive", "").strip()
        date = request.args.get("date", "").strip()
        if not drive:
            return jsonify({"error": "drive is required"}), 400

        try:
            import tempfile

            from .core.inventory import safe_name, scan_all_archive, scan_archive_section, write_sheet

            archive = request.args.get("archive", "").strip()
            all_archive = request.args.get("all", "").strip().lower() in {"1", "true", "yes"}

            if all_archive:
                rows = scan_all_archive(drive)
                filename = f"footage_sheet_{safe_name(drive)}_all_archive.csv"
            elif archive:
                rows = scan_archive_section(drive, archive)
                filename = f"footage_sheet_{safe_name(drive)}_{safe_name(archive)}.csv"
            else:
                rows = scan_archive_section(drive, "YT")
                filename = f"footage_sheet_{safe_name(drive)}_YT.csv"

            if date:
                rows = [row for row in rows if row["date"] == date]
                filename = filename.replace(".csv", f"_{date}.csv")

            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as tmp:
                write_sheet(rows, Path(tmp.name))
                temp_path = Path(tmp.name)

            return send_file(
                temp_path,
                as_attachment=True,
                download_name=filename,
                mimetype="text/csv",
            )
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/template/csv")
    def template_csv():
        path = PROJECT_ROOT / "trim_sheet_template.csv"
        return send_file(path, as_attachment=True, download_name="trim_sheet_template.csv")

    @app.get("/api/template/json")
    def template_json():
        path = PROJECT_ROOT / "trim_sheet_template.json"
        return send_file(path, as_attachment=True, download_name="trim_sheet_template.json")

    @app.get("/api/template/guide")
    def template_guide():
        path = PROJECT_ROOT / "TRIM_SHEET_GUIDE.md"
        return send_file(path, as_attachment=True, download_name="TRIM_SHEET_GUIDE.md")

    @app.post("/api/import/preview")
    def import_preview():
        try:
            text, filename, drive, delete_original = _read_sheet_upload()
            preview = parse_sheet(text, filename, drive, delete_original)
            return jsonify(preview_to_dict(preview))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/import/queue")
    def import_queue():
        try:
            text, filename, drive, delete_original = _read_sheet_upload()
            preview = parse_sheet(text, filename, drive, delete_original)
            result = queue_import(preview, trim_queue)
            return jsonify(result)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400

    return app


def _read_sheet_upload() -> tuple[str, str, str, bool]:
    upload = request.files.get("file")
    drive = (request.form.get("drive") or "").strip()
    delete_original = request.form.get("delete_original", "yes").strip().lower() not in {
        "no",
        "false",
        "0",
    }

    if upload and upload.filename:
        return upload.read().decode("utf-8-sig"), upload.filename, drive, delete_original

    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    filename = str(payload.get("filename", "")).strip()
    drive = str(payload.get("drive", drive)).strip()
    delete_original = bool(payload.get("delete_original", delete_original))
    if text:
        return text, filename, drive, delete_original
    raise ValueError("Upload a CSV/JSON sheet or send JSON with a 'text' field")


def job_to_dict(job) -> dict:
    return {
        "job_id": job.job_id,
        "batch_id": job.batch_id,
        "clip_number": job.clip_number,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "output_path": str(job.output_path),
        "output_name": job.output_path.name,
        "start": format_timestamp(job.start_seconds),
        "end": format_timestamp(job.end_seconds),
        "command": job.command,
    }


def batch_to_dict(batch) -> dict:
    jobs = [job_store.get(job_id) for job_id in batch.job_ids]
    jobs = [job_to_dict(job) for job in jobs if job is not None]
    completed = sum(job["status"] == "completed" for job in jobs)
    failed = sum(job["status"] == "failed" for job in jobs)
    running = sum(job["status"] == "running" for job in jobs)
    queued = sum(job["status"] == "queued" for job in jobs)
    overall_progress = 0.0
    if jobs:
        overall_progress = sum(job["progress"] for job in jobs) / len(jobs)

    return {
        "batch_id": batch.batch_id,
        "input_path": str(batch.input_path),
        "input_name": batch.input_name,
        "delete_original": batch.delete_original,
        "status": batch.status,
        "message": batch.message,
        "clip_count": len(jobs),
        "completed": completed,
        "failed": failed,
        "running": running,
        "queued": queued,
        "progress": overall_progress,
        "jobs": jobs,
    }


def media_info_to_dict(info: MediaInfo) -> dict:
    return {
        "path": str(info.path),
        "name": info.path.name,
        "duration": info.duration,
        "duration_label": format_timestamp(info.duration) if info.duration else None,
        "size_bytes": info.size_bytes,
        "has_gpmf": info.has_gpmf,
        "streams": [
            {
                "index": stream.index,
                "codec_type": stream.codec_type,
                "codec_name": stream.codec_name,
                "codec_tag": stream.codec_tag,
                "handler_name": stream.handler_name,
            }
            for stream in info.streams
        ],
    }


def main() -> None:
    host = os.environ.get("GOPRO_CLEANER_HOST", "127.0.0.1")
    port = int(os.environ.get("GOPRO_CLEANER_PORT", "8765"))
    debug = os.environ.get("GOPRO_CLEANER_DEBUG", "0") == "1"
    app = create_app()
    print(f"GoPro Footage Cleaner running at http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
