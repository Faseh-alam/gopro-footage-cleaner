# Updates — 11 August 2026

## Instant 720p preview playback (HLS streaming)

**Problem:** Even with the fast encoder settings, a long 4K file takes 1–2 minutes to transcode, and playback only switched to 720p when the whole build finished.

**Change:** Previews are now encoded as **HLS** (2-second streaming segments + playlist) instead of one MP4. The player attaches to the preview a few seconds into the build — smooth 5–8× review starts almost immediately, while ffmpeg keeps encoding ahead in the background.

- Backend (`preview_proxy.py`): ffmpeg writes `seg#####.ts` + `index.m3u8` into the preview cache (`hls_flags temp_file`, so segments appear atomically). Status now reports `hls` (playlist URL) and `playable` (≥2 segments exist).
- New endpoint `GET /api/eager/preview/hls/<key>/<name>` serves the playlist (`no-store`, it grows during the build) and segments (cacheable). Key/name are whitelist-validated.
- Frontend: **hls.js** attaches the growing playlist to the same `<video>`; on Safari it plays natively. Status pill shows `720p preview · encoding N%` while the tail is still building.
- The scrub bar keeps the full known duration while the preview grows (element duration only covers encoded segments).
- If the browser can't do HLS or the stream errors fatally, playback falls back to the original file as before.
- Preview cache format bumped (`v7-hls-720p`) — old MP4 previews are ignored and rebuild once as HLS.
- Verified by `scripts/_test_preview_hls.py`: playlist becomes playable **before** the encode finishes, ENDLIST lands at completion, Flask routes serve/deny correctly, finished previews survive a backend restart.

---

# Updates — 8 August 2026

Summary of changes made in today’s work session on the `redesign` branch.

---

## 1. Production app: Flask serves the built UI (no Node for colleagues)

**Problem:** `run.bat` started the Vite **dev** server. That often failed for colleagues (Node/winget/`npm` issues) and the window closed mid-run.

**Change:** Production launches **Flask only**. The React UI is a prebuilt SPA in `gopro_cleaner/web`.

| Script | Purpose |
|--------|---------|
| `run.bat` / `run.sh` | Production — Python + FFmpeg + Flask; opens `http://127.0.0.1:8765/review` |
| `run.dev.bat` / `run.dev.sh` | Development — Vite hot reload + Flask API |

**Maintainer workflow before push:**

```bash
cd gopro_cleaner/frontend
npm install
npm run build:flask   # writes gopro_cleaner/web
```

Then commit `gopro_cleaner/web` with the rest of the changes. Colleagues do **not** need Node.js.

**Supporting pieces:**

- Flask SPA routes for `/`, `/review`, and static assets (never shadow `/api/*`)
- `npm run build:flask` via `frontend/scripts/build-flask.mjs` + `copy-web-to-flask.mjs`
- TanStack SPA build with Nitro disabled so the shell prerenders cleanly
- Production API calls use **same-origin** `/api/...` (dev still uses `http://127.0.0.1:8765`)
- `.gitignore` ignores frontend caches (`node_modules`, `.vite`, `.output`, `dist`) — **source stays in git**
- README updated for the new install/run model

---

## 2. Faster 720p preview / fix “Finalizing preview…” stall

**Problem:** After “Building preview 100%”, the UI sat on **Finalizing preview…** for a minute or more before the 720p file appeared.

**Root causes & fixes:**

1. **Wrong progress units** — ffmpeg’s `out_time_ms` is microseconds; treating it as ms made progress jump to 99% almost immediately. Progress now uses microseconds so the percentage is honest.
2. **Removed `+faststart`** — that flag rewrote the whole proxy after encode (real finalize stall). Previews are served with HTTP Range, so faststart isn’t needed.
3. **GPU encode on Windows by default** — probe NVENC → QuickSync → AMF once per process; fall back to ultrafast x264. Override with `GOPRO_PREVIEW_ENCODER` if needed.
4. **Prefetch next video’s preview** — when the current 720p is ready (or already cached), start building the next unfinished video’s proxy so “Next” often opens 720p immediately.

First open of a file still streams the **original** while the proxy builds, then swaps to 720p; subsequent videos in the queue usually open as 720p right away.

---

## 3. React-only review workflow (earlier today)

Legacy Flask HTML/CSS/JS UI removed; Flask is API (+ now SPA host). Review UX improvements shipped earlier in the day:

- **Trim dock** — floating queue UI (minimize / restore / close)
- **Queue clips** — split button + “Queue all clips”; work segments go to `DCIM/{3-digit}GOPRO/{task-name}/`
- Optional **Delete source** after trim (off by default)
- Parallel trim workers; cancel one / cancel all
- **WhatsApp share clip** — mark in/out, 720p/1080p (default 1080p); IDM-friendly download via prepare-token + GET
- Segment **delete (×)** on annotation rows
- Task filter: Enter only filters; create tasks via “New task”; user tasks deletable, defaults protected

---

## 4. Supabase card tracking (earlier today)

Google Sheets card/summary path replaced with **Supabase**:

- Schema: `supabase/schema.sql` (`cards`, `daily_summaries`)
- Env: `SUPABASE_URL` / `SUPABASE_KEY` in `.env` (see `.env.example`)
- Register on first SD card encounter (`C####`); finish via finish-card; daily summary on register/finish
- Without Supabase, review/trim still work; header shows DB not configured

Also earlier: 720p preview/playback hardening, ffmpeg stderr-to-file (avoid pipe deadlock), Vite port auto-detect for the old dual-server path.

---

## Commits on `redesign` (today)

| Commit | Summary |
|--------|---------|
| `8958ae9` | Supabase card tracking, preview playback harden, task filter Enter fix |
| `a1401e2` | Trim dock, task folders, WhatsApp share clips, React-only docs |
| `7c07035` | Flask serves built SPA; run.dev scripts; preview speed / Finalizing fix |

Branch: https://github.com/Faseh-alam/gopro-footage-cleaner/tree/redesign

---

## Quick reference for colleagues

1. Clone / pull `redesign`
2. Double-click **`run.bat`** (Windows) or run **`./run.sh`**
3. Open **`http://127.0.0.1:8765/review`** (script opens it)
4. Optional: copy `.env.example` → `.env` and set Supabase keys for card stats
