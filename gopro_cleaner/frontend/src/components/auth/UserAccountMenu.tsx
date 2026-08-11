import { useState } from "react";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { formatClock, useAuth } from "@/components/auth/AuthProvider";

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0] || ""}${parts[parts.length - 1][0] || ""}`.toUpperCase();
}

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 px-1 py-1">
      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
        {label}
      </span>
      <span className="font-mono text-[11px] text-foreground">{value}</span>
    </div>
  );
}

export function UserAccountMenu() {
  const { user, today, logout } = useAuth();
  const [loggingOut, setLoggingOut] = useState(false);

  if (!user) return null;

  const displayName = user.full_name || user.email;
  const cards = today?.metrics?.sd_cards_connected ?? 0;
  const footage = today?.footage_label || "0h 0m";
  const start = formatClock(today?.metrics?.start_time);
  const end = today?.metrics?.end_time ? formatClock(today.metrics.end_time) : "—";

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          size="icon"
          variant="outline"
          className="size-8 shrink-0 rounded-full"
          title={displayName}
          aria-label="Account menu"
        >
          <span className="font-mono text-[11px] font-medium tracking-wide">
            {initials(displayName)}
          </span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64 rounded-sm p-2">
        <div className="px-1 pb-2 pt-1">
          <div className="truncate text-sm font-medium text-foreground">{displayName}</div>
          {user.full_name ? (
            <div className="truncate font-mono text-[10px] text-muted-foreground">{user.email}</div>
          ) : null}
        </div>
        <DropdownMenuSeparator />
        <div className="px-1 py-1.5">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
            Today
          </div>
          <MetricRow label="Start" value={start} />
          <MetricRow label="End" value={end} />
          <MetricRow label="SD cards" value={String(cards)} />
          <MetricRow label="Footage" value={footage} />
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={loggingOut}
          className="cursor-pointer gap-2 rounded-sm"
          onSelect={async (event) => {
            event.preventDefault();
            setLoggingOut(true);
            try {
              await logout();
            } finally {
              setLoggingOut(false);
            }
          }}
        >
          <LogOut className="size-3.5" />
          {loggingOut ? "Logging out…" : "Log out"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
