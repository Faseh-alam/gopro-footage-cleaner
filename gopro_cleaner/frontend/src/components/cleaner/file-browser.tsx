import { Folder, FileVideo, File as FileIcon, CornerLeftUp } from "lucide-react";
import { cn } from "@/lib/utils";
import { formatBytes } from "@/lib/api";
import { EmptyState } from "@/components/wc/panel";

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir?: boolean;
  is_video?: boolean;
  is_gopro?: boolean;
  size_bytes?: number;
}

export function Breadcrumb({ path, onNavigate }: { path: string; onNavigate: (p: string) => void }) {
  if (!path) return null;
  const parts = path.split("/").filter(Boolean);
  let running = "";
  return (
    <div className="flex flex-wrap items-center gap-1 pb-3 font-mono text-[11px]">
      {parts.map((part, index) => {
        running += `/${part}`;
        const target = running;
        return (
          <span key={target} className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onNavigate(target)}
              className="rounded-[3px] px-1 py-0.5 text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground"
            >
              {part}
            </button>
            {index < parts.length - 1 && <span className="text-muted-foreground/50">/</span>}
          </span>
        );
      })}
    </div>
  );
}

export function FileList({
  entries,
  parent,
  selectedPath,
  onOpenFolder,
  onSelectVideo,
}: {
  entries: BrowseEntry[];
  parent?: string | null;
  selectedPath: string | null;
  onOpenFolder: (p: string) => void;
  onSelectVideo: (p: string) => void;
}) {
  return (
    <div className="max-h-[26rem] min-h-0 overflow-y-auto">
      {parent && (
        <Row
          icon={<CornerLeftUp className="size-3.5" />}
          name=".."
          meta="Up"
          onClick={() => onOpenFolder(parent)}
        />
      )}
      {entries.length === 0 && !parent && <EmptyState>Nothing here yet.</EmptyState>}
      {entries.map((entry) => {
        if (entry.is_dir) {
          return (
            <Row
              key={entry.path}
              icon={<Folder className="size-3.5" />}
              name={entry.name}
              meta="Folder"
              onClick={() => onOpenFolder(entry.path)}
            />
          );
        }
        if (entry.is_video) {
          return (
            <Row
              key={entry.path}
              icon={<FileVideo className={cn("size-3.5", entry.is_gopro && "text-accent")} />}
              name={entry.name}
              meta={formatBytes(entry.size_bytes)}
              selected={selectedPath === entry.path}
              onClick={() => onSelectVideo(entry.path)}
            />
          );
        }
        return (
          <Row
            key={entry.path}
            icon={<FileIcon className="size-3.5" />}
            name={entry.name}
            meta={formatBytes(entry.size_bytes)}
            disabled
          />
        );
      })}
    </div>
  );
}

function Row({
  icon,
  name,
  meta,
  onClick,
  selected,
  disabled,
}: {
  icon: React.ReactNode;
  name: string;
  meta?: string;
  onClick?: () => void;
  selected?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 border-b border-border/60 px-2 py-2 text-left transition-colors",
        disabled ? "opacity-40" : "hover:bg-surface-2",
        selected && "bg-surface-2",
      )}
    >
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-xs">{name}</span>
      <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        {meta}
      </span>
    </button>
  );
}
