# Pack 30h footage onto 10 Voiceover USBs

Source footage (your Mac):

```text
/Users/faz/wc-sample-30h/videos/<class-name>/*.mp4
```

(~79 GB, 61 classes, 360 clips)

## What each USB gets

```text
USB stick/
  README.txt              ← simple student instructions
  MANIFEST.txt            ← list of classes on this stick
  VoiceoverStation/       ← the app (start-voiceover.bat / .command)
  voiceover/
    class-a/
      *.mp4
    class-b/
      *.mp4
```

You do **not** copy the whole repo by hand. The packer copies a slim app bundle (no `.git`, no `node_modules`, no `.venv`).

## Step 1 — make the plan (once)

```bash
cd "/Users/faz/Documents/Footage cleaning"
chmod +x start-pack-usbs.sh
./start-pack-usbs.sh plan \
  --source "/Users/faz/wc-sample-30h/videos" \
  --sticks 10 \
  --out "/Users/faz/wc-sample-30h/usb_plan.json"
```

This writes a balanced assignment (whole classes, never split mid-class).

## Step 2 — fill sticks in parallel (4 at a time)

1. Plug in up to 4 empty USBs (ExFAT if Mac+Windows). Rename them in Finder if they all say “NO NAME” (`USB01`…).
2. Run:

```bash
./start-pack-usbs.sh fill --plan "/Users/faz/wc-sample-30h/usb_plan.json"
```

3. It lists plugged volumes and maps the next sticks, e.g. USB-01→vol1, USB-02→vol2…
4. Type `Y` — **all plugged sticks copy at the same time** (one progress + ETA line each).
5. When the batch finishes: eject, plug the next 4, press Enter, repeat.

(Old one-by-one mode: add `--one`.)

Check progress anytime:

```bash
./start-pack-usbs.sh show --plan "/Users/faz/wc-sample-30h/usb_plan.json" -v
```

## Student message (after sticks are ready)

```text
1) Plug in YOUR USB + mic
2) Open the USB → open VoiceoverStation
3) Windows: double-click start-voiceover.bat
   Mac: double-click start-voiceover.command
4) Browser opens → Open voiceover folder → select the "voiceover" folder on THIS USB
5) R = record, Space = pause video, R = save, N = next
```

Also see `README.txt` on each stick.

## Updating VoiceoverStation on already-packed USBs

You do **not** need to re-copy all footage.

After rebuilding the app on your Mac:

```bash
cd "/Users/faz/Documents/Footage cleaning"
# plug the USBs (4 at a time is fine)
./start-pack-usbs.sh update-app
```

That replaces only `VoiceoverStation/` on each plugged stick. Footage in `voiceover/` stays.

## Notes

- First app launch on a lab PC needs Python + internet once.
- Prefer copying `VoiceoverStation` to the Desktop if the USB is slow; still open the USB `voiceover` folder for clips.
- USBs should have ~12 GB+ free each for the original pack.
