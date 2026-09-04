# SD Offloader — Process Changes

This document describes how the SD Offloader worked **before**, what it does **now**, and why the workflow changed.

---

## Summary

| Topic | Previous | Current |
|-------|----------|---------|
| What gets copied | Pre-trimmed clips inside task-named folders | Raw `.MP4` files + `.segments.json` sidecars |
| SSD layout | `Batches/<batch>/<card_id>/<task>/…` | `Batches/<batch>/…` (flat — no card folder) |
| SSD+AWS sync | Per-card folder → `s3://…/<batch>/<card>/` (broke on flat layout) | Whole batch folder → `s3://…/<batch>/` (coalesced + follow-up resync) |
| Where labels live | Folder names on the card | `.segments.json` (also **embedded inside each MP4**) |
| When metadata is embedded | Only at SD offload (earlier) | **Every time** GoPro Cleaner saves/updates `.segments.json` (and again at offload) |
| SD card detection (Cleaner) | Volume must be named `C####` | Any volume with `DCIM/###GOPRO` + `.MP4`s (any label) |
| When trimming happens | On the card / local machine before offload | Later on AWS via `aws_trim_batch.py` |
| AWS trim output | `task/clip_01.mp4` | `C####/task/original.MP4` (`C` + last 4 of camera serial) |
| IMU / camera meta | Not part of the offload packaging | Stored in sidecar + embedded in MP4; raw IMU stays in the MP4 `gpmd` track |

---

## Previous process

1. Operator labeled footage in GoPro Cleaner and (typically) **trimmed** work clips into task folders on the SD card.
2. Card layout looked like:

```text
C1234/
  DCIM/
    100GOPRO/
      pipe-welding/
        GX010001.MP4
      cable-pulling/
        GX010002.MP4
```

3. SD Offloader scanned `DCIM/###GOPRO` for **task-named folders** only.
4. It copied those folders onto dual SSDs as:

```text
<SSD>/Batches/batch 6/C1234/pipe-welding/*.MP4
<SSD>/Batches/batch 6/C1234/cable-pulling/*.MP4
```

5. After size verification, transferred task folders were wiped from the card and the card was ejected.
6. Optional AWS sync uploaded that same folder tree to S3.
7. Raw unlabeled MP4s sitting directly under `100GOPRO` were **ignored**.

### Limitations of the old flow

- Trimming had to happen before offload (slow on SD cards; harder to re-do labels).
- Task identity lived only in **folder names**, not inside the video.
- No packaged camera serial / IMU summary for downstream AWS jobs.
- Per-card subfolders (`C1234/`) made the batch tree deeper than needed for the new label-first workflow.

---

## Current process

1. Operator labels footage in GoPro Cleaner. Videos stay **untrimmed** on the card.
2. GoPro Cleaner writes a sidecar next to each video:

```text
GX010001.MP4
GX010001.segments.json
```

3. Card layout looks like:

```text
C1234/
  DCIM/
    100GOPRO/
      GX010001.MP4
      GX010001.segments.json
      GX010002.MP4
      GX010002.segments.json
```

4. SD Offloader copies every root-level `.MP4` and `.segments.json` into the **batch folder only** (no card-ID subfolder):

```text
<SSD>/Batches/batch 6/GX010001.MP4
<SSD>/Batches/batch 6/GX010001.segments.json
```

5. While copying, it **embeds the full segments JSON into the SSD copy of the MP4** (card original is never modified).
6. After verify → wipe transferred files from the card → eject (same safety model as before).
7. Optional AWS sync uploads the flat batch folder.
8. On the AWS server, `scripts/aws_trim_batch.py` rebuilds work clips into task folders.

### Filename collisions (same GoPro name on two cards)

GoPro numbering repeats across cards (`GX010001.MP4` on almost every card).  
If that name already exists in the batch **and it is a different video**, the incoming pair is renamed:

```text
GX010001-1.MP4
GX010001-1.segments.json
```

Same video (sidecar/embed identity) is skipped — not overwritten, not wiped.
A content hash is used only when the SSD copy has no sidecar/embed to compare.
Nothing is overwritten. The rename is stored in the card progress file so resume still works.

### Legacy cards

Old `DCIM/100GOPRO/<task-folder>/*.MP4` layouts are still copied (folder structure kept under the batch).  
`.LRV` / `.THM` and other non-MP4 junk are still skipped.

---

## What is in `.segments.json`?

Written by GoPro Cleaner for each labeled video. Typical fields:

| Field | Purpose |
|-------|---------|
| `segments[]` | Ordered list with `kind` (`work` / `garbage`), `task`, `start`, `end` (seconds) |
| `complete` | `true` only when the whole video is labeled |
| `batch_name`, `factory`, `card_badge` | Session / card context from the UI |
| `device_type`, `device_id` | Device identity from the UI |
| `media_meta` | Camera serial, model, firmware, recorded-at (incl. manual override), IMU sensor list, etc. |
| `duration`, `source`, `size_bytes` | File identity |

### IMU note

- **Raw IMU samples** live inside the MP4 (`gpmd` track). A plain byte copy keeps them intact.
- The JSON stores the **sensor list + camera identity**, not the full sample stream.
- AWS trimming uses stream-copy with the GPMF track mapped, so work clips keep IMU data.

During offload, the transfer log warns if a sidecar is missing key fields (`complete`, `device_id`, `device_type`, `camera_serial`, `recorded_at`, IMU sensor list).

