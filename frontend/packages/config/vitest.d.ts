import type { UserConfig } from "vitest/config";

export function vitestPreset(options?: { environment?: "node" | "jsdom"; setupFiles?: string[] }): UserConfig;
