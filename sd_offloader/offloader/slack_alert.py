"""Fire-and-forget, properly-formatted Slack alerts via an Incoming Webhook.

Uses Slack's attachment format for a colored severity bar + bold title +
clean field grid — the actual "alert" look (PagerDuty/Opsgenie-style), not a
wall of plain-text sentences. No-op when ``slack_webhook_url`` is unset in
config.json — safe to call from anywhere without checking first. Never
raises into the caller; a broken webhook must not take down an upload job.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

from .config import load_config

_TIMEOUT_SEC = 10
_DETAIL_MAX_CHARS = 2500  # Slack attachment text has a practical size limit

_COLORS = {
    "critical": "#E01E5A",  # red   -- hard failure, needs attention
    "warning": "#ECB22E",  # amber -- degraded / needs eyes, not urgent
    "good": "#2EB67D",  # green -- resolved / confirms fine
    "info": "#36C5F0",  # blue  -- suppressed/FYI note
}

_SIGNS = {
    "critical": "\U0001F534",  # red circle
    "warning": "\U0001F7E1",  # yellow circle
    "good": "\U0001F7E2",  # green circle
    "info": "\U0001F535",  # blue circle
}


def format_context(*, batch: str | None = None, disks: str | None = None, card: str | None = None) -> str:
    """Build a compact ' — batch X · disks · card Y' title suffix.

    Slack's notification preview (mobile push, collapsed view) often shows
    only the title line, not the expanded fields grid below it — so the
    essentials (which batch, which disk, which card) need to survive in the
    title itself, not just in the fields, or a bare preview tells you nothing
    actionable.
    """
    parts = []
    if batch:
        parts.append(f"batch {batch}")
    if disks:
        parts.append(disks)
    if card:
        parts.append(f"card {card}")
    return " — " + " · ".join(parts) if parts else ""


def disk_label_from_path(path: str | None) -> str:
    """Disk name from a path like '.../SSD-1/Batches/x' or a card mount root."""
    if not path:
        return ""
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    if "Batches" in parts:
        idx = parts.index("Batches")
        if idx > 0:
            return parts[idx - 1]
    return parts[-1] if parts else ""


def disks_from_sources(sources: list[str] | None) -> str:
    """'SSD-1 + SSD-2'-style label from a job's local source paths, de-duped."""
    if not sources:
        return ""
    seen: set[str] = set()
    labels: list[str] = []
    for src in sources:
        label = disk_label_from_path(src)
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return " + ".join(labels)


def send_alert(
    title: str,
    *,
    severity: str = "critical",
    fields: list[tuple[str, str]] | None = None,
    detail: str | None = None,
) -> None:
    """Post a formatted alert to the configured Slack webhook, in the background.

    title    -- short headline, e.g. "AWS sync failed — retrying (1/3)" — a
                severity sign is prepended automatically; append batch/disk/
                card context yourself via format_context() so it survives in
                a bare notification preview too.
    severity -- "critical" (red) / "warning" (amber) / "good" (green) / "info" (blue)
    fields   -- [(label, value), ...] shown as a clean 2-column grid
    detail   -- optional longer text (e.g. a log tail), shown in a code block
    """
    url = str(load_config().get("slack_webhook_url") or "").strip()
    if not url:
        return
    sign = _SIGNS.get(severity, _SIGNS["critical"])
    full_title = f"{sign} {title}"
    payload = _build_payload(full_title, severity=severity, fields=fields or [], detail=detail)
    threading.Thread(target=_post, args=(url, payload), daemon=True, name="slack-alert").start()


def _build_payload(
    title: str, *, severity: str, fields: list[tuple[str, str]], detail: str | None
) -> dict:
    attachment: dict = {
        "color": _COLORS.get(severity, _COLORS["critical"]),
        "title": title,
        "fallback": title,  # shown in notifications / clients that can't render attachments
        "footer": "SD Card Offloader",
        "ts": int(time.time()),
    }
    if fields:
        attachment["fields"] = [
            {"title": str(label), "value": str(value), "short": len(str(value)) <= 40}
            for label, value in fields
        ]
    if detail:
        trimmed = detail if len(detail) <= _DETAIL_MAX_CHARS else detail[-_DETAIL_MAX_CHARS:]
        attachment["text"] = f"```{trimmed}```"
    return {"text": title, "attachments": [attachment]}


def _post(url: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(request, timeout=_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — alerting must never crash the caller
        pass
