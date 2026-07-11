"""Flask app for SD Card Offloader."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import __version__, aws_upload, engine
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
        try:
            job = engine.upload_batch_now()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "job": job})

    return app


def main() -> None:
    import os

    from .config import ensure_dirs, load_config

    ensure_dirs()
    cfg = load_config()
    port = int(os.environ.get("SD_OFFLOADER_PORT", cfg.get("port") or 8877))
    app = create_app()
    print(f"SD Card Offloader v{__version__} → http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
