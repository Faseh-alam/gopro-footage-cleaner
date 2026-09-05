"""Split class folders across N USB sticks and install Voiceover Station on each.

Typical flow (4 sticks plugged at a time)::

  python -m pack_voiceover_usbs plan \\
    --source "/Users/faz/wc-sample-30h/videos" \\
    --sticks 10

  python -m pack_voiceover_usbs fill --plan usb_plan.json

Plan assigns whole class folders (never splits a class across sticks).
Fill copies: VoiceoverStation app + README.txt + voiceover/<classes>.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".cursor",
    "agent-transcripts",
    "dist",
    ".output",
    "sd_offloader",
}


@dataclass
class ClassFolder:
    name: str
    path: Path
    size_bytes: int
    clip_count: int


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{n} B"


def folder_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def scan_classes(source: Path) -> list[ClassFolder]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Not a folder: {source}")
    rows: list[ClassFolder] = []
    for child in sorted(source.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        clips = [
            p
            for p in child.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
        ]
        if not clips:
            continue
        rows.append(
            ClassFolder(
                name=child.name,
                path=child,
                size_bytes=folder_size(child),
                clip_count=len(clips),
            )
        )
    if not rows:
        raise RuntimeError(f"No class folders with videos under {source}")
    return rows


def build_plan(classes: list[ClassFolder], sticks: int) -> dict:
    if sticks < 1:
        raise ValueError("sticks must be >= 1")
    # Largest-first into currently lightest stick (balance by bytes).
    buckets: list[dict] = [
        {"id": i + 1, "label": f"USB-{i + 1:02d}", "classes": [], "size_bytes": 0, "clip_count": 0}
        for i in range(sticks)
    ]
    for cls in sorted(classes, key=lambda c: c.size_bytes, reverse=True):
        target = min(buckets, key=lambda b: b["size_bytes"])
        target["classes"].append(
            {
                "name": cls.name,
                "source": str(cls.path),
                "size_bytes": cls.size_bytes,
                "clip_count": cls.clip_count,
            }
        )
        target["size_bytes"] += cls.size_bytes
        target["clip_count"] += cls.clip_count
    return {
        "version": 1,
        "created_at": _now(),
        "source": str(classes[0].path.parent),
        "sticks": sticks,
        "class_count": len(classes),
        "total_bytes": sum(c.size_bytes for c in classes),
        "buckets": buckets,
        "filled": {},  # label -> {volume, at}
    }


def save_plan(plan: dict, path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def load_plan(path: Path) -> dict:
    return json.loads(path.expanduser().read_text(encoding="utf-8"))


def list_candidate_volumes() -> list[Path]:
    home = Path.home().resolve()
    out: list[Path] = []
    if sys.platform == "darwin":
        root = Path("/Volumes")
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                lower = entry.name.lower()
                if lower in {"macintosh hd", "macintosh hd - data", "system", "recovery", "preboot", "vm"}:
                    continue
                out.append(entry.resolve())
    elif sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            if drive.exists():
                # Skip system drive roughly.
                if letter.upper() == "C":
                    continue
                out.append(drive)
    else:
        for base in (Path("/media"), Path("/mnt"), Path("/run/media")):
            if not base.is_dir():
                continue
            for entry in base.rglob("*"):
                if entry.is_dir() and entry != base:
                    out.append(entry)
    # Never treat home as a USB.
    return [p for p in out if home not in p.parents and p != home]


def student_readme(stick_label: str, classes: list[str], stick_id: int, sticks: int) -> str:
    class_lines = "\n".join(f"  - {name}" for name in classes) or "  (none)"
    return f"""VOICEOVER USB — {stick_label} (stick {stick_id} of {sticks})
========================================

YOU DO NOT NEED TO CODE.

1. Plug in THIS USB stick and your USB microphone.
2. Open THIS USB in Finder / File Explorer.
3. Open the folder:  VoiceoverStation
4. Start the app:
   - Windows: double-click  start-voiceover.bat
   - Mac: double-click  start-voiceover.command
     (or open Terminal and run:  ./start-voiceover.sh )
