"""End-to-end test for the HLS preview pipeline (build + serve while encoding).

Creates a synthetic 120s clip, builds its preview with the software encoder,
and checks that:
  1. the playlist becomes *playable* (>=2 segments) before the encode finishes,
  2. the finished playlist has an ENDLIST marker and all segments on disk,
  3. Flask serves the playlist (no-store) and segments, rejects bad keys/names,
  4. the legacy /api/eager/preview endpoint returns the HLS pointer.

Run:  python scripts/_test_preview_hls.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Deterministic encoder (hardware probing would make timings machine-specific).
os.environ["GOPRO_PREVIEW_ENCODER"] = "x264"

from gopro_cleaner.core import preview_proxy  # noqa: E402
from gopro_cleaner.core.ffmpeg_tools import ffmpeg_bin  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def make_source(directory: Path) -> Path:
    """120s 1080p test clip — long enough that the encode takes a few seconds."""
    out = directory / "GX_TEST_HLS.MP4"
    subprocess.run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=24:duration=120",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ],
        check=True,
        timeout=300,
    )
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hls_preview_test_"))
    preview_dir: Path | None = None
    try:
        print("== Preparing synthetic source clip ==")
        source = make_source(tmp)
        print(f"  source: {source} ({source.stat().st_size // 1024} KiB)")

        print("== Building preview (HLS) ==")
        st = preview_proxy.preview_status(source, start=True)
        check("build starts as running", st.get("status") == "running", str(st))

        saw_playable_before_ready = False
        deadline = time.time() + 240
        while time.time() < deadline:
            st = preview_proxy.preview_status(source, start=False)
            if st.get("status") == "running" and st.get("playable"):
                saw_playable_before_ready = True
            if st.get("status") in {"ready", "error"}:
                break
            time.sleep(0.05)

        check("build finished ready", st.get("status") == "ready", str(st.get("error") or st))
        check(
            "playable before the encode finished",
            saw_playable_before_ready,
            "(fast machines may legitimately skip this window)" if not saw_playable_before_ready else "",
        )
        check("status carries hls url", bool(st.get("hls")), str(st.get("hls")))

        playlist = Path(st["path"])
        preview_dir = playlist.parent
        text = playlist.read_text(encoding="utf-8")
        seg_names = [line.strip() for line in text.splitlines() if line.strip().endswith(".ts")]
        check("playlist has ENDLIST", "#EXT-X-ENDLIST" in text)
        check("playlist lists 2s segments", len(seg_names) >= 50, f"{len(seg_names)} segments for 120s")
        missing = [n for n in seg_names if not (preview_dir / n).is_file()]
        check("all segments exist on disk", not missing, f"missing: {missing[:3]}")

        print("== Serving over Flask ==")
        from gopro_cleaner.app import create_app

        client = create_app().test_client()
        key = preview_dir.name
        hls_url = str(st["hls"])
        check("hls url embeds cache key", key in hls_url, hls_url)

        r = client.get(hls_url)
        check("GET playlist -> 200", r.status_code == 200, f"{r.status_code}")
        check("playlist is no-store", "no-store" in (r.headers.get("Cache-Control") or ""))
        check(
            "playlist mimetype",
            "mpegurl" in (r.headers.get("Content-Type") or ""),
            r.headers.get("Content-Type") or "",
        )

        r = client.get(f"/api/eager/preview/hls/{key}/{seg_names[0]}")
        check("GET first segment -> 200", r.status_code == 200, f"{r.status_code}")
        check("segment mimetype", "mp2t" in (r.headers.get("Content-Type") or ""), r.headers.get("Content-Type") or "")

        check(
            "traversal-ish name rejected",
            client.get(f"/api/eager/preview/hls/{key}/..%2f..%2fsecret.txt").status_code == 404,
        )
        check(
            "bad key rejected",
            client.get(f"/api/eager/preview/hls/ZZZZZZZZZZZZZZZZZZZZ/index.m3u8").status_code == 404,
        )
        check(
            "stderr.log not servable",
            client.get(f"/api/eager/preview/hls/{key}/stderr.log").status_code == 404,
        )

        r = client.get(f"/api/eager/preview?path={source}")
        body = r.get_json() if r.status_code == 200 else {}
        check(
            "legacy /preview points at hls",
            r.status_code == 200 and body.get("hls") == hls_url,
            f"{r.status_code} {body}",
        )

        print("== Restart / cache behaviour ==")
        preview_proxy._jobs.clear()  # simulate a backend restart
        st2 = preview_proxy.preview_status(source, start=False)
        check("finished preview survives restart as cached-ready", st2.get("status") == "ready" and st2.get("cached") is True, str(st2))

        # Half-built folder (playlist without ENDLIST) must not be reported ready.
        no_end = "\n".join(line for line in text.splitlines() if "ENDLIST" not in line)
        playlist.write_text(no_end, encoding="utf-8")
        st3 = preview_proxy.preview_status(source, start=False)
        check("interrupted build is not ready", st3.get("status") == "idle", str(st3.get("status")))

        print()
        if FAILURES:
            print(f"{len(FAILURES)} FAILED: {FAILURES}")
            return 1
        print("All HLS preview checks passed.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if preview_dir is not None:
            shutil.rmtree(preview_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
