# SD Card Offloader

24/7 server tool: plug labeled GoPro SD cards → copy raw MP4s + their `.segments.json` label files into a batch folder on dual removable SSDs → optional AWS S3 sync. Multi-card parallel transfers with live progress and resume.

## What it copies

From each card (labeled with GoPro Cleaner — videos stay untrimmed on the card):

```text
C1234/
  DCIM/
    100GOPRO/
      GX010001.MP4
      GX010001.segments.json   ← tasks + timestamps + camera/IMU metadata
      GX010002.MP4
      ...
```

Onto SSD — all cards flat into the batch folder (no per-card subfolder):

```text
<SSD>/Batches/batch 6/GX010001.MP4          ← segments JSON also EMBEDDED inside
<SSD>/Batches/batch 6/GX010001.segments.json
<SSD>/Batches/batch 6/GX010001-1.MP4        ← same filename, different video → -1, -2, …
<SSD>/Batches/batch 6/GX010001-1.segments.json
```

GoPro numbering repeats across cards (every card has a `GX010001.MP4`), so
when a name is already taken by a *different* video, the incoming pair is
renamed `GX010001-1.MP4`, then `-2`, and so on — nothing is ever overwritten.
If the batch already holds the **same** video (JSON + size + embed), that
file is skipped. A full-file hash is used only when metadata is missing.

While copying, each MP4's segments JSON is **copied beside that dest MP4**
(same stem: `GX010001-1.MP4` + `GX010001-1.segments.json`) and **embedded into
the SSD copy of the MP4 itself** (an ignorable `skip` box appended at the end —
video, audio and GPMF/IMU tracks are untouched). Card originals are never
modified. If a dest MP4 is skipped as already in the batch but has no JSON or
embed yet, the sidecar is still written and the payload is still embedded.
even if a sidecar file gets separated from its video on S3, the tasks +
timestamps + camera serial / device id travel inside the MP4. If a sidecar is
missing key fields (complete flag, device id, camera serial, recorded-at, IMU
sensor list) a warning shows in the transfer log. Read embedded data back with:

```bash
python scripts/read_embedded_segments.py "E:\Batches\batch 6"            # summary
python scripts/read_embedded_segments.py --json GX010001.MP4             # full payload
```

Legacy `DCIM/100GOPRO/<task-folder>/*.MP4` layouts (pre-trimmed clips) are
still transferred as before. Skips `.LRV`, `.THM`, and other non-MP4 junk.

## Rebuilding work footage on AWS

After the batch is synced to S3, run `scripts/aws_trim_batch.py` on the AWS
server (single file, needs only Python 3.9+ and ffmpeg). It reads each MP4's
embedded segments and cuts **only the work segments** into task-name folders:

```bash
aws s3 sync "s3://your-bucket/footage/batch 6" "/data/batch 6"
python aws_trim_batch.py "/data/batch 6" --output "/data/batch 6 trimmed"
```

```text
/data/batch 6 trimmed/
  C0712/                           ← C + last 4 digits of camera serial
    pipe-welding/GX010001.MP4      ← original filename; stream copy keeps IMU
    cable-pulling/GX010001.MP4
```

Garbage segments are never cut. Videos not marked complete are skipped
(`--include-incomplete` to override); `--dry-run` prints the plan. Every clip
gets its own embedded identity (source file, task, start/end, camera serial,
device id) readable with `read_embedded_segments.py`.

## Quick start

### Windows

