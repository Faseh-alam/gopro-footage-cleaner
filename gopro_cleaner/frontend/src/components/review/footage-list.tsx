import { cn } from "@/lib/utils";
import { Badge } from "@/components/wc/panel";
import type { ReviewController } from "./useReviewController";

export function FootageList({ c }: { c: ReviewController }) {
  const done = c.videos.filter((v) => {
    if (c.scaleAiMode) {
      const ann = c.scaleAiByPath[v.path];
      return Boolean(ann?.segments?.some((segment) => segment.type === "subtask"));
    }
    return c.isHandledPath(v.path);
  }).length;

  const groups = (() => {
    if (!c.scaleAiMode) return [{ task: "", videos: c.videos.map((v, i) => ({ v, i })) }];
    const map = new Map<string, { v: (typeof c.videos)[number]; i: number }[]>();
    c.videos.forEach((v, i) => {
      const task = v.parent_task || "Uncategorized";
      const list = map.get(task) || [];
      list.push({ v, i });
      map.set(task, list);
    });
    return Array.from(map.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([task, videos]) => ({ task, videos }));
  })();

  return (
    <div className="flex min-h-0 flex-1 flex-col border-b border-border">
      <div className="flex items-center justify-between gap-2 px-4 py-3">
        <div className="eyebrow">Footage</div>
        <span className="font-mono text-[10px] text-muted-foreground">
          {done}/{c.videos.length} labeled
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {c.videos.length === 0 && (
          <p className="px-4 pb-4 text-xs text-muted-foreground">
            Open a 50-hour folder or SD card to start reviewing.
          </p>
        )}
        {groups.map((group) => (
          <div key={group.task || "all"}>
            {c.scaleAiMode && group.task ? (
              <div className="sticky top-0 z-10 border-t border-border bg-surface px-4 py-1.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                {group.task}
              </div>
            ) : null}
            {group.videos.map(({ v, i }) => {
              const ann = c.annotationsByPath[v.path];
              const scaleAnn = c.scaleAiByPath[v.path];
              const openable = c.canOpenVideo(i);
              const hasLabels = Boolean(
                scaleAnn?.segments?.some((segment) => segment.type === "subtask"),
              );
              return (
                <button
                  key={v.path}
                  type="button"
                  disabled={!openable}
                  onClick={() => {
                    if (!openable) return;
                    void c.loadVideo(i);
                  }}
                  title={v.relative || v.name}
                  className={cn(
                    "flex w-full items-center justify-between gap-2 border-t border-border px-4 py-2 text-left text-xs transition-colors",
                    i === c.index && "bg-surface-2 text-foreground",
                    openable ? "hover:bg-surface-2" : "cursor-not-allowed opacity-45",
                  )}
                >
                  <span className="min-w-0 truncate">{v.name}</span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    {c.scaleAiMode ? (
                      hasLabels ? (
                        <Badge tone="ok">wip</Badge>
                      ) : i === c.index ? (
                        <Badge tone="accent">now</Badge>
                      ) : null
                    ) : ann?.complete ? (
                      <Badge tone="ok">done</Badge>
                    ) : ann?.segments?.length ? (
                      <Badge tone="warn">wip</Badge>
                    ) : i === c.index ? (
                      <Badge tone="accent">now</Badge>
                    ) : null}
                    {v.duration_label && (
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {v.duration_label}
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
