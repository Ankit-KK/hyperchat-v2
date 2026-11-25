import type { ComponentProps } from "react";
import { cn } from "../cn";

export type InputProps = ComponentProps<"input"> & { invalid?: boolean };

/** A text input. `invalid` sets aria-invalid; pair it with a message referenced by aria-describedby. */
export function Input({ className, invalid, ...props }: InputProps) {
  return (
    <input
      aria-invalid={invalid || undefined}
      className={cn(
        "h-10 w-full rounded-md border border-border bg-background px-3 text-sm",
        "placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring",
        "disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-destructive",
        className,
      )}
      {...props}
    />
  );
}
