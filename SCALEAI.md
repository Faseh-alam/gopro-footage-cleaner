# ScaleAI two-stage micro-task pipeline

Branch: `ScaleAI` — separate from the normal textile review/offload product.

## Folder layout and parent task

Open the parent-task folder, for example:

```text
50 hours/AWS/Label Attachment/
  GX010123.MP4
  GX010124.MP4
```

The app infers `Label Attachment` from the folder name. Labelers do not choose
the parent task again for every video.

## Stage 1 — clean parent-task cycles

1. Turn on **ScaleAI two-stage mode**.
2. Stay on **1 · Parent cycles**.
3. At the beginning of a complete clean repetition, click **Set cycle start**.
4. At its end, click **End + save cycle**.
5. Leave garbage unmarked. Delete and redo a cycle if its boundaries are weak.
6. Choose the cleanest cycle with the download icon. The app records it as the
   one parent-task example and downloads a small 720p WhatsApp MP4.
7. Click **Next video · JSON only**.

Stage 1 does not trim footage. It writes:

```text
GX010123.scaleai.json
```

The existing `GX010123.segments.json` normal annotation is not changed.

## Stage 2 — CEO-confirmed subtasks

After the CEO returns the subtask names:

1. Switch to **2 · Subtasks**.
2. Add the confirmed names, such as `grab-cloth`, `position-label`, and
   `release-cloth`.
3. Select a parent cycle. Playback and frame stepping stay inside that saved
   parent window and automatically continue to the next cycle.
4. Choose a subtask, click **Set subtask start**, then **End + save subtask**.
5. Delete and redo weak ranges. A subtask that crosses its parent-cycle boundary
   is rejected by both the UI and server.

Subtask colors are stable in the task list and timeline. Parent windows keep
the stable parent-task color.

## Weak labeling PCs

Both stages only update `*.scaleai.json`. Copy the original MP4s together with
their sidecars to the strong processing PC. No preliminary trim is required.

ScaleAI precision:

- `,` / `.`: 0.1 seconds
- Shift + `,` / `.`: one frame (approximately 1/30 second)
- Enter: set/save the current stage boundary
- N: next video without trimming

## Strong PC processing

Use **Process folder + stitch** after Stage 2 is complete. The app:

1. reads every layered sidecar below the opened folder;
2. trims only confirmed subtask ranges;
3. writes clips under `_ScaleAI/<subtask>/`;
4. stitches clips with the same subtask into `<subtask>__stitched.MP4`;
5. writes a manifest mapping every stitched interval back to source video,
   parent-cycle ID, and source timestamps.

Individual trims use stream copy and retain GoPro GPMF/IMU. Stitching maps the
`gpmd` track and fails closed when GPMF is expected but missing. Source videos
and individual trims are never deleted by this workflow. Existing stitched
outputs are not overwritten unless overwrite is explicitly enabled.
