"""Quick checks for flat AWS batch upload path helpers."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

from offloader import aws_upload as a  # noqa: E402


def main() -> int:
    assert a.batch_s3_prefix("s3://bucket/footage/", "batch 1") == "s3://bucket/footage/batch 1/"
    assert a.batch_s3_prefix("s3://bucket/footage", "batch 1") == "s3://bucket/footage/batch 1/"
    assert a.batch_s3_prefix("s3://bucket/footage/batch 1/", "batch 1") == "s3://bucket/footage/batch 1/"
    assert a.batch_s3_prefix("s3://bucket/footage/batch 1", "batch 1") == "s3://bucket/footage/batch 1/"
    print("batch_s3_prefix OK")

    local = a._sync_local_arg(Path(r"E:\Batches\batch 1"))
    assert local.replace("\\", "/").endswith("batch 1/")
    print("sync_local_arg OK:", local)

    cmd = "s5cmd sync 'E:/Batches/batch 1/' 's3://bucket/footage/batch 1/'"
    src, dest, batch = a._parse_sync_cmdline(cmd)
    print("parsed single-quote:", src, dest, batch)
    assert batch == "batch 1", batch
    assert dest == "s3://bucket/footage/batch 1/", dest

    cmd2 = 'aws s3 sync "E:/Batches/batch 1/" "s3://bucket/footage/batch 1/"'
    src2, dest2, batch2 = a._parse_sync_cmdline(cmd2)
    print("parsed double-quote:", src2, dest2, batch2)
    assert batch2 == "batch 1"
    assert dest2 == "s3://bucket/footage/batch 1/"

    m = a._BATCH_IN_PATH_RE.search(r"E:\Batches\batch 1\GX01.MP4")
    assert m and m.group(1) == "batch 1", m.group(1) if m else None
    print("BATCH_IN_PATH OK")

    print("ALL PATH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