5. Wait for the browser page "Voiceover Station".
   If nothing opens, go to:  http://127.0.0.1:8765/voiceover
6. Click "Open voiceover folder".
7. Select the folder named  voiceover  on THIS SAME USB.
8. Allow the microphone. Pick your mic. Speak — green bar should move.
9. Select a clip.
10. Keys:
    R     = start / stop recording (saves YOUR voice into the same video file)
    Space = pause / play the video only (keep talking while paused)
    N     = next clip
    Esc   = cancel a take (does not change the file)
11. Leave the black Terminal / Command window open until you are finished.

IMPORTANT
- Say "I …" for YOUR hands and tools (you are the camera).
- If another person is in the picture, say what THEY are doing in third person.
- Describe steps in detail + the room/environment. Aim for lots of talking.

Classes on THIS stick:
{class_lines}

If the page says "refused to connect", run the start script again.
Ask staff if Python is missing (one-time install on the computer).
"""


def _should_skip(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    parts = set(rel.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    # Skip frontend source (built UI is in gopro_cleaner/web).
    if len(rel.parts) >= 2 and rel.parts[0] == "gopro_cleaner" and rel.parts[1] == "frontend":
        return True
    return False


def copy_app_bundle(app_root: Path, dest: Path) -> None:
    """Copy a runnable Voiceover Station without .venv / node_modules / .git."""
    app_root = app_root.expanduser().resolve()
    dest = dest.expanduser().resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    include_files = [
        "requirements.txt",
        "run.sh",
        "run.bat",
        "start-voiceover.sh",
        "start-voiceover.bat",
        "VOICEOVER.md",
    ]
    for name in include_files:
        src = app_root / name
        if src.is_file():
            shutil.copy2(src, dest / name)

    # Make Mac double-click friendly.
    command = dest / "start-voiceover.command"
    command.write_text(
        "#!/bin/bash\n"
        'cd "$(dirname "$0")"\n'
        'chmod +x ./start-voiceover.sh ./run.sh 2>/dev/null || true\n'
        './start-voiceover.sh\n',
        encoding="utf-8",
    )
    os.chmod(command, 0o755)
    for script in (dest / "start-voiceover.sh", dest / "run.sh"):
        if script.is_file():
            os.chmod(script, 0o755)

    # Copy gopro_cleaner package + built web UI only.
    src_pkg = app_root / "gopro_cleaner"
    dst_pkg = dest / "gopro_cleaner"
    if not src_pkg.is_dir():
        raise FileNotFoundError(f"Missing {src_pkg}")
    shutil.copytree(
        src_pkg,
        dst_pkg,
        ignore=shutil.ignore_patterns(
            "frontend",
            "node_modules",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".DS_Store",
        ),
    )
    web = dst_pkg / "web"
    if not (web / "index.html").is_file() and not (web / "_shell.html").is_file():
        raise RuntimeError("gopro_cleaner/web UI build missing — run npm run build:flask first")


def _fmt_eta(seconds: float) -> str:
    if not (seconds >= 0) or seconds == float("inf"):
        return "--:--"
    total = int(seconds + 0.5)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class MultiProgressBoard:
    """Thread-safe multi-line progress (one line per USB)."""

    def __init__(self, slots: list[str]) -> None:
        self._lock = threading.Lock()
        self.slots = list(slots)
        self.states: dict[str, str] = {s: "waiting…" for s in self.slots}
        self._drawn = False

    def update(self, slot: str, line: str) -> None:
        with self._lock:
            self.states[slot] = line
            self._redraw()

    def finish_slot(self, slot: str, line: str) -> None:
        with self._lock:
            self.states[slot] = line
            self._redraw()

    def close(self) -> None:
        with self._lock:
            if self._drawn:
                sys.stdout.write("\n")
                sys.stdout.flush()

    def _redraw(self) -> None:
        n = len(self.slots)
        if self._drawn:
            sys.stdout.write(f"\033[{n}A")
        for slot in self.slots:
            text = f"{slot}: {self.states.get(slot, '')}"
            sys.stdout.write(text[:118].ljust(118) + "\n")
        sys.stdout.flush()
        self._drawn = True


@dataclass
class CopyProgress:
    total_bytes: int
    label: str = ""
    done_bytes: int = 0
    started_at: float = 0.0
    last_print_at: float = 0.0
    current_name: str = ""
    board: MultiProgressBoard | None = None
    quiet: bool = False

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = time.monotonic()

    def add(self, n: int, *, name: str = "") -> None:
        self.done_bytes += max(0, n)
        if name:
            self.current_name = name
        now = time.monotonic()
        if now - self.last_print_at < 0.3 and self.done_bytes < self.total_bytes:
            return
        self.last_print_at = now
        self._emit()

    def finish(self) -> None:
        self.done_bytes = max(self.done_bytes, self.total_bytes)
        self._emit(final=True)
        if self.board is None and not self.quiet:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def status_line(self) -> str:
        total = max(1, self.total_bytes)
        done = min(self.done_bytes, total)
        pct = 100.0 * done / total
        elapsed = max(0.001, time.monotonic() - self.started_at)
        speed = done / elapsed
        remaining = max(0.0, total - done)
        eta = remaining / speed if speed > 1 else float("inf")
        name = self.current_name
        if len(name) > 28:
            name = "…" + name[-27:]
        return (
            f"{pct:5.1f}% {_fmt_bytes(done)}/{_fmt_bytes(total)} "
            f"{_fmt_bytes(int(speed))}/s ETA {_fmt_eta(eta)} {name}"
        )

    def _emit(self, *, final: bool = False) -> None:
        line = self.status_line()
        if self.board is not None:
            self.board.update(self.label, line)
            return
        if self.quiet:
            return
        prefix = f"  {self.label} " if self.label else "  "
        text = (prefix + line)[:120].ljust(120)
        sys.stdout.write("\r" + text)
        sys.stdout.flush()


def _iter_copy_files(src: Path) -> list[tuple[Path, int]]:
    files: list[tuple[Path, int]] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.name.startswith("._"):
            continue
        if path.name == ".DS_Store":
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files.append((path, size))
    return files


def copy_file_progress(src: Path, dst: Path, progress: CopyProgress, *, rel_name: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    chunk = 1024 * 1024  # 1 MiB
    with src.open("rb") as rf, dst.open("wb") as wf:
        while True:
            buf = rf.read(chunk)
            if not buf:
                break
            wf.write(buf)
            progress.add(len(buf), name=rel_name)
    try:
        shutil.copystat(src, dst, follow_symlinks=True)
    except OSError:
        pass


def copy_tree_progress(src: Path, dst: Path, progress: CopyProgress) -> None:
    """Copy a class folder file-by-file, updating shared stick progress."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for path, _size in _iter_copy_files(src):
        rel = path.relative_to(src)
        copy_file_progress(path, dst / rel, progress, rel_name=f"{src.name}/{rel.as_posix()}")


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store", "._*", "__MACOSX"))


