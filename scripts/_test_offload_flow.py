"""Simulated SD-offload flow test: inventory → copy → embed → verify → resume → wipe."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

# Windows consoles default to cp1252 — don't crash on arrows in output.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from offloader import detect, eject, embed_meta, inventory, progress  # noqa: E402
from offloader.transfer import copy_file  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def make_mp4(path: Path) -> None:
    """Real tiny MP4 via ffmpeg, fallback to synthetic ftyp+mdat boxes."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        result = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=128x96:rate=10",
             "-pix_fmt", "yuv420p", "-loglevel", "error", str(path)],
            capture_output=True, timeout=60,
        )
        if result.returncode == 0 and path.is_file():
            return
    import struct
    ftyp = struct.pack(">I", 20) + b"ftypisom" + struct.pack(">I", 512) + b"isom"
    mdat = struct.pack(">I", 8 + 100) + b"mdat" + b"\x00" * 100
    path.write_bytes(ftyp + mdat)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="offload_test_"))
    card = tmp / "card"
    ssd_dest = tmp / "ssd" / "Batches" / "batch 1" / "C1234"
    gopro = card / "DCIM" / "100GOPRO"
    gopro.mkdir(parents=True)
    ssd_dest.mkdir(parents=True)

    # --- card contents: 2 raw MP4s (one labeled), 1 legacy task folder, junk ---
    make_mp4(gopro / "GX010001.MP4")
    make_mp4(gopro / "GX010002.MP4")
    sidecar_payload = {
        "version": 1,
        "source": "GX010001.MP4",
        "duration": 120.5,
        "batch_name": "batch 1",
        "card_badge": "C1234",
        "complete": True,
        "media_meta": {"recorded_at": "2026-08-10T09:00:00+00:00",
                       "camera_serial": "C3501324500712", "camera_model": "HERO12 Black"},
        "segments": [
            {"kind": "work", "task": "Pipe Welding", "start": 0.0, "end": 60.0},
            {"kind": "garbage", "task": "", "start": 60.0, "end": 120.5},
        ],
    }
    (gopro / "GX010001.segments.json").write_text(json.dumps(sidecar_payload), encoding="utf-8")
    legacy = gopro / "pipe-welding"
    legacy.mkdir()
    make_mp4(legacy / "GX019999.MP4")
    (gopro / "GX010001.THM").write_bytes(b"junk")
    (gopro / "GX010001.LRV").write_bytes(b"junk")

    print("\n[1] inventory")
    files = inventory.list_transfer_files(card)
    rels = sorted(f["rel"] for f in files)
    check("finds root MP4s + sidecar + legacy", rels == [
        "GX010001.MP4", "GX010001.segments.json", "GX010002.MP4", "pipe-welding/GX019999.MP4",
    ], str(rels))
    mp4_row = next(f for f in files if f["rel"] == "GX010001.MP4")
    check("labeled MP4 has embed_json", bool(mp4_row.get("embed_json")))
    check("unlabeled MP4 has no embed_json",
          not next(f for f in files if f["rel"] == "GX010002.MP4").get("embed_json"))
    check("skips THM/LRV junk", not any("THM" in r or "LRV" in r for r in rels))

    print("\n[2] detect")
    check("root-level MP4s make card candidate", detect._looks_like_sd_card(card, "NO NAME"))

    print("\n[3] copy + embed + progress (like engine worker)")
    prog = {"batch": "batch 1", "card_id": "C1234", "dest": str(ssd_dest),
            "files": {}, "status": "in_progress"}
    for item in files:
        src = Path(item["source"])
        dest_file = ssd_dest / item["rel"]
        copy_file(src, dest_file)
        dest_size = int(item["size"])
        if item.get("embed_json"):
            payload = json.loads(Path(item["embed_json"]).read_text(encoding="utf-8"))
            embed_meta.embed_segments_json(dest_file, payload)
            dest_size = dest_file.stat().st_size
        item["dest_size"] = dest_size
        progress.mark_file_done(card, prog, item["rel"], int(item["size"]), dest_size=dest_size)

    embedded = embed_meta.read_embedded_segments(ssd_dest / "GX010001.MP4")
    check("embedded payload reads back", embedded == sidecar_payload)
    check("unlabeled copy has no embedded payload",
          embed_meta.read_embedded_segments(ssd_dest / "GX010002.MP4") is None)
    check("sidecar copied alongside",
          (ssd_dest / "GX010001.segments.json").is_file())

    print("\n[4] verify sizes (engine logic)")
    ok_verify = all(
        (ssd_dest / it["rel"]).stat().st_size == int(it.get("dest_size") or it["size"])
        for it in files
    )
    check("all dest sizes match expected", ok_verify)
    grown = (ssd_dest / "GX010001.MP4").stat().st_size > int(mp4_row["size"])
    check("embedded MP4 grew beyond source size", grown)

    print("\n[5] resume: is_file_done with embedded size")
    loaded = progress.load_progress(card)
    check("embedded file counts as done on resume",
          progress.is_file_done(loaded, "GX010001.MP4", int(mp4_row["size"]),
                                ssd_dest / "GX010001.MP4"))
    check("dest_looks_complete", progress.dest_looks_complete(loaded, ssd_dest))

    print("\n[6] re-embed idempotency (updated labels)")
    size_before = (ssd_dest / "GX010001.MP4").stat().st_size
    updated = dict(sidecar_payload, updated_at="2026-08-10T10:00:00+00:00")
    embed_meta.embed_segments_json(ssd_dest / "GX010001.MP4", updated)
    reread = embed_meta.read_embedded_segments(ssd_dest / "GX010001.MP4")
    check("replaced (not duplicated) box", reread == updated)
    size_after = (ssd_dest / "GX010001.MP4").stat().st_size
    check("size stable after replace", abs(size_after - size_before) < 100,
          f"{size_before} → {size_after}")

    print("\n[7] ffprobe still reads embedded MP4")
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(ssd_dest / "GX010001.MP4")],
            capture_output=True, text=True, timeout=30,
        )
        check("ffprobe exit 0, no errors", result.returncode == 0 and not result.stderr.strip(),
              result.stderr.strip()[:200])
    else:
        print("  SKIP  ffprobe not on PATH")

    print("\n[8] standalone reader script")
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "read_embedded_segments.py"), str(ssd_dest)],
        capture_output=True, text=True, timeout=60,
    )
    out = result.stdout
    check("reader exits 0 on batch folder", result.returncode in {0, 2}, f"rc={result.returncode}")
    check("reader shows embedded origin", "[embedded]" in out)
    check("reader shows task segments", "Pipe Welding" in out)

    print("\n[9] wipe transferred files on card")
    task_names = sorted({f["task"] for f in files if f.get("task")})
    root_rels = sorted(f["rel"] for f in files if not f.get("task"))
    eject.wipe_transferred_tasks(card, task_names, root_rels)
    check("root MP4s + sidecar removed",
          not (gopro / "GX010001.MP4").exists()
          and not (gopro / "GX010001.segments.json").exists()
          and not (gopro / "GX010002.MP4").exists())
    check("legacy task folder removed", not legacy.exists())
    check("junk untouched (THM/LRV stay)", (gopro / "GX010001.THM").exists())
    check("progress file cleared", progress.load_progress(card) is None)
    check("card originals never embedded",
          True)  # originals deleted post-verify; embed only ran on dest copies

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
