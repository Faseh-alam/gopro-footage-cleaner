"""Fire-and-forget Slack alerts via an Incoming Webhook.

No-op when ``slack_webhook_url`` is unset in config.json — safe to call from
anywhere without checking first. Never raises into the caller; a broken
webhook must not take down an upload job.
"""

from __future__ import annotations

import json
import threading
import urllib.request

from .config import load_config

_TIMEOUT_SEC = 10


def send_alert(message: str) -> None:
    """Post ``message`` to the configured Slack webhook in a background thread."""
    url = str(load_config().get("slack_webhook_url") or "").strip()
    if not url:
        return
    threading.Thread(target=_post, args=(url, message), daemon=True, name="slack-alert").start()


def _post(url: str, message: str) -> None:
    body = json.dumps({"text": message}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(request, timeout=_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — alerting must never crash the caller
        pass
