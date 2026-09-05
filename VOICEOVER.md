# Voiceover Station — for narrators (no coding)

Record your voice onto GoPro clips. Your narration is written **into the same video file**. IMU / GPMF metadata is kept.

---

## Message you can send to students (copy/paste)

```text
VOICEOVER — how to run (no coding)

1) Plug in your USB stick (with the footage) AND the USB mic.
2) On this computer, open the folder named "Footage cleaning"
   (or whatever we named the app folder).
3) Windows: double-click start-voiceover.bat
   Mac: open Terminal, paste this (fix the path if needed), press Enter:

   cd "/Users/SHARED/Footage cleaning" && ./start-voiceover.sh

4) Wait until a browser page opens: Voiceover Station
   If it does not open, go to: http://127.0.0.1:8765/voiceover
5) Allow microphone when the browser asks.
6) Click "Open voiceover folder" → select the footage folder on the USB.
7) Pick your mic → select a clip → press R to record, Space to pause the video,
   R again to save your voice INTO that same file. N = next clip.
8) Leave the black Terminal/Command window open while you work.
   When finished, close that window (or Ctrl+C).

Do NOT install Cursor or write any code. Python must already be installed
on the lab computer (staff does that once).
```

Replace `/Users/SHARED/Footage cleaning` with the real path on each lab machine (or put the app on the Desktop and use that path).

---

## One-time setup (you / IT — not students)

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/)  
   - Windows: check **Add python.exe to PATH**
2. Put this whole project folder on the lab computer **or on each USB stick** (easiest for students).
3. Confirm `gopro_cleaner/web` exists (it ships in the repo).
4. First run of `start-voiceover.bat` / `start-voiceover.sh` installs Python packages + FFmpeg (needs internet once).

Students do **not** need Node, Git, or Cursor.

### Do you need a public GitHub repo?

**No — not for students.** Best option:

- Copy the project folder onto each USB (or each lab PC Desktop).
- Students only double-click the start script.

Public GitHub is optional if you want them to download a ZIP themselves (`Code → Download ZIP`). Private repo also works if you add them as collaborators or send a ZIP yourself.

---

## Every time you narrate

### Windows

Double-click **`start-voiceover.bat`**

### Mac

```bash
cd "/path/to/Footage cleaning" && ./start-voiceover.sh
```

Or in Finder: right-click `start-voiceover.sh` → Open With → Terminal.

A browser tab opens at **Voiceover Station**. Leave the black Terminal / Command window open while you work. Close it (or press Ctrl+C) when you are done.

## USB layout

```text
voiceover/
  ClassName/
    clip1.MP4
    clip2.MP4
```

(Or any folder of class subfolders with MP4s — open that root in the app.)

## How to record

1. Click **Open voiceover folder** → pick the folder that holds the clips.
2. Choose your **mic**. Speak — the green bar should move.
3. Select a clip. The path under the player is the file that will be rewritten.
4. Press **R** — recording from **0:00** (clip audio muted).
5. Press **Space** to pause/play video while you keep talking (pause-and-describe).
6. Press **R** again — voice **replaces** audio in that same MP4.
7. Play from the **start** to review. **N** next. **Esc** discards a take.

## Narration (client rules)

- Say **I** for your own hands/tools (camera wearer).
- Other people in frame → **third person** (“another worker is kneeling…”).
- Granular steps + environment; aim ≥25 words per minute.

## Tips

- Plug in the USB mic **before** opening the app; click **Refresh** on the mic list if needed.
- Allow microphone access when the browser asks.
- If the page says “refused to connect”, the app is not running — run the start script again.

## Gemini (optional)

Draft script helper only — not required for recording. Not Whisper delivery transcripts.
