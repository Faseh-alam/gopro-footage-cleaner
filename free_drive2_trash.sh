#!/bin/bash
# Run in Terminal.app (with Full Disk Access enabled for Terminal).
# Deletes trashed originals that already have trimmed clips saved.

TRASH="/Volumes/Drive 2/.Trashes/$(id -u)"
ROOT="/Users/faz/Documents/Footage cleaning"

if [ ! -d "$TRASH" ]; then
  echo "Trash folder not found: $TRASH"
  echo "Is Drive 2 plugged in?"
  exit 1
fi

cd "$ROOT" || exit 1

FILES="$(python3 -c "
from recover_drive2_footage import audit_rows, safe_trash_deletions
for name in safe_trash_deletions(audit_rows()):
    print(name)
")"

if [ -z "$FILES" ]; then
  echo "No safe Trash items found."
  exit 0
fi

COUNT="$(printf '%s\n' "$FILES" | wc -l | tr -d ' ')"
echo "Will permanently delete $COUNT trashed originals (clips already saved)."
printf "Press Enter to continue, or Ctrl+C to cancel: "
read -r _

deleted=0
failed=0
while IFS= read -r name; do
  [ -z "$name" ] && continue
  path="$TRASH/$name"
  if [ -f "$path" ]; then
    if rm -f "$path"; then
      echo "  deleted $name"
      deleted=$((deleted + 1))
    else
      echo "  FAILED $name"
      failed=$((failed + 1))
    fi
  else
    echo "  skip (not in trash): $name"
  fi
done <<EOF
$FILES
EOF

echo ""
echo "Done: $deleted deleted, $failed failed"
df -h "/Volumes/Drive 2" | tail -1
