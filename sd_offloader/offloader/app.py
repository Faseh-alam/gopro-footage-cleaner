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

    @app.get("/api/ping")
    def ping():
        """Tiny liveness check — no disk / AWS / WMI work."""
        return jsonify({"ok": True, "version": __version__})

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "aws_cli": False,  # filled lazily; never block page load on PATH scan
                "ready": True,
            }
        )

    @app.get("/api/health/full")
    def health_full():
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "aws_cli": aws_upload.aws_cli_available(),
                "ready": True,
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
    import sys
    import threading
    import time
    import urllib.error
    import urllib.request
    import webbrowser

    from .config import ensure_dirs, load_config

    # Unbuffered-ish logs so the CMD window shows progress immediately
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    ensure_dirs()
    cfg = load_config()
    port = int(os.environ.get("SD_OFFLOADER_PORT", cfg.get("port") or 8877))
    host = os.environ.get("SD_OFFLOADER_HOST", "127.0.0.1")
    open_browser = os.environ.get("SD_OFFLOADER_OPEN_BROWSER", "1") != "0"
    url = f"http://{host}:{port}/"
    health_url = f"http://{host}:{port}/api/ping"

    print(f"Creating app (v{__version__})…", flush=True)
    app = create_app()
    print("App created.", flush=True)

    def _boot() -> None:
        # Give Flask a moment to bind before any disk/WMI work.
        time.sleep(2.0)
        try:
            print("Background: restoring session / AWS job list…", flush=True)
            engine.restore_ui_state()
            print("Background: restore finished.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"Background restore warning: {exc}", flush=True)

    def _open_when_ready() -> None:
        if not open_browser:
            return
        for attempt in range(60):
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    if getattr(resp, "status", 200) == 200:
                        print(f"Ready — opening browser: {url}", flush=True)
                        webbrowser.open(url)
                        return
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(0.5)
        print(
            f"Server health check did not succeed. Open manually: {url}",
            flush=True,
        )

    threading.Thread(target=_boot, daemon=True, name="offloader-restore").start()
    threading.Thread(target=_open_when_ready, daemon=True, name="offloader-browser").start()

    print(f"SD Card Offloader v{__version__} listening on {url}", flush=True)
    print("If the browser does not open, paste that URL into Chrome.", flush=True)
    try:
        try:
            from waitress import serve

            print("Using waitress server (multi-thread).", flush=True)
            serve(app, host=host, port=port, threads=16, channel_timeout=30)
        except ImportError:
            print("waitress not installed — falling back to Flask server.", flush=True)
            app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except OSError as exc:
        print(f"ERROR: could not bind {host}:{port} — {exc}", flush=True)
        print("Another program may still be using the port. Close it and retry.", flush=True)
        raise
