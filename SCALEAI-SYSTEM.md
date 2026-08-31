# Scale AI 50-hour program — full system guide

This document explains **the whole program**: what it is, how the frontend and backend fit together, how labels and clips are stored, and how trim / stitch / playback work.

- **Labellers** should use **[SCALEAI.md](SCALEAI.md)** (how to use Review Station, keyboard, daily start).
- **Team leads and developers** should use **this file** (architecture, APIs, JSON, clip names, internals).

Current app version: **`2.20.0-scaleai`**. Branch in use: **`scaleai-50h-testing`**. Login is **off** on this version.

---

## Contents

1. [What this program is](#1-what-this-program-is)
2. [How the pieces fit together](#2-how-the-pieces-fit-together)
3. [How to start it](#3-how-to-start-it)
4. [Frontend](#4-frontend)
5. [Backend](#5-backend)
6. [Storage](#6-storage)
7. [Labeling model](#7-labeling-model)
8. [Trim, stitch, and IMU](#8-trim-stitch-and-imu)
9. [Video playback and previews](#9-video-playback-and-previews)
10. [Login / auth](#10-login--auth)
11. [Other pages (Cleaner, Metadata)](#11-other-pages-cleaner-metadata)
12. [Tests](#12-tests)
13. [Repository layout](#13-repository-layout)
14. [Rules and design choices](#14-rules-and-design-choices)

---

## 1. What this program is

Labellers watch long GoPro videos of a person doing a job (folding, packing, applying stickers, and similar work). For each useful action they mark **when it starts and ends** and give it a **subtask name**. Unusable footage is **garbage**.

The program then:

1. Saves those times next to the source video (JSON).
2. Cuts each labeled piece into a short MP4 (trim).
3. Joins all clips of the same subtask into one stitched MP4 (stitch).
4. Tries to keep GoPro **IMU / GPMF** (gyro / accelerometer) on every cut and join. If IMU would be lost, trim **fails** instead of writing a broken clip.

The product name on screen is **Review Station** (World Context). Scale AI 50-hour mode is the default.

There used to be a Stage 1 / Stage 2 cycle. **That cycle is gone.** One labeling path: free-form subtask + garbage on the 50-hour folder tree.

---

## 2. How the pieces fit together

```text
Labeller PC
  run.bat  →  Python venv  →  Flask on 127.0.0.1:8765
                                │
                                ├─ API  /api/...
                                └─ UI   gopro_cleaner/web  (built React SPA)

Browser  →  http://127.0.0.1:8765/review
         →  React Review Station talks to Flask with fetch
         →  Flask reads/writes JSON next to the footage on disk
         →  Flask runs FFmpeg in the background for trim / stitch / preview
```

| Layer | Role |
|-------|------|
| **Browser UI** | Review Station: player, keys, ScaleAI panel, footage list |
| **Flask** | Serves the UI **and** the API. One process, one port (**8765**) |
| **Disk** | Source MP4s, `{video}.json`, `manifest.json`, clip folders, stitched files |
| **FFmpeg** | Probe duration, lossless trim with GPMF, stitch, HEVC→H.264 preview |

Production labellers do **not** need Node.js. Maintainers rebuild the UI and commit `gopro_cleaner/web`:

```bash
cd gopro_cleaner/frontend
npm install
npm run build:flask
```

UI hot-reload development uses `run.dev.bat` / `run.dev.sh` (Vite + Flask). In Vite, the UI calls Flask at `http://127.0.0.1:8765`. In production, API URLs are same-origin (`/api/...`).

---

## 3. How to start it

### Labellers (Windows)

1. Double-click **`run.bat`** in the project folder.
2. Leave the black window open.
3. Browser opens `http://127.0.0.1:8765/review`.
4. Click **Open 50-hour folder** and pick the **parent** folder (the one that contains the task folders).

`run.bat` will:

- Create `.venv` if missing
- Install Python packages from `requirements.txt`
- Ensure FFmpeg (system install, or download via `static-ffmpeg`)
- Set `GOPRO_LITE_MODE=1`
- Serve the built UI from `gopro_cleaner/web`
- Open Review Station

Change port with environment variable **`GOPRO_CLEANER_PORT`** (default `8765`).

Health check: `GET http://127.0.0.1:8765/api/health`  
Returns version, git SHA, and whether FFmpeg/ffprobe were found.

### Maintainers (UI source)

Two terminals: Flask (`python -m gopro_cleaner`) and Vite (`npm run dev` in `gopro_cleaner/frontend`). Then commit a fresh `build:flask` before labellers use `run.bat`.

---

## 4. Frontend

**Stack:** React + Vite + TanStack Router, built as a static SPA that Flask hosts.

**Source:** `gopro_cleaner/frontend/`  
**Shipped build:** `gopro_cleaner/web/` (this is what `run.bat` serves)

### Pages

| URL | Page | 50-hour labellers |
|-----|------|-------------------|
| `/review` | **Review Station** | **Stay here** |
| `/` | Footage Cleaner (paste clip times, CSV sheet) | Do not use unless a supervisor says so |
| `/metadata` | Metadata / snapshot tools | Ignore |
| `/login` | Employee login | Redirects to `/review` (auth is off) |

### Review Station layout

- **Top bar:** SD card dropdown (ignore for 50-hour), Scan, Open 50-hour folder, Update, Metadata / Cleaner links
- **Centre:** video player, **Labeled region** bar (zoomed timeline), share-clip (I / O / WhatsApp)
- **Right:** ScaleAI 50-hour panel (counts, segment list, Trim / Stitch / Next), subtask names, Footage list, Keys
- **Footer:** IMU note, app version, **status messages** (saved, errors, trim progress)

The brain of the page is `useReviewController.ts`. It owns:

- scan / load video
- keyboard actions (T, G, U, Enter, N, Ctrl+Z, speed, scrub)
- pending mark, save, undo
- HLS preview for ScaleAI (HEVC files would otherwise show a black player in Chromium)
- trim / stitch polling

Supporting UI:

| File | Job |
|------|-----|
| `routes/review.tsx` | Page shell + global keyboard |
| `player-panel.tsx` | Player, labeled-region bar, WhatsApp in/out |
| `scaleai-panel.tsx` | 50-hour stats, segment rows, Trim / Stitch |
| `task-panel.tsx` | Subtask name list |
| `footage-list.tsx` | Videos grouped by main task |
| `trim-dock.tsx` | Trim progress dock |
| `lib/api.ts` | `fetch` wrapper to Flask |

### Keyboard (ScaleAI)

| Key | Action |
|-----|--------|
| Space | Play / pause (play starts at 1×) |
| ← → or [ ] | Speed −0.5× / +0.5× (**0.5× to 5×**, no reverse) |
| , . | Scrub ±0.1 s (Shift = one frame) |
| T or D | Mark end of a work segment (pending) |
| ↑ ↓ | Highlight a subtask name |
| Enter | Assign highlighted or typed name |
| G | Garbage up to playhead |
| U | Pending → **Unlabeled task**; otherwise undo |
| Ctrl+Z | Clear pending, or undo last saved mark (waits until the save is on disk) |
| N | Next video (blocked if a mark is pending, or while trim/stitch runs) |
| Home | Jump to 0:00 |
| Esc | Leave the name box |
| I / O | WhatsApp clip in / out |

While the name box is focused, **T** is a normal letter (`taking cloth`).

**T → Enter does not leave the labeled-region bar glowing.** Glow is only when you **click** a subtask name, count, or segment row to find a mark. Click elsewhere to clear it.

### Frontend → backend

All JSON calls go through `api()` in `lib/api.ts`. Errors come back as `{ "error": "..." }` and show in the footer + a toast.

---

## 5. Backend

**Stack:** Flask (`gopro_cleaner/app.py`), one process.

Blueprints:

| Blueprint | File | Purpose |
|-----------|------|---------|
| Eager / Review | `eager_routes.py` | Scan, ScaleAI segments, trim, stitch, preview, share clip |
| Auth | `core/routes_auth.py` | Login/signup (currently disabled) |
| Cards | `core/routes_cards.py` | SD-card / Supabase card tracking (not used for 50-hour labeling) |

### ScaleAI 50-hour APIs

These are the ones Review Station uses for the 50-hour job.

| Method | Path | What it does |
|--------|------|----------------|
| GET | `/api/eager/scaleai/annotation?path=&root=` | Load `{video}.json` + labels + progress |
| POST | `/api/eager/scaleai/segments` | Add a subtask or garbage segment |
| PATCH | `/api/eager/scaleai/segments` | Change times or re-label (rehomes clip if already trimmed) |
| DELETE | `/api/eager/scaleai/segments` | Delete one segment |
| POST | `/api/eager/scaleai/segments/undo` | Remove the last segment on that video |
| GET | `/api/eager/scaleai/progress?root=` | Dataset hours / % complete |
| POST / GET | `/api/eager/scaleai/labels` | Add or list subtask names for a main task |
| POST | `/api/eager/scaleai/process-video` | Queue trims for **this** source video |
| POST | `/api/eager/scaleai/process-folder` | Queue trims for every labeled source in the task |
| POST | `/api/eager/scaleai/stitch-video` | Stitch each subtask’s clips in that task folder |
| POST | `/api/eager/scan` | `mode: "scaleai"` — list source MP4s under the 50-hour root |
| POST | `/api/eager/pick-folder` | Native OS folder picker |
| POST | `/api/eager/share-clip` | Encode a short WhatsApp MP4 (max **300 s**) |
| GET | `/api/eager/trim/status` | Background trim jobs |
| GET | `/api/eager/preview/hls/<key>/<name>` | HLS playlist for the browser-compatible preview |
| GET | `/api/health` | Version + FFmpeg |
| POST | `/api/update` | Pull current git branch and restart (supervisor only) |

Scan `mode: "scaleai"` lists **source** videos only. It skips already-trimmed clip names (`CAM001-001-001.mp4`) and `*-stitched.mp4`.

Core Python modules:

| Module | Job |
|--------|-----|
| `core/fifty_hour_store.py` | All 50-hour JSON, manifest, clip serials, progress |
| `core/eager.py` | Folder scan |
| `core/eager_trim_queue.py` | Background trim jobs |
| `core/trimmer.py` | FFmpeg stream-copy trim + GPMF check |
| `core/scaleai_stitch.py` | Concatenate clips with GPMF |
| `core/preview_proxy.py` | HEVC → H.264 HLS preview |
| `core/ffmpeg_tools.py` | Find / download FFmpeg |
| `core/self_update.py` | Git pull + relaunch |

Older modules still exist for the **textile / SD-card** Review path (`annotation_store.py`, `scaleai_store.py` leftover sidecars). ScaleAI 50-hour **does not write** `*.segments.json`. If a 50-hour `{video}.json` exists, the old textile saver no-ops so the two formats cannot fight.

---

## 6. Storage

Nothing lives in a database for 50-hour labeling. **Everything is files next to the footage.**

### Folder you open

Open the **parent** 50-hour root, not one task folder and not one MP4.

```text
50-hour/                          ← Open 50-hour folder (this)
  applying-sticker/               ← one main task
    GX072170.MP4                  ← source
    GX072170.json                 ← labels for that source (created on first save)
    GX072171.MP4
    GX072171.json
    manifest.json                 ← shared subtask IDs for this main task
    _labeling/progress.json       ← under the opened root (hours)
    applying-sticker-001/         ← after trim
      CAM001-001-001.mp4
      CAM001-001-002.mp4
      applying-sticker-001-stitched.mp4
    Unlabeled-task-00N/           ← unlabeled clips until renamed
  garment-folding-general/        ← another main task
    ...
```

Parent task name = **first folder under the opened root** (wrappers like `AWS`, `Google Drive`, `50 hours` are skipped if present).

### `{video}.json` (one per source MP4)

Created on the **first saved mark**, not when you merely open the video.

Example shape:

```json
{
  "version": 2,
  "source_video": "GX072170.MP4",
  "source_path": "C:\\...\\applying-sticker\\GX072170.MP4",
  "parent_task": "applying-sticker",
  "camera_serial": "CAM001",
  "cl_number": null,
  "duration_seconds": 1234.5,
  "media_meta": { "camera_serial": "CAM001", "video_codec": "hevc" },
  "segments": [
    {
      "id": 1,
      "start": 14.1,
      "end": 17.84,
      "duration": 3.74,
      "type": "subtask",
      "label": "applying-sticker",
      "subtask_id": "001",
      "clip_filename": "CAM001-001-001.mp4",
      "clip_serial": 1
    },
    {
      "id": 2,
      "start": 17.85,
      "end": 27.85,
      "duration": 10.0,
      "type": "garbage",
      "label": "garbage"
    }
  ],
  "updated_at": "2026-08-31T15:00:00+0500"
}
```

- Writes are **atomic** (temp file + `fsync` + replace) so a crash should not leave half a JSON.
- An in-process lock (`RLock`) serializes writes on one PC. **Two PCs must not open the same folder at once.**

### `manifest.json` (one per main-task folder)

Stable subtask IDs and clip inventory for the whole task:

```json
{
  "version": 2,
  "subtasks": [
    {
      "id": "001",
      "name": "applying-sticker",
      "folder": "applying-sticker-001",
      "total_clips": 37,
      "clips": [
        { "filename": "CAM001-001-001.mp4", "source": "GX072170.MP4", ... }
      ]
    }
  ],
  "total_duration_seconds": 0,
  "total_stitched_duration_seconds": 0,
  "updated_at": "..."
}
```

Names you create on the **first video** of a main task reappear on later videos because they are stored here.

### Clip file names

```text
{CAMERA}-{SUBTASK_ID}-{CLIP_SERIAL}.mp4
   CAM001      001           001
```

- **Camera** from GoPro serial when known (else a token from the file).
- **Subtask ID** is the three-digit id in `manifest.json` (same action always keeps the same id).
- **Clip serial** is per camera, continuous (`001`, `002`, …). Another camera in the same folder can reuse `001` because the camera prefix differs.

Folder name: `{safe-label}-{id}/` e.g. `applying-sticker-001/`.

If you later assign an **Unlabeled** clip a real name, the file **moves** into that subtask folder. Only the **middle** number (subtask id) changes. Camera and clip serial stay the same.

### `_labeling/progress.json`

Usable labeled **hours** per main task. **Garbage is excluded.** Unlabeled-task **is included** (it is still work). Refreshed when segments are added, undone, deleted, or updated.

### What is not a source video

Scan and trim ignore:

- `*-stitched.mp4`
- Names matching `CAMERA-###-###.mp4` (already a clip)

### Leftover old files

When a 50-hour folder is opened or saved, the program migrates or removes leftovers such as:

- `segment.json`
- `*.segments.json`
- `*.scaleai.json`

Do not put those back. Do not edit JSON in Notepad.

### Preview cache (not next to footage)

Browser-compatible previews live under the user profile, not in the 50-hour folder:

`%USERPROFILE%\.cache\gopro-cleaner\previews` (Windows)

Safe to delete if a preview is stuck; the app will rebuild it.

---

## 7. Labeling model

### Pending then save

1. Playhead is the **end** of the piece.
2. **T** (or **D**) opens a **pending** range from the last mark (or 0:00) to the playhead.
3. **Enter** (name) or **U** (Unlabeled task) or **G** (garbage) writes JSON.

There is **no Save button**. After Enter / U / G the UI updates immediately; the POST to Flask writes disk. Undo waits for that write so it cannot delete the wrong mark.

### Segment types

| Type | Label | Counts as usable hours? |
|------|--------|-------------------------|
| `subtask` | Any name, including **Unlabeled task** | Yes |
| `garbage` | always `garbage` | No |

Minimum length: **0.05 s**. If a new mark shares an end boundary with the previous one, start is bumped by **0.01 s** so clips do not overlap.

The store **allows gaps** (you can delete a middle segment). Labellers are still told to cover the whole timeline (subtask or garbage). Coverage % on the player is **work seconds / duration**, not garbage.

### Next video

- **N** / Next video / Footage click is blocked while a mark is **pending**.
- Also blocked while trim or stitch is running (status line explains why).
- ScaleAI **N** goes to the next file in the scan list. It does not require 100% coverage (the textile SD-card mode does).

### Frontend save path

`POST /api/eager/scaleai/segments` → `fifty_hour_store.add_segment` → atomic `{video}.json` + `add_label` into `manifest.json` + `refresh_progress`.

---

## 8. Trim, stitch, and IMU

### Trim this video / Trim whole folder

1. JSON clip filenames are written **before** FFmpeg starts (so a crash does not leave unnamed files).
2. Each subtask segment is queued as a background job.
3. FFmpeg **stream-copies** video, audio, and GPMF (`gpmd` / GoPro MET). No re-encode of the source.
4. If the **source had GPMF** and the **output does not**, the job **fails**. No silent IMU loss.
5. Optional `bin/udtacopy` can restore GoPro container headers when present.
6. Existing clips with the same name are **skipped** (retry only missing ones).
7. Footer may show `36/37 clips on disk · 2 missing · 1 extra` — that is an audit of JSON vs files for this camera, not a crash.

### Stitch each subtask

`scaleai_stitch.py` concatenates all clips in a subtask folder into `{folder}-stitched.mp4` **inside that folder**, still stream-copying GPMF when every clip has it.

All clips for one stitch must share compatible codecs (same GoPro settings). Mixed resolution / codec **fails closed**.

Keep individual clips until ScaleAI confirms the stitched file.

### WhatsApp download

Re-encodes a short H.264 clip (1080p or 720p). Max **5 minutes**. This path is for sending a sample, not for the dataset.

---

## 9. Video playback and previews

Many 50-hour files are **HEVC**. Chromium often cannot decode them (black frame, moving clock).

ScaleAI always requests a **browser-compatible 720p H.264 HLS preview** (`preview_proxy.py`). You can still label while it builds. Speed is **0.5×–5×** (not capped at 2×).

The preview is 1:1 with the original timeline, so marks are always in **source seconds**.

Textile / SD-card mode can use GoPro LRV or a fast SSD copy instead. ScaleAI does **not** swap to the original HEVC file (that is what used to paint a black player).

---

## 10. Login / auth

Both layers have **`AUTH_DISABLED = true`**:

- Frontend: `AuthProvider.tsx` — `/login` redirects to `/review`
- Backend: `routes_auth.py` — Supabase is not required

Labellers do not sign in. Optional Supabase (cards, employee hours) is for the old SD-card workflow. If `.env` is missing, 50-hour labeling still works.

---

## 11. Other pages (Cleaner, Metadata)

Still in the repo; **not the 50-hour labeling path**.

- **Footage Cleaner (`/`):** paste in/out times or import a CSV trim sheet; queue lossless trims in the source folder.
- **Metadata:** snapshot / probe helpers.
- **SD cards, batches, Finish card:** textile Review Station + optional Supabase.

`README.md` describes those older flows. Treat **SCALEAI.md** as the labeller source of truth and **this file** as the 50-hour system source of truth.

---

## 12. Tests

Under `gopro_cleaner/tests/`:

| File | Covers |
|------|--------|
| `test_fifty_hour_store.py` | Segments, overlap bump, clip serials, unlabeled rehome, manifest, progress |
| `test_fifty_hour_routes.py` | HTTP API, trim queue naming |
| `test_scaleai_stitch.py` | GPMF stitch, ordering, mismatch rejection |
| `test_scaleai_scan.py` | Folder scan, skip exports |
| `test_scaleai_store.py` / `test_scaleai_routes.py` | Older ScaleAI sidecar (legacy) |
| `test_export_batch.py` | Download audit counts |
| `test_preview_proxy.py` | Preview cache |
| `test_self_update.py` | Update pull |

Run (from repo root, venv active):

```bash
python -m pytest gopro_cleaner/tests/test_fifty_hour_store.py gopro_cleaner/tests/test_fifty_hour_routes.py gopro_cleaner/tests/test_scaleai_stitch.py gopro_cleaner/tests/test_scaleai_scan.py -q
```

There is little automated frontend/keyboard coverage. Labeling keys are verified by using Review Station.

---

## 13. Repository layout

```text
SCALEAI.md                 Labeller guide (non-technical)
SCALEAI-SYSTEM.md          This file
README.md                  Older developer / Cleaner / SD-card docs
run.bat / run.sh           Production start (venv + Flask + UI)
run.dev.bat / run.dev.sh   UI hot reload
requirements.txt           Python deps
gopro_cleaner/
  app.py                   Flask app
  eager_routes.py          Review + ScaleAI APIs
  web/                     Built SPA (commit after UI changes)
  frontend/                React source
  core/
    fifty_hour_store.py    50-hour JSON + clips
    trimmer.py             FFmpeg trim + GPMF verify
    scaleai_stitch.py      Stitch
    preview_proxy.py       HLS preview
    eager_trim_queue.py    Job queue
  tests/
```

---

## 14. Rules and design choices

- Do not close the black window while labeling, trimming, or stitching.
- Do not rename / move / delete MP4 or JSON by hand.
- Do not edit `manifest.json` or `{video}.json` in Notepad.
- Do not open the same footage folder on two PCs at once (no cross-machine file lock).
- Do not use Cleaner for this 50-hour job unless a supervisor says so.
- Do not press **Update** unless asked (it pulls git and restarts).
- After UI source changes, run `npm run build:flask` and commit `gopro_cleaner/web`, or `run.bat` users keep the old screen.

**By design**

- Gaps on the timeline are allowed in code; labellers are instructed to cover every second.
- Coverage % is **work time**, not garbage.
- Unlabeled task counts as labeled work until it is given a real name.
- Trim stops if IMU would be stripped.
- Auth is off on this version.
