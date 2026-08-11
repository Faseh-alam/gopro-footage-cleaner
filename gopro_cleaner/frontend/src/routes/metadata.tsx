import { Logo } from "@/components/wc/logo";
import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowUpRight,
  CornerLeftUp,
  FileVideo,
  Folder,
  Loader2,
  RefreshCw,
  HardDrive,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Dropdown, type SelectOption } from "@/components/wc/dropdown";
import { Badge, EmptyState, Panel } from "@/components/wc/panel";
import { api, formatBytes } from "@/lib/api";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/metadata")({
  head: () => ({
    meta: [
      { title: "Metadata Inspector — World Context" },
      {
        name: "description",
        content:
          "Browse drives and inspect GoPro camera, IMU, and labeling metadata for every MP4.",
      },
    ],
  }),
  component: MetadataPage,
});

interface Drive {
  name: string;
  path: string;
  kind: string;
  label?: string;
  is_sd_card?: boolean;
  free_bytes?: number | null;
  total_bytes?: number | null;
}

interface BrowseEntry {
  name: string;
  path: string;
  is_dir?: boolean;
  is_video?: boolean;
  is_gopro?: boolean;
  size_bytes?: number;
  has_sidecar?: boolean;
  has_embedded?: boolean;
  has_labels?: boolean;
}

interface ScanRow {
  path: string;
  imu_detected?: boolean;
  has_gpmf?: boolean;
  sensors?: string[];
  has_labels?: boolean;
  camera_serial?: string | null;
  camera_model?: string | null;
}

interface InspectPayload {
  path: string;
  name: string;
  size_bytes?: number | null;
  imu_detected: boolean;
  has_gpmf: boolean;
  sensors: string[];
  media_meta: Record<string, any>;
  labeling_source: "sidecar" | "embedded" | "none";
  has_sidecar: boolean;
  has_embedded: boolean;
  labeling: Record<string, any> | null;
}

