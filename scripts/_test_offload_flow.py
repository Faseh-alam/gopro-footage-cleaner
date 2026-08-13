"""Simulated SD-offload flow test.

Covers: inventory → flat-batch copy (no card subfolder) → cross-card filename
collisions → embed → verify → resume → wipe → AWS-side work-clip rebuild.
Run with ffmpeg/ffprobe on PATH for the full set (falls back to synthetic MP4s).
"""

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

from offloader import detect, eject, embed_meta, engine, inventory, progress  # noqa: E402
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


def make_card(root: Path, *, with_sidecar: dict | None, legacy: bool = False) -> Path:
    gopro = root / "DCIM" / "100GOPRO"
    gopro.mkdir(parents=True)
    make_mp4(gopro / "GX010001.MP4")
    if with_sidecar is not None:
        (gopro / "GX010001.segments.json").write_text(
            json.dumps(with_sidecar), encoding="utf-8"
        )
    if legacy:
        make_mp4(gopro / "GX010002.MP4")
        folder = gopro / "pipe-welding"
        folder.mkdir()
        make_mp4(folder / "GX019999.MP4")
        (gopro / "GX010001.THM").write_bytes(b"junk")
        (gopro / "GX010001.LRV").write_bytes(b"junk")
    return gopro


def simulate_worker(card_root: Path, dest: Path, card_id: str) -> tuple[list[dict], dict]:
    """Copy+embed loop exactly like engine._copy_card_worker (no threads/UI)."""
    files = inventory.list_transfer_files(card_root)
    prog = {"batch": "batch 1", "card_id": card_id, "dest": str(dest),
            "files": {}, "status": "in_progress"}
    engine._resolve_dest_names(files, dest, prog, card_id)
    for item in files:
        src = Path(item["source"])
        dest_rel = item.get("dest_rel") or item["rel"]
        dest_file = dest / dest_rel
        if progress.is_file_done(prog, item["rel"], int(item["size"]), dest_file):
            item["dest_size"] = dest_file.stat().st_size
            continue
        copy_file(src, dest_file)
        dest_size = int(item["size"])
        if item.get("embed_json"):
            payload = json.loads(Path(item["embed_json"]).read_text(encoding="utf-8"))
            embed_meta.embed_segments_json(dest_file, payload)
            dest_size = dest_file.stat().st_size
        item["dest_size"] = dest_size
        progress.mark_file_done(card_root, prog, item["rel"], int(item["size"]),
                                dest_size=dest_size, dest_rel=dest_rel)
    return files, prog


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="offload_test_"))
    batch_dest = tmp / "ssd" / "Batches" / "batch 1"
    batch_dest.mkdir(parents=True)

    full_sidecar = {
        "version": 1,
        "source": "GX010001.MP4",
        "duration": 1.0,
        "batch_name": "batch 1",
        "factory": "Karachi Plant",
        "card_badge": "C1234",
        "device_type": "gopro",
        "device_id": "GP-07",
        "complete": True,
        "media_meta": {
            "recorded_at": "2026-08-10T09:00:00+00:00",
            "camera_serial": "C3501324500712",
            "camera_model": "HERO12 Black",
            "firmware": "H23.01.02.32.00",
            "sensors": ["Accelerometer", "Gyroscope", "GPS (Lat., Long., Alt., 2D speed, 3D speed)"],
        },
        "segments": [
            {"kind": "work", "task": "Pipe Welding", "start": 0.0, "end": 0.6},
            {"kind": "garbage", "task": "", "start": 0.6, "end": 1.0},
        ],
    }
    incomplete_sidecar = {
        "version": 1,
        "source": "GX010001.MP4",
        "duration": 1.0,
        "batch_name": "batch 1",
        "card_badge": "C5678",
        "complete": False,
        "media_meta": {},
        "segments": [{"kind": "work", "task": "Cable Pulling", "start": 0.0, "end": 0.4}],
    }

    card_a = tmp / "cardA"
    card_b = tmp / "cardB"
    gopro_a = make_card(card_a, with_sidecar=full_sidecar, legacy=True)
    make_card(card_b, with_sidecar=incomplete_sidecar)

    print("\n[1] inventory + detect")
    files_a = inventory.list_transfer_files(card_a)
    rels = sorted(f["rel"] for f in files_a)
    check("finds root MP4s + sidecar only (no task folders)", rels == [
        "GX010001.MP4", "GX010001.segments.json", "GX010002.MP4",
    ], str(rels))
    check("legacy task folder is ignored",
          "pipe-welding/GX019999.MP4" not in rels)
    check("root-level MP4s make card candidate", detect._looks_like_sd_card(card_a, "NO NAME"))
    check("card id from JSON serial is C0712",
          engine._card_id_from_sidecars(files_a) == "C0712")

    print("\n[2] card A → flat batch folder (no card subfolder)")
    files_a, prog_a = simulate_worker(card_a, batch_dest, "C0712")
    check("MP4 sits directly in batch folder", (batch_dest / "GX010001.MP4").is_file())
    check("sidecar sits directly in batch folder",
          (batch_dest / "GX010001.segments.json").is_file())
    check("no C1234 / C0712 subfolder created",
          not (batch_dest / "C1234").exists() and not (batch_dest / "C0712").exists())
    check("legacy task folder not copied",
          not (batch_dest / "pipe-welding").exists())
    embedded = embed_meta.read_embedded_segments(batch_dest / "GX010001.MP4")
    check("embedded payload reads back", embedded == full_sidecar)
    check("embedded payload has camera serial + device + IMU sensors",
          bool(embedded)
          and embedded["media_meta"]["camera_serial"] == "C3501324500712"
          and embedded["device_id"] == "GP-07"
          and embedded["device_type"] == "gopro"
          and len(embedded["media_meta"]["sensors"]) == 3)

    print("\n[3] card B same filename → collision suffix")
    files_b, prog_b = simulate_worker(card_b, batch_dest, "C5678")
    mp4_b = next(f for f in files_b if f["rel"] == "GX010001.MP4")
    side_b = next(f for f in files_b if f["rel"].endswith(".segments.json"))
    check("MP4 renamed with card suffix", mp4_b["dest_rel"] == "GX010001__C5678.MP4",
          str(mp4_b["dest_rel"]))
    check("sidecar follows renamed stem",
          side_b["dest_rel"] == "GX010001__C5678.segments.json", str(side_b["dest_rel"]))
    check("both files exist in batch folder",
          (batch_dest / "GX010001__C5678.MP4").is_file()
          and (batch_dest / "GX010001__C5678.segments.json").is_file())
    check("card A copy untouched",
          embed_meta.read_embedded_segments(batch_dest / "GX010001.MP4") == full_sidecar)

    print("\n[4] resume with renamed + embedded files")
    loaded_b = progress.load_progress(card_b)
    entry = (loaded_b.get("files") or {}).get("GX010001.MP4") or {}
    check("progress remembers dest_rel", entry.get("dest_rel") == "GX010001__C5678.MP4")
    check("renamed embedded file counts as done",
          progress.is_file_done(loaded_b, "GX010001.MP4", int(mp4_b["size"]),
                                batch_dest / "GX010001__C5678.MP4"))
    check("dest_looks_complete via dest_rel", progress.dest_looks_complete(loaded_b, batch_dest))
    files_b2, _ = simulate_worker(card_b, batch_dest, "C5678")
    # Re-running with saved progress must not create a double-suffixed copy.
    check("re-run reuses same names (no dupes)",
          not any("__C5678__" in p.name for p in batch_dest.iterdir()))

    print("\n[5] metadata completeness check")
    check("full sidecar passes", engine._missing_metadata(full_sidecar) == [])
    missing = engine._missing_metadata(incomplete_sidecar)
    check("incomplete sidecar reports missing fields",
          "complete labeling" in missing and "camera_serial" in missing
          and "device_id" in missing and "IMU sensor list" in missing, str(missing))
    audit_a = engine._audit_files(inventory.list_transfer_files(card_a))
    check("audit flags MP4 without JSON",
          "GX010002.MP4" in audit_a["missing_json"] and audit_a["blockers"],
          str(audit_a["blockers"]))
    audit_full = engine._audit_files([
        {"rel": "GX010001.MP4", "size": 1, "embed_json": str(gopro_a / "GX010001.segments.json")},
        {"rel": "GX010001.segments.json", "size": 1},
    ])
    check("complete labeled pair has no wipe blockers",
          audit_full["blockers"] == [] and audit_full["ok"] == 1, str(audit_full["blockers"]))
    labeled_a, notes_a = engine._select_labeled_files(inventory.list_transfer_files(card_a))
    labeled_rels = sorted(f["rel"] for f in labeled_a)
    check("unlabeled GX010002 not selected for copy",
          "GX010002.MP4" not in labeled_rels and "GX010001.MP4" in labeled_rels,
          str(labeled_rels))
    check("skip note names the leftover file",
          any("GX010002.MP4" in n for n in notes_a), str(notes_a))
    labeled_b, _ = engine._select_labeled_files(inventory.list_transfer_files(card_b))
    check("incomplete JSON is not copied", labeled_b == [], str([f["rel"] for f in labeled_b]))

    print("\n[6] AWS script: rebuild work clips into task folders")
    ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    if ffmpeg_ok:
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "aws_trim_batch.py"), str(batch_dest)],
            capture_output=True, text=True, timeout=300,
        )
        out_root = batch_dest / "_trimmed"
        # C + last 4 of serial C3501324500712 → C0712 / task / original name
        clip = out_root / "C0712" / "pipe-welding" / "GX010001.MP4"
        check("script exits 0", result.returncode == 0,
              (result.stdout + result.stderr)[-300:])
        check("work clip at camera_id/task/original.mp4", clip.is_file(), str(clip))
        check("camera id folder from serial last-4", (out_root / "C0712").is_dir())
        check("no garbage clips / folders",
              sorted(p.name for p in (out_root / "C0712").iterdir()) == ["pipe-welding"]
              if (out_root / "C0712").is_dir() else False)
        check("incomplete video skipped by default",
              "GX010001__C5678.MP4" in result.stdout
              and not (out_root / "C5678" / "cable-pulling").exists())
        clip_meta = embed_meta.read_embedded_segments(clip) if clip.is_file() else None
        check("clip carries its own embedded identity",
              bool(clip_meta) and clip_meta.get("task") == "Pipe Welding"
              and clip_meta.get("clip_of") == "GX010001.MP4"
              and clip_meta.get("camera_id") == "C0712"
              and clip_meta.get("media_meta", {}).get("camera_serial") == "C3501324500712")
        if clip.is_file():
            probe = subprocess.run(
                [shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(clip)], capture_output=True, text=True, timeout=60,
            )
            dur = float(json.loads(probe.stdout or "{}").get("format", {}).get("duration") or 0)
            check("clip playable, duration sane", probe.returncode == 0 and 0.2 <= dur <= 1.1,
                  f"duration={dur}")
        # --include-incomplete picks up card B (no serial → falls back to card_badge C5678)
        result2 = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "aws_trim_batch.py"), str(batch_dest),
             "--include-incomplete"],
            capture_output=True, text=True, timeout=300,
        )
        check("--include-incomplete cuts cable-pulling clip",
              (out_root / "C5678" / "cable-pulling" / "GX010001__C5678.MP4").is_file(),
              (result2.stdout + result2.stderr)[-200:])
        shutil.rmtree(out_root, ignore_errors=True)
    else:
        print("  SKIP  ffmpeg/ffprobe not on PATH")

    print("\n[7] wipe transferred files on card A")
    root_rels = sorted(f["rel"] for f in files_a)
    eject.wipe_transferred_files(card_a, root_rels)
    check("root MP4s + sidecar removed",
          not (gopro_a / "GX010001.MP4").exists()
          and not (gopro_a / "GX010001.segments.json").exists()
          and not (gopro_a / "GX010002.MP4").exists())
    check("untransferred task folder left on card", (gopro_a / "pipe-welding").is_dir())
    check("junk untouched (THM/LRV stay)", (gopro_a / "GX010001.THM").exists())
    check("progress file cleared", progress.load_progress(card_a) is None)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