1. Install Python 3.10+ and for AWS preferably **[s5cmd](https://github.com/peak/s5cmd)** (fast) plus [AWS CLI v2](https://aws.amazon.com/cli/) credentials via `aws configure`
2. Double-click `run.bat`
3. Browser opens `http://127.0.0.1:8877`

### Mac

```bash
cd sd_offloader
chmod +x run.sh
./run.sh
```

## Daily workflow

1. Plug both removable SSDs
2. Open the UI → **Refresh drives & batches**
3. Pick **SSD 1** and **SSD 2**
4. **Batch on SSDs** — select an existing batch already on the drives (e.g. `batch 3` from home), or **+ Create new batch…**
5. Choose mode:
   - **SSD only** — free cards fast; upload to AWS later from the office
   - **SSD + AWS** — when each card finishes, CMD syncs the **whole flat batch folder** (`Batches/<batch>/` → `s3://…/<batch>/`) with **s5cmd** (plain sync first; `--numworkers 20` on failure). If an upload is already running, a follow-up resync is queued so files from later cards are not missed. UI shows progress and re-attaches after restarts.
6. Paste S3 folder URI (not keys), e.g. `s3://your-bucket/footage/`
7. **Start SD → SSD for this batch** — continues dumping cards into that batch (UI shows each card’s live transfer)
8. **Upload this batch to AWS (CMD)** — if both SSDs have that batch, use **Upload this SSD to AWS** on the disk card instead (one SSD per job). Opens CMD (survives server restart) **and** shows live progress. Failed transfers auto-retry; use **Restart** in the job card if needed. After upload, **Verify sizes** compares this SSD's files vs their S3 keys; only then is that SSD's batch folder deleted
9. Plug SD cards — parallel copy with live MB/s / ETA; completed cards are verified, transferred files wiped, ejected

### Office resume example (batch 3 dumped at home, no internet)

1. On the server: pick SSDs → select **batch 3** from the list  
2. Start SD → SSD if more cards still need dumping  
3. Click **Upload this SSD to AWS** on the disk that holds the batch (or the top Upload button if only one SSD has it) — watch progress on the page; **Restart** resumes missing files (skips what’s already on S3 at the same size). A different video with the same filename is stored as `GX010001-1.MP4`.

If SSD 1 fills up mid-batch, new cards spill to SSD 2 under the **same** batch folder name. Upload each SSD separately into `…/batch 6/`. Same names with different sizes become `-1` / `-2` on S3 — nothing overwrites Video A.

## Resume after crash / unplug

A progress file is written on the SD card:

`C1234/.gopro_offload_progress.json`

Replug the card (same batch session) and it continues from unfinished files.

## AWS login (one-time on the server)

**Do not put Access Keys in this app.** Use AWS CLI:

```bash
aws configure
```

Enter:

- AWS Access Key ID  
- AWS Secret Access Key  
- Default region  
- Output: `json`

Keys are stored by AWS CLI:

| OS | Location |
|----|----------|
| Windows | `C:\Users\<you>\.aws\credentials` |
| Mac | `~/.aws/credentials` |

Test:

```bash
aws s3 ls s3://your-bucket/footage/
```

In the offloader UI, only set:

```text
s3://your-bucket/footage/
```

Then click **Test AWS connection** — it uploads (and removes) an empty `_offloader_connection_test.txt` using your `aws configure` credentials. No keys go in any app config file.

The app runs `aws s3 sync` into `s3://your-bucket/footage/batch 6/…`.

### Upload later (SSD-only mode)

After cards are dumped, click **Upload this SSD to AWS** for each disk that has the batch. Same filename + same size is skipped on S3; a different-sized file is renamed to `GX010001-1.MP4`. Local delete happens only after that SSD's keys verify.

## IAM tip

Give the IAM user at least:

- `s3:ListBucket` on the bucket  
- `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `your-bucket/footage*`

## Ports

Default: `8877`  
Override: `SD_OFFLOADER_PORT=8899`

## Safety notes

- Wipe/eject happens only after size verification  
- Only transferred, size-verified MP4s / sidecars / task folders under `DCIM/…GOPRO` are deleted on the card  
- After upload the UI compares **this job's files** vs their S3 keys (extra objects from the other SSD are OK); **Delete local** only runs after that verify succeeds. A 30-minute listing timeout does **not** delete.
- Same filename on a second SSD never overwrites the first on S3 (`-1` / `-2`). Same size is treated as already present (rare same-size different-content collision is not hashed on S3).
- Config: `s5cmd_numworkers` (default 20 — used only after plain sync fails), `aws_upload_retries` (default 5) in `config.json`