def pack_stick(
    *,
    plan: dict,
    stick_id: int,
    volume: Path,
    app_root: Path,
    dry_run: bool = False,
    board: MultiProgressBoard | None = None,
    quiet: bool = False,
) -> dict:
    buckets = {int(b["id"]): b for b in plan["buckets"]}
    if stick_id not in buckets:
        raise ValueError(f"Unknown stick id {stick_id}")
    bucket = buckets[stick_id]
    label = bucket["label"]
    volume = volume.expanduser().resolve()
    if not volume.is_dir():
        raise FileNotFoundError(f"Volume not found: {volume}")

    app_dest = volume / "VoiceoverStation"
    voice_dest = volume / "voiceover"
    readme = volume / "README.txt"
    manifest = volume / "MANIFEST.txt"

    class_names = [c["name"] for c in bucket["classes"]]
    summary = {
        "label": label,
        "volume": str(volume),
        "classes": class_names,
        "size_bytes": bucket["size_bytes"],
        "clip_count": bucket["clip_count"],
        "dry_run": dry_run,
    }
    def log(msg: str) -> None:
        if board is not None:
            board.update(label, msg)
        elif not quiet:
            print(msg)

    if not quiet and board is None:
        print(f"\n=== Packing {label} → {volume} ===")
        print(f"  classes: {len(class_names)}  clips: {bucket['clip_count']}  ~{_fmt_bytes(bucket['size_bytes'])}")
        for name in class_names:
            print(f"    - {name}")

    if dry_run:
        return summary

    free = shutil.disk_usage(volume).free
    need = bucket["size_bytes"] + 80_000_000
    if free < need:
        raise RuntimeError(
            f"Not enough free space on {volume}: need ~{_fmt_bytes(need)}, free {_fmt_bytes(free)}"
        )

    log("copying app…")
    copy_app_bundle(app_root, app_dest)

    readme.write_text(
        student_readme(label, class_names, stick_id, int(plan["sticks"])),
        encoding="utf-8",
    )

    voice_dest.mkdir(parents=True, exist_ok=True)
    footage_total = int(bucket["size_bytes"])
    progress = CopyProgress(
        total_bytes=max(1, footage_total),
        label=label,
        board=board,
        quiet=quiet and board is None,
    )
    log(f"copying footage (~{_fmt_bytes(footage_total)})…")
    for cls in bucket["classes"]:
        src = Path(cls["source"])
        dst = voice_dest / cls["name"]
        copy_tree_progress(src, dst, progress)
    progress.finish()

    manifest.write_text(
        "\n".join(
            [
                f"{label}",
                f"packed_at={_now()}",
                f"volume={volume}",
                f"size_bytes={bucket['size_bytes']}",
                f"clip_count={bucket['clip_count']}",
                "classes:",
                *[f"  {n}" for n in class_names],
                "",
            ]
        ),
        encoding="utf-8",
    )
    log(f"DONE → {volume.name}")
    if board is not None:
        board.finish_slot(label, f"DONE {_fmt_bytes(footage_total)} → {volume.name}")
    elif not quiet:
        print(f"  done → {volume}")
    return summary


