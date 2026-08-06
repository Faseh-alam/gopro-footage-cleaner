import { Logo } from "@/components/wc/logo";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dropdown, type SelectOption } from "@/components/wc/dropdown";
import { Checkbox, TextArea } from "@/components/wc/field";
import { Badge, EmptyState, Panel } from "@/components/wc/panel";
import { Breadcrumb, FileList, type BrowseEntry } from "@/components/cleaner/file-browser";
import { QueueList, type QueueBatch } from "@/components/cleaner/queue-list";
import { SheetImport } from "@/components/cleaner/sheet-import";
import { api, formatBytes } from "@/lib/api";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Footage Cleaner — World Context" },
      {
        name: "description",
        content:
          "Browse drives, mark useful clips per GoPro video and queue background trims that preserve IMU/GPMF data.",
      },
      { property: "og:title", content: "Footage Cleaner — World Context" },
      {
        property: "og:description",
        content:
          "Browse drives, mark useful clips per GoPro video and queue background trims that preserve IMU/GPMF data.",
      },
    ],
  }),
  component: Index,
});

interface ProbeInfo {
  name: string;
  duration_label?: string;
  size_bytes?: number;
  has_gpmf?: boolean;
}

const EXAMPLE_LINE = "00:00 - 01:30";

function Index() {
  const [volumes, setVolumes] = useState<{ name: string; path: string }[]>([]);
  const [currentPath, setCurrentPath] = useState<string>("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedInfo, setSelectedInfo] = useState<ProbeInfo | null>(null);
  const [clips, setClips] = useState("");
  const [deleteOriginal, setDeleteOriginal] = useState(true);
  const [queueing, setQueueing] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [probing, setProbing] = useState(false);
  const [batches, setBatches] = useState<QueueBatch[]>([]);
  const [queueSummary, setQueueSummary] = useState("No jobs queued");
  const [status, setStatus] = useState("Ready");
  const [version, setVersion] = useState("…");
  const [drive, setDrive] = useState("");
  const [date, setDate] = useState("");
  const [dateOptions, setDateOptions] = useState<SelectOption[]>([]);

  const clipsRef = useRef<HTMLTextAreaElement>(null);
  const currentPathRef = useRef("");
  const trackedBatches = useRef(new Map<string, string>());

  currentPathRef.current = currentPath;

  const browse = useCallback(async (path: string) => {
    try {
      setBrowsing(true);
      setStatus(`Opening ${path}`);
      const data = await api<{ path: string; parent?: string | null; entries: BrowseEntry[] }>(
        `/api/browse?path=${encodeURIComponent(path)}`,
      );
      setCurrentPath(data.path);
      setParent(data.parent ?? null);
      setEntries(data.entries || []);
      setStatus(`Showing ${data.entries?.length ?? 0} items`);
    } catch (error: any) {
      setStatus(error.message);
      toast.error(error.message);
    } finally {
      setBrowsing(false);
    }
  }, []);

  const refreshQueue = useCallback(async () => {
    try {
      const data = await api<{ batches?: QueueBatch[]; summary?: any }>("/api/queue");
      const list = data.batches || [];
      setBatches(list);

      if (data.summary?.total) {
        const active = (data.summary.running || 0) + (data.summary.queued || 0);
        const done = (data.summary.completed || 0) + (data.summary.failed || 0);
        setQueueSummary(`${active} active · ${done} done · ${data.summary.total} total`);
      } else if (list.length) {
        const active = list.filter((b) => ["queued", "running"].includes(b.status)).length;
        setQueueSummary(`${active} active · ${list.length} total`);
      } else {
        setQueueSummary("No jobs queued");
      }

      const folder = currentPathRef.current;
      const shouldRefreshFolder = list.some((batch) => {
        if (!["completed", "failed"].includes(batch.status)) return false;
        if (!trackedBatches.current.has(batch.batch_id)) return false;
        return trackedBatches.current.get(batch.batch_id) !== batch.status && folder;
      });
      for (const batch of list) trackedBatches.current.set(batch.batch_id, batch.status);
      if (shouldRefreshFolder && folder) await browse(folder);
    } catch (error: any) {
      setStatus(error.message);
    }
  }, [browse]);

  useEffect(() => {
    api<{ volumes: { name: string; path: string }[] }>("/api/volumes")
      .then((data) => setVolumes(data.volumes || []))
      .then(() => browse("/Volumes"))
      .catch((error) => {
        setStatus(error.message);
        toast.error(error.message);
      });

    api<{ version?: string }>("/api/health")
      .then((data) => setVersion(data.version || "?"))
      .catch(() => setVersion("offline"));
  }, [browse]);

  useEffect(() => {
    refreshQueue();
    const timer = window.setInterval(refreshQueue, 1500);
    return () => window.clearInterval(timer);
  }, [refreshQueue]);

  useEffect(() => {
    setDate("");
    setDateOptions([]);
    if (!drive) return;
    let cancelled = false;
    api<{ entries: BrowseEntry[] }>(
      `/api/browse?path=${encodeURIComponent(`/Volumes/${drive}/archive/YT`)}`,
    )
      .then((data) => {
        if (cancelled) return;
        setDateOptions(
          (data.entries || [])
            .filter((entry) => entry.is_dir)
            .map((entry) => ({ value: entry.name, label: entry.name })),
        );
      })
      .catch(() => {
        /* drive may be offline */
      });
    return () => {
      cancelled = true;
    };
  }, [drive]);

  async function selectVideo(path: string) {
    try {
      setProbing(true);
      setStatus("Reading video metadata…");
      const info = await api<ProbeInfo>(`/api/probe?path=${encodeURIComponent(path)}`);
      setSelectedPath(path);
      setSelectedInfo(info);
      setStatus(`Selected ${info.name}`);
    } catch (error: any) {
      setStatus(error.message);
      toast.error(error.message);
    } finally {
      setProbing(false);
    }
  }

  function clearSelection() {
    setSelectedPath(null);
    setSelectedInfo(null);
  }

  async function queueClips() {
    if (!selectedPath) return;
    if (!clips.trim()) {
      toast.error("Paste at least one clip line, e.g. 00:00 - 7:45");
      return;
    }
    setQueueing(true);
    setStatus("Queueing clips...");
    try {
      const data = await api<{ batch_id: string; status: string; clip_count: number; input_name: string }>(
        "/api/batch",
        {
          method: "POST",
          body: JSON.stringify({ path: selectedPath, clips, delete_original: deleteOriginal }),
        },
      );
      trackedBatches.current.set(data.batch_id, data.status);
      setClips("");
      clearSelection();
      refreshQueue();
      setStatus(`Queued ${data.clip_count} clips for ${data.input_name}`);
      toast.success(`Queued ${data.clip_count} clips for ${data.input_name}`);
    } catch (error: any) {
      toast.error(error.message);
      setStatus(error.message);
    } finally {
      setQueueing(false);
    }
  }

  const volumeOptions: SelectOption[] = volumes.map((v) => ({ value: v.path, label: v.name }));
  const driveOptions: SelectOption[] = volumes
    .filter((v) => v.path.startsWith("/Volumes/"))
    .map((v) => ({ value: v.name, label: v.name }));

  return (
    <div className="min-h-screen bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border px-6 py-5">
        <div className="flex items-center gap-3">
          <Logo className="size-7" />
          <div>
            <div className="eyebrow">World Context</div>
            <h1 className="mt-1 text-lg font-bold tracking-tight">GoPro Footage Cleaner</h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Dropdown
            className="w-56"
            size="sm"
            value=""
            onChange={(path) => path && browse(path)}
            options={volumeOptions}
            placeholder="Quick open a drive…"
          />
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (currentPath) browse(currentPath);
              refreshQueue();
            }}
          >
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
          <Link
            to="/review"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-accent transition-opacity hover:opacity-75"
          >
            Review station <ArrowUpRight className="size-3.5" />
          </Link>
        </div>
      </header>

      <main className="grid gap-4 p-6">
        <div className="grid gap-4 lg:grid-cols-2">
          <Panel eyebrow="01 / Source" title="Footage browser" bodyClassName="p-4">
            <Breadcrumb path={currentPath} onNavigate={browse} />
            {browsing && (
              <div className="flex items-center gap-2 pb-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" /> Loading folder…
              </div>
            )}
            <FileList
              entries={entries}
              parent={parent}
              selectedPath={selectedPath}
              onOpenFolder={browse}
              onSelectVideo={selectVideo}
            />
          </Panel>

          <Panel
            eyebrow="02 / Clips"
            title="Selected video"
            actions={
              selectedInfo ? (
                <Button size="sm" variant="ghost" onClick={clearSelection}>
                  Clear
                </Button>
              ) : null
            }
            bodyClassName="grid gap-4 p-4"
          >
            {probing && !selectedInfo ? (
              <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                <Loader2 className="size-3.5 animate-spin" /> Probing video…
              </div>
            ) : !selectedInfo ? (
              <EmptyState>
                Select a GoPro video from the browser, paste all useful timestamps, then queue the batch.
              </EmptyState>
            ) : (
              <>
                <div className="grid gap-2 border-b border-border pb-4">
                  <div className="font-mono text-sm">{selectedInfo.name}</div>
                  <div className="text-xs text-muted-foreground">
                    {selectedInfo.duration_label || "Unknown duration"} ·{" "}
                    {formatBytes(selectedInfo.size_bytes)}
                  </div>
                  <div>
                    <Badge tone={selectedInfo.has_gpmf ? "ok" : "warn"}>
                      {selectedInfo.has_gpmf ? "IMU / GPMF detected" : "No GPMF metadata track"}
                    </Badge>
                  </div>
                </div>

                <p className="text-xs leading-relaxed text-muted-foreground">
                  One clip per line as <code className="font-mono text-foreground">start - end</code>. Clips
                  save beside the original as{" "}
                  <code className="font-mono text-foreground">filename-1.MP4</code>.
                </p>

                <div className="grid gap-1.5">
                  <label htmlFor="clips-input" className="eyebrow">
                    Timestamps
                  </label>
                  <TextArea
                    id="clips-input"
                    ref={clipsRef}
                    rows={8}
                    value={clips}
                    onChange={(e) => setClips(e.target.value)}
                    placeholder={"00:00 - 7:45\n10:00 - 12:00\n16:00 - 17:00"}
                  />
                </div>

                <Checkbox
                  label="Delete original raw footage after all clips export successfully"
                  checked={deleteOriginal}
                  onChange={(e) => setDeleteOriginal(e.target.checked)}
                />

                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" size="sm" loading={queueing} onClick={queueClips}>
                    Queue all clips
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => {
                      setClips((prev) => (prev.trim() ? `${prev.trim()}\n${EXAMPLE_LINE}` : EXAMPLE_LINE));
                      clipsRef.current?.focus();
                    }}
                  >
                    Add example line
                  </Button>
                </div>
              </>
            )}
          </Panel>
        </div>

        <SheetImport
          driveOptions={driveOptions}
          dateOptions={dateOptions}
          drive={drive}
          onDriveChange={setDrive}
          date={date}
          onDateChange={setDate}
          setStatus={setStatus}
          onQueued={refreshQueue}
        />

        <Panel
          eyebrow="04 / Workers"
          title="Background queue"
          actions={<span className="font-mono text-[11px] text-muted-foreground">{queueSummary}</span>}
        >
          <QueueList batches={batches} />
        </Panel>
      </main>

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-6 py-4 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        <span>Requires ffmpeg · IMU / GPMF preserved · App v{version}</span>
        <span className="max-w-[55%] truncate normal-case tracking-normal">{status}</span>
      </footer>
    </div>
  );
}
