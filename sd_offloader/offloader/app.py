"""Flask app for SD Card Offloader."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import __version__, aws_upload, batches, engine
from .config import load_config, save_config
from .detect import list_volumes

APP_ROOT = Path(__file__).resolve().parents[1]


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(APP_ROOT / "templates"),
        static_folder=str(APP_ROOT / "static"),
    )

    @app.get("/")
    def index():
        return render_template("index.html", version=__version__)

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "aws_cli": aws_upload.aws_cli_available(),
            }
        )

    @app.get("/api/config")
    def get_config():
        return jsonify(load_config())

    @app.post("/api/config")
    def post_config():
        payload = request.get_json(silent=True) or {}
        allowed = {"s3_uri", "ssd1", "ssd2", "last_batch", "mode", "port"}
        data = {k: payload[k] for k in allowed if k in payload}
        return jsonify(save_config(data))

    @app.get("/api/volumes")
    def volumes():
        return jsonify({"volumes": list_volumes()})

    @app.get("/api/batches")
    def api_batches():
        ssd1 = request.args.get("ssd1", "").strip()
        ssd2 = request.args.get("ssd2", "").strip()
        if not ssd1 and not ssd2:
            cfg = load_config()
            ssd1 = str(cfg.get("ssd1") or "")
            ssd2 = str(cfg.get("ssd2") or "")
        rows = batches.list_batches(ssd1, ssd2)
        return jsonify({"batches": rows, "count": len(rows)})

    @app.get("/api/status")
    def status():
        return jsonify(engine.get_status())

    @app.post("/api/session/start")
    def session_start():
        payload = request.get_json(silent=True) or {}
        try:
            result = engine.start_session(
                batch=str(payload.get("batch", "")),
                mode=str(payload.get("mode", "ssd_only")),
                ssd1=str(payload.get("ssd1", "")),
                ssd2=str(payload.get("ssd2", "")),
                s3_uri=str(payload.get("s3_uri", "")),
            )
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify(result)

    @app.post("/api/session/stop")
    def session_stop():
        return jsonify(engine.stop_session())

    @app.post("/api/aws/upload-batch")
    def aws_upload_batch():
        payload = request.get_json(silent=True) or {}
        # Allow uploading a specific batch without starting the watcher
        batch = str(payload.get("batch") or "").strip()
        s3_uri = str(payload.get("s3_uri") or "").strip()
        ssd1 = str(payload.get("ssd1") or "").strip()
        ssd2 = str(payload.get("ssd2") or "").strip()
        try:
            if batch or s3_uri or ssd1 or ssd2:
                cfg = load_config()
                batch = batch or str(cfg.get("last_batch") or "")
                s3_uri = s3_uri or str(cfg.get("s3_uri") or "")
                ssd1 = ssd1 or str(cfg.get("ssd1") or "")
                ssd2 = ssd2 or str(cfg.get("ssd2") or "")
                if not batch:
                    return jsonify({"error": "Select a batch first"}), 400
                if not s3_uri:
                    return jsonify({"error": "Set S3 URI first"}), 400
                save_config(
                    {
                        "last_batch": batch,
                        "s3_uri": s3_uri,
                        "ssd1": ssd1,
                        "ssd2": ssd2,
                    }
                )
                engine.bind_batch_context(batch=batch, ssd1=ssd1, ssd2=ssd2, s3_uri=s3_uri)
                job = aws_upload.start_batch_upload(
                    s3_uri=s3_uri,
                    batch_name=batch,
                    ssd1=ssd1,
                    ssd2=ssd2,
                    card_id=None,
                    show_console=True,
                )
                engine.log_message(f"AWS CMD upload started for batch {batch} (survives restart; UI tracks log)")
            else:
                job = engine.upload_batch_now(external_window=True)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "job": job})

    @app.post("/api/aws/test")
    def aws_test():
        payload = request.get_json(silent=True) or {}
        s3_uri = str(payload.get("s3_uri") or "").strip()
        if not s3_uri:
            cfg = load_config()
            s3_uri = str(cfg.get("s3_uri") or "").strip()
        if not s3_uri:
            return jsonify({"error": "Paste an S3 URI first (e.g. s3://bucket/prefix/)"}), 400
        try:
            save_config({"s3_uri": s3_uri})
            result = aws_upload.test_aws_connection(s3_uri)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify(result)

    return app


def main() -> None:
    import os
    import threading

    from .config import ensure_dirs, load_config

    ensure_dirs()
    cfg = load_config()
    port = int(os.environ.get("SD_OFFLOADER_PORT", cfg.get("port") or 8877))
    app = create_app()

    def _boot() -> None:
        try:
            print("Restoring previous session / AWS jobs in background…")
            engine.restore_ui_state()
            print("Restore complete.")
        except Exception as exc:  # noqa: BLE001
            print(f"Restore warning: {exc}")

    threading.Thread(target=_boot, daemon=True, name="offloader-restore").start()
    print(f"SD Card Offloader v{__version__} → http://127.0.0.1:{port}")
    print("(Page should open immediately; heavy SSD scans run in the background.)")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
