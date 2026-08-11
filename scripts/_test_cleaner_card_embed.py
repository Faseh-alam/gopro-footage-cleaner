"""GoPro Cleaner: untitled SD detection + embed-on-sidecar-save."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from gopro_cleaner.core import annotation_store, embed_meta, volumes  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def make_mp4(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        r = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
             "-pix_fmt", "yuv420p", "-loglevel", "error", str(path)],
            capture_output=True, timeout=60,
        )
        if r.returncode == 0 and path.is_file():
            return
    import struct
    path.write_bytes(
        struct.pack(">I", 20) + b"ftypisom" + struct.pack(">I", 512) + b"isom"
        + struct.pack(">I", 108) + b"mdat" + b"\x00" * 100
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cleaner_card_"))
    try:
        print("\n[1] untitled volume with DCIM/100GOPRO MP4s")
        card = tmp / "UNTITLED_VOL"
        gopro = card / "DCIM" / "100GOPRO"
        gopro.mkdir(parents=True)
        make_mp4(gopro / "GX010001.MP4")
        check("gopro_has_mp4", volumes._gopro_has_mp4(gopro))
        check(
            "display id from random label",
            volumes._display_card_id(card, "EOS_DIGITAL") == "EOS_DIGITAL",
        )
        check(
            "display id for blank label uses CARD- prefix",
            volumes._display_card_id(card, "NO NAME").startswith("CARD-")
            or volumes._display_card_id(card, "").startswith("CARD-"),
        )
        # Simulate list_sd_cards core rule without scanning real drives
        gopro_root = volumes._find_gopro_root(card)
        check("finds 100GOPRO", gopro_root is not None and gopro_root.name.upper() == "100GOPRO")
        check(
            "would be listed (has MP4s, any name)",
            gopro_root is not None and volumes._gopro_has_mp4(gopro_root),
        )

        print("\n[2] classic C#### still preferred")
        check("C1234 label wins", volumes._display_card_id(card, "C1234") == "C1234")

        print("\n[3] save_annotation embeds segments into MP4")
        video = gopro / "GX010001.MP4"
        result = annotation_store.save_annotation(
            video,
            {
                "duration": 1.0,
                "batch_name": "batch 1",
                "card_badge": "EOS_DIGITAL",
                "device_type": "gopro",
                "device_id": "GP-01",
                "media_meta": {
                    "camera_serial": "C3501324500712",
                    "camera_model": "HERO12 Black",
                    "recorded_at": "2026-08-10T09:00:00+00:00",
                    "sensors": ["Accelerometer", "Gyroscope"],
                },
                "segments": [
                    {"kind": "work", "task": "Pipe Welding", "start": 0.0, "end": 0.5},
                    {"kind": "garbage", "start": 0.5, "end": 1.0},
                ],
            },
        )
        ann = result["annotation"]
        check("sidecar written", annotation_store.sidecar_path_for(video).is_file())
        embedded = embed_meta.read_embedded_segments(video)
        check("MP4 has embedded payload", embedded is not None)
        check(
            "embedded has start/end/kind/task",
            bool(embedded)
            and embedded["segments"][0]["kind"] == "work"
            and embedded["segments"][0]["task"] == "Pipe Welding"
            and embedded["segments"][0]["start"] == 0.0
            and embedded["segments"][0]["end"] == 0.5,
        )
        check(
            "embedded has camera serial",
            bool(embedded)
            and (embedded.get("media_meta") or {}).get("camera_serial") == "C3501324500712",
        )
        check(
            "sidecar size refreshed after embed",
            ann.get("size_bytes") == video.stat().st_size,
        )

        print("\n[4] camera id helper for AWS layout")
        sys.path.insert(0, str(REPO / "scripts"))
        # Import helpers from aws_trim_batch without running main
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "aws_trim_batch", REPO / "scripts" / "aws_trim_batch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        check(
            "serial → C0712",
            mod.camera_id_from_payload(ann) == "C0712",
            mod.camera_id_from_payload(ann),
        )
        check(
            "clip keeps original name for single task clip",
            mod.clip_filename("GX010001.MP4", 1, 1) == "GX010001.MP4",
        )
        check(
            "clip suffixes when same task has 2 segments",
            mod.clip_filename("GX010001.MP4", 2, 2) == "GX010001_02.MP4",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
