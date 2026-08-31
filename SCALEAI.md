# Scale AI — Labeller Guide

This is the instruction manual for the **Scale AI 50-hour labeling program** (Review Station). It is written for labellers who are not technical. Follow it in order the first time you use a new PC. After that, use the **daily start** and **keyboard** sections.

You do not need to know programming. You do not need to edit any files by hand.

For how the frontend, backend, APIs, and files on disk work, see **[SCALEAI-SYSTEM.md](SCALEAI-SYSTEM.md)**.

---

## What this program is for

Labellers watch long GoPro videos of a person doing a job (folding garments, packing boxes, applying stickers, and similar work). For each useful action on screen, you mark **when it starts and ends** and give it a **subtask name**. Footage that is not usable (camera shake, walking, talking, empty hands, and so on) is marked as **garbage**.

The program then cuts those marked pieces into short clips and can join all clips of the same subtask into one video. That is the labeled dataset.

Your marks are saved automatically as you work. There is no separate Save button.

---

## Contents

1. [Before the first use (once per PC)](#1-before-the-first-use-once-per-pc)
2. [How to open the program every day](#2-how-to-open-the-program-every-day)
3. [How the footage folder must look](#3-how-the-footage-folder-must-look)
4. [What you see on screen](#4-what-you-see-on-screen)
5. [How to label a video](#5-how-to-label-a-video)
6. [Keyboard shortcuts](#6-keyboard-shortcuts)
7. [Unlabeled clips, mistakes, and highlights](#7-unlabeled-clips-mistakes-and-highlights)
8. [Trim and stitch](#8-trim-and-stitch)
9. [Sending a short clip on WhatsApp](#9-sending-a-short-clip-on-whatsapp)
10. [How to stop at the end of a shift](#10-how-to-stop-at-the-end-of-a-shift)
11. [If something goes wrong](#11-if-something-goes-wrong)
12. [Rules — do not do these](#12-rules--do-not-do-these)
13. [For team leads — files the program writes](#13-for-team-leads--files-the-program-writes)

---

## 1. Before the first use (once per PC)

A supervisor or IT person should do this once. Labellers do not need to repeat it every day.

### What the PC needs

- Windows 10 or Windows 11
- **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/)
  - During install, tick **Add python.exe to PATH**
- A copy of this project folder on the PC (the folder that contains `run.bat`)
- Internet for the **first** run only (it downloads a few tools, including FFmpeg)
- The 50-hour footage folder on a local disk or SSD (not only in the cloud)

You do **not** need Node.js, GitHub, or a login for normal labeling on this version.

### First launch

1. Open the project folder in File Explorer.
2. Double-click **`run.bat`**.
3. Leave the black window open. The first run can take several minutes while it installs what it needs.
4. A browser tab should open at:

   `http://127.0.0.1:8765/review`

5. You should see **Review Station** with a button **Open 50-hour folder**.

If the black window closes with an error, read the message, take a photo, and send it to your supervisor. Common first-run issues are Python not installed, or Python installed without “Add to PATH”.

---

## 2. How to open the program every day

1. Double-click **`run.bat`** in the project folder.
2. A **black window** opens. **Do not close it** while you work. That window is the program.
3. The browser opens Review Station by itself. If it does not, open Chrome or Edge and paste:

   `http://127.0.0.1:8765/review`

4. Click **Open 50-hour folder** and pick the **parent** folder that contains the task folders (see the next section).
5. Wait until videos appear in the **Footage** list on the right. The first video loads in the player.

You do not log in on this version.

### The black window

| Situation | What to do |
|-----------|------------|
| Black window is open | The program is running. Leave it alone. |
| You closed it by accident | Double-click `run.bat` again, then refresh the browser. |
| You see lots of text scrolling | Normal. Ignore it unless the window closes. |

---

## 3. How the footage folder must look

Click **Open 50-hour folder** on the **root** folder, not on a single video and not on one task folder.

```text
50-hour/                          ← open this folder
  garment-folding-general/        ← one main task
    GX010001.MP4
    GX010002.MP4
  gear-box-fitting/               ← another main task
    GX010002.MP4
```

- Each **folder** under the root is one **main task** (the job being recorded).
- Each **MP4** inside that folder is one source video.
- The first video of a new main task has **no** subtask names yet. You invent them as you label.
- Names you create on the first video **reappear automatically** on later videos in the **same** main task.

After you label and trim, the program adds folders and files next to the videos. You do not create those by hand.

---

## 4. What you see on screen

The page title is **Review Station**. Scale AI 50-hour mode is already on.

### Top bar

| Control | What it does |
|---------|----------------|
| **SD card** dropdown | Ignore for 50-hour folder work. |
| **Scan** | Reloads the folder you already opened. Use this if new videos were copied in. |
| **Open 50-hour folder** | Choose the footage root. This is how you start. |
| **Update** | Only if a supervisor asks you to pull a new version. Confirm the popup, then wait for the page to reload. |
| **Metadata / Cleaner** | Other tools. Labellers stay on Review Station. |

### Centre — the player

- The video
- A timeline under it, coloured by subtask
- Playback speed and time
- A coverage percentage (how much of this video is already marked as work)

A thin bar may say **Preparing browser-compatible preview**. That means the program is making a copy Chromium can play (many GoPro files are HEVC). You can still label while it builds. Speed goes from **0.5× to 5×**.

### Right side

1. **ScaleAI 50-hour panel** — current main task, which video you are on (`2 / 5`), counts per subtask, the list of segments you already marked, and the Trim / Stitch buttons.
2. **Task list** — every subtask name used in this main task. Each name has a colour that matches the timeline.
3. **Footage** — all videos, grouped by main task. **now** = the video you are watching. **wip** = already has at least one work mark.
4. **Keys** — click to expand the shortcut list on screen.

### Bottom bar

Status messages (saved, trimming, errors) appear on the right of the footer. Read them if something feels stuck.

---

## 5. How to label a video

Work from the **start** of the video toward the end. Cover the whole timeline: every second should be either a **subtask** or **garbage**.

### Mark a useful action

1. Press **Space** to play. Use **←** and **→** to change speed (0.5× to 5×).
2. Watch until the action **ends**.
3. Press **T** once. ( **D** does the same thing.)
4. The panel shows a **pending** range, for example `0:12 → 0:18 — type a label and press Enter`.
5. Type the subtask name (for example `pick-cloth`) **or** use **↓ / ↑** to highlight an existing name.
6. Press **Enter**.
7. The segment is saved. Play on to the next action.

The first press of **T** marks the end of the piece. Do not press **T** a second time to “cancel”. If the mark is wrong, use **Ctrl+Z**.

### Naming subtasks

- Use short, clear names: `pick-cloth`, `place-label`, `fold-sleeve`.
- Spell the same action the **same way** every time. The list on the right is there so you can pick instead of retyping.
- A new name is created when you type it and press **Enter**.
- Later videos in the same main task already show those names.

### Garbage

If the stretch is not a real work action, scrub to the end of that stretch and press **G**. Garbage does **not** count toward usable labeled hours.

### Next video

When this video is done:

- Press **N**, or
- Click **Next video** in the ScaleAI panel, or
- Click another file in the **Footage** list.

If a mark is still **pending** (you pressed **T** but have not assigned a name), **N** and the Footage list stay on this video. Press **Enter** or **U** to save, or **Ctrl+Z** to clear, then change videos.

Labels are already saved after **Enter** / **U** / **G**. You do not wait for a save.

You cannot press **N** while **Trim** or **Stitch** is still running. The status line at the bottom explains why.

---

## 6. Keyboard shortcuts

Print this table and keep it next to the monitor.

| Key | Action |
|-----|--------|
| **Space** | Play or pause (play always starts at 1×) |
| **←** or **[** | Slower by 0.5× |
| **→** or **]** | Faster by 0.5× |
| **,** | Jump back 0.1 seconds |
| **.** | Jump forward 0.1 seconds |
| **Shift + ,** or **<** | Back one frame |
| **Shift + .** or **>** | Forward one frame |
| **T** or **D** | Mark the end of a work segment |
| **↓** / **↑** | Highlight a subtask in the list |
| **Enter** | Assign the highlighted or typed name (creates a new name if needed) |
| **G** | Mark garbage up to the playhead |
| **U** | If a mark is pending: save it as **Unlabeled task**. Otherwise: undo |
| **Ctrl+Z** | Undo the last mark, or clear a pending mark |
| **N** | Next video (blocked while a mark is pending, or while Trim/Stitch is running) |
| **Home** | Jump to the start of the video |
| **Esc** | Leave the name box without assigning |
| **I** | Set WhatsApp clip **in** point (see section 9) |
| **O** | Set WhatsApp clip **out** point |

While the name box is focused, **T** is a normal letter so you can type names such as `taking cloth`. Shortcuts work again when you leave that box.

---

## 7. Unlabeled clips, mistakes, and highlights

### Unlabeled task

If you know the time range is work but you are **not sure of the name**:

1. Press **T** at the end of the action.
2. Press **U** (do not type a name).

The segment is saved as **Unlabeled task**. It still counts as labeled work on this video. You can assign a real name later from the dropdown on that row in the ScaleAI panel.

The panel shows **UNLABELED IN VIDEO** with a count. Click that row to highlight those stretches on the timeline.

### Fix a wrong name

- **Ctrl+Z** immediately after a mistake.
- **Delete** on a segment row removes that mark so you can mark it again.
- For an unlabeled row, use **Assign label later…** and pick a real subtask. The clip file is renamed and moved into the correct subtask folder if it was already trimmed.

### See one subtask on the timeline

Click a subtask name (in the task list or in the ScaleAI counts). Matching pieces light up under the player. Click the same name on a **segment row** to jump the playhead to that piece. Click elsewhere on the page to clear the highlight.

Pressing **T** and assigning a name does **not** leave the labeled-region bar highlighted. That glow is only for when you click a name or count to find a mark.

---

## 8. Trim and stitch

Label first. Cut files second. Do not close the black window while these buttons are working.

| Button | When to use it | What it does |
|--------|----------------|--------------|
| **Trim this video** | After the current video is fully marked | Cuts each labeled piece into a short MP4 inside that subtask’s folder |
| **Trim whole folder** | After a **main task** folder is labeled | Trims every source video in that folder |
| **Stitch each subtask** | After trims for that main task are done | Joins all clips of the same subtask into one `…-stitched.mp4` |

While trim or stitch is running, the buttons show **Trimming…** or **Stitching…**. The status line at the bottom reports progress. **N** is blocked until it finishes.

### Where clips go

Example after trim (names will match your camera and subtasks):

```text
garment-folding-general/
  pick-cloth-001/
    CAM001-001-001.mp4
    CAM001-001-002.mp4
  place-label-002/
    CAM001-002-001.mp4
```

After stitch, each subtask folder also gets a combined file, for example:

`pick-cloth-001/pick-cloth-001-stitched.mp4`

Unlabeled clips go in an **Unlabeled-task** folder until you assign a real name. Assigning a name moves the clip into the correct folder and changes only the **middle number** in the file name (the subtask id). The camera id and clip number stay the same.

---

## 9. Sending a short clip on WhatsApp

Use this when a supervisor needs to see a few seconds of footage.

**From the player**

1. Pause where the clip should start. Press **I** (or **In**).
2. Pause where it should end. Press **O** (or **Out**).
3. Click **Download for WhatsApp**.
4. Wait for encoding, then save the file and send it.

**From an unlabeled segment**

Each unlabeled row has a **Download** button that encodes that one piece.

Clips longer than **5 minutes** cannot be downloaded this way. Mark a shorter range instead.

---

## 10. How to stop at the end of a shift

1. Finish any **Trim** or **Stitch** that is still running (wait until the buttons return to normal).
2. You may close the browser tab.
3. Click the black window, press **Ctrl+C**, and wait until it says the window can close — or close the black window.

Your labels are already on disk. Tomorrow, open `run.bat` again, open the same 50-hour folder, and continue from the Footage list.

---

## 11. If something goes wrong

| What you see | What to do |
|--------------|------------|
| Browser did not open | Open Chrome or Edge and go to `http://127.0.0.1:8765/review` |
| Page will not load | The black window is not running. Start `run.bat` again. |
| Black window closed | Start `run.bat` again. Do not work in an old browser tab until it is running. |
| Empty Footage list | You opened a folder that has no task folders with MP4s. Open the **parent** 50-hour folder. |
| **Preparing browser-compatible preview** for a long time | Wait. Large files take time. If it never finishes, restart `run.bat` and click **Scan**. |
| Video is black or will not play | Wait for the compatible preview. If it never starts, restart `run.bat` and click **Scan**. |
| **T** seems to do nothing | You may be typing in the name box. Click the player, then press **T**. If a mark is already pending, assign it with **Enter** or undo with **Ctrl+Z**. |
| Typed a name with **T** in it and something jumped | Click in the name box first; **T** is then just a letter. |
| Wrong folder / wrong task | Click **Open 50-hour folder** again. |
| Trim failed | Read the red status at the bottom. Do not delete videos. Tell your supervisor. |
| **Update** asked for confirmation | Only continue if a supervisor told you to. The app restarts itself; wait for the page to reload. |

If nothing above helps: leave the black window open, take a screenshot of the **full screen** including the bottom status line, and send it to your supervisor.

---

## 12. Rules — do not do these

- Do not close the **black window** while you are labeling, trimming, or stitching.
- Do not rename, move, or delete **MP4** or **JSON** files yourself.
- Do not edit `manifest.json` or any `.json` file in Notepad.
- Do not open the same footage folder on two PCs at the same time.
- Do not use the **Cleaner** page for this 50-hour job unless a supervisor says so.
- Do not pull **Update** unless you were asked to.

---

## 13. For team leads — files the program writes

Labellers do not need this section. It is here so leads know what appears on disk.

| File or folder | Role |
|----------------|------|
| `{video}.json` next to each source MP4 | That video’s times, labels, camera info, and clip references. Example: `GX010001.MP4` → `GX010001.json` |
| `manifest.json` in the main-task folder | Shared subtask names and stable three-digit IDs for the whole task |
| `_labeling/progress.json` | Dataset progress (usable hours; garbage excluded) |
| `{subtask-name}-{id}/` | Trimmed clips for that subtask, from every source video |
| `{subtask}-stitched.mp4` | One joined video per subtask, inside that subtask folder |

Older leftover files such as `segment.json` or `*.segments.json` are migrated or cleaned when a 50-hour folder is opened. Do not put them back.

Trims copy the GoPro motion metadata when it exists. If that copy would be lost, trim **stops** instead of writing a broken clip.

There is no Stage 1 / Stage 2 cycle on this version. Scale AI 50-hour mode is the default.

---

## Quick recap

1. Double-click **`run.bat`**. Leave the black window open.
2. Click **Open 50-hour folder** and pick the parent folder.
3. Play → **T** at the end of an action → type or pick a name → **Enter**.
4. **G** for junk. **U** if you need to name it later. **Ctrl+Z** to undo. **N** for the next video.
5. **Trim this video** when the video is fully marked. **Stitch each subtask** when the main task’s clips are ready.
6. At the end of the day, wait for trim/stitch to finish, then close the browser and the black window.
