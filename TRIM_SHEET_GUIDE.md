# Trim Sheet — for helpers

Your manager will give you a sheet that **already lists every video**.  
You only fill in the **timestamps** column.

## Columns

| date | camera | task | footage | timestamps |
|------|--------|------|---------|------------|
| 24-04-26 | C8278 | | GX012185.MP4 | `0 - 21.03, 22.42 - 48.10` |
| 26-04-26 | C5223 | task-Safety-Sticker | GX014891.MP4 | `0 - 5.30` |

- **date** — already filled in (folder name)
- **camera** — already filled in (camera serial, e.g. C8278)
- **task** — already filled in (leave blank if empty)
- **footage** — already filled in (video file name)
- **timestamps** — **you fill this in**

## How to write timestamps

Use the same style as before:

```text
0 - 21.03, 22.42 - 48.10
```

- `0` = start of video
- `21.03` = 21 minutes and 3 seconds
- `22.42 - 48.10` = from 22:42 to 48:10
- Separate multiple clips with a comma

### Examples

| timestamps |
|------------|
| `0 - 21.03` |
| `0 - 44.04` |
| `2.12 - 6.33, 8.36 - 11.06` |
| `9.34 - 34.14` |

**Skip videos with no useful footage** — leave the timestamps cell blank.

## When you are done

Save as CSV and send it back. Do not change the date, camera, or footage columns.
