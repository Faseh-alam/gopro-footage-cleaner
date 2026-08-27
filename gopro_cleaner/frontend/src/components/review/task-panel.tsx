import { useState } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { TextInput } from "@/components/wc/field";
import type { ReviewController } from "./useReviewController";
import { taskColor } from "./task-color";
import { ScaleAiPanel } from "./scaleai-panel";

export function TaskPanel({ c }: { c: ReviewController }) {
  const [newTask, setNewTask] = useState("");
  const groups = c.orderedTaskGroups();

  const renderTask = (task: string) => {
    const deletable = c.isUserDefinedTask(task);
    const color = taskColor(task);
    return (
      <div
        key={task}
        className={cn(
          "flex min-w-0 items-center gap-0.5 rounded-sm border pr-0.5 transition-colors",
          task === c.selectedTaskValue
            ? "border-accent/50 bg-accent/10"
            : "border-transparent hover:bg-surface-2",
        )}
      >
        <button
          type="button"
          onClick={() => {
            c.setSelectedTaskValue(task);
            c.touchRecentTask(task);
            if (c.scaleAiMode && c.scaleAiPending) {
              void c.commitScaleAiSegment(task, "subtask");
            }
          }}
          className={cn(
            "flex min-w-0 flex-1 items-center gap-2 truncate px-2 py-1.5 text-left text-xs transition-colors",
            task === c.selectedTaskValue ? "text-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          <span
            className="size-2.5 shrink-0 rounded-full"
            style={{ backgroundColor: color.solid }}
            aria-hidden
          />
          <span className="truncate">{task}</span>
        </button>
        {deletable ? (
          <button
            type="button"
            title={`Remove task “${task}”`}
            aria-label={`Remove task ${task}`}
            onClick={(e) => {
              e.stopPropagation();
              void c.removeTask(task);
            }}
            className="grid size-7 shrink-0 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-destructive/15 hover:text-destructive"
          >
            <X className="size-3.5" strokeWidth={2} />
          </button>
        ) : null}
      </div>
    );
  };

  const taskPicker = (
    <>
      <TextInput
        ref={c.taskSearchRef}
        type="search"
        autoComplete="off"
        placeholder={
          c.scaleAiMode
            ? "Type label · Enter assigns (creates if new)"
            : "Filter tasks…"
        }
        value={c.taskSearch}
        onChange={(e) => c.setTaskSearch(e.currentTarget.value)}
        onFocus={() => c.focusTaskSearch()}
        onKeyDown={(e) => {
          // Enter is handled by the review capture handler so ↑/↓ selection wins
          // over the raw filter text. Do not call labelCurrentClip() here.
          if (e.key === "Escape") {
            e.preventDefault();
            e.currentTarget.blur();
            c.playerWrapRef.current?.focus?.();
          }
        }}
        className="h-8 text-xs"
      />

      <div className="max-h-56 overflow-auto rounded-sm border border-border p-1">
        {groups.matches.length === 0 && (
          <div className="px-2 py-4 text-center text-xs text-muted-foreground">
            {c.scaleAiMode
              ? c.taskSearch.trim()
                ? `Enter to create “${c.taskSearch.trim()}”`
                : "Type a subtask label"
              : "No matching tasks"}
          </div>
        )}
        {groups.recent.length > 0 && <div className="eyebrow px-2 py-1">Recent</div>}
        {groups.recent.map(renderTask)}
        {groups.others.length > 0 && groups.recent.length > 0 && (
          <div className="eyebrow px-2 py-1">All</div>
        )}
        {groups.others.map(renderTask)}
      </div>

      {!c.scaleAiMode ? (
        <div className="flex gap-2">
          <TextInput
            ref={c.newTaskInputRef}
            placeholder="New task (A)"
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
      ) : null}
    </>
  );

  return (
    <div className="grid gap-3 border-b border-border p-4">
      <label className="flex items-center gap-2 text-xs">
        <input
          type="checkbox"
          checked={c.scaleAiMode}
          onChange={(e) => void c.setScaleAiMode(e.currentTarget.checked)}
        />
        <span className="font-medium">ScaleAI 50-hour mode</span>
      </label>

      {c.scaleAiMode ? (
        <>
          <ScaleAiPanel c={c} />
          <div className="grid gap-3">
            <div className="eyebrow">Subtask labels</div>
            {taskPicker}
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              T → type label → Enter · G = garbage · U = undo · N = next
            </p>
            <Button size="sm" variant="outline" onClick={() => void c.undoSegment()}>
              Undo last{" "}
              <kbd className="ml-1 font-mono text-[10px] text-muted-foreground">U</kbd>
            </Button>
          </div>
        </>
      ) : (
        <>
          <div className="eyebrow">Task</div>
          {taskPicker}
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            Pick a listed task · T then Enter repeats last · G = garbage
          </p>
          <div className="grid gap-2">
            <Button size="sm" variant="outline" onClick={() => c.undoSegment()}>
              Delete last markup{" "}
              <kbd className="ml-1 font-mono text-[10px] text-muted-foreground">U</kbd>
            </Button>
            <Button size="sm" variant="destructive" onClick={() => c.deleteCurrentFile()}>
              Move video to Trash
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
