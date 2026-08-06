import { Badge, ProgressBar } from "@/components/wc/panel";

export interface QueueJob {
  output_name: string;
  start: string;
  end: string;
  status: string;
  error?: string;
}

export interface QueueBatch {
  batch_id: string;
  input_name: string;
  status: string;
  clip_count: number;
  completed: number;
  progress?: number;
  message?: string;
  jobs: QueueJob[];
}

const tone = (status: string) =>
  status === "completed" ? "ok" : status === "failed" ? "danger" : status === "running" ? "accent" : "muted";

export function QueueList({ batches }: { batches: QueueBatch[] }) {
  if (!batches.length) {
    return (
      <div className="rounded-sm border border-dashed border-border px-4 py-8 text-center text-xs text-muted-foreground">
        Queued batches will appear here.
      </div>
    );
  }

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {batches.map((batch) => (
        <article key={batch.batch_id} className="rounded-sm border border-border p-3">
          <div className="flex items-start justify-between gap-2">
            <h3 className="min-w-0 truncate font-mono text-xs">{batch.input_name}</h3>
            <Badge tone={tone(batch.status) as never}>{batch.status}</Badge>
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            {batch.completed}/{batch.clip_count} clips · {batch.message || batch.status}
          </p>
          <ProgressBar value={batch.progress || 0} className="mt-2.5" />
          <ul className="mt-2.5 grid gap-1">
            {batch.jobs.map((job, i) => (
              <li key={i} className="truncate font-mono text-[10px] text-muted-foreground">
                {job.output_name}: {job.start} → {job.end} · {job.status}
                {job.error ? ` (${job.error.split("\n")[0]})` : ""}
              </li>
            ))}
          </ul>
        </article>
      ))}
    </div>
  );
}
