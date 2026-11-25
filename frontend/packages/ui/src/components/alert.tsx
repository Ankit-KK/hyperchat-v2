import type { ComponentProps } from "react";
import { cn } from "../cn";

export type AlertProps = ComponentProps<"div"> & { tone?: "info" | "error" };

/** A message box. "error" uses role="alert" (announced at once); "info" uses role="status" (announced politely). */
export function Alert({ className, tone = "info", ...props }: AlertProps) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "rounded-md border p-4 text-sm",
        tone === "error" ? "border-destructive text-destructive" : "border-border bg-muted",
        className,
      )}
      {...props}
    />
  );
}