function MetadataPage() {
  const [drives, setDrives] = useState<Drive[]>([]);
  const [drivePath, setDrivePath] = useState("");
  const [currentPath, setCurrentPath] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [detail, setDetail] = useState<InspectPayload | null>(null);
  const [scanMap, setScanMap] = useState<Record<string, ScanRow>>({});
  const [browsing, setBrowsing] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [status, setStatus] = useState("Pick a drive to browse");

  const driveOptions: SelectOption[] = useMemo(
    () =>
      drives.map((d) => ({
        value: d.path,
        label:
          d.free_bytes != null && d.total_bytes
            ? `${d.name} · ${formatBytes(d.free_bytes)} free`
            : d.name,
      })),
    [drives],
  );

  const loadDrives = useCallback(async () => {
    try {
      const data = await api<{ drives: Drive[] }>("/api/meta/drives");
      setDrives(data.drives || []);
      setStatus(`${data.drives?.length ?? 0} drives / cards available`);
    } catch (error: any) {
      toast.error(error.message || "Could not list drives");
    }
  }, []);

  const browse = useCallback(async (path: string) => {
    if (!path) return;
    try {
      setBrowsing(true);
      setStatus(`Opening ${path}`);
      const data = await api<{ path: string; parent?: string | null; entries: BrowseEntry[] }>(
        `/api/browse?path=${encodeURIComponent(path)}`,
      );
      setCurrentPath(data.path);
      setParent(data.parent ?? null);
      setEntries(data.entries || []);
      setSelectedPath(null);
      setDetail(null);
      setScanMap({});
      const videos = (data.entries || []).filter((e) => e.is_video).length;
      setStatus(`${data.entries?.length ?? 0} items · ${videos} video(s)`);
    } catch (error: any) {
      setStatus(error.message);
      toast.error(error.message);
    } finally {
      setBrowsing(false);
    }
  }, []);

  const inspect = useCallback(async (path: string) => {
    try {
      setInspecting(true);
      setSelectedPath(path);
      const data = await api<InspectPayload>(`/api/meta/inspect?path=${encodeURIComponent(path)}`);
      setDetail(data);
      setScanMap((prev) => ({
        ...prev,
        [path]: {
          path,
          imu_detected: data.imu_detected,
          has_gpmf: data.has_gpmf,
          sensors: data.sensors,
          has_labels: data.has_sidecar || data.has_embedded,
          camera_serial: data.media_meta?.camera_serial,
          camera_model: data.media_meta?.camera_model,
        },
      }));
      setStatus(
        data.imu_detected
          ? `IMU detected · ${data.sensors?.length || 0} sensor stream(s)`
          : "No IMU / GPMF track detected",
      );
    } catch (error: any) {
      setDetail(null);
      toast.error(error.message || "Inspect failed");
    } finally {
      setInspecting(false);
    }
  }, []);

  const scanFolder = useCallback(async () => {
    if (!currentPath) return;
    try {
      setScanning(true);
      setStatus("Scanning IMU on videos in this folder…");
      const data = await api<{ videos: ScanRow[] }>(
        `/api/meta/scan?path=${encodeURIComponent(currentPath)}`,
      );
      const next: Record<string, ScanRow> = {};
      for (const row of data.videos || []) next[row.path] = row;
      setScanMap(next);
      const imuCount = (data.videos || []).filter((v) => v.imu_detected).length;
      setStatus(`Scan done · ${imuCount}/${data.videos?.length ?? 0} with IMU`);
    } catch (error: any) {
      toast.error(error.message || "Scan failed");
    } finally {
      setScanning(false);
    }
  }, [currentPath]);

  useEffect(() => {
    loadDrives();
  }, [loadDrives]);

  useEffect(() => {
    if (drivePath) browse(drivePath);
  }, [drivePath, browse]);

  return (
    <div className="min-h-screen bg-background">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border px-6 py-5">
        <div className="flex items-center gap-3">
          <Logo className="size-7" />
          <div>
            <div className="eyebrow">World Context</div>
            <h1 className="mt-1 text-lg font-bold tracking-tight">Metadata Inspector</h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Dropdown
            className="w-72"
            size="sm"
            value={drivePath}
            onChange={setDrivePath}
            options={driveOptions}
            placeholder="Select SSD / SD card / drive…"
          />
          <Button size="sm" variant="ghost" onClick={loadDrives} title="Refresh drives">
            <HardDrive className="size-3.5" /> Drives
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={!currentPath || browsing}
            onClick={() => browse(currentPath)}
          >
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
          <Link
            to="/review"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-[#b96d72] transition-opacity hover:opacity-75"
          >
            Review <ArrowUpRight className="size-3.5" />
          </Link>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 font-mono text-[11px] uppercase tracking-[0.14em] text-[#b96d72] transition-opacity hover:opacity-75"
          >
            Cleaner <ArrowUpRight className="size-3.5" />
          </Link>
        </div>
      </header>

      <p className="border-b border-border px-6 py-2 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {status}
      </p>

      <main className="grid gap-4 p-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <Panel
          eyebrow="01 / Browser"
          title="Files"
          actions={
            <Button size="sm" variant="outline" disabled={!currentPath || scanning} onClick={scanFolder}>
              {scanning ? <Loader2 className="size-3.5 animate-spin" /> : null}
              Scan IMU
            </Button>
          }
          bodyClassName="flex min-h-[28rem] flex-col gap-2 p-4"
        >
          <PathBreadcrumb path={currentPath} onNavigate={browse} />
          {browsing && (
            <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" /> Loading folder…
            </div>
          )}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {parent && (
              <FileRow
                icon={<CornerLeftUp className="size-3.5" />}
                name=".."
                meta="Up"
                onClick={() => browse(parent)}
              />
            )}
            {!entries.length && !parent && !browsing && (
              <EmptyState>Select a drive above to browse MP4 files.</EmptyState>
            )}
            {entries.map((entry) => {
              if (entry.is_dir) {
                return (
                  <FileRow
                    key={entry.path}
                    icon={<Folder className="size-3.5" />}
                    name={entry.name}
                    meta="Folder"
                    onClick={() => browse(entry.path)}
                  />
                );
              }
              if (entry.is_video) {
                const scan = scanMap[entry.path];
                const imu =
                  scan?.imu_detected === true
                    ? "IMU"
                    : scan?.imu_detected === false
                      ? "no IMU"
                      : null;
                const labels = entry.has_labels || scan?.has_labels;
                return (
                  <FileRow
                    key={entry.path}
                    icon={
                      <FileVideo
                        className={cn("size-3.5", entry.is_gopro && "text-accent")}
                      />
                    }
                    name={entry.name}
                    meta={formatBytes(entry.size_bytes)}
                    badges={[
                      labels ? { text: "Labels", tone: "ok" as const } : null,
                      imu === "IMU"
                        ? { text: "IMU", tone: "accent" as const }
                        : imu === "no IMU"
                          ? { text: "No IMU", tone: "muted" as const }
                          : null,
                    ].filter(Boolean) as { text: string; tone: "ok" | "accent" | "muted" }[]}
                    selected={selectedPath === entry.path}
                    onClick={() => inspect(entry.path)}
                  />
                );
              }
              return null;
            })}
          </div>
        </Panel>

        <Panel
          eyebrow="02 / Detail"
          title={detail?.name || "Video metadata"}
          actions={
            detail ? (
              <div className="flex items-center gap-2">
                <Badge tone={detail.imu_detected ? "ok" : "danger"}>
                  {detail.imu_detected ? "IMU detected" : "IMU not detected"}
                </Badge>
                <Badge tone={detail.labeling_source === "none" ? "muted" : "accent"}>
                  {detail.labeling_source === "none"
                    ? "No labels"
                    : `Labels · ${detail.labeling_source}`}
                </Badge>
              </div>
            ) : null
          }
          bodyClassName="min-h-[28rem] overflow-y-auto p-4"
        >
          {inspecting && !detail ? (
            <div className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" /> Reading metadata…
            </div>
          ) : !detail ? (
            <EmptyState>
              Select an MP4 from the browser. Camera, IMU, and labeling metadata will appear here.
            </EmptyState>
          ) : (
            <DetailView detail={detail} />
          )}
        </Panel>
      </main>
    </div>
  );
}

function PathBreadcrumb({ path, onNavigate }: { path: string; onNavigate: (p: string) => void }) {
  if (!path) return null;
  const crumbs: { label: string; target: string }[] = [];

  if (path.includes("\\") || /^[A-Za-z]:/.test(path)) {
    // Windows paths from the Flask API.
    const cleaned = path.replace(/\//g, "\\");
    const driveMatch = cleaned.match(/^([A-Za-z]:)(.*)$/);
    if (driveMatch) {
      const drive = driveMatch[1];
      let running = `${drive}\\`;
      crumbs.push({ label: drive, target: running });
      for (const part of driveMatch[2].split("\\").filter(Boolean)) {
        running = running.endsWith("\\") ? `${running}${part}` : `${running}\\${part}`;
        crumbs.push({ label: part, target: running });
      }
    }
  } else {
    let running = "";
    for (const part of path.split("/").filter(Boolean)) {
      running += `/${part}`;
      crumbs.push({ label: part, target: running });
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1 pb-1 font-mono text-[11px]">
      {crumbs.map((c, i) => (
        <span key={`${c.target}-${i}`} className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => onNavigate(c.target)}
            className="rounded-[3px] px-1 py-0.5 text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground"
          >
            {c.label}
          </button>
          {i < crumbs.length - 1 && <span className="text-muted-foreground/50">/</span>}
        </span>
      ))}
    </div>
  );
}

function FileRow({
  icon,
  name,
  meta,
  badges,
  onClick,
  selected,
}: {
  icon: React.ReactNode;
  name: string;
  meta?: string;
  badges?: { text: string; tone: "ok" | "accent" | "muted" | "danger" | "warn" }[];
  onClick?: () => void;
  selected?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 border-b border-border/60 px-2 py-2 text-left transition-colors hover:bg-surface-2",
        selected && "bg-surface-2",
      )}
    >
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="min-w-0 flex-1 truncate font-mono text-xs">{name}</span>
      <span className="flex shrink-0 items-center gap-1">
        {badges?.map((b) => (
          <Badge key={b.text} tone={b.tone}>
            {b.text}
          </Badge>
        ))}
      </span>
      <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
        {meta}
      </span>
    </button>
  );
}

function DetailView({ detail }: { detail: InspectPayload }) {
  const meta = detail.media_meta || {};
  const labeling = detail.labeling || {};
  const segments = Array.isArray(labeling.segments) ? labeling.segments : [];
  const labelingMeta = (labeling.media_meta as Record<string, any>) || {};

  const cameraRows: [string, string][] = [
    ["File", detail.name],
    ["Path", detail.path],
    ["Size", detail.size_bytes != null ? formatBytes(detail.size_bytes) : "—"],
    ["Recorded", String(meta.recorded_at || labelingMeta.recorded_at || "—")],
    ["Camera model", String(meta.camera_model || labelingMeta.camera_model || "—")],
    ["Camera serial", String(meta.camera_serial || labelingMeta.camera_serial || "—")],
    ["Firmware", String(meta.firmware || labelingMeta.firmware || "—")],
    ["Media UID", String(meta.media_uid || labelingMeta.media_uid || "—")],
    ["Lens serial", String(meta.lens_serial || labelingMeta.lens_serial || "—")],
    [
      "Video",
      meta.width && meta.height
        ? `${meta.width}×${meta.height}${meta.fps ? ` · ${meta.fps} fps` : ""}${
            meta.video_codec ? ` · ${meta.video_codec}` : ""
          }`
        : "—",
    ],
    ["GPMF track", detail.has_gpmf ? "Yes" : "No"],
    ["IMU detected", detail.imu_detected ? "Yes" : "No"],
    [
      "IMU sensors",
      detail.sensors?.length ? detail.sensors.join(", ") : "None detected",
    ],
  ];

  const labelRows: [string, string][] = [
    ["Label source", detail.labeling_source],
    ["Sidecar .segments.json", detail.has_sidecar ? "Yes" : "No"],
    ["Embedded in MP4", detail.has_embedded ? "Yes" : "No"],
    ["Complete", labeling.complete === true ? "Yes" : labeling.complete === false ? "No" : "—"],
    ["Batch", String(labeling.batch_name || "—")],
    ["Factory", String(labeling.factory || "—")],
    ["Card badge", String(labeling.card_badge || "—")],
    ["Device", `${labeling.device_type || "—"} / ${labeling.device_id || "—"}`],
    ["Duration", labeling.duration != null ? `${Number(labeling.duration).toFixed(3)} s` : "—"],
    ["Segments", String(segments.length)],
    ["Updated", String(labeling.updated_at || "—")],
  ];

  return (
    <div className="grid gap-5">
      <MetaTable title="Camera & IMU" rows={cameraRows} />
      <MetaTable title="Labeling (from segments JSON)" rows={labelRows} />

      <div>
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
          Segments
        </div>
        {!segments.length ? (
          <EmptyState>No work/garbage segments saved for this file.</EmptyState>
        ) : (
          <div className="overflow-x-auto rounded-sm border border-border">
            <table className="w-full min-w-[32rem] border-collapse text-left font-mono text-[11px]">
              <thead className="bg-surface-2/80 text-[10px] uppercase tracking-[0.12em] text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">#</th>
                  <th className="px-3 py-2 font-medium">Kind</th>
                  <th className="px-3 py-2 font-medium">Task</th>
                  <th className="px-3 py-2 font-medium">Start</th>
                  <th className="px-3 py-2 font-medium">End</th>
                  <th className="px-3 py-2 font-medium">Duration</th>
                </tr>
              </thead>
              <tbody>
                {segments.map((seg: any, i: number) => {
                  const start = Number(seg.start || 0);
                  const end = Number(seg.end || 0);
                  return (
                    <tr key={seg.id || i} className="border-t border-border/70">
                      <td className="px-3 py-1.5 text-muted-foreground">{i + 1}</td>
                      <td className="px-3 py-1.5">
                        <Badge tone={seg.kind === "work" ? "ok" : "warn"}>
                          {seg.kind || "?"}
                        </Badge>
                      </td>
                      <td className="px-3 py-1.5">{seg.task || "—"}</td>
                      <td className="px-3 py-1.5">{start.toFixed(3)}</td>
                      <td className="px-3 py-1.5">{end.toFixed(3)}</td>
                      <td className="px-3 py-1.5">{Math.max(0, end - start).toFixed(3)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function MetaTable({ title, rows }: { title: string; rows: [string, string][] }) {
  return (
    <div>
      <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {title}
      </div>
      <div className="overflow-hidden rounded-sm border border-border">
        <table className="w-full border-collapse text-left text-xs">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k} className="border-b border-border/70 last:border-0">
                <th className="w-[11rem] bg-surface-2/50 px-3 py-2 font-mono text-[10px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                  {k}
                </th>
                <td className="break-all px-3 py-2 font-mono text-[11px] text-foreground/90">
                  {v}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
