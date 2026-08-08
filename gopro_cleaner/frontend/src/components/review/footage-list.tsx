import { cn } from "@/lib/utils";
import { Badge } from "@/components/wc/panel";
import type { ReviewController } from "./useReviewController";

export function FootageList({ c }: { c: ReviewController }) {
  const done = c.videos.filter((v) => c.isHandledPath(v.path)).length;

  return (
    <div className="flex min-h-0 flex-1 flex-col border-b border-border">
      <div className="flex items-center justify-between gap-2 px-4 py-3">
        <div className="eyebrow">Footage</div>
        <span className="font-mono text-[10px] text-muted-foreground">
          {done}/{c.videos.length} done
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {c.videos.length === 0 && (
          <p className="px-4 pb-4 text-xs text-muted-foreground">
            Open a footage folder or SD card to start reviewing.
          </p>
        )}
        {c.videos.map((v, i) => {
          const ann = c.annotationsByPath[v.path];
          return (
            <button
              key={v.path}
              type="button"
              onClick={() => c.loadVideo(i)}
              className={cn(
                "flex w-full items-center justify-between gap-2 border-t border-border px-4 py-2 text-left text-xs transition-colors hover:bg-surface-2",
                i === c.index && "bg-surface-2 text-foreground",
              )}
            >
              <span className="min-w-0 truncate">{v.name}</span>
              <span className="flex shrink-0 items-center gap-1.5">
                {v.duration_label && (
                  <span className="font-mono text-[10px] text-muted-foreground">{v.duration_label}</span>
                )}
                {ann?.complete ? (
                  <Badge tone="ok">done</Badge>
                ) : ann?.segments?.length ? (
                  <Badge tone="warn">wip</Badge>
                ) : null}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

