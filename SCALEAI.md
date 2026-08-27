# ScaleAI micro-task pipeline (plain language)

Branch: `ScaleAI` — separate from the normal textile review/offload product.

## What you asked for (example)

One 30‑minute stitching video. The person grabs cloth ~20 times.

1. You mark each grab as a tiny work segment and name the task `grab-cloth`
   (maybe 0.5–0.7s each).
2. Those marks are saved as **JSON next to the video** (no heavy trim yet).
3. When you process that video, the app cuts those 20 snippets and puts them in
   a folder named `grab-cloth/`.
4. Then it **stitches** them into **one file**:

```text
grab-cloth__stitched.MP4   ≈ 0.7s × 20 ≈ 14 seconds
```

That 14‑second video is *only* grabbing cloth, back to back. Same idea for
`place-on-machine`, `stretch-forward`, etc.

That is what “one stitched file per micro-task” means — **not** “per SD card”
and **not** “per Google Drive folder”. One micro-task name → one combined clip
(for the footage you processed).

## Where the videos live

Google Drive download on a local PC is fine. Use **Open footage** / browse to
that folder. No SD card required.

## Weak labeling PCs vs strong process PC

**Labeling (weak PC)** only writes sidecars:

```text
GX010123.MP4
GX010123.segments.json     ← timestamps + task names (grab-cloth, …)
GX010123.segments.txt
```

Trimming dozens of sub‑second clips is slow on weak machines. So the flow is:

| Step | Where | What happens |
|------|--------|--------------|
| Mark work / garbage / tasks | Label PC | JSON only |
| Next video | Label PC | Leave JSON; no trim |
| **Trim this video** (optional) | Label PC or strong PC | Cuts clips into `grab-cloth/`, … |
| **Trim + stitch this video** | Prefer strong PC | Cut, then build `*__stitched.MP4` |
| Copy folder to strong PC | USB / network | Take MP4s + `.segments.json` |

You can mark everything first, then process overnight on the better machine.

## What is `eager_tasks.json`?

It is **only the dropdown list of task names** the app remembers.

- Normal product list (coarse textile names like `Garment-Edge-Hemming`) lives in
  `eager_tasks.json` / `eager_tasks.default.json`.
- ScaleAI starts from **`scaleai_tasks.json`**, which begins **empty**.
- You **add micro-tasks live** with “New task” while labeling
  (`grab-cloth`, `fold-once`, `put-in-bag`, …).
- Those names are saved so tomorrow’s session still has them.

It is **not** the footage, **not** the timestamps, and **not** the stitched
output. Timestamps live in `*.segments.json` beside each video.

## Scrub precision (recommendation for textile)

Folding / grab / place actions are often **0.2–0.5s**. Current `,` / `.` at
**1 second** is too coarse for that.

In **ScaleAI mode**:

- `,` / `.` move **0.1s** (good default for textile micro-tasks)
- Hold **Shift** + `,` / `.` for **one frame** (~1/30s ≈ 0.033s) when you need
  tighter edges

You do not need perfect frame accuracy on every mark; 0.1s is usually enough
if labeling discipline is good.

## IMU / GPMF

Individual trims keep IMU (stream copy). Stitched files also map the `gpmd`
track. Absolute continuous time across joins is not guaranteed; each segment’s
IMU is preserved in order. Stitch **fails** if IMU would be lost.

## Safety

- Marking never deletes source videos.
- Trim/stitch never overwrite an existing `*__stitched.MP4` unless you confirm
  overwrite.
- Individual clips stay on disk after stitch until you delete them yourself.
