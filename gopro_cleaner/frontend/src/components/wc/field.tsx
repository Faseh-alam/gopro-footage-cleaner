import { cn } from "@/lib/utils";
import type { InputHTMLAttributes, TextareaHTMLAttributes, ReactNode } from "react";

const fieldBase =
  "w-full rounded-sm border border-border-strong bg-transparent text-foreground placeholder:text-muted-foreground/70 transition-colors focus:outline-none focus:ring-2 focus:ring-ring disabled:opacity-40";

export function TextInput({
  className,
  ref,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { ref?: React.Ref<HTMLInputElement> }) {
  return <input ref={ref} className={cn(fieldBase, "h-9 px-3 text-sm", className)} {...props} />;
}

export function TextArea({
  className,
  ref,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  ref?: React.Ref<HTMLTextAreaElement>;
}) {
  return (
    <textarea
      ref={ref}
      className={cn(fieldBase, "resize-y px-3 py-2 font-mono text-xs leading-relaxed", className)}
      {...props}
    />
  );
}

export function FileInput({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      type="file"
      className={cn(
        fieldBase,
        "px-3 py-1.5 text-xs file:mr-3 file:rounded-sm file:border file:border-border-strong file:bg-transparent file:px-2 file:py-1 file:text-xs file:text-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function Checkbox({
  label,
  className,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label className={cn("flex cursor-pointer items-center gap-2 text-xs text-muted-foreground", className)}>
      <input
        type="checkbox"
        className="size-3.5 accent-[oklch(0.63_0.19_260)] rounded-none"
        {...props}
      />
      <span>{label}</span>
    </label>
  );
}

export function Field({
  label,
  children,
  className,
  htmlFor,
}: {
  label: string;
  children: ReactNode;
  className?: string;
  htmlFor?: string;
}) {
  return (
    <div className={cn("grid gap-1.5", className)}>
      <label htmlFor={htmlFor} className="eyebrow">
        {label}
      </label>
      {children}
    </div>
  );
}