def cmd_plan(args: argparse.Namespace) -> int:
    source = Path(args.source)
    classes = scan_classes(source)
    plan = build_plan(classes, args.sticks)
    plan["app_root"] = str(Path(args.app_root).expanduser().resolve())
    out = Path(args.out).expanduser()
    save_plan(plan, out)
    print(f"Planned {plan['class_count']} classes → {plan['sticks']} sticks")
    print(f"Total footage ~{_fmt_bytes(plan['total_bytes'])}")
    print(f"Wrote {out}")
    for b in plan["buckets"]:
        print(
            f"  {b['label']}: {len(b['classes'])} classes, "
            f"{b['clip_count']} clips, ~{_fmt_bytes(b['size_bytes'])}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan))
    filled = plan.get("filled") or {}
    for b in plan["buckets"]:
        mark = "DONE" if b["label"] in filled else "todo"
        print(
            f"{b['label']} [{mark}]  {len(b['classes'])} classes  "
            f"{b['clip_count']} clips  ~{_fmt_bytes(b['size_bytes'])}"
        )
        if args.verbose:
            for c in b["classes"]:
                print(f"    - {c['name']}")
            if b["label"] in filled:
                print(f"    filled → {filled[b['label']]}")
    return 0


def _pick_volume(volumes: list[Path]) -> Path | None:
    if not volumes:
        print("No external volumes found. Plug in a USB and try again.")
        return None
    print("\nMounted volumes:")
    for i, v in enumerate(volumes, 1):
        usage = shutil.disk_usage(v)
        print(f"  {i}) {v}  free={_fmt_bytes(usage.free)}")
    raw = input("Pick volume number (or Enter to cancel): ").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        print("Invalid number")
        return None
    if idx < 1 or idx > len(volumes):
        print("Out of range")
        return None
    return volumes[idx - 1]


def cmd_fill(args: argparse.Namespace) -> int:
    """Pack plugged USBs in parallel (default), or one-at-a-time with --one."""
    plan_path = Path(args.plan).expanduser()
    plan = load_plan(plan_path)
    app_root = Path(args.app_root or plan.get("app_root") or ".").expanduser().resolve()
    filled = plan.setdefault("filled", {})

    if args.one:
        return _cmd_fill_one_at_a_time(args, plan, plan_path, app_root, filled)

    while True:
        todo = [b for b in plan["buckets"] if b["label"] not in filled]
        if not todo:
            print("All sticks in the plan are marked filled.")
            break

        volumes = list_candidate_volumes()
        if not volumes:
            print("No external USB volumes found under /Volumes.")
            print("Plug in sticks, wait for Finder, then press Enter (or q to quit).")
            raw = input("> ").strip().lower()
            if raw in {"q", "quit"}:
                break
            continue

        batch = todo[: len(volumes)]
        pairs = list(zip(batch, volumes, strict=False))

        print("\nStill to pack overall:")
        for b in todo:
            print(
                f"  {b['id']}) {b['label']}  {len(b['classes'])} classes  "
                f"~{_fmt_bytes(b['size_bytes'])}"
            )

        print(f"\nFound {len(volumes)} plugged volume(s). Will pack THESE in parallel:")
        for bucket, volume in pairs:
            free = shutil.disk_usage(volume).free
            print(
                f"  {bucket['label']}  (~{_fmt_bytes(bucket['size_bytes'])})  →  "
                f"{volume}  (free {_fmt_bytes(free)})"
            )
        if len(todo) > len(pairs):
            print(
                f"  ({len(todo) - len(pairs)} stick(s) left for the next batch after these finish)"
            )

        confirm = input("Start parallel copy now? [Y/n] ").strip().lower()
        if confirm in {"n", "no"}:
            print("Cancelled. Plug different sticks or quit.")
            raw = input("Press Enter to rescan, or q to quit: ").strip().lower()
            if raw in {"q", "quit"}:
                break
            continue

        board = MultiProgressBoard([b["label"] for b, _ in pairs])
        print("\nCopying in parallel (one progress line per USB):\n")
        for label in board.slots:
            board.update(label, "starting…")

        errors: list[str] = []
        summaries: list[dict] = []

        def _worker(bucket: dict, volume: Path) -> dict:
            return pack_stick(
                plan=plan,
                stick_id=int(bucket["id"]),
                volume=volume,
                app_root=app_root,
                dry_run=bool(args.dry_run),
                board=board,
                quiet=True,
            )

        with ThreadPoolExecutor(max_workers=max(1, len(pairs))) as pool:
            futures = {
                pool.submit(_worker, bucket, volume): (bucket, volume)
                for bucket, volume in pairs
            }
            for fut in as_completed(futures):
                bucket, volume = futures[fut]
                try:
                    summaries.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{bucket['label']} → {volume}: {exc}")
                    board.finish_slot(bucket["label"], f"ERROR {exc}")

        board.close()

        if not args.dry_run:
            for summary in summaries:
                filled[summary["label"]] = {
                    "volume": summary["volume"],
                    "at": _now(),
                    "classes": summary["classes"],
                }
            plan["updated_at"] = _now()
            save_plan(plan, plan_path)
            print(f"\nSaved progress → {plan_path}")

        if errors:
            print("\nSome sticks failed:")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"\nBatch complete ({len(summaries)} stick(s)).")

        remaining = [b for b in plan["buckets"] if b["label"] not in filled]
        if not remaining:
            print("All sticks are done.")
            break

        print(
            f"\nEject these USBs, plug the next batch "
            f"(up to {len(remaining)} left), then continue."
        )
        raw = input("Press Enter when next USBs are ready (or q to quit): ").strip().lower()
        if raw in {"q", "quit"}:
            break
    return 0


