# Scale AI 50-hour labeling

Keyboard-first subtask labeling for folders shaped like:

```text
50-hour/
  garment-folding-general/
    GX010001.MP4
    GX010001.json
  gear-box-fitting/
    GX010002.MP4
```

## Flow

1. Enable **ScaleAI 50-hour mode** (default on this branch).
2. Click **Open 50-hour folder** and pick the local dataset root (SSD now, SD card later).
3. Parent task = first folder under that root.
4. Press **T** (or **D**) at the end of a subtask → type a label → **Enter**.
   - Existing labels autocomplete/filter.
   - A new name is created on Enter and saved under `_labeling/tasks.json`.
5. **G** marks garbage (excluded from usable hours).
6. **U** undoes; **N** goes to the next video (annotations already autosaved).
7. **Trim this video** exports:

```text
GX010001/
  pick-cloth/
    0001.MP4
    0002.MP4
  export_manifest.csv
```

Trims use stream-copy and fail closed if source GPMF would be lost.

8. **Stitch this video** concatenates every clip inside each subtask folder into
   `pick-cloth__stitched.MP4` (same stream-copy + GPMF/`gpmd` path as trim).

## Files

| Path | Purpose |
|------|---------|
| `VIDEO.json` | Adjacent annotation (segments + camera/CL metadata) |
| `_labeling/tasks.json` | Per-parent-task label vocabulary + optional target hours |
| `_labeling/progress.json` | Dataset progress (usable labeled hours; garbage excluded) |

There is no cycle / Stage 1–2 workflow on this branch.
