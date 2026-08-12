"""Shared ffmpeg encode slot for preview + skim proxies.

One concurrent encode keeps laptops responsive during review. Skim builds should
acquire this slot ahead of careful 720p HLS when both are queued.
"""

from __future__ import annotations

import threading

# Only one ffmpeg proxy encode at a time — prevents the machine from locking up.
encode_slots = threading.Semaphore(1)
