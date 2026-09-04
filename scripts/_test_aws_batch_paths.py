"""Quick checks for flat AWS batch upload path helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

from offloader import aws_upload as a  # noqa: E402


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


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

    zero = a._parse_s5cmd_du_output("0 bytes in 0 objects: s3://bucket/prefix/")
    assert zero == (0, 0), zero
    real = a._parse_s5cmd_du_output("251353320 bytes in 3 objects: s3://bucket/prefix/*")
    assert real == (251353320, 3), real
    print("parse s5cmd du OK")

    dest = "s3://bucket/footage/batch01/"

    def du_zero_then_wildcard(cmd, **_kwargs):
        target = cmd[-1]
        if cmd[:2] == ["s5cmd", "du"] and target.endswith("*"):
            return _Proc("251353320 bytes in 3 objects: s3://bucket/footage/batch01/*")
        if cmd[:2] == ["s5cmd", "du"]:
            return _Proc("0 bytes in 0 objects: s3://bucket/footage/batch01/")
        raise AssertionError(f"unexpected command {cmd}")

    with patch.object(a, "s5cmd_available", return_value=True), patch.object(
        a, "aws_cli_available", return_value=False
    ), patch.object(a.subprocess, "run", side_effect=du_zero_then_wildcard):
        summary = a._s3_prefix_summary(dest)
    assert summary == (251353320, 3), summary
    print("s5cmd du zero-prefix falls back to wildcard OK")

    def du_zero_then_aws(cmd, **_kwargs):
        if cmd[:2] == ["s5cmd", "du"]:
            return _Proc("0 bytes in 0 objects: s3://bucket/footage/batch01/")
        if cmd[:3] == ["aws", "s3", "ls"]:
            return _Proc("Total Size: 251353320\nTotal Objects: 3\n")
        raise AssertionError(f"unexpected command {cmd}")

    with patch.object(a, "s5cmd_available", return_value=True), patch.object(
        a, "aws_cli_available", return_value=True
    ), patch.object(a.subprocess, "run", side_effect=du_zero_then_aws):
        summary = a._s3_prefix_summary(dest)
    assert summary == (251353320, 3), summary
    print("s5cmd du zero falls back to aws ls summarize OK")

    timeouts: list[object] = []

    def capture_timeout(cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if cmd[:2] == ["s5cmd", "du"]:
            return _Proc("100 bytes in 1 objects: s3://bucket/footage/batch01/")
        if cmd[:2] == ["s5cmd", "ls"]:
            return _Proc("2026-01-01 00:00:00 100 s3://bucket/footage/batch01/GX010001.MP4")
        if cmd[:3] == ["aws", "s3", "ls"]:
            return _Proc("Total Size: 100\nTotal Objects: 1\n")
        raise AssertionError(f"unexpected command {cmd}")

    with patch.object(a, "s5cmd_available", return_value=True), patch.object(
        a, "aws_cli_available", return_value=True
    ), patch.object(a.subprocess, "run", side_effect=capture_timeout):
        a._s3_prefix_summary(dest)
        a.list_s3_object_sizes(dest)
    assert timeouts, "expected listing commands"
    assert all(t == a.AWS_VERIFY_TIMEOUT_SECONDS for t in timeouts), timeouts
    assert a.AWS_VERIFY_TIMEOUT_SECONDS == 30 * 60
    print("verify listing timeout is 1800s OK")

    print("ALL PATH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
