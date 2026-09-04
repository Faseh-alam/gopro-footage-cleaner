"""Wipe-safety regression tests.

Copy/embed/collision-rename stay as they are. Wipe must never delete an MP4
that was not copied and verified.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from unittest.mock import patch

from offloader import eject, embed_meta, engine, inventory  # noqa: E402
from offloader import pairing  # noqa: E402
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


def _copy_files(files: list[dict], dest: Path, card_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with engine._dest_batch_lock(dest):
        engine._resolve_dest_names(files, dest, {"files": {}}, card_id)
    for item in files:
        dest_rel = engine._flat_name(item.get("dest_rel") or item["rel"])
        item["dest_rel"] = dest_rel
        dest_file = dest / dest_rel
        if item.get("already_in_batch") and dest_file.is_file():
            item["copied"] = False
            item["dest_size"] = dest_file.stat().st_size
            if inventory._item_is_mp4(item):
                engine._ensure_dest_sidecar(dest_file, item.get("embed_json"))
            continue
        copy_file(Path(item["source"]), dest_file)
        item["copied"] = True
        item["dest_size"] = dest_file.stat().st_size
        if inventory._item_is_mp4(item):
            engine._ensure_dest_sidecar(dest_file, item.get("embed_json"))
    transferred = {
        engine._flat_name(item.get("dest_rel") or item["rel"])
        for item in files
        if inventory._item_is_mp4(item)
    }
    missing = [
        name
        for name in sorted(transferred)
        if inventory.sidecar_for_mp4(dest / name) is None
    ]
    if missing:
        raise RuntimeError(f"dest MP4s missing JSON sidecar: {missing}")


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
        copied = bool(item.get("copied", True))
        already = bool(item.get("already_in_batch"))
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
                "wipe": copied and not already,
                "already_in_batch": already,
            }
        )
    return rows


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


def _stamp_identity(mp4: Path, serial: str) -> None:
    side = mp4.with_suffix(".json")
    side.write_text(
        json.dumps(
            {
                "source": mp4.name,
                "size_bytes": mp4.stat().st_size,
                "complete": True,
                "device_id": serial,
                "card_badge": serial,
                "media_meta": {
                    "camera_serial": serial,
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                },
                "segments": [{"kind": "work", "task": "x", "start": 0, "end": 1}],
            }
        ),
        encoding="utf-8",
    )


def test_duplicate_card_skips_copy_and_wipe() -> None:
    print("\n[9] duplicate card (same videos already in batch) — skip copy, do not wipe")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        card1 = Path(tmp) / "card1"
        card2 = Path(tmp) / "card2"
        specs = [
            ("GX010001.MP4", b"same-video-body-1111"),
            ("GX010002.MP4", b"same-video-body-2222"),
        ]
        gopro1 = _card_with_mp4s(card1, specs)
        gopro2 = _card_with_mp4s(card2, specs)
        _stamp_identity(gopro1 / "GX010001.MP4", "CAM-A")
        _stamp_identity(gopro1 / "GX010002.MP4", "CAM-A")
        _stamp_identity(gopro2 / "GX010001.MP4", "CAM-A")
        _stamp_identity(gopro2 / "GX010002.MP4", "CAM-A")
        files1 = inventory.list_transfer_files(card1)
        _copy_files(files1, dest, "SD-1")
        dest_mp4s_before = sorted(p.name for p in dest.iterdir() if p.suffix.upper() == ".MP4")
        eject.wipe_verified_sources(card1, [f["source"] for f in files1])
        files2 = inventory.list_transfer_files(card2)
        _copy_files(files2, dest, "SD-2")
        mp4s2 = [f for f in files2 if f.get("kind") == "mp4"]
        check("both MP4s marked already_in_batch", all(f.get("already_in_batch") for f in mp4s2))
        check("second card did not copy", all(f.get("copied") is False for f in mp4s2))
        dest_mp4s_after = sorted(p.name for p in dest.iterdir() if p.suffix.upper() == ".MP4")
        check(
            "SSD still has only the original two MP4s",
            dest_mp4s_before == dest_mp4s_after,
            str(dest_mp4s_after),
        )
        check("no -1 copy for the duplicate card", not any("-1." in n for n in dest_mp4s_after))
        manifest2 = _manifest_for(files2, dest)
        wipe_sources = [r["source"] for r in manifest2 if r.get("wipe")]
        check("wipe list is empty", wipe_sources == [])
        snapshot = list(inventory.list_card_mp4_paths(card2))
        check("duplicate card still has both MP4s", len(snapshot) == 2, str(snapshot))
        check(
            "duplicate card still has JSON sidecars",
            (gopro2 / "GX010001.json").is_file() and (gopro2 / "GX010002.json").is_file(),
        )


def test_labeled_duplicate_does_not_hash() -> None:
    print("\n[9b] labeled same-video skip never SHA-256 hashes")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        card1 = Path(tmp) / "card1"
        card2 = Path(tmp) / "card2"
        specs = [("GX010001.MP4", b"labeled-same-video-XXXX")]
        gopro1 = _card_with_mp4s(card1, specs)
        gopro2 = _card_with_mp4s(card2, specs)
        _stamp_identity(gopro1 / "GX010001.MP4", "CAM-A")
        _stamp_identity(gopro2 / "GX010001.MP4", "CAM-A")
        files1 = inventory.list_transfer_files(card1)
        _copy_files(files1, dest, "SD-1")
        with patch.object(pairing, "file_digest") as digest:
            digest.side_effect = AssertionError("SHA-256 must not run for labeled same-video skip")
            files2 = inventory.list_transfer_files(card2)
            _copy_files(files2, dest, "SD-2")
            mp4 = next(f for f in files2 if f.get("kind") == "mp4")
            check("duplicate skipped by identity", bool(mp4.get("already_in_batch")))
            check("file_digest was not called", digest.call_count == 0, str(digest.call_count))


def test_same_name_different_identity_suffix_copy() -> None:
    print("\n[10] same filename, different sidecar identity → suffix copy + wipe the new card")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        card1 = Path(tmp) / "card1"
        card2 = Path(tmp) / "card2"
        gopro1 = _card_with_mp4s(card1, [("GX010001.MP4", b"video-from-card-AAAA")])
        gopro2 = _card_with_mp4s(card2, [("GX010001.MP4", b"video-from-card-BBBB")])
        _stamp_identity(gopro1 / "GX010001.MP4", "CAM-A")
        _stamp_identity(gopro2 / "GX010001.MP4", "CAM-B")
        files1 = inventory.list_transfer_files(card1)
        _copy_files(files1, dest, "SD-A")
        eject.wipe_verified_sources(card1, [f["source"] for f in files1])
        files2 = inventory.list_transfer_files(card2)
        _copy_files(files2, dest, "SD-B")
        mp4_b = next(f for f in files2 if f.get("kind") == "mp4")
        check("second video is not treated as already in batch", not mp4_b.get("already_in_batch"))
        check(
            "second video saved as -1",
            mp4_b.get("dest_rel") == "GX010001-1.MP4",
            str(mp4_b.get("dest_rel")),
        )
        check(
            "both dest MP4s exist",
            (dest / "GX010001.MP4").is_file() and (dest / "GX010001-1.MP4").is_file(),
        )
        check(
            "dest bodies differ",
            (dest / "GX010001.MP4").read_bytes() != (dest / "GX010001-1.MP4").read_bytes(),
        )
        manifest2 = _manifest_for(files2, dest)
        try:
            eject.assert_wipe_allowed(card2, manifest2)
            blocked = False
            msg = ""
        except eject.WipeBlocked as exc:
            blocked = True
            msg = str(exc)
        check("wipe allowed for the new identity", not blocked, msg)
        eject.wipe_verified_sources(card2, [r["source"] for r in manifest2 if r.get("wipe")])
        check("second card wiped after suffix copy", inventory.list_card_mp4_paths(card2) == [])


def test_numeric_suffix_chain_and_two_ssds() -> None:
    print("\n[11] collisions use -1/-2; existing -1 kept; two SSDs stay independent")
    with tempfile.TemporaryDirectory() as tmp:
        dest_a = Path(tmp) / "ssdA" / "batch01"
        dest_b = Path(tmp) / "ssdB" / "batch01"
        card1 = Path(tmp) / "card1"
        card2 = Path(tmp) / "card2"
        card3 = Path(tmp) / "card3"
        gopro1 = _card_with_mp4s(card1, [("GX010001.MP4", b"video-from-card-AAAA")])
        gopro2 = _card_with_mp4s(card2, [("GX010001.MP4", b"video-from-card-BBBB")])
        gopro3 = _card_with_mp4s(card3, [("GX010001.MP4", b"video-from-card-CCCC")])
        _stamp_identity(gopro1 / "GX010001.MP4", "CAM-A")
        _stamp_identity(gopro2 / "GX010001.MP4", "CAM-B")
        _stamp_identity(gopro3 / "GX010001.MP4", "CAM-C")

        files1 = inventory.list_transfer_files(card1)
        _copy_files(files1, dest_a, "SD-A")
        files2 = inventory.list_transfer_files(card2)
        _copy_files(files2, dest_a, "SD-B")
        body_minus_1 = (dest_a / "GX010001-1.MP4").read_bytes()
        files3 = inventory.list_transfer_files(card3)
        _copy_files(files3, dest_a, "SD-C")
        mp4_c = next(f for f in files3 if f.get("kind") == "mp4")
        check("third different video is GX010001-2.MP4", mp4_c.get("dest_rel") == "GX010001-2.MP4",
              str(mp4_c.get("dest_rel")))
        check(
            "all three dest names exist on SSD A",
            (dest_a / "GX010001.MP4").is_file()
            and (dest_a / "GX010001-1.MP4").is_file()
            and (dest_a / "GX010001-2.MP4").is_file(),
        )
        check("existing -1 was not overwritten", (dest_a / "GX010001-1.MP4").read_bytes() == body_minus_1)

        files1b = inventory.list_transfer_files(card1)
        _copy_files(files1b, dest_b, "SD-A")
        mp4_b = next(f for f in files1b if f.get("kind") == "mp4")
        check("SSD B still uses the original name", mp4_b.get("dest_rel") == "GX010001.MP4")
        check("SSD B has no -1", not (dest_b / "GX010001-1.MP4").exists())
        check(
            "two SSD batch folders are independent",
            (dest_b / "GX010001.MP4").is_file() and not (dest_b / "GX010001-2.MP4").exists(),
        )


def test_unclear_dest_hash_same_bytes_skips() -> None:
    print("\n[12] dest has no sidecar/embed — same bytes → hash skip, no copy")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        dest.mkdir()
        body = b"ftypmdat-same-bytes-payload-XXXX"
        (dest / "GX010001.MP4").write_bytes(body)
        card = Path(tmp) / "card"
        _card_with_mp4s(card, [("GX010001.MP4", body)])
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-H")
        mp4 = next(f for f in files if f.get("kind") == "mp4")
        check("hash match marked already_in_batch", bool(mp4.get("already_in_batch")))
        check("reuses original dest name", mp4.get("dest_rel") == "GX010001.MP4", str(mp4.get("dest_rel")))
        check("was not copied again", mp4.get("copied") is False)
        check("no -1 created", not (dest / "GX010001-1.MP4").exists())


def test_unclear_dest_hash_different_bytes_renames() -> None:
    print("\n[13] dest has no sidecar/embed — different bytes, same size → -1")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        dest.mkdir()
        body_a = b"AAAA" * 40
        body_b = b"BBBB" * 40
        (dest / "GX010001.MP4").write_bytes(body_a)
        card = Path(tmp) / "card"
        _card_with_mp4s(card, [("GX010001.MP4", body_b)])
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-H")
        mp4 = next(f for f in files if f.get("kind") == "mp4")
        check("not treated as the same video", not mp4.get("already_in_batch"))
        check("saved as -1", mp4.get("dest_rel") == "GX010001-1.MP4", str(mp4.get("dest_rel")))
        check("original dest body unchanged", (dest / "GX010001.MP4").read_bytes() == body_a)
        check("new dest has card bytes", (dest / "GX010001-1.MP4").read_bytes() == body_b)


def test_legacy_cardid_name_still_matched() -> None:
    print("\n[14] legacy GX010001__C5678.MP4 still counts as the same video")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        dest.mkdir()
        body = b"legacy-same-video-bytes-YYYY"
        dest_mp4 = dest / "GX010001__C5678.MP4"
        dest_mp4.write_bytes(body)
        (dest / "GX010001__C5678.json").write_text(
            json.dumps(
                {
                    "source": "GX010001.MP4",
                    "size_bytes": len(body),
                    "complete": True,
                    "device_id": "CAM-A",
                    "card_badge": "CAM-A",
                    "media_meta": {
                        "camera_serial": "CAM-A",
                        "recorded_at": "2026-01-01T00:00:00+00:00",
                    },
                    "segments": [{"kind": "work", "task": "x", "start": 0, "end": 1}],
                }
            ),
            encoding="utf-8",
        )
        card = Path(tmp) / "card"
        gopro = _card_with_mp4s(card, [("GX010001.MP4", body)])
        _stamp_identity(gopro / "GX010001.MP4", "CAM-A")
        files = inventory.list_transfer_files(card)
        _copy_files(files, dest, "SD-L")
        mp4 = next(f for f in files if f.get("kind") == "mp4")
        check("reuses legacy dest name", mp4.get("dest_rel") == "GX010001__C5678.MP4",
              str(mp4.get("dest_rel")))
        check("skipped copy", bool(mp4.get("already_in_batch")) and mp4.get("copied") is False)
        check("did not also write GX010001.MP4", not (dest / "GX010001.MP4").exists())


def test_skip_backfills_json_and_embed() -> None:
    print("\n[15] dest MP4 with no JSON/embed — skip copy, still attach sidecar + embed")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "batch"
        dest.mkdir()
        ftyp = struct.pack(">I", 20) + b"ftypisom" + struct.pack(">I", 512) + b"isom"
        mdat = struct.pack(">I", 8 + 100) + b"mdat" + b"\x00" * 100
        body = ftyp + mdat
        dest_mp4 = dest / "GX010001.MP4"
        dest_mp4.write_bytes(body)
        card = Path(tmp) / "card"
        gopro = _card_with_mp4s(card, [("GX010001.MP4", body)])
        _stamp_identity(gopro / "GX010001.MP4", "CAM-A")
        files = inventory.list_transfer_files(card)
        mp4 = next(f for f in files if f.get("kind") == "mp4")
        engine._preserve_dest_metadata(
            "SD-H", mp4["rel"], Path(mp4["source"]), dest_mp4, mp4.get("embed_json") or ""
        )
        side = inventory.sidecar_for_mp4(dest_mp4)
        check("JSON written beside dest MP4", bool(side and side.is_file()), str(side))
        embedded = embed_meta.read_embedded_segments(dest_mp4)
        check(
            "metadata embedded into dest MP4",
            bool(embedded) and embedded.get("device_id") == "CAM-A",
            str(embedded),
        )
        check(
            "sidecar JSON still has camera identity",
            bool(side) and json.loads(side.read_text(encoding="utf-8")).get("device_id") == "CAM-A",
        )


def main() -> int:
    test_six_copied_wipe_allowed()
    test_four_of_six_wipe_blocked()
    test_nested_100gopro_discovered()
    test_missing_json_card_untouched()
    test_duplicate_filenames_kept_separate()
    test_wrong_size_blocks_wipe()
    test_non_mp4_does_not_block()
    test_no_rmtree_on_gopro_folder()
    test_duplicate_card_skips_copy_and_wipe()
    test_labeled_duplicate_does_not_hash()
    test_same_name_different_identity_suffix_copy()
    test_numeric_suffix_chain_and_two_ssds()
    test_unclear_dest_hash_same_bytes_skips()
    test_unclear_dest_hash_different_bytes_renames()
    test_legacy_cardid_name_still_matched()
    test_skip_backfills_json_and_embed()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