---

## Embedding into the MP4

On the **SSD copy only**, the offloader appends the full segments JSON as a top-level MP4 `skip` box tagged `WCSG`:

- Players / ffmpeg / GoPro tools ignore `skip` boxes → playback unaffected.
- Video, audio, and `gpmd` tracks are never re-encoded or rewritten.
- Survives SSD copy and S3 upload.
- Sidecar `.segments.json` is still copied alongside as a human/tool-readable backup.

Read it back:

```bash
python scripts/read_embedded_segments.py "E:\Batches\batch 6"
python scripts/read_embedded_segments.py --json GX010001.MP4
```

Embedded payload includes each segment’s `start`, `end`, `kind`, and `task` name, plus device/camera metadata.

---

## Metadata Inspector page (GoPro Cleaner)

New UI at `/metadata` (links from Cleaner + Review headers):

- Dropdown of **This PC / Home**, fixed drives, removable SSDs, and detected GoPro SD cards
- File-manager browser of folders + MP4s (Labels badge when sidecar/embedded present)
- **Scan IMU** probes the current folder and marks each video IMU / No IMU
- Detail panel: camera serial/model/firmware, IMU yes/no + sensor list, full labeling fields, and a segments table (`kind` / `task` / `start` / `end`)

APIs: `GET /api/meta/drives`, `GET /api/meta/inspect`, `GET /api/meta/scan`.

## GoPro Cleaner SD detection (updated)

**Before:** only volumes named `C####` (or with a `C####` root folder) appeared in the SD card picker.

**Now:** every connected volume that has `DCIM/<3-digit>GOPRO` with at least one `.MP4` is listed — volume name can be blank, “NO NAME”, or anything random. Display id prefers classic `C####` when present; otherwise uses the volume label or `CARD-E` (drive letter).

Scan path is still `DCIM/###GOPRO` (or `DCIM` if several `100GOPRO` / `101GOPRO` folders exist).

## When metadata is written into the MP4

Whenever GoPro Cleaner updates `.segments.json` (`save_annotation` — every segment save / complete labeling), it also embeds the **full** JSON into that MP4 (`start`, `end`, `kind`, `task`, camera serial, device id, IMU sensor list, …). The sidecar is still written beside the file. SD Offloader embeds again on copy as a safety net.

## AWS rebuild (new)

After the batch is on S3 / the AWS machine:

```bash
aws s3 sync "s3://your-bucket/footage/batch 6" "/data/batch 6"
python scripts/aws_trim_batch.py "/data/batch 6" --output "/data/batch 6 trimmed"
```

Output (camera id = `C` + last 4 digits of `media_meta.camera_serial`):

```text
/data/batch 6 trimmed/
  C0712/
    pipe-welding/
      GX010001.MP4
    cable-pulling/
      GX010001.MP4
```

Behavior:

- Cuts **only** `work` segments (garbage is never cut).
- One source with multiple tasks → one clip per task folder, **original filename**.
- Multiple work segments of the same task from one source → `GX010001_01.MP4`, `_02`, …
- Missing serial falls back to `C####` card_badge, else `C0000`.
- Skips videos not marked `complete` (use `--include-incomplete` to override).
- `--dry-run` prints the plan without cutting.
- Uses the same stream-copy recipe as local GoPro Cleaner trimming (video + audio + `gpmd`).
- Needs only Python 3.9+ and `ffmpeg` / `ffprobe` on the AWS host.

---

## What stayed the same

- Dual-SSD selection and space spillover into the same batch name.
- Resume via `.gopro_offload_progress.json` on the card.
- Size verification before wipe/eject.
- Modes: **SSD only** and **SSD + AWS**.
- AWS upload via `aws` / `s5cmd`, with UI progress and retry.
- Optional delete-local after verified S3 sizes.
- UI at `http://127.0.0.1:8877` (`run.bat` / `run.sh`).

---

## Files touched by this change

| Path | Role |
|------|------|
| `sd_offloader/offloader/inventory.py` | Discover root MP4s + sidecars (legacy task folders still supported) |
| `sd_offloader/offloader/engine.py` | Flat batch dest, embed + metadata warnings, collision-safe names |
| `sd_offloader/offloader/space.py` | Batch root only (no `card_dest` subfolder) |
| `sd_offloader/offloader/embed_meta.py` | Append / read `WCSG` segments box inside MP4 |
| `sd_offloader/offloader/progress.py` | Resume with `dest_size` / `dest_rel` after embed or rename |
| `sd_offloader/offloader/eject.py` | Wipe root files + legacy task folders after verify |
| `sd_offloader/offloader/detect.py` | Treat root-level MP4s as valid card content |
| `scripts/read_embedded_segments.py` | Inspect embedded / sidecar labels |
| `scripts/aws_trim_batch.py` | AWS-side work-clip rebuild into task folders |
| `scripts/_test_offload_flow.py` | Simulated end-to-end regression test |

---

## End-to-end pipeline (current)

```text
SD card (labeled, untrimmed)
        │
        ▼
GoPro Cleaner  →  GX….MP4 + GX….segments.json
        │
        ▼
SD Offloader   →  Batches/<batch>/  (flat copy + embed JSON into MP4)
        │
        ▼
AWS S3 sync
        │
        ▼
aws_trim_batch.py  →  <task-slug>/<clip>.mp4   (work only, IMU kept)
```