def _cmd_fill_one_at_a_time(
    args: argparse.Namespace,
    plan: dict,
    plan_path: Path,
    app_root: Path,
    filled: dict,
) -> int:
    """Old interactive one-stick mode (`fill --one`)."""
    while True:
        todo = [b for b in plan["buckets"] if b["label"] not in filled]
        if not todo:
            print("All sticks in the plan are marked filled.")
            break
        print("\nStill to pack:")
        for b in todo:
            print(
                f"  {b['id']}) {b['label']}  {len(b['classes'])} classes  "
                f"~{_fmt_bytes(b['size_bytes'])}"
            )
        raw = input("Enter stick number to pack now (or Enter to quit): ").strip()
        if not raw:
            break
        try:
            stick_id = int(raw)
        except ValueError:
            print("Invalid number")
            continue
        volumes = list_candidate_volumes()
        volume = _pick_volume(volumes)
        if volume is None:
            continue
        confirm = input(
            f"Copy {plan['buckets'][stick_id - 1]['label']} onto {volume}? [y/N] "
        ).strip().lower()
        if confirm not in {"y", "yes"}:
            print("Skipped")
            continue
        try:
            summary = pack_stick(
                plan=plan,
                stick_id=stick_id,
                volume=volume,
                app_root=app_root,
                dry_run=bool(args.dry_run),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")
            continue
        if not args.dry_run:
            filled[summary["label"]] = {
                "volume": summary["volume"],
                "at": _now(),
                "classes": summary["classes"],
            }
            plan["updated_at"] = _now()
            save_plan(plan, plan_path)
            print(f"Saved progress → {plan_path}")
            print("You can eject this USB and plug the next one.")
    return 0


def cmd_update_app(args: argparse.Namespace) -> int:
    """Replace VoiceoverStation/ on all plugged USBs (footage untouched)."""
    app_root = Path(args.app_root).expanduser().resolve()
    volumes = list_candidate_volumes()
    if not volumes:
        print("No external USB volumes found. Plug sticks in and retry.")
        return 1
    print(f"Will update VoiceoverStation on {len(volumes)} volume(s):")
    for v in volumes:
        print(f"  - {v}")
    confirm = input("Replace app folders now? [Y/n] ").strip().lower()
    if confirm in {"n", "no"}:
        print("Cancelled")
        return 0
    for volume in volumes:
        dest = volume / "VoiceoverStation"
        print(f"Updating {dest} …")
        try:
            copy_app_bundle(app_root, dest)
            print(f"  OK → {dest}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {volume}: {exc}")
    print("Done. Footage folders were not modified.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pack Voiceover Station + footage onto USB sticks")
    sub = p.add_subparsers(dest="cmd", required=True)

    plan = sub.add_parser("plan", help="Create balanced USB assignment JSON")
    plan.add_argument(
        "--source",
        default="/Users/faz/wc-sample-30h/videos",
        help="Folder of class subfolders with MP4s",
    )
    plan.add_argument("--sticks", type=int, default=10)
    plan.add_argument(
        "--app-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Footage cleaning repo root (Voiceover Station source)",
    )
    plan.add_argument(
        "--out",
        default=str(Path.home() / "wc-sample-30h" / "usb_plan.json"),
        help="Where to write the plan JSON",
    )
    plan.set_defaults(func=cmd_plan)

    show = sub.add_parser("show", help="Print plan + fill status")
    show.add_argument("--plan", default=str(Path.home() / "wc-sample-30h" / "usb_plan.json"))
    show.add_argument("-v", "--verbose", action="store_true")
    show.set_defaults(func=cmd_show)

    fill = sub.add_parser(
        "fill",
        help="Copy onto all plugged USBs in parallel (next N unfilled sticks)",
    )
    fill.add_argument("--plan", default=str(Path.home() / "wc-sample-30h" / "usb_plan.json"))
    fill.add_argument("--app-root", default="")
    fill.add_argument("--dry-run", action="store_true")
    fill.add_argument(
        "--one",
        action="store_true",
        help="Old mode: pack one stick at a time interactively",
    )
    fill.set_defaults(func=cmd_fill)

    upd = sub.add_parser(
        "update-app",
        help="Replace VoiceoverStation app on plugged USBs (keep voiceover footage)",
    )
    upd.add_argument(
        "--app-root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Repo root with built gopro_cleaner/web",
    )
    upd.set_defaults(func=cmd_update_app)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
