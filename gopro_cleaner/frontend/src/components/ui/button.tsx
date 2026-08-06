import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-sm border text-sm font-medium transition-colors select-none disabled:pointer-events-none disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground border-transparent hover:bg-primary/85",
        primary: "bg-primary text-primary-foreground border-transparent hover:bg-primary/85",
        accent: "bg-accent text-accent-foreground border-transparent hover:bg-accent/85",
        secondary: "bg-transparent text-foreground border-border-strong hover:bg-surface-2",
        outline: "bg-transparent text-foreground border-border-strong hover:bg-surface-2",
        ghost:
          "bg-transparent text-muted-foreground border-transparent hover:text-foreground hover:bg-surface-2",
        destructive:
          "bg-transparent text-destructive border-destructive/45 hover:bg-destructive/15",
        danger: "bg-transparent text-destructive border-destructive/45 hover:bg-destructive/15",
        link: "border-transparent text-accent underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-7 px-2.5 text-xs",
        md: "h-9 px-3.5",
        lg: "h-10 px-5",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, type, loading, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        type={asChild ? undefined : (type ?? "button")}
        disabled={asChild ? undefined : disabled || loading}
        aria-busy={loading || undefined}
        className={cn(buttonVariants({ variant, size }), className)}
        {...props}
      >
        {asChild ? (
          children
        ) : (
          <>
            {loading && <Loader2 className="animate-spin" aria-hidden />}
            {children}
          </>
        )}
      </Comp>
    );
  },
);
Button.displayName = "Button";

export function LinkButton({
  className,
  variant,
  size,
  ...props
}: React.AnchorHTMLAttributes<HTMLAnchorElement> & VariantProps<typeof buttonVariants>) {
  return (
    <a className={cn(buttonVariants({ variant, size }), "no-underline", className)} {...props} />
  );
}
