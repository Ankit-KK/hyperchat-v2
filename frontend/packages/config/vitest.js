import { defineConfig } from "vitest/config";

/**
 * Shared Vitest settings. `environment: "jsdom"` for components, "node" for plain modules.
 * JSX is compiled by Vite's esbuild with the automatic runtime, so no React plugin is needed.
 */
export function vitestPreset({ environment = "node", setupFiles = [] } = {}) {
  return defineConfig({
    esbuild: { jsx: "automatic" },
    test: {
      environment,
      setupFiles,
      include: ["src/**/*.test.{ts,tsx}", "**/*.test.{ts,tsx}"],
      exclude: ["**/node_modules/**", "**/.next/**"],
      restoreMocks: true,
    },
  });
}
