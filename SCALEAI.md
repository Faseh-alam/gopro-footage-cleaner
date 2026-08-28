# Scale AI 50-hour labeling

Keyboard-first subtask labeling for folders shaped like:

```text
50-hour/
  garment-folding-general/
    GX010001.MP4
    GX010001.json
    GX010002.MP4
    GX010002.json
    manifest.json
  gear-box-fitting/
    GX010002.MP4
```

## Flow

1. Enable **ScaleAI 50-hour mode** (default on this branch).
2. Click **Open 50-hour folder** and pick the local dataset root (SSD now, SD card later).
3. Parent task = first folder under that root.
4. Press **T** (or **D**) at the end of a subtask → type a label → **Enter**.
   - The first video starts with no predefined subtasks.
   - New names are assigned stable IDs in the task's `manifest.json`.
   - Later videos in the same main task automatically show those names.
5. **G** marks garbage (excluded from usable hours).
6. **U** undoes; **N** goes to the next video (annotations already autosaved).
7. **Trim this video** places clips in the shared folder for each subtask:

```text
garment-folding-general/
  pick-cloth-001/
    CAM001-001-001.mp4
    CAM001-001-002.mp4
  place-label-002/
    CAM001-002-001.mp4
```

Trims use stream-copy and fail closed if source GPMF would be lost.

8. **Stitch each subtask** collects clips from every source video in the main
   task and creates one output inside each subtask folder, such as
   `pick-cloth-001/pick-cloth-001-stitched.mp4`.

## Files

- `{video}.json`: that source video's camera/CL metadata, duration, timestamps,
  and generated clip references.
- `manifest.json`: subtask names, stable three-digit IDs, clip serial numbers,
  camera serials, source video names, and total clip counts for the whole task.
- `_labeling/progress.json`: dataset progress (usable labeled hours; garbage excluded).

There is no cycle / Stage 1–2 workflow on this branch.
