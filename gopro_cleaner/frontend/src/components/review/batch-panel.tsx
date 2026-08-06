import { useState } from "react";
import { Button } from "@/components/ui/button";
import { TextArea } from "@/components/wc/field";
import { Badge } from "@/components/wc/panel";
import type { ReviewController } from "./useReviewController";

export function BatchPanel({ c }: { c: ReviewController }) {
  const [csv, setCsv] = useState("");
  const [open, setOpen] = useState(false);
  const cards: any[] = c.batchDetail?.cards || [];

  return (
    <section className="panel-surface">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span className="eyebrow">Batch</span>
        <span className="truncate text-xs text-muted-foreground">
          {c.batchDetail
            ? `${c.batchDetail.batch_name || c.batchId} · ${cards.length} cards`
            : "No active batch — paste CSV and start."}
        </span>
      </button>

      {open && (
        <div className="grid gap-4 border-t border-border p-4 md:grid-cols-2">
          <div className="grid gap-2">
            <p className="text-[11px] text-muted-foreground">
              CSV columns: <code className="font-mono">batch_name,factory,card_badge,device_type,device_id</code>
            </p>
            <TextArea
              rows={4}
              value={csv}
              onChange={(e) => setCsv(e.currentTarget.value)}
              placeholder={"batch_name,factory,card_badge,device_type,device_id\nbatch-1,Factory A,C1234,gopro,GP-01"}
            />
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="accent" onClick={() => c.importBatchCsv(csv)}>
                Start batch from CSV
              </Button>
              <Button size="sm" variant="outline" onClick={() => c.refreshBatch()}>
                Refresh batch
              </Button>
              <Button size="sm" variant="outline" onClick={() => c.finishCard()}>
                Finish card
              </Button>
              <Button size="sm" variant="accent" onClick={() => c.completeBatch()}>
                Batch complete
              </Button>
            </div>
          </div>

          <div className="grid content-start gap-1.5">
            {cards.length === 0 && <p className="text-xs text-muted-foreground">No cards in this batch yet.</p>}
            {cards.map((card: any) => (
              <div
                key={card.card_badge}
                className="flex items-center justify-between gap-2 rounded-sm border border-border px-3 py-2 text-xs"
              >
                <span className="truncate">
                  {card.card_badge} · <span className="text-muted-foreground">{card.device_type || "—"}</span>
                </span>
                <Badge tone={card.finished ? "ok" : "muted"}>{card.finished ? "done" : "open"}</Badge>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
