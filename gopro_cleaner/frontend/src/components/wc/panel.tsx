import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

export function Panel({
  title,
  eyebrow,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: string;
  eyebrow?: string;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn("panel-surface flex min-h-0 flex-col", className)}>
      {(title || actions || eyebrow) && (
        <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-0.5">{eyebrow}</div>}
            {title && <h2 className="truncate text-sm font-semibold">{title}</h2>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn("min-h-0 flex-1 p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

export function Badge({
  children,
  tone = "muted",
  className,
}: {
  children: ReactNode;
  tone?: "muted" | "ok" | "warn" | "danger" | "accent";
  className?: string;
}) {
  const tones = {
    muted: "border-border text-muted-foreground",
    ok: "border-success/40 text-success",
    warn: "border-warning/40 text-warning",
    danger: "border-destructive/40 text-destructive",
    accent: "border-accent/40 text-accent",
  } as const;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function ProgressBar({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cn("h-[3px] w-full overflow-hidden rounded-full bg-surface-2", className)}>
      <div
        className="h-full bg-accent transition-[width] duration-300"
        style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
      />
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="grid place-items-center rounded-sm border border-dashed border-border px-4 py-10 text-center text-xs text-muted-foreground">
      {children}
    </div>
  );
}
