import { useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { TextInput } from "@/components/wc/field";
import type { ReviewController } from "./useReviewController";

export function TaskPanel({ c }: { c: ReviewController }) {
  const [newTask, setNewTask] = useState("");
  const groups = c.orderedTaskGroups();

  const renderTask = (task: string) => (
    <button
      key={task}
      type="button"
      onClick={() => {
        c.setSelectedTaskValue(task);
        c.touchRecentTask(task);
      }}
      className={cn(
        "block w-full truncate rounded-sm border px-2 py-1.5 text-left text-xs transition-colors",
        task === c.selectedTaskValue
          ? "border-accent/50 bg-accent/10 text-foreground"
          : "border-transparent text-muted-foreground hover:bg-surface-2 hover:text-foreground",
      )}
    >
      {task}
    </button>
  );

  return (
    <div className="grid gap-3 border-b border-border p-4">
      <div className="eyebrow">Task</div>

      <TextInput
        ref={c.taskSearchRef}
        type="search"
        autoComplete="off"
        placeholder="Filter tasks… (recent pinned above)"
        value={c.taskSearch}
        onChange={(e) => c.setTaskSearch(e.currentTarget.value)}
        onFocus={() => c.focusTaskSearch()}
        className="h-8 text-xs"
      />

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Filter only · pick a listed task · New task below to create · T then Enter repeats last · G = garbage
      </p>

      <div className="max-h-56 overflow-auto rounded-sm border border-border p-1">
        {groups.matches.length === 0 && (
          <div className="px-2 py-4 text-center text-xs text-muted-foreground">No matching tasks</div>
        )}
        {groups.recent.length > 0 && <div className="eyebrow px-2 py-1">Recent</div>}
        {groups.recent.map(renderTask)}
        {groups.others.length > 0 && groups.recent.length > 0 && <div className="eyebrow px-2 py-1">All</div>}
        {groups.others.map(renderTask)}
      </div>

      <div className="flex gap-2">
        <TextInput
          placeholder="New task"
          value={newTask}
          onChange={(e) => setNewTask(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              c.addTask(newTask);
              setNewTask("");
            }
            if (e.key === "Escape") {
              e.preventDefault();
              e.currentTarget.blur();
              c.playerWrapRef.current?.focus?.();
            }
          }}
          className="h-8 text-xs"
        />
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            c.addTask(newTask);
            setNewTask("");
          }}
        >
          Add
        </Button>
      </div>

      <div className="grid gap-2">
        <Button size="sm" variant="outline" onClick={() => c.undoSegment()}>
          Delete last markup <kbd className="ml-1 font-mono text-[10px] text-muted-foreground">U</kbd>
        </Button>
        <Button size="sm" variant="destructive" onClick={() => c.deleteCurrentFile()}>
          Move video to Trash
        </Button>
      </div>
    </div>
  );
}
