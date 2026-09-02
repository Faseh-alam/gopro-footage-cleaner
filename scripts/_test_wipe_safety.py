"""Wipe-safety regression tests.

Copy/embed/collision-rename stay as they are. Wipe must never delete an MP4
that was not copied and verified.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from offloader import eject, engine, inventory  # noqa: E402
from offloader.transfer import copy_file  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _write_mp4(path: Path, payload: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if payload is not None else b"ftypmdat" + path.name.encode())


def _write_json(mp4: Path) -> Path:
    side = mp4.with_suffix(".json")
    side.write_text(
        json.dumps({"source": mp4.name, "size_bytes": mp4.stat().st_size, "complete": True}),
        encoding="utf-8",
    )
    return side


def _card_with_mp4s(root: Path, specs: list[tuple[str, bytes | None]]) -> Path:
    """specs: relative path under DCIM/100GOPRO, optional unique payload."""
    gopro = root / "DCIM" / "100GOPRO"
    gopro.mkdir(parents=True)
    (gopro / "GX010001.THM").write_bytes(b"thm")
    for rel, payload in specs:
        mp4 = gopro / rel
        _write_mp4(mp4, payload)
        _write_json(mp4)
    return gopro


def _manifest_for(files: list[dict], dest: Path, *, verified: bool | None = None) -> list[dict]:
    rows = []
    for item in files:
        dest_rel = engine._flat_name(item.get("dest_rel") or item["rel"])
        dest_file = dest / dest_rel
        ok = dest_file.is_file() if verified is None else verified
        if verified is None and dest_file.is_file():
            try:
                ok = dest_file.stat().st_size == int(item.get("dest_size") or item["size"])
            except OSError:
                ok = False
        rows.append(
            {
                "source": item["source"],
                "rel": item["rel"],
                "dest_rel": dest_rel,
                "dest": str(dest_file),
                "size": int(item["size"]),
                "dest_size": int(item.get("dest_size") or item["size"]),
                "kind": item.get("kind") or "mp4",
                "verified": bool(ok),
            }
        )
    return rows


def _copy_files(files: list[dict], dest: Path, card_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with engine._dest_batch_lock(dest):
        engine._resolve_dest_names(files, dest, {"files": {}}, card_id)
    for item in files:
        dest_rel = engine._flat_name(item.get("dest_rel") or item["rel"])
        item["dest_rel"] = dest_rel
        dest_file = dest / dest_rel
        copy_file(Path(item["source"]), dest_file)
        item["dest_size"] = dest_file.stat().st_size


def test_six_copied_wipe_allowed() -> None:
    print("\n[1] 6 MP4s all copied → wipe allowed")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        specs = [(f"GX01000{i}.MP4", None) for i in range(1, 7)]
        gopro = _card_with_mp4s(card, specs)
        files = inventory.list_transfer_files(card)
        mp4s = [f for f in files if f.get("kind") == "mp4"]
        check("discovered 6 MP4s", len(mp4s) == 6, str(len(mp4s)))
        check("no unpaired", inventory.unpaired_mp4s(files) == [])
        _copy_files(files, dest, "SD-A")
        manifest = _manifest_for(files, dest)
        try:
            eject.assert_wipe_allowed(card, manifest)
            blocked = False
        except eject.WipeBlocked:
            blocked = True
        check("wipe allowed after 6/6 verify", not blocked)
        before_thm = (gopro / "GX010001.THM").is_file()
        eject.wipe_verified_sources(card, [m["source"] for m in manifest])
        left = inventory.list_card_mp4_paths(card)
        check("all 6 MP4s deleted", left == [], str(left))
        check("THM leftover does not block (still present)", before_thm and (gopro / "GX010001.THM").is_file())


def test_four_of_six_wipe_blocked() -> None:
    print("\n[2] 6 MP4s but only 4 in manifest → wipe MUST be blocked")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        specs = [(f"GX01000{i}.MP4", None) for i in range(1, 7)]
        _card_with_mp4s(card, specs)
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-B")
        mp4s = [f for f in files if f.get("kind") == "mp4"][:4]
        keep = set()
        for row in mp4s:
            keep.add(str(Path(row["source"]).resolve()).lower())
            if row.get("embed_json"):
                keep.add(str(Path(row["embed_json"]).resolve()).lower())
        partial = [f for f in files if str(Path(f["source"]).resolve()).lower() in keep]
        manifest = _manifest_for(partial, dest)
        snapshot = [p for p in inventory.list_card_mp4_paths(card)]
        try:
            eject.assert_wipe_allowed(card, manifest)
            blocked = False
            msg = ""
        except eject.WipeBlocked as exc:
            blocked = True
            msg = str(exc)
        check("wipe blocked when 2 MP4s undiscovered", blocked, msg)
        check("card MP4s untouched", inventory.list_card_mp4_paths(card) == snapshot)
        check("message names remaining count", "2 MP4" in msg, msg)


def test_nested_100gopro_discovered() -> None:
    print("\n[3] MP4 inside nested 100GOPRO is discovered and copied")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        _card_with_mp4s(
            card,
            [
                ("GX010001.MP4", None),
                ("100GOPRO/GX101.MP4", b"nested-unique"),
                ("100GOPRO/100GOPRO/GX102.MP4", b"deep-unique"),
            ],
        )
        files = inventory.list_transfer_files(card)
        mp4s = [f for f in files if f.get("kind") == "mp4"]
        rels = sorted(f["rel"].replace("\\", "/") for f in mp4s)
        check("nested + deep MP4s listed", "100GOPRO/GX101.MP4" in rels and "100GOPRO/100GOPRO/GX102.MP4" in rels, str(rels))
        _copy_files(files, dest, "SD-C")
        check("nested copied flat", (dest / "GX101.MP4").is_file())
        check("deep copied flat", (dest / "GX102.MP4").is_file() or any(p.name.startswith("GX102") for p in dest.iterdir()))
        check("no 100GOPRO dest folder", not (dest / "100GOPRO").exists())
        manifest = _manifest_for(files, dest)
        eject.assert_wipe_allowed(card, manifest)
        eject.wipe_verified_sources(card, [m["source"] for m in manifest])
        check("nested source MP4s wiped", inventory.leftover_mp4s(card, []) == inventory.list_card_mp4_paths(card) and not inventory.list_card_mp4_paths(card))


def test_missing_json_card_untouched() -> None:
    print("\n[4] missing JSON for any MP4 → card remains untouched")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        gopro = card / "DCIM" / "100GOPRO"
        gopro.mkdir(parents=True)
        for i in range(1, 4):
            mp4 = gopro / f"GX01000{i}.MP4"
            _write_mp4(mp4)
            if i != 2:
                _write_json(mp4)
        files = inventory.list_transfer_files(card)
        missing = inventory.unpaired_mp4s(files)
        check("unpaired flags the JSON-less MP4", any("GX010002" in m for m in missing), str(missing))
        before = {p.name: p.stat().st_size for p in inventory.list_card_mp4_paths(card)}
        # Even a fully-verified-looking 2-file manifest must not wipe while GX010002 remains.
        paired = [f for f in files if "GX010002" not in str(f.get("rel"))]
        dest = Path(tmp) / "batch"
        _copy_files(paired, dest, "SD-D")
        manifest = _manifest_for(paired, dest)
        try:
            eject.assert_wipe_allowed(card, manifest)
            blocked = False
            msg = ""
        except eject.WipeBlocked as exc:
            blocked = True
            msg = str(exc)
        check("wipe blocked because unpaired MP4 remains", blocked, msg)
        after = {p.name: p.stat().st_size for p in inventory.list_card_mp4_paths(card)}
        check("all 3 source MP4s still on card", before == after, str(after))


def test_duplicate_filenames_kept_separate() -> None:
    print("\n[5] duplicate filenames in different folders are separate transfers")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        _card_with_mp4s(
            card,
            [
                ("GX101.MP4", b"root-file-AAAAAAAA"),
                ("100GOPRO/GX101.MP4", b"nested-file-BBBBBBBB"),
            ],
        )
        files = inventory.list_transfer_files(card)
        mp4s = [f for f in files if f.get("kind") == "mp4"]
        check("two MP4 rows", len(mp4s) == 2, str(len(mp4s)))
        check("distinct source paths", mp4s[0]["source"] != mp4s[1]["source"])
        _copy_files(files, dest, "SD-E")
        dest_rels = sorted(engine._flat_name(f.get("dest_rel") or f["rel"]) for f in mp4s)
        check("two distinct dest names", len(set(dest_rels)) == 2, str(dest_rels))
        dest_files = [dest / n for n in dest_rels]
        check("both dest files exist", all(p.is_file() for p in dest_files))
        bodies = {p.read_bytes() for p in dest_files}
        check("dest bodies are different", len(bodies) == 2)
        manifest = _manifest_for(files, dest)
        eject.assert_wipe_allowed(card, manifest)
        eject.wipe_verified_sources(card, [m["source"] for m in manifest])
        check("both sources wiped", inventory.list_card_mp4_paths(card) == [])


def test_wrong_size_blocks_wipe() -> None:
    print("\n[6] dest missing/wrong size → wipe MUST be blocked")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        _card_with_mp4s(card, [(f"GX01000{i}.MP4", None) for i in range(1, 4)])
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-F")
        mp4_item = next(f for f in files if f.get("kind") == "mp4")
        dest_file = dest / engine._flat_name(mp4_item.get("dest_rel") or mp4_item["rel"])
        dest_file.write_bytes(b"truncated")
        manifest = _manifest_for(files, dest)
        bad = [r for r in manifest if r["dest_rel"] == dest_file.name]
        check("verify marks truncated dest unverified", bad and not bad[0]["verified"])
        snapshot = list(inventory.list_card_mp4_paths(card))
        try:
            eject.assert_wipe_allowed(card, manifest)
            blocked = False
            msg = ""
        except eject.WipeBlocked as exc:
            blocked = True
            msg = str(exc)
        check("wipe blocked on unverified dest", blocked, msg)
        check("card still has original MP4s", inventory.list_card_mp4_paths(card) == snapshot)


def test_non_mp4_does_not_block() -> None:
    print("\n[7] unrelated non-MP4 files may remain without blocking wipe")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        gopro = _card_with_mp4s(card, [("GX010001.MP4", None)])
        (gopro / "GX010001.LRV").write_bytes(b"lrv")
        (gopro / "MISC.TXT").write_text("notes", encoding="utf-8")
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-G")
        manifest = _manifest_for(files, dest)
        try:
            eject.assert_wipe_allowed(card, manifest)
            blocked = False
            msg = ""
        except eject.WipeBlocked as exc:
            blocked = True
            msg = str(exc)
        check("wipe allowed with THM/LRV/TXT leftover", not blocked, msg)
        eject.wipe_verified_sources(card, [m["source"] for m in manifest])
        check("LRV still on card", (gopro / "GX010001.LRV").is_file())
        check("TXT still on card", (gopro / "MISC.TXT").is_file())
        check("THM still on card", (gopro / "GX010001.THM").is_file())
        check("MP4+JSON removed", not (gopro / "GX010001.MP4").is_file())


def test_no_rmtree_on_gopro_folder() -> None:
    print("\n[8] wipe unlinks files only — 100GOPRO folder is not rmtree'd")
    with tempfile.TemporaryDirectory() as tmp:
        card = Path(tmp) / "card"
        dest = Path(tmp) / "batch"
        gopro = _card_with_mp4s(card, [("100GOPRO/GX101.MP4", None)])
        nested = gopro / "100GOPRO"
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-H")
        manifest = _manifest_for(files, dest)
        eject.assert_wipe_allowed(card, manifest)
        eject.wipe_verified_sources(card, [m["source"] for m in manifest])
        check("nested 100GOPRO directory still exists", nested.is_dir())
        check("file inside was unlinked", not (nested / "GX101.MP4").exists())


def main() -> int:
    test_six_copied_wipe_allowed()
    test_four_of_six_wipe_blocked()
    test_nested_100gopro_discovered()
    test_missing_json_card_untouched()
    test_duplicate_filenames_kept_separate()
    test_wrong_size_blocks_wipe()
    test_non_mp4_does_not_block()
    test_no_rmtree_on_gopro_folder()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
