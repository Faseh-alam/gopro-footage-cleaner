# GoPro Footage Cleaner

Trim and organize large GoPro egocentric recordings **without losing IMU / GPMF metadata**.

Most video editors strip GoPro’s metadata track when you trim. This tool uses `ffmpeg` stream copy with explicit GPMF (`gpmd`) mapping so gyro and accelerometer data stay attached to each exported clip.

The app has two parts:

| Part | Role |
|------|------|
| **React UI** (`gopro_cleaner/web`) | Built SPA — browse, review, queue trims |
| **Flask** (`127.0.0.1:8765`) | Serves the UI **and** the API (probe, trim, annotations, cards, optional Supabase) |

Production (`run.bat` / `run.sh`) only needs Python and the committed build in `gopro_cleaner/web` — colleagues do **not** need Node. Maintainers rebuild and commit that folder before pushing:

```bash
cd gopro_cleaner/frontend
npm install
npm run build:flask
```

UI hot-reload development uses `run.dev.bat` / `run.dev.sh`.

---

## Requirements

- **Windows 10/11** or **macOS**
- **Python 3.10+** ([python.org](https://www.python.org/downloads/) — on Windows, check “Add python.exe to PATH”)
- **FFmpeg** — installed automatically on first run via the `static-ffmpeg` Python package (a system FFmpeg on `PATH` also works)

For UI development only:

- **Node.js 18+** and **npm**, plus a local copy of `gopro_cleaner/frontend`

Optional:

- **[GoPro Labs `udtacopy`](https://github.com/gopro/labs/tree/master/docs/control/chapters/bin)** at `bin/udtacopy` for best downstream GoPro header compatibility
- **Supabase** project for daily card stats (see [Card tracking](#card-tracking-supabase))

---

## Installation

### Windows (recommended)

1. Clone or download this repository.
2. Double-click **`run.bat`** (or run it from Command Prompt in the repo root).

`run.bat` will:

- Create `.venv` and install Python dependencies from `requirements.txt`
- Download FFmpeg if needed
- Start **Flask** on port **8765**, serving both the API and the built UI from `gopro_cleaner/web`
- Open **`http://127.0.0.1:8765/review`**

No Node.js is required for this path. If the window closes with an error, the script pauses so you can read the message.

### macOS / Linux

```bash
cd /path/to/gopro-footage-cleaner
chmod +x run.sh
./run.sh
```

Press **Ctrl+C** to stop the server.

### Change the API port

Set environment variable **`GOPRO_CLEANER_PORT`** before starting (default `8765`). In production the UI uses same-origin `/api/...` URLs. In Vite dev, the UI calls `http://127.0.0.1:8765`.

### UI development (Vite)

Use **`run.dev.bat`** (Windows) or **`run.dev.sh`** (macOS/Linux) for hot reload. Before you push UI changes, run `npm run build:flask` and commit the updated `gopro_cleaner/web` so `run.bat` users get the new UI without installing Node.

Manual two-terminal setup:

**Terminal 1 — API**

```bash
cd /path/to/gopro-footage-cleaner
python3 -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.          # Windows: set PYTHONPATH=.
export GOPRO_LITE_MODE=1
python -m gopro_cleaner
```

**Terminal 2 — UI (dev)**

```bash
cd gopro_cleaner/frontend
npm install
npm run dev
```

Open the URL Vite prints (e.g. `http://localhost:5173/review`).

---

## Card tracking (Supabase)

Card registration and daily summaries are stored in **Supabase** (Postgres), not Google Sheets.

1. Create a Supabase project.
2. Run the SQL in **`supabase/schema.sql`** in the Supabase SQL editor (creates `cards` and `daily_summaries` tables).
3. Copy **`.env.example`** to **`.env`** in the repo root and set:

   ```env
   SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
   SUPABASE_KEY=your_publishable_or_anon_key
   ```

4. Restart Flask so it loads `.env`.

**Behavior**

- When a real SD card is connected (name like `C1234`) and you open Review, the app registers it **once per day** if it is not already in the database.
- **Finish card** writes final stats (durations, used space, finish time) and refreshes the **daily summary**.
- Summaries update when a card is first registered or when a card is finished — not on every keystroke.
- If Supabase is not configured, review and trimming still work; the header shows **DB not configured**.

---

## Using the app

After startup, use the **Vite** URL (not `8765` alone):

| Page | URL | Purpose |
|------|-----|---------|
| **Footage Cleaner** | `http://localhost:<vite-port>/` | Browse drives, paste clip times, queue background trims |
| **Review Station** | `http://localhost:<vite-port>/review` | SD cards, segment annotation, tasks, batch trim queue |

Links in the header switch between the two pages.

---

### Footage Cleaner (`/`)

For operators who already know exact in/out times (or use a helper CSV).

1. Pick a **drive** or browse folders in the file list.
2. Select a **GoPro video** — the app probes duration and shows whether **GPMF / IMU** was detected.
3. Paste **one clip range per line** in the text area (see [Timestamp formats](#timestamp-formats)).
4. Optionally enable **delete original** after all clips succeed (moves to Trash, recoverable).
5. Click **Queue** — trims run in the background as `filename-1.MP4`, `filename-2.MP4`, … in the **same folder** as the source.

**Bulk import**

1. Download the **CSV template** from the app (or use `trim_sheet_template.csv` in the repo).
2. Helpers fill **`footage`** and **`timestamps`** only (see **`TRIM_SHEET_GUIDE.md`**).
3. In the app: choose drive → upload sheet → **Preview** → **Queue entire sheet**.

If the same file name exists in multiple folders, use a path in the sheet, e.g. `24-04-26/C8278/GX012185.MP4`.

---

### Review Station (`/review`)

For reviewing long SD-card footage: mark **work** (with a task name) and **garbage**, then queue physical trims.

#### Setup

1. Insert an SD card or click **Open footage** to pick a folder (native folder picker).
2. **Detect** refreshes the SD card list; choose a card in the dropdown.
3. **Scan** loads all `.MP4` files and opens the first unfinished file.

Large files may show **Building preview …** while a **720p proxy** is generated for smoother playback at high speed. Until the proxy is ready, playback may be capped at **2×** on the original file.

#### Annotation model

Each video is split into contiguous **segments** from `0:00` to the end:

- **Work** segments are labeled with a **task** (e.g. `forward-stitch`, `picking`).
- **Garbage** segments have no task.

The player header shows **% covered** — when the timeline is fully labeled, the file is **complete**.

#### Typical flow (one work section)

1. Scrub or play to the **end** of a useful section (`, ` / `.` for ±1s; **← →** change speed).
2. Press **T** — marks work from the last boundary to the playhead and opens **task selection**.
3. Filter tasks in the search box (↑↓ to highlight). **Enter** assigns the selected task (filter text alone does **not** create a task).
4. Add new tasks only in the **New task** field at the bottom of the task panel.
5. For useless footage: scrub to the end of the garbage region and press **G**.
6. Press **U** to remove the last markup if you made a mistake.
7. When the file is fully covered, press **N** to go to the **next unfinished** video.

#### Header actions

| Control | Action |
|---------|--------|
| **Queue clips** | Sends completed annotations to the background trim queue |
| **Finish card** | Completes the batch for this card and updates Supabase card + daily summary |
| **Save card data** | Manual save / refresh card row (when DB is configured) |
| DB indicator | Green = Supabase connected |

Camera serial folders (`C1234`, `C8278`, …) are detected when scanning archive/tribe drives.

#### Keyboard shortcuts (Review)

| Key | Action |
|-----|--------|
| **Space** | Play / pause (resets to 1× when starting play) |
| **← →** (or `[` `]` ) | Playback speed −0.5× / +0.5× |
| **,** **.** | Scrub −1s / +1s |
| **T** | End work segment at playhead → pick task |
| **Enter** | Assign task to pending work (or repeat last task when filter is empty) |
| **G** | Mark garbage to playhead |
| **U** | Delete last markup |
| **N** | Next unfinished video |
| **Home** | Jump to 0:00 |

In the **task filter** field: ↑↓ move selection, **Enter** assign, **Esc** clear and return focus to the player.

---

### Folder layout after labeling

Task names create folders **beside** your footage on the same drive:

```text
E:\
  GH012330.MP4                 (whole file kept if fully useful)
  GH012330-1.MP4               (trimmed clip)
  forward-stitch\
    GH012330-1.MP4             (moved when using label/move workflow)
  picking\
    GH012330-2.MP4
```

Sidecar annotation files live next to each video: `GX010001.segments.json`.

---

## Timestamp formats

All of these work in the Cleaner clip box and in CSV sheets:

```text
7:30
00:07:30
1:07:30
7m30s
450
```

Example block:

```text
00:00 - 7:45
10:00 - 12:00
16:00 - 17:00
```

Use `7:45` for seven minutes forty-five seconds, not `745`.

---

## How IMU preservation works

The trimmer:

1. Detects the GoPro metadata stream (`gpmd` / handler `GoPro MET`) with `ffprobe`
2. Runs a lossless `ffmpeg` trim with video, audio, and GPMF streams copied (aligned to your marked start/end)
3. Verifies the output still contains GPMF — **fails the trim if IMU was on the source but missing in the clip**
4. Optionally runs `udtacopy` to restore GoPro-specific container headers

The Review player shows preview/proxy status and coverage; successful background trims keep IMU when the source had GPMF.

---

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| Blank page at `http://127.0.0.1:8765` | Normal — Flask is API-only. Open the **Vite** URL from `run.bat` / `run.sh` or `npm run dev`. |
| **DB not configured** | Add `.env` with Supabase keys and restart Flask. |
| Card not saved | Only connected SD cards named **`C####`** are registered; arbitrary folders are rejected. |
| **No GPMF metadata track detected** | Source may lack IMU or use an unusual layout. |
| Trim fails / `WinError 2` / missing ffmpeg | Re-run `run.bat` or `run.sh`, or install system FFmpeg (`ffmpeg -version`). |
| Preview stuck at **Building preview** | Wait for encode to finish; restart Flask if a build was interrupted. Old proxies live under `%USERPROFILE%\.cache\gopro-cleaner\previews` (Windows). |
| Node not found (Windows) | Install Node, close terminal, run `run.bat` again. |
| API errors from UI | Ensure Flask is running on **8765** and nothing else blocked that port. |

Check **`GET /api/health`** on the Flask server for version and FFmpeg status.

---

## Safety

- **Delete original** and **Move video to Trash** use the system Trash (recoverable on macOS; Windows Recycle Bin where supported), not permanent deletion.
- Card paths and credentials: keep **`.env`** out of git (it is listed in `.gitignore`).

---

## Repository layout (short)

```text
run.bat / run.sh          One-command startup (venv + npm + both servers)
requirements.txt          Python dependencies
.env.example              Supabase template (copy to .env)
supabase/schema.sql       Database tables for card tracking
gopro_cleaner/
  app.py                  Flask API entry
  eager_routes.py         Review / eager API routes
  core/                   Trimming, annotations, preview proxies, Supabase
  frontend/               React + Vite UI (TanStack Router)
scripts/wait_open_vite.py Opens /review when Vite is ready
trim_sheet_template.csv   Helper sheet template
TRIM_SHEET_GUIDE.md       Instructions for helpers filling sheets
```

---

## Recommended workflow for very large archives

1. Connect the drive and open **Review Station**.
2. **Detect** → **Scan** → annotate work/garbage and tasks per file.
3. **Queue clips** and **Finish card** when the SD batch is done.
4. Use **Footage Cleaner** for any remaining one-off trims from pre-written timestamp lists.
5. Delete originals only after confirming clips play correctly in your downstream pipeline.

Stream-copy trimming is fast and does not re-encode video, which matters when processing terabytes of footage.
