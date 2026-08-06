import { cn } from "@/lib/utils";

export function Logo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 613 600"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      className={cn("size-6 shrink-0 text-foreground", className)}
    >
      <path
        d="M407.513 600L613 401.409V209.618L406.646 407.351H6.06946L206.361 600H407.513ZM306.934 398.867C332.842 398.867 358.265 389.363 378.033 370.013C417.568 331.321 417.568 268.679 378.033 229.987C338.497 191.288 274.503 191.288 234.968 229.987C195.432 268.679 195.432 331.321 234.968 370.013C254.735 389.363 281.025 398.867 306.934 398.867ZM0 391.227L206.361 193.494H607.804L407.513 0H206.361L0 198.591V391.227Z"
        fill="currentColor"
      />
    </svg>
  );
}
