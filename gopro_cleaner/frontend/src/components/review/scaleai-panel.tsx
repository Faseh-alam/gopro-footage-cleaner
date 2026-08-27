import { Check, Download, Play, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { taskColor } from "./task-color";
import type { ReviewController } from "./useReviewController";

export function ScaleAiPanel({ c }: { c: ReviewController }) {
  const annotation = c.currentScaleAi();
  const cycles = annotation?.parent_cycles || [];
  const activeCycle =
    cycles.find((cycle) => cycle.id === c.scaleAiActiveCycleId) || cycles[0] || null;
  const activeSegments = (annotation?.subtask_segments || []).filter(
    (segment) => segment.parent_cycle_id === activeCycle?.id,
  );

  return (
    <div className="grid gap-3 rounded-sm border border-border bg-surface-2/40 p-3">
      <div>
        <div className="eyebrow">Two-stage ScaleAI</div>
        <div className="mt-1 truncate text-sm font-semibold">
          {annotation?.parent_task || "Open a task folder"}
        </div>
        <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
          Parent task is inferred from the folder name. Both stages save JSON only.
        </p>
        {annotation?.parent_example ? (
          <p className="mt-1 text-[10px] font-medium text-success">
            Example selected · {c.basenamePath(annotation.parent_example.source)}
          </p>
        ) : null}
      </div>

      <div className="grid grid-cols-2 overflow-hidden rounded-sm border border-border">
        <button
          type="button"
          onClick={() => void c.changeScaleAiStage("parent")}
          className={`px-2 py-2 text-xs ${
            c.scaleAiStage === "parent"
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-surface-2"
          }`}
        >
          1 · Parent cycles
        </button>
        <button
          type="button"
          onClick={() => void c.changeScaleAiStage("subtask")}
          className={`border-l border-border px-2 py-2 text-xs ${
            c.scaleAiStage === "subtask"
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-surface-2"
          }`}
        >
          2 · Subtasks
        </button>
      </div>

      {c.scaleAiStage === "parent" ? (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Button
              size="sm"
              variant={c.scaleAiParentStart == null ? "outline" : "accent"}
              onClick={() => c.markScaleAiParentStart()}
            >
              {c.scaleAiParentStart == null
                ? "Set cycle start"
                : `Start ${c.formatTime(c.scaleAiParentStart)}`}
            </Button>
            <Button
              size="sm"
              variant="accent"
              disabled={c.scaleAiParentStart == null}
              onClick={() => void c.saveScaleAiParentCycle()}
            >
              End + save cycle
            </Button>
          </div>

          <div className="max-h-56 overflow-auto rounded-sm border border-border">
            {!cycles.length ? (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">
                No clean cycles yet. Leave garbage unmarked.
              </p>
            ) : (
              cycles.map((cycle, index) => {
                const selected = annotation?.example_cycle_id === cycle.id;
                return (
                  <div
                    key={cycle.id}
                    className="flex items-center gap-2 border-b border-border px-2 py-2 last:border-b-0"
                  >
                    <button
                      type="button"
                      onClick={() => c.selectScaleAiCycle(cycle.id)}
                      className="grid size-7 shrink-0 place-items-center rounded-sm hover:bg-surface-2"
                      title="Play / inspect this cycle"
                    >
                      <Play className="size-3.5" />
                    </button>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium">
                        Cycle {index + 1}
                        {selected ? (
                          <span className="ml-1.5 text-success">· example</span>
                        ) : null}
                      </div>
                      <div className="font-mono text-[10px] text-muted-foreground">
                        {c.formatTime(cycle.start)} → {c.formatTime(cycle.end)} ·{" "}
                        {(cycle.end - cycle.start).toFixed(1)}s
                      </div>
                    </div>
                    <button
                      type="button"
                      disabled={c.scaleAiExampleBusy}
                      onClick={() => void c.prepareScaleAiExample(cycle.id)}
                      className={`grid size-7 shrink-0 place-items-center rounded-sm ${
                        selected
                          ? "bg-success/15 text-success"
                          : "text-muted-foreground hover:bg-accent/15 hover:text-accent"
                      }`}
                      title={
                        selected
                          ? "Download selected WhatsApp example again"
                          : "Select and prepare as WhatsApp example"
                      }
                    >
                      {selected ? <Check className="size-3.5" /> : <Download className="size-3.5" />}
                    </button>
                    <button
                      type="button"
                      onClick={() => void c.deleteScaleAiParentCycle(cycle.id)}
                      className="grid size-7 shrink-0 place-items-center rounded-sm text-muted-foreground hover:bg-destructive/15 hover:text-destructive"
                      title="Delete cycle and linked subtasks"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                );
              })
            )}
          </div>

          <Button size="sm" variant="outline" onClick={() => void c.nextScaleAiVideo()}>
            Next video · JSON only
          </Button>
        </>
      ) : (
        <>
          <div className="flex flex-wrap gap-1">
            {cycles.map((cycle, index) => (
              <button
                key={cycle.id}
                type="button"
                onClick={() => c.selectScaleAiCycle(cycle.id)}
                className={`rounded-sm border px-2 py-1 font-mono text-[10px] ${
                  cycle.id === activeCycle?.id
                    ? "border-accent bg-accent/15 text-accent"
                    : "border-border text-muted-foreground"
                }`}
              >
                Cycle {index + 1}
              </button>
            ))}
          </div>

          {activeCycle ? (
            <p className="font-mono text-[10px] text-muted-foreground">
              Allowed window {c.formatTime(activeCycle.start)} → {c.formatTime(activeCycle.end)}
            </p>
          ) : null}

          <div className="grid grid-cols-2 gap-2">
            <Button
              size="sm"
              variant={c.scaleAiSubtaskStart == null ? "outline" : "accent"}
              disabled={!activeCycle}
              onClick={() => c.markScaleAiSubtaskStart()}
            >
              {c.scaleAiSubtaskStart == null
                ? "Set subtask start"
                : `Start ${c.formatTime(c.scaleAiSubtaskStart)}`}
            </Button>
            <Button
              size="sm"
              variant="accent"
              disabled={c.scaleAiSubtaskStart == null || !c.selectedTaskValue}
              onClick={() => void c.saveScaleAiSubtask()}
            >
              End + save subtask
            </Button>
          </div>

          <div className="max-h-44 overflow-auto rounded-sm border border-border">
            {!activeSegments.length ? (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">
                Choose/add a subtask below, then mark it inside this cycle.
              </p>
            ) : (
              activeSegments.map((segment) => {
                const color = taskColor(segment.task);
                return (
                  <div
                    key={segment.id}
                    className="flex items-center gap-2 border-b border-border px-2 py-2 last:border-b-0"
                  >
                    <span
                      className="size-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: color.solid }}
                    />
                    <button
                      type="button"
                      onClick={() => c.scheduleSeek(segment.start, true)}
                      className="min-w-0 flex-1 truncate text-left text-xs"
                    >
                      {segment.task}
                    </button>
                    <span className="font-mono text-[9px] text-muted-foreground">
                      {(segment.end - segment.start).toFixed(2)}s
                    </span>
                    <button
                      type="button"
                      onClick={() => void c.deleteScaleAiSubtask(segment.id)}
                      className="grid size-7 place-items-center text-muted-foreground hover:text-destructive"
                      title="Delete subtask segment"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </div>
                );
              })
            )}
          </div>

          <div className="grid gap-1.5">
            <Button
              size="sm"
              variant="outline"
              onClick={() => void c.processCurrentVideoScaleAi({ stitch: false })}
            >
              Process this video later / now
            </Button>
            <Button size="sm" variant="accent" onClick={() => void c.processScaleAiFolder()}>
              Process folder + stitch
            </Button>
            <Button size="sm" variant="outline" onClick={() => void c.nextScaleAiVideo()}>
              Next video · JSON only
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
