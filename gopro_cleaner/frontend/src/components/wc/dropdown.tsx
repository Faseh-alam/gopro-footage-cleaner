import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface DropdownProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  className?: string;
  size?: "sm" | "md";
  id?: string;
  disabled?: boolean;
}

/** Custom dropdown — no native <select>, keeps styling consistent everywhere. */
export function Dropdown({
  value,
  onChange,
  options,
  placeholder = "Select…",
  className,
  size = "md",
  id,
  disabled,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const commit = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  return (
    <div ref={ref} className={cn("relative", className)}>
      <button
        id={id}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown" || e.key === "Enter") {
            if (!open) {
              e.preventDefault();
              setOpen(true);
              return;
            }
          }
          if (!open) return;
          if (e.key === "Escape") setOpen(false);
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((i) => Math.min(options.length - 1, i + 1));
          }
          if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => Math.max(0, i - 1));
          }
          if (e.key === "Enter") {
            e.preventDefault();
            const opt = options[active];
            if (opt && !opt.disabled) commit(opt.value);
          }
        }}
        className={cn(
          "flex w-full items-center justify-between gap-2 rounded-sm border border-border-strong bg-transparent text-left text-foreground transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40",
          size === "sm" ? "h-7 px-2 text-xs" : "h-9 px-3 text-sm",
        )}
      >
        <span className={cn("truncate", !selected && "text-muted-foreground")}>
          {selected ? selected.label : placeholder}
        </span>
        <ChevronDown
          className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
        />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute z-50 mt-1 max-h-64 w-full overflow-auto rounded-sm border border-border-strong bg-popover p-1 shadow-2xl"
        >
          {options.length === 0 && (
            <div className="px-2 py-1.5 text-xs text-muted-foreground">No options</div>
          )}
          {options.map((opt, i) => (
            <button
              key={opt.value + i}
              type="button"
              role="option"
              aria-selected={opt.value === value}
              disabled={opt.disabled}
              onMouseEnter={() => setActive(i)}
              onClick={() => commit(opt.value)}
              className={cn(
                "block w-full truncate rounded-[3px] px-2 py-1.5 text-left text-xs transition-colors",
                opt.value === value ? "text-foreground" : "text-muted-foreground",
                i === active && "bg-surface-2 text-foreground",
                opt.disabled && "opacity-40",
              )}
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
