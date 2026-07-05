# GoPro Footage Cleaner

Trim useful segments from large GoPro egocentric recordings **without losing IMU / GPMF metadata**.

Most video editors strip GoPro's metadata track when you trim. This tool uses `ffmpeg` stream copy with explicit GPMF (`gpmd`) mapping so gyro/accelerometer data stays attached to each exported clip.

## Eager Review Station (SD card + camera folders)

Two-phase workflow for Windows and Mac — review in the browser at `http://127.0.0.1:8765/review`.

### Windows — one command

1. Install [Python 3.10+](https://www.python.org/downloads/) and [ffmpeg](https://ffmpeg.org/download.html) (add both to PATH)
2. Clone or download this repo
3. Double-click **`run.bat`** (or run it from Command Prompt)

The script creates a virtual environment, installs dependencies, starts the server, and opens the review page in your browser.

### macOS

```bash
chmod +x run.sh
./run.sh
```

### Workflow

**Phase 1 — Clean footage**

1. Choose a drive (`E:\`, `D:\Tribes`, etc.) or a camera folder (`C1234`)
2. Click **Scan raw footage**
3. Use **◀◀ Back** / **Forward ▶▶** to step through footage (each click jumps farther: 2s → 4s → 8s → …)
4. Click **Mark start**, step to where work ends, click **Mark end**
5. Press **T** to trim and save the clip (`FILENAME-1.MP4` beside the raw file)
6. Repeat for more useful segments, then press **N** to remove the raw file and go to the next one

**Phase 2 — Label tasks**

1. Switch to **2 · Label tasks** and scan the same folder (now shows trimmed clips)
2. Pick a task from the list (or add a new one)
3. Press **N** — the clip **moves** into `Labeled/<task-name>/` on the same drive

Shortcuts: **←** **→** step · **I** mark start · **O** mark end · **T** trim · **N** next · **K** keep entire file · **S** skip

### Folder layout after labeling

```text
E:\                          (SD card or C1234 camera folder)
  GH012330-1.MP4             (during phase 1, beside raw file)
  GH012330-2.MP4
  Labeled\
    task-stitching\
      GH012330-1.MP4
    picking\
      GH012330-2.MP4
```

Camera serial folders (`C1234`, `C8278`, …) are auto-detected under your archive/tribes drive.

## Helper sheet (only 2 columns)

Give your team `trim_sheet_template.csv` and `TRIM_SHEET_GUIDE.md`.

| footage | timestamps |
|---------|------------|
| GX012185.MP4 | 00:00 - 7:45, 10:00 - 12:00 |
| GX014891.MP4 | 00:00 - 5:30 |

Helpers only write the **file name** and **useful times**. You choose the **drive** in the app when uploading.

If the same file name exists in multiple folders, helpers use a path like `24-04-26/C8278/GX012185.MP4`.

## Bulk import

1. Download **CSV template** from the app
2. Helpers fill `footage` + `timestamps`
3. In the app: pick **drive** → upload CSV → **Preview** → **Queue entire sheet**

## What it does

1. Browse a connected drive or any folder
2. Select a GoPro video
3. Paste **all useful clip timestamps at once** (one per line)
4. Queue the batch and immediately move on to the next video
5. Clips export in the background as `filename-1.MP4`, `filename-2.MP4`, etc.
6. Optionally delete the original raw file **only after every clip succeeds**

## Clip sheet format

Paste one clip per line:

```text
00:00 - 7:45
10:00 - 12:00
16:00 - 17:00
```

Use `7:45` rather than `745` for seven minutes forty-five seconds.

## Requirements

- Windows 10/11 or macOS
- Python 3.10+
- [ffmpeg](https://ffmpeg.org/) on your `PATH`

Optional but recommended for best metadata compatibility:

- GoPro Labs [`udtacopy`](https://github.com/gopro/labs/tree/master/docs/control/chapters/bin) binary placed at `bin/udtacopy`

## Quick start

**Windows:** double-click `run.bat`

**macOS:**

```bash
cd "/path/to/Footage cleaning"
chmod +x run.sh
./run.sh
```

## Timestamp formats

All of these work:

- `7:30`
- `00:07:30`
- `1:07:30`
- `7m30s`
- `450`

## Output naming

Clips are saved in the **same folder** as the source file:

```text
GX010123-1.MP4
GX010123-2.MP4
GX010123-3.MP4
```

## How IMU preservation works

The trimmer:

1. Detects the GoPro metadata stream (`gpmd` / handler `GoPro MET`) with `ffprobe`
2. Runs a lossless `ffmpeg` trim with video, audio, and GPMF streams copied
3. Verifies the output still contains GPMF
4. Optionally runs `udtacopy` to restore GoPro-specific container headers

## Recommended workflow for 15 TB

1. Connect the drive
2. Open the app and browse to the drive in the left panel
3. Preview each file in Finder/QuickTime and note useful ranges
4. Extract clips one by one in this app
5. Delete the original only after confirming the clips play correctly

Because trimming is stream copy, it is fast and does not re-encode video.

## Troubleshooting

- **"No GPMF metadata track detected"**: the source file may not contain IMU data, or it uses an unusual stream layout.
- **Trim fails**: confirm `ffmpeg` is installed (`ffmpeg -version`).
- **Metadata still missing in downstream tools**: place `udtacopy` in `bin/udtacopy` and retry.

## Safety

Deleting the original moves the file to the macOS Trash (recoverable), not permanent deletion.
